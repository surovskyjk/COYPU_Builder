"""Vertical alignment: PVI grade lines with symmetric vertical curves at interior PVIs.

Both LandXML `ParaCurve` (parabola of given length) and `CircCurve` (length + radius) are evaluated as the
standard parabola z(x) = z_BVC + g1·x + (g2 − g1)·x² / (2L); for a circular curve L = R·|g2 − g1| so both
agree to second order, which is the railway-design convention for small grades.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from coypu_builder.domain.geometry._common import s_array


@dataclass(frozen=True, slots=True)
class VerticalCurve:
    station: float
    length: float
    kind: str = "parabolic"  # "parabolic" | "circular"
    radius: float | None = None


@dataclass(frozen=True)
class VerticalAlignment:
    pvi_stations: np.ndarray
    pvi_elevations: np.ndarray
    curves: tuple[VerticalCurve, ...] = ()
    _grades: np.ndarray = field(init=False, repr=False, compare=False)
    _half: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        st = np.asarray(self.pvi_stations, dtype=np.float64)
        el = np.asarray(self.pvi_elevations, dtype=np.float64)
        if st.ndim != 1 or st.shape != el.shape or len(st) < 1:
            raise ValueError("pvi_stations and pvi_elevations must be equal-length 1-D arrays")
        if len(st) > 1 and np.any(np.diff(st) <= 0):
            raise ValueError("PVI stations must be strictly increasing")
        object.__setattr__(self, "pvi_stations", st)
        object.__setattr__(self, "pvi_elevations", el)
        grades = np.diff(el) / np.diff(st) if len(st) > 1 else np.zeros(0)
        half = np.zeros(len(st))
        for curve in self.curves:
            i = int(np.argmin(np.abs(st - curve.station)))
            if abs(st[i] - curve.station) > 1e-6 or i == 0 or i == len(st) - 1:
                continue
            half[i] = max(curve.length, 0.0) / 2.0
        # Clip curves that would overlap the neighbouring tangent so evaluation stays single-valued.
        for i in range(1, len(st) - 1):
            room = min(st[i] - st[i - 1] - half[i - 1], st[i + 1] - st[i] - half[i + 1])
            if half[i] > room:
                half[i] = max(room, 0.0)
        object.__setattr__(self, "_grades", grades)
        object.__setattr__(self, "_half", half)

    @classmethod
    def constant(cls, elevation: float, station_start: float, station_end: float) -> VerticalAlignment:
        return cls(np.array([station_start, station_end]), np.array([elevation, elevation]))

    @property
    def grades(self) -> np.ndarray:
        return self._grades

    def curve_half_lengths(self) -> np.ndarray:
        return self._half

    def key_stations(self) -> np.ndarray:
        """PVIs plus BVC/EVC points — everything a sampler should not skip."""
        st, half = self.pvi_stations, self._half
        keys = [st, st[half > 0] - half[half > 0], st[half > 0] + half[half > 0]]
        return np.unique(np.concatenate(keys))

    def _tangent_and_curve(self, s: np.ndarray):
        st, el, g, half = self.pvi_stations, self.pvi_elevations, self._grades, self._half
        n = len(st)
        if n == 1:
            return np.full(s.shape, el[0]), np.zeros(s.shape)
        i = np.clip(np.searchsorted(st, s, side="right") - 1, 0, n - 2)
        z = el[i] + g[i] * (s - st[i])
        grade = g[i].copy()

        # Curve at the start PVI of the interval (interior only): s in [S_i, S_i + h_i)
        j = i
        active = (j > 0) & (half[j] > 0) & (s < st[j] + half[j])
        # Curve at the end PVI of the interval: s in [S_{i+1} - h, S_{i+1}]
        k = i + 1
        active_k = (k < n - 1) & (half[k] > 0) & (s >= st[k] - half[k])
        j = np.where(active_k, k, j)
        active = active | active_k

        if np.any(active):
            jj = j[active]
            h = half[jj]
            g1, g2 = g[jj - 1], g[jj]
            L = 2.0 * h
            x = s[active] - (st[jj] - h)
            z_bvc = el[jj] - g1 * h
            z[active] = z_bvc + g1 * x + (g2 - g1) * x * x / (2.0 * L)
            grade[active] = g1 + (g2 - g1) * x / L
        return z, grade

    def elevation(self, s) -> np.ndarray:
        return self._tangent_and_curve(s_array(s))[0]

    def gradient(self, s) -> np.ndarray:
        """Dimensionless rise/run (0.01 = 10 ‰)."""
        return self._tangent_and_curve(s_array(s))[1]
