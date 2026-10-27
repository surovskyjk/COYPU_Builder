"""Imagery reader: GeoTIFF, JPEG and PNG images with a world file, for `kind = imagery` (T-134, Part A).

Georeferencing comes from the GeoTIFF tags or from a world file beside the image (`.jgw`/`.jpgw`,
`.pgw`/`.pngw`, `.tfw`/`.tifw`, or `.wld`); GDAL reads those itself. A world file carries no CRS, so such an
image declares none and the import needs `horizontal_crs`, exactly as any source that declares no CRS
(ADR 0009).

Only the window that covers the clip is read, decimated with average resampling to `display_resolution_m`
(1 m by default, never finer than the source). A source in another CRS is warped onto a grid in the project
CRS with nearest-neighbour sampling. Bands: 1 is grey, 3 is RGB, 4 is RGBA with alpha 0 meaning not valid.

`rasterio` is imported lazily and comes from the optional `gis` extra.
"""

from __future__ import annotations

import hashlib
import math
import zipfile
import zlib
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.imagery import ImageryRaster
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.gis.envelope import GisExtraMissing
from coypu_builder.io.mesh.imagery_tiles import IMAGE_SIZE
from coypu_builder.io.mesh.terrain_tiles import tile_edge
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import note, sha256_file
from coypu_builder.io.terrain.archive import SourceFile, is_zip, open_source, peek
from coypu_builder.io.terrain.raster import clip_window

FORMAT_ID = "raster-image"
MEMBER_SUFFIXES = (".tif", ".tiff", ".jpg", ".jpeg", ".png")
DISPLAY_RESOLUTION_M = tile_edge(0) / IMAGE_SIZE  # 1 m: the L0 tile's pixel
MAX_PIXELS = 100_000_000  # per object, after decimation; above it, ask for a coarser display resolution
ACCEPTED_BANDS = (1, 3, 4)
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG"
_TIFF_MAGICS = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_WORLD_FILE_EXTENSIONS = {
    ".jpg": ("jgw", "jpgw", "wld"),
    ".jpeg": ("jgw", "jpgw", "wld", "jpegw"),
    ".png": ("pgw", "pngw", "wld"),
    ".tif": ("tfw", "tifw", "wld"),
    ".tiff": ("tfw", "tifw", "wld"),
}
_EDGE_POINTS = 21  # samples per side when a footprint is carried between CRSs
_EPS = 1e-9


def _rasterio() -> Any:
    try:
        return import_module("rasterio")
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'rasterio' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc


def _sidecars(path: Path) -> list[Path]:
    """The world files that exist beside `path`."""
    extensions = _WORLD_FILE_EXTENSIONS.get(path.suffix.lower(), ())
    found = []
    for extension in extensions:
        candidate = path.with_suffix(f".{extension}")
        if candidate.is_file():
            found.append(candidate)
    return found


def _content_hash(path: Path) -> str:
    """The image's SHA-256 with its world files, so a changed world file counts as a changed object."""
    files = [path, *_sidecars(path)]
    return hashlib.sha256("".join(sha256_file(f) for f in files).encode("ascii")).hexdigest()


WORLD_FILE_MAX_BYTES = 4096  # a world file is six numbers


def _restore_world_file(archive: Path, source: SourceFile) -> None:
    """A ZIP member is extracted alone, so the world file stored beside it in the archive would be lost: copy
    it next to the extracted image, under the name GDAL looks for."""
    if not is_zip(archive):
        return
    extensions = _WORLD_FILE_EXTENSIONS.get(source.path.suffix.lower(), ())
    stem = Path(source.name).stem.lower()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name = Path(info.filename)
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or name.stem.lower() != stem
                    or name.suffix.lower().lstrip(".") not in extensions
                    or info.file_size > WORLD_FILE_MAX_BYTES
                ):
                    continue
                with bundle.open(info) as handle:
                    data = handle.read(WORLD_FILE_MAX_BYTES + 1)
                if len(data) <= WORLD_FILE_MAX_BYTES:
                    source.path.with_suffix(name.suffix.lower()).write_bytes(data)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise ValueError(f"'{archive.name}' is not a readable ZIP archive: {exc}") from exc


def _check_palette(ds: Any) -> None:
    from rasterio.enums import ColorInterp

    if ds.colorinterp[0] == ColorInterp.palette:
        raise ValueError("the image uses an indexed-colour palette; convert it to RGB")


def _check_bands(count: int) -> None:
    if count not in ACCEPTED_BANDS:
        raise ValueError(f"the image has {count} bands; only 1 (grey), 3 (RGB) and 4 (RGBA) can be imported")


def _check_dtype(dtype: str) -> None:
    if dtype != "uint8":
        raise ValueError(f"the image holds {dtype} samples; only 8-bit (uint8) imagery can be imported")


def _check_pixels(count: int) -> None:
    if count > MAX_PIXELS:
        raise ValueError(
            f"the requested image area holds {count} pixels (limit {MAX_PIXELS}); set a coarser "
            "display_resolution_m or a clip to import less"
        )


def _crs_string(crs: Any) -> str | None:
    if crs is None:
        return None
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg is not None else crs.to_wkt()


class RasterImageReader:
    format_id = FORMAT_ID
    kinds = frozenset({SourceKind.IMAGERY})
    suffixes = (*MEMBER_SUFFIXES, ".zip")
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        try:
            _name, head = peek(path, MEMBER_SUFFIXES, 4)
        except (ValueError, OSError):
            return False
        return head[:3] == _JPEG_MAGIC or head == _PNG_MAGIC or head in _TIFF_MAGICS

    def inspect(self, path: Path) -> InspectReport:
        rasterio = _rasterio()
        warnings: list[str] = []
        with open_source(path, MEMBER_SUFFIXES) as source:
            _restore_world_file(Path(path), source)
            try:
                with rasterio.open(source.path) as ds:
                    _check_palette(ds)
                    declared = _crs_string(ds.crs)
                    extent = tuple(float(v) for v in ds.bounds)
                    counts = {"width": int(ds.width), "height": int(ds.height), "bands": int(ds.count)}
                    dtype = str(ds.dtypes[0])
                    t = ds.transform
                    georeferenced = not t.is_identity
                    rotated = t.b != 0.0 or t.d != 0.0
                    north_up = t.a > 0.0 and t.e < 0.0
                    cell = (abs(float(t.a)), abs(float(t.e)))
            except rasterio.errors.RasterioError as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable image: {exc}") from exc
        _check_bands(counts["bands"])
        _check_dtype(dtype)
        if not georeferenced:
            warnings.append(
                "the image is not georeferenced (no GeoTIFF tags and no world file beside it); "
                "it cannot be imported"
            )
        elif rotated or not north_up:
            warnings.append("the image is rotated or not north-up; only north-up images can be imported")
        else:
            warnings.append(f"resolution: cell size {cell[0]:g} x {cell[1]:g} (source units)")
        if declared is None and georeferenced:
            warnings.append("the world file declares no coordinate reference system")
        return InspectReport(
            format_id=FORMAT_ID,
            kind=SourceKind.IMAGERY,
            declared_crs=declared,
            extent=extent,
            counts=counts,
            suggested=ImportSettings(horizontal_crs=declared),
            licence_hint=None,
            warnings=tuple(warnings),
        )

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> Iterable[ImportedObject]:
        rasterio = _rasterio()
        with open_source(path, MEMBER_SUFFIXES) as source:
            _restore_world_file(Path(path), source)
            digest = _content_hash(source.path)
            try:
                with rasterio.open(source.path) as ds:
                    raster = _read_raster(ds, source.stem, settings, transform, clip_polygon)
            except rasterio.errors.RasterioError as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable image: {exc}") from exc
            key = source.stem
        if raster is not None:
            yield ImportedObject(key, digest, raster)


def _read_raster(
    ds: Any, name: str, settings: ImportSettings, tr: CrsTransform, polygon: Any
) -> ImageryRaster | None:
    t = ds.transform
    if t.is_identity:
        raise ValueError("the image is not georeferenced")
    if t.b != 0.0 or t.d != 0.0 or not (t.a > 0.0 and t.e < 0.0):
        raise ValueError("only north-up images (no rotation, rows running south) can be imported")
    order = tr.native_order if tr.coordinate_order == "auto" else tr.coordinate_order
    if order != "EN":
        raise ValueError(
            "an image is georeferenced by its own transform, which is east-north; coordinate_order 'NE' "
            "does not apply to it"
        )
    if tr.source_crs.is_krovak and min(ds.bounds.left, ds.bounds.bottom, ds.bounds.right, ds.bounds.top) > 0:
        raise ValueError(
            "an image with positive Krovak coordinates (X south, Y west) cannot be read; "
            "give it as East-North (negative) EPSG:5514 values"
        )
    _check_bands(int(ds.count))
    _check_dtype(str(ds.dtypes[0]))
    _check_palette(ds)
    display = settings.display_resolution_m or DISPLAY_RESOLUTION_M
    if tr.identical:
        raster = _read_direct(ds, tr, polygon, display)
    else:
        raster = _read_warped(ds, tr, polygon, display)
    if raster is None:
        note(f"{name}: the image does not overlap the clip region")
        return None
    if not raster.valid.any():
        note(f"{name}: no valid pixels inside the clip region (all nodata, transparent or outside the clip)")
        return None
    return raster


def _decimated_shape(width: int, height: int, src_e: float, src_n: float, display: float) -> tuple[int, int]:
    """Pixels of a window of `width x height` source cells once each is at least `display` metres, and never
    finer than the source."""
    out_w = max(1, math.ceil(width * src_e / max(display, src_e) - _EPS))
    out_h = max(1, math.ceil(height * src_n / max(display, src_n) - _EPS))
    return out_w, out_h


def _colour(data: np.ndarray) -> np.ndarray:
    """(bands, rows, cols) as (rows, cols, 3) uint8: grey is repeated over the three channels."""
    if data.shape[0] == 1:
        return np.ascontiguousarray(np.repeat(np.moveaxis(data, 0, -1), 3, axis=2), dtype=np.uint8)
    return np.ascontiguousarray(np.moveaxis(data[:3], 0, -1), dtype=np.uint8)


def _read_direct(ds: Any, tr: CrsTransform, polygon: Any, display: float) -> ImageryRaster | None:
    from rasterio.enums import Resampling
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
    src_e, src_n = abs(float(t.a)) * unit, abs(float(t.e)) * unit
    out_w, out_h = _decimated_shape(width, height, src_e, src_n, display)
    _check_pixels(out_w * out_h)
    window = Window(col0, row0, width, height)
    shape = (out_h, out_w)
    if ds.count == 1:
        colour = _colour(ds.read([1], window=window, out_shape=(1, *shape), resampling=Resampling.average))
    else:
        colour = _colour(
            ds.read([1, 2, 3], window=window, out_shape=(3, *shape), resampling=Resampling.average)
        )
    valid = np.ones(shape, dtype=bool)
    if ds.count == 4:
        alpha = ds.read(4, window=window, out_shape=shape, resampling=Resampling.nearest)
        valid &= alpha > 0
    elif ds.nodata is not None:
        cells = ds.read(window=window, out_shape=(ds.count, *shape), resampling=Resampling.nearest)
        valid &= ~np.all(cells == ds.nodata, axis=0)
    cell_e, cell_n = width * src_e / out_w, height * src_n / out_h
    origin_e, origin_n = (t.c + col0 * t.a) * unit, (t.f + row0 * t.e) * unit
    raster = ImageryRaster(
        origin_e=origin_e,
        origin_n=origin_n,
        cell_e=cell_e,
        cell_n=cell_n,
        rgb=colour,
        valid=valid,
        native_resolution_m=max(src_e, src_n),
    )
    _mask_outside(raster, polygon)
    return raster


def _mask_outside(raster: ImageryRaster, polygon: Any) -> None:
    """Mark invalid the pixels whose centre lies outside `polygon`, a block of rows at a time."""
    if polygon is None:
        return
    from shapely import contains_xy

    step = max(1, 1_000_000 // max(raster.cols, 1))
    cols = np.arange(raster.cols, dtype=np.float64)
    for start in range(0, raster.rows, step):
        stop = min(start + step, raster.rows)
        e = raster.origin_e + (cols + 0.5) * raster.cell_e
        n = raster.origin_n - (np.arange(start, stop, dtype=np.float64) + 0.5) * raster.cell_n
        ee, nn = np.meshgrid(e, n)
        inside = contains_xy(polygon, ee.ravel(), nn.ravel()).reshape(ee.shape)
        raster.valid[start:stop] &= inside


def _edge_points(left: float, bottom: float, right: float, top: float) -> tuple[np.ndarray, np.ndarray]:
    u = np.linspace(0.0, 1.0, _EDGE_POINTS)
    xs = left + (right - left) * u
    ys = bottom + (top - bottom) * u
    x = np.concatenate([xs, xs, np.full_like(ys, left), np.full_like(ys, right)])
    y = np.concatenate([np.full_like(xs, bottom), np.full_like(xs, top), ys, ys])
    return x, y


def _read_warped(ds: Any, tr: CrsTransform, polygon: Any, display: float) -> ImageryRaster | None:
    """A source in another CRS, warped to a grid in the project CRS by nearest neighbour."""
    from rasterio.crs import CRS as RioCRS
    from rasterio.enums import Resampling
    from rasterio.transform import Affine
    from rasterio.vrt import WarpedVRT

    t, unit = ds.transform, tr.linear_unit_m
    src_crs, project = tr.source_crs.crs, tr.project_crs
    left, bottom, right, top = (v * unit for v in ds.bounds)
    # the source's cell size in the project CRS, measured at its centre the way the height readers do
    cx, cy = (left + right) / 2.0, (bottom + top) / 2.0
    probe_x = np.array([cx, cx + t.a * unit, cx])
    probe_y = np.array([cy, cy, cy + t.e * unit])
    pe, pn = project.from_crs(src_crs, probe_x, probe_y)
    own_e = math.hypot(pe[1] - pe[0], pn[1] - pn[0])
    own_n = math.hypot(pe[2] - pe[0], pn[2] - pn[0])
    fe, fn = project.from_crs(src_crs, *_edge_points(left, bottom, right, top))
    ok = np.isfinite(fe) & np.isfinite(fn)
    if not ok.any():
        raise ValueError("the image cannot be transformed into the project CRS")
    min_e, min_n = float(fe[ok].min()), float(fn[ok].min())
    max_e, max_n = float(fe[ok].max()), float(fn[ok].max())
    if polygon is not None:
        p_min_e, p_min_n, p_max_e, p_max_n = polygon.bounds
        min_e, min_n = max(min_e, p_min_e), max(min_n, p_min_n)
        max_e, max_n = min(max_e, p_max_e), min(max_n, p_max_n)
    if min_e >= max_e or min_n >= max_n:
        return None
    cell_e, cell_n = max(display, own_e), max(display, own_n)
    origin_e = math.floor(min_e / cell_e + _EPS) * cell_e
    origin_n = math.ceil(max_n / cell_n - _EPS) * cell_n
    cols = max(1, math.ceil((max_e - origin_e) / cell_e - _EPS))
    rows = max(1, math.ceil((origin_n - min_n) / cell_n - _EPS))
    _check_pixels(cols * rows)
    grid_transform = Affine(cell_e, 0.0, origin_e, 0.0, -cell_n, origin_n)
    crs = RioCRS.from_wkt(project.crs.to_wkt())
    # `add_alpha` makes the last band the footprint: 0 outside the source and where it has no data
    with WarpedVRT(
        ds,
        crs=crs,
        transform=grid_transform,
        width=cols,
        height=rows,
        resampling=Resampling.nearest,
        add_alpha=ds.count != 4,  # an RGBA source brings its own alpha band
    ) as vrt:
        bands = vrt.read()
    colour = _colour(bands[: 1 if ds.count == 1 else 3])
    valid = bands[-1] > 0
    raster = ImageryRaster(
        origin_e=origin_e,
        origin_n=origin_n,
        cell_e=cell_e,
        cell_n=cell_n,
        rgb=colour,
        valid=valid,
        native_resolution_m=max(own_e, own_n),
    )
    _mask_outside(raster, polygon)
    return raster
