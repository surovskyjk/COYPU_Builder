"""Rail and ballast cross-section profiles: 2-D polylines `(y, z)` in the track plane, exactly the LRS
offsets of `domain/lrs.py` (`y` left-positive, `z` along the plane normal `up`). `z = 0` is the track plane
itself (rail head top, where `frames().origin` sits). These are simplified silhouettes for rendering, not
engineering cross-sections -- see `docs/data-contracts/track-mesh.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_RAIL_HEIGHT_M = 0.172
"""Roughly a UIC60 rail's height; the silhouette below is not a UIC60 profile."""

_HEAD_HALF_WIDTH_M = 0.036
_WEB_HALF_WIDTH_M = 0.008
_FOOT_HALF_WIDTH_M = 0.075
_HEAD_DEPTH_M = 0.035
_FOOT_DEPTH_M = 0.02

RAIL_HEAD_WIDTH_M = 2.0 * _HEAD_HALF_WIDTH_M


@dataclass(frozen=True)
class Profile:
    name: str
    points: np.ndarray  # (k, 2) float64, (y, z)
    closed: bool


def _rail_silhouette(rail_height_m: float) -> np.ndarray:
    """A 10-point head/web/foot silhouette, base (head top) at `z=0`, foot at `z=-rail_height_m`. A simple
    (non-self-intersecting), counter-clockwise ring -- `sweep.py`'s vertex normals are correct for either
    winding, but its triangle indices assume counter-clockwise, so this ordering is load-bearing."""
    web_depth = rail_height_m - _HEAD_DEPTH_M - _FOOT_DEPTH_M
    if web_depth <= 0.0:
        raise ValueError(f"rail_height_m={rail_height_m} too small for the fixed head/foot depths")
    z_web = -(_HEAD_DEPTH_M + web_depth)
    z_base = -rail_height_m
    # Points 0 and 1 are the two head-top corners (this order matters: callers average them to get the
    # head's own y-centre). The rest run counter-clockwise (+y/-z plotted as a standard x/y plane) down the
    # left side, across the foot, and up the right side, so `sweep.py`'s outward-normal winding comes out
    # right without a separate correction.
    return np.array(
        [
            (_HEAD_HALF_WIDTH_M, 0.0),
            (-_HEAD_HALF_WIDTH_M, 0.0),
            (-_HEAD_HALF_WIDTH_M, -_HEAD_DEPTH_M),
            (-_WEB_HALF_WIDTH_M, z_web),
            (-_FOOT_HALF_WIDTH_M, z_web),
            (-_FOOT_HALF_WIDTH_M, z_base),
            (_FOOT_HALF_WIDTH_M, z_base),
            (_FOOT_HALF_WIDTH_M, z_web),
            (_WEB_HALF_WIDTH_M, z_web),
            (_HEAD_HALF_WIDTH_M, -_HEAD_DEPTH_M),
        ],
        dtype=np.float64,
    )


def rail_profile(gauge_mm: float, rail_height_m: float = DEFAULT_RAIL_HEIGHT_M) -> tuple[Profile, Profile]:
    """Two rail profiles, head centres at `y = ±(gauge + rail_head_width)/2` so the two rail heads are
    `gauge` apart at their inner (gauge-side) edges. Head top at `z=0` (the track plane), foot at
    `z=-rail_height_m`."""
    silhouette = _rail_silhouette(rail_height_m)
    center = (gauge_mm / 1000.0 + RAIL_HEAD_WIDTH_M) / 2.0
    left = silhouette + np.array([center, 0.0])
    right = silhouette + np.array([-center, 0.0])
    return Profile("rail_left", left, closed=True), Profile("rail_right", right, closed=True)


def ballast_profile(
    gauge_mm: float, shoulder_m: float = 0.4, depth_m: float = 0.5, side_slope: float = 1.5
) -> Profile:
    """A trapezoidal prism below the track plane. Top half-width is half the gauge plus `shoulder_m`; the
    bottom widens by `side_slope` (horizontal run per metre of depth) over `depth_m`. Top is pinned to
    `z=-DEFAULT_RAIL_HEIGHT_M` -- the rail base level -- since sleeper thickness is not modelled and this
    keeps the ballast from overlapping the rail silhouettes above it (see track-mesh.md)."""
    top_half_width = gauge_mm / 2000.0 + shoulder_m
    bottom_half_width = top_half_width + side_slope * depth_m
    top_z = -DEFAULT_RAIL_HEIGHT_M
    bottom_z = top_z - depth_m
    points = np.array(
        [
            (top_half_width, top_z),
            (-top_half_width, top_z),
            (-bottom_half_width, bottom_z),
            (bottom_half_width, bottom_z),
        ],
        dtype=np.float64,
    )
    return Profile("ballast", points, closed=True)
