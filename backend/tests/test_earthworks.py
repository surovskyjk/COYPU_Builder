"""T-137 Part A: earthworks -- the formation template, cut and fill per section, volumes, no-ground spans, and
`alignment.ground_profile` over the wire.

The analytic cases are synthetic: straight alignments with analytic ground, so the exact areas are known by
hand (the formulas are in each test). Only the live check at the end reads terrain, and it runs only when
`COYPU_DATA` is set.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import laspy
import msgspec
import numpy as np
import pytest
from scipy.spatial import cKDTree
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.earthworks import (
    FLAG_NO_GROUND,
    FLAG_OPEN_SIDE,
    FLAG_PARTIAL,
    FormationTemplate,
    check_work_budget,
    end_area_volume,
    ground_profile,
    no_ground_spans,
    section_areas,
    section_line,
    section_offsets,
    station_grid,
)
from coypu_builder.domain.geometry import (
    Alignment,
    CantProfile,
    CircularArc,
    HorizontalAlignment,
    Line,
    RotationPivot,
    VerticalAlignment,
)
from coypu_builder.domain.lrs import frames
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.domain.terrain_surface import NO_SOURCE, TerrainStack, tin_from_points
from coypu_builder.io.mesh.terrain_tiles import lattice
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import register_builtin_providers
from coypu_builder.io.sources.runner import run_provider_import
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import Envelope, FormationTemplateDTO
from coypu_builder.server import handlers, serve
from coypu_builder.server.codec import decode_frame, encode_frame, unpack_blob
from coypu_builder.server.session import ProjectState, SourceEntry, TerrainCache
from fixtures.synthetic.terrain import sample_irregular, surface

FIXTURES = Path(__file__).parent / "fixtures"
KRALUPY_XML = FIXTURES / "kralupy" / "kralupy_neratovice_092.xml"
KRALUPY_COYPU = FIXTURES / "kralupy" / "kralupy_neratovice_092.coypu"
KROVAK = ProjectCRS("EPSG:5514")
TEMPLATE = FormationTemplate()
OFFSETS = section_offsets(TEMPLATE)
FORMATION = 99.2  # a rail at 100 m with the default 0.8 m depth
PRETTY = 1e-9


def _straight(length: float = 300.0, elevation: float = 100.0, heading: float = 0.0) -> Alignment:
    """A straight from E = 0, N = 0 along `heading` (radians from +E). With the default, due east: a station
    is
    E = s and the left offsets are +N."""
    horizontal = HorizontalAlignment((Line(0.0, 0.0, heading, length),), station_start=0.0)
    return Alignment.flat(horizontal, elevation=elevation)


# --- criterion 1: analytic sections ----------------------------------------------------------------------


def test_flat_at_formation_is_zero():
    cut, fill, flags = section_areas(OFFSETS, np.full(OFFSETS.shape, FORMATION), FORMATION, TEMPLATE)
    assert (cut, fill, flags) == (0.0, 0.0, 0)


def test_flat_two_metres_above_is_18_m2_cut():
    """Trapezoid: the bottom is W = 6 m wide, the top W + 2 * 1.5 * 2 = 12 m, height 2 m: (6 + 12) / 2 * 2."""
    cut, fill, flags = section_areas(OFFSETS, np.full(OFFSETS.shape, FORMATION + 2.0), FORMATION, TEMPLATE)
    assert cut == pytest.approx(18.0, rel=PRETTY)
    assert fill == 0.0 and flags == 0


def test_flat_two_metres_below_is_18_m2_fill():
    cut, fill, flags = section_areas(OFFSETS, np.full(OFFSETS.shape, FORMATION - 2.0), FORMATION, TEMPLATE)
    assert fill == pytest.approx(18.0, rel=PRETTY)
    assert cut == 0.0 and flags == 0


def test_sloping_plane_gives_the_exact_mixed_section():
    """Ground g(x) = F - 0.2 + 0.1 x, with F = 0 here. Right side (x > 0): the edge at x = 3 is at 0.1 >= 0,
    so it is a cut side with template t = (x - 3) / 1.5 beyond the edge. Its daylight is where
    g - t = 1.8 - (17/30) x = 0, x = 54/17, so cut = [0, 2] fill of 0.2 - 0.1 x (= 1/5), [2, 3] cut of
    0.1 x - 0.2 (= 1/20), and beyond the edge a triangle 0.1 at x = 3 over 3/17 (= 3/340).
    Right cut = 1/20 + 3/340 = 1/17, right fill = 1/5.
    Left side (u = -x > 0): the edge is at -0.5 < 0, a fill side with t = -(u - 3) / 1.5. Its daylight is at
    u = 66/17, where the integrand 2.2 - (17/30) u vanishes. Centre fill = int_0^3 (0.2 + 0.1 u) du = 21/20;
    beyond the edge int_3^(66/17) (2.2 - (17/30) u) du = 15/68. Left fill = 21/20 + 15/68 = 108/85.
    Total: cut 1/17, fill 1/5 + 108/85 = 25/17."""
    ground = FORMATION - 0.2 + 0.1 * OFFSETS
    cut, fill, flags = section_areas(OFFSETS, ground, FORMATION, TEMPLATE)
    assert cut == pytest.approx(1.0 / 17.0, rel=PRETTY)
    assert fill == pytest.approx(25.0 / 17.0, rel=PRETTY)
    assert flags == 0


def test_end_area_volume_is_exact_for_linear_areas():
    """A(s) = 2 + 0.5 s is linear, so the average-end-area rule is exact: int_0^40 A ds = 2 * 40 + 0.25 * 40^2
    = 480 with the stations 0, 10, 25, 40. Marking station 25 invalid removes the two pairs that touch it:
    only (0, 10) remains, (2 + 7) / 2 * 10 = 45."""
    stations = np.array([0.0, 10.0, 25.0, 40.0])
    areas = 2.0 + 0.5 * stations
    everything = end_area_volume(stations, areas, np.ones(4, dtype=bool))
    assert everything == pytest.approx(480.0, rel=PRETTY)
    gap = end_area_volume(stations, areas, np.array([True, True, False, True]))
    assert gap == pytest.approx(45.0, rel=PRETTY)


# --- criterion 2: no ground ------------------------------------------------------------------------------


def test_gap_gives_nan_flag_1_and_a_span_and_volumes_do_not_bridge_it():
    """Ground 2 m above formation everywhere except E in [100, 150], where there is none. Stations 100 to 150
    (every 10 m) are flag 1 with zero area and NaN ground. Valid pairs: 0-10 ... 80-90 (90 m) and 160-170
    ... 290-300 (140 m), so cut = 18 m2 * 230 m = 4140 m3. A pair that bridged the gap, 90-100 or 150-160,
    would add (18 + 0) / 2 * 10 = 90 m3 each, 180 m3 in all."""
    alignment = _straight()
    stations = np.arange(0.0, 301.0, 10.0)

    def heights(e, n):
        e = np.asarray(e, dtype=np.float64)
        h = np.where((e >= 100.0) & (e <= 150.0), np.nan, FORMATION + 2.0)
        return h, np.zeros(e.shape, dtype=np.int16)

    profile = ground_profile(alignment, heights, stations, TEMPLATE)
    gap = (stations >= 100.0) & (stations <= 150.0)
    assert (profile.flags[gap] == FLAG_NO_GROUND).all()
    assert (profile.flags[~gap] == 0).all()
    assert np.isnan(profile.ground_h[gap]).all() and not np.isnan(profile.ground_h[~gap]).any()
    assert (profile.cut_area[gap] == 0.0).all() and (profile.fill_area[gap] == 0.0).all()
    assert profile.no_ground_spans == ((100.0, 150.0),)
    assert profile.cut_volume_m3 == pytest.approx(18.0 * 230.0, rel=PRETTY)
    assert profile.fill_volume_m3 == 0.0


def test_nan_at_the_centreline_is_flag_1_with_zero_areas_and_no_side_is_read():
    ground = FORMATION + 2.0 + 0.0 * OFFSETS
    ground[int(np.argmin(np.abs(OFFSETS)))] = np.nan
    cut, fill, flags = section_areas(OFFSETS, ground, FORMATION, TEMPLATE)
    assert (cut, fill, flags) == (0.0, 0.0, FLAG_NO_GROUND)


# --- criterion 3 (domain side): sources, one-sided ground ------------------------------------------------


def test_source_matches_the_stack():
    """Two surfaces: a fine one over E 40..130 and a coarse one over the whole line. The source blob is the
    stack's own answer at the centreline, and the ground is the stack's height there, exactly."""
    fine = tin_from_points(sample_irregular((40.0, -30.0, 130.0, 30.0), 3.0, seed=1))
    coarse = tin_from_points(sample_irregular((-50.0, -60.0, 350.0, 60.0), 8.0, seed=2))
    stack = TerrainStack((fine, coarse), ("fine", "coarse"))
    alignment = _straight()
    stations = np.arange(0.0, 301.0, 10.0)
    profile = ground_profile(alignment, stack.heights_and_source, stations, TEMPLATE)

    e, n = stations, np.zeros_like(stations)
    want_h, want_src = stack.heights_and_source(e, n)
    assert np.array_equal(profile.source, want_src)
    assert np.array_equal(profile.ground_h, want_h, equal_nan=True)
    inside_fine = (stations >= 60.0) & (stations <= 110.0)
    assert (profile.source[inside_fine] == 0).all()
    assert (profile.source[stations >= 200.0] == 1).all()
    assert profile.source.dtype == np.int16 and NO_SOURCE == -1


def test_one_sided_ground_sets_flag_4():
    """Ground only on the positive (left) side: the right edge at x = -3 has none, so that side is skipped and
    its centre pairs are NaN. The left side is a cut side with its daylight at x = 6, so cut = half of 18
    = 9 m2, from the centre [0, 3] (2 * 3) and the side [3, 6] (3)."""
    ground = np.where(OFFSETS >= 0.0, FORMATION + 2.0, np.nan)
    cut, fill, flags = section_areas(OFFSETS, ground, FORMATION, TEMPLATE)
    assert cut == pytest.approx(9.0, rel=PRETTY)
    assert fill == 0.0
    assert flags == FLAG_PARTIAL


def test_a_gap_inside_a_side_sets_flag_4_and_its_pairs_are_not_integrated():
    """Ground 2 m above formation, except at x = 4 (no ground). Centre 2 * 6 = 12; the left side is whole,
    int_3^6 2 = 3 m2 (its daylight at x = -6). On the right, the pairs (3, 3.5), (4.5, 5), (5, 5.5) and
    (5.5, 6) are integrated: 0.916667 + 0.416667 + 0.25 + 0.083333 = 5/3, and the pairs that touch x = 4
    are skipped. Cut = 12 + 3 + 5/3 = 50/3, fill 0, flag 4."""
    ground = np.where(np.isclose(OFFSETS, 4.0), np.nan, FORMATION + 2.0)
    cut, fill, flags = section_areas(OFFSETS, ground, FORMATION, TEMPLATE)
    assert cut == pytest.approx(50.0 / 3.0, rel=PRETTY)
    assert fill == 0.0
    assert flags == FLAG_PARTIAL


# --- further analytic and contract cases -----------------------------------------------------------------


def test_section_offsets_include_the_centre_and_the_formation_edges_exactly():
    assert np.all(np.diff(OFFSETS) > 0.0)
    for value in (0.0, -3.0, 3.0, -60.0, 60.0):
        assert np.any(OFFSETS == value)
    assert OFFSETS.min() == -60.0 and OFFSETS.max() == 60.0
    odd = section_offsets(
        FormationTemplate(formation_width_m=5.0, sample_spacing_m=0.4, max_half_width_m=10.0)
    )
    assert np.any(odd == 2.5) and np.any(odd == -2.5) and np.any(odd == 0.0) and np.any(odd == 10.0)


def test_an_open_side_sets_flag_2_and_stops_at_the_limit():
    """Ground 1000 m above formation never meets the cut template (it rises 1/1.5 m per m) within 60 m. Each
    side is integrated to 60 m: centre 6 * 1000 = 6000; each side int_0^57 (1000 - u / 1.5) du = 57 000 - 1083
    = 55 917. Total cut = 6000 + 2 * 55 917 = 117 834 m2."""
    cut, fill, flags = section_areas(OFFSETS, np.full(OFFSETS.shape, FORMATION + 1000.0), FORMATION, TEMPLATE)
    assert flags == FLAG_OPEN_SIDE
    assert cut == pytest.approx(117834.0, rel=PRETTY)
    assert fill == 0.0


def test_flat_profile_volume_is_area_times_length():
    stations = np.arange(0.0, 101.0, 10.0)
    profile = ground_profile(
        _straight(),
        lambda e, n: (np.full(np.shape(e), FORMATION + 2.0), np.zeros(np.shape(e), dtype=np.int16)),
        stations,
        TEMPLATE,
    )
    assert profile.cut_volume_m3 == pytest.approx(18.0 * 100.0, rel=PRETTY)
    assert profile.fill_volume_m3 == 0.0
    assert profile.no_ground_spans == ()
    assert np.allclose(profile.rail_h, 100.0)


def test_no_ground_spans_are_maximal_runs():
    stations = np.arange(5.0)
    mask = np.array([True, True, False, True, True])
    assert no_ground_spans(stations, mask) == ((0.0, 1.0), (3.0, 4.0))
    assert no_ground_spans(stations, np.zeros(5, dtype=bool)) == ()


def test_the_template_is_validated():
    with pytest.raises(ValueError):
        FormationTemplate(formation_width_m=0.0)
    with pytest.raises(ValueError):
        FormationTemplate(cut_slope=float("nan"))
    with pytest.raises(ValueError):
        FormationTemplate(max_half_width_m=3.0)  # not beyond half the formation width
    with pytest.raises(ValueError):
        FormationTemplate(sample_spacing_m=0.001)  # too many samples per side


# --- review fixes (T-137): along-track sampling, asymmetric slopes, edge ties, grid, budget, section line ---


@pytest.mark.parametrize("heading", [0.0, np.pi / 2, np.pi / 4], ids=["east", "north", "45deg"])
def test_sloping_plane_through_ground_profile(heading):
    """Ground F - 0.2 + 0.1 * (offset to the left) on straights east, north and at 45 degrees. The offset of
    (E, N)
    to the left is (E, N) . (-sin heading, cos heading), so every station sees the section of
    test_sloping_plane_gives_the_exact_mixed_section: cut 1/17 and fill 25/17. Sampled along the track
    instead of
    across it, every section would see one constant ground and fail."""
    alignment = _straight(heading=heading)
    left_e, left_n = -np.sin(heading), np.cos(heading)

    def heights(e, n):
        e = np.asarray(e, dtype=np.float64)
        n = np.asarray(n, dtype=np.float64)
        return FORMATION - 0.2 + 0.1 * (e * left_e + n * left_n), np.zeros(e.shape, dtype=np.int16)

    stations = np.arange(0.0, 301.0, 10.0)
    profile = ground_profile(alignment, heights, stations, TEMPLATE)
    assert (profile.flags == 0).all()
    assert np.allclose(profile.cut_area, 1.0 / 17.0, rtol=PRETTY, atol=0.0)
    assert np.allclose(profile.fill_area, 25.0 / 17.0, rtol=PRETTY, atol=0.0)


def test_asymmetric_slopes():
    """cut_slope 2 and fill_slope 1. Flat +2 m: the centre gives 12 and each side int_0^4 (2 - u / 2) du = 4,
    so
    cut = 20 m2. Flat -2 m: each side int_0^2 (2 - u) du = 2, so fill = 12 + 4 = 16 m2."""
    template = FormationTemplate(cut_slope=2.0, fill_slope=1.0)
    offsets = section_offsets(template)
    cut, fill, flags = section_areas(offsets, np.full(offsets.shape, FORMATION + 2.0), FORMATION, template)
    assert cut == pytest.approx(20.0, rel=PRETTY) and fill == 0.0 and flags == 0
    cut, fill, flags = section_areas(offsets, np.full(offsets.shape, FORMATION - 2.0), FORMATION, template)
    assert fill == pytest.approx(16.0, rel=PRETTY) and cut == 0.0 and flags == 0


def test_edge_tie_daylights_beyond_the_edge():
    """Ground exactly at formation level at the edge, rising 1:1 beyond it, is a cut side: the ground rises
    faster
    than the 1:1.5 template, so neither side daylights and both run to the limit, flag 2. Each side gives
    int_0^57 u / 3 du = 541.5 m2, so the cut is 1083 m2. Ground 1e-9 m higher gives the same cut. Taking the
    edge
    sample as a daylight would give zero for the first and a large cut for the second."""
    tie = FORMATION + np.maximum(0.0, np.abs(OFFSETS) - 3.0)
    at_edge = section_areas(OFFSETS, tie, FORMATION, TEMPLATE)
    higher = section_areas(OFFSETS, tie + 1e-9, FORMATION, TEMPLATE)
    assert at_edge[0] == pytest.approx(1083.0, rel=PRETTY)
    assert at_edge[0] == pytest.approx(higher[0], rel=PRETTY)
    assert at_edge[1] == higher[1] == 0.0
    assert at_edge[2] == higher[2] == FLAG_OPEN_SIDE


def test_stations_fall_on_round_chainages():
    """Start, then every multiple of the spacing strictly between, then the end. A multiple within 1e-6 m of
    an
    end is that end, not a row of its own, and no multiple drifts."""
    np.testing.assert_allclose(
        station_grid(3.7, 47.2, 10.0), [3.7, 10.0, 20.0, 30.0, 40.0, 47.2], rtol=0.0, atol=1e-12
    )
    assert station_grid(10.0, 40.0, 10.0).tolist() == [10.0, 20.0, 30.0, 40.0]
    assert station_grid(10.0 - 1e-7, 40.0, 10.0).tolist() == [10.0 - 1e-7, 20.0, 30.0, 40.0]
    assert station_grid(10.0 + 1e-7, 40.0 - 1e-7, 10.0).tolist() == [10.0 + 1e-7, 20.0, 30.0, 40.0 - 1e-7]
    long = station_grid(12_700.0, 17_500.0, 10.0)
    assert len(long) == 481 and np.array_equal(long[1:-1], np.arange(1271, 1750) * 10.0)


def test_work_budget_is_refused_before_computing():
    """Each limit on its own refuses the profile before any terrain is queried: the heights callable fails if
    it
    is ever called."""

    def never(e, n):
        raise AssertionError("terrain was queried before the work budget was checked")

    alignment = _straight()
    narrow = FormationTemplate(max_half_width_m=6.0)  # 25 samples per section
    assert len(section_offsets(narrow)) == 25
    with pytest.raises(ValueError, match="spacing_m"):  # 100 001 stations: over the station limit
        ground_profile(alignment, never, np.arange(100_001, dtype=np.float64), narrow)
    fine = FormationTemplate(sample_spacing_m=0.01)  # 12 001 samples per section
    assert len(section_offsets(fine)) == 12_001
    with pytest.raises(ValueError, match="spacing_m"):  # 1 100 x 12 001 samples: over the sample limit
        ground_profile(alignment, never, np.arange(1100, dtype=np.float64), fine)
    with pytest.raises(ValueError, match="spacing_m"):
        check_work_budget(1100, fine)
    check_work_budget(999, fine)  # 11 988 999 samples, inside both limits


def test_section_line_equals_frames_centreline():
    """On a line, into a curve, with a grade change and cant: the centre, the across direction and the rail
    height
    from the horizontal and vertical alignments equal those of frames(..., pivot=CENTERLINE) to 1e-9."""
    horizontal = HorizontalAlignment(
        (Line(0.0, 0.0, 0.0, 200.0), CircularArc(200.0, 0.0, 0.0, 300.0, 1 / 400)), station_start=1000.0
    )
    vertical = VerticalAlignment(np.array([1000.0, 1200.0, 1500.0]), np.array([100.0, 104.0, 101.0]))
    cant = CantProfile(np.array([1000.0, 1400.0, 1500.0]), np.array([0.0, 120.0, 0.0]))
    alignment = Alignment(horizontal, vertical, cant)
    stations = np.linspace(1000.0, 1500.0, 51)
    e, n, across_e, across_n, rail_h = section_line(alignment, stations)
    fr = frames(alignment, stations, pivot=RotationPivot.CENTERLINE)
    assert np.any(fr.cant_mm > 0.0)
    np.testing.assert_allclose(e, fr.origin[:, 0], rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(n, fr.origin[:, 1], rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(across_e, -np.sin(fr.heading), rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(across_n, np.cos(fr.heading), rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(rail_h, fr.elevation, rtol=0.0, atol=1e-9)
    np.testing.assert_allclose(rail_h, fr.origin[:, 2], rtol=0.0, atol=1e-9)


def test_formation_template_dto_defaults_equal_the_domain_defaults():
    assert msgspec.structs.asdict(FormationTemplateDTO()) == dataclasses.asdict(FormationTemplate())


# --- criterion 3: over the wire --------------------------------------------------------------------------


class _Level:
    """A surface at one height, optionally slow, and optionally with ground only within `reach` = (E, N,
    radius): a stand-in analysis surface for the wire tests."""

    def __init__(self, height: float, delay: float = 0.0, reach: tuple[float, float, float] | None = None):
        self.height = height
        self.delay = delay
        self.reach = reach

    def heights(self, e, n) -> np.ndarray:
        if self.delay:
            time.sleep(self.delay)  # a worker thread, as a real build would hold it
        out = np.full(np.shape(e), self.height, dtype=np.float64)
        if self.reach is not None:
            e0, n0, radius = self.reach
            out[np.hypot(np.asarray(e) - e0, np.asarray(n) - n0) > radius] = np.nan
        return out

    def bounds(self):
        return (-1e9, -1e9, 1e9, 1e9)


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _recv(ws, timeout: float = 20.0):
    async with asyncio.timeout(timeout):
        frame = await ws.recv()
    return decode_frame(frame)


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None):
    await ws.send(
        encode_frame(Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params))
    )
    return await _recv(ws)


async def _open(ws, import_params: dict[str, Any]) -> dict[str, Any]:
    hello, _tail = await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
    assert hello.type == "res"
    created, _tail = await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})
    assert created.type == "res"
    reply, _tail = await _call(ws, "i", "import.landxml", {"path": str(KRALUPY_XML), **import_params})
    assert reply.type == "res", reply.error
    return reply.result


def _use_stack(monkeypatch, stack: TerrainStack, source_ids=("level",), sources=()) -> TerrainCache:
    """Stands in for the terrain build: `stack` is the current revision. `sources` (id, dataset name,
    attribution)
    are added to the project as an import would add them."""
    cache = TerrainCache(stack=stack, source_ids=tuple(source_ids), warnings=("a test warning",))

    async def ensure(session, project):
        for source_id, name, attribution in sources:
            dataset = SimpleNamespace(name=name, attribution=attribution)
            project.sources[source_id] = SimpleNamespace(dataset=dataset)
        return cache

    monkeypatch.setattr(handlers, "_ensure_terrain", ensure)
    return cache


async def test_profile_idealised_is_stored_and_returned_and_defaults_to_false(server_url):
    async with connect(server_url) as ws:
        default = await _open(ws, {})
        assert default["alignments"][0]["profile_idealised"] is False
        flagged, _tail = await _call(
            ws, "i2", "import.landxml", {"path": str(KRALUPY_XML), "profile_idealised": True}
        )
        assert flagged.result["alignments"][0]["profile_idealised"] is True
        info, _tail = await _call(ws, "g", "project.get")
        assert [a["profile_idealised"] for a in info.result["alignments"]] == [False, True]


async def test_profile_idealised_is_accepted_by_import_coypu(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})
        default, _tail = await _call(ws, "c0", "import.coypu", {"path": str(KRALUPY_COYPU)})
        assert default.type == "res", default.error
        assert default.result["alignments"][0]["profile_idealised"] is False
        await _call(ws, "p2", "project.new", {"crs": "EPSG:5514"})
        flagged, _tail = await _call(
            ws, "c", "import.coypu", {"path": str(KRALUPY_COYPU), "profile_idealised": True}
        )
        assert flagged.type == "res", flagged.error
        assert flagged.result["alignments"][0]["profile_idealised"] is True
        info, _tail = await _call(ws, "g", "project.get")
        assert [a["profile_idealised"] for a in info.result["alignments"]] == [True]


async def test_ground_profile_over_the_wire(server_url, monkeypatch, kralupy):
    start = kralupy.alignment.station_start
    origin = frames(kralupy.alignment, np.array([start]), pivot=RotationPivot.CENTERLINE).origin[0]
    # ground within 150 m of the first station only: the later stations have none, so `source` holds 255
    stack = TerrainStack((_Level(0.0, reach=(origin[0], origin[1], 150.0)),), ("dmr",))
    cache = _use_stack(
        monkeypatch, stack, source_ids=("dmr",), sources=[("dmr", "CUZK test sheets", "test attribution")]
    )
    async with connect(server_url) as ws:
        imported = await _open(ws, {"profile_idealised": True})
        assert imported["alignments"][0]["profile_idealised"] is True
        alignment_id = imported["alignments"][0]["alignment_id"]
        params = {
            "alignment_id": alignment_id,
            "spacing_m": 10.0,
            "station_from": start + 100.0,
            "station_to": start + 400.0,
        }
        reply, tail = await _call(ws, "gp", "alignment.ground_profile", params)
        assert reply.type == "res", reply.error
        stations = station_grid(start + 100.0, start + 400.0, 10.0)
        assert len(stations) == 31
        assert reply.result["row_count"] == len(stations)
        assert reply.result["source_ids"] == ["dmr"]
        assert reply.result["source_names"] == ["CUZK test sheets"]
        assert reply.result["attributions"] == {"dmr": "test attribution"}
        assert reply.result["terrain_revision"] == cache.revision
        assert reply.result["template"] == dataclasses.asdict(FormationTemplate())
        assert reply.result["warnings"] == ["a test warning"]
        blobs = {ref.name: unpack_blob(tail, ref) for ref in reply.blobs}
        dtypes = {ref.name: ref.dtype for ref in reply.blobs}
        assert dtypes["station"] == dtypes["rail_h"] == dtypes["ground_h"] == dtypes["cut_area"] == "<f8"
        assert dtypes["fill_area"] == "<f8" and dtypes["source"] == dtypes["flags"] == "<u1"
        expected = ground_profile(kralupy.alignment, stack.heights_and_source, stations, TEMPLATE)
        assert np.array_equal(blobs["station"], stations)
        assert np.array_equal(blobs["rail_h"], expected.rail_h)
        assert np.array_equal(blobs["ground_h"], expected.ground_h, equal_nan=True)
        assert np.array_equal(blobs["source"], np.where(expected.source < 0, 255, expected.source))
        assert (blobs["source"] == 255).any() and (blobs["source"] == 0).any()
        assert (blobs["flags"] == FLAG_NO_GROUND).any() and (blobs["flags"] & FLAG_OPEN_SIDE).any()
        assert np.array_equal(blobs["cut_area"], expected.cut_area)
        assert np.array_equal(blobs["fill_area"], expected.fill_area)
        assert np.array_equal(blobs["flags"], expected.flags)
        assert reply.result["fill_volume_m3"] == pytest.approx(expected.fill_volume_m3, rel=PRETTY)
        assert reply.result["cut_volume_m3"] == pytest.approx(expected.cut_volume_m3, rel=PRETTY)

        bad = [
            ({"spacing_m": 0.5}, "E_BAD_PARAMS"),
            ({"spacing_m": 150.0}, "E_BAD_PARAMS"),
            ({"station_from": start + 400.0, "station_to": start + 100.0}, "E_BAD_PARAMS"),
            ({"station_from": start - 1.0}, "E_BAD_PARAMS"),
            ({"template": {"formation_width_m": -1.0}}, "E_BAD_PARAMS"),
        ]
        for extra, code in bad:
            refused, _tail = await _call(
                ws, "bad", "alignment.ground_profile", {"alignment_id": alignment_id, **extra}
            )
            assert (refused.type, refused.error.code) == ("err", code), extra

        # 1 100 stations of 12 001 samples is over the sample limit: refused before the terrain is built, so a
        # build that is reached first fails the request with an internal error instead
        async def built_too_early(session, project):
            raise AssertionError("the terrain was built before the work budget was checked")

        monkeypatch.setattr(handlers, "_ensure_terrain", built_too_early)
        over_budget = {
            "alignment_id": alignment_id,
            "spacing_m": 1.0,
            "station_to": start + 1100.0,
            "template": {"sample_spacing_m": 0.01},
        }
        refused, _tail = await _call(ws, "budget", "alignment.ground_profile", over_budget)
        assert (refused.type, refused.error.code) == ("err", "E_BAD_PARAMS"), refused.error
        assert "spacing_m" in refused.error.message
        unknown, _tail = await _call(ws, "u", "alignment.ground_profile", {"alignment_id": "nope"})
        assert (unknown.type, unknown.error.code) == ("err", "E_NOT_FOUND")


async def test_ground_profile_without_a_project_is_refused(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        refused, _tail = await _call(ws, "g", "alignment.ground_profile", {"alignment_id": "x"})
        assert (refused.type, refused.error.code) == ("err", "E_NO_PROJECT")


async def test_a_project_without_terrain_has_no_ground_anywhere(server_url, kralupy):
    """No terrain source: every station is flag 1, the one span covers the whole range, and the answer names
    no source. It is not an error."""
    start = kralupy.alignment.station_start
    async with connect(server_url) as ws:
        imported = await _open(ws, {})
        alignment_id = imported["alignments"][0]["alignment_id"]
        reply, tail = await _call(
            ws,
            "gp",
            "alignment.ground_profile",
            {
                "alignment_id": alignment_id,
                "spacing_m": 50.0,
                "station_from": start,
                "station_to": start + 100.0,
            },
        )
        assert reply.type == "res", reply.error
        assert reply.result["source_ids"] == [] and reply.result["source_names"] == []
        assert reply.result["row_count"] == len(station_grid(start, start + 100.0, 50.0))
        assert reply.result["no_ground_spans"] == [[start, start + 100.0]]
        flags = unpack_blob(tail, next(ref for ref in reply.blobs if ref.name == "flags"))
        assert (flags == FLAG_NO_GROUND).all()


def _corridor_points(
    alignment, start: float, stop: float, width: float, spacing: float, seed: int, lift: float = 0.0
) -> np.ndarray:
    """Synthetic terrain within `width` of the centreline, kept only where the nearest centreline point lies
    between stations `start` and `stop`, so a stretch of the line has data and the rest has none. `lift`
    raises the surface, as a surface model stands above the ground."""
    dense = np.arange(alignment.station_start, alignment.station_start + 1200.0, 1.0)
    centre = frames(alignment, dense, pivot=RotationPivot.CENTERLINE).origin[:, :2]
    low, high = centre.min(axis=0) - width, centre.max(axis=0) + width
    points = sample_irregular((low[0], low[1], high[0], high[1]), spacing, seed)
    distance, nearest = cKDTree(centre).query(points[:, :2])
    station = dense[nearest]
    keep = (distance <= width) & (station >= start) & (station <= stop)
    points = points[keep]
    points[:, 2] += lift
    return points


def _las(path: Path, points: np.ndarray) -> str:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.write(path)
    return str(path)


async def _project(ws, terrain: list[tuple[str, dict[str, Any]]]) -> tuple[str, list[str]]:
    """A new project with the Kralupy line and the terrain files, imported in order.

    Returns the alignment id and the source ids."""
    created, _tail = await _call(ws, "p", "project.new", {"crs": "EPSG:5514"})
    assert created.type == "res", created.error
    imported, _tail = await _call(ws, "i", "import.landxml", {"path": str(KRALUPY_XML)})
    assert imported.type == "res", imported.error
    source_ids = []
    for number, (path, settings) in enumerate(terrain):
        run, _tail = await _call(
            ws,
            f"t{number}",
            "import.run",
            {"paths": [path], "kind": "terrain", "settings": settings},
        )
        assert run.type == "res", run.error
        source_ids.append(run.result["source_id"])
    return imported.result["alignments"][0]["alignment_id"], source_ids


async def _profile(ws, alignment_id: str, start: float, stop: float):
    reply, tail = await _call(
        ws,
        "gp",
        "alignment.ground_profile",
        {"alignment_id": alignment_id, "spacing_m": 10.0, "station_from": start, "station_to": stop},
    )
    assert reply.type == "res", reply.error
    return reply.result, {ref.name: unpack_blob(tail, ref) for ref in reply.blobs}


async def test_ground_profile_flags_surface_model_only_stations(server_url, tmp_path, kralupy):
    """F57, over the wire. Bare ground covers the first 300 m of the line, and a surface model 5 m higher
    covers the first kilometre. Past 320 m there is no bare ground under the centreline but a surface model
    is there: flag 9, no ground, zero areas and the surface model's index. The totals and spans equal those of
    the same project without the surface model. A terrain tile over the gap still shows the surface model."""
    alignment = kralupy.alignment
    start = alignment.station_start
    bare = _las(tmp_path / "bare.las", _corridor_points(alignment, start, start + 300.0, 100.0, 6.0, seed=1))
    model = _las(
        tmp_path / "model.las",
        _corridor_points(alignment, start, start + 1100.0, 150.0, 10.0, seed=2, lift=5.0),
    )
    bare_settings = {"horizontal_crs": "EPSG:5514"}
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        alone_id, _ = await _project(ws, [(bare, bare_settings)])
        without, without_blobs = await _profile(ws, alone_id, start, start + 1000.0)
        both_id, (bare_id, model_id) = await _project(
            ws, [(bare, bare_settings), (model, {**bare_settings, "surface_model": True})]
        )
        with_model, blobs = await _profile(ws, both_id, start, start + 1000.0)

        listed, _tail = await _call(ws, "sl", "source.list")
        listed_flags = {entry["id"]: entry["surface_model"] for entry in listed.result["sources"]}
        assert listed_flags == {bare_id: False, model_id: True}
        got, _tail = await _call(ws, "sg", "source.get", {"source_id": model_id})
        assert got.result["settings"]["surface_model"] is True

        stations = blobs["station"]
        gap = stations > start + 320.0
        near = stations <= start + 250.0
        assert gap.any() and near.any()
        assert (without_blobs["flags"][gap] == FLAG_NO_GROUND).all()
        assert (blobs["flags"][gap] == FLAG_NO_GROUND | handlers.FLAG_SURFACE_MODEL).all()
        assert np.isnan(blobs["ground_h"][gap]).all()
        assert (blobs["cut_area"][gap] == 0.0).all() and (blobs["fill_area"][gap] == 0.0).all()
        model_index = with_model["source_ids"].index(model_id)
        assert (blobs["source"][gap] == model_index).all()
        for name in ("flags", "ground_h", "source"):
            assert np.array_equal(blobs[name][near], without_blobs[name][near], equal_nan=True), name
        assert with_model["cut_volume_m3"] == without["cut_volume_m3"]
        assert with_model["fill_volume_m3"] == without["fill_volume_m3"]
        assert with_model["no_ground_spans"] == without["no_ground_spans"]
        spans = with_model["no_ground_spans"]
        assert spans and spans[0][0] <= start + 320.0 and spans[-1][1] >= start + 1000.0

        centre = frames(alignment, np.array([start + 800.0]), pivot=RotationPivot.CENTERLINE).origin[0]
        ix, iy = int(np.floor(centre[0] / 512.0)), int(np.floor(centre[1] / 512.0))
        listing, _tail = await _call(ws, "tl", "terrain.tiles", {"level": 0})
        assert listing.type == "res", listing.error
        assert {"ix": ix, "iy": iy} in [{"ix": t["ix"], "iy": t["iy"]} for t in listing.result["tiles"]]
        tile, tile_tail = await _call(
            ws,
            "tt",
            "terrain.tile",
            {"level": 0, "ix": ix, "iy": iy, "revision": listing.result["revision"]},
        )
        assert tile.type == "res", tile.error
        heights = unpack_blob(tile_tail, tile.blobs[0])
        info, _tail = await _call(ws, "pg", "project.get")
        base_height = info.result["base_point"]["height"]  # the tile origin is relative to the base point
        absolute = tile.result["tile_origin"][1] + base_height + heights
        e, n = lattice(0, ix, iy)
        valid = np.isfinite(absolute)
        assert valid.sum() > 1000
        np.testing.assert_allclose(absolute[valid], (surface(e, n) + 5.0)[valid], rtol=0.0, atol=0.1)


async def test_flag_8_comes_only_with_flag_1_when_the_model_outranks_the_bare_source(
    server_url, tmp_path, kralupy
):
    """F57, the review's case, over the wire. A finer surface model (3 m, 5 m higher) outranks a coarser
    bare-ground source (10 m) wherever both cover the centreline. Bare ground covers the first 300 m and the
    model the first 700 m. Flag 8 comes only on the flag-1 stations past 300 m: the bare-ground rows the model
    covers too keep flags 0 and the bare source's index, and every row with flag 8 also has flag 1."""
    alignment = kralupy.alignment
    start = alignment.station_start
    bare = _las(
        tmp_path / "bare_coarse.las",
        _corridor_points(alignment, start, start + 300.0, 100.0, 10.0, seed=3),
    )
    model = _las(
        tmp_path / "model_fine.las",
        _corridor_points(alignment, start, start + 700.0, 150.0, 3.0, seed=4, lift=5.0),
    )
    bare_settings = {"horizontal_crs": "EPSG:5514"}
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        both_id, (bare_id, model_id) = await _project(
            ws, [(bare, bare_settings), (model, {**bare_settings, "surface_model": True})]
        )
        with_model, blobs = await _profile(ws, both_id, start, start + 1000.0)

    stations = blobs["station"]
    flags = blobs["flags"]
    ground = blobs["ground_h"]
    source = blobs["source"]
    surface = (flags & handlers.FLAG_SURFACE_MODEL) != 0
    bare_rows = np.isfinite(ground)
    assert bare_rows.any() and surface.any()
    no_8 = (flags & handlers.FLAG_SURFACE_MODEL) == 0
    has_1 = (flags & FLAG_NO_GROUND) != 0
    assert (no_8 | has_1).all(), "flag 8 without flag 1"
    assert (flags[bare_rows] == 0).all(), "a bare-ground row carries a flag"
    assert (source[bare_rows] == with_model["source_ids"].index(bare_id)).all()
    assert (flags[surface] == FLAG_NO_GROUND | handlers.FLAG_SURFACE_MODEL).all()
    assert (source[surface] == with_model["source_ids"].index(model_id)).all()
    assert np.isnan(ground[surface]).all()
    assert stations[surface].min() >= start + 300.0 - 1e-6
    assert stations[bare_rows].max() <= start + 300.0 + 1e-6


async def test_session_ping_keeps_answering_while_the_ground_profile_samples(
    server_url, monkeypatch, kralupy
):
    _use_stack(monkeypatch, TerrainStack((_Level(0.0, delay=1.5),), ("level",)))
    async with connect(server_url) as ws:
        imported = await _open(ws, {})
        alignment_id = imported["alignments"][0]["alignment_id"]
        start = kralupy.alignment.station_start
        params = {
            "alignment_id": alignment_id,
            "spacing_m": 10.0,
            "station_from": start,
            "station_to": start + 300.0,
        }
        await ws.send(
            encode_frame(
                Envelope(
                    v=PROTOCOL_VERSION, id="gp", type="req", method="alignment.ground_profile", params=params
                )
            )
        )
        reply = None
        latencies: list[float] = []
        number = 0
        while reply is None:
            number += 1
            ping_id = f"ping{number}"
            sent = time.perf_counter()
            await ws.send(
                encode_frame(
                    Envelope(v=PROTOCOL_VERSION, id=ping_id, type="req", method="session.ping", params={})
                )
            )
            answered = False
            while not answered:
                envelope, _tail = await _recv(ws)
                if envelope.id == "gp":
                    reply = envelope
                else:
                    assert envelope.id == ping_id and envelope.type == "res", envelope
                    latencies.append(time.perf_counter() - sent)
                    answered = True
            if reply is None:
                await asyncio.sleep(0.05)
        assert reply.type == "res", reply.error
        assert len(latencies) >= 2 and max(latencies) < 1.0, latencies


# --- criterion 4: the live Kralupy profile (only on the data machine) -------------------------------------


@pytest.mark.network
def test_live_kralupy_ground_profile_12_7_to_17_5_km(kralupy, tmp_path, monkeypatch):
    """The DMR 5G sheets of the corridor, by the provider, as the live terrain test reads them. Prints the
    figures that criterion 4 asks for."""
    if not os.environ.get("COYPU_DATA"):
        pytest.skip("COYPU_DATA is not set: the live Kralupy profile is run on the data machine")
    monkeypatch.setattr(provider_cache, "providers_cache_root", lambda: tmp_path / "provider-cache")
    register_builtin_readers()
    register_builtin_providers()
    alignments = {"kralupy": kralupy.alignment}
    settings = ImportSettings(horizontal_crs="EPSG:5514", vertical_crs="EPSG:5705")
    outcome = run_provider_import("cuzk-dmr5g", {}, SourceKind.TERRAIN, settings, KROVAK, alignments)
    coverage = handlers.clip_polygon(outcome.dataset.settings.clip, alignments)
    project = ProjectState(project_id="live", crs=KROVAK)
    project.sources[outcome.dataset.id] = SourceEntry(
        outcome.dataset, outcome.payloads, outcome.warnings, coverage, outcome.resolution_m
    )
    stack, _coverage, warnings = handlers._build_terrain(handlers._terrain_snapshot(project))
    stations = station_grid(12700.0, 17500.0, 10.0)
    profile = ground_profile(kralupy.alignment, stack.heights_and_source, stations, TEMPLATE)

    fr = frames(kralupy.alignment, stations, pivot=RotationPivot.CENTERLINE)
    direct = stack.heights(fr.origin[:, 0], fr.origin[:, 1])
    np.testing.assert_allclose(profile.ground_h, direct, atol=1e-6, equal_nan=True)

    diff = profile.ground_h - profile.rail_h

    def at(metres: float) -> float:
        return float(diff[int(np.argmin(np.abs(stations - metres)))])

    for km in (13000.0, 14000.0, 16000.0):
        print(f"ground minus rail at {km / 1000:.1f} km: {at(km):.2f} m")
    assert abs(at(13000.0) - 17.0) < 1.0  # about +17 m at km 13.0 (F32, T-132)
    assert -1.0 <= at(16000.0) <= 3.0  # about 0 to 2 m by km 16, within 1 m
    print("no-ground spans:", profile.no_ground_spans)
    # the Vltava bridge: about [16 660, 16 800] on the 10 m grid
    assert any(abs(a - 16660.0) <= 20.0 and abs(b - 16800.0) <= 20.0 for a, b in profile.no_ground_spans)
    print(
        f"cut {profile.cut_volume_m3:.0f} m3, fill {profile.fill_volume_m3:.0f} m3 "
        "(nominal: the profile is idealised)"
    )
    for warning in warnings:
        print("warning:", warning)
