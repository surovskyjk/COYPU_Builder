"""ADR 0003: `protocol.messages.METHODS` is the single source of truth for the wire method table.

Checks that the registry and `DISPATCH` cannot drift apart, that the generated docs are checked in fresh,
and that `session.ping` behaves as specified.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import msgspec
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.protocol.messages import METHODS, Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame
from coypu_builder.server.handlers import DISPATCH

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
GENERATOR_PATH = REPO_ROOT / "tools" / "gen_protocol_docs.py"
DOCS_PATH = REPO_ROOT / "docs" / "protocol" / "ipc.md"
SERVER_SOURCE_FILES = (
    BACKEND_ROOT / "src" / "coypu_builder" / "server" / "handlers.py",
    BACKEND_ROOT / "src" / "coypu_builder" / "server" / "app.py",
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_protocol_docs", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_methods_and_dispatch_have_identical_key_sets():
    assert {spec.name for spec in METHODS} == set(DISPATCH)


def test_method_params_and_result_are_structs_or_none():
    for spec in METHODS:
        for field_name, kind in (("params", spec.params), ("result", spec.result)):
            assert kind is None or (isinstance(kind, type) and issubclass(kind, msgspec.Struct)), (
                f"{spec.name}.{field_name} must be a msgspec.Struct subclass or None, got {kind!r}"
            )


def test_docs_are_fresh():
    generator = _load_generator()
    generated = generator.render()
    on_disk = DOCS_PATH.read_text(encoding="utf-8")
    assert generated == on_disk, "docs/protocol/ipc.md is stale; regenerate with tools/gen_protocol_docs.py"


def test_docs_generation_is_deterministic():
    generator = _load_generator()
    assert generator.render() == generator.render()


def test_no_bare_error_code_string_literals():
    pattern = re.compile(r'"E_[A-Z_]+"')
    for path in SERVER_SOURCE_FILES:
        matches = pattern.findall(path.read_text(encoding="utf-8"))
        assert not matches, f"{path} still has bare error-code string literals: {matches}"


async def _call(
    ws, request_id: str, method: str, params: dict[str, Any] | None = None
) -> tuple[Envelope, bytes]:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())


async def test_session_ping_round_trip():
    srv = await serve(host="127.0.0.1", port=0)
    try:
        port = srv.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            before_hello, _ = await _call(ws, "1", "session.ping", {"client_time_ms": 42})
            assert before_hello.type == "err"
            assert before_hello.error.code == "E_NO_SESSION"

            hello, _ = await _call(ws, "2", "session.hello", {"client": "pytest", "client_version": "0"})
            assert hello.type == "res"

            ping, _ = await _call(ws, "3", "session.ping", {"client_time_ms": 12345})
            assert ping.type == "res", ping.error
            assert ping.result["client_time_ms"] == 12345
            assert isinstance(ping.result["server_time_ms"], int)
            assert ping.result["server_time_ms"] > 0
    finally:
        srv.close()
        await srv.wait_closed()
