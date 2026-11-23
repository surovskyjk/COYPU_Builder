"""T-134 Part B: the ČÚZK orthophoto providers, `cuzk-ortho-wmts` (streamed) and `cuzk-ortho-sheets` (bulk).

Everything runs against local HTTP servers bound to 127.0.0.1 with synthetic tiles and sheets. The allow-list
stays in force; each test widens it to the loopback host only, as the Copernicus tests do. The one live test
(`-m network`) streams Kralupy imagery from the ČÚZK service and skips without a connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import os
import socket
import struct
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION, config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.imagery import compose_layers
from coypu_builder.domain.sources import BBoxClip, ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainGrid
from coypu_builder.domain.terrain_surface import GridSurface, TerrainStack
from coypu_builder.io.gis import cuzk_atom
from coypu_builder.io.gis.cuzk_atom import CuzkDataset, CuzkError
from coypu_builder.io.imagery import (
    cuzk_bulk,
    cuzk_wmts,
    layer_for_payload,
    register_imagery_readers,
    tile_report,
)
from coypu_builder.io.imagery.cuzk_wmts import (
    LAYER,
    TileMatrix,
    WmtsDescriptorReader,
    WmtsLayer,
    WmtsSource,
    choose_matrix,
    covering_tiles,
    parse_capabilities,
    read_descriptor,
    write_descriptor,
)
from coypu_builder.io.mesh.imagery_tiles import IMAGE_SIZE, compose_tile
from coypu_builder.io.mesh.terrain_tiles import tile_bounds
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.runner import inspect_provider, run_import, run_provider_import
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import handlers, serve
from coypu_builder.server.codec import decode_frame, encode_frame
from test_cuzk_atom import _dataset_feed, _entry_xml, _service_feed

KROVAK = ProjectCRS("EPSG:5514")
TILE_IX, TILE_IY = -1459, -2002  # the L0 tile of the synthetic world, as in test_imagery.py
TILE_W, TILE_N = TILE_IX * 512.0, (TILE_IY + 1) * 512.0
CLIP = (TILE_W - 200.0, TILE_N - 1100.0, TILE_W + 1000.0, TILE_N + 200.0)  # the L0 tile and more
SETTINGS = ImportSettings(horizontal_crs="EPSG:5514")
RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
BACKGROUND = (40, 40, 40)
TL_E, TL_N = -925000.0, -920000.0  # the TopLeftCorner of every TileMatrix: East, North
PIXEL_PER_SCALE = 0.00028
# (identifier, ScaleDenominator, MatrixWidth, MatrixHeight) of the ČÚZK default028mm TileMatrixSet
TILE_MATRICES = (
    ("0", 7315214.630428341, 1, 1),
    ("1", 3657607.3152141706, 2, 2),
    ("2", 1828803.6576070853, 4, 3),
    ("3", 914401.8288035427, 8, 5),
    ("4", 457200.9144017713, 16, 10),
    ("5", 228600.45720088566, 32, 19),
    ("6", 114300.22860044283, 63, 38),
    ("7", 57150.114300221416, 125, 76),
    ("8", 28575.057150110708, 249, 152),
    ("9", 14287.528575055354, 497, 304),
    ("10", 7143.764287527677, 993, 608),
    ("11", 3571.8821437638385, 1986, 1215),
    ("12", 1785.9410718819192, 3971, 2429),
    ("13", 892.9705359409596, 7942, 4858),
    ("14", 446.4852679704798, 15883, 9715),
)
WMTS_PATH = "/WMTS"
TILE_PREFIX = f"{WMTS_PATH}/tile/1.0.0/{LAYER}/default/default028mm/"
CAPS_QUERY = "?service=WMTS&request=GetCapabilities"
# the matrix each level 0 to 4 takes: the coarsest whose pixel is at most the level's pixel x 1.001,
# is 1.00013 m, so level 0 takes it; a strict comparison would take TileMatrix 12 and four times the tiles)
EXPECTED_MATRIX = {0: "11", 1: "10", 2: "9", 3: "8", 4: "7"}
L0_MATRIX = EXPECTED_MATRIX[0]
L0_WINDOW = (TILE_W, TILE_N - 512.0, TILE_W + 512.0, TILE_N)
BIG_CLIP = (TL_E - 1.0e7, TL_N - 1.0e7, TL_E + 1.0e7, TL_N + 1.0e7)  # hostile-test clip: everything is inside


def _pixel(tm_id: str) -> float:
    scale = next(s for i, s, _w, _h in TILE_MATRICES if i == tm_id)
    return scale * PIXEL_PER_SCALE


# --- the synthetic world: markers near three corners and off-centre, on a dark ground -----------------


def world_rgb(e: np.ndarray, n: np.ndarray) -> np.ndarray:
    rgb = np.full(e.shape + (3,), BACKGROUND, dtype=np.uint8)

    def paint(e0: float, e1: float, n0: float, n1: float, colour: tuple[int, int, int]) -> None:
        inside = (e >= e0) & (e < e1) & (n >= n0) & (n < n1)
        rgb[inside] = colour

    paint(TILE_W + 20, TILE_W + 28, TILE_N - 28, TILE_N - 20, RED)  # north-west
    paint(TILE_W + 484, TILE_W + 492, TILE_N - 28, TILE_N - 20, GREEN)  # north-east
    paint(TILE_W + 484, TILE_W + 492, TILE_N - 492, TILE_N - 484, BLUE)  # south-east
    paint(TILE_W + 300, TILE_W + 308, TILE_N - 158, TILE_N - 150, YELLOW)  # off-centre
    return rgb


def tile_image(tm_id: str, row: int, col: int) -> bytes:
    """A lossless PNG of one TileMatrix tile, sampled from the world at its pixel centres (row 0 is north)."""
    pixel = _pixel(tm_id)
    tile = 256 * pixel
    c = np.arange(256, dtype=np.float64)
    e = TL_E + col * tile + (c + 0.5) * pixel
    n = TL_N - row * tile - (c + 0.5) * pixel
    ee, nn = np.meshgrid(e, n)
    buffer = io.BytesIO()
    Image.fromarray(world_rgb(ee, nn)).save(buffer, format="PNG")
    return buffer.getvalue()


def _tile_world(tm_id: str, row: int, col: int) -> np.ndarray:
    pixel = _pixel(tm_id)
    tile = 256 * pixel
    c = np.arange(256, dtype=np.float64)
    e = TL_E + col * tile + (c + 0.5) * pixel
    n = TL_N - row * tile - (c + 0.5) * pixel
    ee, nn = np.meshgrid(e, n)
    return world_rgb(ee, nn)


def tile_jpeg(tm_id: str, row: int, col: int) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(_tile_world(tm_id, row, col)).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def tile_rgba_half_transparent(tm_id: str, row: int, col: int) -> bytes:
    """A PNG of one tile whose first 128 columns are fully transparent and the rest opaque."""
    rgba = np.zeros((256, 256, 4), dtype=np.uint8)
    rgba[..., :3] = _tile_world(tm_id, row, col)
    rgba[:, 128:, 3] = 255
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


def png_claiming_size(width: int, height: int) -> bytes:
    """A PNG whose header claims a huge image, with no pixels behind it: a decompression bomb in miniature."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


def expected_tiles(tm_id: str, bounds: tuple[float, float, float, float]) -> set[tuple[int, int]]:
    """(row, col) of every tile of `tm_id` whose square overlaps `bounds` with positive area. Computed here,
    independently of the provider's own arithmetic."""
    _id, _scale, width, height = next(t for t in TILE_MATRICES if t[0] == tm_id)
    tile = 256 * _pixel(tm_id)
    min_e, min_n, max_e, max_n = bounds
    found = set()
    for col in range(width):
        left, right = TL_E + col * tile, TL_E + (col + 1) * tile
        if not (left < max_e and right > min_e):
            continue
        for row in range(height):
            top, bottom = TL_N - row * tile, TL_N - (row + 1) * tile
            if top > min_n and bottom < max_n:
                found.add((row, col))
    return found


def capabilities_xml(base: str) -> bytes:
    """A GetCapabilities document shaped like the ČÚZK one: the ORTOFOTO layer, its tile resource template
    and the default028mm TileMatrixSet in EPSG:5514. Synthetic; the live check is the network test."""
    tiles = (
        f"{WMTS_PATH}/tile/1.0.0/{LAYER}/{{Style}}/{{TileMatrixSet}}/{{TileMatrix}}/{{TileRow}}/{{TileCol}}"
    )
    template = base + tiles
    matrices = "".join(
        f"<TileMatrix><ows:Identifier>{i}</ows:Identifier><ScaleDenominator>{scale!r}</ScaleDenominator>"
        f"<TopLeftCorner>{TL_E} {TL_N}</TopLeftCorner><TileWidth>256</TileWidth><TileHeight>256</TileHeight>"
        f"<MatrixWidth>{width}</MatrixWidth><MatrixHeight>{height}</MatrixHeight></TileMatrix>"
        for i, scale, width, height in TILE_MATRICES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Capabilities xmlns="http://www.opengis.net/wmts/1.0" xmlns:ows="http://www.opengis.net/ows/1.1" '
        'version="1.0.0"><ows:ServiceIdentification><ows:Title>ORTOFOTO</ows:Title>'
        "</ows:ServiceIdentification><Contents><Layer>"
        f"<ows:Title>{LAYER}</ows:Title><ows:Identifier>{LAYER}</ows:Identifier>"
        '<ows:BoundingBox crs="urn:ogc:def:crs:EPSG::5514">'
        "<ows:LowerCorner>-907841.0560214706 -1230916.868999999</ows:LowerCorner>"
        "<ows:UpperCorner>-416691.67027852684 -932111.7296999991</ows:UpperCorner>"
        "</ows:BoundingBox>"
        '<Style isDefault="true"><ows:Title>Default Style</ows:Title><ows:Identifier>default</ows:Identifier>'
        "</Style><Format>image/jpgpng</Format>"
        "<TileMatrixSetLink><TileMatrixSet>default028mm</TileMatrixSet></TileMatrixSetLink>"
        f'<ResourceURL format="image/jpgpng" resourceType="tile" template="{template}"/>'
        "</Layer><TileMatrixSet><ows:Identifier>default028mm</ows:Identifier>"
        "<ows:SupportedCRS>urn:ogc:def:crs:EPSG::5514</ows:SupportedCRS>"
        f"{matrices}</TileMatrixSet></Contents></Capabilities>\n"
    ).encode()


# --- fixtures ---------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Fresh registries with the built-in readers and providers, a private provider cache, and no waiting."""
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "user-config" / "config.toml")
    register_builtin_readers()
    register_imagery_readers()
    monkeypatch.setattr(provider_cache, "providers_cache_root", lambda: tmp_path / "provider-cache")
    monkeypatch.setattr(cuzk_wmts, "_sleep", lambda seconds: None)
    monkeypatch.setattr(cuzk_atom, "_sleep", lambda seconds: None)
    monkeypatch.setattr(
        cuzk_wmts, "_BREAKERS", {}
    )  # each test starts with closed breakers and no cache total
    monkeypatch.setattr(cuzk_wmts, "_CACHE_TOTALS", {})


class FakeClock:
    """The breaker's clock: a test moves it forward without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(cuzk_wmts, "_clock", fake)
    return fake


@dataclass
class WmtsState:
    base: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    log: list[dict[str, Any]] = field(default_factory=list)
    status: dict[str, int] = field(default_factory=dict)  # request path -> forced status, no body
    redirects: dict[str, str] = field(default_factory=dict)  # request path -> Location
    wrong_size: set[str] = field(default_factory=set)  # tile paths answered with a 128 x 128 image
    oversize: set[str] = field(default_factory=set)  # tile paths declared as 3 MB
    unbounded: set[str] = field(default_factory=set)  # tile paths streamed as 3 MB with no Content-Length
    jpeg: set[str] = field(default_factory=set)  # tile paths answered as JPEG rather than PNG
    raw: dict[str, bytes] = field(default_factory=dict)  # tile path -> the body answered instead of the tile
    delay: float = 0.0
    active: int = 0
    max_active: int = 0
    caps: bytes = b""

    def requests(self, method: str = "GET", path: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.log if r["method"] == method and (path is None or r["path"] == path)]

    def tile_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.log if r["method"] == "GET" and r["path"].startswith(TILE_PREFIX)]


def tile_path(tm_id: str, row: int, col: int) -> str:
    return f"{TILE_PREFIX}{tm_id}/{row}/{col}"


class WmtsHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def _handle(self, send_body: bool) -> None:
        state: WmtsState = self.server.state
        path = self.path.partition("?")[0]
        headers = {k.lower(): v for k, v in self.headers.items()}
        record: dict[str, Any] = {"method": self.command, "path": path, "headers": headers, "status": 0}
        with state.lock:
            state.log.append(record)
            state.active += 1
            state.max_active = max(state.max_active, state.active)
        try:
            self._respond(state, record, path, headers, send_body)
        finally:
            with state.lock:
                state.active -= 1

    def _respond(self, state, record, path, headers, send_body) -> None:
        if path in state.redirects:
            return self._send(record, 302, b"", {"Location": state.redirects[path]})
        if path in state.status:
            return self._send(record, state.status[path], b"", {})
        if path == WMTS_PATH:
            return self._send(record, 200, state.caps, {"Content-Type": "text/xml"}, send_body)
        if not path.startswith(TILE_PREFIX):
            return self._send(record, 404, b"", {})
        tm_id, row, col = path.removeprefix(TILE_PREFIX).split("/")
        if state.delay:
            time.sleep(state.delay)
        image = tile_image(tm_id, int(row), int(col))
        content_type = "image/png"
        if path in state.jpeg:
            image, content_type = tile_jpeg(tm_id, int(row), int(col)), "image/jpeg"
        if path in state.wrong_size:
            buffer = io.BytesIO()
            Image.fromarray(np.zeros((128, 128, 3), dtype=np.uint8)).save(buffer, format="PNG")
            image = buffer.getvalue()
        if path in state.raw:
            image = state.raw[path]
        etag = f'"{hashlib.md5(image).hexdigest()}"'
        if headers.get("if-none-match") == etag:
            return self._send(record, 304, b"", {"ETag": etag})
        if path in state.oversize:
            return self._stream(record, 3 * 1024 * 1024, declared=True, send_body=send_body)
        if path in state.unbounded:
            return self._stream(record, 3 * 1024 * 1024, declared=False, send_body=send_body)
        self._send(record, 200, image, {"ETag": etag, "Content-Type": content_type}, send_body)

    def _send(
        self, record, status: int, body: bytes, headers: dict[str, str], send_body: bool = True
    ) -> None:
        record["status"] = status
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)

    def _stream(self, record, count: int, *, declared: bool, send_body: bool) -> None:
        record["status"] = 200
        self.send_response(200)
        if declared:
            self.send_header("Content-Length", str(count))
        self.end_headers()
        if not send_body:
            return
        block = bytes(65536)
        sent = 0
        try:
            while sent < count:
                piece = block[: min(len(block), count - sent)]
                self.wfile.write(piece)
                sent += len(piece)
        except OSError:
            pass  # the client refused the body and closed the connection
        record["sent"] = sent


@pytest.fixture
def wmts(monkeypatch):
    """A local stand-in for the ČÚZK WMTS. The allow-list stays on; it only gains the loopback host."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), WmtsHandler)
    httpd.daemon_threads = True
    state = WmtsState(base=f"http://127.0.0.1:{httpd.server_address[1]}")
    state.caps = capabilities_xml(state.base)
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_PORTS", (None, httpd.server_address[1]))
    monkeypatch.setattr(cuzk_wmts, "CAPABILITIES_URL", state.base + WMTS_PATH + CAPS_QUERY)
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def requests_sent(monkeypatch):
    """Every URL a requests.Session is asked for. A refusal must leave this empty."""
    import requests

    sent: list[str] = []
    original = requests.Session.request

    def spy(self, method, url, **kwargs):
        sent.append(url)
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    return sent


def _bbox(box) -> dict[str, Any]:
    min_e, min_n, max_e, max_n = box
    return {"clip": {"type": "bbox", "min_e": min_e, "min_n": min_n, "max_e": max_e, "max_n": max_n}}


def _import_wmts(box=CLIP, *, project: ProjectCRS = KROVAK):
    return run_provider_import("cuzk-ortho-wmts", _bbox(box), SourceKind.IMAGERY, SETTINGS, project, {})


def _wmts_layer(outcome, source_id: str = "wmts") -> Any:
    (source,) = outcome.payloads.values()
    assert isinstance(source, WmtsSource)
    return layer_for_payload(source_id, source)


def _flat_terrain() -> TerrainStack:
    cell = 1.0
    size = 1100
    east = TILE_W - 200.0 + np.arange(size) * cell
    heights = np.tile(150.0 + 0.01 * (east - east[0]), (size, 1))
    grid = TerrainGrid(TILE_W - 200.0, TILE_N + 200.0, cell, cell, heights)
    return TerrainStack((GridSurface(grid),), ("terrain",))


def _colour_near(pixels: np.ndarray, row: int, col: int, colour: tuple[int, int, int], tol: int = 45) -> bool:
    window = pixels[row - 1 : row + 2, col - 1 : col + 2].astype(int)
    return bool(np.any(np.all(np.abs(window - np.array(colour)) <= tol, axis=2)))


def _marker_tile() -> tuple[int, int]:
    """(row, col) of the level-0 TileMatrix tile that holds the north-west marker."""
    pixel = 256 * _pixel(L0_MATRIX)
    col = int((TILE_W + 24 - TL_E) // pixel)
    row = int((TL_N - (TILE_N - 24)) // pixel)
    return row, col


# --- the matrix choice, the corner and the orientation ---------------------------------------------------


def test_wmts_matrix_choice_and_corner(wmts):
    outcome = _import_wmts()
    (source,) = outcome.payloads.values()
    assert (source.layer, source.tile_matrix_set) == (LAYER, "default028mm")
    assert (source.matrices[0].top_left_e, source.matrices[0].top_left_n) == (TL_E, TL_N)
    for level, expected in EXPECTED_MATRIX.items():
        assert choose_matrix(source.matrices, 512.0 * 2**level / IMAGE_SIZE).identifier == expected, level
    assert choose_matrix(source.matrices, 0.1).identifier == "14"  # finer than every matrix: the finest
    rgb, valid = _wmts_layer(outcome).render(L0_WINDOW, IMAGE_SIZE)
    assert valid.all()
    assert _colour_near(rgb, 23, 23, RED)  # north-west: top rows, west columns
    assert _colour_near(rgb, 23, 487, GREEN)  # north-east
    assert _colour_near(rgb, 487, 487, BLUE)  # south-east
    assert _colour_near(rgb, 153, 303, YELLOW)  # off-centre
    assert not _colour_near(rgb, 487, 23, RED)  # the north-west marker is not mirrored to the south
    assert _colour_near(rgb, 300, 300, BACKGROUND)


def test_the_capabilities_corner_is_east_north_as_the_matrix_widths_show(monkeypatch):
    """The first TopLeftCorner token is East. The fixture's corner has different East and North values, so a
    swapped reading fails the exact assertion (it would put East at -920000). The matrix widths then cover the
    layer's bounding box (491 km East, 299 km North) only in the East-North order."""
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_PORTS", (None, 9))
    source = parse_capabilities(capabilities_xml("http://127.0.0.1:9"), CLIP)
    min_e, min_n, max_e, max_n = (
        -907841.0560214706,
        -1230916.868999999,
        -416691.67027852684,
        -932111.7296999991,
    )
    tm = next(m for m in source.matrices if m.identifier == "2")  # 4 wide, 3 high
    assert (tm.top_left_e, tm.top_left_n) == (TL_E, TL_N)
    assert tm.matrix_width * tm.tile_edge_e >= max_e - min_e
    assert tm.matrix_height * tm.tile_edge_n >= max_n - min_n
    assert tm.top_left_e <= min_e and tm.top_left_n >= max_n


def test_wmts_tile_fetches_only_what_it_needs(wmts):
    outcome = _import_wmts()
    assert [r["path"] for r in wmts.log] == [WMTS_PATH]  # nothing but the capabilities at import
    layer = _wmts_layer(outcome)
    stack = TerrainStack(())
    tile = compose_tile([layer], stack, None, 0, TILE_IX, TILE_IY)
    wanted = expected_tiles(L0_MATRIX, L0_WINDOW)
    fetched = {tuple(int(v) for v in r["path"].split("/")[-2:]) for r in wmts.tile_requests()}
    assert fetched == wanted and len(wanted) == 9  # a 512 m window of 256.03 m tiles: 3 by 3
    assert all(r["path"].startswith(TILE_PREFIX + L0_MATRIX + "/") for r in wmts.tile_requests())
    assert tile.alpha is None and tile.source_ids == ("wmts",)
    print(f"L0 tile: {len(wanted)} WMTS tiles fetched cold")

    before = len(wmts.tile_requests())
    compose_tile([layer], stack, None, 0, TILE_IX, TILE_IY)
    assert len(wmts.tile_requests()) == before  # a second request is answered from the cache

    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    images = [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]
    assert len(images) == len(wanted)
    old = time.time() - 31 * 86400
    for image in images:
        os.utime(image, (old, old))
    compose_tile([layer], stack, None, 0, TILE_IX, TILE_IY)
    again = wmts.tile_requests()[before:]
    assert len(again) == len(wanted)
    assert all("if-none-match" in r["headers"] for r in again)
    assert all(r["status"] == 304 for r in again)  # revalidated: no body was sent
    assert min(p.stat().st_mtime for p in images) > old + 86400  # and the 304s refreshed their time


def test_wmts_refuses_foreign_hosts(wmts, requests_sent, monkeypatch, tmp_path):
    """Each refusal happens before any request. Only the redirect case reaches the loopback test server, and
    there the redirect target is the only host that is refused."""
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_SCHEMES", ("https",))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_HOSTS", ("cuzk.gov.cz",))
    refusals = {
        "foreign host": "https://evil.example/WMTS?service=WMTS&request=GetCapabilities",
        "plain http": "http://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS?request=x",
        "userinfo": "https://user:secret@ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS",
    }
    for name, url in refusals.items():
        monkeypatch.setattr(cuzk_wmts, "CAPABILITIES_URL", url)
        with pytest.raises(ValueError, match="refused URL"):
            _import_wmts()
        assert requests_sent == [], name
    assert wmts.log == []

    monkeypatch.setattr(cuzk_wmts, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_HOSTS", ("127.0.0.1",))
    monkeypatch.setattr(cuzk_wmts, "CAPABILITIES_URL", wmts.base + WMTS_PATH + CAPS_QUERY)
    wmts.redirects[WMTS_PATH] = "http://evil.example/steal"
    with pytest.raises(ValueError, match="refused URL"):
        _import_wmts()
    assert requests_sent and all(url.startswith(wmts.base) for url in requests_sent), requests_sent

    requests_sent.clear()
    source = parse_capabilities(capabilities_xml(wmts.base), CLIP)
    hostile = WmtsSource(
        "https://evil.example/tile/{TileMatrix}/{TileRow}/{TileCol}",
        source.layer,
        source.tile_matrix_set,
        source.matrices,
        CLIP,
    )
    descriptor = write_descriptor(tmp_path, hostile).rename(tmp_path / "hostile.wmts")
    with pytest.raises(ValueError, match="refused URL"):
        read_descriptor(descriptor)
    with pytest.raises(ValueError, match="refused URL"):
        run_import([descriptor], SourceKind.IMAGERY, SETTINGS, KROVAK, {}, format_id="wmts-descriptor")
    assert requests_sent == []


def test_wmts_503_degrades_to_relief(wmts):
    layer = _wmts_layer(_import_wmts())
    row, col = _marker_tile()
    wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    tile = compose_tile([layer], _flat_terrain(), (100.0, 200.0), 0, TILE_IX, TILE_IY)
    assert len(wmts.requests(path=tile_path(L0_MATRIX, row, col))) == 3  # retried twice, then given up
    assert "relief" in tile.source_ids and "wmts" in tile.source_ids
    warnings = layer.warning_messages()
    assert len(warnings) == 1 and "HTTP 503" in warnings[0] and f"{L0_MATRIX}/{row}/{col}" in warnings[0]
    jpeg = np.asarray(Image.open(io.BytesIO(tile.jpeg)).convert("RGB"))
    assert not _colour_near(jpeg, 23, 23, RED)  # relief, not the marker, where the tile failed
    assert _colour_near(jpeg, 487, 487, BLUE)  # the rest of the imagery still shows


def test_a_tile_of_the_wrong_size_or_over_the_cap_is_a_failed_fetch(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    row, col = _marker_tile()
    wmts.wrong_size.add(tile_path(L0_MATRIX, row, col))
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert not _colour_near(rgb, 23, 23, RED) and valid.sum() < valid.size
    assert any(
        "unusable tile" in w for w in layer.warning_messages()
    )  # the size is in the log, not the warning
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    assert not (root / L0_MATRIX / str(row) / f"{col}.png").exists()  # a bad tile is never kept

    # two tiles the first window never asked for: the two tiles east of the marker's
    tile_edge = 256 * _pixel(L0_MATRIX)
    far_min_e = TL_E + (col + 9) * tile_edge
    far = (far_min_e, TILE_N - 512.0, far_min_e + 512.0, TILE_N)
    wmts.oversize.add(tile_path(L0_MATRIX, row, col + 9))
    wmts.unbounded.add(tile_path(L0_MATRIX, row, col + 10))
    layer.render(far, IMAGE_SIZE)
    messages = " | ".join(layer.warning_messages())
    assert "Content-Length" in messages  # declared over the cap: refused before its body is read
    assert "the body is over the 2097152 bytes allowed" in messages  # streamed past the cap: cut off


def test_a_render_never_has_more_than_four_tile_requests_in_flight(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    wmts.delay = 0.05
    layer.render((TILE_W - 4096.0, TILE_N - 4096.0, TILE_W + 4096.0, TILE_N + 4096.0), IMAGE_SIZE)
    assert 2 <= wmts.max_active <= cuzk_wmts.TILE_CONCURRENCY


def test_the_tile_folder_is_bounded_and_the_oldest_tiles_go_first(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    first = L0_WINDOW
    second = (TILE_W + 1024.0, TILE_N - 512.0, TILE_W + 1536.0, TILE_N)
    layer.render(first, IMAGE_SIZE)
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    first_files = {p for p in root.rglob("*") if p.suffix in (".jpg", ".png")}
    assert first_files
    monkeypatch.setattr(cuzk_wmts, "TILE_CACHE_CAP_BYTES", 0)
    layer.render(second, IMAGE_SIZE)
    kept = {p for p in root.rglob("*") if p.suffix in (".jpg", ".png")}
    assert kept and not (first_files & kept)  # the first window's tiles went; the second's stayed


def test_a_project_not_in_epsg_5514_is_refused_for_the_wmts_provider(wmts):
    with pytest.raises(ValueError, match="EPSG:5514"):
        _import_wmts(box=(0.0, 0.0, 10.0, 10.0), project=ProjectCRS("EPSG:3035"))
    assert wmts.log == []


def test_a_descriptor_names_its_object_by_layer_and_matrix_set(wmts, tmp_path):
    source = parse_capabilities(capabilities_xml(wmts.base), CLIP)
    path = write_descriptor(tmp_path, source)
    assert path.name == f"{LAYER}.wmts" and b"\r" not in path.read_bytes()
    assert read_descriptor(path) == source
    outcome = run_import([path], SourceKind.IMAGERY, SETTINGS, KROVAK, {}, format_id="wmts-descriptor")
    assert [o.key for o in outcome.dataset.objects] == [f"{LAYER}/default028mm"]
    assert next(iter(outcome.payloads.values())) == source
    assert WmtsDescriptorReader().sniff(path)
    assert not WmtsDescriptorReader().sniff(tmp_path / "missing.wmts")


def test_a_capabilities_template_on_a_foreign_host_is_refused_before_any_tile(wmts):
    wmts.caps = wmts.caps.replace(wmts.base.encode(), b"https://evil.example")
    with pytest.raises(ValueError, match="refused URL"):
        _import_wmts()
    assert wmts.tile_requests() == []


def test_the_providers_carry_their_titles_licence_and_attribution():
    by_id = {p.provider_id: p for p in registry.providers()}
    streamed, sheets = by_id["cuzk-ortho-wmts"], by_id["cuzk-ortho-sheets"]
    assert streamed.description == (
        "Aerial photos of the Czech Republic, streamed tile by tile while you look at them. "
        "Nothing is downloaded in advance."
    )
    assert sheets.description == (
        "Aerial photos of the Czech Republic, downloaded one map sheet at a time for offline work. "
        "Large: check the size first."
    )
    for provider in (streamed, sheets):
        assert provider.licence == "CC BY 4.0" and provider.attribution == "© ČÚZK"
        assert tuple(provider.notices) == (
            "Licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Resampled for display.",
        )
        assert provider.kinds == frozenset({SourceKind.IMAGERY})
    dto = handlers._provider_dto(streamed)
    assert dto is not None and dto.attribution == "© ČÚZK" and dto.kinds == ("imagery",)


# --- the bulk sheets ---------------------------------------------------------------------------------

SHEET_BOXES = {
    "TESTA1": (-747100.0, -1025700.0, -746500.0, -1024300.0),
    "TESTB1": (-746500.0, -1025700.0, -745900.0, -1024300.0),
}
SHEET_CLIP = (-747000.0, -1025600.0, -746000.0, -1024400.0)  # across both sheets


def _outline(box) -> str:
    min_e, min_n, max_e, max_n = box
    e = np.array([min_e, max_e, max_e, min_e, min_e])
    n = np.array([min_n, min_n, max_n, max_n, min_n])
    lon, lat = KROVAK.to_wgs84(e, n)
    return " ".join(f"{float(la)!r} {float(lo)!r}" for lo, la in zip(lon, lat, strict=True))


def _sheet_zip(code: str, box) -> bytes:
    """A ČÚZK-style orthophoto sheet: a 1 m JPEG of the world with its world file, in one ZIP. As ČÚZK does,
    the members are named by the edition first (`WRTO24.2025.KRAV82.jpg`), so their stem is not the code."""
    min_e, min_n, max_e, max_n = box
    cell = 1.0
    cols, rows = round((max_e - min_e) / cell), round((max_n - min_n) / cell)
    e = min_e + (np.arange(cols) + 0.5) * cell
    n = max_n - (np.arange(rows) + 0.5) * cell
    ee, nn = np.meshgrid(e, n)
    jpeg = io.BytesIO()
    Image.fromarray(world_rgb(ee, nn)).save(jpeg, format="JPEG", quality=95)
    world = f"{cell}\n0.0\n0.0\n{-cell}\n{min_e + cell / 2}\n{max_n - cell / 2}\n".encode("ascii")
    member = f"WRTO24.2025.{code}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{member}.jpg", jpeg.getvalue())
        archive.writestr(f"{member}.jgw", world)
    return buffer.getvalue()


@dataclass
class AtomState:
    base: str = ""
    routes: dict[str, bytes] = field(default_factory=dict)
    log: list[tuple[str, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class AtomHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def _serve(self, send_body: bool) -> None:
        state: AtomState = self.server.state
        path = self.path.partition("?")[0]
        with state.lock:
            state.log.append((self.command, path))
        body = state.routes.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{hashlib.md5(body).hexdigest()}"')
        self.end_headers()
        if send_body:
            self.wfile.write(body)


@pytest.fixture
def ortho(monkeypatch):
    """A local ČÚZK ORTOFOTO service of two sheets. The allow-list gains the loopback host only."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AtomHandler)
    state = AtomState(base=f"http://127.0.0.1:{httpd.server_address[1]}")
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    entries = []
    for code, box in SHEET_BOXES.items():
        stem = f"{code}.WRTO24.2025"
        feed, file_path = f"/feeds/{stem}.xml", f"/files/{stem}.zip"
        body = _sheet_zip(code, box)
        polygon = _outline(box)
        entries.append(
            _entry_xml(
                f"CZ-00025712-CUZK_ORTOFOTO_WRTO24.2025.{code}",
                f"Synthetic - mapový list: Sheet {code}",
                state.base + feed,
                polygon,
                "2026-01-19T13:40:46+02:00",
            )
        )
        state.routes[feed] = _dataset_feed(state.base + file_path, len(body), polygon)
        state.routes[file_path] = body
    state.routes["/ORTOFOTO.xml"] = _service_feed(entries)
    monkeypatch.setattr(cuzk_atom, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_atom, "ALLOWED_HOSTS", ("127.0.0.1", *cuzk_atom.ALLOWED_HOSTS))
    monkeypatch.setitem(cuzk_atom.SERVICE_FEEDS, CuzkDataset.ORTOFOTO, state.base + "/ORTOFOTO.xml")
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_bulk_sheets_size_guard(ortho):
    refused = _bbox(SHEET_CLIP) | {"max_total_mb": 0.000001}
    with pytest.raises(ValueError, match=r"MB, over the limit of 1e-06 MB"):
        run_provider_import("cuzk-ortho-sheets", refused, SourceKind.IMAGERY, SETTINGS, KROVAK, {})
    assert [p for _m, p in ortho.log if p.startswith("/files/")] == []  # refused before any sheet is fetched

    report = inspect_provider("cuzk-ortho-sheets", _bbox(SHEET_CLIP), KROVAK, {})[1]
    assert report.counts["sheets"] == 2 and report.counts["bytes"] > 0
    assert report.declared_crs == "EPSG:5514" and report.licence_hint == "CC BY 4.0"
    print(f"synthetic sheet total: {report.counts['bytes']} bytes in {report.counts['sheets']} sheets")

    outcome = run_provider_import(
        "cuzk-ortho-sheets",
        _bbox(SHEET_CLIP) | {"max_total_mb": 200.0},
        SourceKind.IMAGERY,
        SETTINGS,
        KROVAK,
        {},
    )
    assert sorted(o.key for o in outcome.dataset.objects) == sorted(SHEET_BOXES)  # keyed by the sheet code
    assert outcome.dataset.licence == "CC BY 4.0" and outcome.dataset.attribution == "© ČÚZK"
    assert any("orthophoto editions" in w and "WRTO24.2025" in w for w in outcome.warnings)
    layers = [layer_for_payload(o.key, outcome.payloads[o.id]) for o in outcome.dataset.objects]
    rgb, valid, winners = compose_layers(layers, L0_WINDOW, IMAGE_SIZE)
    # the sheets are clipped to the query clip, whose west edge (E -747000) is eight columns into the window
    assert not valid[:, :6].any() and valid[:, 10:].all()
    assert set(winners) == set(SHEET_BOXES)
    assert _colour_near(rgb, 23, 23, RED)
    assert _colour_near(rgb, 153, 303, YELLOW)


def test_a_sheet_file_is_keyed_by_the_code_its_name_leads_with():
    assert cuzk_bulk.sheet_code(Path("ORTOFOTO/KRAV82.WRTO24.2025.zip")) == "KRAV82"
    with pytest.raises(ValueError):
        cuzk_bulk.sheet_code(Path("ORTOFOTO/.WRTO24.2025.zip"))


# --- the server: the streamed provider through import.run and imagery.tiles ---------------------------------


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None):
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    frame = await asyncio.wait_for(ws.recv(), 60)
    reply, _tail = decode_frame(frame)
    return reply


def _terrain_tif(path: Path) -> Path:
    """A 2 m height grid over the L0 tile of the synthetic world, in EPSG:5514, rising eastward."""
    rows = cols = 256
    heights = np.tile(150.0 + 0.01 * np.arange(cols) * 2.0, (rows, 1)).astype(np.float64)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="float64",
        crs="EPSG:5514",
        transform=Affine(2.0, 0.0, TILE_W, 0.0, -2.0, TILE_N),
    ) as ds:
        ds.write(heights, 1)
    return path


async def test_the_wmts_provider_through_the_server(server_url, wmts, tmp_path):
    terrain = _terrain_tif(tmp_path / "terrain.tif")
    async with connect(server_url) as ws:
        early = await _call(ws, "f0", "import.formats", {})
        assert early.type == "err"  # no session yet
        assert (
            await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        ).type == "res"
        assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})).type == "res"
        listed = await _call(ws, "fm", "import.formats", {})
        providers = {p["provider_id"]: p for p in listed.result["providers"]}
        assert providers["cuzk-ortho-wmts"]["attribution"] == "© ČÚZK"
        assert providers["cuzk-ortho-sheets"]["licence"] == "CC BY 4.0"
        terrain_reply = await _call(
            ws,
            "t",
            "import.run",
            {"path": str(terrain), "kind": "terrain", "settings": {"horizontal_crs": "EPSG:5514"}},
        )
        assert terrain_reply.type == "res", terrain_reply.error
        imported = await _call(
            ws,
            "i",
            "import.run",
            {
                "provider": "cuzk-ortho-wmts",
                "query": _bbox(CLIP),
                "kind": "imagery",
                "settings": {"horizontal_crs": "EPSG:5514"},
            },
        )
        assert imported.type == "res", imported.error
        source_id = imported.result["source_id"]
        listing = await _call(ws, "l", "imagery.tiles", {"level": 0})
        assert listing.type == "res" and listing.result["warnings"] == []
        tile = next(t for t in listing.result["tiles"] if (t["ix"], t["iy"]) == (TILE_IX, TILE_IY))
        assert tile["source_ids"] == [source_id]
        row, col = _marker_tile()
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
        composed = await _call(
            ws,
            "c",
            "imagery.tile",
            {"level": 0, "ix": TILE_IX, "iy": TILE_IY, "revision": listing.result["revision"]},
        )
        assert composed.type == "res", composed.error
        assert "relief" in composed.result["source_ids"]
        listing = await _call(ws, "l2", "imagery.tiles", {"level": 0})
        assert any("HTTP 503" in w for w in listing.result["warnings"]), listing.result["warnings"]
        assert listing.result["attributions"][source_id] == "© ČÚZK"
        assert listing.result["notices"][source_id] == [cuzk_wmts.NOTICE]


# --- the allow-list, the hostile input, the clip, the alpha, the disk and the breaker ----------------------

OTHER_WINDOW = (TILE_W + 1024.0, TILE_N - 512.0, TILE_W + 1536.0, TILE_N)  # two tiles east of the L0 window


def _wmts_tiles(window: tuple[float, float, float, float] = L0_WINDOW) -> set[tuple[int, int]]:
    return expected_tiles(L0_MATRIX, window)


def _fetched_tiles(wmts: WmtsState) -> set[tuple[int, int]]:
    return {tuple(int(v) for v in r["path"].split("/")[-2:]) for r in wmts.tile_requests()}


def _breaker_for_test():
    return cuzk_wmts._breaker("127.0.0.1")


def test_production_allow_list_is_one_host():
    """The module's production defaults, read before any test widens them."""
    assert cuzk_wmts.ALLOWED_SCHEMES == ("https",)
    assert cuzk_wmts.ALLOWED_HOSTS == ("ags.cuzk.gov.cz",)
    assert cuzk_wmts.ALLOWED_PORTS == (None, 443)
    assert cuzk_wmts.CAPABILITIES_URL.startswith("https://ags.cuzk.gov.cz/")
    for url in ("https://ags.cuzk.gov.cz/arcgis1/x", "https://ags.cuzk.gov.cz:443/arcgis1/x"):
        assert cuzk_wmts._check_url(url) == url
    for url in (
        "https://cuzk.gov.cz/x",
        "https://ags.cuzk.gov.cz.evil.example/x",
        "https://x.ags.cuzk.gov.cz/x",
        "https://ags.cuzk.gov.cz:8443/x",
        "http://ags.cuzk.gov.cz/x",
        "https://user:pw@ags.cuzk.gov.cz/x",
    ):
        with pytest.raises(ValueError, match="refused URL"):
            cuzk_wmts._check_url(url)


def test_redirect_loop_is_refused_after_five_hops(wmts):
    wmts.redirects[WMTS_PATH] = wmts.base + WMTS_PATH  # the capabilities answer with a redirect to themselves
    with pytest.raises(OSError, match="more than 5 redirects"):
        _import_wmts()
    assert len(wmts.requests(path=WMTS_PATH)) == 6  # the request, and five followed redirects


def test_the_tile_matrix_tolerance_is_a_tenth_of_a_percent():
    """A matrix of 1.0005 m qualifies for a 1 m target; one of 1.002 m does not."""

    def matrix(identifier: str, pixel_m: float) -> TileMatrix:
        return TileMatrix(identifier, pixel_m / PIXEL_PER_SCALE, TL_E, TL_N, 256, 256, 4, 4)

    assert choose_matrix([matrix("q", 1.0005), matrix("x", 2.0)], 1.0).identifier == "q"
    assert choose_matrix([matrix("c", 1.002), matrix("f", 0.5)], 1.0).identifier == "f"


def test_hostile_matrices_and_tile_counts_fetch_nothing(wmts, requests_sent, tmp_path):
    source = parse_capabilities(capabilities_xml(wmts.base), CLIP)
    base = source.matrices[0]
    hostile_matrices = (
        replace(base, scale_denominator=1.0),  # a pixel of 0.00028 m, below 0.01 m
        replace(base, scale_denominator=1.0e6 / PIXEL_PER_SCALE),  # a pixel of 1 000 km
        replace(base, tile_width=2048, tile_height=2048),  # a tile of 2048 px, above 1024
    )
    for hostile in hostile_matrices:
        path = write_descriptor(tmp_path, replace(source, matrices=(hostile,))).replace(
            tmp_path / "hostile.wmts"
        )
        with pytest.raises(ValueError):
            read_descriptor(path)
    assert requests_sent == []

    scenarios = {
        "81 tiles of 1024 px": (
            TileMatrix("a", 1.0 / PIXEL_PER_SCALE, TL_E, TL_N, 1024, 1024, 10**5, 10**5),
            (TL_E, TL_N - 9216.0, TL_E + 9216.0, TL_N),
        ),
        "64 tiles of 1024 px, a 67 Mpx mosaic": (
            TileMatrix("b", 1.0 / PIXEL_PER_SCALE, TL_E, TL_N, 1024, 1024, 10**5, 10**5),
            (TL_E, TL_N - 8192.0, TL_E + 8192.0, TL_N),
        ),
        "about 1.5e11 tiles of 256 px": (
            TileMatrix("c", 0.01 / PIXEL_PER_SCALE, TL_E, TL_N, 256, 256, 10**5, 10**5),
            (TL_E, TL_N - 1.0e6, TL_E + 1.0e6, TL_N),
        ),
    }
    for name, (matrix, window) in scenarios.items():
        source_hostile = WmtsSource(
            wmts.base + TILE_PREFIX + "{TileMatrix}/{TileRow}/{TileCol}",
            LAYER,
            "default028mm",
            (matrix,),
            BIG_CLIP,
        )
        layer = WmtsLayer("hostile", source_hostile)
        rgb, valid = layer.render(window, 512)
        assert not valid.any() and not rgb.any(), name
        messages = layer.warning_messages()
        assert len(messages) == 1 and "tiles" in messages[0], name
    assert requests_sent == [] and wmts.tile_requests() == []


def test_render_respects_the_clip(wmts):
    clip = (TILE_W + 256.0, TILE_N - 512.0, TILE_W + 1000.0, TILE_N + 200.0)
    layer = _wmts_layer(_import_wmts(box=clip))
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert (
        not valid[:, :255].any() and valid[:, 257:].all()
    )  # the columns west of the clip edge are not valid
    assert not _colour_near(rgb, 23, 23, RED)  # the north-west marker lies outside the clip
    assert _fetched_tiles(wmts) == _wmts_tiles((clip[0], clip[1], L0_WINDOW[2], L0_WINDOW[3]))


def test_png_alpha_is_not_valid(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles():
        wmts.raw[tile_path(L0_MATRIX, row, col)] = tile_rgba_half_transparent(L0_MATRIX, row, col)
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert 0.4 < valid.mean() < 0.6  # the transparent halves of the tiles are not valid
    assert not rgb[~valid].any()
    assert layer.warning_messages() == ()  # a transparent tile is a good tile


def test_jpeg_tiles_are_stored_and_reused(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    wanted = _wmts_tiles()
    wmts.jpeg = {tile_path(L0_MATRIX, row, col) for row, col in wanted}
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert valid.all() and _colour_near(rgb, 23, 23, RED)
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    stored = [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]
    assert len(stored) == len(wanted) and all(p.suffix == ".jpg" for p in stored)
    before = len(wmts.tile_requests())
    again, _again_valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert len(wmts.tile_requests()) == before  # the stored JPEGs are answered from the cache
    assert np.array_equal(again, rgb)


def test_decoder_errors_are_failed_tiles(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    garbage_tile, truncated_tile, bomb_tile = sorted(_wmts_tiles())[:3]
    good = tile_jpeg(L0_MATRIX, *truncated_tile)
    wmts.raw[tile_path(L0_MATRIX, *garbage_tile)] = b"this is not an image at all"
    wmts.raw[tile_path(L0_MATRIX, *truncated_tile)] = good[: len(good) // 2]
    wmts.raw[tile_path(L0_MATRIX, *bomb_tile)] = png_claiming_size(100_000, 100_000)
    layer.render(L0_WINDOW, IMAGE_SIZE)  # raises nothing
    messages = layer.warning_messages()
    for row, col in (garbage_tile, truncated_tile, bomb_tile):
        assert any(f"{L0_MATRIX}/{row}/{col} not loaded (unusable tile" in m for m in messages), (row, col)
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    for row, col in (garbage_tile, truncated_tile, bomb_tile):
        assert not (root / L0_MATRIX / str(row) / f"{col}.png").exists()  # a bad tile is never kept
    assert _breaker_for_test().state == "closed"  # an answer that is not an image is not a host failure


def test_disk_errors_degrade_and_do_not_raise(wmts, monkeypatch):
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    real_read, real_unlink, real_write = Path.read_bytes, Path.unlink, cuzk_wmts._atomic_write

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "access denied")

    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    monkeypatch.setattr(cuzk_wmts, "_atomic_write", denied)
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)  # the decoded tiles are used, uncached
    assert valid.all() and _colour_near(rgb, 23, 23, RED)
    assert layer.warning_messages() == ()
    assert not any(root.rglob("*.png"))
    monkeypatch.setattr(cuzk_wmts, "_atomic_write", real_write)
    layer.render(L0_WINDOW, IMAGE_SIZE)  # stores the tiles
    assert any(root.rglob("*.png"))

    def unreadable(self, *args, **kwargs):
        if str(self).startswith(str(root)):
            raise PermissionError(13, "access denied")
        return real_read(self, *args, **kwargs)

    def undeletable(self, *args, **kwargs):
        if str(self).startswith(str(root)):
            raise PermissionError(13, "access denied")
        return real_unlink(self, *args, **kwargs)

    victim = next(root.rglob("*.png"))
    victim.write_bytes(b"garbage, not an image")  # a stored tile that no longer decodes
    monkeypatch.setattr(Path, "unlink", undeletable)
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)  # it is discarded, which fails; the render does not
    assert valid.all() and _colour_near(rgb, 23, 23, RED)
    monkeypatch.setattr(Path, "read_bytes", unreadable)
    rgb, valid = layer.render(
        L0_WINDOW, IMAGE_SIZE
    )  # the stored tiles cannot be read: they are fetched again
    assert valid.all() and _colour_near(rgb, 23, 23, RED)
    monkeypatch.setattr(cuzk_wmts, "TILE_CACHE_CAP_BYTES", 0)
    monkeypatch.setattr(cuzk_wmts, "_scan", denied)
    layer.render(OTHER_WINDOW, IMAGE_SIZE)  # the cap cannot be checked: the render still returns


def test_cache_cap_walks_only_when_over(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    walks: list[str] = []
    real_scan = cuzk_wmts._scan

    def counting(root):
        walks.append(str(root))
        return real_scan(root)

    monkeypatch.setattr(cuzk_wmts, "_scan", counting)
    layer.render(L0_WINDOW, IMAGE_SIZE)
    assert len(walks) == 1  # the first store seeds the running total with one walk
    layer.render(OTHER_WINDOW, IMAGE_SIZE)  # more tiles, far under the cap
    assert len(walks) == 1
    monkeypatch.setattr(cuzk_wmts, "TILE_CACHE_CAP_BYTES", 0)
    layer.render((TILE_W + 2048.0, TILE_N - 512.0, TILE_W + 2560.0, TILE_N), IMAGE_SIZE)
    assert len(walks) == 2  # over the cap: one walk, which evicts


def test_failed_revalidation_keeps_the_stale_tile(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    layer.render(L0_WINDOW, IMAGE_SIZE)
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    old = time.time() - 31 * 86400
    for image in [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]:
        os.utime(image, (old, old))
    reasons: list[str] = []
    real_failed = cuzk_wmts._Breaker.failed

    def recording(self, reason, probe):
        reasons.append(reason)
        real_failed(self, reason, probe)

    monkeypatch.setattr(cuzk_wmts._Breaker, "failed", recording)
    row, col = _marker_tile()
    wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert _colour_near(rgb, 23, 23, RED) and valid.all()  # the stale copy is still shown
    assert any("HTTP 503" in reason for reason in reasons)  # and the failure counted toward the breaker
    del wmts.status[tile_path(L0_MATRIX, row, col)]
    wmts.raw[tile_path(L0_MATRIX, row, col)] = b"<html>an error page</html>"
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert _colour_near(rgb, 23, 23, RED) and valid.all()
    assert any("unusable tile" in reason for reason in reasons)  # a bad body counts too
    assert (root / L0_MATRIX / str(row) / f"{col}.png").exists()  # and the stale copy is kept


def test_fetch_slots_are_shared_across_renders(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    wmts.delay = 0.05
    windows = [
        (TILE_W - 4096.0, TILE_N - 4096.0, TILE_W + 4096.0, TILE_N + 4096.0),
        (TILE_W + 16384.0, TILE_N - 4096.0, TILE_W + 24576.0, TILE_N + 4096.0),
    ]
    threads = [threading.Thread(target=layer.render, args=(window, IMAGE_SIZE)) for window in windows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(wmts.tile_requests()) >= 8
    assert 2 <= wmts.max_active <= cuzk_wmts.TILE_CONCURRENCY  # two renders at once, never more than four


def test_breaker_trips_after_three(wmts, clock):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles():
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    layer.render(L0_WINDOW, IMAGE_SIZE)
    assert _breaker_for_test().state == "open" and _breaker_for_test().cool_down_s == 30.0
    aggregated = [m for m in layer.warning_messages() if "not reachable" in m]
    assert aggregated == [
        "ČÚZK orthophoto is not reachable (HTTP 503); shaded relief is shown; retrying in 30 s"
    ]


def test_breaker_open_makes_no_request(wmts, clock, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles() | _wmts_tiles(OTHER_WINDOW):
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    layer.render(L0_WINDOW, IMAGE_SIZE)
    assert _breaker_for_test().state == "open"
    sleeps: list[float] = []
    monkeypatch.setattr(cuzk_wmts, "_sleep", sleeps.append)
    before = len(wmts.tile_requests())
    rgb, valid = layer.render(OTHER_WINDOW, IMAGE_SIZE)
    assert len(wmts.tile_requests()) == before  # no request while the breaker is open
    assert sleeps == []  # and no backoff either
    assert not valid.any()
    assert len([m for m in layer.warning_messages() if "not reachable" in m]) == 1  # one aggregated warning


def test_breaker_probes_once_after_cool_down(wmts, clock):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles() | _wmts_tiles(OTHER_WINDOW):
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    layer.render(L0_WINDOW, IMAGE_SIZE)
    breaker = _breaker_for_test()
    assert breaker.state == "open"
    clock.now += 29.0
    before = len(wmts.tile_requests())
    layer.render(OTHER_WINDOW, IMAGE_SIZE)
    assert len(wmts.tile_requests()) == before  # still cooling down
    clock.now += 1.0  # 30 s: one probe, with one attempt, for all the tiles of the window
    layer.render(OTHER_WINDOW, IMAGE_SIZE)
    assert len(wmts.tile_requests()) == before + 1
    assert breaker.state == "open" and breaker.cool_down_s == 60.0  # the failed probe doubles the cool-down
    wmts.status.clear()
    clock.now += 60.0  # the probe after the doubled cool-down succeeds
    layer.render(OTHER_WINDOW, IMAGE_SIZE)
    assert breaker.state == "closed" and breaker.cool_down_s == 30.0


def test_breaker_resets_on_success(wmts):
    breaker = _breaker_for_test()
    breaker.failed("HTTP 503", probe=False)
    breaker.failed("HTTP 503", probe=False)
    assert breaker.state == "closed" and breaker.failures == 2
    breaker.succeeded(probe=False)
    assert breaker.failures == 0
    breaker.failed("HTTP 503", probe=False)
    breaker.failed("HTTP 503", probe=False)
    assert breaker.state == "closed"  # two after the reset are not three
    breaker.failed("HTTP 503", probe=False)
    assert breaker.state == "open"


def test_missing_tiles_do_not_trip_the_breaker(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles():
        wmts.status[tile_path(L0_MATRIX, row, col)] = 404
    with tile_report() as report:
        rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    breaker = _breaker_for_test()
    assert breaker.state == "closed" and breaker.failures == 0
    assert not valid.any()
    assert report.missing == 0  # a 404 is an answer: the composition is complete
    assert layer.warning_messages() == ()  # and there is nothing to warn anyone about


def test_a_render_past_its_deadline_degrades_at_once(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    wmts.delay = 1.0
    monkeypatch.setattr(cuzk_wmts, "RENDER_DEADLINE_S", 0.2)
    started = time.perf_counter()
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert time.perf_counter() - started < 0.9  # the render does not wait for the slow server
    assert not valid.any()
    assert any("deadline" in m for m in layer.warning_messages())


def test_the_descriptor_clip_is_narrowed_by_the_import_clip(wmts, tmp_path):
    source = parse_capabilities(capabilities_xml(wmts.base), BIG_CLIP)
    path = write_descriptor(tmp_path, source).replace(tmp_path / "narrow.wmts")
    narrow = BBoxClip(TILE_W, TILE_N - 512.0, TILE_W + 512.0, TILE_N)
    settings = ImportSettings(horizontal_crs="EPSG:5514", clip=narrow)
    outcome = run_import([path], SourceKind.IMAGERY, settings, KROVAK, {}, format_id="wmts-descriptor")
    (payload,) = outcome.payloads.values()
    assert payload.clip == (TILE_W, TILE_N - 512.0, TILE_W + 512.0, TILE_N)
    far_box = BBoxClip(2.0e7, 2.0e7, 2.0e7 + 1.0, 2.0e7 + 1.0)  # outside BIG_CLIP, which reaches 9.1e6 east
    far = ImportSettings(horizontal_crs="EPSG:5514", clip=far_box)
    with pytest.raises(ValueError, match="does not overlap"):
        run_import([path], SourceKind.IMAGERY, far, KROVAK, {}, format_id="wmts-descriptor")


async def test_incomplete_composition_is_not_cached_and_says_so(server_url, wmts, tmp_path):
    terrain = _terrain_tif(tmp_path / "terrain.tif")
    async with connect(server_url) as ws:
        assert (
            await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        ).type == "res"
        assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})).type == "res"
        assert (
            await _call(
                ws,
                "t",
                "import.run",
                {"path": str(terrain), "kind": "terrain", "settings": {"horizontal_crs": "EPSG:5514"}},
            )
        ).type == "res"
        imported = await _call(
            ws,
            "i",
            "import.run",
            {
                "provider": "cuzk-ortho-wmts",
                "query": _bbox(CLIP),
                "kind": "imagery",
                "settings": {"horizontal_crs": "EPSG:5514"},
            },
        )
        assert imported.type == "res", imported.error
        source_id = imported.result["source_id"]
        listing = await _call(ws, "l", "imagery.tiles", {"level": 0})
        row, col = _marker_tile()
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
        params = {"level": 0, "ix": TILE_IX, "iy": TILE_IY, "revision": listing.result["revision"]}
        first = await _call(ws, "c1", "imagery.tile", params)
        assert first.type == "res" and first.result["complete"] is False
        after_first = len(wmts.tile_requests())
        del wmts.status[tile_path(L0_MATRIX, row, col)]  # the service recovers
        second = await _call(ws, "c2", "imagery.tile", params)
        assert second.type == "res" and second.result["complete"] is True
        assert (
            len(wmts.tile_requests()) > after_first
        )  # the degraded composition was not kept: it was composed again
        assert second.result["source_ids"] == [source_id]


# --- the re-review: warnings for people, answers, the deadline, the breaker's honesty ----------------------


def tile_palette_half_transparent(tm_id: str, row: int, col: int) -> bytes:
    """A palette PNG whose index 0 is transparent: the first 128 columns of the tile are index 0."""
    colours = [BACKGROUND, RED, GREEN, BLUE, YELLOW]
    world = _tile_world(tm_id, row, col)
    index = np.zeros((256, 256), dtype=np.uint8)
    for number, colour in enumerate(colours, start=1):
        index[np.all(world == np.array(colour, dtype=np.uint8), axis=2)] = number
    index[:, :128] = 0
    image = Image.new("P", (256, 256))
    image.frombytes(index.tobytes())
    palette = [0, 0, 0] + [channel for colour in colours for channel in colour]
    image.putpalette(palette + [0] * (768 - len(palette)))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", transparency=0)
    return buffer.getvalue()


def test_a_connection_error_warning_is_short_and_says_cannot_connect(wmts, monkeypatch):
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()  # nothing listens on this port now: the connection is refused
    layer = _wmts_layer(
        _import_wmts(BIG_CLIP)
    )  # the capabilities come from the stand-in, as in the other tests
    monkeypatch.setattr(cuzk_wmts, "ALLOWED_PORTS", (None, port))
    unreachable = WmtsLayer(
        "unreachable",
        replace(
            layer.source,
            service_url=f"http://127.0.0.1:{port}" + TILE_PREFIX + "{TileMatrix}/{TileRow}/{TileCol}",
        ),
    )
    unreachable.render(L0_WINDOW, IMAGE_SIZE)
    aggregated = [m for m in unreachable.warning_messages() if "not reachable" in m]
    assert len(aggregated) == 1
    assert "cannot connect" in aggregated[0] and len(aggregated[0]) < 120
    assert (
        "Errno" not in aggregated[0] and "127.0.0.1" not in aggregated[0]
    )  # the exception text is in the log


def test_a_warning_clears_when_its_tile_loads_and_the_breaker_closes(wmts, clock):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles() | _wmts_tiles(OTHER_WINDOW):
        wmts.status[tile_path(L0_MATRIX, row, col)] = 503
    layer.render(L0_WINDOW, IMAGE_SIZE)
    assert any("not loaded" in m for m in layer.warning_messages())
    wmts.status.clear()
    clock.now += 30.0
    layer.render(L0_WINDOW, IMAGE_SIZE)  # the probe succeeds and closes the breaker
    layer.render(L0_WINDOW, IMAGE_SIZE)  # a render with nothing missing: every tile warning goes
    assert _breaker_for_test().state == "closed"
    assert layer.warning_messages() == ()


def test_the_deadline_shows_the_stale_copies(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    layer.render(L0_WINDOW, IMAGE_SIZE)  # stores the nine tiles
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    old = time.time() - 31 * 86400
    for image in [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]:
        os.utime(image, (old, old))
    wmts.delay = 1.0  # the revalidations answer slowly, past the render's deadline
    monkeypatch.setattr(cuzk_wmts, "RENDER_DEADLINE_S", 0.2)
    started = time.perf_counter()
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert time.perf_counter() - started < 0.9
    assert valid.all() and _colour_near(rgb, 23, 23, RED)  # the stale copies are shown
    assert not any("not loaded" in m for m in layer.warning_messages())


def test_a_404_is_an_answer_remembered_for_thirty_days(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    row, col = _marker_tile()
    path = tile_path(L0_MATRIX, row, col)
    wmts.status[path] = 404
    with tile_report() as first:
        rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert first.missing == 0 and layer.warning_messages() == ()  # complete, and nothing to warn about
    assert valid.any() and not valid.all()  # the 404's pixels are not valid
    assert len(wmts.requests(path=path)) == 1
    with tile_report() as second:
        layer.render(L0_WINDOW, IMAGE_SIZE)
    assert second.missing == 0
    assert len(wmts.requests(path=path)) == 1  # no request on the second render: the answer is remembered
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    assert (root / L0_MATRIX / str(row) / f"{col}.absent").exists()


def test_a_worker_that_waited_for_a_slot_rechecks_the_breaker(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    held = [cuzk_wmts._SLOTS.acquire() for _ in range(cuzk_wmts.TILE_CONCURRENCY)]
    assert all(held)  # this test holds every fetch slot
    worker = threading.Thread(target=layer.render, args=(L0_WINDOW, IMAGE_SIZE))
    worker.start()
    time.sleep(0.3)  # the workers have passed the breaker and are waiting for a slot
    breaker = _breaker_for_test()
    for _ in range(cuzk_wmts.BREAKER_TRIP_AFTER):
        breaker.failed("HTTP 503", probe=False)
    assert breaker.state == "open"
    for _ in range(cuzk_wmts.TILE_CONCURRENCY):
        cuzk_wmts._SLOTS.release()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert wmts.tile_requests() == []  # no worker sent a request once the breaker was open


def test_bad_revalidation_bodies_trip_the_breaker_when_serial(wmts, monkeypatch):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    layer.render(L0_WINDOW, IMAGE_SIZE)
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    old = time.time() - 31 * 86400
    for image in [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]:
        os.utime(image, (old, old))
    for row, col in _wmts_tiles():
        wmts.raw[tile_path(L0_MATRIX, row, col)] = b"<html>an error page, not a tile</html>"
    monkeypatch.setattr(cuzk_wmts, "TILE_CONCURRENCY", 1)  # one worker: the bodies arrive one at a time
    before = len(wmts.tile_requests())
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert _breaker_for_test().state == "open"  # the third bad body tripped it
    assert len(wmts.tile_requests()) - before == 3  # the rest made no request
    assert valid.all() and _colour_near(rgb, 23, 23, RED)  # and the stale copies are shown


def test_a_corrupt_validator_sidecar_is_ignored(tmp_path):
    image = tmp_path / "3.png"
    image.write_bytes(b"x")
    image.with_suffix(".json").write_text("[1, 2]", encoding="utf-8")  # valid JSON, not an object
    assert cuzk_wmts._validators(image) is None
    image.with_suffix(".json").write_text('{"etag": 5}', encoding="utf-8")  # an object with no usable field
    assert cuzk_wmts._validators(image) is None


def test_a_render_over_64_tiles_is_refused_below_the_pixel_cap(wmts, requests_sent):
    """81 tiles of 256 px make 5.3 Mpx, under the mosaic cap, so only the tile count refuses them."""
    matrix = TileMatrix("t", 1.0 / PIXEL_PER_SCALE, TL_E, TL_N, 256, 256, 10**5, 10**5)
    source = WmtsSource(
        wmts.base + TILE_PREFIX + "{TileMatrix}/{TileRow}/{TileCol}",
        LAYER,
        "default028mm",
        (matrix,),
        BIG_CLIP,
    )
    layer = WmtsLayer("many", source)
    rgb, valid = layer.render((TL_E, TL_N - 2304.0, TL_E + 2304.0, TL_N), 512)
    assert not valid.any() and requests_sent == []
    assert len(layer.warning_messages()) == 1


def test_a_palette_png_with_transparency_is_not_valid_where_transparent(wmts):
    layer = _wmts_layer(_import_wmts(BIG_CLIP))
    for row, col in _wmts_tiles():
        wmts.raw[tile_path(L0_MATRIX, row, col)] = tile_palette_half_transparent(L0_MATRIX, row, col)
    rgb, valid = layer.render(L0_WINDOW, IMAGE_SIZE)
    assert 0.4 < valid.mean() < 0.6  # the transparent index covers half of each tile
    assert not rgb[~valid].any()
    assert layer.warning_messages() == ()


def test_an_in_flight_success_does_not_close_an_open_breaker(clock):
    breaker = _breaker_for_test()
    for _ in range(cuzk_wmts.BREAKER_TRIP_AFTER):
        breaker.failed("HTTP 503", probe=False)
    assert breaker.state == "open"
    breaker.succeeded(probe=False)  # a request that was in flight when the breaker opened answers
    assert breaker.state == "open"
    clock.now += 30.0
    assert breaker.admit() is True  # the probe
    breaker.succeeded(probe=False)  # and a non-probe success while it is half-open
    assert breaker.state == "half_open"


def test_the_cool_down_stops_at_five_minutes(clock):
    breaker = _breaker_for_test()
    for _ in range(cuzk_wmts.BREAKER_TRIP_AFTER):
        breaker.failed("HTTP 503", probe=False)
    for _ in range(8):
        clock.now += breaker.cool_down_s
        assert breaker.admit() is True
        breaker.failed("HTTP 503", probe=True)
    assert breaker.cool_down_s == cuzk_wmts.BREAKER_COOL_DOWN_MAX_S == 300.0


def test_the_breaker_stops_a_worker_between_retries(wmts, monkeypatch):
    path = tile_path(L0_MATRIX, 0, 0)
    wmts.status[path] = 503
    breaker = _breaker_for_test()

    def trip_while_backing_off(_seconds: float) -> None:
        for _ in range(cuzk_wmts.BREAKER_TRIP_AFTER):
            breaker.failed("HTTP 503", probe=False)

    monkeypatch.setattr(cuzk_wmts, "_sleep", trip_while_backing_off)
    with cuzk_wmts._Client() as client:
        with pytest.raises(cuzk_wmts._BreakerOpen):
            client.get(wmts.base + path, limit=cuzk_wmts.MAX_TILE_BYTES)
    assert len(wmts.requests(path=path)) == 1  # the retry after the backoff was not sent


def test_a_probe_abandoned_at_the_deadline_does_not_leave_the_breaker_half_open(wmts, clock):
    url = wmts.base + tile_path(L0_MATRIX, 0, 0)
    breaker = _breaker_for_test()
    for _ in range(cuzk_wmts.BREAKER_TRIP_AFTER):
        breaker.failed("HTTP 503", probe=False)
    clock.now += 30.0
    with cuzk_wmts._Client() as client:
        with pytest.raises(cuzk_wmts._Deadline):
            client.get(url, limit=cuzk_wmts.MAX_TILE_BYTES, deadline=time.monotonic() - 1.0)
    assert breaker.state == "open"  # abandoned: back to open, not stuck half-open
    clock.now += 30.0
    with cuzk_wmts._Client() as client:
        reply = client.get(url, limit=cuzk_wmts.MAX_TILE_BYTES)  # the next probe is admitted
    cuzk_wmts._settle(url, reply)
    assert breaker.state == "closed"


def test_the_deadline_is_checked_between_retries(wmts, monkeypatch):
    path = tile_path(L0_MATRIX, 0, 0)
    wmts.status[path] = 503
    monkeypatch.setattr(cuzk_wmts, "_sleep", lambda _seconds: time.sleep(0.3))
    with cuzk_wmts._Client() as client:
        with pytest.raises(cuzk_wmts._Deadline):
            client.get(wmts.base + path, limit=cuzk_wmts.MAX_TILE_BYTES, deadline=time.monotonic() + 0.2)
    assert len(wmts.requests(path=path)) == 1  # the second attempt was not sent: the deadline had passed


def test_a_newer_sheet_edition_keeps_the_object_id(tmp_path):
    first = tmp_path / "a" / "TESTA1.WRTO24.2025.zip"
    second = tmp_path / "b" / "TESTA1.WRTO24.2026.zip"
    for path in (first, second):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_sheet_zip("TESTA1", SHEET_BOXES["TESTA1"]))
    original = run_import([first], SourceKind.IMAGERY, SETTINGS, KROVAK, {}, format_id="cuzk-ortho-sheet")
    newer = run_import(
        [second],
        SourceKind.IMAGERY,
        SETTINGS,
        KROVAK,
        {},
        format_id="cuzk-ortho-sheet",
        previous=original.dataset,
    )
    assert [o.key for o in newer.dataset.objects] == ["TESTA1"]  # keyed by the code, not the edition
    assert newer.dataset.objects[0].id == original.dataset.objects[0].id  # the GUID is kept


async def test_complete_is_false_at_the_wire_when_the_breaker_is_open_or_the_deadline_passed(
    server_url, wmts, tmp_path, monkeypatch
):
    terrain = _terrain_tif(tmp_path / "terrain.tif")
    async with connect(server_url) as ws:
        assert (
            await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        ).type == "res"
        assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})).type == "res"
        await _call(
            ws,
            "t",
            "import.run",
            {"path": str(terrain), "kind": "terrain", "settings": {"horizontal_crs": "EPSG:5514"}},
        )
        imported = await _call(
            ws,
            "i",
            "import.run",
            {
                "provider": "cuzk-ortho-wmts",
                "query": _bbox(CLIP),
                "kind": "imagery",
                "settings": {"horizontal_crs": "EPSG:5514"},
            },
        )
        assert imported.type == "res", imported.error
        listing = await _call(ws, "l", "imagery.tiles", {"level": 0})
        params = {"level": 0, "ix": TILE_IX, "iy": TILE_IY, "revision": listing.result["revision"]}
        for row, col in _wmts_tiles():
            wmts.status[tile_path(L0_MATRIX, row, col)] = 503
        tripping = await _call(ws, "c1", "imagery.tile", params)
        assert tripping.result["complete"] is False
        open_breaker = await _call(ws, "c2", "imagery.tile", params)
        assert (
            open_breaker.result["complete"] is False
        )  # the breaker is open: nothing was requested, and it says so
        wmts.status.clear()
        monkeypatch.setattr(cuzk_wmts, "_BREAKERS", {})  # a closed breaker again, with nothing cached
        wmts.delay = 1.0
        monkeypatch.setattr(cuzk_wmts, "RENDER_DEADLINE_S", 0.2)
        late = await _call(ws, "c3", "imagery.tile", params)
        assert late.result["complete"] is False  # the deadline passed before the answers came


# --- live (opt-in) -----------------------------------------------------------------------------------------


@pytest.mark.network
def test_live_kralupy_l0_streams_from_the_wmts(tmp_path, monkeypatch):
    """Streams the L0 orthophoto of a 1 km box at the centre of sheet KRAV82 from the ČÚZK service, cold and
    then warm, and reads the ORTOFOTO sheet total for that box. No sheet is downloaded."""
    import requests

    try:
        index = cuzk_atom.load_sheet_index(CuzkDataset.ORTOFOTO, tmp_path / "atom")
    except (OSError, CuzkError) as exc:
        pytest.skip(f"no network: {exc}")
    krav = next(s for s in index if s.code == "KRAV82")
    lon = np.array([p[0] for p in krav.outline_lonlat])
    lat = np.array([p[1] for p in krav.outline_lonlat])
    east, north = KROVAK.from_crs("EPSG:4326", lon, lat)
    centre_e = (float(east.min()) + float(east.max())) / 2
    centre_n = (float(north.min()) + float(north.max())) / 2
    box = (centre_e - 500.0, centre_n - 500.0, centre_e + 500.0, centre_n + 500.0)
    print(f"sheet {krav.stem}: E {east.min():.0f}..{east.max():.0f}, N {north.min():.0f}..{north.max():.0f}")

    urls: list[str] = []
    original = requests.Session.request

    def counting(self, method, url, **kwargs):
        urls.append(url)
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", counting)
    started = time.perf_counter()
    outcome = _import_wmts(box)
    print(f"import (capabilities and descriptor only): {time.perf_counter() - started:.2f} s")
    (source,) = outcome.payloads.values()
    l0 = choose_matrix(source.matrices, 512.0 / IMAGE_SIZE)
    assert l0.identifier == L0_MATRIX and l0.pixel_m == pytest.approx(1.00013, rel=1e-4)
    layer = layer_for_payload("wmts-live", source)
    tiles = [
        (ix, iy)
        for ix in range(int(box[0] // 512), int(box[2] // 512) + 1)
        for iy in range(int(box[1] // 512), int(box[3] // 512) + 1)
    ]
    flat = TerrainStack(())
    started = time.perf_counter()
    jpeg_sizes = [len(compose_tile([layer], flat, None, 0, ix, iy).jpeg) for ix, iy in tiles]
    cold = time.perf_counter() - started
    tile_urls = [u for u in urls if "/tile/" in u]
    root = provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID)
    stored = [p for p in root.rglob("*") if p.suffix in (".jpg", ".png")]
    stored_bytes = sum(p.stat().st_size for p in stored)
    before_warm = len(urls)
    started = time.perf_counter()
    for ix, iy in tiles:
        compose_tile([layer], flat, None, 0, ix, iy)
    warm = time.perf_counter() - started
    print(
        f"L0 tiles {len(tiles)}; WMTS tile requests {len(tile_urls)}; stored {len(stored)} files, "
        f"{stored_bytes} bytes; cold {cold:.2f} s; warm {warm:.3f} s ({len(urls) - before_warm} requests); "
        f"composed L0 JPEG sizes {min(jpeg_sizes)}..{max(jpeg_sizes)} bytes"
    )
    assert stored and len(urls) == before_warm
    cols, rows = covering_tiles(l0, tile_bounds(0, tiles[0][0], tiles[0][1]))
    assert len(cols) * len(rows) == 9  # a 512 m level-0 tile is 3 by 3 TileMatrix 11 tiles of 256.03 m
    # placement: the level-0 tile at the sheet's centre is a real image, at its computed row and column
    centre_tile = (math.floor(centre_e / 512.0), math.floor(centre_n / 512.0))
    rgb_c, valid_c = layer.render(tile_bounds(0, *centre_tile), IMAGE_SIZE)
    row = math.floor((l0.top_left_n - centre_n) / l0.tile_edge_n)
    col = math.floor((centre_e - l0.top_left_e) / l0.tile_edge_e)
    stored_tile = cuzk_wmts._stored(provider_cache.provider_cache_dir(cuzk_wmts.PROVIDER_ID), l0, row, col)
    assert stored_tile is not None, (row, col)  # the WMTS tile at that row and column was fetched
    tile_pixels = np.asarray(Image.open(stored_tile).convert("RGB"))
    assert tile_pixels.shape == (256, 256, 3) and tile_pixels.std() > 5  # not blank, not a 404 page
    assert valid_c.mean() > 0.99 and rgb_c.std() > 5
    assert layer.warning_messages() == ()
    print(f"placement: TileMatrix {l0.identifier} row {row} col {col}, tile std {tile_pixels.std():.1f}")

    # one small ranged GET of the ORTOFOTO ZIP's tail: its central directory names the members
    (sheet_file,) = cuzk_atom.resolve_files([krav], tmp_path / "atom")
    cuzk_atom._check_url(sheet_file.url, "sheet file")
    before_zip = len(urls)
    response = cuzk_atom._request(
        requests.Session(),
        "GET",
        sheet_file.url,
        headers={"Range": "bytes=-65536"},
        stream=True,
        ok=(206,),
    )
    try:
        tail = response.content
    finally:
        response.close()
    with zipfile.ZipFile(io.BytesIO(tail)) as archive:
        members = archive.namelist()
    assert any(name.lower().endswith(".jpg") for name in members), members
    cached = tmp_path / "atom" / sheet_file.sheet.dataset.value / f"{sheet_file.sheet.stem}.zip"
    assert (
        cuzk_bulk.sheet_code(cached) == krav.code
    )  # the object key the sheet reader gives is the sheet code
    assert [u for u in urls[before_zip:] if u.endswith(".zip")] == [sheet_file.url]  # one ranged request only
    print(f"ZIP tail: {len(tail)} bytes, members {members}")

    sheets = inspect_provider("cuzk-ortho-sheets", _bbox(box), KROVAK, {})[1]
    print(
        f"ORTOFOTO sheets for the box: {sheets.counts['sheets']}, {sheets.counts['bytes'] / 1e6:.2f} MB total"
    )
    assert sheets.counts["sheets"] >= 1
    assert [u for u in urls if u.endswith(".zip")] == [sheet_file.url]  # no sheet was downloaded whole
