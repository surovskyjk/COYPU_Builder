"""T-132: terrain display tiles -- the tile grid, the heightfield bake, the paged `terrain.*` methods.

Domain-level tests bake tiles straight from a `TerrainStack`; the last group imports LAS sheets over a real
loopback WebSocket and pages the tiles the way the client does: list, then fetch each.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import pytest
import shapely
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION, config
from coypu_builder.domain.crs import BasePoint, points_to_godot
from coypu_builder.domain.model.ids import new_id
from coypu_builder.domain.sources import FileOrigin, ImportSettings, SourceDataset, SourceKind
from coypu_builder.domain.terrain import TerrainGrid
from coypu_builder.domain.terrain_surface import GridSurface, TerrainStack, tin_from_points
from coypu_builder.io.mesh.terrain_tiles import (
    HEIGHT_QUANTUM_M,
    MAX_LEVEL,
    SAMPLES,
    bake_tile,
    lattice,
    tile_bounds,
    tile_edge,
    tiles_for_coverage,
)
from coypu_builder.io.sources import registry
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame, unpack_blob
from coypu_builder.server.session import ProjectState, SourceEntry, TerrainCache
from fixtures.synthetic.terrain import sample_irregular, surface

BOX = (-747000.0, -1026000.0, -745800.0, -1024400.0)  # 1.2 x 1.6 km: a handful of L0 tiles, some whole
SHEET_BOX = (-747100.0, -1025700.0, -745900.0, -1024300.0)  # 1.2 x 1.4 km: holds whole L0 tiles
MAX_RESPONSE_BYTES = 1_000_000


@pytest.fixture(scope="module")
def stack() -> TerrainStack:
    return TerrainStack((tin_from_points(sample_irregular(BOX, 6.0, seed=41)),))


# --- the grid --------------------------------------------------------------------------------------------


def test_tile_edges_and_corners_follow_the_level():
    assert [tile_edge(level) for level in range(MAX_LEVEL + 1)] == [512.0, 1024.0, 2048.0, 4096.0, 8192.0]
    assert tile_bounds(0, 3, -2) == (1536.0, -1024.0, 2048.0, -512.0)
    assert tile_bounds(2, -1, 0) == (-2048.0, 0.0, 0.0, 2048.0)
    for bad in (-1, MAX_LEVEL + 1):
        with pytest.raises(ValueError):
            tile_edge(bad)


@pytest.mark.parametrize("level", range(MAX_LEVEL + 1))
def test_neighbouring_lattices_share_their_edge_exactly(level):
    e, n = lattice(level, -3, 5)
    edge = tile_edge(level)
    assert e.shape == n.shape == (SAMPLES, SAMPLES)
    assert e[0, 0] == -3 * edge and n[0, 0] == 5 * edge
    assert e[0, 1] - e[0, 0] == edge / 128.0
    east_e, east_n = lattice(level, -2, 5)
    assert np.array_equal(e[:, -1], east_e[:, 0]) and np.array_equal(n[:, -1], east_n[:, 0])
    north_e, north_n = lattice(level, -3, 6)
    assert np.array_equal(n[-1, :], north_n[0, :]) and np.array_equal(e[-1, :], north_e[0, :])
    assert (lattice(0, 0, 0)[0][0, 1] - lattice(0, 0, 0)[0][0, 0]) == 4.0  # 4 m at L0
    assert (lattice(4, 0, 0)[0][0, 1] - lattice(4, 0, 0)[0][0, 0]) == 64.0  # 64 m at L4


def _u_shape() -> shapely.Polygon:
    e0, n0 = -746900.0, -1025300.0
    return shapely.Polygon(
        [
            (e0, n0),
            (e0 + 900.0, n0),
            (e0 + 900.0, n0 + 900.0),
            (e0 + 700.0, n0 + 900.0),
            (e0 + 700.0, n0 + 150.0),
            (e0 + 200.0, n0 + 150.0),
            (e0 + 200.0, n0 + 900.0),
            (e0, n0 + 900.0),
        ]
    )


@pytest.mark.parametrize("level", range(MAX_LEVEL + 1))
def test_the_tile_list_covers_the_polygon_and_lists_no_tile_wholly_outside(level):
    polygon = _u_shape()
    keys = tiles_for_coverage(level, [polygon])
    assert keys == sorted(set(keys), key=lambda k: (k[1], k[0]))
    squares = {key: shapely.box(*tile_bounds(level, *key)) for key in keys}
    for key, square in squares.items():
        assert shapely.intersects(square, polygon), key
    rng = np.random.default_rng(level)
    e = rng.uniform(polygon.bounds[0], polygon.bounds[2], 4000)
    n = rng.uniform(polygon.bounds[1], polygon.bounds[3], 4000)
    inside = shapely.contains_xy(polygon, e, n)
    edge = tile_edge(level)
    owners = {
        (int(np.floor(x / edge)), int(np.floor(y / edge))) for x, y in zip(e[inside], n[inside], strict=True)
    }
    assert owners <= set(keys)


def test_a_bounds_box_lists_exactly_the_tiles_it_overlaps_and_not_the_one_it_only_touches():
    assert tiles_for_coverage(0, [(0.0, 0.0, 512.0, 512.0)]) == [(0, 0)]
    assert tiles_for_coverage(0, [(0.0, 0.0, 512.5, 512.0)]) == [(0, 0), (1, 0)]
    assert tiles_for_coverage(0, [(-1.0, -1.0, 1.0, 1.0)]) == [(-1, -1), (0, -1), (-1, 0), (0, 0)]
    assert tiles_for_coverage(0, []) == []
    union = tiles_for_coverage(0, [(0.0, 0.0, 600.0, 100.0), (0.0, 0.0, 100.0, 600.0)])
    assert union == [(0, 0), (1, 0), (0, 1)]


# --- the bake --------------------------------------------------------------------------------------------


def test_tile_heights_equal_the_analysis_surface_at_the_lattice_points(stack):
    key = tiles_for_coverage(0, [BOX])[3]
    tile = bake_tile(stack, 0, *key)
    e, n = lattice(0, *key)
    want = stack.heights(e, n)
    got = tile.absolute_heights()
    assert tile.heights.dtype == np.float32 and tile.heights.shape == (129, 129)
    assert np.array_equal(np.isnan(got), np.isnan(want))
    assert np.nanmax(np.abs(got - want)) <= 1e-3
    assert np.nanmax(np.abs(got - want)) <= HEIGHT_QUANTUM_M / 2.0 + 1e-12
    assert tile.valid_fraction == pytest.approx(np.isfinite(want).mean())
    assert tile.origin[:2] == (key[0] * 512.0, key[1] * 512.0)
    assert tile.origin[2] == np.nanmin(got) and np.nanmin(tile.heights) == 0.0


def test_the_origin_is_the_minimum_valid_height_and_the_values_are_relative_to_it(stack):
    key = tiles_for_coverage(0, [BOX])[0]
    tile = bake_tile(stack, 0, *key)
    assert np.nanmin(tile.heights) == 0.0
    assert 0.0 < np.nanmax(tile.heights) < 50.0  # relief of the tile, not an absolute height near 178 m


def test_tiles_beyond_the_data_are_nan_and_a_tile_with_no_data_has_origin_height_zero(stack):
    far = bake_tile(stack, 0, 100, 100)
    assert np.isnan(far.heights).all() and far.valid_fraction == 0.0 and far.origin[2] == 0.0
    partial = [bake_tile(stack, 0, *k) for k in tiles_for_coverage(0, [stack.bounds()])]
    assert any(0.0 < t.valid_fraction < 1.0 for t in partial)
    assert any(t.valid_fraction == 1.0 for t in partial)


def test_shared_edges_decode_to_bit_identical_heights(stack):
    keys = set(tiles_for_coverage(0, [stack.bounds()]))
    tiles = {k: bake_tile(stack, 0, *k) for k in keys}
    pairs = 0
    for (ix, iy), tile in tiles.items():
        east = tiles.get((ix + 1, iy))
        if east is not None:
            a, b = tile.absolute_heights()[:, -1], east.absolute_heights()[:, 0]
            assert np.array_equal(a, b, equal_nan=True)
            pairs += 1
        north = tiles.get((ix, iy + 1))
        if north is not None:
            a, b = tile.absolute_heights()[-1, :], north.absolute_heights()[0, :]
            assert np.array_equal(a, b, equal_nan=True)
            pairs += 1
    assert pairs >= 4


def test_float32_holds_every_stored_value_exactly_below_4096_m_of_relief(stack):
    tile = bake_tile(stack, 0, *tiles_for_coverage(0, [BOX])[1])
    scaled = tile.heights.astype(np.float64) / HEIGHT_QUANTUM_M
    assert np.array_equal(scaled[np.isfinite(scaled)], np.round(scaled[np.isfinite(scaled)]))


def test_coarse_levels_sample_the_same_surface_at_wider_spacing(stack):
    tile = bake_tile(stack, 3, *tiles_for_coverage(3, [BOX])[0])
    e, n = lattice(3, tile.ix, tile.iy)
    want = stack.heights(e, n)
    got = tile.absolute_heights()
    assert np.array_equal(np.isnan(got), np.isnan(want)) and np.isfinite(got).any()
    assert np.nanmax(np.abs(got - want)) <= 1e-3 and tile.edge_m == 4096.0


def test_a_grid_source_bakes_to_the_bilinear_values_at_its_lattice_points():
    """4 m cells on the 4 m L0 lattice: every interior lattice point is a cell corner, where bilinear
    interpolation between centres is the mean of the four cells that meet there."""
    rows = cols = 128
    heights = np.random.default_rng(3).uniform(100.0, 130.0, (rows, cols))
    surf = GridSurface(TerrainGrid(0.0, 512.0, 4.0, 4.0, heights))
    tile = bake_tile(surf, 0, 0, 0)
    assert tile.valid_fraction == 1.0
    top, bottom = heights[:127][::-1], heights[1:128][::-1]  # the cell rows above and below lattice row r
    mean_of_four = 0.25 * (top[:, :-1] + top[:, 1:] + bottom[:, :-1] + bottom[:, 1:])
    assert np.abs(tile.absolute_heights()[1:128, 1:128] - mean_of_four).max() <= HEIGHT_QUANTUM_M


# --- the cache -------------------------------------------------------------------------------------------


def test_the_tile_cache_is_a_bounded_lru_keyed_by_revision(stack):
    cache = TerrainCache(max_tiles=2)
    tiles = [bake_tile(stack, 0, *k) for k in tiles_for_coverage(0, [BOX])[:3]]
    cache.put_tile(tiles[0])
    cache.put_tile(tiles[1])
    assert cache.get_tile(0, tiles[0].ix, tiles[0].iy) is tiles[0]  # makes 0 the most recent
    cache.put_tile(tiles[2])
    assert cache.get_tile(0, tiles[1].ix, tiles[1].iy) is None
    assert cache.get_tile(0, tiles[0].ix, tiles[0].iy) is tiles[0]
    assert len(cache.tiles) == 2 and isinstance(cache.tiles, OrderedDict)
    assert all(key[0] == cache.revision for key in cache.tiles)
    assert (0, tiles[1].ix, tiles[1].iy) in cache.meta  # the listing metadata outlives the eviction
    assert TerrainCache().revision != TerrainCache().revision
    assert TerrainCache().max_tiles == 512


# --- the wire --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def builtin_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "user-config" / "config.toml")
    register_builtin_readers()


def _write_las(path: Path, points: np.ndarray) -> Path:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.write(path)
    return path


async def _recv(ws, timeout: float = 10.0):
    """The next frame, decoded; a reply that never comes fails the test instead of hanging it."""
    async with asyncio.timeout(timeout):
        frame = await ws.recv()
    envelope, tail = decode_frame(frame)
    return envelope, tail, len(frame)


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None):
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return await _recv(ws)


async def _collect(ws, count: int) -> dict[str, Envelope]:
    replies: dict[str, Envelope] = {}
    while len(replies) < count:
        envelope, _tail, _size = await _recv(ws)
        replies[envelope.id] = envelope
    return replies


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _open(ws, base_point: dict[str, float] | None = None) -> None:
    await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
    params: dict[str, Any] = {"crs": "EPSG:5514"}
    if base_point is not None:
        params["base_point"] = base_point
    assert (await _call(ws, "p", "project.new", params))[0].type == "res"


def _sheets(tmp_path: Path, seed: int = 7) -> list[str]:
    bbox = SHEET_BOX
    sample = sample_irregular(bbox, 8.0, seed=seed)
    half = (bbox[0] + bbox[2]) / 2.0
    parts = [sample[sample[:, 0] < half], sample[sample[:, 0] >= half]]
    return [str(_write_las(tmp_path / f"S{i}.las", part)) for i, part in enumerate(parts)]


async def _import(ws, request_id: str, paths: list[str], clip: dict | None = None, **extra):
    settings: dict[str, Any] = {"horizontal_crs": "EPSG:5514"}
    if clip is not None:
        settings["clip"] = clip
    reply, _tail, _size = await _call(
        ws, request_id, "import.run", {"paths": paths, "kind": "terrain", "settings": settings, **extra}
    )
    assert reply.type == "res", reply.error
    return reply.result


async def test_a_project_without_terrain_lists_nothing_and_has_no_tile(server_url):
    async with connect(server_url) as ws:
        await _open(ws)
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert listed.type == "res", listed.error
        assert listed.result["tiles"] == [] and listed.result["level"] == 0
        missing, _tail, _size = await _call(
            ws, "2", "terrain.tile", {"level": 0, "ix": 0, "iy": 0, "revision": listed.result["revision"]}
        )
        assert (missing.type, missing.error.code) == ("err", "E_NOT_FOUND")


async def test_list_then_fetch_each_tile_and_a_reimport_makes_a_stale_fetch_not_found(server_url, tmp_path):
    paths = _sheets(tmp_path)
    base = {"easting": -746800.0, "northing": -1025000.0, "height": 150.0}
    async with connect(server_url) as ws:
        await _open(ws, base)
        await _import(ws, "i1", paths)

        listed, _tail, list_size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert listed.type == "res", listed.error
        revision = listed.result["revision"]
        tiles = listed.result["tiles"]
        assert len(tiles) >= 4 and list_size < MAX_RESPONSE_BYTES
        assert all(t["edge_m"] == 512.0 and 0.0 <= t["valid_fraction"] <= 1.0 for t in tiles)
        assert any(t["valid_fraction"] == 1.0 for t in tiles)

        sizes = []
        for info in tiles:
            reply, tail, size = await _call(
                ws,
                f"t{info['ix']}_{info['iy']}",
                "terrain.tile",
                {"level": 0, "ix": info["ix"], "iy": info["iy"], "revision": revision},
            )
            assert reply.type == "res", reply.error
            sizes.append(size)
            heights = unpack_blob(tail, reply.blobs[0])
            assert reply.blobs[0].name == "heights" and reply.blobs[0].dtype == "<f4"
            assert heights.shape == (129, 129)
            assert reply.result["tile_origin"] == info["tile_origin"]
            assert reply.result["valid_fraction"] == info["valid_fraction"]
            assert reply.result["revision"] == revision
            expected = points_to_godot(
                np.array([[info["ix"] * 512.0, info["iy"] * 512.0, 0.0]]),
                BasePoint(base["easting"], base["northing"], base["height"]),
                dtype=np.float64,
            )[0]
            origin = info["tile_origin"]
            assert origin[0] == expected[0] and origin[2] == expected[2]
            rel_min = np.nanmin(heights) if np.isfinite(heights).any() else 0.0
            assert rel_min == 0.0
            if np.isfinite(heights).any():
                assert 100.0 < origin[1] + base["height"] < 260.0  # a plausible terrain height, not a relief
        assert max(sizes) < MAX_RESPONSE_BYTES
        assert (
            max(sizes) < 85_000
        )  # one L0 tile: 129 * 129 * (4 + 1) bytes (heights, source_index) plus the envelope

        second = await _import(ws, "i3", paths[:1])
        assert second["added"] == 1
        stale, _tail, _size = await _call(
            ws,
            "s",
            "terrain.tile",
            {"level": 0, "ix": tiles[0]["ix"], "iy": tiles[0]["iy"], "revision": revision},
        )
        assert (stale.type, stale.error.code) == ("err", "E_NOT_FOUND")
        relisted, _tail, _size = await _call(ws, "r", "terrain.tiles", {"level": 0})
        assert relisted.result["revision"] != revision
        fresh, _tail, _size = await _call(
            ws,
            "f",
            "terrain.tile",
            {"level": 0, "ix": tiles[0]["ix"], "iy": tiles[0]["iy"], "revision": relisted.result["revision"]},
        )
        assert fresh.type == "res", fresh.error
        assert fresh.result["revision"] == relisted.result["revision"]


async def test_the_revision_is_stable_until_the_sources_change(server_url, tmp_path):
    paths = _sheets(tmp_path)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", paths)
        first, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 1})
        second, _tail, _size = await _call(ws, "2", "terrain.tiles", {"level": 2})
        assert first.result["revision"] == second.result["revision"]
        assert first.result["tiles"] and second.result["tiles"]
        again, _tail, _size = await _call(ws, "3", "terrain.tiles", {"level": 1})
        assert again.result == first.result


async def test_tiles_not_in_the_list_bad_levels_and_bad_params_are_refused(server_url, tmp_path):
    paths = _sheets(tmp_path)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", paths)
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        revision = listed.result["revision"]
        outside, _tail, _size = await _call(
            ws, "2", "terrain.tile", {"level": 0, "ix": 5000, "iy": 5000, "revision": revision}
        )
        assert (outside.type, outside.error.code) == ("err", "E_NOT_FOUND")
        for number, (method, params) in enumerate(
            (
                ("terrain.tiles", {"level": 5}),
                ("terrain.tiles", {"level": -1}),
                ("terrain.tiles", {"level": "high"}),
                ("terrain.tile", {"level": 9, "ix": 0, "iy": 0, "revision": revision}),
                ("terrain.tile", {"level": 0, "ix": 0, "iy": 0}),
            )
        ):
            reply, _tail, _size = await _call(ws, f"b{number}", method, params)
            assert (reply.type, reply.error.code) == ("err", "E_BAD_PARAMS"), (method, params)


async def test_the_list_follows_the_import_clip_not_the_whole_sheet(server_url, tmp_path):
    paths = _sheets(tmp_path)
    clip = {"min_e": -746900.0, "min_n": -1025300.0, "max_e": -746500.0, "max_n": -1025000.0}
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", paths, clip={"type": "bbox", **clip})
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert listed.type == "res", listed.error
        keys = {(t["ix"], t["iy"]) for t in listed.result["tiles"]}
        edge = 512.0
        assert keys == {
            (ix, iy)
            for ix in range(int(np.floor(clip["min_e"] / edge)), int(np.ceil(clip["max_e"] / edge)))
            for iy in range(int(np.floor(clip["min_n"] / edge)), int(np.ceil(clip["max_n"] / edge)))
        }
        assert all(tile["valid_fraction"] > 0.0 for tile in listed.result["tiles"])


async def test_a_newer_source_takes_precedence_over_an_older_one(server_url, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    older = _sheets(tmp_path / "a", seed=1)
    sample = sample_irregular(SHEET_BOX, 8.0, seed=2)
    sample[:, 2] += 50.0
    newer = [str(_write_las(tmp_path / "b" / "UP.las", sample))]
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", older)
        await _import(ws, "i2", newer)
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        revision = listed.result["revision"]
        info = max(listed.result["tiles"], key=lambda t: t["valid_fraction"])
        assert info["valid_fraction"] == 1.0
        reply, tail, _size = await _call(
            ws, "2", "terrain.tile", {"level": 0, "ix": info["ix"], "iy": info["iy"], "revision": revision}
        )
        height = info["tile_origin"][1] + float(np.nanmin(unpack_blob(tail, reply.blobs[0])))
        e, n = lattice(0, info["ix"], info["iy"])
        assert height == pytest.approx(float(surface(e, n).min()) + 50.0, abs=0.5)


async def test_a_level_with_too_many_tiles_is_refused_with_a_hint(server_url, tmp_path, monkeypatch):
    from coypu_builder.server import handlers

    monkeypatch.setattr(handlers, "MAX_LISTED_TILES", 3)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", _sheets(tmp_path))
        crowded, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert (crowded.type, crowded.error.code) == ("err", "E_BAD_PARAMS")
        assert "coarser level" in crowded.error.message
        coarse, _tail, _size = await _call(ws, "2", "terrain.tiles", {"level": 4})
        assert coarse.type == "res" and 0 < len(coarse.result["tiles"]) <= 3


def test_a_source_set_directly_still_gets_a_new_cache_and_revision():
    dataset = SourceDataset(
        id=new_id(),
        kind=SourceKind.TERRAIN,
        name="direct",
        format_id="test",
        origin=FileOrigin((), ()),
        settings=ImportSettings(),
        declared_crs=None,
        effective_crs="EPSG:5514",
        transformation="none",
        transformation_accuracy_m=0.0,
        licence="",
        attribution="",
        imported_at="",
        tool_version="",
        objects=(),
    )
    project = ProjectState(project_id="p")
    first = project.terrain_cache()
    assert project.terrain_cache() is first
    project.sources[dataset.id] = SourceEntry(dataset, {})
    second = project.terrain_cache()
    assert second is not first and second.revision != first.revision
    project.invalidate_terrain()
    assert project.terrain_cache().revision not in (first.revision, second.revision)


def test_a_sources_clip_polygon_cuts_its_triangles_in_the_stack():
    from coypu_builder.domain.model.ids import EntityId
    from coypu_builder.domain.sources import ObjectRecord
    from coypu_builder.domain.terrain import TerrainPoints
    from coypu_builder.server import handlers

    def _stack_of(project):
        return handlers._build_terrain(handlers._terrain_snapshot(project))[0]

    e0, n0 = -747000.0, -1025400.0
    clip = shapely.Polygon(
        [
            (e0, n0),
            (e0 + 100, n0),
            (e0 + 100, n0 + 300),
            (e0 + 60, n0 + 300),
            (e0 + 60, n0 + 40),
            (e0 + 40, n0 + 40),
            (e0 + 40, n0 + 300),
            (e0, n0 + 300),
        ]
    )  # fmt: skip  -- two 40 m arms with a 20 m gap, which no edge limit would cut
    sample = sample_irregular(clip.bounds, 3.0, seed=5)
    cloud = sample[shapely.contains_xy(clip, sample[:, 0], sample[:, 1])]
    object_id = EntityId(new_id())

    def project_with(coverage) -> ProjectState:
        dataset = SourceDataset(
            id=new_id(), kind=SourceKind.TERRAIN, name="u", format_id="test", origin=FileOrigin((), ()),
            settings=ImportSettings(), declared_crs=None, effective_crs="EPSG:5514", transformation="none",
            transformation_accuracy_m=0.0, licence="", attribution="", imported_at="", tool_version="",
            objects=(ObjectRecord("u", object_id, "x"),),
        )  # fmt: skip
        project = ProjectState(project_id="p")
        project.sources[dataset.id] = SourceEntry(
            dataset, {object_id: TerrainPoints(cloud, None)}, (), coverage
        )
        return project

    probe_e, probe_n = np.full(50, e0 + 50.0), np.linspace(n0 + 70.0, n0 + 290.0, 50)  # the middle of the gap
    cut = _stack_of(project_with(clip)).heights(probe_e, probe_n)
    assert np.isnan(cut).all()
    assert np.isfinite(
        _stack_of(project_with(None)).heights(probe_e, probe_n)
    ).all()  # without the clip it is bridged
    assert np.isfinite(
        _stack_of(project_with(clip)).heights(np.array([e0 + 20.0]), np.array([n0 + 150.0]))
    ).all()


# --- long calls stay off the event loop (ADR 0003 addendum) --------------------------------------------


async def _send(ws, request_id: str, method: str, params: dict[str, Any]) -> None:
    await ws.send(
        encode_frame(Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params))
    )


async def _ping_while(ws, request_id: str, method: str, params: dict[str, Any], interval: float = 0.1):
    """Send one request without waiting for it, then ping every `interval` s until its reply arrives. Returns
    that reply and the round-trip time of every ping."""
    await _send(ws, request_id, method, params)
    reply = None
    latencies: list[float] = []
    number = 0
    while reply is None:
        number += 1
        ping_id = f"ping{number}"
        sent = time.perf_counter()
        await _send(ws, ping_id, "session.ping", {})
        answered = False
        while not answered:
            envelope, _tail, _size = await _recv(ws)
            if envelope.id == request_id:
                reply = envelope
            else:
                assert envelope.id == ping_id and envelope.type == "res", envelope
                latencies.append(time.perf_counter() - sent)
                answered = True
        if reply is None:
            await asyncio.sleep(interval)
    return reply, latencies


async def test_session_ping_keeps_answering_during_a_multi_second_terrain_build(server_url, tmp_path):
    """~600 000 points: Qhull alone takes several seconds. The client treats 3 missed 2 s pings as a lost
    connection, so every ping must come back well inside that, and the list must still be right."""
    bbox = (-747600.0, -1026200.0, -744400.0, -1023400.0)  # 3.2 x 2.8 km at 4.2 m
    sample = sample_irregular(bbox, 4.2, seed=12)
    assert len(sample) > 450_000
    path = str(_write_las(tmp_path / "big.las", sample))
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", [path])
        started = time.perf_counter()
        reply, latencies = await _ping_while(ws, "tiles", "terrain.tiles", {"level": 2})
        elapsed = time.perf_counter() - started
        assert reply.type == "res", reply.error
        assert elapsed > 1.5, f"the build was too quick ({elapsed:.1f} s) to show anything"
        assert len(latencies) >= 10 and max(latencies) < 1.0, (len(latencies), max(latencies))
        tiles = reply.result["tiles"]
        assert tiles and all(t["valid_fraction"] > 0.0 for t in tiles)
        edge = 2048.0
        assert {(t["ix"], t["iy"]) for t in tiles} == {
            (ix, iy)
            for ix in range(int(np.floor(bbox[0] / edge)), int(np.ceil(bbox[2] / edge)))
            for iy in range(int(np.floor(bbox[1] / edge)), int(np.ceil(bbox[3] / edge)))
        }
        detail, _tail, _size = await _call(
            ws, "d", "terrain.tile",
            {"level": 2, "ix": tiles[0]["ix"], "iy": tiles[0]["iy"], "revision": reply.result["revision"]},
        )  # fmt: skip
        assert detail.type == "res", detail.error


@pytest.fixture
def slow_build(monkeypatch):
    """Counts the blocking stack builds and makes each take `delay` s, as a long triangulation would."""
    from coypu_builder.server import handlers

    calls: list[int] = []
    real = handlers._build_terrain

    lock = threading.Lock()
    state = {"running": 0}

    def slow(snapshot):
        with lock:
            calls.append(len(snapshot))
            state["running"] += 1
            slow.peak = max(slow.peak, state["running"])
        try:
            time.sleep(slow.delay)
            return real(snapshot)
        finally:
            with lock:
                state["running"] -= 1

    slow.delay = 0.6
    slow.peak = 0
    slow.calls = calls
    monkeypatch.setattr(handlers, "_build_terrain", slow)
    return slow


async def test_concurrent_requests_for_one_revision_share_one_build(server_url, tmp_path, slow_build):
    paths = _sheets(tmp_path)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", paths)
        for number, level in enumerate((0, 1, 2)):
            await _send(ws, f"a{number}", "terrain.tiles", {"level": level})
        replies = await _collect(ws, 3)
        assert all(r.type == "res" for r in replies.values())
        assert len({r.result["revision"] for r in replies.values()}) == 1
        assert len(slow_build.calls) == 1


async def test_a_listing_is_for_the_revision_it_was_ordered_after_and_a_superseded_build_is_not_current(
    server_url, tmp_path, slow_build
):
    older = _sheets(tmp_path, seed=1)
    bbox = (-745000.0, -1025700.0, -744200.0, -1025100.0)  # far from the first source: its own tiles
    newer = [str(_write_las(tmp_path / "N.las", sample_irregular(bbox, 8.0, seed=2)))]
    newer_tile = (int(np.floor(bbox[0] / 512.0)), int(np.floor(bbox[1] / 512.0)))
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", older)
        await _send(ws, "first", "terrain.tiles", {"level": 0})
        await asyncio.sleep(0.2)  # the first build is in its worker thread
        await _import(ws, "i2", newer)  # supersedes that revision; the thread cannot be stopped
        first = (await _collect(ws, 1))["first"]
        assert first.type == "res", first.error
        # it was sent before the import: it lists the old state, under the old revision
        assert newer_tile not in {(t["ix"], t["iy"]) for t in first.result["tiles"]}
        assert len(slow_build.calls) == 1  # the superseded build did not make the new revision current
        again, _tail, _size = await _call(ws, "again", "terrain.tiles", {"level": 0})
        assert newer_tile in {(t["ix"], t["iy"]) for t in again.result["tiles"]}
        assert again.result["revision"] != first.result["revision"]
        assert len(slow_build.calls) == 2 and slow_build.calls[-1] == 2
        old = first.result["tiles"][0]
        stale, _tail, _size = await _call(
            ws, "stale", "terrain.tile",
            {"level": 0, "ix": old["ix"], "iy": old["iy"], "revision": first.result["revision"]},
        )  # fmt: skip
        assert (stale.type, stale.error.code) == ("err", "E_NOT_FOUND")


async def test_a_stale_tile_request_is_refused_without_building_the_new_stack(
    server_url, tmp_path, slow_build
):
    paths = _sheets(tmp_path)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", paths)
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        tile = listed.result["tiles"][0]
        assert len(slow_build.calls) == 1
        await _import(ws, "i2", paths[:1])
        started = time.perf_counter()
        stale, _tail, _size = await _call(
            ws, "2", "terrain.tile",
            {"level": 0, "ix": tile["ix"], "iy": tile["iy"], "revision": listed.result["revision"]},
        )  # fmt: skip
        assert (stale.type, stale.error.code) == ("err", "E_NOT_FOUND")
        assert len(slow_build.calls) == 1 and time.perf_counter() - started < 0.4


async def test_terrain_builds_never_overlap_even_when_a_newer_revision_is_asked_for(
    server_url, tmp_path, slow_build
):
    older = _sheets(tmp_path, seed=1)
    bbox = (-745000.0, -1025700.0, -744200.0, -1025100.0)
    newer = [str(_write_las(tmp_path / "N.las", sample_irregular(bbox, 8.0, seed=2)))]
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", older)
        await _send(ws, "first", "terrain.tiles", {"level": 0})
        await asyncio.sleep(0.2)  # the first build is in its worker thread
        await _import(ws, "i2", newer)  # supersedes it, but the thread cannot be stopped
        await _send(ws, "second", "terrain.tiles", {"level": 1})  # asks for the new revision at once
        replies = await _collect(ws, 2)
        assert all(r.type == "res" for r in replies.values())
        assert replies["first"].result["revision"] != replies["second"].result["revision"]
    assert slow_build.peak == 1 and len(slow_build.calls) == 2


@pytest.fixture
def counted_bakes(monkeypatch):
    """Wraps the tile bake: counts calls and the most that ran at once, and takes `delay` s per tile."""
    from coypu_builder.server import handlers

    real = handlers.bake_tile
    lock = threading.Lock()
    state = {"calls": 0, "running": 0, "peak": 0, "delay": 0.0, "fail": False}

    def counted(*args):
        with lock:
            state["calls"] += 1
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
        try:
            if state["fail"]:
                raise RuntimeError("bake failed")
            time.sleep(state["delay"])
            return real(*args)
        finally:
            with lock:
                state["running"] -= 1

    monkeypatch.setattr(handlers, "bake_tile", counted)
    return state


async def test_two_requests_for_one_tile_share_one_bake(server_url, tmp_path, counted_bakes):
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", _sheets(tmp_path))
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        revision = listed.result["revision"]
        counted_bakes["calls"] = 0
        counted_bakes["delay"] = 0.3
        key = (listed.result["tiles"][0]["ix"] // 2, listed.result["tiles"][0]["iy"] // 2)  # an L1 tile
        params = {"level": 1, "ix": key[0], "iy": key[1], "revision": revision}
        for number in range(3):
            await _send(ws, f"t{number}", "terrain.tile", params)
        replies = await _collect(ws, 3)
        assert all(r.type == "res" for r in replies.values()), [r.error for r in replies.values()]
        assert counted_bakes["calls"] == 1


async def test_a_failing_bake_thread_is_an_internal_error_and_the_connection_survives(
    server_url, tmp_path, counted_bakes
):
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", _sheets(tmp_path))
        counted_bakes["fail"] = True
        failed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert (failed.type, failed.error.code) == ("err", "E_INTERNAL")
        assert "bake failed" in failed.error.message
        counted_bakes["fail"] = False
        ok, _tail, _size = await _call(ws, "2", "terrain.tiles", {"level": 0})  # nothing was cached as failed
        assert ok.type == "res" and ok.result["tiles"]


async def test_a_failure_in_the_coroutine_after_its_awaits_is_an_internal_error(
    server_url, tmp_path, monkeypatch
):
    from coypu_builder.server import handlers

    def broken(project, origin):
        raise ValueError("projection failed")

    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", _sheets(tmp_path))
        monkeypatch.setattr(handlers, "_tile_origin_godot", broken)
        failed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert (failed.type, failed.error.code) == ("err", "E_INTERNAL")
        assert "projection failed" in failed.error.message
        ping, _tail, _size = await _call(ws, "2", "session.ping", {})
        assert ping.type == "res"


async def test_session_ping_keeps_answering_while_a_listing_bakes_its_tiles(
    server_url, tmp_path, counted_bakes
):
    """A listing bakes every tile of the level. Each bake is made to take 0.4 s: baked on the event loop
    they would hold every ping for that long, so the slowest ping proves they run in worker threads."""
    counted_bakes["delay"] = 0.4
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i", _sheets(tmp_path))
        started = time.perf_counter()
        reply, latencies = await _ping_while(ws, "tiles", "terrain.tiles", {"level": 0})
        elapsed = time.perf_counter() - started
        assert reply.type == "res", reply.error
        tile_count = len(reply.result["tiles"])
        assert tile_count >= 8 and elapsed > 0.8, (tile_count, elapsed)
        assert len(latencies) >= 5 and max(latencies) < 0.2, (len(latencies), max(latencies))
        assert 1 < counted_bakes["peak"] <= 4  # parallel, but bounded by the connection's limit


async def test_a_tile_request_is_judged_against_the_state_at_its_arrival_not_after_a_later_import(
    server_url, tmp_path
):
    older = _sheets(tmp_path)
    bbox = (-745000.0, -1025700.0, -744200.0, -1025100.0)
    newer = str(_write_las(tmp_path / "N.las", sample_irregular(bbox, 8.0, seed=2)))
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "i1", older)
        listed, _tail, _size = await _call(ws, "1", "terrain.tiles", {"level": 0})
        revision, tile = listed.result["revision"], listed.result["tiles"][0]
        params = {"level": 0, "ix": tile["ix"], "iy": tile["iy"], "revision": revision}
        settings = {"horizontal_crs": "EPSG:5514"}
        await _send(ws, "tile", "terrain.tile", params)
        await _send(ws, "import", "import.run", {"paths": [newer], "kind": "terrain", "settings": settings})
        replies = await _collect(ws, 2)
        assert replies["tile"].type == "res"  # arrived before the import: the old revision is current
        assert replies["import"].type == "res"
        after, _tail, _size = await _call(ws, "after", "terrain.tile", params)
        assert (after.type, after.error.code) == ("err", "E_NOT_FOUND")
