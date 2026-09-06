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


def curvature_side(
    alignment: Alignment, s: np.ndarray, search_m: float = 250.0, step_m: float = 5.0
) -> np.ndarray:
    """sign(κ) at s; on straights, the sign of the nearest curved station within ±search_m (0 if none).

    Cant ramps that run onto a straight need to know which side is 'inside'; the adjacent curve tells us.
    """
    h = alignment.horizontal
    k = h.curvature(s)
    side = np.sign(k)
    flat = np.abs(k) < CURVATURE_EPS
    if not np.any(flat):
        return side
    offsets = np.arange(step_m, search_m + step_m, step_m)
    for i in np.flatnonzero(flat):
        si = s[i]
        for d in offsets:
            for cand in (si + d, si - d):
                if h.stations[0] <= cand <= h.stations[-1]:
                    kc = h.curvature(cand)[0]
                    if abs(kc) >= CURVATURE_EPS:
                        side[i] = np.sign(kc)
                        break
            if side[i] != 0:
                break
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
