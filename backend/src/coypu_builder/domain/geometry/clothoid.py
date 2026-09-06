"""Euler spiral (clothoid) with linear curvature, exact via Fresnel integrals.

The segment is a window of the canonical clothoid κ(u) = u / A² (u = arc length from the point of zero
curvature). With curvature rate dκ = (κ1 − κ0) / L the window starts at u0 = κ0 / dκ; the canonical
curve is mirrored (σ = −1) when dκ < 0. This single formulation covers entry spirals (κ0 = 0), exit
spirals (κ1 = 0) and compound spirals (both non-zero, same or opposite sign) without special cases.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.special import fresnel

from coypu_builder.domain.geometry._common import CURVATURE_EPS, s_array
from coypu_builder.domain.geometry.arc import CircularArc
from coypu_builder.domain.geometry.line import Line


@dataclass(frozen=True, slots=True)
class Clothoid:
    start_x: float
    start_y: float
    heading_start: float
    length: float
    curvature_start: float
    curvature_end: float

    kind = "clothoid"

    @property
    def curvature_rate(self) -> float:
        return (self.curvature_end - self.curvature_start) / self.length

    @property
    def is_degenerate(self) -> bool:
        return abs(self.curvature_end - self.curvature_start) < CURVATURE_EPS

    @property
    def clothoid_parameter(self) -> float:
        """A = sqrt(L / |κ1 − κ0|); equals sqrt(R·L) for a spiral from straight to radius R."""
        return float(np.sqrt(1.0 / abs(self.curvature_rate))) if not self.is_degenerate else float("inf")

    @property
    def heading_end(self) -> float:
        return float(self.heading(self.length)[0])

    @property
    def start(self) -> np.ndarray:
        return np.array([self.start_x, self.start_y])

    @property
    def end(self) -> np.ndarray:
        return self.point(self.length)[0]

    def _fallback(self) -> Line | CircularArc:
        if abs(self.curvature_start) < CURVATURE_EPS:
            return Line(self.start_x, self.start_y, self.heading_start, self.length)
        return CircularArc(self.start_x, self.start_y, self.heading_start, self.length, self.curvature_start)

    def _canonical(self, u: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        dk = self.curvature_rate
        sigma = 1.0 if dk > 0 else -1.0
        a2 = 1.0 / abs(dk)
        a = np.sqrt(np.pi * a2)
        s_fr, c_fr = fresnel(u / a)
        return a * c_fr, sigma * a * s_fr, sigma * u * u / (2.0 * a2)

    def point(self, s) -> np.ndarray:
        s = s_array(s)
        if self.is_degenerate:
            return self._fallback().point(s)
        u0 = self.curvature_start / self.curvature_rate
        x0, y0, phi0 = self._canonical(np.array([u0]))
        x, y, _ = self._canonical(u0 + s)
        rot = self.heading_start - phi0[0]
        c, sn = np.cos(rot), np.sin(rot)
        dx, dy = x - x0[0], y - y0[0]
        return np.column_stack([self.start_x + c * dx - sn * dy, self.start_y + sn * dx + c * dy])

    def heading(self, s) -> np.ndarray:
        s = s_array(s)
        return self.heading_start + self.curvature_start * s + 0.5 * self.curvature_rate * s * s

    def curvature(self, s) -> np.ndarray:
        return self.curvature_start + self.curvature_rate * s_array(s)
