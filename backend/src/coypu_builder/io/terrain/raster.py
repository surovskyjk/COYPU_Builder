"""GeoTIFF / COG height-grid reader.

Only the window of the raster that covers the clip polygon is read. A source already in the project CRS is
returned cell for cell. A source in another CRS is resampled onto a grid in the project CRS (its own
resolution, or `display_resolution_m`) with bilinear interpolation; the destination cell centres are mapped
back into the source with the same pyproj operation the point readers use, so a raster, a point cloud and
a TIN of one project agree to the accuracy that operation states. Heights stay float64, the source's nodata
becomes NaN, and cells outside the clip polygon are NaN.

`rasterio` is imported lazily and comes from the optional `gis` extra.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainGrid
from coypu_builder.io.gis.envelope import GisExtraMissing
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import note, sha256_file
from coypu_builder.io.terrain.archive import open_source, peek

FORMAT_ID = "geotiff"
MEMBER_SUFFIXES = (".tif", ".tiff")
MAX_CELLS = 100_000_000  # per object, float64: refuse beyond ~800 MB and ask for a clip
BLOCK_CELLS = 1_000_000
_EDGE_POINTS = 21  # samples per side when a footprint is carried between CRSs
_EPS = 1e-9
_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def _rasterio() -> Any:
    try:
        return import_module("rasterio")
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'rasterio' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc


def clip_window(
    transform: Any, size: tuple[int, int], bounds: tuple[float, float, float, float]
) -> tuple[int, int, int, int] | None:
    """The smallest whole-cell window `(col_off, row_off, width, height)` of a north-up raster of
    `size = (width, height)` that covers `bounds = (min_x, min_y, max_x, max_y)`, or `None` if they do not
    overlap. Offsets within 1e-9 of a cell edge snap to it, so a clip on the grid takes no extra cell."""
    width, height = size
    min_x, min_y, max_x, max_y = bounds
    col0 = math.floor((min_x - transform.c) / transform.a + _EPS)
    col1 = math.ceil((max_x - transform.c) / transform.a - _EPS)
    row0 = math.floor((max_y - transform.f) / transform.e + _EPS)
    row1 = math.ceil((min_y - transform.f) / transform.e - _EPS)
    col0, col1 = max(col0, 0), min(col1, width)
    row0, row1 = max(row0, 0), min(row1, height)
    if col1 <= col0 or row1 <= row0:
        return None
    return col0, row0, col1 - col0, row1 - row0


class GeoTiffReader:
    format_id = FORMAT_ID
    kinds = frozenset({SourceKind.TERRAIN})
    suffixes = (*MEMBER_SUFFIXES, ".zip")
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        try:
            _name, head = peek(path, MEMBER_SUFFIXES, 4)
        except (ValueError, OSError):
            return False
        return head in _MAGICS

    def inspect(self, path: Path) -> InspectReport:
        rasterio = _rasterio()
        warnings: list[str] = []
        with open_source(path, MEMBER_SUFFIXES) as source:
            try:
                with rasterio.open(source.path) as ds:
                    declared = _crs_string(ds.crs)
                    extent = tuple(float(v) for v in ds.bounds)
                    extent = (extent[0], extent[1], extent[2], extent[3])
                    counts = {"width": int(ds.width), "height": int(ds.height), "bands": int(ds.count)}
                    nodata = ds.nodata
                    t = ds.transform
                    georeferenced = not t.is_identity
                    rotated = t.b != 0.0 or t.d != 0.0
                    north_up = t.a > 0.0 and t.e < 0.0
                    bands = int(ds.count)
                    cell = (abs(float(t.a)), abs(float(t.e)))
            except rasterio.errors.RasterioError as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable raster: {exc}") from exc
        if not georeferenced:
            warnings.append("the raster is not georeferenced; it cannot be imported")
        elif rotated or not north_up:
            warnings.append("the raster is rotated or not north-up; only north-up rasters can be imported")
        if georeferenced:
            warnings.append(f"resolution: cell size {cell[0]:g} x {cell[1]:g} (source units)")
        if bands > 1:
            warnings.append(f"the raster has {bands} bands; band 1 is read as the height")
        return InspectReport(
            format_id=FORMAT_ID,
            kind=SourceKind.TERRAIN,
            declared_crs=declared,
            extent=extent,
            counts=counts,
            suggested=ImportSettings(
                horizontal_crs=declared,
                nodata=float(nodata) if nodata is not None and math.isfinite(nodata) else None,
            ),
            licence_hint=None,
            warnings=tuple(warnings),
        )

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> Iterable[ImportedObject]:
        rasterio = _rasterio()
        with open_source(path, MEMBER_SUFFIXES) as source:
            digest = sha256_file(source.path)
            try:
                with rasterio.open(source.path) as ds:
                    grid = _read_grid(ds, source.stem, settings, transform, clip_polygon)
            except rasterio.errors.RasterioError as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable raster: {exc}") from exc
            key = source.stem
        if grid is not None:
            yield ImportedObject(key, digest, grid)


def _crs_string(crs: Any) -> str | None:
    if crs is None:
        return None
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg is not None else crs.to_wkt()


def _read_grid(
    ds: Any, name: str, settings: ImportSettings, tr: CrsTransform, polygon: Any
) -> TerrainGrid | None:
    t = ds.transform
    if t.is_identity:
        raise ValueError("the raster is not georeferenced")
    if t.b != 0.0 or t.d != 0.0 or not (t.a > 0.0 and t.e < 0.0):
        raise ValueError("only north-up rasters (no rotation, rows running south) can be imported")
    order = tr.native_order if tr.coordinate_order == "auto" else tr.coordinate_order
    if order != "EN":
        raise ValueError(
            "a raster is georeferenced by its own transform, which is east-north; coordinate_order 'NE' "
            "does not apply to it"
        )
    if tr.source_crs.is_krovak and min(ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top) > 0:
        raise ValueError(
            "a raster with positive Krovak coordinates (X south, Y west) cannot be read; "
            "give it as East-North (negative) EPSG:5514 values"
        )
    if tr.identical:
        grid = _read_direct(ds, settings, tr, polygon)
    else:
        grid = _read_resampled(ds, settings, tr, polygon)
    if grid is None:
        note(f"{name}: the raster does not overlap the clip region")
        return None
    if not np.isfinite(grid.heights).any():
        note(f"{name}: no valid heights inside the clip region (all nodata or outside the clip polygon)")
        return None
    return grid


def _raw_heights(ds: Any, window: Any, settings: ImportSettings) -> np.ndarray:
    """Band 1 of `window` as float64 with every kind of nodata (and non-finite value) as NaN."""
    masked = ds.read(1, window=window, masked=True)
    heights = np.ma.filled(masked.astype(np.float64), np.nan)
    if settings.nodata is not None:
        heights[heights == settings.nodata] = np.nan
    heights[~np.isfinite(heights)] = np.nan
    return heights


def _check_size(cells: int) -> None:
    if cells > MAX_CELLS:
        raise ValueError(
            f"the requested raster area holds {cells} cells (limit {MAX_CELLS}); set a clip to import less"
        )


def _read_direct(ds: Any, settings: ImportSettings, tr: CrsTransform, polygon: Any) -> TerrainGrid | None:
    from rasterio.windows import Window

    t, unit = ds.transform, tr.linear_unit_m
    if polygon is None:
        found = (0, 0, ds.width, ds.height)
    else:
        min_e, min_n, max_e, max_n = polygon.bounds
        found = clip_window(
            t, (ds.width, ds.height), (min_e / unit, min_n / unit, max_e / unit, max_n / unit)
        )
    if found is None:
        return None
    col0, row0, width, height = found
    _check_size(width * height)
    heights = _raw_heights(ds, Window(col0, row0, width, height), settings) * unit
    grid = TerrainGrid(
        origin_e=(t.c + col0 * t.a) * unit,
        origin_n=(t.f + row0 * t.e) * unit,
        cell_e=t.a * unit,
        cell_n=-t.e * unit,
        heights=heights,
    )
    _mask_outside(grid, polygon)
    return grid


def _mask_outside(grid: TerrainGrid, polygon: Any) -> None:
    """Set the cells whose centre is outside `polygon` to NaN, a block of rows at a time."""
    if polygon is None:
        return
    from shapely import contains_xy

    e, n = grid.centres()
    step = max(1, BLOCK_CELLS // max(grid.cols, 1))
    for start in range(0, grid.rows, step):
        rows = slice(start, min(start + step, grid.rows))
        ee, nn = np.meshgrid(e, n[rows])
        outside = ~contains_xy(polygon, ee.ravel(), nn.ravel()).reshape(ee.shape)
        grid.heights[rows][outside] = np.nan


def _boundary(min_x: float, min_y: float, max_x: float, max_y: float) -> tuple[np.ndarray, np.ndarray]:
    """Points along the four sides of a rectangle, for carrying its footprint between CRSs."""
    u = np.linspace(0.0, 1.0, _EDGE_POINTS)
    xs = min_x + (max_x - min_x) * u
    ys = min_y + (max_y - min_y) * u
    x = np.concatenate([xs, xs, np.full_like(ys, min_x), np.full_like(ys, max_x)])
    y = np.concatenate([np.full_like(xs, min_y), np.full_like(xs, max_y), ys, ys])
    return x, y


def _bounds_of(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float] | None:
    ok = np.isfinite(x) & np.isfinite(y)
    if not ok.any():
        return None
    return float(x[ok].min()), float(y[ok].min()), float(x[ok].max()), float(y[ok].max())


def _read_resampled(ds: Any, settings: ImportSettings, tr: CrsTransform, polygon: Any) -> TerrainGrid | None:
    from rasterio.windows import Window

    t, unit = ds.transform, tr.linear_unit_m
    src_crs, project = tr.source_crs.crs, tr.project_crs
    width, height = ds.width, ds.height

    # the source footprint, and the resolution of its cells, in the project CRS
    left, bottom, right, top = (v * unit for v in ds.bounds)
    footprint = _bounds_of(*project.from_crs(src_crs, *_boundary(left, bottom, right, top)))
    if footprint is None:
        raise ValueError("the raster cannot be transformed into the project CRS")
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    probe_x = np.array([cx, cx + t.a * unit, cx])
    probe_y = np.array([cy, cy, cy + t.e * unit])
    pe, pn = project.from_crs(src_crs, probe_x, probe_y)
    own_e, own_n = math.hypot(pe[1] - pe[0], pn[1] - pn[0]), math.hypot(pe[2] - pe[0], pn[2] - pn[0])
    if settings.display_resolution_m is not None:
        cell_e = cell_n = settings.display_resolution_m
    else:
        cell_e, cell_n = own_e, own_n
    if not (math.isfinite(cell_e) and math.isfinite(cell_n) and cell_e > 0.0 and cell_n > 0.0):
        raise ValueError("cannot derive a cell size in the project CRS")

    min_e, min_n, max_e, max_n = footprint
    if polygon is not None:
        p_min_e, p_min_n, p_max_e, p_max_n = polygon.bounds
        min_e, min_n, max_e, max_n = (
            max(min_e, p_min_e),
            max(min_n, p_min_n),
            min(max_e, p_max_e),
            min(max_n, p_max_n),
        )
    if min_e >= max_e or min_n >= max_n:
        return None

    origin_e = math.floor(min_e / cell_e + _EPS) * cell_e
    origin_n = math.ceil(max_n / cell_n - _EPS) * cell_n
    cols = max(1, math.ceil((max_e - origin_e) / cell_e - _EPS))
    rows = max(1, math.ceil((origin_n - min_n) / cell_n - _EPS))
    _check_size(cols * rows)
    grid = TerrainGrid(origin_e, origin_n, cell_e, cell_n, np.full((rows, cols), np.nan))

    # the source window that bilinear sampling of that grid can touch, plus two cells of margin
    sx, sy = project.to_crs(src_crs, *_boundary(*grid.bounds))
    needed = _bounds_of(np.asarray(sx) / unit, np.asarray(sy) / unit)
    if needed is None:
        return None
    window = clip_window(t, (width, height), needed)
    if window is None:
        return None
    col0, row0, w, h = window
    col0, row0 = max(col0 - 2, 0), max(row0 - 2, 0)
    w, h = min(w + 4, width - col0), min(h + 4, height - row0)
    _check_size(w * h)
    source = _raw_heights(ds, Window(col0, row0, w, h), settings)

    e_centres, n_centres = grid.centres()
    step = max(1, BLOCK_CELLS // cols)
    for start in range(0, rows, step):
        block = slice(start, min(start + step, rows))
        ee, nn = np.meshgrid(e_centres, n_centres[block])
        ee, nn = ee.ravel(), nn.ravel()
        if polygon is not None:
            from shapely import contains_xy

            inside = contains_xy(polygon, ee, nn)
        else:
            inside = np.ones(len(ee), dtype=bool)
        values = np.full(len(ee), np.nan)
        if inside.any():
            sx, sy = project.to_crs(src_crs, ee[inside], nn[inside])
            values[inside] = _bilinear(source, (col0, row0), t, unit, (width, height), sx, sy)
        grid.heights[block] = values.reshape(-1, cols)
    grid.heights[:] *= unit
    return grid


def _bilinear(
    source: np.ndarray,
    offset: tuple[int, int],
    t: Any,
    unit: float,
    size: tuple[int, int],
    x: np.ndarray,
    y: np.ndarray,
) -> np.ndarray:
    """Bilinear heights at source-CRS points (metres, `unit` applied) from `source`, a window of the raster
    at `offset = (col, row)`. A point is NaN if it is off the raster, or if a cell it has weight on is nodata
    (a zero weight on a nodata cell does not count)."""
    width, height = size
    with np.errstate(invalid="ignore"):
        u = (x / unit - t.c) / t.a - 0.5
        v = (y / unit - t.f) / t.e - 0.5
    valid = (
        np.isfinite(u) & np.isfinite(v) & (u >= -0.5) & (u <= width - 0.5) & (v >= -0.5) & (v <= height - 0.5)
    )
    u = np.clip(np.where(valid, u, 0.0), 0.0, width - 1.0)
    v = np.clip(np.where(valid, v, 0.0), 0.0, height - 1.0)
    c0, r0 = np.floor(u).astype(np.int64), np.floor(v).astype(np.int64)
    c1, r1 = np.minimum(c0 + 1, width - 1), np.minimum(r0 + 1, height - 1)
    fc, fr = u - c0, v - r0
    out = np.zeros(len(u))
    bad = ~valid
    for rr, cc, weight in (
        (r0, c0, (1 - fr) * (1 - fc)),
        (r0, c1, (1 - fr) * fc),
        (r1, c0, fr * (1 - fc)),
        (r1, c1, fr * fc),
    ):
        ri, ci = rr - offset[1], cc - offset[0]
        inside = (ri >= 0) & (ri < source.shape[0]) & (ci >= 0) & (ci < source.shape[1])
        z = np.full(len(u), np.nan)
        z[inside] = source[ri[inside], ci[inside]]
        used = weight > 0.0
        bad |= used & ~np.isfinite(z)
        out += np.where(used & np.isfinite(z), weight * z, 0.0)
    out[bad] = np.nan
    return out
