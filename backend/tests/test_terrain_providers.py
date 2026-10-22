"""T-133: terrain providers (ČÚZK DMR 5G / DMR 4G, Copernicus GLO-30), the provider cache, source precedence.

Everything runs against local HTTP servers and synthetic data: LAZ sheets and a synthetic cloud-optimised
GeoTIFF. The tests marked `network` touch the live ČÚZK and Copernicus hosts and run only with `-m network`.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import threading
import time
import tracemalloc
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import pytest
import rasterio
import shapely
from rasterio.transform import from_origin
from websockets.asyncio.client import connect

from coypu_builder import config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import (
    BBoxClip,
    CorridorClip,
    ImportSettings,
    ProviderOrigin,
    SourceKind,
)
from coypu_builder.domain.terrain import TerrainGrid, TerrainPoints
from coypu_builder.domain.terrain_surface import (
    NO_SOURCE,
    GridSurface,
    StackSource,
    TerrainStack,
    order_sources,
    payload_resolution_m,
    resolution_band,
    source_resolution_m,
    stack_from_sources,
    tin_from_points,
)
from coypu_builder.io.gis import cuzk_atom
from coypu_builder.io.gis.cuzk_atom import CuzkDataset
from coypu_builder.io.mesh.terrain_tiles import NO_SOURCE_BYTE, SAMPLES, bake_tile, lattice
from coypu_builder.io.providers import (
    cache as provider_cache,
)
from coypu_builder.io.providers import (
    canonical_query,
    cuzk_terrain,
    register_builtin_providers,
)
from coypu_builder.io.providers import copernicus as cop
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.runner import (
    NoAlignments,
    inspect_provider,
    pin_provider_source,
    run_provider_import,
)
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import METHODS, ErrorCode
from coypu_builder.server import handlers, serve
from coypu_builder.server.codec import unpack_blob
from coypu_builder.server.session import ProjectState, SourceEntry
from fixtures.synthetic.terrain import sample_irregular, surface
from test_cuzk_atom import (
    Handler as AtomHandler,
)
from test_cuzk_atom import (
    Route,
    _dataset_feed,
    _entry_xml,
    _service_feed,
)
from test_cuzk_atom import (
    State as AtomState,
)
from test_terrain_tiles import _call, _collect, _open, _ping_while, _send

KROVAK = ProjectCRS("EPSG:5514")
REPO_ROOT = Path(__file__).resolve().parents[2]
SHEET_BOX = (-747100.0, -1025700.0, -745900.0, -1024300.0)
HALF = (SHEET_BOX[0] + SHEET_BOX[2]) / 2.0
SHEET_BOXES = {
    "TESTA1": (SHEET_BOX[0], SHEET_BOX[1], HALF, SHEET_BOX[3]),
    "TESTB1": (HALF, SHEET_BOX[1], SHEET_BOX[2], SHEET_BOX[3]),
}
CLIP_BOX = (-747000.0, -1025600.0, -746000.0, -1024400.0)  # across both sheets
BBOX_QUERY = {
    "clip": {"type": "bbox", **dict(zip(("min_e", "min_n", "max_e", "max_n"), CLIP_BOX, strict=True))}
}
SETTINGS_5514 = ImportSettings(horizontal_crs="EPSG:5514", vertical_crs="EPSG:5705")


# --- registries and the cache ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Fresh registries with the built-in readers and providers, a private provider cache, and no waiting."""
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "user-config" / "config.toml")
    register_builtin_readers()
    register_builtin_providers()
    monkeypatch.setattr(provider_cache, "providers_cache_root", lambda: tmp_path / "provider-cache")
    monkeypatch.setattr(cuzk_atom, "_sleep", lambda seconds: None)
    monkeypatch.setattr(cop, "_sleep", lambda seconds: None)


@pytest.fixture
def cache_root(tmp_path) -> Path:
    return tmp_path / "provider-cache"


# --- synthetic ČÚZK service ---------------------------------------------------------------------------------


def _write_laz(path: Path, points: np.ndarray) -> Path:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.classification = np.full(len(points), 8, dtype=np.uint8)
    las.write(path)
    return path


def _sheet_points(code: str, seed: int, bump: float = 0.0) -> np.ndarray:
    box = SHEET_BOXES[code]
    cloud = sample_irregular(box, 8.0, seed=seed)
    cloud[:, 2] += bump
    return cloud


def _laz_zip(tmp: Path, code: str, seed: int = 1, bump: float = 0.0) -> bytes:
    """A ČÚZK-style sheet: a ZIP holding `<code>.laz`."""
    laz = _write_laz(tmp / f"{code}.laz", _sheet_points(code, seed, bump))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.write(laz, arcname=f"{code}.laz")
    laz.unlink()
    return buffer.getvalue()


def _tif_zip(tmp: Path, code: str, box, cell: float = 5.0, bump: float = 0.0) -> bytes:
    """A DMR 4G style sheet: a ZIP holding `<code>.tif`, a `cell` metre grid in EPSG:5514."""
    min_e, min_n, max_e, max_n = box
    cols, rows = int((max_e - min_e) / cell), int((max_n - min_n) / cell)
    e = min_e + (np.arange(cols) + 0.5) * cell
    n = max_n - (np.arange(rows) + 0.5) * cell
    ee, nn = np.meshgrid(e, n)
    path = tmp / f"{code}.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=cols,
        height=rows,
        count=1,
        dtype="float32",
        crs="EPSG:5514",
        transform=from_origin(min_e, max_n, cell, cell),
        nodata=-9999.0,
    ) as dst:
        dst.write((surface(ee, nn) + bump).astype("float32"), 1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.write(path, arcname=f"{code}.tif")
    path.unlink()
    return buffer.getvalue()


def _outline(box) -> str:
    """georss:polygon text (latitude first) of an S-JTSK box."""
    min_e, min_n, max_e, max_n = box
    e = np.array([min_e, max_e, max_e, min_e, min_e])
    n = np.array([min_n, min_n, max_n, max_n, min_n])
    lon, lat = KROVAK.to_wgs84(e, n)
    return " ".join(f"{float(la)!r} {float(lo)!r}" for lo, la in zip(lon, lat, strict=True))


def _publish(state: AtomState, bodies: dict[str, bytes], dataset: CuzkDataset = CuzkDataset.DMR5G) -> None:
    entries = []
    for code, body in bodies.items():
        polygon = _outline(SHEET_BOXES[code])
        feed_path, file_path = f"/{dataset.value}/feeds/{code}.xml", f"/{dataset.value}/files/{code}.zip"
        entries.append(
            _entry_xml(
                f"CZ-00025712-CUZK_{dataset.value}_{code}",
                f"Synthetic - mapový list: Sheet {code}",
                state.base + feed_path,
                polygon,
                "2026-01-19T13:40:46+02:00",
            )
        )
        state.routes[feed_path] = Route(
            _dataset_feed(state.base + file_path, len(body), polygon),
            etag=f'"feed-{code}-{hashlib.md5(body).hexdigest()[:8]}"',
        )
        state.routes[file_path] = Route(body, etag=f'"{code}-{hashlib.md5(body).hexdigest()[:8]}"')
    state.routes[f"/{dataset.value}.xml"] = Route(_service_feed(entries), etag=f'"service-{dataset.value}"')


def _file_gets(state: AtomState) -> list:
    return [r for r in state.requests("GET") if "/files/" in r.path]


@pytest.fixture
def atom():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AtomHandler)
    httpd.state = AtomState()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    httpd.state.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield httpd.state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def cuzk(atom, monkeypatch, tmp_path):
    """A local ČÚZK DMR 5G service of two sheets. The allow-list stays on; it only gains the loopback host."""
    monkeypatch.setattr(cuzk_atom, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_atom, "ALLOWED_HOSTS", ("127.0.0.1", *cuzk_atom.ALLOWED_HOSTS))
    monkeypatch.setitem(cuzk_atom.SERVICE_FEEDS, CuzkDataset.DMR5G, atom.base + "/DMR5G.xml")
    monkeypatch.setitem(cuzk_atom.SERVICE_FEEDS, CuzkDataset.DMR4G, atom.base + "/DMR4G-TIFF.xml")
    work = tmp_path / "work"
    work.mkdir()
    atom.work = work
    _publish(atom, {code: _laz_zip(work, code, seed=number) for number, code in enumerate(SHEET_BOXES, 1)})
    return atom


def _import_cuzk(query=None, *, previous=None, provider="cuzk-dmr5g", settings=SETTINGS_5514):
    return run_provider_import(
        provider,
        BBOX_QUERY if query is None else query,
        SourceKind.TERRAIN,
        settings,
        KROVAK,
        {},
        previous=previous,
    )


# --- ČÚZK provider --------------------------------------------------------------------------------


def test_the_builtin_providers_are_registered_with_their_licences():
    found = {p.provider_id: p for p in registry.providers()}
    assert set(found) == {"cuzk-dmr5g", "cuzk-dmr4g", "copernicus-glo30"}
    assert found["cuzk-dmr5g"].licence == found["cuzk-dmr4g"].licence == "CC BY 4.0"
    assert found["cuzk-dmr5g"].attribution == "© ČÚZK"
    assert all(SourceKind.TERRAIN in p.kinds for p in found.values())
    register_builtin_providers()  # idempotent
    assert len(registry.providers()) == 3


def test_a_provider_import_is_one_source_with_one_object_per_sheet_and_full_provenance(cuzk):
    outcome = _import_cuzk()
    dataset = outcome.dataset
    assert [o.key for o in dataset.objects] == ["TESTA1", "TESTB1"]
    assert all(isinstance(p, TerrainPoints) and len(p.points) > 100 for p in outcome.payloads.values())
    assert dataset.format_id == "las" and dataset.kind == SourceKind.TERRAIN
    origin = dataset.origin
    assert isinstance(origin, ProviderOrigin) and origin.provider_id == "cuzk-dmr5g"
    zips = [cuzk.routes[f"/DMR5G/files/{code}.zip"].body for code in SHEET_BOXES]
    assert origin.sha256 == tuple(hashlib.sha256(z).hexdigest() for z in zips)
    query = json.loads(origin.query)
    assert query["max_total_mb"] == 500.0 and query["clip"]["type"] == "bbox"
    assert origin.query == canonical_query(query)  # canonical: sorted keys, no spaces
    assert dataset.attribution == "© ČÚZK" and dataset.licence == "CC BY 4.0"
    assert dataset.name == "ČÚZK DMR 5G"
    assert dataset.settings.horizontal_crs == "EPSG:5514" and dataset.settings.point_classes is None
    assert dataset.settings.clip == BBoxClip(*CLIP_BOX)  # the query's clip, as settings.clip was left out
    assert outcome.resolution_m is not None and 2.0 < outcome.resolution_m < 8.0
    inside = np.vstack([p.points for p in outcome.payloads.values()])
    assert inside[:, 0].min() >= CLIP_BOX[0] and inside[:, 0].max() <= CLIP_BOX[2]
    # the downloads are in the provider cache, and nowhere else
    cache_dir = provider_cache.provider_cache_dir("cuzk-dmr5g")
    assert (cache_dir / "DMR5G" / "TESTA1.zip").is_file()
    assert not any(REPO_ROOT.glob("**/TESTA1.zip"))


def test_a_clip_in_the_settings_wins_over_the_query_clip(cuzk):
    narrow = BBoxClip(-746900.0, -1025500.0, -746700.0, -1025300.0)
    outcome = _import_cuzk(settings=ImportSettings(horizontal_crs="EPSG:5514", clip=narrow))
    assert outcome.dataset.settings.clip == narrow
    points = np.vstack([p.points for p in outcome.payloads.values()])
    assert points[:, 0].max() <= -746700.0 and points[:, 1].min() >= -1025500.0


def test_reimport_after_one_etag_changes_reports_one_changed_and_keeps_the_guids(cuzk):
    first = _import_cuzk()
    ids = {o.key: o.id for o in first.dataset.objects}
    cuzk.routes.pop("/DMR5G.xml")
    new_a = _laz_zip(cuzk.work, "TESTA1", seed=77, bump=5.0)
    _publish(cuzk, {"TESTA1": new_a, "TESTB1": cuzk.routes["/DMR5G/files/TESTB1.zip"].body})
    second = _import_cuzk(previous=first.dataset)
    recon = second.reconciliation
    assert (recon.changed, recon.unchanged, recon.added, recon.removed) == (("TESTA1",), ("TESTB1",), (), ())
    assert second.dataset.id == first.dataset.id
    assert {o.key: o.id for o in second.dataset.objects} == ids
    hashes = {o.key: o.content_hash for o in second.dataset.objects}
    old_hashes = {o.key: o.content_hash for o in first.dataset.objects}
    assert hashes["TESTA1"] != old_hashes["TESTA1"] and hashes["TESTB1"] == old_hashes["TESTB1"]
    assert second.dataset.origin.sha256[0] == hashlib.sha256(new_a).hexdigest()


def test_a_second_identical_import_downloads_nothing(cuzk):
    first = _import_cuzk()
    gets = len(_file_gets(cuzk))
    assert gets == 2
    second = _import_cuzk()
    assert len(_file_gets(cuzk)) == gets  # only HEAD probes and conditional feed requests
    assert second.dataset.origin.sha256 == first.dataset.origin.sha256
    entries = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g")).entries()
    assert set(entries) == {"DMR5G/TESTA1.zip", "DMR5G/TESTB1.zip"}
    a = entries["DMR5G/TESTA1.zip"]
    assert a.size == len(cuzk.routes["/DMR5G/files/TESTA1.zip"].body) and a.url.endswith("/files/TESTA1.zip")
    assert a.sha256 == hashlib.sha256(cuzk.routes["/DMR5G/files/TESTA1.zip"].body).hexdigest()


def test_inspect_reports_the_selection_its_size_and_the_settings_it_proposes(cuzk):
    provider, report = inspect_provider("cuzk-dmr5g", BBOX_QUERY, KROVAK, {})
    assert provider.provider_id == "cuzk-dmr5g"
    assert report.counts["sheets"] == 2
    assert report.counts["bytes"] == sum(len(cuzk.routes[f"/DMR5G/files/{c}.zip"].body) for c in SHEET_BOXES)
    assert report.declared_crs == "EPSG:5514" and report.licence_hint == "CC BY 4.0"
    s = report.suggested
    assert (s.horizontal_crs, s.vertical_crs, s.point_classes) == ("EPSG:5514", "EPSG:5705", None)
    assert s.clip == BBoxClip(*CLIP_BOX)
    assert any("class 8" in w for w in report.warnings)
    assert report.extent is not None and report.extent[0] < CLIP_BOX[0] and report.extent[2] > CLIP_BOX[2]
    assert not _file_gets(cuzk)  # inspect reads feeds only
    _, small = inspect_provider("cuzk-dmr5g", {**BBOX_QUERY, "max_total_mb": 0.001}, KROVAK, {})
    assert any("over max_total_mb" in w for w in small.warnings)
    far = {"clip": {"type": "bbox", "min_e": 0.0, "min_n": 0.0, "max_e": 100.0, "max_n": 100.0}}
    _, none = inspect_provider("cuzk-dmr5g", far, KROVAK, {})
    assert none.counts["sheets"] == 0 and any("no sheet" in w for w in none.warnings)


def test_the_size_limit_refuses_before_any_download(cuzk):
    with pytest.raises(ValueError, match="limit"):
        _import_cuzk({**BBOX_QUERY, "max_total_mb": 0.001})
    assert not _file_gets(cuzk)


def _corrupt(path: Path) -> None:
    """Flip every 7th byte: the same size, different content."""
    data = bytearray(path.read_bytes())
    for index in range(0, len(data), 7):
        data[index] ^= 0xFF
    path.write_bytes(bytes(data))


def test_a_corrupted_cached_sheet_is_fetched_again(cuzk):
    first = _import_cuzk()
    zip_path = provider_cache.provider_cache_dir("cuzk-dmr5g") / "DMR5G" / "TESTA1.zip"
    _corrupt(zip_path)
    before = len([r for r in _file_gets(cuzk) if r.path.endswith("/TESTA1.zip")])
    second = _import_cuzk()
    after = len([r for r in _file_gets(cuzk) if r.path.endswith("/TESTA1.zip")])
    assert after - before == 1  # the damaged ZIP was fetched once more, and only that one
    assert second.dataset.origin.sha256 == first.dataset.origin.sha256
    assert [o.key for o in second.dataset.objects] == ["TESTA1", "TESTB1"]


def test_a_clip_selecting_too_many_sheets_is_refused_before_any_sheet_is_looked_up(cuzk, monkeypatch):
    monkeypatch.setattr(cuzk_terrain, "MAX_SHEETS", 1)
    everything = {
        "clip": {
            "type": "bbox",
            "min_e": -760000.0,
            "min_n": -1040000.0,
            "max_e": -730000.0,
            "max_n": -1010000.0,
        }
    }
    with pytest.raises(ValueError, match="selects 2 ČÚZK sheets"):
        _import_cuzk(everything)
    assert not [r for r in cuzk.requests("GET") if "/feeds/" in r.path]  # no per-sheet request


def test_a_clip_that_touches_no_sheet_is_refused(cuzk):
    far = {"clip": {"type": "bbox", "min_e": 0.0, "min_n": 0.0, "max_e": 100.0, "max_n": 100.0}}
    with pytest.raises(ValueError, match="no ČÚZK sheet"):
        _import_cuzk(far)


def test_dmr4g_is_read_by_the_geotiff_reader_with_the_sheet_code_as_object_key(cuzk):
    bodies = {code: _tif_zip(cuzk.work, code, box) for code, box in SHEET_BOXES.items()}
    _publish(cuzk, bodies, CuzkDataset.DMR4G)
    outcome = _import_cuzk(provider="cuzk-dmr4g")
    assert outcome.dataset.format_id == "geotiff" and outcome.dataset.origin.provider_id == "cuzk-dmr4g"
    assert [o.key for o in outcome.dataset.objects] == ["TESTA1", "TESTB1"]
    assert all(isinstance(p, TerrainGrid) and p.cell_e == 5.0 for p in outcome.payloads.values())
    assert outcome.resolution_m == pytest.approx(5.0)
    assert outcome.dataset.attribution == "© ČÚZK"


def test_query_defaults_are_written_out_and_bad_queries_are_refused():
    ctuzk = {p.provider_id: p for p in registry.providers()}
    cuzk_q = ctuzk["cuzk-dmr5g"].normalise_query({})
    assert cuzk_q == {"clip": {"type": "corridor", "buffer_m": 250.0}, "max_total_mb": 500.0}
    assert ctuzk["copernicus-glo30"].normalise_query({}) == {"clip": {"type": "corridor", "buffer_m": 2000.0}}
    again = ctuzk["cuzk-dmr5g"].normalise_query(cuzk_q)
    assert again == cuzk_q  # normalising is idempotent
    for bad in (
        {"unknown": 1},
        {"clip": "everything"},
        {"clip": {"type": "circle"}},
        {"clip": {"type": "bbox", "min_e": 0}},
        {"clip": {"type": "corridor", "buffer_m": "wide"}},
        {"clip": {"type": "corridor", "buffer_m": 0.0}},
        {"clip": {"type": "corridor", "alignment_ids": 5}},
        {"clip": {"type": "corridor", "alignment_ids": [5]}},
        {"max_total_mb": 0},
        {"max_total_mb": "lots"},
    ):
        with pytest.raises(ValueError):
            ctuzk["cuzk-dmr5g"].normalise_query(bad)


def test_a_corridor_clip_needs_alignments_and_an_unknown_provider_or_kind_is_refused(cuzk):
    with pytest.raises(NoAlignments):
        _import_cuzk({})  # the default clip is a corridor round the (absent) alignments
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        _import_cuzk(provider="nope")
    with pytest.raises(ValueError, match="does not supply imagery"):
        run_provider_import("cuzk-dmr5g", BBOX_QUERY, SourceKind.IMAGERY, SETTINGS_5514, KROVAK, {})
    with pytest.raises(ValueError, match="only be used while"):
        registry.providers()[0].inspect({})


def test_the_default_corridor_selects_the_sheets_along_the_alignment_at_250_m(cuzk, kralupy):
    """The synthetic sheets lie over Kralupy stations 12.7 to 17.5 km, so the default clip finds them."""
    alignments = {"a": kralupy.alignment}
    _, report = inspect_provider("cuzk-dmr5g", {}, KROVAK, alignments)
    assert isinstance(report.suggested.clip, CorridorClip) and report.suggested.clip.buffer_m == 250.0
    assert 1 <= report.counts["sheets"] <= 2
    outcome = run_provider_import(
        "cuzk-dmr5g", {}, SourceKind.TERRAIN, SETTINGS_5514, KROVAK, alignments, name="along the line"
    )
    assert outcome.dataset.name == "along the line"
    assert json.loads(outcome.dataset.origin.query)["clip"] == {"type": "corridor", "buffer_m": 250.0}
    assert isinstance(outcome.dataset.settings.clip, CorridorClip)


# --- the cache ------------------------------------------------------------------------------------


def _filled(tmp_path: Path, sizes: dict[str, int], cap: int):
    cache = provider_cache.ProviderCache(tmp_path / "c", cap_bytes=cap)
    for name, size in sizes.items():
        target = cache.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        cache.record(name, url=f"https://example.invalid/{name}")
        time.sleep(0.002)  # distinct last_used stamps, in the order the files are named
    return cache


def test_eviction_takes_the_least_recently_used_first_and_stops_at_the_cap(tmp_path):
    cache = _filled(tmp_path, {"a": 100, "b": 100, "c": 100, "d": 100}, cap=250)
    assert cache.total_bytes() == 400
    assert cache.evict() == ["a", "b"]
    assert set(cache.entries()) == {"c", "d"} and cache.total_bytes() <= 250
    assert not cache.path("a").exists()
    cache.touch(["c"])  # c is now the most recently used
    cache.cap_bytes = 100
    assert cache.evict() == ["d"]
    assert set(cache.entries()) == {"c"}


def test_pinned_and_protected_files_are_never_evicted_even_over_the_cap(tmp_path):
    cache = _filled(tmp_path, {"a": 100, "b": 100, "c": 100, "d": 100}, cap=150)
    cache.pin("source-1", ["a"])
    evicted = cache.evict(protect=["b"])
    assert evicted == ["c", "d"]
    assert set(cache.entries()) == {"a", "b"}  # 200 bytes, over the 150 cap, because both are kept
    cache.unpin("source-1")
    assert cache.evict(protect=["b"]) == ["a"]
    assert cache.pinned() == frozenset()


def test_the_index_lists_files_with_sizes_hashes_urls_and_validators(tmp_path):
    cache = _filled(tmp_path, {"sub/one.bin": 10}, cap=1000)
    entry = cache.record("sub/one.bin", url="https://x.invalid/one", etag='"e"', last_modified="Mon")
    data = json.loads(cache.index_path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["files"]) == 1
    (item,) = data["files"]
    assert item["file"] == "sub/one.bin" and item["size"] == 10
    assert item["sha256"] == hashlib.sha256(b"x" * 10).hexdigest() == entry.sha256
    assert item["url"] == "https://x.invalid/one" and item["etag"] == '"e"' and item["last_modified"] == "Mon"
    assert {"fetched_at", "last_used"} <= set(item)
    cache.path("sub/one.bin").unlink()  # safe to delete by hand: the file just leaves the index
    assert cache.entries() == {}
    cache.index_path.write_text("not json", encoding="utf-8")
    assert cache.entries() == {}  # a damaged index is not an error


def test_verify_is_false_for_a_vanished_file(tmp_path, monkeypatch):
    cache = _filled(tmp_path, {"a.bin": 10}, cap=1000)
    entry = cache.get("a.bin")
    cache.path("a.bin").unlink()
    assert cache.verify("a.bin") is False  # the index no longer lists it, so it is not trusted
    monkeypatch.setattr(cache, "get", lambda file: entry)  # the file vanishes between the index and the read
    assert cache.verify("a.bin") is False


def test_the_cache_refuses_the_repository_and_paths_outside_itself(tmp_path):
    with pytest.raises(ValueError, match="repository"):
        provider_cache.ProviderCache(REPO_ROOT / "backend" / "cache-must-not-exist")
    cache = provider_cache.ProviderCache(tmp_path / "c")
    for bad in ("../x", "/abs", "a/../../b", "C:/x", ""):
        with pytest.raises(ValueError):
            cache.path(bad)
    with pytest.raises(ValueError):
        cache.relative(tmp_path / "elsewhere.bin")
    assert provider_cache.provider_cache_dir("cuzk-dmr5g").name == "cuzk-dmr5g"
    for bad_id in ("", "../x", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            provider_cache.provider_cache_dir(bad_id)


def test_the_default_cache_location_is_per_user_and_outside_the_repository(monkeypatch, tmp_path):
    monkeypatch.undo()  # the real function, not the test's redirect
    root = provider_cache.providers_cache_root()
    assert root.name == "providers" and not root.resolve().is_relative_to(REPO_ROOT)
    assert provider_cache.DEFAULT_CAP_BYTES == 5 * 10**9


def test_a_stored_sources_files_survive_the_cap_and_a_newer_fetch_does_not_evict_what_it_just_got(
    cuzk, monkeypatch
):
    monkeypatch.setattr(provider_cache, "DEFAULT_CAP_BYTES", 1)  # every fetch is over the cap
    only_a = {
        "clip": {
            "type": "bbox",
            "min_e": -747000.0,
            "min_n": -1025600.0,
            "max_e": -746700.0,
            "max_n": -1025000.0,
        }
    }
    only_b = {
        "clip": {
            "type": "bbox",
            "min_e": -746300.0,
            "min_n": -1025600.0,
            "max_e": -746000.0,
            "max_n": -1025000.0,
        }
    }
    first = _import_cuzk(only_a)  # stored: TESTA1 is pinned to this source
    pin_provider_source(first)
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    assert set(cache.entries()) == {"DMR5G/TESTA1.zip"}
    second = _import_cuzk(only_b)  # TESTB1 is protected while it is fetched, TESTA1 is pinned
    pin_provider_source(second)
    assert [o.key for o in second.dataset.objects] == ["TESTB1"]
    assert set(cache.entries()) == {"DMR5G/TESTA1.zip", "DMR5G/TESTB1.zip"}
    cache.unpin(first.dataset.id)  # the first source is no longer stored
    cache.unpin(second.dataset.id)
    again = _import_cuzk(only_b)  # only the fetch in progress is kept now
    assert [o.key for o in again.dataset.objects] == ["TESTB1"]
    assert set(cache.entries()) == {"DMR5G/TESTB1.zip"}
    assert not cache.path("DMR5G/TESTA1.zip").exists()


# --- synthetic Copernicus tiles -------------------------------------------------------------------

COG_SIZE = 1800  # pixels per side of a synthetic 1 degree tile (2 arc seconds)
COG_BLOCK = 128


def cog_height(lon, lat):
    """The synthetic surface: a plane steep enough that half a cell is a measurable height."""
    return 150.0 + 3000.0 * (np.asarray(lon) - 14.0) + 2000.0 * (np.asarray(lat) - 50.0)


def _make_cog(path: Path, lat0: int, lon0: int) -> bytes:
    cell = 1.0 / COG_SIZE
    lon = lon0 + (np.arange(COG_SIZE) + 0.5) * cell
    lat = lat0 + 1.0 - (np.arange(COG_SIZE) + 0.5) * cell
    ll, la = np.meshgrid(lon, lat)
    noise = np.random.default_rng(lat0 * 1000 + lon0).normal(0.0, 0.0005, ll.shape)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=COG_SIZE,
        height=COG_SIZE,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(lon0, lat0 + 1.0, cell, cell),
        tiled=True,
        blockxsize=COG_BLOCK,
        blockysize=COG_BLOCK,
        compress="deflate",
        predictor=3,
    ) as dst:
        dst.write((cog_height(ll, la) + noise).astype("float32"), 1)
    return path.read_bytes()


@pytest.fixture(scope="module")
def cog_bodies(tmp_path_factory) -> dict[str, bytes]:
    folder = tmp_path_factory.mktemp("cogs")
    return {cop.tile_name(50, lon0): _make_cog(folder / f"t{lon0}.tif", 50, lon0) for lon0 in (14, 15)}


@dataclass
class CogState:
    base: str = ""
    files: dict[str, bytes] = field(default_factory=dict)  # URL path -> body
    redirects: dict[str, str] = field(default_factory=dict)
    status: dict[str, int] = field(default_factory=dict)  # URL path -> forced status
    ignore_ranges: bool = False
    oversize_body: int = 0  # answer ranges with a correct Content-Range but this many body bytes
    unbounded_body: int = 0  # answer ranges with no Content-Length: the body runs until the connection closes
    wrong_range_start: bool = False  # answer ranges with a Content-Range that starts one byte late
    log: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def gets(self, path: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.log if r["method"] == "GET" and (path is None or r["path"] == path)]

    def bytes_sent(self) -> int:
        return sum(r["sent"] for r in self.log)


class CogHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        state: CogState = self.server.state
        record = {
            "method": "GET",
            "path": self.path,
            "range": self.headers.get("Range"),
            "accept_encoding": self.headers.get("Accept-Encoding"),
            "sent": 0,
        }
        with state.lock:
            state.log.append(record)
        if self.path in state.redirects:
            self.send_response(302)
            self.send_header("Location", state.redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        forced = state.status.get(self.path)
        body = state.files.get(self.path)
        if forced or body is None:
            self.send_response(forced or 404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        total = len(body)
        wanted = self.headers.get("Range")
        if wanted and not state.ignore_ranges:
            first, _, last = wanted.removeprefix("bytes=").partition("-")
            start, end = int(first), min(int(last) if last else total - 1, total - 1)
            if state.unbounded_body:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                self._stream_zeros(state.unbounded_body, record)
                return
            if state.oversize_body:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Length", str(state.oversize_body))
                self.end_headers()
                self._stream_zeros(state.oversize_body, record)
                return
            chunk = body[start : end + 1]
            self.send_response(206)
            shown = start + 1 if state.wrong_range_start else start
            self.send_header("Content-Range", f"bytes {shown}-{end}/{total}")
        else:
            chunk = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)
        record["sent"] = len(chunk)

    def _stream_zeros(self, count: int, record: dict[str, Any]) -> None:
        """Send `count` zero bytes in small pieces, so the server never holds the whole body."""
        block = bytes(65536)
        try:
            while count > 0:
                piece = block[: min(len(block), count)]
                self.wfile.write(piece)
                count -= len(piece)
                record["sent"] += len(piece)
        except OSError:
            pass  # the client refused the body and closed the connection


@pytest.fixture
def cog(cog_bodies, monkeypatch):
    """A local stand-in for the Copernicus bucket, serving the synthetic tiles with byte ranges. The
    allow-list stays on; it only gains the plain-http loopback host."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CogHandler)
    state = CogState(base=f"http://127.0.0.1:{httpd.server_address[1]}")
    state.files = {f"/{name}/{name}.tif": body for name, body in cog_bodies.items()}
    httpd.state = state
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    monkeypatch.setattr(cop, "BASE_URL", state.base)
    monkeypatch.setattr(cop, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cop, "ALLOWED_HOSTS", ("127.0.0.1",))
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


TILE_14 = cop.tile_name(50, 14)
TILE_15 = cop.tile_name(50, 15)
SETTINGS_4326 = ImportSettings(horizontal_crs="EPSG:4326", vertical_crs="EPSG:3855")
ACROSS_15 = {"clip": {"type": "bbox", "min_e": 14.9985, "min_n": 50.1, "max_e": 15.0015, "max_n": 50.1005}}


def _bbox_query(min_e, min_n, max_e, max_n):
    return {"clip": {"type": "bbox", "min_e": min_e, "min_n": min_n, "max_e": max_e, "max_n": max_n}}


def _import_cop(query=None, *, previous=None, crs=KROVAK, settings=SETTINGS_4326):
    return run_provider_import(
        "copernicus-glo30",
        BBOX_QUERY if query is None else query,
        SourceKind.TERRAIN,
        settings,
        crs,
        {},
        previous=previous,
    )


def test_tile_names_and_urls_follow_the_bucket_layout():
    assert cop.tile_name(50, 14) == "Copernicus_DSM_COG_10_N50_00_E014_00_DEM"
    assert cop.tile_name(-3, -70) == "Copernicus_DSM_COG_10_S03_00_W070_00_DEM"
    assert cop.tile_name(0, 0).startswith("Copernicus_DSM_COG_10_N00_00_E000_00")
    assert cop.tile_url(cop.tile_name(50, 14)) == (
        "https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_N50_00_E014_00_DEM/Copernicus_DSM_COG_10_N50_00_E014_00_DEM.tif"
    )
    assert cop._tiles_for((14.2, 50.2, 14.4, 50.3)) == [(50, 14)]
    assert cop._tiles_for((14.9, 50.2, 15.1, 50.3)) == [(50, 14), (50, 15)]
    assert cop._tiles_for((14.0, 50.0, 15.0, 51.0)) == [(50, 14)]  # touching the next tile is not covering it
    assert len(cop._tiles_for((13.5, 49.5, 14.5, 50.5))) == 4


@pytest.mark.parametrize(
    "url",
    [
        "http://copernicus-dem-30m.s3.amazonaws.com/x.tif",
        "https://evil.example/x.tif",
        "https://copernicus-dem-30m.s3.amazonaws.com.evil.example/x.tif",
        "https://evilcopernicus-dem-30m.s3.amazonaws.com/x.tif",
        "https://copernicus-dem-30m.s3.amazonaws.com@evil.example/x.tif",
        "https://user:pw@copernicus-dem-30m.s3.amazonaws.com/x.tif",
        "https://copernicus-dem-30m.s3.amazonaws.com/a b.tif",
        "https://copernicus-dem-30m.s3.amazonaws.com\\@evil.example/x.tif",
        "https://copernicus-dem-30m.s3.amazonaws.cóm/x.tif",
        "file:///etc/passwd",
    ],
)
def test_the_url_allow_list_refuses_foreign_hosts_plain_http_and_tricks(url):
    with pytest.raises(ValueError, match="refused URL"):
        cop._check_url(url)
    assert cop._check_url("https://copernicus-dem-30m.s3.amazonaws.com/x.tif")


def test_only_the_window_covering_the_clip_is_read_over_range_requests(cog, cog_bodies):
    total = len(cog_bodies[TILE_14])
    outcome = _import_cop()
    reads = cog.gets(f"/{TILE_14}/{TILE_14}.tif")
    assert reads and all(r["range"] and r["range"].startswith("bytes=") for r in reads)  # never a plain GET
    assert len(reads) <= 20 and cog.bytes_sent() < 0.35 * total, (len(reads), cog.bytes_sent(), total)
    print(f"range reads: {len(reads)} requests, {cog.bytes_sent()} of {total} bytes")
    (grid,) = outcome.payloads.values()
    assert isinstance(grid, TerrainGrid)
    assert [o.key for o in outcome.dataset.objects] == [TILE_14]  # the object key is the tile name


def test_the_warped_grid_matches_the_synthetic_surface_within_half_a_cell(cog):
    outcome = _import_cop()
    (grid,) = outcome.payloads.values()
    rng = np.random.default_rng(5)
    e = rng.uniform(CLIP_BOX[0] + 30.0, CLIP_BOX[2] - 30.0, 400)
    n = rng.uniform(CLIP_BOX[1] + 30.0, CLIP_BOX[3] - 30.0, 400)
    lon, lat = KROVAK.to_wgs84(e, n)
    got = GridSurface(grid).heights(e, n)
    want = cog_height(lon, lat)
    assert np.isfinite(got).all()
    # half a cell, in height: the plane rises 3000 m per degree east and 2000 m per degree north
    half_cell_m = 0.5 * max(grid.cell_e, grid.cell_n)
    gradient = np.hypot(3000.0 / 71_500.0, 2000.0 / 111_200.0)  # m of height per m on the ground near 50 N
    tolerance = half_cell_m * gradient
    worst = float(np.abs(got - want).max())
    print(f"worst height error {worst:.4f} m against half a cell = {tolerance:.2f} m")
    assert worst <= tolerance
    assert worst < 0.1 * tolerance  # in practice a small fraction of that


def test_inspect_warns_of_the_surface_model_and_the_vertical_datum_and_reads_nothing(cog):
    _, report = inspect_provider("copernicus-glo30", BBOX_QUERY, KROVAK, {})
    assert report.counts == {"tiles": 1} and report.declared_crs == "EPSG:4326"
    text = " | ".join(report.warnings)
    assert "surface model" in text and "tree canopy" in text
    assert "EGM2008" in text and "EPSG:3855" in text and "not transformed" in text
    s = report.suggested
    assert (s.horizontal_crs, s.vertical_crs) == ("EPSG:4326", "EPSG:3855") and s.clip == BBoxClip(*CLIP_BOX)
    assert report.licence_hint.startswith("Copernicus WorldDEM-30 licence")
    assert cog.log == []  # inspect needs no network


def test_the_source_records_origin_licence_and_the_attribution_notice(cog):
    outcome = _import_cop()
    d = outcome.dataset
    assert d.origin.provider_id == "copernicus-glo30" and d.format_id == "geotiff"
    assert json.loads(d.origin.query) == {"clip": BBOX_QUERY["clip"]}
    assert len(d.origin.sha256) == 1 and len(d.origin.sha256[0]) == 64
    assert d.licence == cop.LICENCE and d.attribution == cop.ATTRIBUTION
    assert d.attribution.startswith("produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and ")
    assert d.attribution.endswith(
        "provided under COPERNICUS by the European Union and ESA; all rights reserved"
    )
    assert d.settings.vertical_crs == "EPSG:3855" and d.effective_crs == "EPSG:4326"


def test_a_second_import_of_the_same_clip_is_served_from_the_cache(cog):
    first = _import_cop()
    requests_before = len(cog.log)
    assert requests_before > 0
    second = _import_cop(previous=first.dataset)
    assert len(cog.log) == requests_before  # not one request
    recon = second.reconciliation
    assert (recon.changed, recon.unchanged, recon.added, recon.removed) == ((), (TILE_14,), (), ())
    assert [o.id for o in second.dataset.objects] == [o.id for o in first.dataset.objects]
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("copernicus-glo30"))
    (name,) = cache.entries()
    assert name.startswith("windows/") and name.endswith(f"/{TILE_14}.tif")
    _import_cop(_bbox_query(-747000.0, -1025600.0, -746500.0, -1025000.0))  # another window of the same tile
    assert len(cache.entries()) == 2 and len(cog.log) > requests_before


def test_a_clip_across_two_tiles_gives_one_object_per_tile_named_after_it(cog):
    outcome = _import_cop(ACROSS_15, crs=ProjectCRS("EPSG:4326"))  # a project in degrees: the box is easy
    assert [o.key for o in outcome.dataset.objects] == [TILE_14, TILE_15]
    assert cog.gets(f"/{TILE_14}/{TILE_14}.tif") and cog.gets(f"/{TILE_15}/{TILE_15}.tif")
    west, east = outcome.payloads.values()
    assert west.bounds[2] <= 15.0 + 1e-9 and east.bounds[0] >= 15.0 - 1e-9


def test_a_missing_tile_is_skipped_and_no_tile_at_all_is_refused(cog):
    crs = ProjectCRS("EPSG:4326")
    del cog.files[f"/{TILE_15}/{TILE_15}.tif"]
    outcome = _import_cop(ACROSS_15, crs=crs)
    assert [o.key for o in outcome.dataset.objects] == [TILE_14]
    skipped = [w for w in outcome.warnings if TILE_15 in w]
    assert skipped and "no such tile (HTTP 404)" in skipped[0] and "skipped" in skipped[0]
    cog.files.clear()
    with pytest.raises(ValueError, match="no Copernicus DEM tile"):
        _import_cop(_bbox_query(14.1, 50.1, 14.11, 50.11), crs=crs)


def test_a_plain_http_base_url_is_refused_before_any_request(cog, monkeypatch):
    monkeypatch.setattr(cop, "ALLOWED_SCHEMES", ("https",))
    monkeypatch.setattr(cop, "ALLOWED_HOSTS", ("copernicus-dem-30m.s3.amazonaws.com",))
    with pytest.raises(ValueError, match="refused URL"):
        _import_cop()
    assert cog.log == []


def test_a_foreign_host_is_refused_before_any_request(cog, monkeypatch):
    monkeypatch.setattr(cop, "BASE_URL", "http://localhost:9")  # a different host name than the allowed one
    with pytest.raises(ValueError, match="refused URL"):
        _import_cop()
    assert cog.log == []


def test_a_redirect_to_a_foreign_host_is_refused_and_never_requested(cog, monkeypatch):
    import requests

    sent: list[str] = []
    original = requests.Session.request

    def spy(self, method, url, **kwargs):
        sent.append(url)
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    cog.redirects[f"/{TILE_14}/{TILE_14}.tif"] = "http://evil.example/steal.tif"
    with pytest.raises(ValueError, match="refused URL"):
        _import_cop()
    assert sent and all(u.startswith(cog.base) for u in sent), sent  # evil.example was never contacted


def test_a_server_without_range_support_fails_instead_of_downloading_the_whole_tile(cog):
    cog.ignore_ranges = True
    with pytest.raises(OSError, match="range request"):
        _import_cop()
    assert len(cog.gets()) == 1


def test_transient_failures_are_retried_and_a_dead_service_is_an_os_error(cog):
    cog.status[f"/{TILE_14}/{TILE_14}.tif"] = 503
    with pytest.raises(OSError, match="giving up after 3 attempts"):
        _import_cop()
    assert len(cog.gets()) == 3


def _corrupt_bytes(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for index in range(0, len(data), 7):
        data[index] ^= 0xFF
    path.write_bytes(bytes(data))


def test_an_oversized_range_body_is_refused_without_reading_it(cog):
    cog.oversize_body = 60_000_000
    tracemalloc.start()
    try:
        with pytest.raises(OSError, match="Content-Length"):
            _import_cop()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak


def test_a_body_without_content_length_is_cut_off_at_the_requested_length(cog):
    cog.unbounded_body = 60_000_000
    tracemalloc.start()
    try:
        with pytest.raises(OSError, match="longer than"):
            _import_cop()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, peak
    assert cog.bytes_sent() < cop.MAX_RESPONSE_BYTES


def test_every_request_asks_for_an_identity_encoding(cog):
    _import_cop()
    assert cog.gets() and all(r["accept_encoding"] == "identity" for r in cog.gets())


def test_a_range_over_the_response_cap_is_refused_before_any_request(cog):
    http = cop._Http()
    try:
        with pytest.raises(OSError, match="is over"):
            http.get_range(f"{cog.base}/x/x.tif", 0, cop.MAX_RESPONSE_BYTES)
    finally:
        http.close()
    assert cog.log == []


def test_a_file_larger_than_the_cap_is_refused(cog, monkeypatch):
    monkeypatch.setattr(cop, "MAX_FILE_BYTES", 1000)
    with pytest.raises(OSError, match="does not answer"):
        _import_cop()
    assert len(cog.gets()) == 1


def test_the_fetch_byte_limit_is_enforced(cog, monkeypatch):
    monkeypatch.setattr(cop, "MAX_FETCH_BYTES", 200_000)
    with pytest.raises(OSError, match="byte limit"):
        _import_cop()


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)  # rasterio's handler prints it; harmless
def test_the_fetch_request_limit_is_enforced(cog, monkeypatch):
    # the synthetic window needs two range requests (header, then tiles), so a limit of 1 refuses the second
    monkeypatch.setattr(cop, "MAX_FETCH_REQUESTS", 1)
    with pytest.raises(OSError, match="request limit"):
        _import_cop()
    assert len(cog.gets()) == 1


def test_a_wrong_content_range_start_is_refused(cog):
    cog.wrong_range_start = True
    with pytest.raises(OSError, match="does not answer"):
        _import_cop()
    assert len(cog.gets()) == 1  # refused, and not retried


def test_a_corrupted_cached_window_is_fetched_again(cog):
    first = _import_cop()
    url_path = f"/{TILE_14}/{TILE_14}.tif"
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("copernicus-glo30"))
    (key,) = cache.entries()
    _corrupt_bytes(cache.path(key))  # same size, different bytes
    reads_before = len(cog.gets(url_path))
    second = _import_cop()
    reads_again = len(cog.gets(url_path)) - reads_before
    assert reads_again == reads_before  # the window was read again, exactly as the first import read it
    assert second.dataset.origin.sha256 == first.dataset.origin.sha256
    assert [o.key for o in second.dataset.objects] == [TILE_14]


def test_stale_partial_windows_are_removed_and_fresh_ones_kept(cog):
    directory = provider_cache.provider_cache_dir("copernicus-glo30")
    directory.mkdir(parents=True)
    old, fresh = directory / "~x.part", directory / "~y.part"
    old.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    _import_cop()
    assert not old.exists() and fresh.exists()


def test_the_clip_is_densified_to_100_m_before_it_is_taken_to_lon_lat(monkeypatch):
    import shapely

    calls: list[float] = []
    original = shapely.segmentize

    def spy(geometry, max_segment_length):
        calls.append(max_segment_length)
        return original(geometry, max_segment_length)

    monkeypatch.setattr(shapely, "segmentize", spy)
    box = shapely.box(-747000.0, -1025600.0, -746000.0, -1024400.0)
    min_lon, min_lat, max_lon, max_lat = cop._lonlat_bounds(box, KROVAK)
    assert calls == [100.0]
    assert 14.0 < min_lon < max_lon < 15.0 and 50.0 < min_lat < max_lat < 51.0


def test_the_liability_notice_is_quoted_exactly_from_the_provider_contract():
    text = (REPO_ROOT / "docs" / "data-contracts" / "terrain-providers.md").read_text(encoding="utf-8")
    quoted = re.search(r'"(The organisations in charge[^"]*?WorldDEM-30)"', text)
    assert quoted is not None
    assert " ".join(quoted.group(1).split()) == cop.LIABILITY_NOTICE


async def test_copernicus_surface_and_datum_warnings_are_stored_without_an_inspect(server_url, cog):
    params = {
        "provider": "copernicus-glo30",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:4326", "vertical_crs": "EPSG:3855"},
    }
    async with connect(server_url) as ws:
        await _open(ws)
        run = await _run(ws, "1", params)  # no import.inspect before it
        assert cop.SURFACE_WARNING in run["warnings"] and cop.DATUM_WARNING in run["warnings"]
        got, _t, _s = await _call(ws, "2", "source.get", {"source_id": run["source_id"]})
        assert cop.SURFACE_WARNING in got.result["warnings"] and cop.DATUM_WARNING in got.result["warnings"]


def test_range_file_reads_more_than_the_block_cache_holds():
    data = np.random.default_rng(11).bytes(16 << 20)

    class Stub:
        def get_range(self, url, start, end):
            return data[start : end + 1], len(data)

    remote = cop._RangeFile(Stub(), "https://copernicus-dem-30m.s3.amazonaws.com/x/x.tif")
    assert remote.read(9 << 20) == data[: 9 << 20]  # 72 blocks in one read: more than the 64 cached
    offset = 3 * cop.BLOCK_BYTES - 100  # an unaligned read across a block boundary, after eviction
    remote.seek(offset)
    assert remote.read(300) == data[offset : offset + 300]


def test_an_index_entry_that_escapes_the_cache_is_dropped_and_the_provider_still_works(cuzk):
    first = _import_cuzk()
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    data = json.loads(cache.index_path.read_text(encoding="utf-8"))
    data["files"].append({**data["files"][0], "file": "../x"})
    data["files"].append({**data["files"][0], "file": "DMR5G/TESTC1.zip", "size": "big"})
    cache.index_path.write_text(json.dumps(data), encoding="utf-8")
    assert set(cache.entries()) == {"DMR5G/TESTA1.zip", "DMR5G/TESTB1.zip"}
    again = _import_cuzk(previous=first.dataset)
    assert len(again.reconciliation.unchanged) == 2 and not again.reconciliation.changed


def test_a_clip_over_too_many_tiles_is_refused(cog):
    with pytest.raises(ValueError, match="smaller clip"):
        _import_cop(_bbox_query(-900000.0, -1300000.0, -500000.0, -900000.0))
    assert cog.log == []


# --- precedence -----------------------------------------------------------------------------------

FINE_BOX = (-746900.0, -1025400.0, -746500.0, -1025000.0)
COARSE_OFFSET = 2.0


def _coarse_grid(box=SHEET_BOX, cell: float = 30.0, offset: float = COARSE_OFFSET) -> TerrainGrid:
    min_e, min_n, max_e, max_n = box
    cols, rows = int((max_e - min_e) / cell), int((max_n - min_n) / cell)
    e = min_e + (np.arange(cols) + 0.5) * cell
    n = max_n - (np.arange(rows) + 0.5) * cell
    ee, nn = np.meshgrid(e, n)
    return TerrainGrid(min_e, max_n, cell, cell, surface(ee, nn) + offset)


def _fine_points(spacing: float = 2.0, seed: int = 3) -> TerrainPoints:
    return TerrainPoints(sample_irregular(FINE_BOX, spacing, seed=seed), None)


def test_resolution_is_measured_from_the_payload_itself():
    cloud = TerrainPoints(sample_irregular(FINE_BOX, 8.0, seed=1), None)
    assert 2.0 < payload_resolution_m(cloud) < 6.0  # the median nearest-neighbour distance
    assert payload_resolution_m(_coarse_grid()) == pytest.approx(30.0)
    assert payload_resolution_m(TerrainPoints(np.zeros((1, 3)), None)) is None
    assert source_resolution_m([cloud, _coarse_grid()]) == payload_resolution_m(cloud)  # the finest object
    assert source_resolution_m([]) is None


def test_two_surveys_of_one_density_tie_and_different_densities_do_not():
    a = payload_resolution_m(TerrainPoints(sample_irregular(FINE_BOX, 8.0, seed=1), None))
    b = payload_resolution_m(TerrainPoints(sample_irregular(FINE_BOX, 8.0, seed=2), None))
    assert a != b  # the raw numbers wobble ...
    assert resolution_band(a) == resolution_band(b)  # ... the bands do not
    assert resolution_band(1.4) < resolution_band(5.0) < resolution_band(30.0)
    assert resolution_band(None) == resolution_band(float("nan")) == float("inf")


def _source(source_id: str, resolution: float | None, recency: int) -> StackSource:
    return StackSource(source_id, GridSurface(_coarse_grid()), resolution, recency)


def test_sources_are_ordered_finest_declared_resolution_first_then_newest():
    dmr5g, dmr4g, copernicus = _source("dmr5g", 1.41, 1), _source("dmr4g", 5.0, 2), _source("cop", 30.0, 3)
    ordered = order_sources([copernicus, dmr4g, dmr5g])
    assert [s.source_id for s in ordered] == ["dmr5g", "dmr4g", "cop"]  # not by import order
    older, newer = _source("older", 8.0, 1), _source("newer", 8.3, 2)  # one band: newest first
    assert [s.source_id for s in order_sources([older, newer])] == ["newer", "older"]
    unknown = _source("unknown", None, 9)
    assert [s.source_id for s in order_sources([unknown, copernicus, dmr5g])] == ["dmr5g", "cop", "unknown"]
    stack = stack_from_sources([copernicus, dmr5g])
    assert stack.source_ids == ("dmr5g", "cop") and len(stack.surfaces) == 2


def _fine_in_coarse() -> TerrainStack:
    fine = StackSource("fine", tin_from_points(_fine_points().points), 1.0, 1)
    coarse = StackSource("coarse", GridSurface(_coarse_grid()), 30.0, 2)  # newer, but coarser
    return stack_from_sources([coarse, fine])


def test_a_user_file_beats_a_provider_of_the_same_band():
    surface = GridSurface(_coarse_grid())
    provider = StackSource("provider", surface, 30.0, 2, from_provider=True)  # imported last
    user = StackSource("user", surface, 30.0, 1)
    assert [s.source_id for s in order_sources([provider, user])] == ["user", "provider"]
    assert stack_from_sources([provider, user]).source_ids == ("user", "provider")
    finer = StackSource("finer provider", surface, 1.4, 3, from_provider=True)
    assert [s.source_id for s in order_sources([user, finer])] == ["finer provider", "user"]
    snapshot = _snapshot(
        ("provider", "provider grid", [_coarse_grid(offset=0.0)], None, 30.0, 2, True),
        ("user", "user grid", [_coarse_grid()], None, 30.0, 1, False),
    )
    stack, _coverage, _warnings = handlers._build_terrain(snapshot)
    assert stack.source_ids == ("user", "provider")


def test_the_stack_answers_from_the_fine_source_inside_its_coverage_and_the_coarse_one_outside():
    stack = _fine_in_coarse()
    assert stack.source_ids == ("fine", "coarse")
    inside = np.array([(-746700.0, -1025200.0), (-746850.0, -1025350.0)])
    outside = np.array([(-746000.0, -1024800.0), (-747000.0, -1025600.0)])
    heights, index = stack.heights_and_source(inside[:, 0], inside[:, 1])
    assert list(index) == [0, 0] and heights.shape == (2,)
    assert np.abs(heights - surface(inside[:, 0], inside[:, 1])).max() < 0.2  # the fine surface's own value
    heights, index = stack.heights_and_source(outside[:, 0], outside[:, 1])
    assert list(index) == [1, 1]
    assert np.abs(heights - (surface(outside[:, 0], outside[:, 1]) + COARSE_OFFSET)).max() < 0.5
    far = stack.heights_and_source(np.array([0.0]), np.array([0.0]))
    assert np.isnan(far[0]).all() and list(far[1]) == [NO_SOURCE]
    assert np.array_equal(
        stack.heights(inside[:, 0], inside[:, 1]), stack.heights_and_source(inside[:, 0], inside[:, 1])[0]
    )
    scalar, idx = stack.heights_and_source(-746700.0, -1025200.0)  # shapes follow the inputs
    assert np.shape(scalar) == () == np.shape(idx)


def test_a_stack_with_source_ids_checks_their_number():
    with pytest.raises(ValueError, match="names 1 sources for 2"):
        TerrainStack((GridSurface(_coarse_grid()), GridSurface(_coarse_grid())), ("only-one",))
    assert TerrainStack((GridSurface(_coarse_grid()),)).source_ids == ()  # the old form still works


def test_tile_source_index_matches_the_stack_and_marks_no_data_with_255():
    stack = _fine_in_coarse()
    seen: set[int] = set()
    for ix, iy in [(-1459, -2003), (-1459, -2002), (-1458, -2003), (-1458, -2002), (-1460, -2002)]:
        tile = bake_tile(stack, 0, ix, iy)
        e, n = lattice(0, ix, iy)
        heights, index = stack.heights_and_source(e.ravel(), n.ravel())
        want = np.where(index >= 0, index, NO_SOURCE_BYTE).astype(np.uint8).reshape(SAMPLES, SAMPLES)
        assert tile.source_index.dtype == np.uint8 and tile.source_index.shape == (SAMPLES, SAMPLES)
        assert np.array_equal(tile.source_index, want)
        assert np.array_equal(tile.source_index == NO_SOURCE_BYTE, np.isnan(tile.heights))
        seen |= set(np.unique(tile.source_index).tolist())
    assert {0, 1} <= seen


def test_a_single_surface_is_source_zero_where_it_has_data():
    tile = bake_tile(GridSurface(_coarse_grid()), 0, -1459, -2003)
    valid = np.isfinite(tile.heights)
    assert (tile.source_index[valid] == 0).all() and (tile.source_index[~valid] == NO_SOURCE_BYTE).all()


def _snapshot(*entries):
    return tuple(entries)


SEAM = re.compile(r"mean ([0-9.]+) m, 95th percentile ([0-9.]+) m over (\d+) points")


def test_the_height_step_at_a_seam_is_reported_as_mean_and_95th_percentile():
    snapshot = _snapshot(
        ("fine", "fine survey", [_fine_points()], None, None, 1, False),
        ("coarse", "coarse grid", [_coarse_grid()], None, None, 2, False),  # imported later, but coarser
    )
    stack, coverage, warnings = handlers._build_terrain(snapshot)
    assert stack.source_ids == ("fine", "coarse") and len(coverage) == 2
    (seam,) = warnings
    assert "seam between 'fine survey' and 'coarse grid'" in seam and "not blended" in seam
    mean, p95, count = SEAM.search(seam).groups()
    assert float(mean) == pytest.approx(COARSE_OFFSET, abs=0.3) and float(p95) >= float(mean)
    assert float(p95) < COARSE_OFFSET + 1.0 and int(count) >= 100


def test_sources_that_do_not_meet_have_no_seam_and_a_skipped_source_is_still_reported():
    far = TerrainGrid(-740000.0, -1020000.0, 30.0, 30.0, np.full((20, 20), 300.0))
    snapshot = _snapshot(
        ("fine", "fine", [_fine_points()], None, None, 1, False),
        ("far", "far grid", [far], None, None, 2, False),
        (
            "thin",
            "thin",
            [TerrainPoints(np.array([[0.0, 0.0, 1.0], [1.0, 1.0, 2.0]]), None)],
            None,
            None,
            3,
            False,
        ),
    )
    stack, _coverage, warnings = handlers._build_terrain(snapshot)
    assert stack.source_ids == ("fine", "far")
    assert len(warnings) == 1 and "terrain source 'thin' skipped" in warnings[0]  # and no seam


# --- over the wire --------------------------------------------------------------------------------


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


def _las(path: Path, points: np.ndarray) -> str:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.write(path)
    return str(path)


async def _run(ws, request_id: str, params: dict[str, Any]):
    reply, _tail, _size = await _call(ws, request_id, "import.run", params)
    assert reply.type == "res", reply.error
    return reply.result


async def test_tiles_carry_a_source_map_and_the_ordered_source_ids_finest_first(server_url, tmp_path):
    fine = _las(tmp_path / "fine.las", sample_irregular(FINE_BOX, 2.5, seed=3))
    coarse = _las(tmp_path / "coarse.las", sample_irregular(SHEET_BOX, 12.0, seed=4))
    settings = {"horizontal_crs": "EPSG:5514"}
    async with connect(server_url) as ws:
        await _open(ws)
        fine_id = (await _run(ws, "1", {"paths": [fine], "kind": "terrain", "settings": settings}))[
            "source_id"
        ]
        coarse_id = (await _run(ws, "2", {"paths": [coarse], "kind": "terrain", "settings": settings}))[
            "source_id"
        ]
        listed, _t, _s = await _call(ws, "3", "terrain.tiles", {"level": 0})
        assert listed.type == "res", listed.error
        # the older source is the finer one, so it is first even though the other was imported later
        assert listed.result["source_ids"] == [fine_id, coarse_id]
        assert any("seam between" in w for w in listed.result["warnings"])
        revision = listed.result["revision"]
        found: set[int] = set()
        for info in listed.result["tiles"]:
            reply, tail, _size = await _call(
                ws, f"t{info['ix']}_{info['iy']}", "terrain.tile",
                {"level": 0, "ix": info["ix"], "iy": info["iy"], "revision": revision},
            )  # fmt: skip
            assert reply.type == "res", reply.error
            assert [b.name for b in reply.blobs] == ["heights", "source_index"]
            assert reply.blobs[1].dtype == "<u1" and tuple(reply.blobs[1].shape) == (129, 129)
            assert reply.result["source_ids"] == [fine_id, coarse_id]
            heights = unpack_blob(tail, reply.blobs[0])
            index = unpack_blob(tail, reply.blobs[1])
            assert np.array_equal(index == 255, np.isnan(heights))
            e, n = lattice(0, info["ix"], info["iy"])
            m = 20.0
            in_fine = (
                (e > FINE_BOX[0] + m) & (e < FINE_BOX[2] - m) & (n > FINE_BOX[1] + m) & (n < FINE_BOX[3] - m)
            )
            far_from_fine = (
                (e < FINE_BOX[0] - m) | (e > FINE_BOX[2] + m) | (n < FINE_BOX[1] - m) | (n > FINE_BOX[3] + m)
            )
            inner = (
                (e > SHEET_BOX[0] + 30)
                & (e < SHEET_BOX[2] - 30)
                & (n > SHEET_BOX[1] + 30)
                & (n < SHEET_BOX[3] - 30)
            )
            assert (index[in_fine] == 0).all()
            assert (index[far_from_fine & inner & np.isfinite(heights)] == 1).all()
            found |= set(np.unique(index).tolist())
        assert {0, 1} <= found
        got = await _call(ws, "g", "source.get", {"source_id": fine_id})
        other = await _call(ws, "h", "source.get", {"source_id": coarse_id})
        assert 0.5 < got[0].result["resolution_m"] < other[0].result["resolution_m"]


async def test_a_project_without_terrain_has_no_source_ids(server_url):
    async with connect(server_url) as ws:
        await _open(ws)
        listed, _t, _s = await _call(ws, "1", "terrain.tiles", {"level": 0})
        assert listed.result["source_ids"] == [] and listed.result["tiles"] == []


async def test_provider_requests_follow_the_import_rules_over_the_wire(server_url, cuzk, tmp_path):
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        noproject, _t, _s = await _call(
            ws, "n", "import.inspect", {"provider": "cuzk-dmr5g", "query": BBOX_QUERY}
        )
        assert (noproject.type, noproject.error.code) == ("err", "E_NO_PROJECT")
        assert (await _call(ws, "p", "project.new", {}))[0].type == "res"  # a project without a CRS
        nocrs, _t, _s = await _call(
            ws, "c", "import.inspect", {"provider": "cuzk-dmr5g", "query": BBOX_QUERY}
        )
        assert (nocrs.type, nocrs.error.code) == ("err", "E_CRS_REQUIRED")
        assert (await _call(ws, "p2", "project.new", {"crs": "EPSG:5514"}))[0].type == "res"

        inspected, _t, _s = await _call(
            ws, "i", "import.inspect", {"provider": "cuzk-dmr5g", "query": BBOX_QUERY}
        )
        assert inspected.type == "res", inspected.error
        assert inspected.result["counts"]["sheets"] == 2 and inspected.result["declared_crs"] == "EPSG:5514"
        suggested = inspected.result["suggested_settings"]
        assert suggested["horizontal_crs"] == "EPSG:5514" and suggested["point_classes"] is None
        assert suggested["clip"]["type"] == "bbox"

        for number, params in enumerate(
            (
                {"provider": "cuzk-dmr5g", "path": str(tmp_path / "x.las")},
                {"provider": "cuzk-dmr5g", "paths": [str(tmp_path / "x.las")]},
                {"provider": "no-such-provider"},
                {"query": {"max_total_mb": 5}},  # a query without a provider
                {"provider": "cuzk-dmr5g", "query": {"clip": "everything"}},
                {"provider": "cuzk-dmr5g", "query": {"surprise": 1}},
            )
        ):
            for method in ("import.inspect", "import.run"):
                body = {**params, **({"kind": "terrain", "settings": {}} if method == "import.run" else {})}
                reply, _t, _s = await _call(ws, f"b{number}{method}", method, body)
                assert (reply.type, reply.error.code) == ("err", "E_BAD_PARAMS"), (method, params, reply)
        # a corridor clip in a project without alignments, and a service that does not answer
        empty, _t, _s = await _call(ws, "e", "import.inspect", {"provider": "cuzk-dmr5g"})
        assert (empty.type, empty.error.code) == ("err", "E_EMPTY")
        cuzk.routes.pop("/DMR5G.xml")
        down, _t, _s = await _call(ws, "d", "import.inspect", {"provider": "cuzk-dmr5g", "query": BBOX_QUERY})
        assert (down.type, down.error.code) == (
            "err",
            "E_BAD_PARAMS",
        ) and "download service" in down.error.message
        declared = next(m for m in METHODS if m.name == "import.inspect").errors
        assert {ErrorCode.NO_PROJECT, ErrorCode.CRS_REQUIRED, ErrorCode.EMPTY} <= set(declared)


async def test_a_provider_import_reimport_and_tiles_over_the_wire(server_url, cuzk):
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514", "vertical_crs": "EPSG:5705"},
    }
    async with connect(server_url) as ws:
        await _open(ws)
        first = await _run(ws, "1", params)
        assert (first["added"], first["changed"], first["unchanged"], first["removed"]) == (2, 0, 0, 0)
        assert first["effective_crs"] == "EPSG:5514"
        got, _t, _s = await _call(ws, "2", "source.get", {"source_id": first["source_id"]})
        result = got.result
        assert result["origin"]["type"] == "provider" and result["origin"]["provider_id"] == "cuzk-dmr5g"
        assert (
            len(result["origin"]["sha256"]) == 2
            and json.loads(result["origin"]["query"])["max_total_mb"] == 500.0
        )
        assert result["attribution"] == "© ČÚZK" and result["licence"] == "CC BY 4.0"
        assert [o["key"] for o in result["objects"]] == ["TESTA1", "TESTB1"]
        assert result["settings"]["clip"]["type"] == "bbox" and result["resolution_m"] > 0.0
        listing, _t, _s = await _call(ws, "3", "source.list", {})
        assert listing.result["sources"][0]["attribution"] == "© ČÚZK"

        again = await _run(ws, "4", {**params, "reimport_of": first["source_id"]})
        assert again["source_id"] == first["source_id"]
        assert (again["added"], again["changed"], again["unchanged"], again["removed"]) == (0, 0, 2, 0)
        assert len(_file_gets(cuzk)) == 2  # nothing was downloaded twice

        tiles, _t, _s = await _call(ws, "5", "terrain.tiles", {"level": 0})
        assert tiles.type == "res", tiles.error
        assert tiles.result["tiles"] and tiles.result["source_ids"] == [first["source_id"]]
        info = tiles.result["tiles"][0]
        tile, tail, _size = await _call(
            ws, "6", "terrain.tile",
            {"level": 0, "ix": info["ix"], "iy": info["iy"], "revision": tiles.result["revision"]},
        )  # fmt: skip
        assert tile.type == "res"
        heights, index = unpack_blob(tail, tile.blobs[0]), unpack_blob(tail, tile.blobs[1])
        assert np.array_equal(index == 255, np.isnan(heights))  # the one source, index 0, where it has data
        assert (index[np.isfinite(heights)] == 0).all() and set(np.unique(index).tolist()) <= {0, 255}


async def test_session_ping_keeps_answering_while_a_provider_downloads(server_url, cuzk):
    """The download and the read are blocking work: they run in worker threads, so pings are unaffected."""
    cuzk.delay = 0.05  # each 4 KB of a sheet takes 50 ms to arrive
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514"},
    }
    async with connect(server_url) as ws:
        await _open(ws)
        started = time.perf_counter()
        reply, latencies = await _ping_while(ws, "run", "import.run", params, interval=0.05)
        elapsed = time.perf_counter() - started
        assert reply.type == "res", reply.error
        assert elapsed > 0.6 and len(latencies) >= 5 and max(latencies) < 0.5, (elapsed, len(latencies))


async def test_project_new_releases_provider_pins(server_url, cuzk):
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514"},
    }
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    async with connect(server_url) as ws:
        await _open(ws)
        await _run(ws, "1", params)
        assert cache.pinned()  # the import pins the files of its source
        assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"}))[0].type == "res"
        assert cache.pinned() == frozenset()


async def test_closing_a_connection_releases_its_provider_pins(server_url, cuzk):
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514"},
    }
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    async with connect(server_url) as ws:
        await _open(ws)
        await _run(ws, "1", params)
        assert cache.pinned()
    for _ in range(100):  # the server's handler for the closed connection runs after it has closed
        if not cache.pinned():
            break
        await asyncio.sleep(0.02)
    assert cache.pinned() == frozenset()


async def test_a_connection_closed_during_a_provider_import_leaves_no_pin(server_url, cuzk, monkeypatch):
    cuzk.delay = 0.1  # the download is still going when the connection closes
    done = threading.Event()
    original = handlers._run_provider_with_coverage

    def marked(*args):
        try:
            return original(*args)
        finally:
            done.set()

    monkeypatch.setattr(handlers, "_run_provider_with_coverage", marked)
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514"},
    }
    cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    ws = await connect(server_url)
    await _open(ws)
    await _send(ws, "run", "import.run", params)
    await asyncio.sleep(0.4)
    await ws.close()
    await asyncio.sleep(0.3)
    assert not done.is_set()  # the worker thread is still reading after the connection has gone
    assert await asyncio.to_thread(done.wait, 60)
    await asyncio.sleep(0.2)
    assert cache.pinned() == frozenset()  # nothing stored, so nothing pinned


async def test_a_provider_import_is_a_write_barrier_for_the_requests_pipelined_after_it(server_url, cuzk):
    params = {
        "provider": "cuzk-dmr5g",
        "query": BBOX_QUERY,
        "kind": "terrain",
        "settings": {"horizontal_crs": "EPSG:5514"},
    }
    async with connect(server_url) as ws:
        await _open(ws)
        await _send(ws, "run", "import.run", params)
        await _send(ws, "list", "source.list", {})
        replies = await asyncio.wait_for(_collect(ws, 2), 30)
        assert replies["run"].type == "res" and len(replies["list"].result["sources"]) == 1


# --- live services (-m network) -------------------------------------------------------------------


@pytest.mark.network
def test_live_kralupy_dmr5g_and_copernicus_by_provider(kralupy, tmp_path, monkeypatch):
    """The acceptance run of T-133: both sources of the Kralupy corridor by provider, L0 to L4 tile counts,
    the download figures, the seam between them and the datum-offset estimate. Prints every figure."""
    from coypu_builder.io.mesh.terrain_tiles import tiles_for_coverage

    monkeypatch.undo()  # the real hosts, and the real allow-lists
    monkeypatch.setattr(provider_cache, "providers_cache_root", lambda: tmp_path / "provider-cache")
    register_builtin_readers_and_providers()
    alignments = {"kralupy": kralupy.alignment}
    settings = ImportSettings(horizontal_crs="EPSG:5514", vertical_crs="EPSG:5705")

    started = time.perf_counter()
    _, report5g = inspect_provider("cuzk-dmr5g", {}, KROVAK, alignments)
    print(f"DMR 5G inspect: {report5g.counts} in {time.perf_counter() - started:.1f} s")
    started = time.perf_counter()
    dmr = run_provider_import("cuzk-dmr5g", {}, SourceKind.TERRAIN, settings, KROVAK, alignments)
    t_dmr = time.perf_counter() - started
    cache5g = provider_cache.ProviderCache(provider_cache.provider_cache_dir("cuzk-dmr5g"))
    size5g = cache5g.total_bytes()
    sheets = [o.key for o in dmr.dataset.objects]
    points = sum(len(p.points) for p in dmr.payloads.values())
    print(
        f"DMR 5G: {len(sheets)} sheets {sheets}, {size5g / 1e6:.1f} MB downloaded, "
        f"{points} points, {t_dmr:.1f} s"
    )

    for warning in dmr.warnings:
        print("DMR 5G import warning:", warning)
    assert 12 <= report5g.counts["sheets"] <= 16 and "KRAV82" in sheets
    started = time.perf_counter()
    again = run_provider_import(
        "cuzk-dmr5g", {}, SourceKind.TERRAIN, settings, KROVAK, alignments, previous=dmr.dataset
    )
    print(
        f"DMR 5G re-import: {time.perf_counter() - started:.1f} s, "
        f"changed {len(again.reconciliation.changed)} unchanged {len(again.reconciliation.unchanged)}"
    )

    started = time.perf_counter()
    cop_settings = ImportSettings(horizontal_crs="EPSG:4326", vertical_crs="EPSG:3855")
    glo = run_provider_import("copernicus-glo30", {}, SourceKind.TERRAIN, cop_settings, KROVAK, alignments)
    t_glo = time.perf_counter() - started
    cache_cop = provider_cache.ProviderCache(provider_cache.provider_cache_dir("copernicus-glo30"))
    tiles_read = [o.key for o in glo.dataset.objects]
    stats = next(p for p in registry.providers() if p.provider_id == "copernicus-glo30").last_stats
    print(
        f"Copernicus GLO-30: tiles {tiles_read}, {stats['requests']} range requests, "
        f"{stats['bytes'] / 1e3:.0f} kB "
        f"transferred, window cache {cache_cop.total_bytes() / 1e3:.0f} kB, {t_glo:.1f} s, "
        f"{sum(g.heights.size for g in glo.payloads.values())} cells"
    )
    started = time.perf_counter()
    run_provider_import("copernicus-glo30", {}, SourceKind.TERRAIN, cop_settings, KROVAK, alignments)
    print(f"Copernicus re-import from the cache: {time.perf_counter() - started:.2f} s")

    project = ProjectState(project_id="live", crs=KROVAK)
    for outcome in (dmr, glo):
        coverage = handlers.clip_polygon(outcome.dataset.settings.clip, alignments)
        project.sources[outcome.dataset.id] = SourceEntry(
            outcome.dataset, outcome.payloads, outcome.warnings, coverage, outcome.resolution_m
        )
    print(f"declared resolutions: DMR 5G {dmr.resolution_m:.2f} m, GLO-30 {glo.resolution_m:.2f} m")
    started = time.perf_counter()
    stack, coverage, warnings = handlers._build_terrain(handlers._terrain_snapshot(project))
    print(f"stack built in {time.perf_counter() - started:.1f} s; sources {stack.source_ids}")
    for level in range(5):
        print(f"L{level}: {len(tiles_for_coverage(level, list(coverage)))} tiles")
    for warning in warnings:
        print("warning:", warning)
    assert stack.source_ids == (dmr.dataset.id, glo.dataset.id)  # finest first
    assert any("seam between" in w for w in warnings)

    # the datum-offset estimate: GLO-30 minus DMR 5G where both have data
    rng = np.random.default_rng(1)
    area = handlers.clip_polygon(dmr.dataset.settings.clip, alignments)
    min_e, min_n, max_e, max_n = area.bounds
    e, n = rng.uniform(min_e, max_e, 60000), rng.uniform(min_n, max_n, 60000)
    keep = shapely.contains_xy(area, e, n)
    e, n = e[keep], n[keep]
    fine, coarse = stack.surfaces[0].heights(e, n), stack.surfaces[1].heights(e, n)
    both = np.isfinite(fine) & np.isfinite(coarse)
    d = coarse[both] - fine[both]
    low = d <= np.percentile(
        d, 50
    )  # trees and buildings only raise a surface model: the lower half is open ground
    band = (d >= np.percentile(d, 10)) & (d <= np.percentile(d, 50))
    print(
        f"GLO-30 minus DMR 5G over {both.sum()} points: mean {d.mean():.2f} m, median {np.median(d):.2f} m, "
        f"lower-half mean {d[low].mean():.2f} m, 10th-50th percentile band mean {d[band].mean():.2f} m, "
        f"p5 {np.percentile(d, 5):.2f} m, p95 {np.percentile(d, 95):.2f} m"
    )
    assert both.sum() > 1000


def register_builtin_readers_and_providers() -> None:
    register_builtin_readers()
    register_builtin_providers()
