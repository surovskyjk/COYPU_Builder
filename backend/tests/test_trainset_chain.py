"""Acceptance tests for the T-112 trainset chain (docs/tasks/task_112_trainset_chain.md) and its Follow-up
F10 (make the golden exercise cant roll).

`kralupy`, `kralupy_project`, `krovak`, `tram_network`, `tram_alignments` come from conftest.py.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import numpy as np
import pytest

from coypu_builder.domain.geometry import Alignment, HorizontalAlignment, Line
from coypu_builder.domain.kinematics import trainset as trainset_module
from coypu_builder.domain.kinematics.trainset import pose_trainset, pose_trainset_many
from coypu_builder.domain.lrs import project_xy
from coypu_builder.domain.model.ids import new_id
from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.model.trainset import Trainset
from coypu_builder.domain.model.vehicle import CarSpec
from coypu_builder.io.catalogue.vehicles import load_catalogue

# --- fixtures and helpers --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalogue():
    return load_catalogue()


@pytest.fixture(scope="module")
def dmu_trainset(catalogue):
    """A three-car consist (same railcar repeated) so inter-car geometry is exercised."""
    return Trainset.from_spec(catalogue["dmu_br650_cd840"], units=3)


def _straight_alignment(length: float = 300.0) -> Alignment:
    line = Line(0.0, 0.0, 0.0, length)
    return Alignment.flat(HorizontalAlignment((line,)))


def _car(**overrides) -> CarSpec:
    defaults = dict(
        name="c",
        length_m=10.0,
        width_m=3.0,
        height_m=4.0,
        floor_height_m=0.6,
        bogie_pivot_distance_m=6.0,
        bogie_wheelbase_m=2.0,
        wheel_diameter_m=0.8,
    )
    defaults.update(overrides)
    return CarSpec(**defaults)


def _trainset(cars, coupling_gap_m: float = 1.0) -> Trainset:
    return Trainset(
        id=new_id(),
        spec_key="test",
        cars=tuple(cars),
        coupling_gap_m=coupling_gap_m,
        mode=Mode.HEAVY_RAIL,
        gauge_mm=1435.0,
    )


# --- criterion 1: bogie positions lie on the alignment ---------------------------------------------------


def test_bogie_positions_project_back_to_their_pivot_station(kralupy, dmu_trainset):
    aln = kralupy.alignment
    for station_lead in (200.0, 3850.0, 8260.0, 16850.0):
        pose = pose_trainset(aln, dmu_trainset, station_lead)
        for car in pose.cars:
            for bogie in (car.lead, car.trail):
                s, y = project_xy(aln, bogie.position[:2])
                assert s[0] == pytest.approx(bogie.station, abs=1e-3)
                assert y[0] == pytest.approx(0.0, abs=1e-3)


# --- criterion 2: body centre lies inside the sharpest curve, matching the chord's mid-ordinate ----------


def test_body_centre_offset_matches_mid_ordinate_at_sharpest_curve(kralupy, dmu_trainset):
    aln = kralupy.alignment
    station_lead = 16850.0
    pose = pose_trainset(aln, dmu_trainset, station_lead)
    car = pose.cars[0]  # front car: fully inside the constant-curvature arc at this station_lead
    pivot_m = dmu_trainset.cars[car.index].bogie_pivot_distance_m

    curvature = float(aln.horizontal.curvature(np.array([station_lead]))[0])
    radius = 1.0 / abs(curvature)
    half_chord = pivot_m / 2.0
    mid_ordinate = radius - np.sqrt(radius**2 - half_chord**2)

    s, y = project_xy(aln, car.origin[:2])
    assert y[0] != 0.0
    # A curve's chord always lies between the arc and its centre -- offset is signed the same way as
    # curvature (positive/left for a left-hand curve, negative/right for a right-hand one).
    assert np.sign(y[0]) == np.sign(curvature)
    assert abs(y[0]) == pytest.approx(mid_ordinate, abs=1e-3)


# --- criterion 3: on a straight with no cant/gradient, forward == tangent, roll == 0 ----------------------


def test_forward_matches_tangent_and_roll_zero_on_a_flat_straight():
    aln = _straight_alignment()
    ts = _trainset([_car()])
    pose = pose_trainset(aln, ts, station_lead=150.0, direction=1)
    car = pose.cars[0]
    assert np.allclose(car.forward, car.lead.tangent, atol=1e-9)
    assert car.roll == pytest.approx(0.0, abs=1e-9)


# --- criterion 4: car spacing is conserved ----------------------------------------------------------------


@pytest.mark.parametrize("direction", [1, -1])
def test_car_spacing_is_conserved(direction):
    aln = _straight_alignment()
    ts = _trainset([_car(length_m=10.0), _car(length_m=12.0), _car(length_m=8.0)], coupling_gap_m=0.75)
    pose = pose_trainset(aln, ts, station_lead=150.0, direction=direction)
    for a, b in zip(pose.cars, pose.cars[1:], strict=False):
        expected_next_front = a.station_rear - direction * ts.coupling_gap_m
        assert b.station_front == pytest.approx(expected_next_front, abs=1e-9)


# --- criterion 5: direction = -1 mirrors the layout and reverses each body's forward ----------------------


def test_reverse_direction_extends_toward_increasing_station_and_flips_forward():
    aln = _straight_alignment()
    ts = _trainset([_car(), _car()], coupling_gap_m=0.5)
    pose = pose_trainset(aln, ts, station_lead=150.0, direction=-1)

    assert pose.cars[1].station_front > pose.cars[0].station_front  # consist extends toward +station

    tangent = pose.cars[0].lead.tangent
    for car in pose.cars:
        assert np.dot(car.forward, tangent) < 0.0  # each body faces decreasing station
        assert np.allclose(car.forward, -tangent, atol=1e-9)


# --- criterion 6: clamping ------------------------------------------------------------------------------


def test_clamping_near_either_end_is_flagged_and_stays_finite(kralupy, dmu_trainset):
    aln = kralupy.alignment
    consist_length = dmu_trainset.length_m

    near_start = pose_trainset(aln, dmu_trainset, station_lead=aln.station_start + consist_length / 2)
    assert near_start.clamped
    near_end = pose_trainset(aln, dmu_trainset, station_lead=aln.station_end + 10.0)
    assert near_end.clamped

    well_inside = pose_trainset(aln, dmu_trainset, station_lead=8260.0)
    assert not well_inside.clamped

    for pose in (near_start, near_end):
        for car in pose.cars:
            for value in (car.origin, car.forward, car.left, car.up, car.lead.position, car.trail.position):
                assert np.all(np.isfinite(value))
            assert np.isfinite(car.roll)


# --- criterion 7: the tram fixture, mode-agnostically, with a two-car consist and a 1000 mm base ---------


def test_tram_fixture_poses_with_a_two_car_consist(catalogue, tram_alignments):
    tram_spec = catalogue["tram_generic"]
    two_car = Trainset(
        id=new_id(),
        spec_key=tram_spec.key,
        cars=tram_spec.cars[:2],
        coupling_gap_m=tram_spec.coupling_gap_m,
        mode=tram_spec.mode,
        gauge_mm=tram_spec.gauge_mm,
    )
    assert two_car.gauge_mm == pytest.approx(1000.0)
    street_id = next(iter(tram_alignments))
    aln = tram_alignments[street_id]
    assert aln.cant.superelevation_base_mm == pytest.approx(1100.0)

    saw_nonzero_roll = False
    for station_lead in np.linspace(two_car.length_m + 1.0, aln.station_end - 1.0, 15):
        pose = pose_trainset(aln, two_car, station_lead=float(station_lead))
        for car in pose.cars:
            for bogie in (car.lead, car.trail):
                s, y = project_xy(aln, bogie.position[:2])
                assert s[0] == pytest.approx(bogie.station, abs=1e-3)
                assert y[0] == pytest.approx(0.0, abs=1e-3)
            if abs(car.roll) > 1e-6:
                saw_nonzero_roll = True
    assert saw_nonzero_roll  # the street's cant ramp (60-90 m) was actually exercised


def test_module_has_no_mode_branch():
    source = inspect.getsource(trainset_module)
    assert "Mode" not in source


# --- criterion 8: pose_trainset_many agrees with repeated pose_trainset calls ----------------------------


def test_pose_trainset_many_matches_repeated_single_calls(kralupy, dmu_trainset):
    aln = kralupy.alignment
    stations = np.array([200.0, 3850.0, 8260.0, 16850.0])
    batched = pose_trainset_many(aln, dmu_trainset, stations, direction=1)
    singles = [pose_trainset(aln, dmu_trainset, float(s), direction=1) for s in stations]

    for b, s in zip(batched, singles, strict=True):
        assert b.clamped == s.clamped
        for cb, cs in zip(b.cars, s.cars, strict=True):
            assert np.allclose(cb.origin, cs.origin, atol=1e-12)
            assert np.allclose(cb.forward, cs.forward, atol=1e-12)
            assert np.allclose(cb.up, cs.up, atol=1e-12)
            assert np.allclose(cb.left, cs.left, atol=1e-12)
            assert cb.roll == pytest.approx(cs.roll, abs=1e-12)
            for bb, bs in zip((cb.lead, cb.trail), (cs.lead, cs.trail), strict=True):
                assert np.allclose(bb.position, bs.position, atol=1e-12)
                assert bb.station == pytest.approx(bs.station, abs=1e-12)


# --- degenerate zero-pivot-distance car --------------------------------------------------------------


@pytest.mark.parametrize("direction", [1, -1])
def test_zero_pivot_distance_car_falls_back_to_the_centre_frame(direction):
    aln = _straight_alignment()
    ts = _trainset([_car(bogie_pivot_distance_m=0.0)])
    pose = pose_trainset(aln, ts, station_lead=150.0, direction=direction)
    car = pose.cars[0]

    assert car.lead.station == pytest.approx(car.trail.station, abs=1e-12)
    assert np.allclose(car.lead.position, car.trail.position, atol=1e-12)
    assert np.isclose(np.linalg.norm(car.forward), 1.0)
    assert np.allclose(car.forward, direction * car.lead.tangent, atol=1e-9)
    centre_station = 150.0 - direction * 10.0 / 2.0
    assert car.lead.station == pytest.approx(centre_station, abs=1e-9)


def test_zero_pivot_distance_car_stays_finite_even_when_clamped(kralupy, catalogue):
    aln = kralupy.alignment
    ts = _trainset([_car(bogie_pivot_distance_m=0.0)])
    pose = pose_trainset(aln, ts, station_lead=aln.station_end + 50.0)
    car = pose.cars[0]
    assert pose.clamped
    assert np.all(np.isfinite(car.forward))
    assert np.isclose(np.linalg.norm(car.forward), 1.0)


# --- Follow-up F10: the golden's tram block actually exercises cant roll ---------------------------------

GOLDEN_TRAINSET_CHAIN = Path(__file__).resolve().parents[2] / "shared" / "golden" / "trainset_chain.json"


def _tram_block() -> dict:
    with GOLDEN_TRAINSET_CHAIN.open(encoding="utf-8") as f:
        payload = json.load(f)
    assert "tram_block" in payload, "regenerate the golden with `uv run python ../tools/make_golden.py`"
    return payload["tram_block"]


def test_kralupy_block_still_has_nine_samples_all_zero_roll():
    """Guards the *other* half of F10's contract: the tram block must be additive, so the pre-existing
    Kralupy block (whose cant is a documented zero placeholder) is untouched."""
    with GOLDEN_TRAINSET_CHAIN.open(encoding="utf-8") as f:
        payload = json.load(f)
    assert len(payload["samples"]) == 9
    for sample in payload["samples"]:
        for car in sample["cars"]:
            assert car["body"]["roll"] == pytest.approx(0.0, abs=1e-9)


def test_golden_tram_block_exercises_non_zero_roll_and_pivot_roll_divergence():
    """F10 acceptance criterion 1, asserted directly against the committed golden artifact (not just the
    domain code), so this cannot silently regress if the fixture or the golden-writing code changes."""
    tram_block = _tram_block()
    assert len(tram_block["samples"]) >= 4

    samples_with_nonzero_roll = 0
    max_pivot_roll_divergence = 0.0
    for sample in tram_block["samples"]:
        any_nonzero = False
        for car in sample["cars"]:
            lead_roll = car["lead_pivot"]["roll"]
            trail_roll = car["trail_pivot"]["roll"]
            if abs(car["body"]["roll"]) > 1e-9:
                any_nonzero = True
            max_pivot_roll_divergence = max(max_pivot_roll_divergence, abs(lead_roll - trail_roll))
        if any_nonzero:
            samples_with_nonzero_roll += 1

    assert samples_with_nonzero_roll >= 4
    assert max_pivot_roll_divergence > 1e-4


def test_golden_tram_block_matches_a_fresh_pose_of_the_same_fixture(tram_alignments, catalogue):
    """Cross-check the committed golden against an independently-built pose (fresh `build_tram_loop()` +
    `pose_trainset`), so a stale or hand-edited golden would be caught, not just an all-zero one."""
    tram_block = _tram_block()
    base_h = tram_block["base_point"]["height"]

    spec = catalogue["tram_generic"]
    two_car = Trainset(
        id=new_id(),
        spec_key=spec.key,
        cars=spec.cars[:2],
        coupling_gap_m=spec.coupling_gap_m,
        mode=spec.mode,
        gauge_mm=spec.gauge_mm,
    )
    street_id, loop_id = list(tram_alignments.keys())
    alignments_by_label = {"street": tram_alignments[street_id], "loop": tram_alignments[loop_id]}

    for sample in tram_block["samples"]:
        aln = alignments_by_label[sample["alignment"]]
        pose = pose_trainset(aln, two_car, sample["station_lead"], direction=sample["direction"])
        assert pose.clamped == sample["clamped"]
        for car, expected in zip(pose.cars, sample["cars"], strict=True):
            assert car.lead.roll == pytest.approx(expected["lead_pivot"]["roll"], abs=1e-9)
            assert car.trail.roll == pytest.approx(expected["trail_pivot"]["roll"], abs=1e-9)
            assert car.roll == pytest.approx(expected["body"]["roll"], abs=1e-9)
            godot_h = car.origin[2] - base_h
            assert godot_h == pytest.approx(expected["body"]["godot_position"][1], abs=1e-9)
