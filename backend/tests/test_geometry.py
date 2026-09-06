import numpy as np
import pytest

from coypu_builder.domain.geometry import CircularArc, Clothoid, HorizontalAlignment, Line


def _series_clothoid_xy(s: float, a: float) -> tuple[float, float]:
    """Feeder's Fresnel power series (reference implementation, entry spiral from a straight)."""
    a2, a4, a6, a8 = a**2, a**4, a**6, a**8
    x = s - s**5 / (40.0 * a4) + s**9 / (3456.0 * a8)
    y = s**3 / (6.0 * a2) - s**7 / (336.0 * a6) + s**11 / (42240.0 * a4 * a6)
    return x, y


def _check_tangent_consistency(seg, n: int = 25, h: float = 1e-4):
    """dP/ds must equal (cos θ, sin θ) and dθ/ds must equal κ everywhere on the segment."""
    s = np.linspace(h, seg.length - h, n)
    p_plus, p_minus = seg.point(s + h), seg.point(s - h)
    d = (p_plus - p_minus) / (2 * h)
    th = seg.heading(s)
    assert np.allclose(d, np.column_stack([np.cos(th), np.sin(th)]), atol=1e-7)
    dth = (seg.heading(s + h) - seg.heading(s - h)) / (2 * h)
    k = seg.curvature_at(s) if seg.kind == "arc" else seg.curvature(s)
    assert np.allclose(dth, k, atol=1e-8)


def test_line_basics():
    line = Line.from_points((0.0, 0.0), (3.0, 4.0))
    assert line.length == pytest.approx(5.0)
    assert line.end.tolist() == pytest.approx([3.0, 4.0])
    assert line.heading(2.5)[0] == pytest.approx(np.arctan2(4, 3))
    assert line.curvature_end == 0.0


def test_arc_from_center_ccw_and_cw():
    ccw = CircularArc.from_center((1.0, 0.0), (0.0, 0.0), (0.0, 1.0), "ccw")
    assert ccw.radius == pytest.approx(1.0)
    assert ccw.curvature == pytest.approx(1.0)
    assert ccw.length == pytest.approx(np.pi / 2)
    assert ccw.heading_start == pytest.approx(np.pi / 2)
    assert ccw.end.tolist() == pytest.approx([0.0, 1.0], abs=1e-12)
    assert ccw.heading_end == pytest.approx(np.pi)
    assert ccw.center.tolist() == pytest.approx([0.0, 0.0], abs=1e-12)
    _check_tangent_consistency(ccw)

    cw = CircularArc.from_center((0.0, 1.0), (0.0, 0.0), (1.0, 0.0), "cw")
    assert cw.curvature == pytest.approx(-1.0)
    assert cw.heading_start == pytest.approx(0.0)
    assert cw.length == pytest.approx(np.pi / 2)
    assert cw.end.tolist() == pytest.approx([1.0, 0.0], abs=1e-12)
    _check_tangent_consistency(cw)


def test_reflex_arc_sweep():
    arc = CircularArc.from_center((1.0, 0.0), (0.0, 0.0), (0.0, -1.0), "ccw")
    assert arc.length == pytest.approx(1.5 * np.pi)
    assert arc.end.tolist() == pytest.approx([0.0, -1.0], abs=1e-12)


def test_arc_explicit_length_wins_over_sweep():
    arc = CircularArc.from_center((1.0, 0.0), (0.0, 0.0), (0.0, 1.0), "ccw", length=1.0)
    assert arc.length == 1.0


def test_entry_spiral_matches_fresnel_series():
    radius, length = 300.0, 50.0
    a = np.sqrt(radius * length)
    spiral = Clothoid(0.0, 0.0, 0.0, length, 0.0, 1.0 / radius)
    assert spiral.clothoid_parameter == pytest.approx(a)
    # The reference series is truncated after s^11; its s^13 term is ~2e-9 at s = 50 m, A² = 15000.
    for s in (5.0, 20.0, 37.5, 50.0):
        x_ref, y_ref = _series_clothoid_xy(s, a)
        x, y = spiral.point(s)[0]
        assert x == pytest.approx(x_ref, abs=5e-9)
        assert y == pytest.approx(y_ref, abs=5e-9)
    assert spiral.heading_end == pytest.approx(length / (2 * radius))
    assert spiral.curvature(length)[0] == pytest.approx(1 / radius)
    _check_tangent_consistency(spiral)


def test_entry_spiral_cw_is_mirrored():
    ccw = Clothoid(0.0, 0.0, 0.0, 50.0, 0.0, 1.0 / 300.0)
    cw = Clothoid(0.0, 0.0, 0.0, 50.0, 0.0, -1.0 / 300.0)
    p1, p2 = ccw.point(50.0)[0], cw.point(50.0)[0]
    assert p1[0] == pytest.approx(p2[0])
    assert p1[1] == pytest.approx(-p2[1])
    _check_tangent_consistency(cw)


def test_exit_spiral_is_reversed_entry_spiral():
    entry = Clothoid(10.0, -3.0, 0.4, 60.0, 0.0, 1.0 / 450.0)
    end = entry.end
    back = Clothoid(end[0], end[1], entry.heading_end + np.pi, 60.0, -1.0 / 450.0, 0.0)
    assert back.end.tolist() == pytest.approx([10.0, -3.0], abs=1e-9)
    assert (back.heading_end - (0.4 + np.pi)) % (2 * np.pi) == pytest.approx(0.0, abs=1e-12)
    _check_tangent_consistency(back)


@pytest.mark.parametrize(
    "k0,k1", [(1 / 1000, 1 / 400), (-1 / 400, -1 / 1000), (1 / 800, -1 / 800), (-1 / 500, 1 / 2000)]
)
def test_compound_and_reverse_spirals_are_consistent(k0, k1):
    seg = Clothoid(100.0, 200.0, -1.1, 80.0, k0, k1)
    assert seg.curvature(0.0)[0] == pytest.approx(k0)
    assert seg.curvature(80.0)[0] == pytest.approx(k1)
    _check_tangent_consistency(seg)


def test_degenerate_clothoid_falls_back_to_arc_and_line():
    arc_like = Clothoid(0.0, 0.0, 0.0, 30.0, 1 / 500, 1 / 500)
    arc = CircularArc(0.0, 0.0, 0.0, 30.0, 1 / 500)
    assert np.allclose(arc_like.point(30.0), arc.point(30.0), atol=1e-12)
    line_like = Clothoid(0.0, 0.0, 0.3, 30.0, 0.0, 0.0)
    assert np.allclose(line_like.point(30.0), Line(0.0, 0.0, 0.3, 30.0).point(30.0), atol=1e-12)


def test_horizontal_alignment_chain_and_lookup():
    line = Line(0.0, 0.0, 0.0, 100.0)
    spiral = Clothoid(*line.end, line.heading_end, 40.0, 0.0, 1 / 300)
    arc = CircularArc(*spiral.end, spiral.heading_end, 120.0, 1 / 300)
    h = HorizontalAlignment((line, spiral, arc), station_start=1000.0)
    assert h.stations.tolist() == pytest.approx([1000.0, 1100.0, 1140.0, 1260.0])
    assert h.length == pytest.approx(260.0)
    for j in h.junctions():
        assert j.gap_m < 1e-9
        assert j.heading_jump_rad < 1e-12
    idx, local = h.locate([1000.0, 1099.999, 1100.0, 1140.0, 1260.0, 5000.0])
    assert idx.tolist() == [0, 0, 1, 2, 2, 2]
    assert local.tolist() == pytest.approx([0.0, 99.999, 0.0, 0.0, 120.0, 120.0])
    p = h.point([1100.0, 1140.0])
    assert p[0].tolist() == pytest.approx(line.end.tolist())
    assert p[1].tolist() == pytest.approx(spiral.end.tolist())
    assert h.curvature([1050.0, 1120.0, 1200.0]).tolist() == pytest.approx([0.0, 0.5 / 300, 1 / 300])
    assert h.segment_index([1000.0, 1259.0]).tolist() == [0, 2]
