"""ADR 0006: the LRS model is mode-agnostic. These assertions run against the 1000 mm tram fixture,
the one place a rail-only assumption (implicit 1435 mm gauge, a branch on `Mode.HEAVY_RAIL`) would show up.
"""

import dataclasses

import numpy as np
import pytest

from coypu_builder.domain.geometry import Alignment
from coypu_builder.domain.lrs import frames, project_xy, to_xyz
from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.sampling import bake_frame_table
from fixtures.synthetic.tram_loop import SUPERELEVATION_BASE_MM


def _alignment(tram_alignments: dict, name: str) -> Alignment:
    return next(a for a in tram_alignments.values() if a.name == name)


def test_frames_roll_matches_the_1000mm_base_formula(tram_alignments):
    loop = _alignment(tram_alignments, "terminal_loop")
    s = np.linspace(loop.horizontal.stations[0], loop.horizontal.station_end, 200)
    fr = frames(loop, s)

    cant_mm = loop.cant.cant(s)
    curvature = loop.horizontal.curvature(s)
    expected = -np.sign(curvature) * np.arcsin(np.clip(cant_mm / SUPERELEVATION_BASE_MM, -1.0, 1.0))
    assert np.allclose(fr.roll, expected, atol=1e-12)
    assert np.any(cant_mm > 0)


def test_to_xyz_and_project_xy_round_trip_on_a_tram_alignment(tram_alignments):
    street = _alignment(tram_alignments, "street")
    # Stations chosen off the cant ramps: roll is zero there, so the lateral offset is not foreshortened
    # and the round trip is exact rather than merely close.
    s_true = np.array([10.0, 45.0, 130.0])
    y_true = np.array([1.0, -0.8, 0.3])
    points = to_xyz(street, s_true, y=y_true)

    s_hat, y_hat = project_xy(street, points[:, :2])
    assert s_hat.tolist() == pytest.approx(s_true.tolist(), abs=1e-9)
    assert y_hat.tolist() == pytest.approx(y_true.tolist(), abs=1e-9)

    back = to_xyz(street, s_hat, y=y_hat)
    assert np.allclose(back, points, atol=1e-9)


def test_bake_frame_table_works_on_the_tram_alignment(tram_alignments):
    loop = _alignment(tram_alignments, "terminal_loop")
    table = bake_frame_table(loop, spacing_m=2.0)
    assert len(table) > 0
    for arr in (table.origin, table.tangent, table.left, table.up):
        assert arr.shape == (len(table), 3)
    assert np.all(np.isfinite(table.origin))
    assert table.station[0] == loop.horizontal.stations[0]
    assert table.station[-1] == loop.horizontal.station_end


def test_no_frame_math_branches_on_heavy_rail_mode(tram_alignments):
    loop = _alignment(tram_alignments, "terminal_loop")
    loop_heavy = dataclasses.replace(loop, mode=Mode.HEAVY_RAIL)
    s = np.linspace(loop.horizontal.stations[0], loop.horizontal.station_end, 50)

    tram_frames = frames(loop, s)
    heavy_frames = frames(loop_heavy, s)

    assert np.array_equal(tram_frames.origin, heavy_frames.origin)
    assert np.array_equal(tram_frames.roll, heavy_frames.roll)
    assert np.array_equal(tram_frames.up, heavy_frames.up)
    assert np.array_equal(tram_frames.left, heavy_frames.left)
