import dataclasses
import time
from dataclasses import dataclass
from unittest import mock

import numpy as np
import pytest

from coypu_builder.domain import lrs
from coypu_builder.domain.geometry import (
    Alignment,
    CantProfile,
    CircularArc,
    HorizontalAlignment,
    Line,
    RotationPivot,
    VerticalAlignment,
)
from coypu_builder.domain.geometry._common import CURVATURE_EPS, s_array
from coypu_builder.domain.lrs import curvature_side, frames


def _reference_curvature_side(
    alignment: Alignment, s: np.ndarray, search_m: float = 250.0, step_m: float = 5.0
) -> np.ndarray:
    """sign(κ) at s; on straights, the sign of the nearest curved station within ±search_m (0 if none).

    Cant ramps that run onto a straight need to know which side is 'inside'; the adjacent curve tells us.
    """
    h = alignment.horizontal
    k = h.curvature(s)
    side = np.sign(k)
    flat = np.abs(k) < CURVATURE_EPS
    if not np.any(flat):
        return side
    offsets = np.arange(step_m, search_m + step_m, step_m)
    for i in np.flatnonzero(flat):
        si = s[i]
        for d in offsets:
            for cand in (si + d, si - d):
                if h.stations[0] <= cand <= h.stations[-1]:
                    kc = h.curvature(cand)[0]
                    if abs(kc) >= CURVATURE_EPS:
                        side[i] = np.sign(kc)
                        break
            if side[i] != 0:
                break
    return side


@dataclass(frozen=True)
class _FaintLine:
    """A segment whose curvature is nonzero but below CURVATURE_EPS, so the loop treats it as straight."""

    start_x: float
    start_y: float
    heading_start: float
    length: float
    faint: float

    kind = "faint"

    def point(self, s) -> np.ndarray:
        return Line(self.start_x, self.start_y, self.heading_start, self.length).point(s)

    def heading(self, s) -> np.ndarray:
        return np.full(s_array(s).shape, self.heading_start)

    def curvature(self, s) -> np.ndarray:
        return np.full(s_array(s).shape, self.faint)


def _with_cant(base: Alignment) -> Alignment:
    s0, s1 = base.station_start, base.station_end
    cs = np.linspace(s0, s1, 40)
    cant = CantProfile(cs, 110.0 * (0.5 + 0.5 * np.sin(2.0 * np.pi * (cs - s0) / 900.0)))
    return Alignment(base.horizontal, base.vertical, cant, name=base.name, mode=base.mode)


def _alignment_from_segments(*segments) -> Alignment:
    h = HorizontalAlignment(tuple(segments))
    cant = CantProfile(np.array([h.station_start, h.station_end]), np.array([120.0, 120.0]))
    return Alignment(h, VerticalAlignment.constant(100.0, h.station_start, h.station_end), cant)


def _straight_between_curves(straight_m: float = 485.0) -> Alignment:
    """ccw arc 0..300, straight 300..300+straight_m, cw arc after it.

    With 485 m the straight's midpoint 542.5 has both neighbours curved first at the same offset (245 m, on
    the 5 m grid), so it is a real tie. A 500 m straight is not: its station at 300.0 belongs to the line.
    """
    arc_a = CircularArc(0.0, 0.0, 0.0, 300.0, 1.0 / 500.0)
    x, y = arc_a.end
    line = Line(float(x), float(y), arc_a.heading_end, straight_m)
    x, y = line.end
    arc_b = CircularArc(float(x), float(y), line.heading_end, 300.0, -1.0 / 500.0)
    return _alignment_from_segments(arc_a, line, arc_b)


def _short_arcs_at_both_ends() -> Alignment:
    """ccw arc 0..1, straight 1..1001, ccw arc 1001..1002: a curve shorter than one step at each end."""
    arc_a = CircularArc(0.0, 0.0, 0.0, 1.0, 1.0 / 500.0)
    x, y = arc_a.end
    line = Line(float(x), float(y), arc_a.heading_end, 1000.0)
    x, y = line.end
    arc_b = CircularArc(float(x), float(y), line.heading_end, 1.0, 1.0 / 500.0)
    return _alignment_from_segments(arc_a, line, arc_b)


@pytest.fixture(scope="module")
def cant_kralupy(kralupy) -> Alignment:
    return _with_cant(kralupy.alignment)


@pytest.fixture(scope="module")
def window_stations(cant_kralupy) -> np.ndarray:
    """The first 3 km of Kralupy at 1 m."""
    s0 = cant_kralupy.station_start
    return np.arange(s0, s0 + 3000.0, 1.0)


@pytest.fixture(scope="module")
def window_reference(cant_kralupy, window_stations):
    """Reference (side, frames) for the 3 km window, computed once for the module."""
    side = _reference_curvature_side(cant_kralupy, window_stations)
    return side, _frames_with_side(cant_kralupy, window_stations, RotationPivot.LOW_RAIL, side)


@pytest.fixture(scope="module")
def line_10m_reference(cant_kralupy):
    """Reference side for the whole Kralupy line at 10 m (about 1,820 stations)."""
    s = np.arange(cant_kralupy.station_start, cant_kralupy.station_end, 10.0)
    return s, _reference_curvature_side(cant_kralupy, s)


def test_window_covers_a_straight_and_both_hands(cant_kralupy, window_stations):
    h = cant_kralupy.horizontal
    kinds = np.array([h.segments[i].kind for i in h.segment_index(window_stations)])
    k = np.sign(h.curvature(window_stations))
    assert np.any(kinds == "line")
    for hand in (1.0, -1.0):
        assert np.any((kinds == "clothoid") & (k == hand))
        assert np.any((kinds == "arc") & (k == hand))


def test_matches_the_reference_loop_on_kralupy_window_1m(cant_kralupy, window_stations, window_reference):
    assert np.array_equal(curvature_side(cant_kralupy, window_stations), window_reference[0])


def test_matches_the_reference_loop_on_kralupy_whole_line_10m(cant_kralupy, line_10m_reference):
    s, reference = line_10m_reference
    assert np.array_equal(curvature_side(cant_kralupy, s), reference)


def test_matches_the_reference_loop_near_both_ends_of_kralupy(kralupy):
    aln = _with_cant(kralupy.alignment)
    s0, s1 = aln.station_start, aln.station_end
    s = np.concatenate([np.arange(s0, s0 + 250.0, 0.5), np.arange(s1 - 250.0, s1, 0.5)])
    assert np.array_equal(curvature_side(aln, s), _reference_curvature_side(aln, s))


@pytest.mark.parametrize(
    ("search_m", "step_m"),
    [(250.0, 5.0), (60.0, 10.0), (37.0, 2.5), (120.0, 4.0), (2.0, 5.0), (0.0, 5.0)],
)
def test_matches_the_reference_loop_across_a_straight(search_m, step_m):
    aln = _straight_between_curves()
    s = np.arange(250.0, 850.0 + 0.5, 0.5)
    got = curvature_side(aln, s, search_m=search_m, step_m=step_m)
    assert np.array_equal(got, _reference_curvature_side(aln, s, search_m=search_m, step_m=step_m))


def test_matches_the_reference_loop_on_a_straight_longer_than_the_search():
    aln = _straight_between_curves(straight_m=1200.0)
    s = np.arange(0.0, 1800.0 + 2.0, 2.0)
    got = curvature_side(aln, s)
    assert np.array_equal(got, _reference_curvature_side(aln, s))
    middle = got[(s >= 600.0) & (s <= 1200.0)]
    assert middle.size > 0
    assert np.all(middle == 0.0)


def test_tie_at_the_exact_midpoint_goes_to_the_forward_candidate():
    aln = _straight_between_curves()
    s = np.arange(300.0, 785.0 + 0.5, 0.5)
    got = curvature_side(aln, s)
    assert np.array_equal(got, _reference_curvature_side(aln, s))
    midpoint = got[np.flatnonzero(s == 542.5)]
    assert midpoint.tolist() == [-1.0]  # 245 m forward is the cw arc, 245 m back is the ccw arc: forward wins


def test_out_of_range_candidates_are_skipped_not_clamped():
    aln = _short_arcs_at_both_ends()
    s = np.array([2.5, 4.0, 997.0, 998.5, 5.0])
    got = curvature_side(aln, s)
    assert got.tolist() == [0.0, 0.0, 1.0, 0.0, 1.0]  # 5.0 - 5 == 0.0 is the first station: in range, curved
    assert np.array_equal(got, _reference_curvature_side(aln, s))


def test_sub_eps_curvature_keeps_its_sign_unless_the_first_step_is_curved():
    s = np.array([100.0, 298.0])
    for faint, expected in ((1e-13, [1.0, -1.0]), (-1e-13, [-1.0, 1.0])):
        arc = CircularArc(300.0, 0.0, 0.0, 300.0, -float(np.sign(faint)) / 500.0)
        aln = _alignment_from_segments(_FaintLine(0.0, 0.0, 0.0, 300.0, faint), arc)
        got = curvature_side(aln, s)
        assert np.array_equal(got, _reference_curvature_side(aln, s))
        assert got.tolist() == expected


def test_sub_eps_search_reaches_exactly_the_first_step():
    arc_then_faint = _alignment_from_segments(
        CircularArc(0.0, 0.0, 0.0, 300.0, 1.0 / 500.0), _FaintLine(300.0, 0.0, 0.0, 300.0, -1e-13)
    )
    s = np.array([302.0, 400.0])
    got = curvature_side(arc_then_faint, s)
    assert np.array_equal(got, _reference_curvature_side(arc_then_faint, s))
    assert got.tolist() == [1.0, -1.0]  # 302: the arc is at -5 m, the second column; 400: nothing in reach

    faint_then_arc = _alignment_from_segments(
        _FaintLine(0.0, 0.0, 0.0, 300.0, 1e-13), CircularArc(300.0, 0.0, 0.0, 300.0, -1.0 / 500.0)
    )
    s = np.array([293.0, 298.0])
    got = curvature_side(faint_then_arc, s)
    assert np.array_equal(got, _reference_curvature_side(faint_then_arc, s))
    assert got.tolist() == [1.0, -1.0]  # 293: the arc is at +10 m, the third column, out of reach


def test_curvature_exactly_at_eps_counts_as_curved_and_is_not_searched():
    s = np.array([298.0])
    on_faint = _alignment_from_segments(
        _FaintLine(0.0, 0.0, 0.0, 300.0, CURVATURE_EPS), CircularArc(300.0, 0.0, 0.0, 300.0, -1.0 / 500.0)
    )
    got = curvature_side(on_faint, s)
    assert np.array_equal(got, _reference_curvature_side(on_faint, s))
    assert got.tolist() == [1.0]  # |κ| = CURVATURE_EPS is not flat: keeps sign(κ), not searched

    eps_ahead = _alignment_from_segments(
        Line(0.0, 0.0, 0.0, 300.0), _FaintLine(300.0, 0.0, 0.0, 300.0, CURVATURE_EPS)
    )
    got = curvature_side(eps_ahead, s)
    assert np.array_equal(got, _reference_curvature_side(eps_ahead, s))
    assert got.tolist() == [1.0]  # the +5 m candidate has |κ| = CURVATURE_EPS, so it counts as curved


@pytest.fixture(scope="module")
def straight_case():
    """The straight alignment on 250..850 m at 0.5 m, with its reference side, computed once."""
    aln = _straight_between_curves()
    s = np.arange(250.0, 850.0 + 0.5, 0.5)
    return aln, s, _reference_curvature_side(aln, s)


@pytest.mark.parametrize("chunk", [1, 250, lrs._CANDIDATE_CHUNK])
def test_chunking_does_not_change_the_result(monkeypatch, straight_case, chunk):
    aln, s, reference = straight_case
    monkeypatch.setattr(lrs, "_CANDIDATE_CHUNK", chunk)
    assert np.array_equal(curvature_side(aln, s), reference)


def test_curved_stations_keep_their_sign():
    aln = _straight_between_curves()
    on_arc = np.array([10.0, 200.0, 900.0, 1050.0])
    assert curvature_side(aln, on_arc).tolist() == [1.0, 1.0, -1.0, -1.0]


def _frames_with_side(aln, s, pivot, side):
    """frames() with curvature_side replaced by a precomputed array (the reference, run once)."""
    with mock.patch.object(lrs, "curvature_side", lambda *_args, **_kwargs: side):
        return frames(aln, s, pivot)


def _assert_frames_equal(reference, got):
    for field in dataclasses.fields(reference):
        expected = getattr(reference, field.name)
        actual = getattr(got, field.name)
        assert np.array_equal(actual, expected), field.name


def test_frames_unchanged_with_cant_on_kralupy(cant_kralupy, window_stations, window_reference):
    _assert_frames_equal(window_reference[1], frames(cant_kralupy, window_stations))


def test_frames_unchanged_with_cant_on_a_straight(straight_case):
    aln, s, side = straight_case
    for pivot in (RotationPivot.LOW_RAIL, RotationPivot.CENTERLINE):
        _assert_frames_equal(_frames_with_side(aln, s, pivot, side), frames(aln, s, pivot))


def test_curvature_side_is_fast(kralupy):
    aln = _with_cant(kralupy.alignment)
    s = np.linspace(aln.station_start, aln.station_end, 18000)
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        frames(aln, s)
        times.append(time.perf_counter() - t0)
    print(f"\nframes() on 18000 cant stations: best {min(times):.3f} s, runs {[round(t, 3) for t in times]}")
    assert min(times) < 1.0
