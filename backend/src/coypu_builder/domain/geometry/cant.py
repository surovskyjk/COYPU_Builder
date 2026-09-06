"""Cant (superelevation) profile and the rotation-pivot convention.

Cant D is the height difference between the outer and inner rail heads, positive magnitude in mm.
Roll about the tangent axis (right-hand rule, +T from left toward up) is `roll = −sign(κ)·asin(D / b)` with
b = superelevation base (rail-head centre distance, 1500 mm for 1435 mm gauge): the outer rail of a
left-hand (ccw, κ > 0) curve is the right rail, so the track plane rolls negative (left side down).

The vertical profile elevation refers to the rotation pivot. With `RotationPivot.LOW_RAIL` (LandXML
`rotationPoint="insideRail"`, railway practice) the profile follows the inner rail head, so the centre of
the track plane sits (b/2)·sin|roll| ≈ D/2 above it; with `CENTERLINE` the profile is the plane centre.
`lrs.frames` applies this so cant never shifts the vertical datum in curves.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from coypu_builder.domain.geometry._common import s_array


class RotationPivot(StrEnum):
    LOW_RAIL = "low_rail"
    CENTERLINE = "centerline"

    @classmethod
    def from_landxml(cls, rotation_point: str | None) -> RotationPivot:
        value = (rotation_point or "").strip().lower()
        if value in ("insiderail", "inside_rail", "lowrail", "low_rail", "inner"):
            return cls.LOW_RAIL
        if value in ("center", "centre", "centerline", "centreline"):
            return cls.CENTERLINE
        return cls.LOW_RAIL


@dataclass(frozen=True)
class CantProfile:
    stations: np.ndarray
    cant_mm: np.ndarray
    gauge_mm: float = 1435.0
    superelevation_base_mm: float = 1500.0
    pivot: RotationPivot = RotationPivot.LOW_RAIL

    def __post_init__(self):
        st = np.asarray(self.stations, dtype=np.float64)
        d = np.abs(np.asarray(self.cant_mm, dtype=np.float64))
        if st.shape != d.shape or st.ndim != 1 or len(st) == 0:
            raise ValueError("stations and cant_mm must be equal-length non-empty 1-D arrays")
        order = np.argsort(st, kind="stable")
        object.__setattr__(self, "stations", st[order])
        object.__setattr__(self, "cant_mm", d[order])

    @classmethod
    def zero(cls, station_start: float, station_end: float, **kwargs) -> CantProfile:
        return cls(np.array([station_start, station_end]), np.zeros(2), **kwargs)

    def cant(self, s) -> np.ndarray:
        """Linear interpolation between cant stations, clamped outside."""
        return np.interp(s_array(s), self.stations, self.cant_mm)

    def roll(self, s, curvature_sign) -> np.ndarray:
        """Signed roll angle [rad] for the given curvature sign(s)."""
        ratio = np.clip(self.cant(s) / self.superelevation_base_mm, -1.0, 1.0)
        return -np.sign(np.asarray(curvature_sign, dtype=np.float64)) * np.arcsin(ratio)

    def key_stations(self) -> np.ndarray:
        return self.stations
