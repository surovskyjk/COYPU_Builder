from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from coypu_builder.domain.geometry._common import s_array, wrap_angle


@runtime_checkable
class HorizontalSegment(Protocol):
    kind: str
    start_x: float
    start_y: float
    heading_start: float
    length: float

    @property
    def curvature_start(self) -> float: ...
    @property
    def curvature_end(self) -> float: ...
    @property
    def heading_end(self) -> float: ...
    @property
    def start(self) -> np.ndarray: ...
    @property
    def end(self) -> np.ndarray: ...
    def point(self, s) -> np.ndarray: ...
    def heading(self, s) -> np.ndarray: ...


def _curvature_of(seg, s_local: np.ndarray) -> np.ndarray:
    if seg.kind == "arc":
        return seg.curvature_at(s_local)
    return seg.curvature(s_local)


@dataclass(frozen=True)
class SegmentDiscontinuity:
    index: int
    gap_m: float
    heading_jump_rad: float
    curvature_jump: float


@dataclass(frozen=True)
class HorizontalAlignment:
    """Chain of segments on an absolute station axis.

    Stations default to `station_start` plus cumulative geometric lengths. When `station_bounds` (n+1
    values, e.g. LandXML `staStart` attributes) is given, those are the LRS truth: each segment's geometric
    length is re-parametrised linearly onto its station span (`station_scale`, ≈ 1 for consistent files),
    so stations stay bit-identical to the source while positions stay on the file's geometry.
    """

    segments: tuple[HorizontalSegment, ...]
    station_start: float = 0.0
    station_bounds: np.ndarray | None = field(default=None, compare=False)
    _bounds: np.ndarray = field(init=False, repr=False, compare=False)
    _scale: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not self.segments:
            raise ValueError("HorizontalAlignment needs at least one segment")
        lengths = np.array([seg.length for seg in self.segments], dtype=np.float64)
        if self.station_bounds is None:
            bounds = np.concatenate([[self.station_start], self.station_start + np.cumsum(lengths)])
        else:
            bounds = np.asarray(self.station_bounds, dtype=np.float64)
            if bounds.shape != (len(self.segments) + 1,):
                raise ValueError("station_bounds must have one value per segment boundary")
            if np.any(np.diff(bounds) <= 0):
                raise ValueError("station_bounds must be strictly increasing")
            object.__setattr__(self, "station_start", float(bounds[0]))
        object.__setattr__(self, "_bounds", bounds)
        object.__setattr__(self, "_scale", lengths / np.diff(bounds))

    @property
    def stations(self) -> np.ndarray:
        """Segment boundary stations, length n+1."""
        return self._bounds

    @property
    def station_scale(self) -> np.ndarray:
        """Geometric length / station span per segment (all 1.0 unless station_bounds were given)."""
        return self._scale

    @property
    def station_end(self) -> float:
        return float(self._bounds[-1])

    @property
    def length(self) -> float:
        return float(self._bounds[-1] - self._bounds[0])

    def locate(self, s) -> tuple[np.ndarray, np.ndarray]:
        """Segment index and local geometric arc length for each station (clamped to the extent)."""
        s = np.clip(s_array(s), self._bounds[0], self._bounds[-1])
        idx = np.searchsorted(self._bounds[1:-1], s, side="right")
        return idx, (s - self._bounds[idx]) * self._scale[idx]

    def _eval(self, s, fn) -> np.ndarray:
        idx, local = self.locate(s)
        out = None
        for i in np.unique(idx):
            mask = idx == i
            values = fn(self.segments[i], local[mask])
            if out is None:
                out = np.empty((len(idx),) + values.shape[1:], dtype=np.float64)
            out[mask] = values
        return out

    def point(self, s) -> np.ndarray:
        return self._eval(s, lambda seg, sl: seg.point(sl))

    def heading(self, s) -> np.ndarray:
        return self._eval(s, lambda seg, sl: seg.heading(sl))

    def curvature(self, s) -> np.ndarray:
        return self._eval(s, _curvature_of)

    def segment_index(self, s) -> np.ndarray:
        return self.locate(s)[0]

    def discontinuities(self) -> list[SegmentDiscontinuity]:
        """Geometric continuity between consecutive segments (C0 gap, C1 heading jump, C2 curvature jump)."""
        out = []
        for i in range(1, len(self.segments)):
            prev, cur = self.segments[i - 1], self.segments[i]
            gap = float(np.hypot(*(cur.start - prev.end)))
            jump = float(abs(wrap_angle(cur.heading_start - prev.heading_end)))
            out.append(SegmentDiscontinuity(i, gap, jump, float(cur.curvature_start - prev.curvature_end)))
        return out
