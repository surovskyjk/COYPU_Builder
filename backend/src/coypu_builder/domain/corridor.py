"""Plan-view corridor geometry: the centreline polyline that corridor buffers are built from."""

from __future__ import annotations

import numpy as np

from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.sampling import bake_stations

_RANGE_TOLERANCE_M = 1e-6
_GRID_SPACING_M = 100.0


def plan_centreline(
    alignment: Alignment,
    station_from: float | None = None,
    station_to: float | None = None,
    max_chord_error_m: float = 0.05,
) -> np.ndarray:
    """(n, 2) float64 (E, N) in the project CRS, ordered by station, for the station range requested.

    Stations are absolute metres (the `frames()` basis). `None` means the alignment's own end. Both range
    ends are sampled exactly; between them the samples are the alignment's key stations, a coarse grid and
    the curve refinement of `bake_stations`, so a chord deviates from the true centreline by at most
    `max_chord_error_m`.
    """
    s_min, s_max = alignment.station_start, alignment.station_end
    a = s_min if station_from is None else float(station_from)
    b = s_max if station_to is None else float(station_to)
    if a < s_min - _RANGE_TOLERANCE_M or b > s_max + _RANGE_TOLERANCE_M:
        raise ValueError(
            f"station range {a:.3f}..{b:.3f} m lies outside the alignment's {s_min:.3f}..{s_max:.3f} m"
        )
    if not a < b:
        raise ValueError(f"station_from ({a:.3f}) must be less than station_to ({b:.3f})")
    a, b = max(a, s_min), min(b, s_max)

    stations = bake_stations(alignment, spacing_m=_GRID_SPACING_M, max_chord_error_m=max_chord_error_m)
    inner = stations[(stations > a + 1e-9) & (stations < b - 1e-9)]
    samples = np.concatenate([[a], inner, [b]])
    return np.asarray(alignment.horizontal.point(samples), dtype=np.float64)
