"""Shaded relief for places with no imagery: colour by height, modulated by a hillshade (T-134, Part A).

Pure numpy. Heights are a (rows, cols) grid with row 0 the northernmost row, as in `domain/imagery.py`. Every
result covers the interior of that grid, so a (rows, cols) input gives (rows - 2, cols - 2) output, and the
result for an interior cell uses its four neighbours.
"""

from __future__ import annotations

import math

import numpy as np

SUN_AZIMUTH_DEG = 315.0  # clockwise from north: north-west
SUN_ALTITUDE_DEG = 45.0
RAMP = (
    (0.00, (70, 120, 70)),
    (0.35, (165, 185, 110)),
    (0.70, (190, 160, 115)),
    (1.00, (240, 238, 232)),
)


def _sun() -> np.ndarray:
    azimuth = math.radians(SUN_AZIMUTH_DEG)
    altitude = math.radians(SUN_ALTITUDE_DEG)
    return np.array(
        [
            math.sin(azimuth) * math.cos(altitude),
            math.cos(azimuth) * math.cos(altitude),
            math.sin(altitude),
        ]
    )


def hillshade(heights: np.ndarray, cell_m: float) -> np.ndarray:
    """(rows - 2, cols - 2) float in [0, 1] for the interior of a (rows, cols) height grid.

    Central differences: `dz/de = (h[r, c+1] - h[r, c-1]) / (2 cell)` and
    `dz/dn = (h[r-1, c] - h[r+1, c]) / (2 cell)`. The surface normal is `(-dz/de, -dz/dn, 1)`, normalised;
    the light is `(sin az cos alt, cos az cos alt, sin alt)` in (E, N, up), and the shade is `max(0, n . L)`.
    NaN where any of the five heights (the cell and its four neighbours) is not finite.
    """
    h = np.asarray(heights, dtype=np.float64)
    if h.ndim != 2:
        raise ValueError(f"heights must be a 2-D grid, got shape {h.shape}")
    if h.shape[0] < 3 or h.shape[1] < 3:
        return np.zeros((max(h.shape[0] - 2, 0), max(h.shape[1] - 2, 0)))
    centre = h[1:-1, 1:-1]
    east, west = h[1:-1, 2:], h[1:-1, :-2]
    north, south = h[:-2, 1:-1], h[2:, 1:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        dz_de = (east - west) / (2.0 * cell_m)
        dz_dn = (north - south) / (2.0 * cell_m)
        norm = np.sqrt(dz_de * dz_de + dz_dn * dz_dn + 1.0)
        light_e, light_n, light_up = _sun()
        shade = np.maximum(0.0, (-dz_de * light_e - dz_dn * light_n + light_up) / norm)
    missing = ~(
        np.isfinite(centre) & np.isfinite(east) & np.isfinite(west) & np.isfinite(north) & np.isfinite(south)
    )
    return np.where(missing, np.nan, shade)


def hypsometric(heights: np.ndarray, low: float, high: float) -> np.ndarray:
    """(..., 3) float colour: `t = clip((h - low) / (high - low), 0, 1)`, linear between the `RAMP` stops.
    `high == low` gives `t = 0.5` everywhere."""
    h = np.asarray(heights, dtype=np.float64)
    if high == low:
        t = np.full(h.shape, 0.5)
    else:
        with np.errstate(invalid="ignore"):
            t = np.clip((h - low) / (high - low), 0.0, 1.0)
    stops = np.array([stop for stop, _ in RAMP])
    colours = np.array([colour for _, colour in RAMP], dtype=np.float64)
    out = np.empty(h.shape + (3,), dtype=np.float64)
    for channel in range(3):
        out[..., channel] = np.interp(t, stops, colours[:, channel])
    return out


def relief(heights: np.ndarray, cell_m: float, low: float, high: float) -> tuple[np.ndarray, np.ndarray]:
    """(rgb uint8, valid bool) for the interior of the grid:
    `rgb = round(hypsometric * (0.25 + 0.75 * hillshade))`, and `valid` is the finite part of the hillshade.
    Invalid samples are (0, 0, 0)."""
    h = np.asarray(heights, dtype=np.float64)
    shade = hillshade(h, cell_m)
    colour = hypsometric(h[1:-1, 1:-1], low, high)
    valid = np.isfinite(shade)
    with np.errstate(invalid="ignore"):
        lit = np.where(valid[..., None], colour * (0.25 + 0.75 * shade)[..., None], 0.0)
    rgb = np.round(np.clip(lit, 0.0, 255.0)).astype(np.uint8)
    return rgb, valid
