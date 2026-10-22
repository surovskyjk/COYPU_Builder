"""Analysis surfaces: the ground truth for every terrain query (ADR 0009, decision 5).

A surface answers `heights(e, n)` in the project CRS and returns NaN wherever it holds no data. A TIN stays
the TIN of the original points and a grid stays the native grid; nothing here is resampled, and the display
tiles of `io/mesh/terrain_tiles.py` are never a substitute. Pure numpy and scipy, float64 throughout.

`TerrainStack` layers several surfaces by precedence and is the single query API for cut/fill and draping.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

import numpy as np
from scipy.spatial import Delaunay, QhullError

from coypu_builder.domain.terrain import TerrainGrid, TerrainPayload, TerrainPoints, TerrainTin

Bounds = tuple[float, float, float, float]  # min_e, min_n, max_e, max_n

DEFAULT_MAX_EDGE_M = 30.0  # longest triangle edge kept, for a genuine gap in the data
_INSIDE_TOLERANCE = 1e-9  # barycentric slack, so a query on a shared edge is found by either triangle
_MAX_MOSAIC_FACTOR = 4.0  # a mosaic may hold at most this many times the cells of its sheets
_MAX_MOSAIC_CELLS = 25_000_000  # and at most this many cells (200 MB of float64)


class TerrainSurface(Protocol):
    def heights(self, e: np.ndarray, n: np.ndarray) -> np.ndarray:
        """float64 heights at `(e, n)`, shaped like the broadcast of the inputs; NaN where not covered."""
        ...

    def bounds(self) -> Bounds: ...


def _query(e, n) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    e_arr, n_arr = np.broadcast_arrays(np.asarray(e, dtype=np.float64), np.asarray(n, dtype=np.float64))
    return e_arr.ravel(), n_arr.ravel(), e_arr.shape


# --- TIN ---------------------------------------------------------------------------------------------------


class _BinLevel:
    """The triangles of one cell size, binned by bounding box and sorted by cell id."""

    def __init__(self, cell: float, owners: np.ndarray, low: np.ndarray, high: np.ndarray):
        self.cell = cell
        ix0, iy0 = np.floor(low / cell).astype(np.int64).T
        ix1, iy1 = np.floor(high / cell).astype(np.int64).T
        self.width = int(ix1.max()) + 1
        self.height = int(iy1.max()) + 1
        nx, ny = ix1 - ix0 + 1, iy1 - iy0 + 1
        counts = nx * ny
        total = int(counts.sum())
        entry_owner = np.repeat(np.arange(len(owners), dtype=np.int64), counts)
        first = np.cumsum(counts) - counts
        local = np.arange(total, dtype=np.int64) - np.repeat(first, counts)
        row_width = nx[entry_owner]
        cell_id = (iy0[entry_owner] + local // row_width) * self.width + (
            ix0[entry_owner] + local % row_width
        )
        order = np.argsort(cell_id, kind="stable")
        self.cell_ids = cell_id[order]
        self.entries = owners[entry_owner[order]].astype(np.int32)

    def candidates(self, qx: np.ndarray, qy: np.ndarray, idx: np.ndarray):
        """For the queries `idx`: the start of each one's candidate run in `entries` and its length."""
        x, y = qx[idx], qy[idx]
        with np.errstate(invalid="ignore"):
            ok = (x >= 0.0) & (y >= 0.0)
            cx = np.where(ok, np.floor(x / self.cell), 0.0)
            cy = np.where(ok, np.floor(y / self.cell), 0.0)
        ok &= (cx < self.width) & (cy < self.height)
        cell_id = cy.astype(np.int64) * self.width + cx.astype(np.int64)
        low = np.searchsorted(self.cell_ids, cell_id, side="left")
        count = np.searchsorted(self.cell_ids, cell_id, side="right") - low
        return low, np.where(ok, count, 0)


class _TriangleLocator:
    """Vectorised point location over an arbitrary set of triangles (a Delaunay triangulation, or a TIN as a
    source delivered it, where `Delaunay.find_simplex` cannot apply).

    Triangle bounding boxes are binned on uniform grids and the entries sorted by cell id. A query visits the
    candidates of its cell one rank at a time, so the Python loop is over the (small) maximum number of
    candidates per cell, never over query points. Binning every box into every cell it covers would blow up
    for long thin triangles (a delivered TIN's hull slivers), so a triangle whose box covers more than
    `MAX_CELLS_PER_TRIANGLE` cells at the base cell size goes to a grid with cells twice as large, and so on:
    every triangle has at most that many entries, and the levels are searched finest first.
    """

    MAX_CELLS_PER_TRIANGLE = 16
    MAX_LEVELS = 64  # cells double per level: far beyond any real extent, a hard stop for bad input

    def __init__(self, points: np.ndarray, triangles: np.ndarray):
        self.origin = points[:, :2].min(axis=0)
        self.xy = points[:, :2] - self.origin
        self.z = points[:, 2]
        self.triangles = triangles
        a, b, c = (self.xy[triangles[:, k]] for k in range(3))
        low = np.minimum(np.minimum(a, b), c)
        high = np.maximum(np.maximum(a, b), c)
        extent = np.maximum(high[:, 0] - low[:, 0], high[:, 1] - low[:, 1])
        base = max(1.5 * float(np.median(extent)), 1e-6)
        self.levels: list[_BinLevel] = []
        todo = np.arange(len(triangles), dtype=np.int64)
        cell = base
        for _ in range(self.MAX_LEVELS):
            if not todo.size:
                break
            with np.errstate(invalid="ignore", over="ignore"):
                span = np.floor(high[todo] / cell) - np.floor(low[todo] / cell) + 1.0
            fits = span[:, 0] * span[:, 1] <= self.MAX_CELLS_PER_TRIANGLE
            if fits.any():
                chosen = todo[fits]
                self.levels.append(_BinLevel(cell, chosen, low[chosen], high[chosen]))
            todo = todo[~fits]
            cell *= 2.0
        if todo.size:
            raise ValueError("the triangles cannot be binned (degenerate or non-finite coordinates)")

    @property
    def entry_count(self) -> int:
        return sum(len(level.entries) for level in self.levels)

    def heights(self, e: np.ndarray, n: np.ndarray) -> np.ndarray:
        qx = e - self.origin[0]
        qy = n - self.origin[1]
        out = np.full(len(e), np.nan)
        pending = np.nonzero(np.isfinite(qx) & np.isfinite(qy))[0]
        for level in self.levels:
            if pending.size == 0:
                break
            start, count = level.candidates(qx, qy, pending)
            hit = self._search(level, qx, qy, out, pending, start, count)
            pending = pending[~hit]
        return out

    def _search(self, level, qx, qy, out, idx, start, count) -> np.ndarray:
        """Locate the queries `idx` among the level's candidates; fills `out`, returns which were found."""
        found = np.zeros(len(idx), dtype=bool)
        live = np.nonzero(count > 0)[0]
        rank = 0
        while live.size:
            tri = self.triangles[level.entries[start[live] + rank]]
            ia, ib, ic = tri[:, 0], tri[:, 1], tri[:, 2]
            ax, ay = self.xy[ia, 0], self.xy[ia, 1]
            e1x, e1y = self.xy[ib, 0] - ax, self.xy[ib, 1] - ay
            e2x, e2y = self.xy[ic, 0] - ax, self.xy[ic, 1] - ay
            q = idx[live]
            dx, dy = qx[q] - ax, qy[q] - ay
            za = self.z[ia]
            with np.errstate(divide="ignore", invalid="ignore"):
                det = e1x * e2y - e1y * e2x
                u = (dx * e2y - dy * e2x) / det
                v = (e1x * dy - e1y * dx) / det
                inside = (
                    (u >= -_INSIDE_TOLERANCE) & (v >= -_INSIDE_TOLERANCE) & (u + v <= 1.0 + _INSIDE_TOLERANCE)
                )
                height = za + u * (self.z[ib] - za) + v * (self.z[ic] - za)
            out[q[inside]] = height[inside]
            found[live[inside]] = True
            live = live[~inside & (count[live] > rank + 1)]
            rank += 1
        return found


@dataclass(frozen=True, eq=False)
class TinSurface:
    """A triangulated surface: linear (barycentric) interpolation inside a triangle, NaN outside every one."""

    points: np.ndarray  # (n, 3) float64
    triangles: np.ndarray  # (m, 3) int32
    dropped_triangles: int = 0  # triangles `tin_from_points` removed by either rule; 0 for a given TIN
    dropped_by_clip: int = 0  # of those, the ones whose centroid lay outside the clip

    def __post_init__(self) -> None:
        if self.points.dtype != np.float64 or self.points.ndim != 2 or self.points.shape[1] != 3:
            raise ValueError(
                f"points must be an (n, 3) float64 array, got {self.points.dtype} {self.points.shape}"
            )
        tri = self.triangles
        if tri.dtype != np.int32 or tri.ndim != 2 or tri.shape[1] != 3:
            raise ValueError(f"triangles must be an (m, 3) int32 array, got {tri.dtype} {tri.shape}")
        if not np.isfinite(self.points).all():
            raise ValueError("a TIN's points must be finite (NaN or infinity found)")
        if len(tri) == 0:
            raise ValueError("a TIN needs at least one triangle")
        if int(tri.min()) < 0 or int(tri.max()) >= len(self.points):
            raise ValueError("triangle indices must refer to rows of points")

    @cached_property
    def _locator(self) -> _TriangleLocator:
        return _TriangleLocator(self.points, self.triangles)

    def warm_up(self) -> None:
        """Build the point locator now, so concurrent first queries from several threads do not each build
        one."""
        _ = self._locator

    def heights(self, e, n) -> np.ndarray:
        e_flat, n_flat, shape = _query(e, n)
        return self._locator.heights(e_flat, n_flat).reshape(shape)

    def bounds(self) -> Bounds:
        used = np.zeros(len(self.points), dtype=bool)
        used[self.triangles.ravel()] = True
        xy = self.points[used, :2]
        return (float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max()))


def _collapse_duplicates(points: np.ndarray) -> np.ndarray:
    """Points sorted by (E, N) with coincident plan positions merged to their mean height. Sorting also makes
    the triangulation independent of the order the points arrived in."""
    order = np.lexsort((points[:, 1], points[:, 0]))
    sorted_points = points[order]
    same = (np.diff(sorted_points[:, 0]) == 0.0) & (np.diff(sorted_points[:, 1]) == 0.0)
    if not same.any():
        return sorted_points
    group = np.concatenate([[0], np.cumsum(~same)])
    merged = np.empty((int(group[-1]) + 1, 3))
    first = np.concatenate([[0], np.nonzero(~same)[0] + 1])
    merged[:, :2] = sorted_points[first, :2]
    merged[:, 2] = np.bincount(group, weights=sorted_points[:, 2]) / np.bincount(group)
    return merged


def tin_from_points(
    points: np.ndarray,
    max_edge_m: float | None = None,
    inside: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
) -> TinSurface:
    """Delaunay-triangulate `points` (n, 3) and drop the triangles that would bridge ground without data.

    Two rules, so that no triangle bridges a gap in a concave corridor clip:

    1. `inside(e, n) -> bool array`, when given, is the clip region (it may be a closure over a polygon, which
       keeps this module free of GIS types). A triangle whose centroid is not inside is dropped. This handles
       the concave bends of a corridor exactly, whatever the point density.
    2. A triangle with an edge longer than `max_edge_m` is dropped (default `DEFAULT_MAX_EDGE_M`, 30 m), for a
       real gap in the data such as water or a building that the clip does not know about.

    The clip rule is applied first: `dropped_by_clip` counts its triangles, and `dropped_triangles` counts
    both rules' (the edge rule only among triangles the clip kept). Coincident plan positions are merged to
    their mean height. Raises `ValueError` for fewer than three non-collinear points or when no triangle
    survives.
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3:
        raise ValueError(f"points must be an (n, 3) array, got shape {pts.shape}")
    pts = _collapse_duplicates(pts[np.isfinite(pts).all(axis=1)])
    if len(pts) < 3:
        raise ValueError(f"a TIN needs at least 3 distinct points, got {len(pts)}")
    if max_edge_m is not None and not (math.isfinite(max_edge_m) and max_edge_m > 0.0):
        raise ValueError(f"max_edge_m must be finite and > 0, got {max_edge_m}")
    centre = pts[:, :2].mean(axis=0)
    xy = pts[:, :2] - centre
    try:
        simplices = Delaunay(xy).simplices
    except QhullError as exc:
        raise ValueError("the points are collinear or otherwise cannot be triangulated") from exc
    limit = max_edge_m if max_edge_m is not None else DEFAULT_MAX_EDGE_M

    a, b, c = (xy[simplices[:, k]] for k in range(3))
    longest2 = np.maximum.reduce([((p - q) ** 2).sum(axis=1) for p, q in ((a, b), (b, c), (c, a))])
    in_clip = np.ones(len(simplices), dtype=bool)
    if inside is not None:
        centroid = (a + b + c) / 3.0 + centre
        in_clip = np.asarray(inside(centroid[:, 0], centroid[:, 1]), dtype=bool)
    keep = in_clip & (longest2 <= limit * limit)
    dropped = int(len(simplices) - keep.sum())
    triangles = simplices[keep]
    if len(triangles) == 0:
        raise ValueError(
            f"no triangle is inside the clip with all edges within {limit:g} m; the points are too sparse"
        )
    ccw = (b[keep, 0] - a[keep, 0]) * (c[keep, 1] - a[keep, 1]) - (b[keep, 1] - a[keep, 1]) * (
        c[keep, 0] - a[keep, 0]
    ) > 0.0
    triangles = np.where(ccw[:, None], triangles, triangles[:, [0, 2, 1]])

    used = np.zeros(len(pts), dtype=bool)
    used[triangles.ravel()] = True
    renumber = np.cumsum(used) - 1
    return TinSurface(pts[used], renumber[triangles].astype(np.int32), dropped, int((~in_clip).sum()))


# --- grid --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class GridSurface:
    """A regular grid read as heights at the cell centres, interpolated bilinearly. Inside `bounds()` but
    outside the outermost centres the nearest centre row and column are used, so every cell is covered.
    The result is NaN when any of the four surrounding cells is NaN."""

    grid: TerrainGrid

    def heights(self, e, n) -> np.ndarray:
        g = self.grid
        e_flat, n_flat, shape = _query(e, n)
        min_e, min_n, max_e, max_n = g.bounds
        with np.errstate(invalid="ignore"):
            inside = (e_flat >= min_e) & (e_flat <= max_e) & (n_flat >= min_n) & (n_flat <= max_n)
        fx = np.clip(
            (np.where(inside, e_flat, min_e) - (g.origin_e + 0.5 * g.cell_e)) / g.cell_e, 0.0, g.cols - 1
        )
        fy = np.clip(
            ((g.origin_n - 0.5 * g.cell_n) - np.where(inside, n_flat, max_n)) / g.cell_n, 0.0, g.rows - 1
        )
        c0 = np.minimum(np.floor(fx).astype(np.int64), max(g.cols - 2, 0))
        r0 = np.minimum(np.floor(fy).astype(np.int64), max(g.rows - 2, 0))
        c1 = np.minimum(c0 + 1, g.cols - 1)
        r1 = np.minimum(r0 + 1, g.rows - 1)
        tx, ty = fx - c0, fy - r0
        h = g.heights
        top = (1.0 - tx) * h[r0, c0] + tx * h[r0, c1]
        bottom = (1.0 - tx) * h[r1, c0] + tx * h[r1, c1]
        out = (1.0 - ty) * top + ty * bottom
        out[~inside] = np.nan
        return out.reshape(shape)

    def bounds(self) -> Bounds:
        return self.grid.bounds


def _mosaic(grids: Sequence[TerrainGrid]) -> TerrainGrid | None:
    """One grid holding all of `grids` (earlier ones win where they overlap), or None when they do not share
    a cell size and a cell lattice, or would leave too large a hole between them."""
    first = grids[0]
    ce, cn = first.cell_e, first.cell_n
    placed: list[tuple[TerrainGrid, int, int]] = []
    for grid in grids:
        if not (math.isclose(grid.cell_e, ce, rel_tol=1e-9) and math.isclose(grid.cell_n, cn, rel_tol=1e-9)):
            return None
        col = (grid.origin_e - first.origin_e) / ce
        row = (first.origin_n - grid.origin_n) / cn
        if abs(col - round(col)) > 1e-6 or abs(row - round(row)) > 1e-6:
            return None
        placed.append((grid, round(col), round(row)))
    c_min = min(c for _, c, _ in placed)
    r_min = min(r for _, _, r in placed)
    c_max = max(c + g.cols for g, c, _ in placed)
    r_max = max(r + g.rows for g, _, r in placed)
    cells = (c_max - c_min) * (r_max - r_min)
    if cells > _MAX_MOSAIC_FACTOR * sum(g.heights.size for g in grids) or cells > _MAX_MOSAIC_CELLS:
        return None
    merged = np.full((r_max - r_min, c_max - c_min), np.nan)
    for grid, col, row in reversed(placed):
        window = merged[row - r_min : row - r_min + grid.rows, col - c_min : col - c_min + grid.cols]
        valid = ~np.isnan(grid.heights)
        window[valid] = grid.heights[valid]
    return TerrainGrid(first.origin_e + c_min * ce, first.origin_n - r_min * cn, ce, cn, merged)


# --- stack and sources -------------------------------------------------------------------------------------


NO_SOURCE = -1  # `TerrainStack.heights_and_source` index where no surface has data
RESOLUTION_BAND_STEP = 0.5  # sources whose log2 resolution rounds to the same multiple of this are tied
_RESOLUTION_SAMPLE = 20_000  # query points for the median point spacing of a cloud


@dataclass(frozen=True, eq=False)
class TerrainStack:
    """Surfaces by precedence, highest first: the height at a point is the first one that has data there.

    `source_ids`, when given, names the source of each surface (one per surface, same order), so that
    `heights_and_source` can say which source answered."""

    surfaces: tuple[TerrainSurface, ...]
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_ids and len(self.source_ids) != len(self.surfaces):
            raise ValueError(
                f"source_ids names {len(self.source_ids)} sources for {len(self.surfaces)} surfaces"
            )

    def warm_up(self) -> None:
        """Prepare every surface that has something to prepare (see `TinSurface.warm_up`)."""
        for surface in self.surfaces:
            warm = getattr(surface, "warm_up", None)
            if warm is not None:
                warm()

    def heights_and_source(self, e, n) -> tuple[np.ndarray, np.ndarray]:
        """`(heights, source_index)`: the first surface's value at each point and the index of that surface in
        `surfaces` (`NO_SOURCE`, -1, where none has data). Both are shaped like the broadcast inputs."""
        e_flat, n_flat, shape = _query(e, n)
        out = np.full(e_flat.shape, np.nan)
        index = np.full(e_flat.shape, NO_SOURCE, dtype=np.int16)
        for number, surface in enumerate(self.surfaces):
            missing = np.nonzero(np.isnan(out))[0]
            if missing.size == 0:
                break
            found = surface.heights(e_flat[missing], n_flat[missing])
            out[missing] = found
            index[missing[~np.isnan(found)]] = number
        return out.reshape(shape), index.reshape(shape)

    def heights(self, e, n) -> np.ndarray:
        return self.heights_and_source(e, n)[0]

    def bounds(self) -> Bounds:
        if not self.surfaces:
            raise ValueError("an empty stack has no bounds")
        boxes = [s.bounds() for s in self.surfaces]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


# --- precedence  -----------------------------------------------------------------------------------------


def payload_resolution_m(payload: TerrainPayload) -> float | None:
    """The declared resolution of one terrain object, in metres (smaller is finer): the median distance from a
    point to its nearest neighbour for a cloud (measured from up to 20 000 evenly spread query points against
    the whole cloud), the geometric mean cell size for a grid, the median edge length for a TIN. None when
    it cannot be measured (fewer than two points)."""
    if isinstance(payload, TerrainGrid):
        return float(math.sqrt(payload.cell_e * payload.cell_n))
    if isinstance(payload, TerrainPoints):
        points = payload.points[:, :2]
        if len(points) < 2:
            return None
        from scipy.spatial import cKDTree

        step = max(1, len(points) // _RESOLUTION_SAMPLE)
        centred = points - points.mean(axis=0)
        distance, _ = cKDTree(centred).query(centred[::step], k=2)
        return float(np.median(distance[:, 1]))
    if isinstance(payload, TerrainTin):
        if len(payload.triangles) == 0:
            return None
        step = max(1, len(payload.triangles) // _RESOLUTION_SAMPLE)
        tri = payload.triangles[::step]
        xy = payload.points[:, :2]
        a, b, c = (xy[tri[:, k]] for k in range(3))
        lengths = np.concatenate([np.hypot(*(p - q).T) for p, q in ((a, b), (b, c), (c, a))])
        return float(np.median(lengths))
    raise ValueError(f"not a terrain payload: {type(payload).__name__}")


def source_resolution_m(payloads: Sequence[TerrainPayload]) -> float | None:
    """The finest resolution among a source's objects, None if none can be measured."""
    values = [r for r in (payload_resolution_m(p) for p in payloads) if r is not None and r > 0.0]
    return min(values) if values else None


def resolution_band(resolution_m: float | None) -> float:
    """Sources are compared by band, not by the raw number: a measured spacing wobbles by a few per cent
    between two surveys of the same density, and that must not decide precedence. A band is a factor of
    sqrt(2) wide; an unknown resolution is the coarsest band."""
    if resolution_m is None or not (math.isfinite(resolution_m) and resolution_m > 0.0):
        return math.inf
    return round(math.log2(resolution_m) / RESOLUTION_BAND_STEP)


@dataclass(frozen=True, eq=False)
class StackSource:
    """One terrain source on its way into a `TerrainStack`."""

    source_id: str
    surface: TerrainSurface
    resolution_m: float | None
    recency: int  # larger = imported later
    from_provider: bool = (
        False  # a generic provider (ADR 0009 decision 6): a user's own file beats it in a band
    )


def order_sources(sources: Sequence[StackSource]) -> list[StackSource]:
    """Precedence order: finest declared resolution first (by `resolution_band`); within a band a user's own
    file before a provider (`from_provider`); then the most recently imported first."""
    return sorted(sources, key=lambda s: (resolution_band(s.resolution_m), s.from_provider, -s.recency))


def stack_from_sources(sources: Sequence[StackSource]) -> TerrainStack:
    """A `TerrainStack` of `sources` in precedence order, remembering which source each surface is."""
    ordered = order_sources(sources)
    return TerrainStack(tuple(s.surface for s in ordered), tuple(s.source_id for s in ordered))


def surface_for_source(
    payloads: Sequence[TerrainPayload],
    max_edge_m: float | None = None,
    inside: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
) -> TerrainSurface:
    """The analysis surface of one source, from all of its objects.

    Every point object is merged before triangulating, so the seams between sheets are continuous. A TIN
    keeps its own triangles. Grids that share a cell size and a cell lattice become one `GridSurface`;
    others stay separate. A source of one kind gives that surface; a mixed one a `TerrainStack` in the order
    in which each kind first appears. `max_edge_m` and `inside` (the
    source's clip region) are passed to `tin_from_points`; a TIN as delivered and grids are used as they are.
    """
    if not payloads:
        raise ValueError("a source needs at least one terrain object")
    point_clouds = [p.points for p in payloads if isinstance(p, TerrainPoints)]
    grids = [p for p in payloads if isinstance(p, TerrainGrid)]
    merged_grids: list[TerrainGrid] = []
    if grids:
        mosaic = _mosaic(grids)
        merged_grids = [mosaic] if mosaic is not None else grids

    surfaces: list[TerrainSurface] = []
    points_done = grids_done = False
    for payload in payloads:
        if isinstance(payload, TerrainPoints):
            if not points_done:
                points_done = True
                surfaces.append(tin_from_points(np.vstack(point_clouds), max_edge_m, inside))
        elif isinstance(payload, TerrainGrid):
            if not grids_done:
                grids_done = True
                surfaces.extend(GridSurface(g) for g in merged_grids)
        elif isinstance(payload, TerrainTin):
            surfaces.append(TinSurface(payload.points, payload.triangles))
        else:
            raise ValueError(f"not a terrain payload: {type(payload).__name__}")
    return surfaces[0] if len(surfaces) == 1 else TerrainStack(tuple(surfaces))
