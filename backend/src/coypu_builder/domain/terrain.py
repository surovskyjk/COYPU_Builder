"""Terrain payloads: what the terrain readers hand to the analysis-surface and tile builders (T-132).

Pure numpy, float64 throughout; nothing here is converted to float32 before display tiles are built. Every
position is in the project CRS (E, N, H). The payloads are plain value holders: they validate their shapes
when built and do no geometry. Equality is identity (`eq=False`) because comparing the arrays by `==` is
ambiguous; tests compare the arrays themselves.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, eq=False)
class TerrainPoints:
    """A point cloud clipped to the import region."""

    points: np.ndarray  # (n, 3) float64 -- E, N, H in the project CRS, after clip
    classes: np.ndarray | None  # (n,) uint8 point-cloud classification, if the source has one

    def __post_init__(self) -> None:
        _check_points(self.points, "points")
        if self.classes is not None:
            if self.classes.dtype != np.uint8 or self.classes.shape != (len(self.points),):
                raise ValueError(
                    f"classes must be a uint8 array of shape ({len(self.points)},), "
                    f"got {self.classes.dtype} {self.classes.shape}"
                )


@dataclass(frozen=True, eq=False)
class TerrainGrid:
    """A regular height grid. Row 0 is the northernmost row; cell `[r, c]` covers
    `E in [origin_e + c*cell_e, origin_e + (c+1)*cell_e]` and
    `N in [origin_n - (r+1)*cell_n, origin_n - r*cell_n]`."""

    origin_e: float  # project CRS; the outer corner of cell [0, 0]
    origin_n: float
    cell_e: float  # > 0
    cell_n: float  # > 0; row 0 is the northernmost row
    heights: np.ndarray  # (rows, cols) float64; NaN = nodata or outside the clip

    def __post_init__(self) -> None:
        for name in ("origin_e", "origin_n", "cell_e", "cell_n"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite, got {getattr(self, name)}")
        if not (self.cell_e > 0.0 and self.cell_n > 0.0):
            raise ValueError(f"cell sizes must be > 0, got {self.cell_e} x {self.cell_n}")
        if self.heights.ndim != 2 or self.heights.dtype != np.float64:
            raise ValueError(
                f"heights must be a 2-D float64 array, got {self.heights.dtype} {self.heights.shape}"
            )

    @property
    def rows(self) -> int:
        return int(self.heights.shape[0])

    @property
    def cols(self) -> int:
        return int(self.heights.shape[1])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(min_e, min_n, max_e, max_n) of the whole grid."""
        return (
            self.origin_e,
            self.origin_n - self.rows * self.cell_n,
            self.origin_e + self.cols * self.cell_e,
            self.origin_n,
        )

    def centres(self) -> tuple[np.ndarray, np.ndarray]:
        """Cell-centre coordinates: (E per column, N per row), the latter descending with the row index."""
        e = self.origin_e + (np.arange(self.cols) + 0.5) * self.cell_e
        n = self.origin_n - (np.arange(self.rows) + 0.5) * self.cell_n
        return e, n


@dataclass(frozen=True, eq=False)
class TerrainTin:
    """A triangulated surface as delivered by the source (a LandXML `<Surface>`)."""

    points: np.ndarray  # (n, 3) float64, project CRS
    triangles: np.ndarray  # (m, 3) int32, counter-clockwise in plan view

    def __post_init__(self) -> None:
        _check_points(self.points, "points")
        tri = self.triangles
        if tri.dtype != np.int32 or tri.ndim != 2 or tri.shape[1] != 3:
            raise ValueError(f"triangles must be an (m, 3) int32 array, got {tri.dtype} {tri.shape}")
        if len(tri) and (int(tri.min()) < 0 or int(tri.max()) >= len(self.points)):
            raise ValueError("triangle indices must refer to rows of points")
        if not np.isfinite(self.points).all():
            raise ValueError("a TIN's points must be finite (NaN or infinity found)")


TerrainPayload = TerrainPoints | TerrainGrid | TerrainTin


def _check_points(points: np.ndarray, name: str) -> None:
    if points.dtype != np.float64 or points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f"{name} must be an (n, 3) float64 array, got {points.dtype} {points.shape}")


def triangle_areas2(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    """Twice the signed plan-view area of each triangle: positive = counter-clockwise."""
    a, b, c = (points[triangles[:, k], :2] for k in range(3))
    ab, ac = b - a, c - a
    return ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
