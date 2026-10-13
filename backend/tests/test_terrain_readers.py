"""T-131: the terrain readers -- LAS/LAZ, GeoTIFF and LandXML TIN surfaces -- ZIP members, multi-file sources
and the `paths` protocol parameter.

Every fixture is generated here, in `tmp_path`, from T-130's synthetic surface; nothing reads real data.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any

import laspy
import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import CRS
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION, config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import BBoxClip, CorridorClip, ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainGrid, TerrainPoints, TerrainTin, triangle_areas2
from coypu_builder.io.gis.envelope import EnvelopeOptions, corridor_envelope
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.registry import reader_for
from coypu_builder.io.sources.runner import CrsRequired, inspect_source, run_import
from coypu_builder.io.terrain import archive as archive_module
from coypu_builder.io.terrain import las as las_module
from coypu_builder.io.terrain import raster as raster_module
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame
from fixtures.synthetic.terrain import (
    KRAV82_BBOX,
    points_in_bbox,
    sample_grid,
    sample_irregular,
    split_sheets,
    surface,
)

KROVAK = ProjectCRS("EPSG:5514")
LOCAL_BBOX = (-746800.0, -1025300.0, -746400.0, -1024900.0)
CORRIDOR_BOX = (-746800.0, -1024800.0, -746350.0, -1024000.0)


@pytest.fixture(autouse=True)
def builtin_registry(monkeypatch, tmp_path):
    """A private registry holding only the three built-in readers, and a user config that cannot be real."""
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    monkeypatch.setattr(config, "config_path", lambda: tmp_path / "user-config" / "config.toml")
    register_builtin_readers()


# --- writers -


def write_las(
    path: Path,
    points: np.ndarray,
    classes: np.ndarray | None = None,
    crs: str | None = None,
    scale: float = 1e-6,
    point_format: int = 6,
) -> Path:
    """Write (n, 3) E, N, H as LAS or LAZ (by suffix) with Krovak-scale offsets in the header."""
    header = laspy.LasHeader(point_format=point_format, version="1.4" if point_format >= 6 else "1.2")
    header.scales = [scale] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    if crs is not None:
        header.add_crs(CRS.from_user_input(crs))
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    if classes is not None:
        las.classification = classes
    las.write(path)
    return path


def write_tif(
    path: Path,
    heights: np.ndarray,
    origin_x: float,
    origin_y: float,
    cell: float,
    crs: str | None = "EPSG:5514",
    nodata: float | None = -9999.0,
) -> Path:
    """`heights` row 0 is the northernmost row; (origin_x, origin_y) is the outer corner of cell [0, 0]."""
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=heights.shape[0],
        width=heights.shape[1],
        count=1,
        dtype="float64",
        crs=crs,
        transform=Affine(cell, 0.0, origin_x, 0.0, -cell, origin_y),
        nodata=nodata,
    ) as ds:
        ds.write(heights, 1)
    return path


def landxml_text(
    surfaces: list[dict[str, Any]],
    epsg: int | None = 5514,
    krovak_positive: bool = False,
    unit: str = "meter",
    order: str = "NE",
) -> str:
    """A LandXML 1.2 document. Each surface: name (or None), points (n, 3) E, N, H, faces [(a, b, c, visible)]
    of 0-based point rows, id_offset. Points are written "north east elevation" (positive Krovak X Y with
    `krovak_positive`) with ids `id_offset + row`."""
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">',
        f'<Units><Metric linearUnit="{unit}" areaUnit="squareMeter" volumeUnit="cubicMeter"/></Units>',
    ]
    if epsg is not None:
        out.append(f'<CoordinateSystem name="test" epsgCode="{epsg}"/>')
    out.append("<Surfaces>")
    for surf in surfaces:
        name = f' name="{surf["name"]}"' if surf.get("name") else ""
        out.append(f'<Surface{name}><Definition surfType="TIN"><Pnts>')
        offset = surf.get("id_offset", 1)
        for row, (e, n, h) in enumerate(surf["points"]):
            a, b = (-n, -e) if krovak_positive else (n, e)
            if order == "EN":
                a, b = b, a
            out.append(f'<P id="{offset + row}">{float(a)!r} {float(b)!r} {float(h)!r}</P>')
        out.append("</Pnts><Faces>")
        for a, b, c, visible in surf["faces"]:
            flag = "" if visible else ' i="1"'
            out.append(f"<F{flag}>{offset + a} {offset + b} {offset + c}</F>")
        out.append("</Faces></Definition></Surface>")
    out.append("</Surfaces></LandXML>")
    return "\n".join(out)


def grid_tin(bbox, cell: float, clockwise_every: int = 3, hidden_every: int = 7) -> dict[str, Any]:
    """A regular triangulation of the synthetic surface; some faces clockwise, some invisible."""
    grid = sample_grid(bbox, cell)
    ny, nx = grid.z.shape
    points = grid.points()
    faces = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            p00, p10, p01, p11 = j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1
            for tri in ((p00, p10, p11), (p00, p11, p01)):
                k = len(faces)
                a, b, c = tri if k % clockwise_every else tri[::-1]
                faces.append((a, b, c, k % hidden_every != 0))
    return {"points": points, "faces": faces}


# --- helpers -


def _import(path, settings: ImportSettings | None = None, alignments=None, **kwargs):
    return run_import(
        path, SourceKind.TERRAIN, settings or ImportSettings(), KROVAK, alignments or {}, **kwargs
    )


def _payloads(outcome) -> list[Any]:
    return [outcome.payloads[record.id] for record in outcome.dataset.objects]


def _sorted(points: np.ndarray) -> np.ndarray:
    return points[np.lexsort((points[:, 1], points[:, 0]))]


def _sorted_coarse(points: np.ndarray) -> np.ndarray:
    """Sort on decimetre-rounded columns, for comparing clouds that agree only to a few millimetres."""
    keys = np.round(points, 1)
    return points[np.lexsort((keys[:, 1], keys[:, 0]))]


def _inside_even_odd(polygon, points: np.ndarray) -> np.ndarray:
    """Independent point-in-polygon (even-odd ray crossing) for Polygon / MultiPolygon."""
    parts = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
    x, y = points[:, 0], points[:, 1]
    crossings = np.zeros(len(points), dtype=int)
    for part in parts:
        for ring in (part.exterior, *part.interiors):
            ring_xy = np.asarray(ring.coords)
            for (x0, y0), (x1, y1) in zip(ring_xy[:-1], ring_xy[1:], strict=True):
                if y0 == y1:
                    continue
                straddles = (y0 > y) != (y1 > y)
                x_at = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
                crossings += (straddles & (x < x_at)).astype(int)
    return crossings % 2 == 1


# --- criterion 1: LAS / LAZ -


@pytest.mark.parametrize("suffix", [".las", ".laz"])
def test_las_reads_back_within_a_micrometre_with_krovak_scale_offsets(tmp_path, suffix):
    pts = sample_irregular(KRAV82_BBOX, 40.0, seed=11)
    path = write_las(tmp_path / f"sheet{suffix}", pts, crs="EPSG:5514")
    outcome = _import(path)
    (payload,) = _payloads(outcome)
    assert isinstance(payload, TerrainPoints)
    assert payload.points.dtype == np.float64
    assert np.abs(_sorted(payload.points) - _sorted(pts)).max() <= 1e-6
    assert np.abs(payload.points[:, 2] - surface(payload.points[:, 0], payload.points[:, 1])).max() <= 1e-6
    # exactly `integer * scale + offset` in float64: nothing narrowed to float32 on the way
    las = laspy.read(path)
    expected_x = las.X.astype(np.float64) * las.header.x_scale + las.header.x_offset
    assert np.array_equal(payload.points[:, 0], expected_x)
    assert abs(las.header.offsets[0]) > 700_000.0 and abs(las.header.offsets[1]) > 1_000_000.0
    assert outcome.dataset.objects[0].key == "sheet"


def test_las_without_a_crs_vlr_needs_horizontal_crs(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=1)
    path = write_las(tmp_path / "bare.las", pts)
    _reader, report = inspect_source(path)
    assert (report.format_id, report.declared_crs) == ("las", None)
    assert any("no coordinate reference system" in w for w in report.warnings)
    assert report.counts["points"] == len(pts) and report.counts["point_format_6"] == 1
    assert report.extent == pytest.approx(
        (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max()), abs=1e-6
    )
    with pytest.raises(CrsRequired):
        _import(path)
    outcome = _import(path, ImportSettings(horizontal_crs="EPSG:5514"))
    assert not any("coordinate reference system" in w or "CRS record" in w for w in outcome.warnings)
    assert outcome.dataset.declared_crs is None and outcome.dataset.effective_crs == "EPSG:5514"
    assert len(_payloads(outcome)[0].points) == len(pts)


def test_las_with_a_wkt_vlr_reports_its_crs(tmp_path):
    path = write_las(tmp_path / "wkt.laz", sample_irregular(LOCAL_BBOX, 20.0, seed=1), crs="EPSG:5514")
    _reader, report = inspect_source(path)
    assert report.declared_crs is not None
    assert CRS.from_user_input(report.declared_crs) == CRS.from_epsg(5514)
    assert report.suggested.horizontal_crs == report.declared_crs
    assert not any("no coordinate reference system" in w for w in report.warnings)
    assert _import(path).dataset.declared_crs == report.declared_crs


def test_las_class_filter_and_histogram(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=2)
    classes = np.where(np.arange(len(pts)) % 3 == 0, 2, np.where(np.arange(len(pts)) % 3 == 1, 1, 6)).astype(
        np.uint8
    )
    path = write_las(tmp_path / "classed.las", pts, classes, crs="EPSG:5514")
    _reader, report = inspect_source(path)
    assert report.counts["sample_points"] == len(pts)
    assert {k: v for k, v in report.counts.items() if k.startswith("sample_class_")} == {
        "sample_class_1": int((classes == 1).sum()),
        "sample_class_2": int((classes == 2).sum()),
        "sample_class_6": int((classes == 6).sum()),
    }
    assert not any(k.startswith("class_") for k in report.counts)
    ground = _payloads(_import(path, ImportSettings(point_classes=(2,))))[0]
    assert len(ground.points) == (classes == 2).sum()
    assert set(ground.classes.tolist()) == {2}
    assert np.abs(_sorted(ground.points) - _sorted(pts[classes == 2])).max() <= 1e-6
    both = _payloads(_import(path, ImportSettings(point_classes=(1, 6))))[0]
    assert len(both.points) == (classes != 2).sum() and ground.classes.dtype == np.uint8
    everything = _payloads(_import(path))[0]
    assert len(everything.points) == len(pts)
    empty = _import(path, ImportSettings(point_classes=(9,)))
    assert not empty.payloads
    assert any("classed.las" not in w and "classed: no points with classes (9,)" in w for w in empty.warnings)
    assert any("this file's classes are [1, 2, 6]" in w for w in empty.warnings)
    assert any("the import produced no objects" in w for w in empty.warnings)


@pytest.mark.parametrize("suffix", [".las", ".laz"])
def test_the_class_histogram_samples_the_whole_file_not_its_start(tmp_path, monkeypatch, suffix):
    pts = sample_irregular(KRAV82_BBOX, 20.0, seed=5)
    classes = np.repeat(np.array([2, 5, 8], dtype=np.uint8), len(pts) // 3 + 1)[: len(pts)]
    path = write_las(tmp_path / f"sorted{suffix}", pts, classes, crs="EPSG:5514")
    monkeypatch.setattr(las_module, "SAMPLE_POINTS", 3000)
    monkeypatch.setattr(las_module, "SAMPLE_BLOCKS", 30)
    report = inspect_source(path)[1]
    assert report.counts["points"] == len(pts) and report.counts["sample_points"] == 3000
    sampled = {k: v for k, v in report.counts.items() if k.startswith("sample_class_")}
    assert set(sampled) == {"sample_class_2", "sample_class_5", "sample_class_8"}
    assert sum(sampled.values()) == 3000


def test_las_and_laz_give_identical_payloads(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=3)
    las = _import(write_las(tmp_path / "a.las", pts, crs="EPSG:5514"))
    laz = _import(write_las(tmp_path / "a.laz", pts, crs="EPSG:5514"))
    a, b = _payloads(las)[0], _payloads(laz)[0]
    assert np.array_equal(a.points, b.points) and np.array_equal(a.classes, b.classes)


def test_las_is_reprojected_and_clipped_in_float64(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=4)
    e, n = KROVAK.to_crs("EPSG:32633", pts[:, 0], pts[:, 1])
    path = write_las(tmp_path / "utm.laz", np.column_stack([e, n, pts[:, 2]]), crs="EPSG:32633", scale=1e-4)
    clip = BBoxClip(-746700.0, -1025200.0, -746500.0, -1025000.0)
    outcome = _import(path, ImportSettings(clip=clip))
    (payload,) = _payloads(outcome)
    inside = points_in_bbox(pts, (clip.min_e, clip.min_n, clip.max_e, clip.max_n))
    assert 0 < len(inside) < len(pts)
    assert len(payload.points) == len(inside)
    assert np.abs(_sorted(payload.points) - _sorted(inside)).max() < 0.01
    assert outcome.dataset.transformation != "none"


# --- criterion 2: GeoTIFF -


def _surface_raster(tmp_path, name="grid.tif", cell=20.0, crs="EPSG:5514", bbox=LOCAL_BBOX):
    grid = sample_grid(bbox, cell)
    heights = grid.z[::-1].copy()
    heights[2, 3] = -9999.0
    heights[5, :2] = -9999.0
    path = write_tif(tmp_path / name, heights, bbox[0] - cell / 2, bbox[3] + cell / 2, cell, crs=crs)
    return path, grid, heights


def test_geotiff_in_the_project_crs_reads_cell_for_cell(tmp_path):
    path, grid, heights = _surface_raster(tmp_path)
    _reader, report = inspect_source(path)
    assert (report.format_id, report.declared_crs) == ("geotiff", "EPSG:5514")
    assert report.counts == {"width": 21, "height": 21, "bands": 1}
    assert report.extent == (
        LOCAL_BBOX[0] - 10.0,
        LOCAL_BBOX[1] - 10.0,
        LOCAL_BBOX[2] + 10.0,
        LOCAL_BBOX[3] + 10.0,
    )
    assert report.suggested.nodata == -9999.0
    outcome = _import(path)
    (payload,) = _payloads(outcome)
    assert isinstance(payload, TerrainGrid) and payload.heights.dtype == np.float64
    assert (payload.origin_e, payload.origin_n, payload.cell_e, payload.cell_n) == (
        LOCAL_BBOX[0] - 10.0,
        LOCAL_BBOX[3] + 10.0,
        20.0,
        20.0,
    )
    expected = np.where(heights == -9999.0, np.nan, heights)
    assert payload.heights.shape == (21, 21)
    assert np.array_equal(payload.heights, expected, equal_nan=True)
    assert np.isnan(payload.heights[2, 3]) and np.isnan(payload.heights[5, 1])
    e, n = payload.centres()
    assert np.abs(payload.heights[10, 10] - surface(e[10], n[10])) == 0.0
    for row, col in ((3, 15), (17, 4), (1, 19)):  # off-centre cells: a flipped or transposed read fails
        assert payload.heights[row, col] == surface(e[col], n[row])
    assert outcome.dataset.transformation == "none"


def test_geotiff_nodata_override_and_unit(tmp_path):
    path, _grid, heights = _surface_raster(tmp_path)
    marker = float(heights[8, 8])
    (payload,) = _payloads(_import(path, ImportSettings(nodata=marker, linear_unit_m=1.0)))
    assert np.isnan(payload.heights[8, 8])
    (plain,) = _payloads(_import(path))
    (halved,) = _payloads(_import(path, ImportSettings(linear_unit_m=0.5)))
    assert halved.cell_e == 10.0 and halved.origin_e == (LOCAL_BBOX[0] - 10.0) * 0.5
    assert np.array_equal(halved.heights, plain.heights * 0.5, equal_nan=True)


def test_geotiff_windowed_read_touches_only_the_clip_window(tmp_path, monkeypatch):
    path, _grid, heights = _surface_raster(tmp_path)
    windows = []
    original = raster_module._raw_heights

    def spy(ds, window, settings):
        windows.append((window.col_off, window.row_off, window.width, window.height))
        return original(ds, window, settings)

    monkeypatch.setattr(raster_module, "_raw_heights", spy)
    aligned = BBoxClip(-746650.0, -1025150.0, -746450.0, -1025050.0)
    (payload,) = _payloads(_import(path, ImportSettings(clip=aligned)))
    assert windows == [(8, 8, 10, 5)]
    assert payload.heights.shape == (5, 10)
    assert (payload.origin_e, payload.origin_n) == (-746650.0, -1025050.0)
    assert payload.bounds == (-746650.0, -1025150.0, -746450.0, -1025050.0)
    full = np.where(heights == -9999.0, np.nan, heights)
    assert np.array_equal(payload.heights, full[8:13, 8:18], equal_nan=True)

    windows.clear()
    off_grid = BBoxClip(-746655.0, -1025155.0, -746445.0, -1025045.0)
    (payload,) = _payloads(_import(path, ImportSettings(clip=off_grid)))
    assert windows == [(7, 7, 12, 7)]
    assert payload.heights.shape == (7, 12)
    e, n = payload.centres()
    ee, nn = np.meshgrid(e, n)
    outside = (ee < off_grid.min_e) | (ee > off_grid.max_e) | (nn < off_grid.min_n) | (nn > off_grid.max_n)
    assert outside.any() and np.isnan(payload.heights[outside]).all()
    source = full[7:14, 7:19]
    assert np.array_equal(payload.heights[~outside], source[~outside], equal_nan=True)

    windows.clear()
    far = BBoxClip(0.0, 0.0, 100.0, 100.0)
    assert not _import(path, ImportSettings(clip=far)).payloads
    assert windows == []


def test_geotiff_in_another_crs_is_warped_to_the_project_crs(tmp_path):
    cell = 5.0
    corners = np.array([[LOCAL_BBOX[0], LOCAL_BBOX[1]], [LOCAL_BBOX[2], LOCAL_BBOX[3]]])
    ux, uy = KROVAK.to_crs("EPSG:32633", corners[:, 0], corners[:, 1])
    west, east = np.floor(min(ux) / cell) * cell - 60.0, np.ceil(max(ux) / cell) * cell + 60.0
    south, north = np.floor(min(uy) / cell) * cell - 60.0, np.ceil(max(uy) / cell) * cell + 60.0
    cols, rows = int((east - west) / cell), int((north - south) / cell)
    xs = west + (np.arange(cols) + 0.5) * cell
    ys = north - (np.arange(rows) + 0.5) * cell
    xx, yy = np.meshgrid(xs, ys)
    ee, nn = KROVAK.from_crs("EPSG:32633", xx.ravel(), yy.ravel())
    heights = surface(ee, nn).reshape(rows, cols)
    path = write_tif(tmp_path / "utm.tif", heights, west, north, cell, crs="EPSG:32633")

    _reader, report = inspect_source(path)
    assert report.declared_crs == "EPSG:32633"
    clip = BBoxClip(-746700.0, -1025200.0, -746500.0, -1025000.0)
    outcome = _import(path, ImportSettings(clip=clip))
    (payload,) = _payloads(outcome)
    assert outcome.dataset.transformation != "none"
    assert 4.5 < payload.cell_e < 5.5 and 4.5 < payload.cell_n < 5.5
    e, n = payload.centres()
    ee, nn = np.meshgrid(e, n)
    inside = (ee > clip.min_e + 10) & (ee < clip.max_e - 10) & (nn > clip.min_n + 10) & (nn < clip.max_n - 10)
    got = payload.heights[inside]
    assert np.isfinite(got).all()
    error = np.abs(got - surface(ee[inside], nn[inside])).max()
    assert error < 0.05
    assert np.isnan(
        payload.heights[~((ee >= clip.min_e) & (ee <= clip.max_e) & (nn >= clip.min_n) & (nn <= clip.max_n))]
    ).all()

    (coarse,) = _payloads(_import(path, ImportSettings(clip=clip, display_resolution_m=10.0)))
    assert (coarse.cell_e, coarse.cell_n) == (10.0, 10.0)
    assert coarse.origin_e % 10.0 == 0.0 and coarse.origin_n % 10.0 == 0.0
    assert np.isfinite(coarse.heights).sum() > 0.8 * (20 * 20)


def test_geotiff_nodata_edges_do_not_leak_into_the_warp(tmp_path):
    cell = 10.0
    heights = np.full((60, 60), 200.0)
    heights[20:30, 20:30] = -9999.0
    ux, uy = KROVAK.to_crs("EPSG:32633", [-746500.0], [-1025000.0])
    west, north = np.floor(ux[0] / cell) * cell - 300.0, np.ceil(uy[0] / cell) * cell + 300.0
    path = write_tif(tmp_path / "hole.tif", heights, west, north, cell, crs="EPSG:32633")
    (payload,) = _payloads(
        _import(path, ImportSettings(clip=BBoxClip(-746800.0, -1025300.0, -746200.0, -1024700.0)))
    )
    assert np.isnan(payload.heights).any()
    assert np.nanmax(payload.heights) == pytest.approx(200.0)
    assert np.nanmin(payload.heights) == pytest.approx(200.0)  # a leaked nodata cell would pull values down


def test_geotiff_that_cannot_be_read_is_a_value_error(tmp_path):
    bad = tmp_path / "bad.tif"
    bad.write_bytes(b"II*\x00" + b"\x00" * 64)
    with pytest.raises(ValueError):
        inspect_source(bad)


# --- criterion 3: LandXML TIN -


def test_landxml_surface_resolves_points_faces_and_orientation(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 50.0)
    path = tmp_path / "eg.xml"
    path.write_text(
        landxml_text([{"name": "EG", "points": tin["points"], "faces": tin["faces"], "id_offset": 100}])
    )
    _reader, report = inspect_source(path)
    assert (report.format_id, report.declared_crs) == ("landxml-surface", "EPSG:5514")
    visible = sum(1 for f in tin["faces"] if f[3])
    assert report.counts == {
        "surfaces": 1,
        "points": 81,
        "faces": len(tin["faces"]),
        "invisible_faces": len(tin["faces"]) - visible,
    }
    assert report.extent == pytest.approx(LOCAL_BBOX)
    outcome = _import(path)
    (payload,) = _payloads(outcome)
    assert isinstance(payload, TerrainTin) and outcome.dataset.objects[0].key == "eg/EG"
    assert payload.triangles.dtype == np.int32 and len(payload.triangles) == visible
    used = sorted({i for f in tin["faces"] if f[3] for i in f[:3]})
    assert len(used) == len(payload.points) == 80  # a point only hidden faces use is dropped
    assert np.abs(_sorted(payload.points) - _sorted(tin["points"][used])).max() < 1e-6
    assert (triangle_areas2(payload.points, payload.triangles) > 0.0).all()  # all counter-clockwise
    # every kept triangle is one of the visible source triangles (same three coordinates)
    source = {frozenset(map(tuple, tin["points"][list(f[:3])])) for f in tin["faces"] if f[3]}
    got = {frozenset(map(tuple, payload.points[t])) for t in payload.triangles}
    assert got == source


def test_landxml_krovak_positive_tokens_land_on_the_negative_variant(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 100.0)
    spec = [{"name": "S", "points": tin["points"], "faces": tin["faces"]}]
    negative, positive = tmp_path / "neg.xml", tmp_path / "pos.xml"
    negative.write_text(landxml_text(spec))
    positive.write_text(landxml_text(spec, krovak_positive=True))
    a, b = _payloads(_import(negative))[0], _payloads(_import(positive))[0]
    assert np.abs(a.points - b.points).max() <= 1e-6
    assert np.array_equal(a.triangles, b.triangles)
    assert np.abs(_sorted(a.points) - _sorted(tin["points"])).max() <= 1e-6
    _reader, report = inspect_source(positive)
    assert report.extent == pytest.approx(LOCAL_BBOX)


def test_landxml_other_crs_uses_the_specification_order_and_an_explicit_order_wins(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 100.0)
    e, n = KROVAK.to_crs("EPSG:32633", tin["points"][:, 0], tin["points"][:, 1])
    utm = np.column_stack([e, n, tin["points"][:, 2]])
    path = tmp_path / "utm.xml"
    path.write_text(landxml_text([{"points": utm, "faces": tin["faces"]}], epsg=32633))
    (payload,) = _payloads(_import(path))
    assert np.abs(_sorted_coarse(payload.points) - _sorted_coarse(tin["points"])).max() < 0.01
    (swapped,) = _payloads(_import(path, ImportSettings(coordinate_order="EN")))
    assert np.abs(swapped.points[:, :2] - payload.points[:, :2]).max() > 1.0  # never silent
    (explicit,) = _payloads(_import(path, ImportSettings(coordinate_order="NE")))
    assert np.array_equal(explicit.points, payload.points)

    en_path = tmp_path / "utm_en.xml"
    en_path.write_text(landxml_text([{"points": utm, "faces": tin["faces"]}], epsg=32633, order="EN"))
    (en_first,) = _payloads(_import(en_path, ImportSettings(coordinate_order="EN")))
    assert np.abs(_sorted_coarse(en_first.points) - _sorted_coarse(tin["points"])).max() < 0.01


def test_landxml_two_surfaces_make_two_objects_keyed_by_name_or_index(tmp_path):
    a, b = grid_tin(LOCAL_BBOX, 100.0), grid_tin(KRAV82_BBOX, 700.0)
    path = tmp_path / "two.xml"
    path.write_text(
        landxml_text(
            [
                {"name": "Existing", "points": a["points"], "faces": a["faces"]},
                {"points": b["points"], "faces": b["faces"]},
            ]
        )
    )
    outcome = _import(path)
    assert [r.key for r in outcome.dataset.objects] == ["two/Existing", "two/1"]
    assert inspect_source(path)[1].counts["surfaces"] == 2
    digests = {r.content_hash for r in outcome.dataset.objects}
    assert digests == {hashlib.sha256(path.read_bytes()).hexdigest()}


def test_landxml_clip_keeps_triangles_with_a_vertex_inside_and_drops_unused_points(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 50.0, clockwise_every=2, hidden_every=10**6)
    path = tmp_path / "t.xml"
    path.write_text(landxml_text([{"name": "S", "points": tin["points"], "faces": tin["faces"]}]))
    clip = BBoxClip(-746640.0, -1025140.0, -746560.0, -1025060.0)
    (payload,) = _payloads(_import(path, ImportSettings(clip=clip)))
    inside = lambda p: clip.min_e <= p[0] <= clip.max_e and clip.min_n <= p[1] <= clip.max_n  # noqa: E731
    expected = {
        frozenset(map(tuple, tin["points"][list(f[:3])]))
        for f in tin["faces"]
        if any(inside(tin["points"][i]) for i in f[:3])
    }
    assert 0 < len(expected) < len(tin["faces"])
    assert {frozenset(map(tuple, payload.points[t])) for t in payload.triangles} == expected
    assert len(payload.points) == len(np.unique(payload.triangles))
    assert (triangle_areas2(payload.points, payload.triangles) > 0.0).all()


def test_landxml_malformed_input_is_a_value_error(tmp_path):
    broken = tmp_path / "broken.xml"
    broken.write_text("<LandXML><Surfaces><Surface><Definition><Pnts><P id='1'>1 2")
    with pytest.raises(ValueError):
        inspect_source(broken, "landxml-surface")
    dtd = tmp_path / "dtd.xml"
    dtd.write_text('<!DOCTYPE LandXML [<!ENTITY x "y">]><LandXML><Surfaces><Surface/></Surfaces></LandXML>')
    with pytest.raises(ValueError):
        inspect_source(dtd, "landxml-surface")
    bad_face = tmp_path / "face.xml"
    bad_face.write_text(
        "<LandXML><Surfaces><Surface><Definition><Pnts><P id='1'>0 0 0</P><P id='2'>0 1 0</P></Pnts>"
        "<Faces><F>1 2 9</F></Faces></Definition></Surface></Surfaces></LandXML>"
    )
    with pytest.raises(ValueError, match="unknown point"):
        _import(bad_face, ImportSettings(horizontal_crs="EPSG:5514"), format_id="landxml-surface")


# --- criterion 4: ZIP, multi-file -


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_a_zip_holding_one_file_reads_like_the_bare_file(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=5)
    bare = write_las(tmp_path / "KRAV82.laz", pts, crs="EPSG:5514")
    archive = _zip(tmp_path / "KRAV82.zip", {"KRAV82.laz": bare.read_bytes(), "readme.txt": b"hello"})
    assert reader_for(archive).format_id == "las"
    from_bare, from_zip = _import(bare), _import(archive)
    assert from_zip.dataset.origin.sha256 == (hashlib.sha256(archive.read_bytes()).hexdigest(),)
    assert from_zip.dataset.origin.sha256 != from_bare.dataset.origin.sha256
    assert from_zip.dataset.objects[0].key == "KRAV82"
    assert from_zip.dataset.objects[0].content_hash == from_bare.dataset.objects[0].content_hash
    assert np.array_equal(_payloads(from_zip)[0].points, _payloads(from_bare)[0].points)
    assert inspect_source(archive)[1] == inspect_source(bare)[1]
    assert from_zip.dataset.name == "KRAV82"


def test_zips_of_a_raster_and_a_surface_read_too(tmp_path):
    tif, _grid, _heights = _surface_raster(tmp_path)
    tin = grid_tin(LOCAL_BBOX, 100.0)
    xml = tmp_path / "s.xml"
    xml.write_text(landxml_text([{"name": "S", "points": tin["points"], "faces": tin["faces"]}]))
    z_tif = _zip(tmp_path / "grid.zip", {"inner/grid.tif": tif.read_bytes()})
    z_xml = _zip(tmp_path / "surf.zip", {"s.xml": xml.read_bytes()})
    assert reader_for(z_tif).format_id == "geotiff" and reader_for(z_xml).format_id == "landxml-surface"
    assert np.array_equal(
        _payloads(_import(z_tif))[0].heights, _payloads(_import(tif))[0].heights, equal_nan=True
    )
    assert np.array_equal(_payloads(_import(z_xml))[0].points, _payloads(_import(xml))[0].points)


def test_a_zip_with_two_files_or_none_is_refused(tmp_path):
    data = write_las(
        tmp_path / "x.las", sample_irregular(LOCAL_BBOX, 50.0, seed=1), crs="EPSG:5514"
    ).read_bytes()
    two = _zip(tmp_path / "two.zip", {"a.las": data, "b.laz": data})
    with pytest.raises(ValueError, match="exactly one"):
        _import(two, format_id="las")
    none = _zip(tmp_path / "none.zip", {"notes.txt": b"x"})
    with pytest.raises(ValueError, match="holds no"):
        _import(none, format_id="las")
    with pytest.raises(ValueError):
        reader_for(two)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    with pytest.raises(ValueError):
        _import(broken, format_id="las")


def _write_sheets(tmp_path: Path, suffix: str = ".las", seed: int = 7):
    sample = sample_irregular(KRAV82_BBOX, 30.0, seed=seed)
    boxes = split_sheets(KRAV82_BBOX, 2, 2)
    sheets = [points_in_bbox(sample, box, upper_open=True) for box in boxes]
    paths = [write_las(tmp_path / f"S{i}{suffix}", sheet, crs="EPSG:5514") for i, sheet in enumerate(sheets)]
    return sample, sheets, paths


def test_four_sheets_are_one_source_with_four_objects_that_union_to_the_sample(tmp_path):
    sample, sheets, paths = _write_sheets(tmp_path)
    outcome = _import(paths)
    assert [r.key for r in outcome.dataset.objects] == ["S0", "S1", "S2", "S3"]
    assert outcome.dataset.origin.paths == tuple(str(p) for p in paths)
    assert outcome.dataset.origin.sha256 == tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in paths)
    assert outcome.dataset.name == tmp_path.name
    union = np.vstack([p.points for p in _payloads(outcome)])
    assert len(union) == len(sample) == sum(len(s) for s in sheets)
    assert np.abs(_sorted(union) - _sorted(sample)).max() <= 1e-6
    report = inspect_source(paths)[1]
    assert report.counts["points"] == len(sample) and report.counts["point_format_6"] == 4
    assert report.declared_crs is not None
    assert report.extent == pytest.approx(
        (sample[:, 0].min(), sample[:, 1].min(), sample[:, 0].max(), sample[:, 1].max()), abs=1e-6
    )


def test_a_reimport_with_one_changed_sheet_reports_one_changed_three_unchanged(tmp_path):
    sample, sheets, paths = _write_sheets(tmp_path)
    first = _import(paths)
    write_las(paths[2], sheets[2] + np.array([0.0, 0.0, 0.5]), crs="EPSG:5514")
    second = _import(paths, previous=first.dataset)
    recon = second.reconciliation
    assert (recon.changed, len(recon.unchanged), recon.added, recon.removed) == (("S2",), 3, (), ())
    assert second.dataset.id == first.dataset.id
    assert [o.id for o in second.dataset.objects] == [o.id for o in first.dataset.objects]
    assert second.dataset.objects[2].content_hash != first.dataset.objects[2].content_hash


def test_sheets_outside_the_clip_yield_no_object(tmp_path):
    _sample, _sheets, paths = _write_sheets(tmp_path)
    clip = BBoxClip(
        KRAV82_BBOX[0] + 10.0, KRAV82_BBOX[1] + 10.0, KRAV82_BBOX[0] + 300.0, KRAV82_BBOX[1] + 300.0
    )
    outcome = _import(paths, ImportSettings(clip=clip))
    assert [r.key for r in outcome.dataset.objects] == ["S0"]
    for sheet in ("S1", "S2", "S3"):
        assert any(w.startswith(f"{sheet}: no points inside the clip region") for w in outcome.warnings)


def test_mixed_formats_and_bad_path_lists_are_value_errors(tmp_path):
    _sample, _sheets, paths = _write_sheets(tmp_path)
    tif, _grid, _heights = _surface_raster(tmp_path)
    with pytest.raises(ValueError, match="different formats"):
        _import([paths[0], tif])
    with pytest.raises(ValueError, match="different formats"):
        inspect_source([paths[0], tif])
    with pytest.raises(ValueError, match="twice"):
        _import([paths[0], paths[0]])
    with pytest.raises(ValueError, match="no path"):
        _import([])


def test_files_declaring_different_crs_are_warned_about_and_none_is_assumed(tmp_path):
    pts = sample_irregular(LOCAL_BBOX, 50.0, seed=9)
    a = write_las(tmp_path / "a.las", pts, crs="EPSG:5514")
    b = write_las(tmp_path / "b.las", pts, crs="EPSG:32633")
    _reader, report = inspect_source([a, b])
    assert report.declared_crs is None and report.suggested.horizontal_crs is None
    assert any("different coordinate reference systems" in w for w in report.warnings)
    with pytest.raises(CrsRequired):
        _import([a, b])
    c = write_las(tmp_path / "c.las", pts)
    _reader, mixed = inspect_source([a, c])
    assert mixed.declared_crs is None
    assert any("different coordinate reference systems" in w and "none" in w for w in mixed.warnings)


# --- criterion 5: clip -


def test_a_corridor_clip_over_the_sheets_keeps_exactly_the_points_an_independent_check_keeps(
    tmp_path, kralupy
):
    sample = sample_irregular(CORRIDOR_BOX, 6.0, seed=3)
    boxes = split_sheets(CORRIDOR_BOX, 2, 2)
    paths = [
        write_las(tmp_path / f"C{i}.laz", points_in_bbox(sample, box, upper_open=True), crs="EPSG:5514")
        for i, box in enumerate(boxes)
    ]
    options = EnvelopeOptions(60.0, station_from=14100.0, station_to=14600.0)
    polygon = corridor_envelope([kralupy.alignment], options)
    expected = _inside_even_odd(polygon, sample)
    assert 0 < expected.sum() < len(sample)
    clip = CorridorClip(buffer_m=60.0, station_from=14100.0, station_to=14600.0)
    outcome = _import(paths, ImportSettings(clip=clip), alignments={"aln": kralupy.alignment})
    kept = np.vstack([p.points for p in _payloads(outcome)])
    assert len(kept) == expected.sum()
    assert np.abs(_sorted(kept) - _sorted(sample[expected])).max() <= 1e-6


# --- sniffing and registration -


def test_readers_are_registered_once_and_sniff_by_content(tmp_path):
    register_builtin_readers()
    assert sorted(r.format_id for r in registry.readers()) == ["geotiff", "landxml-surface", "las"]
    odd = tmp_path / "points.dat"
    odd.write_bytes(write_las(tmp_path / "p.las", sample_irregular(LOCAL_BBOX, 50.0, seed=1)).read_bytes())
    assert reader_for(odd).format_id == "las"
    plain = tmp_path / "alignment.xml"
    plain.write_text('<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2"><Alignments/></LandXML>')
    with pytest.raises(ValueError, match="no registered reader"):
        reader_for(plain)
    text = tmp_path / "notes.txt"
    text.write_text("nothing here")
    with pytest.raises(ValueError, match="no registered reader"):
        reader_for(text)


def test_the_payload_types_validate_their_shapes():
    with pytest.raises(ValueError):
        TerrainPoints(np.zeros((3, 2)), None)
    with pytest.raises(ValueError):
        TerrainPoints(np.zeros((3, 3)), np.zeros(2, dtype=np.uint8))
    with pytest.raises(ValueError):
        TerrainGrid(0.0, 0.0, 0.0, 1.0, np.zeros((2, 2)))
    with pytest.raises(ValueError):
        TerrainTin(np.zeros((3, 3)), np.array([[0, 1, 5]], dtype=np.int32))
    grid = TerrainGrid(10.0, 20.0, 2.0, 4.0, np.zeros((3, 5)))
    assert grid.bounds == (10.0, 8.0, 20.0, 20.0)
    e, n = grid.centres()
    assert e[0] == 11.0 and n[0] == 18.0 and n[-1] == 10.0


# --- review round: hardening -


def _extractions(monkeypatch) -> list[str]:
    """Record every ZIP member extraction."""
    calls: list[str] = []
    original = archive_module._extract

    def counting(archive, member, target, path):
        calls.append(member.filename)
        return original(archive, member, target, path)

    monkeypatch.setattr(archive_module, "_extract", counting)
    return calls


def test_a_zip_member_is_extracted_once_per_inspect_and_once_per_import(tmp_path, monkeypatch):
    bare = write_las(tmp_path / "A.laz", sample_irregular(LOCAL_BBOX, 20.0, seed=1), crs="EPSG:5514")
    archive = _zip(tmp_path / "A.zip", {"A.laz": bare.read_bytes()})
    tin = grid_tin(LOCAL_BBOX, 100.0)
    xml_zip = _zip(
        tmp_path / "S.zip",
        {"S.xml": landxml_text([{"name": "S", "points": tin["points"], "faces": tin["faces"]}]).encode()},
    )
    calls = _extractions(monkeypatch)
    assert reader_for(archive).format_id == "las" and reader_for(xml_zip).format_id == "landxml-surface"
    assert calls == []  # sniffing reads a bounded head, it never extracts
    inspect_source(archive)
    assert calls == ["A.laz"]
    _import(archive)  # inspect, hash and read share one extraction
    assert calls == ["A.laz", "A.laz"]
    _import(xml_zip)
    assert calls == ["A.laz", "A.laz", "S.xml"]


def test_extraction_is_removed_when_the_call_ends(tmp_path, monkeypatch):
    bare = write_las(tmp_path / "A.laz", sample_irregular(LOCAL_BBOX, 20.0, seed=1), crs="EPSG:5514")
    archive = _zip(tmp_path / "A.zip", {"A.laz": bare.read_bytes()})
    folders: list[Path] = []
    original = archive_module._extract

    def spy(archive_file, member, target, path):
        folders.append(target.parent)
        assert target.parent.exists()
        return original(archive_file, member, target, path)

    monkeypatch.setattr(archive_module, "_extract", spy)
    _import(archive)
    inspect_source(archive)
    assert len(folders) == 2 and not any(f.exists() or f.parent.exists() for f in folders)


def test_a_high_ratio_member_is_refused_as_a_zip_bomb(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_module, "RATIO_ALLOWANCE_BYTES", 1 << 20)
    bomb = _zip(tmp_path / "bomb.zip", {"zeros.las": bytes(16 << 20)})  # 16 MiB of zeros, about 16 KiB packed
    calls = _extractions(monkeypatch)
    with pytest.raises(ValueError, match="ZIP bomb"):
        _import(bomb, format_id="las")
    with pytest.raises(ValueError, match="ZIP bomb"):
        inspect_source(bomb, "las")
    assert calls == []
    data = write_las(tmp_path / "x.las", sample_irregular(LOCAL_BBOX, 50.0, seed=1)).read_bytes()
    honest = _zip(tmp_path / "ok.zip", {"x.las": data})
    assert _import(honest, ImportSettings(horizontal_crs="EPSG:5514"), format_id="las").payloads


def test_an_oversized_declared_member_is_refused_before_anything_is_written(tmp_path, monkeypatch):
    data = write_las(tmp_path / "x.las", sample_irregular(LOCAL_BBOX, 50.0, seed=1)).read_bytes()
    archive = _zip(tmp_path / "x.zip", {"x.las": data})
    monkeypatch.setattr(archive_module, "MAX_MEMBER_BYTES", len(data) - 1)
    calls = _extractions(monkeypatch)
    with pytest.raises(ValueError, match="declares"):
        _import(archive, format_id="las")
    assert calls == []


def test_a_member_that_streams_more_than_it_declares_is_refused(tmp_path):
    import io

    class Archive:
        def open(self, member):
            return io.BytesIO(bytes(5000))

    member = zipfile.ZipInfo("x.las")
    member.file_size = 1000
    with pytest.raises(ValueError, match="more than the 1000 bytes"):
        archive_module._extract(Archive(), member, tmp_path / "out.bin", tmp_path / "x.zip")


def test_an_encrypted_member_is_refused(tmp_path):
    path = _zip(tmp_path / "enc.zip", {"secret.las": b"LASF" + bytes(100)})
    raw = bytearray(path.read_bytes())
    raw[6] |= 0x1  # general-purpose bit 0 in the local header ...
    raw[raw.rindex(b"PK") + 8] |= 0x1  # ... and in the central directory
    path.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="encrypted"):
        _import(path, format_id="las")
    with pytest.raises(ValueError, match="encrypted"):
        reader_for(path, "las").inspect(path)


def test_geotiff_refusals(tmp_path):
    cell = 20.0
    heights = sample_grid(LOCAL_BBOX, cell).z[::-1].copy()
    settings = ImportSettings(horizontal_crs="EPSG:5514")

    def write(name, transform, crs="EPSG:5514"):
        path = tmp_path / name
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=heights.shape[0],
            width=heights.shape[1],
            count=1,
            dtype="float64",
            crs=crs,
            transform=transform,
        ) as ds:
            ds.write(heights, 1)
        return path

    rotated = write("rot.tif", Affine(cell, 3.0, LOCAL_BBOX[0], 3.0, -cell, LOCAL_BBOX[3]))
    with pytest.raises(ValueError, match="north-up"):
        _import(rotated, settings)
    assert any("rotated" in w for w in inspect_source(rotated)[1].warnings)
    south_up = write("flip.tif", Affine(cell, 0.0, LOCAL_BBOX[0], 0.0, cell, LOCAL_BBOX[1]))
    with pytest.raises(ValueError, match="north-up"):
        _import(south_up, settings)
    positive = write("pos.tif", Affine(cell, 0.0, 746000.0, 0.0, -cell, 1025000.0), crs="EPSG:5513")
    with pytest.raises(ValueError, match="positive Krovak"):
        _import(positive, ImportSettings(horizontal_crs="EPSG:5513"))
    plain, _grid, _heights = _surface_raster(tmp_path)
    with pytest.raises(ValueError, match="coordinate_order 'NE'"):
        _import(plain, ImportSettings(coordinate_order="NE"))
    (ok,) = _payloads(_import(plain, ImportSettings(coordinate_order="EN")))
    assert ok.heights.shape == (21, 21)


def test_geotiff_refuses_more_cells_than_the_limit(tmp_path, monkeypatch):
    plain, _grid, _heights = _surface_raster(tmp_path)
    utm_grid, _g, _h = _surface_raster(tmp_path, name="other.tif")
    monkeypatch.setattr(raster_module, "MAX_CELLS", 100)
    with pytest.raises(ValueError, match="set a clip"):
        _import(plain)
    small = BBoxClip(-746650.0, -1025150.0, -746450.0, -1025050.0)  # 10 x 5 = 50 cells
    assert _import(plain, ImportSettings(clip=small)).payloads  # the window asked for is what counts
    monkeypatch.setattr(raster_module, "MAX_CELLS", 10)
    with pytest.raises(ValueError, match="set a clip"):  # the resampled grid is counted too
        _import(utm_grid, ImportSettings(clip=small, horizontal_crs="EPSG:5513"))


@pytest.mark.parametrize("declared", ["EPSG:5514", "EPSG:5513"])
def test_a_positive_krovak_las_lands_on_the_east_north_truth(tmp_path, declared):
    pts = sample_irregular(LOCAL_BBOX, 20.0, seed=6)
    positive = np.column_stack([-pts[:, 0], -pts[:, 1], pts[:, 2]])  # x = Y (west), y = X (south), both > 0
    assert positive[:, :2].min() > 0
    path = write_las(tmp_path / "pos.laz", positive)
    (payload,) = _payloads(_import(path, ImportSettings(horizontal_crs=declared)))
    assert np.abs(_sorted(payload.points) - _sorted(pts)).max() <= 1e-6
    east_north = write_las(tmp_path / "neg.laz", pts)
    (truth,) = _payloads(_import(east_north, ImportSettings(horizontal_crs=declared)))
    assert np.abs(_sorted(truth.points) - _sorted(pts)).max() <= 1e-6


def test_two_files_with_a_surface_of_the_same_name_make_unique_keys(tmp_path):
    a, b = grid_tin(LOCAL_BBOX, 100.0), grid_tin(KRAV82_BBOX, 700.0)
    first, second = tmp_path / "north.xml", tmp_path / "south.xml"
    first.write_text(landxml_text([{"name": "EG", "points": a["points"], "faces": a["faces"]}]))
    second.write_text(landxml_text([{"name": "EG", "points": b["points"], "faces": b["faces"]}]))
    outcome = _import([first, second])
    assert [r.key for r in outcome.dataset.objects] == ["north/EG", "south/EG"]
    again = _import([first, second], previous=outcome.dataset)
    assert [o.id for o in again.dataset.objects] == [o.id for o in outcome.dataset.objects]
    assert len(again.reconciliation.unchanged) == 2


def test_landxml_elements_with_the_wrong_number_of_values_are_refused(tmp_path):
    def document(points: list[str], faces: str = "<F>1 2 3</F>") -> str:
        body = "".join(f'<P id="{i + 1}">{text}</P>' for i, text in enumerate(points))
        return (
            "<LandXML><Surfaces><Surface name='S'><Definition><Pnts>"
            f"{body}</Pnts><Faces>{faces}</Faces></Definition></Surface></Surfaces></LandXML>"
        )

    settings = ImportSettings(horizontal_crs="EPSG:5514")
    mixed = tmp_path / "mixed.xml"  # 2 + 4 + 3 values: the total still divides by three
    mixed.write_text(document(["1 2", "3 4 5 6", "7 8 9"]))
    with pytest.raises(ValueError, match="exactly three numbers"):
        _import(mixed, settings, format_id="landxml-surface")
    faces = tmp_path / "faces.xml"  # 2 + 4 ids: the total divides by three
    faces.write_text(document(["1 2 3", "4 5 6", "7 8 9", "1 1 1"], faces="<F>1 2</F><F>3 4 1 2</F>"))
    with pytest.raises(ValueError, match="exactly three point ids"):
        _import(faces, settings, format_id="landxml-surface")


def test_landxml_crs_declaration_takes_the_first_epsg_integer_and_survives_nonsense(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 100.0)
    base = landxml_text([{"name": "S", "points": tin["points"], "faces": tin["faces"]}], epsg=None)
    both = tmp_path / "both.xml"
    both.write_text(
        base.replace("<Surfaces>", '<CoordinateSystem desc="EPSG:5514 + EPSG:5705" name="x"/><Surfaces>')
    )
    assert inspect_source(both)[1].declared_crs == "EPSG:5514"
    bad = tmp_path / "bad.xml"
    bad.write_text(base.replace("<Surfaces>", '<CoordinateSystem epsgCode="99999999"/><Surfaces>'))
    _reader, report = inspect_source(bad)
    assert report.declared_crs is None and any("99999999" in w for w in report.warnings)
    with pytest.raises(CrsRequired):
        _import(bad)
    (rescued,) = _payloads(_import(bad, ImportSettings(horizontal_crs="EPSG:5514")))
    assert len(rescued.triangles)


def test_geotiff_inspect_reports_the_cell_size(tmp_path):
    plain, _grid, _heights = _surface_raster(tmp_path)
    assert "resolution: cell size 20 x 20 (source units)" in inspect_source(plain)[1].warnings


def test_a_surface_emptied_by_the_clip_is_explained(tmp_path):
    tin = grid_tin(LOCAL_BBOX, 100.0)
    path = tmp_path / "t.xml"
    path.write_text(landxml_text([{"name": "S", "points": tin["points"], "faces": tin["faces"]}]))
    outcome = _import(path, ImportSettings(clip=BBoxClip(0.0, 0.0, 10.0, 10.0)))
    assert not outcome.payloads
    assert any(w.startswith("t/S: no triangle has a vertex inside the clip region") for w in outcome.warnings)
    assert any("the import produced no objects" in w for w in outcome.warnings)


def test_a_raster_emptied_by_the_clip_or_by_nodata_is_explained(tmp_path):
    plain, _grid, _heights = _surface_raster(tmp_path)
    outside = _import(plain, ImportSettings(clip=BBoxClip(0.0, 0.0, 10.0, 10.0)))
    assert any(w.startswith("grid: the raster does not overlap the clip region") for w in outside.warnings)
    sliver = BBoxClip(-746800.0, -1025300.0, -746799.0, -1025299.0)  # the corner cell, centre outside it
    empty = _import(plain, ImportSettings(clip=sliver))
    assert not empty.payloads
    assert any(w.startswith("grid: no valid heights") for w in empty.warnings)


# --- the wire -


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None) -> Envelope:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())[0]


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def test_paths_over_the_wire(server_url, tmp_path):
    sample, _sheets, paths = _write_sheets(tmp_path)
    tif, _grid, _heights = _surface_raster(tmp_path)
    names = [str(p) for p in paths]
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        assert (await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})).type == "res"

        inspected = await _call(ws, "1", "import.inspect", {"paths": names})
        assert inspected.type == "res", inspected.error
        assert inspected.result["format_id"] == "las"
        assert inspected.result["counts"]["points"] == len(sample)
        assert inspected.result["declared_crs"] is not None

        single = await _call(ws, "2", "import.inspect", {"path": names[0]})
        assert single.type == "res" and single.result["counts"]["points"] < len(sample)

        for number, params in enumerate(
            (
                {},
                {"path": names[0], "paths": names},
                {"paths": []},
                {"paths": [names[0], names[0]]},
                {"paths": [names[0], str(tif)]},
                {"paths": [names[0], "relative.las"]},
            )
        ):
            reply = await _call(ws, f"b{number}", "import.inspect", params)
            assert (reply.type, reply.error.code) == ("err", "E_BAD_PARAMS"), params
        missing = await _call(ws, "m", "import.inspect", {"paths": [names[0], str(tmp_path / "nope.las")]})
        assert missing.error.code == "E_NOT_FOUND"

        settings = {"horizontal_crs": "EPSG:5514"}
        ran = await _call(ws, "3", "import.run", {"paths": names, "kind": "terrain", "settings": settings})
        assert ran.type == "res", ran.error
        assert (ran.result["added"], ran.result["changed"], ran.result["unchanged"]) == (4, 0, 0)
        got = await _call(ws, "4", "source.get", {"source_id": ran.result["source_id"]})
        assert got.result["origin"]["paths"] == names
        assert [o["key"] for o in got.result["objects"]] == ["S0", "S1", "S2", "S3"]

        again = await _call(
            ws,
            "5",
            "import.run",
            {"paths": names, "kind": "terrain", "settings": settings, "reimport_of": ran.result["source_id"]},
        )
        assert (again.result["added"], again.result["changed"], again.result["unchanged"]) == (0, 0, 4)

        mixed = await _call(
            ws, "6", "import.run", {"paths": [names[0], str(tif)], "kind": "terrain", "settings": settings}
        )
        assert (mixed.type, mixed.error.code) == ("err", "E_BAD_PARAMS")
        neither = await _call(ws, "7", "import.run", {"kind": "terrain", "settings": settings})
        assert neither.error.code == "E_BAD_PARAMS"
