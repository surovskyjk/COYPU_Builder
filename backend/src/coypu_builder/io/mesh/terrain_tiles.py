"""Terrain display tiles: the tile grid in the project CRS and the heightfield bake (ADR 0009, decision 5).

A tile at `level` L is `512 * 2**L` m on a side, its south-west corner at `(ix * edge, iy * edge)`, and holds
129 x 129 heights on its own lattice (corners shared with the neighbours). Every height is a query to the
analysis surface (`TerrainStack`) at the lattice point; tiles are for display and never for analysis. The
format is described in `docs/data-contracts/terrain-tiles.md`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any

import numpy as np

from coypu_builder.domain.terrain_surface import Bounds, TerrainSurface

MIN_LEVEL = 0
MAX_LEVEL = 4
BASE_EDGE_M = 512.0
SAMPLES = 129  # per side; the lattice has SAMPLES - 1 intervals
HEIGHT_QUANTUM_M = 2.0**-12  # heights are rounded to this before encoding; see `bake_tile`
NO_SOURCE_BYTE = 255  # in a tile's `source_index`: no source has data at this sample
MAX_SOURCES = 255  # source indices 0..254 fit a byte


@dataclass(frozen=True, eq=False)
class TerrainTile:
    level: int
    ix: int
    iy: int
    edge_m: float
    origin: tuple[float, float, float]  # (E, N, H), project CRS: the SW corner at the minimum valid height
    heights: np.ndarray  # (129, 129) float32, row 0 = south edge, relative to origin H; NaN = no data
    valid_fraction: float
    source_index: np.ndarray | None = None  # (129, 129) uint8, same layout as heights; 255 = no source

    def absolute_heights(self) -> np.ndarray:
        """float64 heights as decoded by a client: the float32 values plus the origin height."""
        return self.heights.astype(np.float64) + self.origin[2]


def check_level(level: int) -> int:
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError(f"level must be between {MIN_LEVEL} and {MAX_LEVEL}, got {level}")
    return level


def tile_edge(level: int) -> float:
    return BASE_EDGE_M * 2.0 ** check_level(level)


def tile_bounds(level: int, ix: int, iy: int) -> Bounds:
    edge = tile_edge(level)
    return (ix * edge, iy * edge, (ix + 1) * edge, (iy + 1) * edge)


def lattice(level: int, ix: int, iy: int) -> tuple[np.ndarray, np.ndarray]:
    """E and N of the lattice points, both shaped (129, 129): `n` grows with the row, `e` with the column.
    Spacing is a power of two in metres, so every coordinate is exact in float64 and a lattice point shared
    by two tiles has the identical coordinates in both."""
    edge = tile_edge(level)
    spacing = edge / (SAMPLES - 1)
    steps = np.arange(SAMPLES, dtype=np.float64) * spacing
    e, n = np.meshgrid(ix * edge + steps, iy * edge + steps)
    return e, n


def tiles_for_coverage(level: int, coverage: Sequence[Any]) -> list[tuple[int, int]]:
    """The `(ix, iy)` of every tile of `level` whose square intersects the coverage, sorted by `(iy, ix)`.

    Each coverage item is a `(min_e, min_n, max_e, max_n)` box, or a shapely Polygon / MultiPolygon (the
    optional `gis` extra, imported only when one is given).
    """
    edge = tile_edge(level)
    found: set[tuple[int, int]] = set()
    for item in coverage:
        is_box = isinstance(item, tuple)
        min_e, min_n, max_e, max_n = item if is_box else item.bounds
        ix0, iy0 = math.floor(min_e / edge), math.floor(min_n / edge)
        ix1 = max(math.ceil(max_e / edge) - 1, ix0)
        iy1 = max(math.ceil(max_n / edge) - 1, iy0)
        ixs, iys = np.meshgrid(np.arange(ix0, ix1 + 1), np.arange(iy0, iy1 + 1))
        ixs, iys = ixs.ravel(), iys.ravel()
        if not is_box:
            shapely = import_module("shapely")
            squares = shapely.box(ixs * edge, iys * edge, (ixs + 1) * edge, (iys + 1) * edge)
            hit = shapely.intersects(item, squares)
            ixs, iys = ixs[hit], iys[hit]
        found.update(zip(ixs.tolist(), iys.tolist(), strict=True))
    return sorted(found, key=lambda key: (key[1], key[0]))


def bake_tile(surface: TerrainSurface, level: int, ix: int, iy: int) -> TerrainTile:
    """Sample `surface` on the tile's lattice and encode it.

    The tile also records which source answered at every sample (`source_index`, an index into the stack's
    ordered sources, 255 where none did): a `TerrainStack` says so itself, any other surface is source 0.

    Heights are rounded to `HEIGHT_QUANTUM_M` (0.24 mm) first, then stored relative to the tile's minimum
    valid height. While a tile's relief is under 4096 m every stored value is an exact multiple of the
    quantum below 2**24 quanta, so float32 holds it without further rounding and a client decodes
    `origin_h + value` back to the quantised height exactly, which is therefore bit-identical along an edge
    shared by two tiles whatever their origins. A tile with no valid sample has origin height 0.
    """
    check_level(level)
    e, n = lattice(level, ix, iy)
    locate = getattr(surface, "heights_and_source", None)
    if locate is not None:
        found, owner = locate(e.ravel(), n.ravel())
        heights = found.reshape(SAMPLES, SAMPLES)
        if len(surface.surfaces) > MAX_SOURCES:
            raise ValueError(f"a tile can name at most {MAX_SOURCES} sources, got {len(surface.surfaces)}")
        source_index = np.where(owner >= 0, owner, NO_SOURCE_BYTE).astype(np.uint8).reshape(SAMPLES, SAMPLES)
    else:
        heights = surface.heights(e.ravel(), n.ravel()).reshape(SAMPLES, SAMPLES)
        source_index = np.where(np.isfinite(heights), 0, NO_SOURCE_BYTE).astype(np.uint8)
    valid = np.isfinite(heights)
    quantised = np.round(heights / HEIGHT_QUANTUM_M) * HEIGHT_QUANTUM_M
    base = float(quantised[valid].min()) if valid.any() else 0.0
    edge = tile_edge(level)
    return TerrainTile(
        level=level,
        ix=ix,
        iy=iy,
        edge_m=edge,
        origin=(ix * edge, iy * edge, base),
        heights=(quantised - base).astype(np.float32),
        valid_fraction=float(valid.mean()),
        source_index=source_index,
    )
