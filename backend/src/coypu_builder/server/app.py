"""Localhost WebSocket IPC server (ADR 0003): one `Session` per connection, envelopes dispatched by method."""

from __future__ import annotations

import uuid

import msgspec
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.protocol.messages import Envelope, ErrorInfo
from coypu_builder.server.codec import decode_frame, encode_frame, pack_blobs
from coypu_builder.server.handlers import DISPATCH, ProtocolError
from coypu_builder.server.session import Session


def _error_frame(request_id: str, method: str, code: str, message: str) -> bytes:
    envelope = Envelope(
        v=PROTOCOL_VERSION, id=request_id, type="err", method=method, error=ErrorInfo(code, message)
    )
    return encode_frame(envelope)


def _dispatch(session: Session, envelope: Envelope) -> bytes:
    handler = DISPATCH.get(envelope.method)
    if handler is None:
        return _error_frame(
            envelope.id, envelope.method, "E_UNKNOWN_METHOD", f"unknown method '{envelope.method}'"
        )
    try:
        result, blobs = handler(session, envelope.params)
    except ProtocolError as exc:
        return _error_frame(envelope.id, envelope.method, exc.code, exc.message)
    refs, tail = pack_blobs(blobs)
    response = Envelope(
        v=PROTOCOL_VERSION, id=envelope.id, type="res", method=envelope.method, result=result, blobs=refs
    )
    return encode_frame(response, tail)


async def _handle_connection(ws: ServerConnection, token: str) -> None:
    session = Session(session_id=str(uuid.uuid4()), expected_token=token)
    try:
        async for raw in ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            try:
                envelope, _tail = decode_frame(bytes(raw))
            except (msgspec.DecodeError, ValueError) as exc:
                await ws.send(_error_frame("", "", "E_BAD_FRAME", str(exc)))
                continue
            await ws.send(_dispatch(session, envelope))
    except ConnectionClosed:
        pass


async def serve(host: str = "127.0.0.1", port: int = 0, token: str = "") -> Server:
    """Start accepting connections; the returned `Server` is already listening (awaitable, use as a context
    manager or call `.close()` / `.wait_closed()` to stop)."""

    async def connection_handler(ws: ServerConnection) -> None:
        await _handle_connection(ws, token)

    return await ws_serve(connection_handler, host, port)
