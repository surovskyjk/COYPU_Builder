"""Linear referencing: (s, y, z) ⇄ (E, N, H) and moving track frames along an alignment.

Frame convention (domain axes E, N, H): `tangent` follows the alignment including pitch, `left` is the
track-plane lateral axis (positive y = left of travel), `up` is normal to the track plane. Cant roll is
applied about the tangent; the rotation pivot decides where the plane centre sits vertically.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry._common import CURVATURE_EPS, s_array
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.geometry.cant import RotationPivot


@dataclass(frozen=True)
class TrackFrames:
    station: np.ndarray  # (n,)   absolute stations [m]
    origin: np.ndarray  # (n,3)  track-plane centre (E, N, H)
    tangent: np.ndarray  # (n,3)  unit
    left: np.ndarray  # (n,3)  unit, rolled
    up: np.ndarray  # (n,3)  unit, rolled
    heading: np.ndarray  # (n,)   plan heading [rad], from +E counter-clockwise
    roll: np.ndarray  # (n,)   [rad], see cant.py
    pitch: np.ndarray  # (n,)   [rad], positive climbing
    cant_mm: np.ndarray  # (n,)
    curvature: np.ndarray  # (n,)   signed [1/m]
    gradient: np.ndarray  # (n,)   dimensionless
    elevation: np.ndarray  # (n,)   profile elevation at the pivot [m]

    def __len__(self) -> int:
        return len(self.station)


_CANDIDATE_CHUNK = 200_000
"""Candidate stations evaluated per vectorised `h.curvature` call (about 1.6 MB of float64 per chunk)."""


def curvature_side(
    alignment: Alignment, s: np.ndarray, search_m: float = 250.0, step_m: float = 5.0
) -> np.ndarray:
    """sign(κ) at s; on straights, the sign of the nearest curved station within ±search_m (0 if none).

    Cant ramps that run onto a straight need to know which side is 'inside'; the adjacent curve tells us.
    Candidates are tried in the order +step, −step, +2·step, −2·step, …, so a tie goes to the forward one.
    Candidates outside the alignment are skipped, not clamped. A station with a curvature below
    CURVATURE_EPS but not zero keeps sign(κ) unless the first ±step candidate is curved.
    """
    h = alignment.horizontal
    s = s_array(s)
    k = h.curvature(s)
    side = np.sign(k)
    flat = np.flatnonzero(np.abs(k) < CURVATURE_EPS)
    offsets = np.arange(step_m, search_m + step_m, step_m)
    if flat.size == 0 or offsets.size == 0:
        return side
    width = 2 * offsets.size
    columns = np.arange(width)
    rows_per_chunk = max(1, _CANDIDATE_CHUNK // width)
    for start in range(0, flat.size, rows_per_chunk):
        idx = flat[start : start + rows_per_chunk]
        si = s[idx]
        cands = np.empty((idx.size, width))
        cands[:, 0::2] = si[:, None] + offsets
        cands[:, 1::2] = si[:, None] - offsets
        kc = h.curvature(cands.ravel()).reshape(cands.shape)
        reach = np.where(k[idx] != 0.0, 2, width)
        usable = (
            (cands >= h.stations[0])
            & (cands <= h.stations[-1])
            & (np.abs(kc) >= CURVATURE_EPS)
            & (columns < reach[:, None])
        )
        rows = np.flatnonzero(usable.any(axis=1))
        first = usable[rows].argmax(axis=1)
        side[idx[rows]] = np.sign(kc[rows, first])
    return side


def frames(alignment: Alignment, s, pivot: RotationPivot | None = None) -> TrackFrames:
    s = s_array(s)
    h, v, c = alignment.horizontal, alignment.vertical, alignment.cant
    pivot = pivot or c.pivot

    xy = h.point(s)
    heading = h.heading(s)
    curvature = h.curvature(s)
    elevation = v.elevation(s)
    gradient = v.gradient(s)
    pitch = np.arctan(gradient)

    cos_h, sin_h = np.cos(heading), np.sin(heading)
    cos_p, sin_p = np.cos(pitch), np.sin(pitch)
    tangent = np.column_stack([cos_h * cos_p, sin_h * cos_p, sin_p])
    left0 = np.column_stack([-sin_h, cos_h, np.zeros_like(s)])
    up0 = np.cross(tangent, left0)

    cant_mm = c.cant(s)
    side = curvature_side(alignment, s) if np.any(cant_mm > 0) else np.sign(curvature)
    roll = c.roll(s, side)
    cos_r, sin_r = np.cos(roll)[:, None], np.sin(roll)[:, None]
    left = cos_r * left0 + sin_r * up0
    up = -sin_r * left0 + cos_r * up0

    origin = np.column_stack([xy[:, 0], xy[:, 1], elevation])
    if pivot is RotationPivot.LOW_RAIL:
        # Inner (low) rail head = plane centre + (b/2)·side·left; the profile elevation is that rail head.
        half_base = c.superelevation_base_mm / 2000.0
        origin[:, 2] -= half_base * side * left[:, 2]

    return TrackFrames(
        s, origin, tangent, left, up, heading, roll, pitch, cant_mm, curvature, gradient, elevation
    )


def to_xyz(alignment: Alignment, s, y=0.0, z=0.0, pivot: RotationPivot | None = None) -> np.ndarray:
    """LRS (s, y, z) → (E, N, H). y is left-positive lateral offset in the track plane, z along `up`."""
    fr = frames(alignment, s, pivot)
    y = np.broadcast_to(np.asarray(y, dtype=np.float64), fr.station.shape)[:, None]
    z = np.broadcast_to(np.asarray(z, dtype=np.float64), fr.station.shape)[:, None]
    return fr.origin + y * fr.left + z * fr.up


def project_xy(
    alignment: Alignment, xy, seed_spacing_m: float = 10.0, iterations: int = 12
) -> tuple[np.ndarray, np.ndarray]:
    """Plan projection of points onto the alignment: returns (s, y) with y left-positive.

    Seeded by the nearest coarse sample, refined by Newton on f(s) = (P(s) − Q)·T(s).
    """
    h = alignment.horizontal
    q = np.atleast_2d(np.asarray(xy, dtype=np.float64))
    grid = np.arange(h.stations[0], h.stations[-1] + seed_spacing_m, seed_spacing_m)
    grid = np.clip(grid, h.stations[0], h.stations[-1])
    pts = h.point(grid)
    d2 = ((q[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2)
    s = grid[np.argmin(d2, axis=1)].astype(np.float64)
    for _ in range(iterations):
        p = h.point(s)
        th = h.heading(s)
        k = h.curvature(s)
        t = np.column_stack([np.cos(th), np.sin(th)])
        n = np.column_stack([-np.sin(th), np.cos(th)])
        diff = p - q
        f = (diff * t).sum(axis=1)
        fp = 1.0 + k * (diff * n).sum(axis=1)
        fp = np.where(np.abs(fp) < 1e-9, 1e-9, fp)
        s = np.clip(s - f / fp, h.stations[0], h.stations[-1])
    p = h.point(s)
    th = h.heading(s)
    n = np.column_stack([-np.sin(th), np.cos(th)])
    y = ((q - p) * n).sum(axis=1)
    return s, y
