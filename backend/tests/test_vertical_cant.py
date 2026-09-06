import numpy as np
import pytest

from coypu_builder.domain.geometry import (
    Alignment,
    CantProfile,
    CircularArc,
    HorizontalAlignment,
    Line,
    RotationPivot,
    VerticalAlignment,
    VerticalCurve,
)
from coypu_builder.domain.lrs import frames, to_xyz


def test_parabolic_crest_curve():
    v = VerticalAlignment(
        np.array([0.0, 1000.0, 2000.0]),
        np.array([100.0, 110.0, 100.0]),
        (VerticalCurve(1000.0, 200.0, "parabolic"),),
    )
    assert v.grades.tolist() == pytest.approx([0.01, -0.01])
    assert v.elevation([0.0, 500.0, 900.0, 1000.0, 1100.0, 1500.0, 2000.0]).tolist() == pytest.approx(
        [100.0, 105.0, 109.0, 109.5, 109.0, 105.0, 100.0]
    )
    assert v.gradient([500.0, 900.0, 1000.0, 1100.0, 1500.0]).tolist() == pytest.approx(
        [0.01, 0.01, 0.0, -0.01, -0.01]
    )
    assert v.key_stations().tolist() == [0.0, 900.0, 1000.0, 1100.0, 2000.0]


def test_circular_curve_uses_length_and_matches_parabola_convention():
    circ = VerticalAlignment(
        np.array([0.0, 1000.0, 2000.0]),
        np.array([100.0, 110.0, 100.0]),
        (VerticalCurve(1000.0, 100.0, "circular", 5000.0),),
    )
    para = VerticalAlignment(
        np.array([0.0, 1000.0, 2000.0]),
        np.array([100.0, 110.0, 100.0]),
        (VerticalCurve(1000.0, 100.0, "parabolic"),),
    )
    s = np.linspace(900.0, 1100.0, 21)
    assert circ.elevation(s).tolist() == pytest.approx(para.elevation(s).tolist())
    assert circ.elevation(1000.0)[0] == pytest.approx(110.0 - 0.02 * 100.0 / 8.0)


def test_overlapping_curves_are_clipped_and_endpoints_carry_no_curve():
    v = VerticalAlignment(
        np.array([0.0, 100.0, 150.0, 400.0]),
        np.array([10.0, 12.0, 11.0, 15.0]),
        (VerticalCurve(0.0, 50.0), VerticalCurve(100.0, 200.0), VerticalCurve(150.0, 200.0)),
    )
    half = v.curve_half_lengths()
    assert half[0] == 0.0 and half[-1] == 0.0
    assert half[1] + half[2] <= 50.0 + 1e-9
    assert np.all(np.isfinite(v.elevation(np.linspace(0, 400, 401))))


def test_constant_and_single_pvi():
    v = VerticalAlignment.constant(250.0, 10.0, 20.0)
    assert v.elevation([10.0, 15.0, 20.0]).tolist() == [250.0, 250.0, 250.0]
    assert v.gradient(15.0)[0] == 0.0
    one = VerticalAlignment(np.array([5.0]), np.array([7.0]))
    assert one.elevation([0.0, 5.0, 9.0]).tolist() == [7.0, 7.0, 7.0]


def test_cant_profile_interpolation_and_roll_sign():
    c = CantProfile(np.array([0.0, 100.0, 200.0]), np.array([0.0, 150.0, -150.0]))
    assert c.cant([50.0, 150.0, 300.0]).tolist() == pytest.approx([75.0, 150.0, 150.0])
    roll_left_curve = c.roll(150.0, +1)[0]
    roll_right_curve = c.roll(150.0, -1)[0]
    assert roll_left_curve == pytest.approx(-np.arcsin(0.1))
    assert roll_right_curve == pytest.approx(np.arcsin(0.1))
    assert c.roll(150.0, 0)[0] == 0.0
    assert RotationPivot.from_landxml("insideRail") is RotationPivot.LOW_RAIL
    assert RotationPivot.from_landxml("centreline") is RotationPivot.CENTERLINE
    assert RotationPivot.from_landxml(None) is RotationPivot.LOW_RAIL


def _curved_alignment(cant_mm: float, pivot: RotationPivot, rotation: str = "ccw") -> Alignment:
    sign = 1.0 if rotation == "ccw" else -1.0
    arc = CircularArc(0.0, 0.0, 0.0, 400.0, sign / 500.0)
    h = HorizontalAlignment((arc,))
    v = VerticalAlignment.constant(100.0, 0.0, 400.0)
    c = CantProfile(np.array([0.0, 400.0]), np.array([cant_mm, cant_mm]), pivot=pivot)
    return Alignment(h, v, c)


@pytest.mark.parametrize("rotation", ["ccw", "cw"])
def test_low_rail_pivot_raises_track_plane_centre_by_half_cant(rotation):
    low = _curved_alignment(150.0, RotationPivot.LOW_RAIL, rotation)
    centre = _curved_alignment(150.0, RotationPivot.CENTERLINE, rotation)
    fl, fc = frames(low, [10.0, 200.0]), frames(centre, [10.0, 200.0])
    expected_rise = 0.75 * np.sin(np.arcsin(0.1))
    assert fl.origin[:, 2].tolist() == pytest.approx([100.0 + expected_rise] * 2)
    assert fc.origin[:, 2].tolist() == pytest.approx([100.0, 100.0])
    assert fl.roll.tolist() == pytest.approx(fc.roll.tolist())
    side = 1.0 if rotation == "ccw" else -1.0
    assert np.sign(fl.roll[0]) == -side
    # The plane normal tilts toward the inside of the curve.
    left0 = np.array([-np.sin(fl.heading[0]), np.cos(fl.heading[0]), 0.0])
    assert np.sign(fl.up[0] @ left0) == side
    # Inner (low) rail head sits exactly at the profile elevation, outer rail 150 mm higher.
    inner = to_xyz(low, [10.0], y=side * 0.75)
    outer = to_xyz(low, [10.0], y=-side * 0.75)
    assert inner[0, 2] == pytest.approx(100.0, abs=1e-12)
    assert outer[0, 2] - inner[0, 2] == pytest.approx(0.150, abs=1e-12)


def test_frames_on_straight_with_gradient_have_pitch_and_no_roll():
    h = HorizontalAlignment((Line(0.0, 0.0, np.pi / 2, 1000.0),))
    v = VerticalAlignment(np.array([0.0, 1000.0]), np.array([100.0, 110.0]))
    aln = Alignment(h, v, CantProfile.zero(0.0, 1000.0))
    fr = frames(aln, [0.0, 500.0])
    assert fr.pitch.tolist() == pytest.approx([np.arctan(0.01)] * 2)
    assert fr.roll.tolist() == [0.0, 0.0]
    assert fr.tangent[0].tolist() == pytest.approx([0.0, np.cos(np.arctan(0.01)), np.sin(np.arctan(0.01))])
    assert fr.left[0].tolist() == pytest.approx([-1.0, 0.0, 0.0])
    assert np.allclose(np.cross(fr.tangent, fr.left), fr.up)
    p = to_xyz(aln, [500.0], y=2.0, z=1.0)
    assert p[0, 0] == pytest.approx(-2.0)
    assert p[0, 2] == pytest.approx(105.0 + np.cos(np.arctan(0.01)), abs=1e-9)


def test_cant_on_straight_takes_side_from_adjacent_curve():
    line = Line(0.0, 0.0, 0.0, 100.0)
    arc = CircularArc(*line.end, 0.0, 300.0, -1 / 400.0)
    h = HorizontalAlignment((line, arc))
    v = VerticalAlignment.constant(0.0, 0.0, 400.0)
    c = CantProfile(np.array([80.0, 100.0, 400.0]), np.array([0.0, 100.0, 100.0]))
    fr = frames(Alignment(h, v, c), [90.0, 200.0])
    assert fr.cant_mm.tolist() == pytest.approx([50.0, 100.0])
    assert np.sign(fr.roll[0]) == np.sign(fr.roll[1]) == 1.0
