from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry._common import s_array


@dataclass(frozen=True, slots=True)
class Line:
    start_x: float
    start_y: float
    heading_start: float
    length: float

    kind = "line"

    @property
    def curvature_start(self) -> float:
        return 0.0

    @property
    def curvature_end(self) -> float:
        return 0.0

    @property
    def heading_end(self) -> float:
        return self.heading_start

    @property
    def start(self) -> np.ndarray:
        return np.array([self.start_x, self.start_y])

    @property
    def end(self) -> np.ndarray:
        return self.point(self.length)[0]

    def point(self, s) -> np.ndarray:
        s = s_array(s)
        return np.column_stack(
            [
                self.start_x + s * np.cos(self.heading_start),
                self.start_y + s * np.sin(self.heading_start),
            ]
        )

    def heading(self, s) -> np.ndarray:
        return np.full(s_array(s).shape, self.heading_start)

    def curvature(self, s) -> np.ndarray:
        return np.zeros(s_array(s).shape)

    @classmethod
    def from_points(cls, start, end) -> Line:
        dx, dy = float(end[0] - start[0]), float(end[1] - start[1])
        return cls(float(start[0]), float(start[1]), float(np.arctan2(dy, dx)), float(np.hypot(dx, dy)))
