"""F50 (T-148): a composition whose stack holds a streamed layer waits for the network, so it passes through
`session.net_gate`, not the CPU gate. A slow service then does not hold the slots that terrain bakes need.

The project is the one of `test_imagery.py: test_ping_answers_while_tiles_compose`. Its synthetic image is
made a streamed layer by two monkeypatches, and every composition is held on a `threading.Event` until the
test releases it. Nothing touches the network.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import numpy as np
from websockets.asyncio.client import connect

import test_imagery
from coypu_builder.domain.imagery import ImageryRaster, RasterLayer
from coypu_builder.io.imagery import layer_for_payload
from coypu_builder.io.imagery.cuzk_wmts import LAYER, PIXEL_PER_SCALE_M, TileMatrix, WmtsLayer, WmtsSource
from coypu_builder.server import handlers
from coypu_builder.server.session import Session
from test_imagery import (
    TILE_N,
    TILE_W,
    _import,
    _list,
    _open,
    _recv,
    _send,
    write_bands,
    write_terrain,
)

# the fixtures of test_imagery.py, shared here; builtin_registry is autouse there, so it applies here too
server_url = test_imagery.server_url
builtin_registry = test_imagery.builtin_registry

_DEADLINE_S = 15.0  # a generous bound for a state the server reaches within milliseconds
_SAFETY_S = 60.0  # a blocked composition gives up after this, so a failing test cannot hang the suite
_POLL_S = 0.01


class _Streamed:
    """A local layer that reports itself as network-bound, and passes everything else through."""

    network_bound = True

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.source_id = inner.source_id
        self.resolution_m = inner.resolution_m

    def bounds(self):
        return self._inner.bounds()

    def render(self, bounds, size):
        return self._inner.render(bounds, size)


class _Blocked:
    """Holds every `compose_tile` call on `release` and counts the calls in progress, under a lock."""

    def __init__(self, original) -> None:
        self.release = threading.Event()
        self._original = original
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0

    def compose(self, layers, stack, heights_range, level, ix, iy):
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
        try:
            self.release.wait(_SAFETY_S)
            return self._original(layers, stack, heights_range, level, ix, iy)
        finally:
            with self._lock:
                self._running -= 1

    def running(self) -> int:
        with self._lock:
            return self._running


def _slow_stream(monkeypatch) -> _Blocked:
    original_layer = handlers.layer_for_payload

    def streamed(source_id: str, payload: object) -> _Streamed:
        return _Streamed(original_layer(source_id, payload))

    blocked = _Blocked(handlers.compose_tile)
    monkeypatch.setattr(handlers, "layer_for_payload", streamed)
    monkeypatch.setattr(handlers, "compose_tile", blocked.compose)
    return blocked


async def _streamed_project(ws, tmp_path: Path) -> dict[str, Any]:
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 8.0, (128, 256), slope=0.0)
    imagery = write_bands(
        tmp_path / "ortho.tif",
        [np.full((512, 1024), value, np.uint8) for value in (100, 150, 200)],
        TILE_W,
        TILE_N,
        2.0,
    )
    await _open(ws)
    await _import(ws, "t", terrain, "terrain")
    await _import(ws, "i", imagery, "imagery")
    listed = await _list(ws, "l")
    assert len(listed["tiles"]) == 8
    return listed


async def _until(predicate, what: str) -> None:
    try:
        async with asyncio.timeout(_DEADLINE_S):
            while not predicate():
                await asyncio.sleep(_POLL_S)
    except TimeoutError as exc:
        raise AssertionError(f"timed out waiting for {what}") from exc


async def test_slow_streamed_imagery_does_not_hold_the_cpu_gate(server_url, tmp_path, monkeypatch):
    blocked = _slow_stream(monkeypatch)
    try:
        async with connect(server_url) as ws:
            listed = await _streamed_project(ws, tmp_path)
            keys = [(t["ix"], t["iy"]) for t in listed["tiles"]]
            for number, (ix, iy) in enumerate(keys[:6]):
                await _send(
                    ws,
                    f"c{number}",
                    "imagery.tile",
                    {"level": 0, "ix": ix, "iy": iy, "revision": listed["revision"]},
                )
            await _until(lambda: blocked.running() >= 4, "four compositions to block")
            # no request has asked for this tile, so the terrain.tile below has to bake it under the CPU gate
            ix, iy = keys[6]
            await _send(
                ws,
                "t",
                "terrain.tile",
                {"level": 0, "ix": ix, "iy": iy, "revision": listed["terrain_revision"]},
            )
            terrain_reply, _tail, _size = await _recv(ws, timeout=2.0)
            assert terrain_reply.id == "t" and terrain_reply.type == "res", terrain_reply.error
            assert blocked.running() == 4 and not blocked.release.is_set()
            blocked.release.set()
            for _ in range(6):
                reply, _tail, _size = await _recv(ws)
                assert reply.type == "res", reply.error
    finally:
        blocked.release.set()


async def test_streamed_compositions_are_bounded_by_the_network_gate(server_url, tmp_path, monkeypatch):
    blocked = _slow_stream(monkeypatch)
    try:
        async with connect(server_url) as ws:
            listed = await _streamed_project(ws, tmp_path)
            for number, tile in enumerate(listed["tiles"]):
                await _send(
                    ws,
                    f"c{number}",
                    "imagery.tile",
                    {"level": 0, "ix": tile["ix"], "iy": tile["iy"], "revision": listed["revision"]},
                )
            await _until(lambda: blocked.running() >= 4, "four compositions to block")
            blocked.release.set()
            for _ in range(8):
                reply, _tail, _size = await _recv(ws)
                assert reply.type == "res", reply.error
    finally:
        blocked.release.set()
    # the peak is taken over the whole run: a network gate wider than 4 would let more than four run at once
    assert blocked.peak <= 4


def test_gate_for_picks_the_network_gate_only_for_network_bound_layers():
    session = Session(session_id="s", expected_token=None)
    raster = ImageryRaster(
        origin_e=TILE_W,
        origin_n=TILE_N,
        cell_e=1.0,
        cell_n=1.0,
        rgb=np.zeros((4, 4, 3), dtype=np.uint8),
        valid=np.ones((4, 4), dtype=bool),
        native_resolution_m=1.0,
    )
    local = RasterLayer("local", raster)
    streamed = _Streamed(RasterLayer("streamed", raster))
    assert session.net_gate is not session.gate
    assert handlers._gate_for(session, None) is session.gate
    assert handlers._gate_for(session, ()) is session.gate
    assert handlers._gate_for(session, (local,)) is session.gate
    assert handlers._gate_for(session, (local, local)) is session.gate
    assert handlers._gate_for(session, (local, streamed)) is session.net_gate
    assert handlers._gate_for(session, (streamed, local)) is session.net_gate


def test_wmts_layer_is_network_bound():
    matrix = TileMatrix("t", 1.0 / PIXEL_PER_SCALE_M, TILE_W, TILE_N, 256, 256, 4, 4)
    service = "https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS/tile/"
    source = WmtsSource(
        service + "{TileMatrix}/{TileRow}/{TileCol}",
        LAYER,
        "default028mm",
        (matrix,),
        (TILE_W, TILE_N - 1024.0, TILE_W + 1024.0, TILE_N),
    )
    layer = WmtsLayer("ortho", source)
    assert layer.network_bound is True
    assert layer_for_payload("ortho", source).network_bound is True
