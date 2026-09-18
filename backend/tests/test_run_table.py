import numpy as np
import pytest

from coypu_builder.domain.kinematics import RunDirection, bake_run_table, normalise_run
from coypu_builder.io.coypu.kinematics_csv import read_stops_csv
from fixtures.synthetic.kinematics import duplicate_time_run, dwell_run, reversed_run_raw, single_row_run


def _assert_clean(table):
    assert np.all(np.isfinite(table.station))
    assert np.all(np.isfinite(table.speed))
    assert np.all(np.isfinite(table.accel))
    assert np.all(table.speed >= 0.0)
    assert len(table) == len(table.station) == len(table.speed) == len(table.accel)


# --- AC6: no NaN, no infinity, no negative speed --------------------------------------------------------


def test_kralupy_run_bakes_clean(kralupy_project, kralupy_stops_csv):
    stops = read_stops_csv(kralupy_stops_csv)
    run = kralupy_project.kinematics_run(0, stops=stops)
    table = bake_run_table(run)
    _assert_clean(table)
    assert table.direction == RunDirection.FORWARD
    assert len(table) > 1
    assert table.duration == pytest.approx(run.time_s[-1])


def test_dwell_run_bakes_clean_with_no_speed_spike():
    run = dwell_run(dwell_s=120.0)
    table = bake_run_table(run)
    _assert_clean(table)

    # Every row whose nominal time falls inside the [approach-end, dwell-end] window must stay within the
    # range spanned by the two source samples bracketing the dwell (0.0 m/s on both sides) — linear
    # interpolation between two source knots cannot overshoot them, so this also proves there is no spike.
    grid_t = np.arange(len(table)) * table.dt
    in_dwell = (grid_t >= 50.0) & (grid_t <= 50.0 + 120.0)
    assert np.any(in_dwell)
    assert np.all(table.speed[in_dwell] <= 10.0 + 1e-9)
    assert np.allclose(table.station[in_dwell], 500.0, atol=1e-6)


def test_duplicate_time_run_bakes_clean():
    table = bake_run_table(duplicate_time_run())
    _assert_clean(table)


def test_single_row_run_bakes_clean():
    run = single_row_run()
    table = bake_run_table(run)
    _assert_clean(table)
    assert len(table) == 1
    assert table.duration == 0.0
    assert table.station[0] == pytest.approx(250.0)


# --- AC7: interpolating the baked table back at the source times reproduces source stations -------------


def test_baked_table_reproduces_source_stations_outside_dwells(kralupy_project, kralupy_stops_csv):
    stops = read_stops_csv(kralupy_stops_csv)
    run = kralupy_project.kinematics_run(0, stops=stops)
    table = bake_run_table(run)

    grid_t = np.arange(len(table)) * table.dt
    grid_t[-1] = table.duration
    reconstructed = np.interp(run.time_s, grid_t, table.station)
    error = np.abs(reconstructed - run.station_m)

    # Linear resampling reconstructs any point exactly, UNLESS the two baked grid points bracketing it
    # straddle a kink (an adjacent-segment slope change) sharp enough that dt=0.05 s doesn't resolve it.
    # That only happens where the source's own 1 m grid is coarsest relative to how fast speed is
    # changing: leaving standstill at the very start of the run, and the single sample on each side of
    # every dwell (arriving at creep speed / departing back to line speed) — i.e. next to a stop, not at
    # one. Those ~20 points (out of 18 186) peak at ~1.4 cm; everywhere else is at least 25x tighter.
    station_index = {float(s): i for i, s in enumerate(run.station_m)}
    near_transition = {0, 1}
    for stop in run.stops:
        i = station_index[stop.station_m]
        near_transition.update({i - 1, i, i + 1})
    near_transition_mask = np.zeros(len(run.station_m), dtype=bool)
    near_transition_mask[sorted(near_transition)] = True

    assert np.max(error[~near_transition_mask]) < 0.01
    assert np.max(error[near_transition_mask]) < 0.02


def test_reversed_run_bakes_correctly():
    raw = reversed_run_raw()
    run = normalise_run(raw["station_m"], raw["time_s"], raw["speed_ms"], raw["accel_ms2"])
    table = bake_run_table(run)

    _assert_clean(table)
    assert table.direction == RunDirection.REVERSE
    assert table.station[0] == pytest.approx(1200.0)
    assert table.station[-1] == pytest.approx(1000.0)
