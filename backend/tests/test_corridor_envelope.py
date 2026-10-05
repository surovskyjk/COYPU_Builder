"""T-138: corridor envelope geometry, GeoJSON and Shapefile files, the CLI and `alignment.envelope`."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
import shapefile
import shapely
from pyproj import CRS
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.__main__ import main
from coypu_builder.domain.corridor import plan_centreline
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.geometry import Alignment, CircularArc, HorizontalAlignment, Line
from coypu_builder.domain.lrs import frames
from coypu_builder.domain.sampling import bake_stations
from coypu_builder.io.gis.envelope import (
    EnvelopeOptions,
    GisExtraMissing,
    corridor_envelope,
    export_corridor_envelope,
    write_envelope,
)
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame

KROVAK = ProjectCRS("EPSG:5514")
FIELDS = ["name", "buffer_m", "sta_from", "sta_to", "crs_src", "created", "tool"]


def _straight(length: float) -> Alignment:
    return Alignment.flat(HorizontalAlignment((Line(0.0, 0.0, 0.0, length),)), name="straight")


def _arc(radius: float, sweep: float) -> Alignment:
    return Alignment.flat(
        HorizontalAlignment((CircularArc(0.0, 0.0, 0.0, radius * sweep, 1.0 / radius),)), name="arc"
    )


def _signed_area(coords) -> float:
    pts = np.asarray(coords, dtype=np.float64)
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _tram_loop(tram_alignments) -> Alignment:
    return next(a for a in tram_alignments.values() if a.name == "terminal_loop")


def _kralupy_annulus(kralupy, tram_alignments, b: float = 10.0):
    """The tram-loop envelope (one hole) moved onto the Kralupy corridor, so it can be reprojected."""
    geometry = corridor_envelope([_tram_loop(tram_alignments)], EnvelopeOptions(buffer_m=b))
    anchor = kralupy.alignment.horizontal.point([9000.0])[0]
    return translate(geometry, xoff=float(anchor[0]), yoff=float(anchor[1]))


# --- domain: plan_centreline -----------------------------------------------------------------------------


def test_plan_centreline_includes_both_range_ends_exactly(kralupy):
    aln = kralupy.alignment
    pts = plan_centreline(aln, 12720.0, 17545.0)
    ends = aln.horizontal.point(np.array([12720.0, 17545.0]))
    assert pts.dtype == np.float64 and pts.shape[1] == 2
    assert np.array_equal(pts[0], ends[0]) and np.array_equal(pts[-1], ends[1])


def test_plan_centreline_defaults_to_the_whole_alignment(kralupy):
    aln = kralupy.alignment
    pts = plan_centreline(aln)
    ends = aln.horizontal.point(np.array([aln.station_start, aln.station_end]))
    assert np.array_equal(pts[0], ends[0]) and np.array_equal(pts[-1], ends[1])


def test_plan_centreline_deviates_from_frames_by_at_most_the_chord_error(kralupy):
    aln = kralupy.alignment
    err = 0.05
    for s_from, s_to in ((None, None), (3000.0, 9000.0)):
        pts = plan_centreline(aln, s_from, s_to, max_chord_error_m=err)
        line = LineString(pts)
        stations = bake_stations(aln, spacing_m=100.0, max_chord_error_m=err)
        a = aln.station_start if s_from is None else s_from
        b = aln.station_end if s_to is None else s_to
        stations = np.concatenate([[a], stations[(stations > a) & (stations < b)], [b]])
        mids = (stations[:-1] + stations[1:]) / 2.0
        origin = frames(aln, mids).origin[:, :2]
        deviation = shapely.distance(line, shapely.points(origin))
        assert float(deviation.max()) <= err + 1e-9


@pytest.mark.parametrize(
    "s_from, s_to",
    [(-1.0, 100.0), (0.0, 1e9), (500.0, 500.0), (600.0, 500.0)],
)
def test_plan_centreline_rejects_bad_ranges(kralupy, s_from, s_to):
    with pytest.raises(ValueError):
        plan_centreline(kralupy.alignment, s_from, s_to)


def test_domain_corridor_module_does_not_import_shapely():
    import coypu_builder.domain.corridor as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "shapely" not in source and "import pyproj" not in source


# --- geometry --------------------------------------------------------------------------------------------


def test_straight_flat_caps_is_the_rectangle():
    length, b = 1000.0, 40.0
    geometry = corridor_envelope([_straight(length)], EnvelopeOptions(buffer_m=b, cap="flat"))
    assert geometry.is_valid and geometry.geom_type == "Polygon"
    assert geometry.area == pytest.approx(2.0 * b * length, rel=1e-6)
    assert geometry.bounds == pytest.approx((0.0, -b, length, b))


def test_arc_flat_caps_area_is_two_b_l_and_centreline_stays_inside():
    radius, b = 300.0, 100.0
    aln = _arc(radius, np.pi / 2.0)
    options = EnvelopeOptions(buffer_m=b, cap="flat")
    geometry = corridor_envelope([aln], options)
    assert geometry.is_valid
    assert geometry.area == pytest.approx(2.0 * b * aln.length, rel=1e-3)

    pts = plan_centreline(aln)
    stations = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
    interior = pts[(stations >= b) & (stations <= aln.length - b)]
    assert len(interior) > 10
    boundary = geometry.boundary
    for p in interior:
        point = Point(p)
        assert geometry.contains(point)
        assert boundary.distance(point) >= b - options.simplify_m - 0.05


@pytest.mark.parametrize("b, holes", [(10.0, 1), (50.0, 0)])
def test_tram_loop_hole_depends_on_buffer_width(tram_alignments, b, holes):
    loop = _tram_loop(tram_alignments)
    geometry = corridor_envelope([loop], EnvelopeOptions(buffer_m=b))
    assert geometry.is_valid and geometry.geom_type == "Polygon"
    assert len(geometry.interiors) == holes
    assert geometry.area < 2.0 * b * loop.length + np.pi * b * b
    if holes:
        centre = loop.horizontal.point([20.0 + np.pi * 30.0])[0]
        centre = (centre + loop.horizontal.point([20.0])[0]) / 2.0
        assert Polygon(geometry.interiors[0]).contains(Point(centre))


def test_tram_network_envelope_is_the_union_of_both_alignments(tram_alignments):
    geometry = corridor_envelope(list(tram_alignments.values()), EnvelopeOptions(buffer_m=15.0))
    parts = [corridor_envelope([a], EnvelopeOptions(buffer_m=15.0)) for a in tram_alignments.values()]
    assert geometry.is_valid
    assert geometry.area >= max(p.area for p in parts) - 1.0
    assert geometry.area <= sum(p.area for p in parts) + 1.0


def test_station_range_needs_exactly_one_alignment(tram_alignments):
    with pytest.raises(ValueError, match="exactly one"):
        corridor_envelope(list(tram_alignments.values()), EnvelopeOptions(buffer_m=5.0, station_from=1.0))


@pytest.mark.parametrize("kwargs", [{"buffer_m": 0.0}, {"buffer_m": -1.0}, {"buffer_m": 5.0, "cap": "x"}])
def test_options_validate(kwargs):
    with pytest.raises(ValueError):
        EnvelopeOptions(**kwargs)


def test_kralupy_round_caps_area_and_vertex_count(kralupy, tmp_path):
    aln = kralupy.alignment
    b = 250.0
    geometry = corridor_envelope([aln], EnvelopeOptions(buffer_m=b))
    assert geometry.is_valid
    expected = 2.0 * b * aln.length + np.pi * b * b
    assert geometry.area == pytest.approx(expected, rel=0.005)
    result = write_envelope(geometry, tmp_path / "k.geojson", KROVAK, None, {"name": "x"})
    assert 0 < result.vertex_count < 2000
    assert result.area_m2 == pytest.approx(geometry.area)


def test_kralupy_sub_range_flat_caps_cuts_at_the_range(kralupy):
    aln = kralupy.alignment
    geometry = corridor_envelope(
        [aln], EnvelopeOptions(buffer_m=100.0, station_from=12720.0, station_to=17545.0, cap="flat")
    )
    point = lambda s: Point(aln.horizontal.point([s])[0])  # noqa: E731
    assert geometry.contains(point(12730.0)) and geometry.contains(point(17535.0))
    assert not geometry.contains(point(12700.0)) and not geometry.contains(point(17565.0))


# --- GeoJSON ---------------------------------------------------------------------------------------------


def _read_geojson(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_geojson_kralupy_is_rfc7946(kralupy, tmp_path):
    aln = kralupy.alignment
    options = EnvelopeOptions(buffer_m=250.0)
    out = tmp_path / "kralupy.geojson"
    result = export_corridor_envelope([aln], options, KROVAK, out)
    assert result.epsg == 4326 and result.files == (out,)

    doc = _read_geojson(out)
    assert doc["type"] == "FeatureCollection" and "crs" not in doc
    assert len(doc["features"]) == 1
    feature = doc["features"][0]
    assert feature["type"] == "Feature" and feature["geometry"]["type"] == "Polygon"
    rings = feature["geometry"]["coordinates"]
    assert len(rings) == 1
    ring = np.asarray(rings[0])
    assert np.array_equal(ring[0], ring[-1])
    assert ring[:, 0].min() >= 12 and ring[:, 0].max() <= 19
    assert ring[:, 1].min() >= 48.5 and ring[:, 1].max() <= 51.1
    assert _signed_area(ring) > 0
    assert result.bounds == pytest.approx((*ring.min(axis=0), *ring.max(axis=0)))
    assert result.vertex_count == len(ring)

    props = feature["properties"]
    assert list(props) == FIELDS
    assert props["name"] == "Track 1" and props["buffer_m"] == 250.0
    assert props["sta_from"] is None and props["sta_to"] is None
    assert props["crs_src"] == "EPSG:5514" and props["tool"].startswith("coypu-builder ")
    assert datetime.fromisoformat(props["created"]).utcoffset().total_seconds() == 0

    expected = corridor_envelope([aln], options)
    x, y = ProjectCRS("EPSG:4326").to_crs(KROVAK.crs, ring[:, 0], ring[:, 1])
    distances = shapely.distance(expected.boundary, shapely.points(np.column_stack([x, y])))
    assert float(distances.max()) < 0.01


def test_geojson_hole_winding_is_clockwise(kralupy, tram_alignments, tmp_path):
    geometry = _kralupy_annulus(kralupy, tram_alignments)
    assert len(geometry.interiors) == 1
    out = tmp_path / "annulus.geojson"
    write_envelope(geometry, out, KROVAK, None, {"name": "annulus"})
    rings = _read_geojson(out)["features"][0]["geometry"]["coordinates"]
    assert len(rings) == 2
    assert _signed_area(rings[0]) > 0
    assert _signed_area(rings[1]) < 0


def test_geojson_other_epsg_writes_legacy_crs_member(kralupy, tmp_path):
    out = tmp_path / "utm.geojson"
    result = export_corridor_envelope(
        [kralupy.alignment], EnvelopeOptions(buffer_m=100.0), KROVAK, out, 32633
    )
    assert result.epsg == 32633
    doc = _read_geojson(out)
    assert doc["crs"] == {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32633"}}
    ring = np.asarray(doc["features"][0]["geometry"]["coordinates"][0])
    assert 100_000 < ring[:, 0].min() and ring[:, 0].max() < 900_000
    assert _signed_area(ring) > 0


def test_geojson_sub_range_attributes(kralupy, tmp_path):
    out = tmp_path / "sub.json"
    options = EnvelopeOptions(buffer_m=100.0, station_from=12720.0, station_to=17545.0, cap="flat")
    export_corridor_envelope([kralupy.alignment], options, KROVAK, out)
    props = _read_geojson(out)["features"][0]["properties"]
    assert props["sta_from"] == 12720.0 and props["sta_to"] == 17545.0


def test_write_creates_missing_parent_directories(kralupy, tmp_path):
    out = tmp_path / "a" / "b" / "k.geojson"
    export_corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=100.0), KROVAK, out)
    assert out.is_file()


def test_write_rejects_unknown_suffix(kralupy, tmp_path):
    geometry = corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=100.0))
    with pytest.raises(ValueError, match="unsupported"):
        write_envelope(geometry, tmp_path / "k.kml", KROVAK, None, {})


# --- Shapefile -------------------------------------------------------------------------------------------


def _read_shapefile(path: Path):
    reader = shapefile.Reader(str(path), encoding="utf-8")
    return reader


def _part_rings(shape) -> list[list[tuple[float, float]]]:
    points = list(shape.points)
    starts = list(shape.parts) + [len(points)]
    return [points[a:b] for a, b in zip(starts[:-1], starts[1:], strict=True)]


def test_shapefile_kralupy_layout_attributes_and_prj(kralupy, tmp_path):
    out = tmp_path / "kralupy.shp"
    options = EnvelopeOptions(buffer_m=250.0)
    result = export_corridor_envelope([kralupy.alignment], options, KROVAK, out)
    assert result.epsg == 5514
    assert [f.suffix for f in result.files] == [".shp", ".shx", ".dbf", ".prj", ".cpg"]
    assert all(f.is_file() for f in result.files)

    reader = _read_shapefile(out)
    assert reader.shapeTypeName == "POLYGON" and len(reader) == 1
    assert [f.name for f in reader.fields if f.name != "DeletionFlag"] == FIELDS
    record = reader.record(0).as_dict()
    assert record["name"] == "Track 1" and record["buffer_m"] == 250.0
    assert record["sta_from"] is None and record["sta_to"] is None
    assert record["crs_src"] == "EPSG:5514" and record["tool"].startswith("coypu-builder ")

    rings = _part_rings(reader.shape(0))
    assert len(rings) == 1
    assert _signed_area(rings[0]) < 0
    assert result.vertex_count == len(rings[0])
    assert result.bounds == pytest.approx(tuple(reader.shape(0).bbox))

    assert CRS.from_wkt(out.with_suffix(".prj").read_text(encoding="utf-8")) == CRS.from_epsg(5514)
    assert out.with_suffix(".cpg").read_text(encoding="ascii").strip() == "UTF-8"

    expected = corridor_envelope([kralupy.alignment], options)
    assert Polygon(rings[0]).symmetric_difference(expected).area < 0.001 * expected.area


def test_shapefile_hole_winding_is_counter_clockwise(tram_alignments, tmp_path):
    out = tmp_path / "loop.shp"
    geometry = corridor_envelope([_tram_loop(tram_alignments)], EnvelopeOptions(buffer_m=10.0))
    assert len(geometry.interiors) == 1
    write_envelope(geometry, out, KROVAK, None, {"name": "loop"})
    rings = _part_rings(_read_shapefile(out).shape(0))
    assert len(rings) == 2
    assert _signed_area(rings[0]) < 0
    assert _signed_area(rings[1]) > 0


def test_shapefile_in_another_epsg_gets_its_own_prj(kralupy, tmp_path):
    out = tmp_path / "wgs.shp"
    result = export_corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=100.0), KROVAK, out, 4326)
    assert result.epsg == 4326
    assert CRS.from_wkt(out.with_suffix(".prj").read_text(encoding="utf-8")).to_epsg() == 4326
    ring = _part_rings(_read_shapefile(out).shape(0))[0]
    assert all(12 <= x <= 19 and 48.5 <= y <= 51.1 for x, y in ring)
    assert _signed_area(ring) < 0


# --- CLI -------------------------------------------------------------------------------------------------


def test_cli_writes_geojson_and_prints_summary(kralupy_xml, tmp_path, capsys):
    out = tmp_path / "cli.geojson"
    code = main(["envelope", str(kralupy_xml), "--buffer", "250", "-o", str(out)])
    text = capsys.readouterr().out
    assert code == 0 and out.is_file()
    assert "Track 1" in text and "km2" in text and "vertices:" in text and "EPSG:4326" in text
    assert str(out) in text


def test_cli_station_range_and_alignment_selection(kralupy_xml, tmp_path, capsys):
    out = tmp_path / "sub.shp"
    args = ["envelope", str(kralupy_xml), "-o", str(out), "--alignment", "Track 1"]
    args += ["--from", "12720", "--to", "17545", "--cap", "flat", "--buffer", "100"]
    assert main(args) == 0
    assert "12720.000 -> 17545.000" in capsys.readouterr().out
    assert _read_shapefile(out).record(0).as_dict()["sta_from"] == 12720.0


def test_cli_bad_input_exits_non_zero_with_one_line(kralupy_xml, tmp_path, capsys):
    out = tmp_path / "x.geojson"
    cases = [
        ["envelope", str(kralupy_xml), "-o", str(out), "--alignment", "nope"],
        ["envelope", str(kralupy_xml), "-o", str(out), "--buffer", "0"],
        ["envelope", str(kralupy_xml), "-o", str(tmp_path / "x.kml")],
        ["envelope", str(tmp_path / "missing.xml"), "-o", str(out)],
        ["envelope", str(kralupy_xml), "-o", str(out), "--from", "-5"],
    ]
    for args in cases:
        assert main(args) != 0
        err = capsys.readouterr().err
        assert err.startswith("error: ") and len(err.strip().splitlines()) == 1
    assert not out.exists()


# --- optional dependency ---------------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["shapely", "shapefile"])
def test_missing_gis_extra_names_the_install_command(kralupy_xml, tmp_path, capsys, monkeypatch, missing):
    monkeypatch.setitem(sys.modules, missing, None)
    out = tmp_path / ("x.shp" if missing == "shapefile" else "x.geojson")
    assert main(["envelope", str(kralupy_xml), "-o", str(out)]) != 0
    err = capsys.readouterr().err
    assert "uv sync --extra gis" in err and len(err.strip().splitlines()) == 1


def test_missing_shapely_raises_the_dedicated_error(kralupy, monkeypatch):
    monkeypatch.setitem(sys.modules, "shapely", None)
    with pytest.raises(GisExtraMissing, match="uv sync --extra gis"):
        corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=100.0))


# --- alignment.envelope over a live server ---------------------------------------------------------------


async def _call(ws, request_id: str, method: str, params: dict | None = None):
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    reply, _tail = decode_frame(await ws.recv())
    return reply


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _open_project(ws, kralupy_xml) -> str:
    assert (await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})).type == "res"
    assert (await _call(ws, "2", "project.new", {})).type == "res"
    imported = await _call(ws, "3", "import.landxml", {"path": str(kralupy_xml)})
    assert imported.type == "res", imported.error
    return imported.result["alignments"][0]["alignment_id"]


async def test_envelope_method_returns_the_contract_shape(server_url, kralupy_xml, tmp_path):
    out = tmp_path / "live.geojson"
    async with connect(server_url) as ws:
        alignment_id = await _open_project(ws, kralupy_xml)
        reply = await _call(
            ws,
            "4",
            "alignment.envelope",
            {"path": str(out), "buffer_m": 250.0, "alignment_ids": [alignment_id]},
        )
        assert reply.type == "res", reply.error
        result = reply.result
        assert set(result) == {"files", "epsg", "area_m2", "vertex_count", "bounds"}
        assert result["files"] == [str(out)] and out.is_file()
        assert result["epsg"] == 4326 and result["vertex_count"] > 0 and len(result["bounds"]) == 4
        assert result["area_m2"] == pytest.approx(9.2885e6, rel=0.005)

        shp = tmp_path / "live.shp"
        reply = await _call(ws, "5", "alignment.envelope", {"path": str(shp), "cap": "flat"})
        assert reply.type == "res", reply.error
        assert reply.result["epsg"] == 5514 and len(reply.result["files"]) == 5


async def test_envelope_method_rejects_bad_params(server_url, kralupy_xml, tmp_path):
    out = str(tmp_path / "bad.geojson")
    async with connect(server_url) as ws:
        alignment_id = await _open_project(ws, kralupy_xml)
        cases = [
            ({"path": out, "buffer_m": 0.0}, "E_BAD_PARAMS"),
            ({"path": out, "cap": "square"}, "E_BAD_PARAMS"),
            ({"path": out, "station_from": -10.0}, "E_BAD_PARAMS"),
            ({"path": out, "alignment_ids": []}, "E_BAD_PARAMS"),
            ({"path": str(tmp_path / "bad.kml")}, "E_BAD_PARAMS"),
            ({"path": out, "epsg": 999999}, "E_BAD_PARAMS"),
            ({"buffer_m": 100.0}, "E_BAD_PARAMS"),
            ({"path": out, "alignment_ids": ["nope"]}, "E_NOT_FOUND"),
        ]
        for i, (params, code) in enumerate(cases):
            reply = await _call(ws, f"b{i}", "alignment.envelope", params)
            assert reply.type == "err" and reply.error.code == code, (params, reply.error)
        ok = await _call(ws, "ok", "alignment.envelope", {"path": out, "alignment_ids": [alignment_id]})
        assert ok.type == "res"


async def test_envelope_method_without_gis_extra_returns_an_error_not_a_crash(
    server_url, kralupy_xml, tmp_path, monkeypatch
):
    async with connect(server_url) as ws:
        await _open_project(ws, kralupy_xml)
        monkeypatch.setitem(sys.modules, "shapely", None)
        reply = await _call(ws, "g", "alignment.envelope", {"path": str(tmp_path / "x.geojson")})
        assert reply.type == "err" and "uv sync --extra gis" in reply.error.message
        monkeypatch.undo()
        assert (await _call(ws, "p", "project.get")).type == "res"


# --- review round: output CRS kind, partial files, overwrite, repository paths, limits -------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
BAD_OUTPUT_EPSG = [4978, 5703, 4979, 7405]  # geocentric, vertical, geographic 3D, compound


@pytest.mark.parametrize("epsg", BAD_OUTPUT_EPSG)
@pytest.mark.parametrize("name", ["g.shp", "g.geojson"])
def test_non_horizontal_output_crs_is_refused_before_anything_is_written(kralupy, tmp_path, epsg, name):
    with pytest.raises(ValueError, match="horizontal 2D"):
        export_corridor_envelope(
            [kralupy.alignment], EnvelopeOptions(buffer_m=100.0), KROVAK, tmp_path / name, epsg
        )
    assert list(tmp_path.iterdir()) == []


def test_cli_non_horizontal_crs_is_one_line(kralupy_xml, tmp_path, capsys):
    assert main(["envelope", str(kralupy_xml), "-o", str(tmp_path / "g.shp"), "--epsg", "4978"]) != 0
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1
    assert list(tmp_path.iterdir()) == []


def test_failed_write_leaves_no_partial_files(kralupy, tmp_path, monkeypatch):
    options = EnvelopeOptions(buffer_m=100.0)

    def broken_poly(self, *args, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(shapefile.Writer, "poly", broken_poly)
        with pytest.raises(OSError, match="disk full"):
            export_corridor_envelope([kralupy.alignment], options, KROVAK, tmp_path / "k.shp")
    assert list(tmp_path.iterdir()) == []

    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise OSError("rename failed")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="rename failed"):
        export_corridor_envelope([kralupy.alignment], options, KROVAK, tmp_path / "k.shp")
    assert list(tmp_path.iterdir()) == []


def test_existing_outputs_are_not_overwritten_unless_asked(kralupy, tmp_path, capsys, kralupy_xml):
    options = EnvelopeOptions(buffer_m=100.0)
    geojson = tmp_path / "k.geojson"
    geojson.write_text("keep me", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        export_corridor_envelope([kralupy.alignment], options, KROVAK, geojson, overwrite=False)
    assert geojson.read_text(encoding="utf-8") == "keep me"
    assert main(["envelope", str(kralupy_xml), "-o", str(geojson)]) != 0
    assert "overwrite" in capsys.readouterr().err
    assert geojson.read_text(encoding="utf-8") == "keep me"
    assert main(["envelope", str(kralupy_xml), "-o", str(geojson), "--force"]) == 0
    assert _read_geojson(geojson)["type"] == "FeatureCollection"

    sibling = tmp_path / "k.prj"
    sibling.write_text("keep me", encoding="utf-8")
    assert main(["envelope", str(kralupy_xml), "-o", str(tmp_path / "k.shp")]) != 0
    assert sibling.read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "k.shp").exists()


@pytest.mark.parametrize("buffer_m, ok", [(5000.0, True), (5000.1, False), (1e6, False)])
def test_buffer_upper_bound(buffer_m, ok):
    if ok:
        EnvelopeOptions(buffer_m=buffer_m)
    else:
        with pytest.raises(ValueError, match="at most 5000"):
            EnvelopeOptions(buffer_m=buffer_m)


def test_cli_buffer_above_the_limit_is_one_line(kralupy_xml, tmp_path, capsys):
    assert main(["envelope", str(kralupy_xml), "-o", str(tmp_path / "x.geojson"), "--buffer", "6000"]) != 0
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1


def test_densify_happens_before_reprojection(tmp_path):
    anchor = (-740_000.0, -1_040_000.0)  # near Kralupy, S-JTSK East North
    square = Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)])
    geometry = translate(square, *anchor)
    out = tmp_path / "dense.geojson"
    result = write_envelope(geometry, out, KROVAK, 4326, {"name": "dense"}, densify_m=50.0)
    ring = np.asarray(_read_geojson(out)["features"][0]["geometry"]["coordinates"][0])
    assert result.vertex_count == len(ring) >= 80
    x, y = ProjectCRS("EPSG:4326").to_crs(KROVAK.crs, ring[:, 0], ring[:, 1])
    edges = np.hypot(np.diff(x), np.diff(y))
    assert float(edges.max()) <= 50.0 + 0.05


def test_dbf_name_is_truncated_to_254_bytes_but_geojson_keeps_it(kralupy, tmp_path):
    import warnings

    geometry = corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=100.0))
    name = "ž" * 200 + "; second alignment"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        write_envelope(geometry, tmp_path / "n.shp", KROVAK, None, {"name": name, "buffer_m": 100.0})
    stored = _read_shapefile(tmp_path / "n.shp").record(0).as_dict()["name"]
    assert stored == "ž" * 127 and len(stored.encode("utf-8")) == 254

    write_envelope(geometry, tmp_path / "n.geojson", KROVAK, None, {"name": name})
    assert _read_geojson(tmp_path / "n.geojson")["features"][0]["properties"]["name"] == name


async def test_envelope_method_refuses_bad_crs_paths_and_overwrite(server_url, kralupy_xml, tmp_path):
    async with connect(server_url) as ws:
        await _open_project(ws, kralupy_xml)
        shp = str(tmp_path / "g.shp")
        existing = tmp_path / "e.geojson"
        existing.write_text("keep me", encoding="utf-8")
        cases = [
            ({"path": shp, "epsg": 4978}, "horizontal 2D"),
            ({"path": str(tmp_path / "g.geojson"), "epsg": 5703}, "horizontal 2D"),
            ({"path": "relative/g.geojson"}, "absolute"),
            ({"path": str(existing)}, "overwrite"),
            ({"path": str(tmp_path / "big.geojson"), "buffer_m": 5001.0}, "at most 5000"),
        ]
        if (REPO_ROOT / ".git").exists():
            cases.append(
                ({"path": str(REPO_ROOT / "backend" / "envelope_test.geojson")}, "outside the repository")
            )
        for i, (params, fragment) in enumerate(cases):
            reply = await _call(ws, f"r{i}", "alignment.envelope", params)
            assert reply.type == "err" and reply.error.code == "E_BAD_PARAMS", (params, reply)
            assert fragment in reply.error.message, (params, reply.error.message)
        assert existing.read_text(encoding="utf-8") == "keep me"
        assert not (REPO_ROOT / "backend" / "envelope_test.geojson").exists()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["e.geojson"]

        reply = await _call(ws, "ow", "alignment.envelope", {"path": str(existing), "overwrite": True})
        assert reply.type == "res", reply.error
        assert _read_geojson(existing)["type"] == "FeatureCollection"
        assert (await _call(ws, "alive", "project.get")).type == "res"
