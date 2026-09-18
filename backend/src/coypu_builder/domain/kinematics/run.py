"""Normalised COYPU train runs: one simulated trip per vehicle, in SI units and on an ascending time axis.

COYPU integrates `t[i] = t[i-1] + ds/v_avg + dwell[i]`, capping the speed used for that integral at
`CREEP_SPEED_MS` near stops (docs/data-contracts/coypu-kinematics.md). A station stop therefore shows up as
a time jump at (almost) constant station, never as a change of sign or a gap in the station axis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

DWELL_THRESHOLD_S_DEFAULT = 3.0
"""Below this, a slow 1-metre grid step at the creep-speed floor (up to 2 s) is normal, not a stop."""

STATION_EPSILON_M_DEFAULT = 1.5
"""Above the real 1 m simulation grid step, so a genuine dwell row is never rejected on this alone."""

CREEP_SPEED_MS = 0.5
"""COYPU's speed floor for the time integral near stops (see the data contract); used to separate the
dwell portion of a time jump from the minimum travel time the source's own station step would take."""

STOP_NAME_MATCH_TOLERANCE_M = 5.0
"""How close a detected/kept stop must be to a reference stop (from a stops CSV) to inherit its name."""


class RunDirection(IntEnum):
    FORWARD = 1  # station increases with time
    REVERSE = -1  # station decreases with time


@dataclass(frozen=True)
class Stop:
    station_m: float
    dwell_s: float = 30.0
    name: str = ""


@dataclass(frozen=True)
class KinematicsRun:
    """One simulated train run, normalised to SI and to an ascending time axis."""

    station_m: np.ndarray  # (n,) absolute metres, LandXML staStart basis
    time_s: np.ndarray  # (n,) non-decreasing, starts at 0.0
    speed_ms: np.ndarray  # (n,) non-negative magnitude
    accel_ms2: np.ndarray  # (n,)
    f_traction_kn: np.ndarray | None = None
    f_braking_kn: np.ndarray | None = None
    f_resistance_kn: np.ndarray | None = None
    direction: RunDirection = RunDirection.FORWARD
    stops: tuple[Stop, ...] = ()
    name: str = ""
    vehicle_index: int = 0
    warnings: tuple[str, ...] = ()


def _stops_from_dwell_array(station_m: np.ndarray, dwell_s: np.ndarray) -> tuple[Stop, ...]:
    idx = np.nonzero(dwell_s > 0.0)[0]
    return tuple(Stop(station_m=float(station_m[i]), dwell_s=float(dwell_s[i])) for i in idx)


def _detect_dwells_from_time_jumps(
    station_m: np.ndarray,
    time_s: np.ndarray,
    dwell_threshold_s: float,
    station_epsilon_m: float,
) -> tuple[Stop, ...]:
    """Only the time jump is available (both CSV dialects carry no dwell column): a step whose time delta
    exceeds what the creep-speed floor could explain for its station delta, with that delta itself small,
    is a dwell. The excess over the floor's travel time is reported as the dwell duration.
    """
    if len(time_s) < 2:
        return ()
    dt = np.diff(time_s)
    ds = np.abs(np.diff(station_m))
    floor_travel_s = ds / CREEP_SPEED_MS
    excess_s = dt - floor_travel_s
    mask = (excess_s > dwell_threshold_s) & (ds < station_epsilon_m)
    return tuple(
        Stop(station_m=float(station_m[i + 1]), dwell_s=float(excess_s[i])) for i in np.nonzero(mask)[0]
    )


def _match_names(stops: tuple[Stop, ...], reference: tuple[Stop, ...]) -> tuple[Stop, ...]:
    if not stops or not reference:
        return stops
    ref_station = np.array([r.station_m for r in reference])
    matched = []
    for stop in stops:
        deltas = np.abs(ref_station - stop.station_m)
        j = int(np.argmin(deltas))
        name = reference[j].name if deltas[j] <= STOP_NAME_MATCH_TOLERANCE_M else stop.name
        matched.append(Stop(stop.station_m, stop.dwell_s, name))
    return tuple(matched)


def normalise_run(
    station_m: np.ndarray,
    time_s: np.ndarray | None,
    speed_ms: np.ndarray,
    accel_ms2: np.ndarray,
    *,
    f_traction_kn: np.ndarray | None = None,
    f_braking_kn: np.ndarray | None = None,
    f_resistance_kn: np.ndarray | None = None,
    dwell_s: np.ndarray | None = None,
    stops: Sequence[Stop] = (),
    name: str = "",
    vehicle_index: int = 0,
    dwell_threshold_s: float = DWELL_THRESHOLD_S_DEFAULT,
    station_epsilon_m: float = STATION_EPSILON_M_DEFAULT,
) -> KinematicsRun:
    """Build a `KinematicsRun` from already-SI arrays in source row order.

    `time_s=None` or empty means the source carried no time series at all; that is rejected rather than
    fabricated. When `dwell_s` is supplied (the `.coypu` archive's own `kinematicsDwellTimesS_i`) it is
    kept as the source of stops; otherwise stops are inferred from time jumps, see
    `_detect_dwells_from_time_jumps`.
    """
    station_m = np.asarray(station_m, dtype=np.float64)
    if time_s is None or np.size(time_s) == 0:
        raise ValueError("kinematics run has no time column; refusing to fabricate a time base")
    time_s = np.asarray(time_s, dtype=np.float64)
    if time_s.shape != station_m.shape:
        raise ValueError(f"time_s has {time_s.size} samples, station_m has {station_m.size}")

    warnings: list[str] = []
    order = np.arange(len(time_s))
    if np.any(np.diff(time_s) < 0.0):
        order = np.argsort(time_s, kind="stable")
        warnings.append("time column was not monotonic; stable-sorted by time")

    def reorder(values: np.ndarray | None) -> np.ndarray | None:
        return None if values is None else np.asarray(values, dtype=np.float64)[order]

    station_m = station_m[order]
    time_s = reorder(time_s)
    speed_ms = np.abs(reorder(speed_ms))
    accel_ms2 = reorder(accel_ms2)
    f_traction_kn = reorder(f_traction_kn)
    f_braking_kn = reorder(f_braking_kn)
    f_resistance_kn = reorder(f_resistance_kn)
    dwell_s = reorder(dwell_s)

    time_s = time_s - time_s[0]

    direction = RunDirection.FORWARD if station_m[-1] >= station_m[0] else RunDirection.REVERSE

    if dwell_s is not None:
        detected = _stops_from_dwell_array(station_m, dwell_s)
    else:
        detected = _detect_dwells_from_time_jumps(station_m, time_s, dwell_threshold_s, station_epsilon_m)
    detected = _match_names(detected, tuple(stops))

    return KinematicsRun(
        station_m=station_m,
        time_s=time_s,
        speed_ms=speed_ms,
        accel_ms2=accel_ms2,
        f_traction_kn=f_traction_kn,
        f_braking_kn=f_braking_kn,
        f_resistance_kn=f_resistance_kn,
        direction=direction,
        stops=detected,
        name=name,
        vehicle_index=vehicle_index,
        warnings=tuple(warnings),
    )
