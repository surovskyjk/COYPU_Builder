"""T-134 Part A: imagery tiles -- local image files, shaded relief, composition onto the terrain tile grid,
and the `imagery.tiles` / `imagery.tile` methods.

Every fixture is synthetic and written into `tmp_path`; nothing touches the network or real data. The tile
used throughout is level 0, ix -1459, iy -2002 (the square 512 m from E -747008 to -746496, N -1025024 to
-1024512).
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.enums import ColorInterp
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION, config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.imagery import (
    ImageryRaster,
    RasterLayer,
    bounds_meet,
    compose_layers,
    sample_raster,
    subsample_factor,
)
from coypu_builder.domain.model.ids import new_id
from coypu_builder.domain.relief import hillshade, hypsometric, relief
from coypu_builder.domain.sources import (
    BBoxClip,
    FileOrigin,
    ImportSettings,
    ObjectRecord,
    ProviderOrigin,
    SourceDataset,
    SourceKind,
)
from coypu_builder.domain.terrain import TerrainGrid
from coypu_builder.domain.terrain_surface import GridSurface, TerrainStack
from coypu_builder.io.imagery import layer_for_payload, raster_image, register_imagery_readers
from coypu_builder.io.mesh.imagery_tiles import (
    IMAGE_SIZE,
    ImageryTile,
    compose_tile,
    encode_jpeg,
    height_range,
)
from coypu_builder.io.mesh.terrain_tiles import tile_bounds
from coypu_builder.io.providers import copernicus
from coypu_builder.io.providers.copernicus import LIABILITY_NOTICE
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.runner import CrsRequired, inspect_source, run_import
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import Envelope, ErrorCode
from coypu_builder.server import handlers, serve
from coypu_builder.server.codec import decode_frame, encode_frame, unpack_blob
from coypu_builder.server.session import ImageryCache, ProjectState, Session, SourceEntry

KROVAK = ProjectCRS("EPSG:5514")
TILE_IX, TILE_IY = -1459, -2002
TILE_W, TILE_N = TILE_IX * 512.0, (TILE_IY + 1) * 512.0  # west edge, north edge of the test tile
RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
BACKGROUND = (40, 40, 40)


@pytest.fixture(autouse=True)
def builtin_registry(monkeypatch, tmp_path):
    """A private registry holding the built-in readers, and a user config that cannot be real."""
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "user-config" / "config.toml")
    register_builtin_readers()
    register_imagery_readers()


# --- writers ----------------------------------------------------------------------------------------------


def write_bands(
    path: Path,
    bands: list[np.ndarray],
    origin_e: float,
    origin_n: float,
    cell: float,
    crs: str | None = "EPSG:5514",
    nodata: float | None = None,
    dtype: str = "uint8",
) -> Path:
    """`bands` are (rows, cols) arrays, row 0 the northernmost; (origin_e, origin_n) the outer corner."""
    rows, cols = bands[0].shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=len(bands),
        dtype=dtype,
        crs=crs,
        transform=Affine(cell, 0.0, origin_e, 0.0, -cell, origin_n),
        nodata=nodata,
    ) as ds:
        ds.write(np.stack(bands).astype(dtype))
        if len(bands) == 4:
            ds.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
    return path


def colour_bands(rgb: np.ndarray) -> list[np.ndarray]:
    return [rgb[..., k] for k in range(3)]


def write_terrain(
    path: Path,
    origin_e: float,
    origin_n: float,
    cell: float,
    shape: tuple[int, int],
    base: float = 150.0,
    slope: float = 0.01,
) -> Path:
    """A synthetic height grid: a gentle plane rising eastward, in metres."""
    rows, cols = shape
    east = np.arange(cols) * cell
    heights = np.tile(base + slope * east, (rows, 1)).astype(np.float64)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="float64",
        crs="EPSG:5514",
        transform=Affine(cell, 0.0, origin_e, 0.0, -cell, origin_n),
        nodata=-9999.0,
    ) as ds:
        ds.write(heights, 1)
    return path


def markers_image(cell: float = 0.5) -> np.ndarray:
    """A 512 m square at `cell` metres: four differently coloured 8 m markers, near the NW, NE and SE corners
    and one off-centre, on a dark background. Offsets are from the west and north edges."""
    size = int(512.0 / cell)
    rgb = np.full((size, size, 3), BACKGROUND, dtype=np.uint8)

    def paint(e0: float, e1: float, n0: float, n1: float, colour: tuple[int, int, int]) -> None:
        rgb[int(n0 / cell) : int(n1 / cell), int(e0 / cell) : int(e1 / cell)] = colour

    paint(20, 28, 20, 28, RED)  # north-west
    paint(484, 492, 20, 28, GREEN)  # north-east
    paint(484, 492, 484, 492, BLUE)  # south-east
    paint(300, 308, 150, 158, YELLOW)  # off-centre
    return rgb


def write_world_file(path: Path, origin_e: float, origin_n: float, cell: float) -> Path:
    """A `.jgw` world file: the centre of the upper-left pixel, then the cell sizes (no CRS)."""
    text = f"{cell}\n0.0\n0.0\n{-cell}\n{origin_e + cell / 2}\n{origin_n - cell / 2}\n"
    path.write_bytes(text.replace("\r\n", "\n").encode("ascii"))
    return path


def write_jpeg(path: Path, rgb: np.ndarray) -> Path:
    Image.fromarray(rgb).save(path, format="JPEG", quality=95)
    return path


# --- domain: pixels, resampling and composition ---------------------------------------------------------


def checkerboard(cell: float, size_m: float) -> ImageryRaster:
    """Black and white cells of `cell` metres, the top-left cell white, covering [0, size_m] squared."""
    cells = int(round(size_m / cell))
    parity = (np.arange(cells)[:, None] + np.arange(cells)[None, :]) % 2
    rgb = np.repeat((parity * 255).astype(np.uint8)[..., None], 3, axis=2)
    return ImageryRaster(
        origin_e=0.0,
        origin_n=size_m,
        cell_e=cell,
        cell_n=cell,
        rgb=rgb,
        valid=np.ones((cells, cells), dtype=bool),
        native_resolution_m=cell,
    )


def test_raster_layer_render_averages_fine_cells():
    raster = checkerboard(0.25, 64.0)
    layer = RasterLayer("checker", raster)
    rgb, valid = layer.render((0.0, 0.0, 64.0, 64.0), 64)  # 1 m pixels: k = 4
    assert valid.all()
    assert set(np.unique(rgb).tolist()) <= {127, 128}
    assert set(np.unique(rgb).tolist()) == {128}  # eight black and eight white sub-samples: 127.5 rounds up
    assert subsample_factor(1.0, 0.25) == 4
    coarse, coarse_valid = sample_raster(raster, (0.0, 0.0, 64.0, 64.0), 32, 1)  # 2 m pixels, k forced to 1
    assert coarse_valid.all()
    assert set(np.unique(coarse).tolist()) <= {0, 255}  # only black or white, never grey


def test_relief_lights_the_north_west_slope():
    rows, cols = 40, 40
    north = (rows - np.arange(rows))[:, None].astype(np.float64)  # row 0 is the north
    east = np.arange(cols, dtype=np.float64)[None, :]
    falling_nw = 0.5 * east - 0.5 * north  # lower to the north-west, higher to the south-east
    falling_se = -falling_nw
    nw = hillshade(falling_nw, 1.0)
    se = hillshade(falling_se, 1.0)
    assert nw.shape == (rows - 2, cols - 2)
    assert float(np.mean(nw)) > float(np.mean(se)) + 0.05
    assert np.all(nw > se)

    flat = hillshade(np.zeros((9, 9)), 1.0)
    assert np.allclose(flat, np.sin(np.radians(45.0)), rtol=0.0, atol=1e-12)

    holed = np.zeros((9, 9))
    holed[5, 5] = np.nan  # interior index (4, 4); its four neighbours and itself are invalid
    shade = hillshade(holed, 1.0)
    assert np.isnan(shade[4, 4]) and np.isnan(shade[3, 4]) and np.isnan(shade[5, 4])
    assert np.isnan(shade[4, 3]) and np.isnan(shade[4, 5])
    assert np.isfinite(shade[0, 0])
    rgb, valid = relief(holed, 1.0, 0.0, 10.0)
    assert not valid[4, 4] and not valid[3, 4] and valid[0, 0]
    assert hypsometric(np.array([1.0]), 1.0, 1.0)[0].tolist() == pytest.approx(
        hypsometric(np.array([0.5]), 0.0, 1.0)[0].tolist()
    )


def test_composition_layers_relief_and_alpha():
    fine_raster = ImageryRaster(  # 1 m, green, over E [W+50, W+80)
        origin_e=TILE_W + 50.0,
        origin_n=TILE_N,
        cell_e=1.0,
        cell_n=1.0,
        rgb=np.tile(np.array(GREEN, dtype=np.uint8), (512, 30, 1)),
        valid=np.ones((512, 30), dtype=bool),
        native_resolution_m=1.0,
    )
    coarse_raster = ImageryRaster(  # 2 m, red, over E [W, W+100)
        origin_e=TILE_W,
        origin_n=TILE_N,
        cell_e=2.0,
        cell_n=2.0,
        rgb=np.tile(np.array(RED, dtype=np.uint8), (256, 50, 1)),
        valid=np.ones((256, 50), dtype=bool),
        native_resolution_m=2.0,
    )
    fine, coarse = RasterLayer("fine", fine_raster), RasterLayer("coarse", coarse_raster)
    terrain = TerrainGrid(
        origin_e=TILE_W - 10.0,
        origin_n=TILE_N + 10.0,
        cell_e=2.0,
        cell_n=2.0,
        heights=np.tile(100.0 + 0.1 * np.arange(155, dtype=np.float64) * 2.0, (266, 1)),
    )  # covers E up to W+300
    stack = TerrainStack((GridSurface(terrain),))
    tile = compose_tile([coarse, fine], stack, (100.0, 130.0), 0, TILE_IX, TILE_IY)
    decoded = np.asarray(Image.open(io.BytesIO(tile.jpeg)).convert("RGB"), dtype=int)
    alpha = tile.alpha
    assert alpha is not None and alpha.shape == (IMAGE_SIZE, IMAGE_SIZE)

    def near(pixel, colour):
        return np.abs(pixel - np.array(colour)).max() <= 12

    assert near(decoded[100, 10], RED) and alpha[100, 10] == 255  # inside the coarse image only
    assert near(decoded[100, 60], GREEN) and alpha[100, 60] == 255  # the finer image wins the overlap
    assert near(decoded[100, 90], RED)  # back to the coarse image after the fine one ends
    assert alpha[100, 200] == 255 and decoded[100, 200].sum() > 0  # terrain only: relief, opaque
    assert alpha[100, 400] == 0 and np.array_equal(decoded[100, 400], [0, 0, 0])  # neither: transparent
    assert tile.source_ids == ("fine", "coarse", "relief")

    no_relief = compose_tile([coarse, fine], stack, None, 0, TILE_IX, TILE_IY)
    assert no_relief.source_ids == ("fine", "coarse") and no_relief.alpha[100, 200] == 0


def test_imagery_raster_validates_its_shapes_and_bounds():
    rgb = np.zeros((4, 6, 3), dtype=np.uint8)
    raster = ImageryRaster(10.0, 20.0, 1.0, 2.0, rgb, np.ones((4, 6), dtype=bool), 1.0)
    assert raster.bounds() == (10.0, 12.0, 16.0, 20.0)
    with pytest.raises(ValueError, match="rgb must be"):
        ImageryRaster(0.0, 0.0, 1.0, 1.0, rgb.astype(np.float64), np.ones((4, 6), bool), 1.0)
    with pytest.raises(ValueError, match="valid must be"):
        ImageryRaster(0.0, 0.0, 1.0, 1.0, rgb, np.ones((4, 5), bool), 1.0)
    with pytest.raises(ValueError, match="cell sizes"):
        ImageryRaster(0.0, 0.0, 0.0, 1.0, rgb, np.ones((4, 6), bool), 1.0)


def test_layers_that_do_not_meet_the_window_are_not_rendered():
    class Exploding:
        source_id = "far"
        resolution_m = 0.5

        def bounds(self):
            return (1000.0, 1000.0, 1100.0, 1100.0)

        def render(self, bounds, size):
            raise AssertionError("a layer outside the window must not render")

    touching = (TILE_W + 512.0, TILE_N - 512.0, TILE_W + 600.0, TILE_N)
    assert not bounds_meet(touching, (TILE_W, TILE_N - 512.0, TILE_W + 512.0, TILE_N))
    rgb, valid, winners = compose_layers([Exploding()], (TILE_W, TILE_N - 512.0, TILE_W + 512.0, TILE_N), 8)
    assert not valid.any() and winners == () and rgb.sum() == 0


def test_height_range_is_the_second_and_ninety_eighth_percentile():
    flat = TerrainStack((GridSurface(TerrainGrid(0.0, 10.0, 1.0, 1.0, np.full((10, 10), 42.0))),))
    assert height_range(flat) == (42.0, 42.0)
    assert height_range(TerrainStack(())) is None


def test_jpeg_is_baseline_rgb_without_exif():
    rgb = np.full((IMAGE_SIZE, IMAGE_SIZE, 3), (90, 120, 200), dtype=np.uint8)
    data = encode_jpeg(rgb)
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG" and image.mode == "RGB" and image.size == (IMAGE_SIZE, IMAGE_SIZE)
    assert "exif" not in image.info and not image.info.get("progressive")


# --- local images: reading the files ---------------------------------------------------------------------


def test_reader_for_kind(tmp_path):
    path = write_bands(tmp_path / "ortho.tif", colour_bands(markers_image()), TILE_W, TILE_N, 0.5)
    assert registry.reader_for(path, kind=SourceKind.IMAGERY).format_id == "raster-image"
    assert registry.reader_for(path).format_id == "geotiff"  # without a kind, as before
    assert registry.reader_for(path, kind=SourceKind.TERRAIN).format_id == "geotiff"
    _reader, report = inspect_source(path, kind=SourceKind.IMAGERY)
    assert report.kind == SourceKind.IMAGERY and report.counts["bands"] == 3


def test_jpeg_with_world_file_needs_a_crs(tmp_path):
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :32] = (200, 100, 50)
    jpg = write_jpeg(tmp_path / "area.jpg", rgb)
    write_world_file(tmp_path / "area.jgw", TILE_W, TILE_N, 1.0)
    _reader, report = inspect_source(jpg)
    assert report.declared_crs is None and report.kind == SourceKind.IMAGERY
    with pytest.raises(CrsRequired):
        run_import([jpg], SourceKind.IMAGERY, ImportSettings(), KROVAK, {})
    outcome = run_import([jpg], SourceKind.IMAGERY, ImportSettings(horizontal_crs="EPSG:5514"), KROVAK, {})
    raster = next(iter(outcome.payloads.values()))
    assert isinstance(raster, ImageryRaster)
    assert raster.bounds() == pytest.approx((TILE_W, TILE_N - 64.0, TILE_W + 64.0, TILE_N), abs=1e-6)
    assert raster.cell_e == 1.0 and raster.native_resolution_m == 1.0
    assert np.abs(raster.rgb[10, 5].astype(int) - [200, 100, 50]).max() < 12  # JPEG loss
    assert raster.rgb[10, 40].max() < 12  # the black half


def test_a_kind_mismatch_is_explained_not_guessed(tmp_path):
    jpg = write_jpeg(tmp_path / "area.jpg", np.zeros((16, 16, 3), dtype=np.uint8))
    write_world_file(tmp_path / "area.jgw", TILE_W, TILE_N, 1.0)
    with pytest.raises(ValueError, match="does not read terrain sources"):
        inspect_source(jpg, kind=SourceKind.TERRAIN)
    with pytest.raises(ValueError, match="does not read terrain sources"):
        run_import([jpg], SourceKind.TERRAIN, ImportSettings(horizontal_crs="EPSG:5514"), KROVAK, {})


def test_raster_image_refusals_and_decimation(tmp_path, monkeypatch):
    two_bands = write_bands(tmp_path / "two.tif", [np.zeros((8, 8), np.uint8)] * 2, TILE_W, TILE_N, 1.0)
    with pytest.raises(ValueError, match="2 bands"):
        inspect_source(two_bands, kind=SourceKind.IMAGERY)

    rgba = np.zeros((64, 64), dtype=np.uint8)
    alpha = np.zeros((64, 64), dtype=np.uint8)
    alpha[:, 32:] = 255  # the left half is transparent
    four = write_bands(tmp_path / "four.tif", [rgba + 200, rgba + 100, rgba + 50, alpha], TILE_W, TILE_N, 0.5)
    outcome = run_import([four], SourceKind.IMAGERY, ImportSettings(horizontal_crs="EPSG:5514"), KROVAK, {})
    raster = next(iter(outcome.payloads.values()))
    assert raster.rgb.shape == (32, 32, 3) and raster.cell_e == 1.0  # decimated to 1 m
    assert not raster.valid[:, :16].any() and raster.valid[:, 16:].all()

    finer = run_import(
        [four],
        SourceKind.IMAGERY,
        ImportSettings(horizontal_crs="EPSG:5514", display_resolution_m=0.25),
        KROVAK,
        {},
    )
    assert next(iter(finer.payloads.values())).cell_e == 0.5  # never finer than the source

    monkeypatch.setattr(raster_image, "MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="limit 100"):
        run_import([four], SourceKind.IMAGERY, ImportSettings(horizontal_crs="EPSG:5514"), KROVAK, {})


def test_a_source_in_another_crs_is_warped_to_the_project_crs(tmp_path):
    lon0, lat0, step = 14.2, 50.25, 0.00005
    size = 200
    to_krovak = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
    centre_e, centre_n = to_krovak.transform(lon0 + size * step / 2, lat0 - size * step / 2)
    left = np.zeros((size, size), dtype=np.uint8)
    left[:, : size // 2] = 255  # the western half is red (set below); both halves in one band each
    red, blue = left.copy(), np.where(left == 255, 0, 255).astype(np.uint8)
    path = write_bands(
        tmp_path / "wgs.tif",
        [red, np.zeros_like(red), blue],
        lon0,
        lat0,
        step,
        crs="EPSG:4326",
    )
    clip = BBoxClip(centre_e - 600.0, centre_n - 600.0, centre_e + 600.0, centre_n + 600.0)
    outcome = run_import([path], SourceKind.IMAGERY, ImportSettings(clip=clip), KROVAK, {})
    raster = next(iter(outcome.payloads.values()))
    assert raster.valid.mean() > 0.5
    # The Krovak grid is rotated against longitude, so the split is tested per pixel: each valid pixel's
    # centre is taken back to longitude and must be red west of the split, blue east of it.
    rows, cols = np.nonzero(raster.valid)
    pixel_e = raster.origin_e + (cols + 0.5) * raster.cell_e
    pixel_n = raster.origin_n - (rows + 0.5) * raster.cell_n
    lon, _lat = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True).transform(pixel_e, pixel_n)
    split = lon0 + size * step / 2
    expected_red = np.asarray(lon) < split
    colour = raster.rgb[rows, cols].astype(int)
    agree = (colour[:, 0] > 128) == expected_red
    assert agree.mean() > 0.97  # the nearest-neighbour edge is the only disagreement
    assert 0.0 < raster.native_resolution_m < 10.0


# --- the wire ---------------------------------------------------------------------------------------------


async def _recv(ws, timeout: float = 15.0):
    async with asyncio.timeout(timeout):
        frame = await ws.recv()
    envelope, tail = decode_frame(frame)
    return envelope, tail, len(frame)


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None):
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return await _recv(ws)


async def _send(ws, request_id: str, method: str, params: dict[str, Any]) -> None:
    await ws.send(
        encode_frame(Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params))
    )


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _open(ws) -> None:
    await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
    assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"}))[0].type == "res"


async def _import(ws, request_id: str, path: Path, kind: str) -> dict[str, Any]:
    settings = {"horizontal_crs": "EPSG:5514"}
    reply, _tail, _size = await _call(
        ws, request_id, "import.run", {"path": str(path), "kind": kind, "settings": settings}
    )
    assert reply.type == "res", reply.error
    return reply.result


async def _list(ws, request_id: str, level: int = 0) -> dict[str, Any]:
    reply, _tail, _size = await _call(ws, request_id, "imagery.tiles", {"level": level})
    assert reply.type == "res", reply.error
    return reply.result


async def _tile(ws, request_id: str, key: tuple[int, int], revision: int, level: int = 0):
    return await _call(
        ws, request_id, "imagery.tile", {"level": level, "ix": key[0], "iy": key[1], "revision": revision}
    )


def _decode(tail: bytes, envelope: Envelope, name: str) -> np.ndarray:
    ref = next(b for b in envelope.blobs if b.name == name)
    return unpack_blob(tail, ref)


def _colour_near(pixels: np.ndarray, row: int, col: int, colour: tuple[int, int, int], tol: int = 45) -> bool:
    """Whether the 3 x 3 neighbourhood of (row, col) holds the colour, to a tolerance for JPEG."""
    window = pixels[row - 1 : row + 2, col - 1 : col + 2].astype(int)
    return bool(np.any(np.all(np.abs(window - np.array(colour)) <= tol, axis=2)))


async def test_local_image_lands_at_four_asymmetric_markers(server_url, tmp_path):
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 2.0, (256, 256))
    imagery = write_bands(tmp_path / "ortho.tif", colour_bands(markers_image()), TILE_W, TILE_N, 0.5)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        source = await _import(ws, "i", imagery, "imagery")
        listed = await _list(ws, "l")
        tile = next(t for t in listed["tiles"] if (t["ix"], t["iy"]) == (TILE_IX, TILE_IY))
        assert tile["source_ids"] == [source["source_id"]]
        reply, tail, _size = await _tile(ws, "c", (TILE_IX, TILE_IY), listed["revision"])
        assert reply.type == "res", reply.error
        assert reply.result["has_alpha"] is False and reply.result["source_ids"] == [source["source_id"]]
        assert "alpha" not in {b.name for b in reply.blobs}
        pixels = np.asarray(Image.open(io.BytesIO(bytes(_decode(tail, reply, "image")))).convert("RGB"))
    assert pixels.shape == (IMAGE_SIZE, IMAGE_SIZE, 3)
    assert _colour_near(pixels, 23, 23, RED)  # north-west: top rows, west columns
    assert _colour_near(pixels, 23, 487, GREEN)  # north-east
    assert _colour_near(pixels, 487, 487, BLUE)  # south-east
    assert _colour_near(pixels, 153, 303, YELLOW)  # off-centre
    assert not _colour_near(pixels, 487, 23, RED)  # the north-west marker is not mirrored to the south
    assert _colour_near(pixels, 300, 300, BACKGROUND)


async def test_imagery_revision_and_attribution(server_url, tmp_path, monkeypatch):
    builds = []
    original_build = handlers._build_terrain

    def counted(snapshot):
        builds.append(len(snapshot))
        return original_build(snapshot)

    monkeypatch.setattr(handlers, "_build_terrain", counted)
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 2.0, (256, 256))
    other = write_terrain(tmp_path / "other.tif", TILE_W + 2048.0, TILE_N, 2.0, (256, 256))
    imagery = write_bands(tmp_path / "ortho.tif", colour_bands(markers_image()), TILE_W, TILE_N, 0.5)
    async with connect(server_url) as ws:
        await _open(ws)
        empty = await _list(ws, "e")
        assert empty["tiles"] == [] and empty["attributions"] == {}
        builds.clear()  # the listing above built an empty stack, which is not under test
        terrain_id = (await _import(ws, "t", terrain, "terrain"))["source_id"]
        first = await _list(ws, "l1")
        assert set(first["attributions"]) == {terrain_id}
        assert first["notices"] == {terrain_id: []}  # a file source has no provider notices
        assert len(builds) == 1

        imagery_id = (await _import(ws, "i", imagery, "imagery"))["source_id"]
        second = await _list(ws, "l2")
        assert second["revision"] != first["revision"]
        assert second["terrain_revision"] == first["terrain_revision"]
        assert second["warnings"] == []  # the import notes of imagery stay out of the listing
        assert set(second["attributions"]) == {terrain_id, imagery_id}
        assert second["notices"] == {terrain_id: [], imagery_id: []}
        assert len(builds) == 1  # an imagery-only import does not rebuild the terrain stack

        stale, _tail, _size = await _tile(ws, "s", (TILE_IX, TILE_IY), first["revision"])
        assert (stale.type, stale.error.code) == ("err", "E_NOT_FOUND")
        fresh, _tail, _size = await _tile(ws, "f", (TILE_IX, TILE_IY), second["revision"])
        assert fresh.type == "res", fresh.error

        await _import(ws, "t2", other, "terrain")
        third = await _list(ws, "l3")
        assert third["revision"] != second["revision"]
        assert third["terrain_revision"] != second["terrain_revision"]  # a terrain import changes both
        assert len(builds) == 2
        gone, _tail, _size = await _tile(ws, "g", (TILE_IX, TILE_IY), second["revision"])
        assert (gone.type, gone.error.code) == ("err", "E_NOT_FOUND")
        not_terrain, _tail, _size = await _tile(ws, "n", (0, 0), third["revision"])
        assert (not_terrain.type, not_terrain.error.code) == ("err", "E_NOT_FOUND")


def test_copernicus_source_carries_its_liability_notice(monkeypatch):
    registry.register_provider(copernicus.CopernicusGlo30Provider())
    grid = TerrainGrid(TILE_W - 512.0, TILE_N + 512.0, 16.0, 16.0, np.full((96, 96), 150.0))
    object_id = new_id()
    dataset = SourceDataset(
        id=new_id(),
        kind=SourceKind.TERRAIN,
        name="glo30",
        format_id="geotiff",
        origin=ProviderOrigin("copernicus-glo30", "{}", ()),
        settings=ImportSettings(),
        declared_crs=None,
        effective_crs="EPSG:5514",
        transformation="none",
        transformation_accuracy_m=0.0,
        licence="",
        attribution="produced using Copernicus",
        imported_at="",
        tool_version="",
        objects=(ObjectRecord("glo30", object_id, "x"),),
    )
    project = ProjectState(project_id="p", crs=KROVAK)
    project.sources[dataset.id] = SourceEntry(dataset, {object_id: grid})
    session = Session(session_id="s", expected_token=None, hello_received=True, project=project)
    reply, _blobs = asyncio.run(handlers.handle_imagery_tiles(session, {"level": 0}))
    assert reply["notices"] == {str(dataset.id): (LIABILITY_NOTICE,)}
    assert reply["attributions"] == {str(dataset.id): "produced using Copernicus"}
    assert handlers._notices_for(FileOrigin((), ())) == ()


async def test_ping_answers_while_tiles_compose(server_url, tmp_path, monkeypatch):
    counts: Counter[tuple[int, int, int]] = Counter()
    original = handlers.compose_tile

    def slow(layers, stack, heights_range, level, ix, iy):
        counts[(level, ix, iy)] += 1
        time.sleep(0.25)  # a GIL-releasing wait, standing in for a heavy composition
        return original(layers, stack, heights_range, level, ix, iy)

    monkeypatch.setattr(handlers, "compose_tile", slow)
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 8.0, (128, 256), slope=0.0)
    imagery = write_bands(
        tmp_path / "ortho.tif",
        [np.full((512, 1024), value, np.uint8) for value in (100, 150, 200)],
        TILE_W,
        TILE_N,
        2.0,
    )
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        await _import(ws, "i", imagery, "imagery")
        listed = await _list(ws, "l")
        keys = [(t["ix"], t["iy"]) for t in listed["tiles"]]
        assert len(keys) == 8
        for number, key in enumerate(keys):
            await _send(
                ws,
                f"c{number}",
                "imagery.tile",
                {"level": 0, "ix": key[0], "iy": key[1], "revision": listed["revision"]},
            )
        replies: dict[str, Envelope] = {}
        latencies: list[float] = []
        number = 0
        while len(replies) < len(keys):
            number += 1
            ping_id = f"ping{number}"
            sent = time.perf_counter()
            await _send(ws, ping_id, "session.ping", {})
            answered = False
            while not answered:
                envelope, _tail, _size = await _recv(ws)
                if envelope.id == ping_id:
                    latencies.append(time.perf_counter() - sent)
                    answered = True
                else:
                    replies[envelope.id] = envelope
    assert all(reply.type == "res" for reply in replies.values())
    assert len(latencies) >= 5 and max(latencies) < 0.1
    assert all(counts[(0, ix, iy)] == 1 for ix, iy in keys)


async def test_two_requests_for_one_tile_compose_it_once(server_url, tmp_path, monkeypatch):
    counts: Counter[tuple[int, int, int]] = Counter()
    original = handlers.compose_tile

    def counted(layers, stack, heights_range, level, ix, iy):
        counts[(level, ix, iy)] += 1
        time.sleep(0.2)
        return original(layers, stack, heights_range, level, ix, iy)

    monkeypatch.setattr(handlers, "compose_tile", counted)
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 2.0, (256, 256))
    imagery = write_bands(tmp_path / "ortho.tif", colour_bands(markers_image()), TILE_W, TILE_N, 0.5)
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        await _import(ws, "i", imagery, "imagery")
        listed = await _list(ws, "l")
        await _send(
            ws,
            "a",
            "imagery.tile",
            {"level": 0, "ix": TILE_IX, "iy": TILE_IY, "revision": listed["revision"]},
        )
        await _send(
            ws,
            "b",
            "imagery.tile",
            {"level": 0, "ix": TILE_IX, "iy": TILE_IY, "revision": listed["revision"]},
        )
        first, _tail, _size = await _recv(ws)
        second, _tail, _size = await _recv(ws)
    assert {first.type, second.type} == {"res"}
    assert counts[(0, TILE_IX, TILE_IY)] == 1


async def test_imagery_tile_blobs(server_url, tmp_path):
    """Three tiles: one fully imaged, one shaded relief only (opaque), one at the edge of the terrain."""
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N + 512.0, 2.0, (1024, 562), slope=0.02)
    imagery = write_bands(
        tmp_path / "ortho.tif",
        [np.full((512, 512), value, np.uint8) for value in (10, 20, 30)],
        TILE_W,
        TILE_N,
        1.0,
    )
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        await _import(ws, "i", imagery, "imagery")
        listed = await _list(ws, "l")
        keys = {(t["ix"], t["iy"]): t for t in listed["tiles"]}
        imaged = (TILE_IX, TILE_IY)
        relief_only = (TILE_IX + 1, TILE_IY)
        edge = (TILE_IX + 2, TILE_IY)
        assert imaged in keys and relief_only in keys and edge in keys
        assert keys[imaged]["source_ids"] and keys[relief_only]["source_ids"] == []

        full, tail, size = await _tile(ws, "a", imaged, listed["revision"])
        assert full.type == "res" and full.result["has_alpha"] is False and full.result["source_ids"]
        assert full.result["width"] == 512 and full.result["height"] == 512

        relief_reply, _tail, _size = await _tile(ws, "b", relief_only, listed["revision"])
        assert relief_reply.result["source_ids"] == ["relief"]

        edge_reply, edge_tail, edge_size = await _tile(ws, "c", edge, listed["revision"])
        assert edge_reply.type == "res" and edge_reply.result["has_alpha"] is True
        alpha = _decode(edge_tail, edge_reply, "alpha")
        assert alpha.shape == (512, 512) and alpha.dtype == np.uint8
        assert set(np.unique(alpha).tolist()) == {0, 255}
        image = Image.open(io.BytesIO(bytes(_decode(edge_tail, edge_reply, "image"))))
        assert image.size == (512, 512) and image.mode == "RGB"
        assert edge_size < 1_000_000 and size < 1_000_000
        assert "alpha" not in {b.name for b in full.blobs}


async def test_a_project_without_imagery_lists_no_source(server_url, tmp_path):
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 2.0, (256, 256))
    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        listed = await _list(ws, "l")
        assert len(listed["tiles"]) == 1 and listed["tiles"][0]["source_ids"] == []
        assert listed["warnings"] == []


# --- the layer factory and the blob sizes ------------------------------------------------------------------


def test_payload_types_have_a_layer_factory():
    raster = ImageryRaster(0.0, 1.0, 1.0, 1.0, np.zeros((1, 1, 3), np.uint8), np.ones((1, 1), bool), 1.0)
    layer = layer_for_payload("src", raster)
    assert layer.source_id == "src" and layer.resolution_m == 1.0
    with pytest.raises(ValueError, match="no imagery layer"):
        layer_for_payload("src", object())


def test_a_partly_imaged_level_zero_tile_is_opaque_where_covered_and_transparent_elsewhere():
    """The left half of the tile has imagery, a strip of terrain adds relief past it, the rest is neither."""
    raster = ImageryRaster(
        origin_e=TILE_W,
        origin_n=TILE_N,
        cell_e=1.0,
        cell_n=1.0,
        rgb=markers_image(1.0),
        valid=np.ones((512, 512), dtype=bool),
        native_resolution_m=1.0,
    )
    raster.valid[:, 256:] = False
    layer = RasterLayer("ortho", raster)
    grid = TerrainGrid(TILE_W - 10.0, TILE_N + 10.0, 2.0, 2.0, np.full((266, 136), 150.0))  # to E W+262
    tile = compose_tile([layer], TerrainStack((GridSurface(grid),)), (150.0, 151.0), 0, TILE_IX, TILE_IY)
    assert tile.alpha is not None
    assert tile.alpha[100, 100] == 255  # imagery
    assert tile.alpha[100, 258] == 255  # relief in the strip the terrain covers
    assert tile.alpha[100, 400] == 0  # neither
    assert tile.source_ids == ("ortho", "relief")


# --- review additions (T-134 Part A) ----------------------------------------------------------------------


def _import_one(path, **settings):
    outcome = run_import(
        [path], SourceKind.IMAGERY, ImportSettings(horizontal_crs="EPSG:5514", **settings), KROVAK, {}
    )
    return next(iter(outcome.payloads.values()))


def test_one_band_grey_image_becomes_three_equal_channels(tmp_path):
    grey = (np.arange(16 * 16).reshape(16, 16) % 250).astype(np.uint8)
    raster = _import_one(write_bands(tmp_path / "grey.tif", [grey], TILE_W, TILE_N, 1.0))
    assert raster.rgb.shape == (16, 16, 3)
    for channel in range(3):
        assert np.array_equal(raster.rgb[..., channel], grey)
    fine = np.repeat(np.repeat(grey, 2, axis=0), 2, axis=1)  # the same picture at 0.5 m: averaged back to 1 m
    decimated = _import_one(write_bands(tmp_path / "grey_fine.tif", [fine], TILE_W, TILE_N, 0.5))
    assert decimated.rgb.shape == (16, 16, 3) and np.array_equal(decimated.rgb[..., 1], grey)
    assert decimated.native_resolution_m == 0.5  # precedence uses the source's own cell, not the display one


def test_a_palette_image_is_refused(tmp_path):
    index = (np.arange(64 * 64).reshape(64, 64) % 4).astype(np.uint8)
    image = Image.new("P", (64, 64))
    image.putdata(index.ravel().tolist())
    image.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * 756)
    image.save(tmp_path / "pal.png")
    write_world_file(tmp_path / "pal.pgw", TILE_W, TILE_N, 1.0)
    with pytest.raises(ValueError, match="indexed-colour palette"):
        inspect_source(tmp_path / "pal.png", kind=SourceKind.IMAGERY)
    with pytest.raises(ValueError, match="indexed-colour palette"):
        _import_one(tmp_path / "pal.png")


def test_rgba_image_in_another_crs_keeps_its_transparency(tmp_path):
    x0, y0 = Transformer.from_crs("EPSG:4326", "EPSG:32633", always_xy=True).transform(14.2, 50.25)
    size, cell = 200, 5.0
    alpha = np.full((size, size), 255, np.uint8)
    alpha[:, : size // 2] = 0  # the western half is transparent
    grey = np.full((size, size), 90, np.uint8)
    path = write_bands(tmp_path / "rgba_utm.tif", [grey, grey, grey, alpha], x0, y0, cell, crs="EPSG:32633")
    centre_e, centre_n = Transformer.from_crs("EPSG:32633", "EPSG:5514", always_xy=True).transform(
        x0 + size * cell / 2, y0 - size * cell / 2
    )
    clip = BBoxClip(centre_e - 700.0, centre_n - 700.0, centre_e + 700.0, centre_n + 700.0)
    raster = next(
        iter(run_import([path], SourceKind.IMAGERY, ImportSettings(clip=clip), KROVAK, {}).payloads.values())
    )
    rows, cols = np.nonzero(raster.valid)
    back = Transformer.from_crs("EPSG:5514", "EPSG:32633", always_xy=True)
    source_e, _n = back.transform(
        raster.origin_e + (cols + 0.5) * raster.cell_e, raster.origin_n - (rows + 0.5) * raster.cell_n
    )
    assert raster.valid.sum() > 1000
    assert ((source_e - x0) / cell >= size / 2 - 1).all()  # nothing valid in the transparent half


def test_nodata_marks_a_pixel_invalid_only_when_every_band_is_nodata(tmp_path):
    red = np.zeros((16, 16), np.uint8)
    green = np.full((16, 16), 120, np.uint8)
    blue = np.full((16, 16), 60, np.uint8)
    red[:, 8:12] = 10
    red[:, 12:], green[:, 12:], blue[:, 12:] = 0, 0, 0  # the eastern quarter is nodata in every band
    raster = _import_one(
        write_bands(tmp_path / "nd.tif", [red, green, blue], TILE_W, TILE_N, 1.0, nodata=0.0)
    )
    assert raster.valid[:, :12].all()  # red 0 beside green 120 is a real colour
    assert not raster.valid[:, 12:].any()


def test_a_zip_keeps_the_world_file_stored_beside_its_image(tmp_path):
    rgb = np.zeros((64, 64, 3), dtype=np.uint8)
    rgb[:, :32] = (200, 100, 50)
    jpg = write_jpeg(tmp_path / "plain.jpg", rgb)
    world = write_world_file(tmp_path / "plain.jgw", TILE_W, TILE_N, 1.0)
    archive = tmp_path / "sheet.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(jpg, "WRTO24.jpg")
        bundle.write(world, "WRTO24.jgw")
    _reader, report = inspect_source(archive, kind=SourceKind.IMAGERY)
    assert report.extent == pytest.approx((TILE_W, TILE_N - 64.0, TILE_W + 64.0, TILE_N), abs=1e-6)
    raster = _import_one(archive)
    assert raster.bounds() == pytest.approx((TILE_W, TILE_N - 64.0, TILE_W + 64.0, TILE_N), abs=1e-6)


def _flat(colour, cells=4):
    return RasterLayer(
        str(colour),
        ImageryRaster(
            0.0,
            float(cells),
            1.0,
            1.0,
            np.tile(np.array(colour, np.uint8), (cells, cells, 1)),
            np.ones((cells, cells), bool),
            1.0,
        ),
    )


def test_render_rounds_half_up_and_needs_at_least_half_the_sub_samples():
    rgb = np.zeros((2, 2, 3), np.uint8)
    rgb[0, 1] = 1
    rgb[1, 0] = 1  # the four cells average 0.5 per channel
    raster = ImageryRaster(0.0, 2.0, 1.0, 1.0, rgb, np.ones((2, 2), bool), 1.0)
    out, valid = RasterLayer("a", raster).render((0.0, 0.0, 2.0, 2.0), 1)  # one 2 m pixel: k = 2
    assert valid[0, 0] and out[0, 0].tolist() == [1, 1, 1]  # half rounds up (a half-to-even round gives 0)
    for kept, expected in ((1, False), (2, True), (3, True), (4, True)):
        mask = np.zeros((2, 2), bool)
        mask.flat[:kept] = True
        part = ImageryRaster(0.0, 2.0, 1.0, 1.0, np.full((2, 2, 3), 200, np.uint8), mask, 1.0)
        _out, valid = RasterLayer("a", part).render((0.0, 0.0, 2.0, 2.0), 1)
        assert bool(valid[0, 0]) is expected, kept


def test_subsample_factor_rounds_up_and_is_capped():
    assert subsample_factor(1.0, 0.4) == 3  # ceil(2.5)
    assert subsample_factor(0.5, 1.0) == 1
    assert subsample_factor(100.0, 1.0) == 8


def test_layers_of_equal_resolution_keep_the_callers_order():
    red, green = _flat(RED), _flat(GREEN)
    bounds = (0.0, 0.0, 4.0, 4.0)
    rgb, _valid, winners = compose_layers([red, green], bounds, 4)
    assert winners == (red.source_id,) and tuple(rgb[0, 0]) == RED
    rgb, _valid, winners = compose_layers([green, red], bounds, 4)
    assert winners == (green.source_id,) and tuple(rgb[0, 0]) == GREEN


def test_hillshade_and_relief_have_closed_form_values():
    cell, rows = 2.0, 9
    east = np.arange(rows) * cell
    north = (rows - 1 - np.arange(rows))[:, None] * cell  # row 0 is the north
    falling_nw = 0.5 * east[None, :] - 0.5 * north  # 0.5 m per metre, lower to the north-west
    assert np.allclose(hillshade(falling_nw, cell), 0.9855985596534886, atol=1e-9)
    steep = 3.0 * (north - east[None, :])  # rises to the north-west: faces away from the sun, so black
    assert np.all(hillshade(steep, cell) == 0.0)
    rgb, valid = relief(np.full((3, 3), 5.0), 1.0, 0.0, 10.0)  # flat, t = 0.5, hillshade sin(45 deg)
    assert valid[0, 0] and rgb[0, 0].tolist() == [137, 136, 88]


def test_height_range_ignores_the_extremes():
    grid = TerrainGrid(0.0, 100.0, 1.0, 1.0, np.tile(np.arange(100, dtype=np.float64), (100, 1)))
    low, high = height_range(TerrainStack((GridSurface(grid),)))
    assert 1.0 < low < 4.0 and 96.0 < high < 99.0


def _terrain_over(bounds, fn, cell=1.0):
    min_e, min_n, max_e, max_n = bounds
    cols, rows = int((max_e - min_e) / cell) + 20, int((max_n - min_n) / cell) + 20
    e = min_e - 10 + (np.arange(cols) + 0.5) * cell
    n = max_n + 10 - (np.arange(rows) + 0.5) * cell
    ee, nn = np.meshgrid(e, n)
    grid = TerrainGrid(min_e - 10, max_n + 10, cell, cell, fn(ee - min_e, nn - min_n))
    return TerrainStack((GridSurface(grid),))


def _luminance(tile):
    pixels = np.asarray(Image.open(io.BytesIO(tile.jpeg)).convert("L"), dtype=float)
    return pixels


def test_relief_edges_land_on_the_right_row_and_column():
    bounds = tile_bounds(0, TILE_IX, TILE_IY)
    east_step = _terrain_over(bounds, lambda e, n: 100.0 + 40.0 * (e >= 100.0))
    profile = _luminance(compose_tile([], east_step, (100.0, 140.0), 0, TILE_IX, TILE_IY))[50:450].mean(
        axis=0
    )
    assert abs(int(np.argmax(np.abs(np.diff(profile)))) - 99) <= 1  # the step is 100 m from the west edge
    north_step = _terrain_over(bounds, lambda e, n: 100.0 + 40.0 * (n >= 362.0))
    profile = _luminance(compose_tile([], north_step, (100.0, 140.0), 0, TILE_IX, TILE_IY))[:, 50:450].mean(
        axis=1
    )
    assert (
        abs(int(np.argmax(np.abs(np.diff(profile)))) - 149) <= 1
    )  # 150 m from the north edge: row 0 is north


def test_a_level_two_tile_uses_its_own_edge_and_pixel_size():
    level, ix, iy = 2, -365, -501
    min_e, min_n, max_e, max_n = tile_bounds(level, ix, iy)
    assert max_e - min_e == 2048.0
    rgb = np.full((2048, 2048, 3), BACKGROUND, np.uint8)
    rgb[600:632, 400:432] = RED  # 32 m marker 400 m east and 600 m south of the north-west corner
    layer = RasterLayer("ortho", ImageryRaster(min_e, max_n, 1.0, 1.0, rgb, np.ones((2048, 2048), bool), 1.0))
    slope = 0.2
    stack = _terrain_over((min_e, min_n, max_e, max_n), lambda e, n: 100.0 + slope * e, cell=8.0)
    tile = compose_tile([layer], stack, (100.0, 100.0), level, ix, iy)
    pixels = np.asarray(Image.open(io.BytesIO(tile.jpeg)).convert("RGB"), dtype=int)
    assert tile.edge_m == 2048.0
    assert (
        np.abs(pixels[153, 103] - np.array(RED)).max() <= 40
    )  # 4 m pixels: column 100 to 107, row 150 to 157
    assert np.abs(pixels[153, 403] - np.array(BACKGROUND)).max() <= 40
    relief_only = compose_tile([], stack, (100.0, 100.0), level, ix, iy)  # 4 m pixels in the hillshade too
    expected, _ok = relief(100.0 + slope * 4.0 * np.arange(5)[None, :] * np.ones((5, 1)), 4.0, 100.0, 100.0)
    got = np.asarray(Image.open(io.BytesIO(relief_only.jpeg)).convert("RGB"), dtype=int)[256, 256]
    assert np.abs(got - expected[2, 1].astype(int)).max() <= 4


def test_the_imagery_tile_cache_is_bounded_and_least_recently_used():
    cache = ImageryCache()

    def tile(n):
        return ImageryTile(0, n, 0, 512.0, b"", None, ())

    for n in range(70):
        cache.put_tile(tile(n))
        assert len(cache.tiles) <= 64
    assert len(cache.tiles) == 64 and cache.get_tile(0, 0, 0) is None and cache.get_tile(0, 69, 0) is not None
    assert cache.get_tile(0, 6, 0) is not None  # now the most recently used
    cache.put_tile(tile(70))
    assert cache.get_tile(0, 6, 0) is not None and cache.get_tile(0, 7, 0) is None


async def test_import_inspect_passes_kind_over_the_wire(server_url, tmp_path):
    path = write_bands(tmp_path / "ortho.tif", colour_bands(markers_image(2.0)), TILE_W, TILE_N, 2.0)
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        as_imagery, _t, _s = await _call(ws, "1", "import.inspect", {"path": str(path), "kind": "imagery"})
        as_default, _t, _s = await _call(ws, "2", "import.inspect", {"path": str(path)})
        bogus, _t, _s = await _call(ws, "3", "import.inspect", {"path": str(path), "kind": "bogus"})
    assert as_imagery.result["format_id"] == "raster-image" and as_imagery.result["kind"] == "imagery"
    assert as_default.result["format_id"] == "geotiff"
    assert (bogus.type, bogus.error.code) == ("err", "E_BAD_PARAMS")


async def test_finer_imagery_first_then_the_newest_import_among_equals(server_url, tmp_path):
    terrain = write_terrain(tmp_path / "terrain.tif", TILE_W, TILE_N, 2.0, (256, 256))

    def flat(name, colour, cell):
        size = int(512.0 / cell)
        bands = [np.full((size, size), value, np.uint8) for value in colour]
        return write_bands(tmp_path / name, bands, TILE_W, TILE_N, cell)

    async with connect(server_url) as ws:
        await _open(ws)
        await _import(ws, "t", terrain, "terrain")
        coarse = (await _import(ws, "a", flat("a.tif", RED, 2.0), "imagery"))["source_id"]
        old_fine = (await _import(ws, "b", flat("b.tif", GREEN, 1.0), "imagery"))["source_id"]
        new_fine = (await _import(ws, "c", flat("c.tif", BLUE, 1.0), "imagery"))["source_id"]
        listed = await _list(ws, "l")
        reply, tail, _size = await _tile(ws, "x", (TILE_IX, TILE_IY), listed["revision"])
        pixels = np.asarray(Image.open(io.BytesIO(bytes(_decode(tail, reply, "image")))).convert("RGB"))
    assert listed["tiles"][0]["source_ids"] == [new_fine, old_fine, coarse]
    assert reply.result["source_ids"] == [new_fine]
    assert np.abs(pixels[256, 256].astype(int) - np.array(BLUE)).max() <= 30


@pytest.mark.parametrize(
    ("image", "world", "fmt"),
    [
        ("a.png", "a.pgw", "PNG"),
        ("a.tif", "a.tfw", "TIFF"),
        ("a.jpg", "a.jgw", "JPEG"),
        ("a.jpeg", "a.wld", "JPEG"),
    ],
)
def test_each_image_format_is_georeferenced_by_its_world_file(tmp_path, image, world, fmt):
    rgb = np.zeros((64, 64, 3), np.uint8)
    rgb[:32, :32], rgb[:32, 32:], rgb[32:, 32:] = RED, GREEN, BLUE
    Image.fromarray(rgb).save(tmp_path / image, format=fmt)
    write_world_file(tmp_path / world, TILE_W, TILE_N, 1.0)
    raster = _import_one(tmp_path / image)
    assert raster.bounds() == pytest.approx((TILE_W, TILE_N - 64.0, TILE_W + 64.0, TILE_N), abs=1e-6)
    assert np.abs(raster.rgb[5, 5].astype(int) - RED).max() <= 3  # north-west
    assert np.abs(raster.rgb[5, 40].astype(int) - GREEN).max() <= 3  # north-east
    assert np.abs(raster.rgb[40, 40].astype(int) - BLUE).max() <= 3  # south-east


def test_a_clipped_image_keeps_its_place_and_masks_cells_whose_centre_is_outside(tmp_path):
    size = 512  # 512 m at 1 m; red encodes the column, green the row
    columns = (np.arange(size)[None, :] % 256).astype(np.uint8) * np.ones((size, 1), np.uint8)
    rows = (np.arange(size)[:, None] % 256).astype(np.uint8) * np.ones((1, size), np.uint8)
    path = write_bands(tmp_path / "enc.tif", [columns, rows, columns * 0], TILE_W, TILE_N, 1.0)
    clip = BBoxClip(TILE_W + 100.7, TILE_N - 300.7, TILE_W + 200.2, TILE_N - 50.2)
    raster = next(
        iter(run_import([path], SourceKind.IMAGERY, ImportSettings(clip=clip), KROVAK, {}).payloads.values())
    )
    assert raster.bounds() == pytest.approx(
        (TILE_W + 100.0, TILE_N - 301.0, TILE_W + 201.0, TILE_N - 50.0), abs=1e-6
    )
    row, col = raster.rows // 2, raster.cols // 2  # the window starts at source column 100 and row 50
    assert raster.rgb[row, col, 0] == (100 + col) % 256 and raster.rgb[row, col, 1] == (50 + row) % 256
    assert (
        not raster.valid[:, 0].any() and not raster.valid[:, -1].any()
    )  # centres 100.5 and 200.5 are outside
    assert raster.valid[:, 1:-1].all()


@pytest.mark.parametrize("source_crs", ["EPSG:4326", "EPSG:32633"])
def test_warped_cells_sit_where_pyproj_puts_them(tmp_path, source_crs):
    """The warp is GDAL's, the point readers use pyproj: they must agree to a small part of a source cell."""
    x0, y0 = Transformer.from_crs("EPSG:4326", source_crs, always_xy=True).transform(14.2, 50.25)
    cell = 0.00005 if source_crs == "EPSG:4326" else 5.0
    size = 400  # red encodes the source column, green the source row (modulo 256)
    columns = (np.arange(size)[None, :] % 256).astype(np.uint8) * np.ones((size, 1), np.uint8)
    rows = (np.arange(size)[:, None] % 256).astype(np.uint8) * np.ones((1, size), np.uint8)
    path = write_bands(tmp_path / "enc.tif", [columns, rows, columns * 0], x0, y0, cell, crs=source_crs)
    centre_e, centre_n = Transformer.from_crs(source_crs, "EPSG:5514", always_xy=True).transform(
        x0 + cell * size / 2, y0 - cell * size / 2
    )
    clip = BBoxClip(centre_e - 900.0, centre_n - 900.0, centre_e + 900.0, centre_n + 900.0)
    settings = ImportSettings(clip=clip, display_resolution_m=1.0)
    raster = next(iter(run_import([path], SourceKind.IMAGERY, settings, KROVAK, {}).payloads.values()))
    row_i, col_i = np.nonzero(raster.valid)
    source_x, source_y = Transformer.from_crs("EPSG:5514", source_crs, always_xy=True).transform(
        raster.origin_e + (col_i + 0.5) * raster.cell_e, raster.origin_n - (row_i + 0.5) * raster.cell_n
    )
    want_col, want_row = np.floor((source_x - x0) / cell), np.floor((y0 - source_y) / cell)
    inside = (want_col >= 1) & (want_col < size - 1) & (want_row >= 1) & (want_row < size - 1)
    off_col = (want_col - raster.rgb[row_i, col_i, 0].astype(int) + 128) % 256 - 128
    off_row = (want_row - raster.rgb[row_i, col_i, 1].astype(int) + 128) % 256 - 128
    assert inside.sum() > 10000
    assert abs(off_col[inside].mean()) < 0.05 and abs(off_row[inside].mean()) < 0.05  # source cells


def _entry(kind: SourceKind, origin, payloads: dict[str, object]) -> SourceEntry:
    ids = {key: new_id() for key in payloads}
    dataset = SourceDataset(
        id=new_id(),
        kind=kind,
        name=kind.value,
        format_id="test",
        origin=origin,
        settings=ImportSettings(),
        declared_crs=None,
        effective_crs="EPSG:5514",
        transformation="none",
        transformation_accuracy_m=0.0,
        licence="",
        attribution="",
        imported_at="",
        tool_version="",
        objects=tuple(ObjectRecord(key, ids[key], "x") for key in payloads),
    )
    return SourceEntry(dataset, {ids[key]: payload for key, payload in payloads.items()})


def test_the_listing_limit_falls_with_the_number_of_imagery_layers():
    side = 15  # 225 level-0 tiles: more than the 221 that 100 layers leave room for
    terrain = _entry(
        SourceKind.TERRAIN,
        FileOrigin((), ()),
        {"terrain": TerrainGrid(0.0, side * 512.0, 16.0, 16.0, np.full((side * 32, side * 32), 150.0))},
    )
    tiny = ImageryRaster(0.0, 512.0, 512.0, 512.0, np.zeros((1, 1, 3), np.uint8), np.ones((1, 1), bool), 1.0)

    def listing(layer_count: int, level: int):
        project = ProjectState(project_id="p")
        project.sources[terrain.dataset.id] = terrain
        if layer_count:
            imagery = _entry(
                SourceKind.IMAGERY, FileOrigin((), ()), {f"o{n}": tiny for n in range(layer_count)}
            )
            project.sources[imagery.dataset.id] = imagery
        session = Session(session_id="s", expected_token=None, hello_received=True, project=project)
        return asyncio.run(handlers.handle_imagery_tiles(session, {"level": level}))[0]

    assert len(listing(0, 0)["tiles"]) == 225  # the 5000-tile cap alone
    with pytest.raises(handlers.ProtocolError) as refused:
        listing(100, 0)  # 900 000 // (70 + 40 * 100) = 221 tiles
    assert refused.value.code == ErrorCode.BAD_PARAMS
    assert "221" in refused.value.message
    assert len(listing(100, 1)["tiles"]) == 64  # a coarser level still lists


def test_a_single_notice_string_is_a_one_item_tuple():
    provider = copernicus.CopernicusGlo30Provider()
    provider.notices = "one notice"
    registry.register_provider(provider)
    assert handlers._notices_for(ProviderOrigin("copernicus-glo30", "{}", ())) == ("one notice",)
