"""T-132 follow-up: how the server runs coroutine handlers (ADR 0003, long calls never block the loop).

Requests are begun in arrival order even when their work overlaps; every handler exception becomes an
`E_INTERNAL` reply for that request; one connection runs a bounded number of long requests. The fake handlers
here stand in for any slow method; the terrain-specific behaviour is in `test_terrain_tiles.py`.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import pytest
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.protocol.messages import METHODS, Envelope, ErrorCode
from coypu_builder.server import app as app_module
from coypu_builder.server import handlers, serve
from coypu_builder.server import session as session_module
from coypu_builder.server.codec import decode_frame, encode_frame
from fixtures.synthetic.terrain import sample_irregular


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _send(ws, request_id: str, method: str, params: dict[str, Any] | None = None) -> None:
    await ws.send(
        encode_frame(Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params))
    )


async def _collect(ws, count: int, timeout: float = 10.0) -> dict[str, Envelope]:
    replies: dict[str, Envelope] = {}
    async with asyncio.timeout(timeout):
        while len(replies) < count:
            envelope, _tail = decode_frame(await ws.recv())
            replies[envelope.id] = envelope
    return replies


HELLO = {"client": "pytest", "client_version": "0"}


# --- arrival order -----------------------------------------------------------------------------------------


async def test_a_pipelined_terrain_request_is_checked_before_the_hello_that_follows_it(server_url):
    async with connect(server_url) as ws:
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        await _send(ws, "hello", "session.hello", HELLO)
        await _send(ws, "new", "project.new", {"crs": "EPSG:5514"})
        replies = await _collect(ws, 3)
        assert (replies["tiles"].type, replies["tiles"].error.code) == ("err", "E_NO_SESSION")
        assert replies["hello"].type == "res" and replies["new"].type == "res"


async def test_a_pipelined_terrain_request_is_checked_before_the_project_that_follows_it(server_url):
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        await _send(ws, "tile", "terrain.tile", {"level": 0, "ix": 0, "iy": 0, "revision": 1})
        await _send(ws, "new", "project.new", {"crs": "EPSG:5514"})
        replies = await _collect(ws, 4)
        for request_id in ("tiles", "tile"):
            assert (replies[request_id].type, replies[request_id].error.code) == ("err", "E_NO_PROJECT")
        assert replies["new"].type == "res"


async def test_a_request_pipelined_behind_a_slow_one_still_runs_its_checks_first(server_url, monkeypatch):
    """The slow request is still in its blocking phase when the next ones arrive; those are evaluated
    against the state at their arrival, not after the slow one finishes."""

    async def slow(session, params):
        handlers._require_project(session)
        await asyncio.sleep(0.3)
        return {"done": True}, {}

    monkeypatch.setitem(handlers.DISPATCH, "run.list", slow)
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _send(ws, "slow", "run.list")  # no project yet: refused at once
        await _send(ws, "new", "project.new", {"crs": "EPSG:5514"})
        await _send(ws, "slow2", "run.list")  # now has a project: runs
        replies = await _collect(ws, 4)
        assert replies["slow"].error.code == "E_NO_PROJECT"
        assert replies["slow2"].type == "res" and replies["slow2"].result == {"done": True}


async def test_session_ping_is_answered_while_a_coroutine_handler_waits(server_url, monkeypatch):
    async def slow(session, params):
        await asyncio.sleep(0.5)
        return {}, {}

    monkeypatch.setitem(handlers.DISPATCH, "catalogue.vehicles", slow)
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _collect(ws, 1)
        await _send(ws, "slow", "catalogue.vehicles")
        started = time.perf_counter()
        await _send(ws, "ping", "session.ping", {})
        reply = (await _collect(ws, 1))["ping"]
        assert reply.id == "ping" and time.perf_counter() - started < 0.3


# --- exceptions --------------------------------------------------------------------------------------------


async def test_any_handler_exception_is_an_internal_error_for_that_request_only(
    server_url, monkeypatch, caplog
):
    def plain(session, params):
        raise KeyError("secret-looking-key")

    async def eventually(session, params):
        await asyncio.sleep(0.01)
        raise RuntimeError("after the first await")

    monkeypatch.setitem(handlers.DISPATCH, "run.list", plain)
    monkeypatch.setitem(handlers.DISPATCH, "catalogue.vehicles", eventually)
    with caplog.at_level(logging.ERROR, logger="coypu_builder.server.app"):
        async with connect(server_url) as ws:
            await _send(ws, "hello", "session.hello", HELLO)
            await _send(ws, "plain", "run.list")
            await _send(ws, "late", "catalogue.vehicles")
            await _send(ws, "ping", "session.ping", {})
            replies = await _collect(ws, 4)
            assert (replies["plain"].type, replies["plain"].error.code) == ("err", "E_INTERNAL")
            assert (replies["late"].type, replies["late"].error.code) == ("err", "E_INTERNAL")
            assert "RuntimeError" in replies["late"].error.message
            assert replies["ping"].type == "res"  # the connection survived both
            await _send(ws, "again", "session.ping", {})
            assert (await _collect(ws, 1))["again"].type == "res"
    messages = [r.getMessage() for r in caplog.records]
    assert any("handler run.list failed: KeyError" in m for m in messages)
    assert any("handler catalogue.vehicles failed: RuntimeError" in m for m in messages)


# --- bounds ------------------------------------------------------------------------------------------------


async def test_beyond_the_in_flight_limit_a_request_is_answered_busy(server_url, monkeypatch):
    async def slow(session, params):
        await asyncio.sleep(0.4)
        return {}, {}

    monkeypatch.setitem(handlers.DISPATCH, "catalogue.vehicles", slow)
    monkeypatch.setattr(app_module, "MAX_EXECUTING", 1)
    monkeypatch.setattr(app_module, "MAX_PENDING", 2)
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _collect(ws, 1)
        for number in range(5):
            await _send(ws, f"r{number}", "catalogue.vehicles")
        replies = await _collect(ws, 5)
        assert [replies[f"r{n}"].type for n in range(5)] == ["res", "res", "res", "err", "err"]
        assert {replies["r3"].error.code, replies["r4"].error.code} == {"E_BUSY"}
        await _send(ws, "after", "catalogue.vehicles")  # the slots are free again
        assert (await _collect(ws, 1))["after"].type == "res"


async def test_a_connection_runs_at_most_max_executing_blocking_jobs_at_once(server_url, monkeypatch):
    monkeypatch.setattr(session_module, "MAX_EXECUTING", 2)
    running = 0
    peak = 0
    lock = threading.Lock()

    def blocking():
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.15)
        with lock:
            running -= 1

    async def job(session, params):
        await handlers._offload(session, blocking)
        return {}, {}

    monkeypatch.setitem(handlers.DISPATCH, "catalogue.vehicles", job)
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _collect(ws, 1)
        for number in range(8):
            await _send(ws, f"j{number}", "catalogue.vehicles")
        replies = await _collect(ws, 8)
        assert all(r.type == "res" for r in replies.values())
    assert peak == 2


# --- per-connection sequential consistency (write barriers) -------------------------------------------------

KRALUPY_XML = Path(__file__).parent / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"


def _write_las(path: Path, points: np.ndarray) -> str:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.write(path)
    return str(path)


def _las(tmp_path: Path, name: str, bbox: tuple[float, float, float, float], seed: int) -> str:
    return _write_las(tmp_path / f"{name}.las", sample_irregular(bbox, 12.0, seed=seed))


A_BOX = (-747100.0, -1025700.0, -745900.0, -1024300.0)
B_BOX = (-745000.0, -1025700.0, -744200.0, -1025100.0)  # a different set of 512 m tiles
RUN = {"kind": "terrain", "settings": {"horizontal_crs": "EPSG:5514"}}


async def _open(ws, crs: str | None = "EPSG:5514") -> None:
    await _send(ws, "hello", "session.hello", HELLO)
    await _send(ws, "new", "project.new", {"crs": crs} if crs else {})
    replies = await _collect(ws, 2)
    assert replies["hello"].type == "res" and replies["new"].type == "res"


def _keys(reply: Envelope) -> set[tuple[int, int]]:
    return {(t["ix"], t["iy"]) for t in reply.result["tiles"]}


def _tile_of(bbox: tuple[float, float, float, float]) -> tuple[int, int]:
    return int(np.floor(bbox[0] / 512.0)), int(np.floor(bbox[1] / 512.0))


async def test_a_read_pipelined_after_an_import_sees_it(server_url):
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _send(ws, "new", "project.new", {})
        await _send(ws, "landxml", "import.landxml", {"path": str(KRALUPY_XML), "crs": "EPSG:5514"})
        await _send(ws, "get", "project.get")
        replies = await _collect(ws, 4)
        assert replies["landxml"].type == "res", replies["landxml"].error
        assert len(replies["get"].result["alignments"]) == 1
        assert replies["get"].result["crs"] is not None


async def test_two_pipelined_imports_and_a_read_after_them_see_both(server_url, tmp_path):
    a, b = _las(tmp_path, "A", A_BOX, 1), _las(tmp_path, "B", B_BOX, 2)
    async with connect(server_url) as ws:
        await _open(ws)
        await _send(ws, "a", "import.run", {"paths": [a], **RUN})
        await _send(ws, "b", "import.run", {"paths": [b], **RUN})
        await _send(ws, "list", "source.list")
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        replies = await _collect(ws, 4)
        assert replies["a"].type == replies["b"].type == "res"
        assert len(replies["list"].result["sources"]) == 2
        assert {_tile_of(A_BOX), _tile_of(B_BOX)} <= _keys(replies["tiles"])


async def test_terrain_tiles_pipelined_after_an_import_lists_the_new_source(server_url, tmp_path):
    a = _las(tmp_path, "A", A_BOX, 1)
    async with connect(server_url) as ws:
        await _open(ws)
        await _send(ws, "a", "import.run", {"paths": [a], **RUN})
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        replies = await _collect(ws, 2)
        assert _tile_of(A_BOX) in _keys(replies["tiles"])


async def test_a_read_sent_before_an_import_does_not_see_it(server_url, tmp_path, monkeypatch):
    a, b = _las(tmp_path, "A", A_BOX, 1), _las(tmp_path, "B", B_BOX, 2)
    real = handlers._build_terrain

    def slow(snapshot):
        time.sleep(0.5)
        return real(snapshot)

    monkeypatch.setattr(handlers, "_build_terrain", slow)
    async with connect(server_url) as ws:
        await _open(ws)
        await _send(ws, "a", "import.run", {"paths": [a], **RUN})
        await _collect(ws, 1)
        await _send(ws, "before", "source.list")
        await _send(ws, "slow", "terrain.tiles", {"level": 0})  # still building when the import arrives
        await _send(ws, "b", "import.run", {"paths": [b], **RUN})
        await _send(ws, "after", "source.list")
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        replies = await _collect(ws, 5)
        assert len(replies["before"].result["sources"]) == 1
        assert len(replies["after"].result["sources"]) == 2
        assert _tile_of(B_BOX) not in _keys(replies["slow"])
        assert _tile_of(B_BOX) in _keys(replies["tiles"])
        assert replies["slow"].result["revision"] != replies["tiles"].result["revision"]


async def test_a_pipelined_import_depends_on_the_alignment_import_before_it(server_url, tmp_path):
    """A corridor-clipped import needs the project CRS and the alignment that `import.landxml` provides."""
    data = _las(tmp_path, "A", (-747650.0, -1026200.0, -744850.0, -1023800.0), 3)
    settings = {**RUN["settings"], "clip": {"type": "corridor", "buffer_m": 250.0}}
    async with connect(server_url) as ws:
        await _open(ws, crs=None)
        await _send(ws, "landxml", "import.landxml", {"path": str(KRALUPY_XML), "crs": "EPSG:5514"})
        await _send(ws, "run", "import.run", {"paths": [data], "kind": "terrain", "settings": settings})
        await _send(ws, "tiles", "terrain.tiles", {"level": 0})
        replies = await _collect(ws, 3)
        assert replies["landxml"].type == "res", replies["landxml"].error
        assert replies["run"].type == "res", replies["run"].error
        assert replies["run"].result["added"] == 1
        assert replies["tiles"].result["tiles"]


async def test_ping_is_answered_at_once_while_an_import_runs(server_url, tmp_path, monkeypatch):
    a = _las(tmp_path, "A", A_BOX, 1)
    real = handlers._run_import_with_coverage

    def slow(*args):
        time.sleep(1.0)
        return real(*args)

    monkeypatch.setattr(handlers, "_run_import_with_coverage", slow)
    async with connect(server_url) as ws:
        await _open(ws)
        await _send(ws, "run", "import.run", {"paths": [a], **RUN})
        await _send(ws, "list", "source.list")  # waits for the import
        latencies = []
        for number in range(5):
            sent = time.perf_counter()
            await _send(ws, f"ping{number}", "session.ping", {})
            reply = (await _collect(ws, 1))[f"ping{number}"]
            assert reply.type == "res"
            latencies.append(time.perf_counter() - sent)
            await asyncio.sleep(0.1)
        assert max(latencies) < 0.2, latencies
        replies = await _collect(ws, 2)
        assert replies["run"].type == "res" and len(replies["list"].result["sources"]) == 1


async def test_a_failed_barrier_still_releases_the_requests_behind_it(server_url):
    async with connect(server_url) as ws:
        await _send(ws, "hello", "session.hello", HELLO)
        await _send(ws, "new", "project.new", {"crs": "EPSG:5514"})
        await _send(ws, "bad", "import.landxml", {"path": "/no/such/file.xml"})
        await _send(ws, "get", "project.get")
        replies = await _collect(ws, 4)
        assert replies["bad"].error.code == "E_NOT_FOUND"
        assert replies["get"].type == "res"


def test_the_method_registry_says_what_changes_state_and_what_can_be_busy():
    mutating = {spec.name for spec in METHODS if spec.mutates}
    assert mutating == {
        "session.hello",
        "project.new",
        "import.landxml",
        "import.coypu",
        "import.kinematics",
        "import.run",
        "trainset.create",
    }
    for spec in METHODS:
        if inspect.iscoroutinefunction(handlers.DISPATCH[spec.name]):
            assert ErrorCode.BUSY in spec.errors, spec.name
        else:
            assert ErrorCode.BUSY not in spec.errors, spec.name
