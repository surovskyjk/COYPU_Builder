"""Localhost WebSocket IPC server (ADR 0003): one `Session` per connection, envelopes dispatched by method."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid

import msgspec
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.protocol.messages import METHODS, Envelope, ErrorCode, ErrorInfo
from coypu_builder.server.codec import decode_frame, encode_frame, pack_blobs
from coypu_builder.server.handlers import DISPATCH, ProtocolError
from coypu_builder.server.session import MAX_EXECUTING, Session

log = logging.getLogger(__name__)

MUTATES = {spec.name: spec.mutates for spec in METHODS}
MAX_PENDING = 64  # coroutine requests of one connection waiting for an execution slot; more is E_BUSY


def _error_frame(request_id: str, method: str, code: ErrorCode, message: str) -> bytes:
    envelope = Envelope(
        v=PROTOCOL_VERSION, id=request_id, type="err", method=method, error=ErrorInfo(code, message)
    )
    return encode_frame(envelope)


async def _dispatch(session: Session, envelope: Envelope) -> bytes:
    """One request to one reply frame. A handler's own refusals are `ProtocolError`s; anything else it raises
    becomes `E_INTERNAL` for this request id and is logged (type and message, never the params, which can
    hold a session token), so the connection stays up and the client is not left to time out."""
    handler = DISPATCH.get(envelope.method)
    if handler is None:
        return _error_frame(
            envelope.id, envelope.method, ErrorCode.UNKNOWN_METHOD, f"unknown method '{envelope.method}'"
        )
    try:
        outcome = handler(session, envelope.params)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        result, blobs = outcome
        refs, tail = pack_blobs(blobs)
        response = Envelope(
            v=PROTOCOL_VERSION, id=envelope.id, type="res", method=envelope.method, result=result, blobs=refs
        )
        return encode_frame(response, tail)
    except ProtocolError as exc:
        return _error_frame(envelope.id, envelope.method, exc.code, exc.message)
    except Exception as exc:
        log.error(
            "handler %s failed: %s: %s", envelope.method, type(exc).__name__, exc, exc_info=exc.__traceback__
        )
        return _error_frame(envelope.id, envelope.method, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")


async def _respond(
    ws: ServerConnection,
    session: Session,
    envelope: Envelope,
    after: asyncio.Future | None,
    finished: asyncio.Future | None,
) -> None:
    """One request as a task: wait for the barrier it is ordered after (if still pending), run, and, if it
    changes state, release the barrier it stands for."""
    try:
        if after is not None:
            await after
        await ws.send(await _dispatch(session, envelope))
    except ConnectionClosed:
        pass
    finally:
        if finished is not None and not finished.done():
            finished.set_result(None)


async def _handle_connection(ws: ServerConnection, token: str | None) -> None:
    """Per-connection sequential consistency: a request sees every state change made by requests sent before
    it on this connection and none made by requests sent after it; `session.ping` is answered at once.

    The dispatcher keeps a chain of write barriers. A request that changes state (`MethodSpec.mutates`) waits
    for the previous barrier, runs, and becomes the new barrier; any other request waits for the latest
    barrier that arrived before it and then takes its snapshot. Requests between two barriers may run
    concurrently. Waiting happens inside the request's own task, never in the read loop.

    A request that has to wait, or whose handler is a coroutine (blocking work runs off the event loop, ADR
    0003), is an *eager* task: it runs synchronously up to its first real suspension, so its session, project
    and parameter checks and its snapshot happen now, before the next frame is read. Anything else runs
    inline. At most `MAX_EXECUTING` + `MAX_PENDING` coroutine requests are in flight per connection
    (`MAX_EXECUTING` of them in their blocking phase, see `Session.gate`); more get `E_BUSY`."""
    session = Session(session_id=str(uuid.uuid4()), expected_token=token)
    running: set[asyncio.Task] = set()
    coroutine_requests: set[asyncio.Task] = set()
    barrier: asyncio.Future | None = None
    try:
        async for raw in ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            try:
                envelope, _tail = decode_frame(bytes(raw))
            except (msgspec.DecodeError, ValueError) as exc:
                await ws.send(_error_frame("", "", ErrorCode.BAD_FRAME, str(exc)))
                continue
            handler = DISPATCH.get(envelope.method)
            if handler is None or envelope.method == "session.ping":
                await ws.send(await _dispatch(session, envelope))
                continue
            is_coroutine = inspect.iscoroutinefunction(handler)
            after = barrier if barrier is not None and not barrier.done() else None
            if after is None and not is_coroutine:
                await ws.send(await _dispatch(session, envelope))
                continue
            if is_coroutine and len(coroutine_requests) >= MAX_EXECUTING + MAX_PENDING:
                await ws.send(
                    _error_frame(
                        envelope.id,
                        envelope.method,
                        ErrorCode.BUSY,
                        f"{len(coroutine_requests)} long requests are already running or waiting here",
                    )
                )
                continue
            finished = asyncio.get_running_loop().create_future() if MUTATES[envelope.method] else None
            task = asyncio.Task(
                _respond(ws, session, envelope, after, finished),
                loop=asyncio.get_running_loop(),
                eager_start=True,
            )
            running.add(task)
            task.add_done_callback(running.discard)
            if is_coroutine:
                coroutine_requests.add(task)
                task.add_done_callback(coroutine_requests.discard)
            if finished is not None:
                barrier = finished
    except ConnectionClosed:
        pass
    finally:
        for task in list(running):
            task.cancel()


async def serve(
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    *,
    allow_no_token: bool = False,
) -> Server:
    """Start accepting connections; the returned `Server` is already listening (awaitable, use as a context
    manager or call `.close()` / `.wait_closed()` to stop).

    `token` is required and non-empty. The only way to run without one is the Python test API
    (`token=None, allow_no_token=True`); the command line cannot. A handshake carrying any `Origin` header
    (a browser) is refused with HTTP 403 before a single message is read; one without it is accepted."""
    if token is None:
        if not allow_no_token:
            raise ValueError(
                "serve() needs a session token; no-token mode is for tests (allow_no_token=True)"
            )
    elif not token:
        raise ValueError("serve() refuses an empty token")
    elif allow_no_token:
        raise ValueError("allow_no_token=True contradicts an explicit token")

    async def connection_handler(ws: ServerConnection) -> None:
        await _handle_connection(ws, token)

    return await ws_serve(connection_handler, host, port, origins=[None])
