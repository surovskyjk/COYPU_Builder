"""Baking a `KinematicsRun` into a time-uniform table the client indexes in O(1) at 60 Hz (ADR 0007)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.kinematics.run import KinematicsRun, RunDirection, Stop


@dataclass(frozen=True)
class RunTable:
    """Time-uniform resample of a `KinematicsRun`; the client indexes it in O(1) at 60 Hz (ADR 0007)."""

    dt: float
    time_start: float  # always 0.0 for now, kept explicit for future partial tables
    station: np.ndarray  # (m,)
    speed: np.ndarray  # (m,)
    accel: np.ndarray  # (m,)
    f_traction: np.ndarray | None
    f_braking: np.ndarray | None
    f_resistance: np.ndarray | None
    direction: RunDirection
    stops: tuple[Stop, ...]

    def __len__(self) -> int:
        return len(self.station)

    @property
    def duration(self) -> float:
        return self.time_start + self.dt * (len(self) - 1)


def _collapse_duplicate_times(time_s: np.ndarray, *columns: np.ndarray | None) -> list[np.ndarray | None]:
    """Keep the last sample of every run of equal (non-decreasing, possibly repeated) time values, so
    `np.interp` receives a strictly increasing x-array."""
    keep = np.concatenate([np.diff(time_s) > 0.0, [True]]) if len(time_s) > 1 else np.array([True])
    return [time_s[keep]] + [None if c is None else c[keep] for c in columns]


def bake_run_table(run: KinematicsRun, dt: float = 0.05) -> RunTable:
    time_u, station_u, speed_u, accel_u, trac_u, brake_u, res_u = _collapse_duplicate_times(
        run.time_s,
        run.station_m,
        run.speed_ms,
        run.accel_ms2,
        run.f_traction_kn,
        run.f_braking_kn,
        run.f_resistance_kn,
    )

    duration = float(time_u[-1])
    n_intervals = max(1, round(duration / dt)) if duration > 0.0 and len(time_u) > 1 else 0
    dt_actual = duration / n_intervals if n_intervals > 0 else dt
    n_rows = n_intervals + 1

    grid_t = np.arange(n_rows, dtype=np.float64) * dt_actual
    grid_t[-1] = duration  # remove float round-off drift; the last row must land exactly on the source end

    def resample(values: np.ndarray | None) -> np.ndarray | None:
        return None if values is None else np.interp(grid_t, time_u, values)

    return RunTable(
        dt=dt_actual,
        time_start=0.0,
        station=resample(station_u),
        speed=resample(speed_u),
        accel=resample(accel_u),
        f_traction=resample(trac_u),
        f_braking=resample(brake_u),
        f_resistance=resample(res_u),
        direction=run.direction,
        stops=run.stops,
    )
