"""Imagery rasters and their composition onto a square window (T-134, Part A).

Pure numpy: no GIS types, no I/O. A raster is a regular grid of 8-bit colours with a validity mask, in the
project CRS. Row 0 is the northernmost row, which is the opposite of the height grids of `domain/terrain.py`
read from the south; the four asymmetric markers of the placement test exist to catch that mix-up. Layers of
different resolutions are composed finest first, a pixel taking the colour of the first layer that has a valid
sample there.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

Bounds = tuple[float, float, float, float]  # min_e, min_n, max_e, max_n
MAX_SUBSAMPLES = 8  # per side: a pixel is averaged over at most 8 x 8 raster cells


@dataclass(frozen=True, eq=False)
class ImageryRaster:
    """Row 0 is the northernmost row; cell `[r, c]` covers
    `E in [origin_e + c*cell_e, origin_e + (c+1)*cell_e]` and
    `N in [origin_n - (r+1)*cell_n, origin_n - r*cell_n]`."""

    origin_e: float  # project CRS, the outer corner of cell [0, 0]
    origin_n: float
    cell_e: float  # > 0
    cell_n: float  # > 0
    rgb: np.ndarray  # (rows, cols, 3) uint8
    valid: np.ndarray  # (rows, cols) bool; False = no data, transparent, or outside the clip
    native_resolution_m: float  # the source's own cell size before any averaging; used for precedence

    def __post_init__(self) -> None:
        for name in ("origin_e", "origin_n", "cell_e", "cell_n", "native_resolution_m"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite, got {getattr(self, name)}")
        if not (self.cell_e > 0.0 and self.cell_n > 0.0 and self.native_resolution_m > 0.0):
            raise ValueError(f"cell sizes must be > 0, got {self.cell_e} x {self.cell_n}")
        if self.rgb.dtype != np.uint8 or self.rgb.ndim != 3 or self.rgb.shape[2] != 3:
            raise ValueError(
                f"rgb must be a (rows, cols, 3) uint8 array, got {self.rgb.dtype} {self.rgb.shape}"
            )
        if self.valid.dtype != np.bool_ or self.valid.shape != self.rgb.shape[:2]:
            raise ValueError(
                f"valid must be a bool array of shape {self.rgb.shape[:2]}, "
                f"got {self.valid.dtype} {self.valid.shape}"
            )
        if self.rgb.shape[0] < 1 or self.rgb.shape[1] < 1:
            raise ValueError("an imagery raster needs at least one cell")

    @property
    def rows(self) -> int:
        return int(self.rgb.shape[0])

    @property
    def cols(self) -> int:
        return int(self.rgb.shape[1])

    def bounds(self) -> Bounds:
        return (
            self.origin_e,
            self.origin_n - self.rows * self.cell_n,
            self.origin_e + self.cols * self.cell_e,
            self.origin_n,
        )


class ImageryLayer(Protocol):
    source_id: str
    resolution_m: float

    def bounds(self) -> Bounds: ...

    def render(self, bounds: Bounds, size: int) -> tuple[np.ndarray, np.ndarray]:
        """(rgb (size, size, 3) uint8, valid (size, size) bool) over the square `bounds`. Row 0 is north.
        Pixel [r, c] has its centre at E = min_e + (c + 0.5) * edge / size,
        N = max_n - (r + 0.5) * edge / size."""
        ...


def subsample_factor(pixel_m: float, cell_m: float) -> int:
    """How many raster cells per side a pixel is averaged over: `ceil(pixel / cell)`, within 1 to 8."""
    return int(min(MAX_SUBSAMPLES, max(1, math.ceil(pixel_m / cell_m - 1e-9))))


def sample_raster(raster: ImageryRaster, bounds: Bounds, size: int, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Render `raster` over the square `bounds` at `size` pixels, each averaged from `k` by `k` sub-samples.

    A sub-sample takes the nearest raster cell. A pixel's colour is the mean of its valid sub-samples,
    rounded half up, and the pixel is valid when at least half of its sub-samples are. Each sub-sample
    offset is one pass over the whole window, so memory is O(size^2) however large `k` is.
    """
    min_e, _min_n, _max_e, max_n = bounds
    pixel = (bounds[2] - min_e) / size
    cols = np.arange(size, dtype=np.float64)
    sums = np.zeros((size, size, 3), dtype=np.float64)
    counts = np.zeros((size, size), dtype=np.int32)
    for i in range(k):
        north = max_n - (np.arange(size, dtype=np.float64) + (i + 0.5) / k) * pixel
        r_idx = np.floor((raster.origin_n - north) / raster.cell_n).astype(np.int64)
        r_ok = (r_idx >= 0) & (r_idx < raster.rows)
        r_idx = np.clip(r_idx, 0, raster.rows - 1)
        for j in range(k):
            east = min_e + (cols + (j + 0.5) / k) * pixel
            c_idx = np.floor((east - raster.origin_e) / raster.cell_e).astype(np.int64)
            c_ok = (c_idx >= 0) & (c_idx < raster.cols)
            c_idx = np.clip(c_idx, 0, raster.cols - 1)
            rows, columns = r_idx[:, None], c_idx[None, :]
            hit = r_ok[:, None] & c_ok[None, :] & raster.valid[rows, columns]
            sums += np.where(hit[..., None], raster.rgb[rows, columns], 0)
            counts += hit
    valid = counts * 2 >= k * k
    mean = sums / np.maximum(counts, 1)[..., None]
    rgb = np.clip(np.floor(mean + 0.5), 0, 255).astype(np.uint8)
    rgb[~valid] = 0
    return rgb, valid


class RasterLayer:
    """An `ImageryLayer` over one `ImageryRaster`; its precedence is the raster's own cell size."""

    def __init__(self, source_id: str, raster: ImageryRaster) -> None:
        self.source_id = source_id
        self.raster = raster

    @property
    def resolution_m(self) -> float:
        return self.raster.native_resolution_m

    def bounds(self) -> Bounds:
        return self.raster.bounds()

    def render(self, bounds: Bounds, size: int) -> tuple[np.ndarray, np.ndarray]:
        min_e, min_n, max_e, max_n = bounds
        edge = max_e - min_e
        if edge <= 0.0 or abs((max_n - min_n) - edge) > 1e-9 * edge:
            raise ValueError(f"render needs a square window, got {bounds}")
        k = subsample_factor(edge / size, min(self.raster.cell_e, self.raster.cell_n))
        return sample_raster(self.raster, bounds, size, k)


def bounds_meet(layer_bounds: Bounds, bounds: Bounds) -> bool:
    """Whether two boxes overlap with positive area: touching edges do not count."""
    return (
        layer_bounds[0] < bounds[2]
        and layer_bounds[2] > bounds[0]
        and layer_bounds[1] < bounds[3]
        and layer_bounds[3] > bounds[1]
    )


def compose_layers(
    layers: Sequence[ImageryLayer], bounds: Bounds, size: int
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    """Compose layers over the square `bounds`, finest `resolution_m` first (ties keep the caller's order).
    For each pixel the first layer with a valid pixel wins. Returns `(rgb, valid, winners)`: `winners` names
    the source ids that won at least one pixel, in the order the layers were rendered. Layers whose bounds do
    not meet `bounds` are not rendered."""
    order = sorted(range(len(layers)), key=lambda i: (layers[i].resolution_m, i))
    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    valid = np.zeros((size, size), dtype=bool)
    winners: list[str] = []
    for index in order:
        layer = layers[index]
        if not bounds_meet(layer.bounds(), bounds):
            continue
        layer_rgb, layer_valid = layer.render(bounds, size)
        take = layer_valid & ~valid
        if take.any():
            rgb[take] = layer_rgb[take]
            valid |= take
            if layer.source_id not in winners:
                winners.append(layer.source_id)
    return rgb, valid, tuple(winners)
