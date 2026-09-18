"""Hand-built kinematics data exercising T-111 edge cases: reversed direction, a long dwell, duplicate
source timestamps and a degenerate single-row run. Not derived from any real project.
"""

from __future__ import annotations

import numpy as np

from coypu_builder.domain.kinematics import KinematicsRun, Stop


def reversed_run_raw() -> dict[str, np.ndarray]:
    """200 m travelled backwards (station descending) at 10 m/s — raw arrays, for `normalise_run`."""
    return {
        "station_m": np.linspace(1200.0, 1000.0, 21),
        "time_s": np.linspace(0.0, 20.0, 21),
        "speed_ms": np.full(21, 10.0),
        "accel_ms2": np.zeros(21),
    }


def dwell_run(dwell_s: float = 120.0) -> KinematicsRun:
    """Accelerate over 500 m, sit at a `dwell_s` dwell, then depart another 400 m."""
    t0 = np.array([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    s0 = np.array([0.0, 100.0, 200.0, 300.0, 400.0, 500.0])
    v0 = np.array([0.0, 10.0, 10.0, 10.0, 10.0, 0.0])

    t_dwell_end = t0[-1] + dwell_s
    t1 = t_dwell_end + np.array([10.0, 20.0, 30.0, 40.0])
    s1 = np.array([600.0, 700.0, 800.0, 900.0])
    v1 = np.array([10.0, 10.0, 10.0, 10.0])

    time_s = np.concatenate([t0, [t_dwell_end], t1])
    station_m = np.concatenate([s0, [500.0], s1])
    speed_ms = np.concatenate([v0, [0.0], v1])
    accel_ms2 = np.zeros_like(time_s)

    return KinematicsRun(
        station_m=station_m,
        time_s=time_s,
        speed_ms=speed_ms,
        accel_ms2=accel_ms2,
        stops=(Stop(station_m=500.0, dwell_s=dwell_s, name="mid_stop"),),
        name="synthetic_dwell_run",
    )


def duplicate_time_run() -> KinematicsRun:
    """A zero-length time interval (two rows sharing a timestamp) that must collapse before interpolation."""
    return KinematicsRun(
        station_m=np.array([0.0, 50.0, 50.0, 100.0, 150.0]),
        time_s=np.array([0.0, 5.0, 5.0, 10.0, 15.0]),
        speed_ms=np.array([0.0, 10.0, 10.0, 10.0, 10.0]),
        accel_ms2=np.zeros(5),
        name="synthetic_duplicate_time_run",
    )


def single_row_run() -> KinematicsRun:
    return KinematicsRun(
        station_m=np.array([250.0]),
        time_s=np.array([0.0]),
        speed_ms=np.array([0.0]),
        accel_ms2=np.array([0.0]),
        name="synthetic_single_row_run",
    )
