"""Reference trainset chain: poses every car and bogie of a `Trainset` along an `Alignment`.

Not on the runtime path (ADR 0007) -- the client (T-123) owns the per-frame implementation; this module
exists to be the float64 reference it is measured against (`shared/golden/trainset_chain.json`) and to serve
headless/CLI use. The algorithm is written up in full, without Python, in
`docs/data-contracts/trainset-chain.md`.

Station layout is walked backwards from `station_lead` (the front face of the first car), car by car, along
the absolute station axis; every pivot station across the whole batch is then evaluated with a single
`lrs.frames` call. Each car body is rigid and spans its two bogie pivots: it *chords* between them rather
than following the tangent at its midpoint, so it visibly cuts across a curve the way a real vehicle does.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.geometry.cant import RotationPivot
from coypu_builder.domain.lrs import TrackFrames, frames
from coypu_builder.domain.model.trainset import Trainset

_DEGENERATE_CHORD_EPS_M = 1e-9
"""Below this pivot-to-pivot chord length there is no chord to take a direction from -- either a genuine
zero `bogie_pivot_distance_m`, or both pivots clamped onto the same alignment endpoint."""


@dataclass(frozen=True)
class BogiePose:
    station: float
    position: np.ndarray  # (3,) (E, N, H)
    tangent: np.ndarray  # (3,)
    left: np.ndarray  # (3,)
    up: np.ndarray  # (3,)
    roll: float
    pitch: float


@dataclass(frozen=True)
class CarPose:
    index: int
    station_front: float
    station_rear: float
    lead: BogiePose
    trail: BogiePose
    origin: np.ndarray  # (3,) body centre (E, N, H)
    forward: np.ndarray  # (3,) unit, chord direction, already signed by travel direction
    left: np.ndarray  # (3,) unit
    up: np.ndarray  # (3,) unit
    roll: float


@dataclass(frozen=True)
class TrainsetPose:
    station_lead: float
    direction: int
    cars: tuple[CarPose, ...]
    clamped: bool  # any pivot, front or rear face fell outside [station_start, station_end]


def pose_trainset(
    alignment: Alignment,
    trainset: Trainset,
    station_lead: float,
    direction: int = 1,
    pivot: RotationPivot | None = None,
) -> TrainsetPose:
    stations = np.array([station_lead], dtype=np.float64)
    return pose_trainset_many(alignment, trainset, stations, direction, pivot)[0]


def pose_trainset_many(
    alignment: Alignment,
    trainset: Trainset,
    station_lead: np.ndarray,
    direction: int = 1,
    pivot: RotationPivot | None = None,
) -> tuple[TrainsetPose, ...]:
    lead_stations = np.atleast_1d(np.asarray(station_lead, dtype=np.float64))
    d = float(direction)
    cars = trainset.cars
    n_sets, n_cars = len(lead_stations), len(cars)

    lengths = np.array([car.length_m for car in cars], dtype=np.float64)
    pivots = np.array([car.bogie_pivot_distance_m for car in cars], dtype=np.float64)
    gap = float(trainset.coupling_gap_m)

    front = np.empty((n_sets, n_cars))
    rear = np.empty((n_sets, n_cars))
    lead_pivot = np.empty((n_sets, n_cars))
    trail_pivot = np.empty((n_sets, n_cars))

    f = lead_stations.copy()
    for i in range(n_cars):
        front[:, i] = f
        r = f - d * lengths[i]
        p_lead = f - d * (lengths[i] - pivots[i]) / 2.0
        p_trail = p_lead - d * pivots[i]
        rear[:, i] = r
        lead_pivot[:, i] = p_lead
        trail_pivot[:, i] = p_trail
        f = r - d * gap

    s0, s1 = alignment.station_start, alignment.station_end
    raw = np.stack([front, rear, lead_pivot, trail_pivot], axis=-1)  # (n_sets, n_cars, 4)
    clamped_per_set = np.any((raw < s0) | (raw > s1), axis=(1, 2))

    front_c = np.clip(front, s0, s1)
    rear_c = np.clip(rear, s0, s1)
    lead_c = np.clip(lead_pivot, s0, s1)
    trail_c = np.clip(trail_pivot, s0, s1)

    n = n_sets * n_cars
    fr = frames(alignment, np.concatenate([lead_c.ravel(), trail_c.ravel()]), pivot)

    sets = []
    for k in range(n_sets):
        car_poses = tuple(
            _car_pose(
                i,
                front_c[k, i],
                rear_c[k, i],
                _bogie_pose(fr, k * n_cars + i),
                _bogie_pose(fr, n + k * n_cars + i),
                d,
            )
            for i in range(n_cars)
        )
        pose = TrainsetPose(float(lead_stations[k]), int(direction), car_poses, bool(clamped_per_set[k]))
        sets.append(pose)
    return tuple(sets)


def _bogie_pose(fr: TrackFrames, idx: int) -> BogiePose:
    return BogiePose(
        station=float(fr.station[idx]),
        position=fr.origin[idx].copy(),
        tangent=fr.tangent[idx].copy(),
        left=fr.left[idx].copy(),
        up=fr.up[idx].copy(),
        roll=float(fr.roll[idx]),
        pitch=float(fr.pitch[idx]),
    )


def _car_pose(
    index: int, station_front: float, station_rear: float, lead: BogiePose, trail: BogiePose, d: float
) -> CarPose:
    chord = lead.position - trail.position
    chord_norm = float(np.linalg.norm(chord))
    if chord_norm < _DEGENERATE_CHORD_EPS_M:
        # Zero pivot distance, or both pivots clamped onto the same endpoint: no chord to take a
        # direction from, so fall back to the frame at that single station, signed by travel direction.
        forward = d * lead.tangent
    else:
        forward = chord / chord_norm

    origin = (lead.position + trail.position) / 2.0
    roll = (lead.roll + trail.roll) / 2.0
    up_raw = lead.up + trail.up
    up_raw = up_raw / np.linalg.norm(up_raw)
    up = up_raw - np.dot(up_raw, forward) * forward
    up = up / np.linalg.norm(up)
    left = np.cross(up, forward)

    return CarPose(
        index=index,
        station_front=float(station_front),
        station_rear=float(station_rear),
        lead=lead,
        trail=trail,
        origin=origin,
        forward=forward,
        left=left,
        up=up,
        roll=float(roll),
    )
