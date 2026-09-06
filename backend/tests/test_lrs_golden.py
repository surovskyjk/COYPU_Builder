"""Golden tests against COYPU's parse of the same Kralupy–Neratovice LandXML (stored in the .coypu archive).

COYPU keeps stations in km, raw Křovák tokens (X, Y) as parsed and dense polylines discretised with
pyclothoids from the Start→PI azimuth. Builder chains exact headings instead, so the dense comparison is a
geometric distance check while stations and key points must agree to numerical precision.
"""

import numpy as np
import pytest

from coypu_builder.domain.lrs import frames, project_xy
from coypu_builder.domain.sampling import bake_frame_table
from coypu_builder.io.landxml.dialects import resolve_coordinates


def _raw_to_en(raw_xy, crs):
    return resolve_coordinates(np.asarray(raw_xy, dtype=np.float64), crs)


def test_element_count_and_stations_match_coypu(kralupy, kralupy_project):
    aln = kralupy.alignment
    lx = kralupy_project.landxml
    n = len(aln.horizontal.segments)
    assert n == 65
    coypu_starts = np.asarray(lx["stationHorizontal"])[::2] * 1000.0
    coypu_ends = np.asarray(lx["stationHorizontal"])[1::2] * 1000.0
    # The file's staStart attributes are the LRS truth (COYPU's kinematics are stationed against them);
    # geometric lengths differ from them at the 1e-4 m level and are re-parametrised per element.
    assert np.allclose(aln.horizontal.stations[:-1], coypu_starts, rtol=0.0, atol=1e-9)
    assert np.allclose(aln.horizontal.stations[1:], coypu_ends, rtol=0.0, atol=1e-9)
    assert aln.station_end == pytest.approx(18184.971666, abs=1e-9)
    assert float(np.max(np.abs(aln.horizontal.station_scale - 1.0))) < 1e-5
    kinds = [seg.kind for seg in aln.horizontal.segments]
    coypu_kinds = [str(t) for t in np.asarray(lx["geometryType"])[::2]]
    mapping = {"Line": "line", "Curve": "arc", "Spiral": "clothoid"}
    assert kinds == [mapping[k] for k in coypu_kinds]


def test_key_points_match_file_exactly(kralupy, kralupy_project, krovak):
    aln = kralupy.alignment
    lx = kralupy_project.landxml
    key = _raw_to_en(np.column_stack([lx["keyX"], lx["keyY"]]), krovak)
    starts = np.array([seg.start for seg in aln.horizontal.segments])
    assert np.allclose(starts, key[:-1], atol=1e-9)
    assert np.allclose(aln.horizontal.segments[-1].end, key[-1], atol=2e-3)


def test_closure_and_continuity(kralupy):
    report = kralupy.report
    assert report.max_end_deviation_m < 2e-3, report.warnings
    assert report.max_heading_jump_rad < 1e-4
    assert not [w for w in report.warnings if "gap" in w]


def test_dense_geometry_matches_coypu_within_tolerance(kralupy, kralupy_project, krovak):
    aln = kralupy.alignment
    # COYPU stores the discretised polylines grouped by element type (all Lines, then Spirals, then Curves).
    by_kind: dict[str, list] = {"Line": [], "Spiral": [], "Curve": []}
    for pts, kind in kralupy_project.landxml["alignmentCoordsOriginal"]:
        by_kind[str(kind)].append(_raw_to_en(pts, krovak))
    ours_by_kind = {"Line": [], "Spiral": [], "Curve": []}
    for seg in aln.horizontal.segments:
        ours_by_kind[{"line": "Line", "clothoid": "Spiral", "arc": "Curve"}[seg.kind]].append(seg)
    worst = {}
    for kind, refs in by_kind.items():
        assert len(refs) == len(ours_by_kind[kind])
        worst[kind] = 0.0
        for seg, ref in zip(ours_by_kind[kind], refs, strict=True):
            ours = seg.point(np.linspace(0.0, seg.length, len(ref)))
            worst[kind] = max(worst[kind], float(np.max(np.hypot(*(ours - ref).T))))
    # Lines are exact. Curves: COYPU discretises with the declared `radius` while Builder uses the
    # Start–Center distance (Feeder writes them ~0.3 mm apart). Spirals: COYPU discretises from the
    # approximate PI azimuth, Builder chains exact headings. Everything agrees to sub-millimetre.
    assert worst["Line"] < 1e-6, worst
    assert worst["Curve"] < 1e-3, worst
    assert worst["Spiral"] < 2e-3, worst


def test_vertical_profile_matches(kralupy, kralupy_project):
    v = kralupy.alignment.vertical
    lx = kralupy_project.landxml
    assert np.allclose(v.pvi_stations, np.asarray(lx["stationVertical"]) * 1000.0, atol=1e-6)
    assert np.allclose(v.pvi_elevations, np.asarray(lx["elevation"]), atol=1e-9)
    assert len(v.curves) == 10
    assert np.allclose(v.elevation(v.pvi_stations[[0, -1]]), np.asarray(lx["elevation"])[[0, -1]])


def test_cant_block_is_zero_placeholder_with_feeder_units(kralupy):
    c = kralupy.alignment.cant
    assert c.gauge_mm == pytest.approx(1435.0)
    assert c.superelevation_base_mm == pytest.approx(1500.0)
    assert float(np.max(c.cant_mm)) == 0.0
    assert len(c.stations) == 66


def test_start_projects_to_wgs84_like_coypu(kralupy, kralupy_project, krovak):
    start = kralupy.alignment.horizontal.segments[0].start
    lon, lat = krovak.to_wgs84(start[0], start[1])
    _, ref_lat, ref_lon = kralupy_project.landxml["denseAlignment"][0]
    assert float(lat) == pytest.approx(ref_lat, abs=1e-8)
    assert float(lon) == pytest.approx(ref_lon, abs=1e-8)


def test_project_xy_round_trip(kralupy):
    aln = kralupy.alignment
    s_true = np.array([1234.5, 9000.0, 17500.25])
    fr = frames(aln, s_true)
    off = fr.origin[:, :2] + 3.0 * fr.left[:, :2] / np.linalg.norm(fr.left[:, :2], axis=1, keepdims=True)
    s, y = project_xy(aln, off)
    assert np.allclose(s, s_true, atol=1e-6)
    assert np.allclose(y, 3.0, atol=1e-6)


def test_frame_table_is_dense_monotonic_and_orthonormal(kralupy):
    aln = kralupy.alignment
    table = bake_frame_table(aln, spacing_m=10.0)
    assert np.all(np.diff(table.station) > 0)
    assert set(np.round(aln.horizontal.stations, 6)).issubset(set(np.round(table.station, 6)))
    for a, b in ((table.tangent, table.left), (table.left, table.up), (table.tangent, table.up)):
        assert np.allclose((a * b).sum(axis=1), 0.0, atol=1e-12)
    assert np.allclose(np.linalg.norm(table.tangent, axis=1), 1.0)
    dets = np.linalg.det(np.stack([table.tangent, table.left, table.up], axis=-1))
    assert np.allclose(dets, 1.0)
    assert table.segment_index.dtype == np.int32
    assert table.segment_index.max() == 64
    assert table.elevation.min() > 160.0 and table.elevation.max() < 185.0
