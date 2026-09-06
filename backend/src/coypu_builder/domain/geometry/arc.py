from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry._common import CURVATURE_EPS, s_array


@dataclass(frozen=True, slots=True)
class CircularArc:
    """Circular arc with signed curvature: positive = counter-clockwise (left turn)."""

    start_x: float
    start_y: float
    heading_start: float
    length: float
    curvature: float

    kind = "arc"

    def __post_init__(self):
        if abs(self.curvature) < CURVATURE_EPS:
            raise ValueError("CircularArc needs non-zero curvature; use Line for straights")

    @property
    def curvature_start(self) -> float:
        return self.curvature

    @property
    def curvature_end(self) -> float:
        return self.curvature

    @property
    def radius(self) -> float:
        return 1.0 / abs(self.curvature)

    @property
    def rotation(self) -> str:
        return "ccw" if self.curvature > 0 else "cw"

    @property
    def heading_end(self) -> float:
        return self.heading_start + self.curvature * self.length

    @property
    def start(self) -> np.ndarray:
        return np.array([self.start_x, self.start_y])

    @property
    def end(self) -> np.ndarray:
        return self.point(self.length)[0]

    @property
    def center(self) -> np.ndarray:
        r = 1.0 / self.curvature
        return np.array(
            [
                self.start_x - r * np.sin(self.heading_start),
                self.start_y + r * np.cos(self.heading_start),
            ]
        )

    def point(self, s) -> np.ndarray:
        s = s_array(s)
        theta = self.heading_start + self.curvature * s
        r = 1.0 / self.curvature
        cx, cy = self.center
        return np.column_stack([cx + r * np.sin(theta), cy - r * np.cos(theta)])

    def heading(self, s) -> np.ndarray:
        return self.heading_start + self.curvature * s_array(s)

    def curvature_at(self, s) -> np.ndarray:
        return np.full(s_array(s).shape, self.curvature)

    @classmethod
    def from_center(cls, start, center, end, rotation: str, length: float | None = None) -> CircularArc:
        """Build from LandXML-style Start/Center/End points. Length from the swept angle unless given."""
        start = np.asarray(start, dtype=np.float64)
        center = np.asarray(center, dtype=np.float64)
        end = np.asarray(end, dtype=np.float64)
        sign = 1.0 if rotation == "ccw" else -1.0
        radius = float(np.hypot(*(start - center)))
        a0 = float(np.arctan2(start[1] - center[1], start[0] - center[0]))
        a1 = float(np.arctan2(end[1] - center[1], end[0] - center[0]))
        sweep = (sign * (a1 - a0)) % (2.0 * np.pi)
        if sweep < 1e-12:
            sweep = 2.0 * np.pi if length is None or length > radius * np.pi else 0.0
        heading = a0 + sign * np.pi / 2.0
        arc_length = float(length) if length is not None else radius * sweep
        return cls(float(start[0]), float(start[1]), heading, arc_length, sign / radius)
