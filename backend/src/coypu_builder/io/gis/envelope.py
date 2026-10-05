"""Corridor envelope: a buffer polygon around the plan centreline of one or more alignments, as a file.

The polygon is built in the project CRS (metric, so the buffer is a true half-width), simplified with
topology preserved, and only then densified and reprojected for the output. GeoJSON follows RFC 7946
(WGS 84, [longitude, latitude], exterior counter-clockwise); Shapefile follows the ESRI convention
(exterior clockwise, holes counter-clockwise). `shapely` and `pyshp` come from the optional `gis` extra and
are imported inside the functions that need them.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import numpy as np
from pyproj.exceptions import ProjError

from coypu_builder import __version__
from coypu_builder.domain.corridor import plan_centreline
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment

GEOJSON_SUFFIXES = (".geojson", ".json")
SHAPEFILE_SUFFIX = ".shp"
WGS84_EPSG = 4326
_QUAD_SEGS = 16
_GEOGRAPHIC_DECIMALS = 8
_PROJECTED_DECIMALS = 3
_DBF_TEXT_WIDTH = 254
MAX_BUFFER_M = 5000.0
SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")


class GisExtraMissing(RuntimeError):
    """An optional GIS dependency is not installed; the message names the command that installs it."""


def _require(module: str) -> Any:
    try:
        return import_module(module)
    except ImportError as exc:
        raise GisExtraMissing(
            f"the '{module}' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc


def validate_output_crs(epsg: int) -> ProjectCRS:
    """The output CRS must be a horizontal 2D CRS: projected or geographic, never vertical, geocentric,
    compound or engineering. Raises `ValueError` otherwise."""
    try:
        crs = ProjectCRS(epsg)
    except ProjError as exc:
        raise ValueError(f"unknown output CRS EPSG:{epsg}: {exc}") from exc
    horizontal = (crs.crs.is_projected or crs.crs.is_geographic) and not crs.crs.is_compound
    if not horizontal or len(crs.crs.axis_info) != 2:
        raise ValueError(f"EPSG:{epsg} is not a horizontal 2D CRS (projected or geographic)")
    return crs


def output_files(path: Path) -> tuple[Path, ...]:
    """Every file `write_envelope` produces for `path`."""
    path = Path(path)
    if path.suffix.lower() == SHAPEFILE_SUFFIX:
        return tuple(path.with_suffix(ext) for ext in SHAPEFILE_PARTS)
    return (path,)


def check_overwrite(path: Path, overwrite: bool) -> None:
    """Refuse to replace an existing output file (any Shapefile part) unless `overwrite` is set."""
    if overwrite:
        return
    existing = [f for f in output_files(path) if f.exists()]
    if existing:
        names = ", ".join(f.name for f in existing)
        raise FileExistsError(
            f"refusing to overwrite existing file(s): {names}; set overwrite (CLI: --force) to replace"
        )


@dataclass(frozen=True)
class EnvelopeOptions:
    buffer_m: float
    station_from: float | None = None
    station_to: float | None = None
    cap: Literal["round", "flat"] = "round"
    simplify_m: float = 0.5
    densify_m: float = 50.0

    def __post_init__(self) -> None:
        if not 0.0 < self.buffer_m <= MAX_BUFFER_M:
            raise ValueError(f"buffer_m must be > 0 and at most {MAX_BUFFER_M:g} m, got {self.buffer_m}")
        if self.cap not in ("round", "flat"):
            raise ValueError(f"cap must be 'round' or 'flat', got '{self.cap}'")
        if self.simplify_m < 0.0:
            raise ValueError(f"simplify_m must be >= 0, got {self.simplify_m}")
        if not self.densify_m > 0.0:
            raise ValueError(f"densify_m must be > 0, got {self.densify_m}")


@dataclass(frozen=True)
class EnvelopeResult:
    files: tuple[Path, ...]
    epsg: int
    area_m2: float
    vertex_count: int
    bounds: tuple[float, float, float, float]


def corridor_envelope(alignments: Sequence[Alignment], options: EnvelopeOptions):
    """Shapely Polygon or MultiPolygon in the project CRS: the union of each alignment's buffer."""
    shapely = _require("shapely")
    if not alignments:
        raise ValueError("at least one alignment is required")
    ranged = options.station_from is not None or options.station_to is not None
    if ranged and len(alignments) != 1:
        raise ValueError("a station range is only valid with exactly one alignment")

    try:
        buffers = []
        for alignment in alignments:
            points = plan_centreline(alignment, options.station_from, options.station_to)
            line = shapely.LineString(points)
            buffers.append(line.buffer(options.buffer_m, quad_segs=_QUAD_SEGS, cap_style=options.cap))
        geometry = shapely.union_all(buffers)
        if options.simplify_m > 0.0:
            geometry = shapely.simplify(geometry, options.simplify_m, preserve_topology=True)
    except shapely.errors.ShapelyError as exc:
        raise ValueError(f"cannot build the corridor geometry: {exc}") from exc
    return geometry


def default_epsg(path: Path | str, project_crs: ProjectCRS) -> int | None:
    """The output EPSG when none is requested: 4326 for GeoJSON, the project CRS for a Shapefile."""
    return WGS84_EPSG if Path(path).suffix.lower() in GEOJSON_SUFFIXES else project_crs.epsg


def envelope_attributes(
    alignments: Sequence[Alignment], options: EnvelopeOptions, project_crs: ProjectCRS
) -> dict[str, str | float | None]:
    epsg = project_crs.epsg
    return {
        "name": "; ".join(a.name or "unnamed" for a in alignments),
        "buffer_m": float(options.buffer_m),
        "sta_from": options.station_from,
        "sta_to": options.station_to,
        "crs_src": f"EPSG:{epsg}" if epsg is not None else project_crs.name,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": f"coypu-builder {__version__}",
    }


def export_corridor_envelope(
    alignments: Sequence[Alignment],
    options: EnvelopeOptions,
    project_crs: ProjectCRS,
    path: Path | str,
    epsg: int | None = None,
    overwrite: bool = True,
) -> EnvelopeResult:
    """Build the envelope, attach the standard attributes and write it: the one call the CLI and the
    `alignment.envelope` handler share."""
    geometry = corridor_envelope(alignments, options)
    attributes = envelope_attributes(alignments, options, project_crs)
    return write_envelope(
        geometry, Path(path), project_crs, epsg, attributes, densify_m=options.densify_m, overwrite=overwrite
    )


def write_envelope(
    geometry,
    path: Path,
    project_crs: ProjectCRS,
    epsg: int | None,
    attributes: Mapping[str, str | float | None],
    densify_m: float = 50.0,
    overwrite: bool = True,
) -> EnvelopeResult:
    """Write `geometry` (project CRS) to `path`; the suffix picks GeoJSON (.geojson, .json) or Shapefile."""
    shapely = _require("shapely")
    path = Path(path)
    suffix = path.suffix.lower()
    is_geojson = suffix in GEOJSON_SUFFIXES
    if not is_geojson and suffix != SHAPEFILE_SUFFIX:
        raise ValueError(f"unsupported envelope format '{path.suffix}'; use .geojson, .json or .shp")
    if geometry.is_empty:
        raise ValueError("the envelope geometry is empty")
    out_epsg = epsg if epsg is not None else default_epsg(path, project_crs)
    if out_epsg is None:
        raise ValueError("the project CRS has no EPSG code; pass an output epsg")
    target = validate_output_crs(out_epsg)
    check_overwrite(path, overwrite)
    prj_wkt = None
    if not is_geojson:
        _require("shapefile")
        prj_wkt = _esri_wkt(target)

    try:
        area_m2 = float(geometry.area)
        projected = _reproject(geometry, project_crs, target, densify_m, shapely)
        decimals = _PROJECTED_DECIMALS if target.is_projected else _GEOGRAPHIC_DECIMALS
        polygons = _rounded_polygons(projected, decimals, shapely)
        # RFC 7946 and ESRI wind rings in opposite directions; orient last, on the coordinates written.
        sign = 1.0 if is_geojson else -1.0
        polygons = [shapely.geometry.polygon.orient(p, sign=sign) for p in polygons]
        union = shapely.MultiPolygon(polygons)
    except (ProjError, shapely.errors.ShapelyError) as exc:
        raise ValueError(f"cannot build the envelope in EPSG:{out_epsg}: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex[:12]
    if is_geojson:
        pairs = [(path.parent / f"~{token}.tmp", path)]
    else:
        tmp_shp = path.parent / f"~{token}.shp"
        pairs = [(tmp_shp.with_suffix(e), path.with_suffix(e)) for e in SHAPEFILE_PARTS]
    replaced: list[Path] = []
    try:
        if is_geojson:
            _write_geojson(polygons, pairs[0][0], out_epsg, attributes)
        else:
            _write_shapefile(polygons, pairs[0][0], prj_wkt, attributes)
        for tmp, final in pairs:
            os.replace(tmp, final)
            replaced.append(final)
    except BaseException:
        for final in replaced:
            final.unlink(missing_ok=True)
        raise
    finally:
        for tmp, _final in pairs:
            tmp.unlink(missing_ok=True)

    vertex_count = sum(len(r.coords) for p in polygons for r in (p.exterior, *p.interiors))
    min_x, min_y, max_x, max_y = (float(v) for v in union.bounds)
    return EnvelopeResult(
        tuple(final for _tmp, final in pairs), out_epsg, area_m2, vertex_count, (min_x, min_y, max_x, max_y)
    )


def _esri_wkt(target: ProjectCRS) -> str:
    from pyproj.enums import WktVersion

    try:
        return target.crs.to_wkt(WktVersion.WKT1_ESRI)
    except ProjError as exc:
        raise ValueError(f"EPSG:{target.epsg} cannot be written as an ESRI .prj: {exc}") from exc


def _reproject(geometry, source: ProjectCRS, target: ProjectCRS, densify_m: float, shapely):
    if source == target:
        return geometry
    dense = shapely.segmentize(geometry, densify_m)
    coords = shapely.get_coordinates(dense)
    x, y = source.to_crs(target.crs, coords[:, 0], coords[:, 1])
    if not (np.all(np.isfinite(x)) and np.all(np.isfinite(y))):
        raise ValueError("reprojection produced non-finite coordinates; the envelope lies outside the CRS")
    return shapely.set_coordinates(dense, np.column_stack([x, y]))


def _rounded_polygons(geometry, decimals: int, shapely) -> list:
    rounded = shapely.set_coordinates(geometry, np.round(shapely.get_coordinates(geometry), decimals))
    return list(rounded.geoms) if rounded.geom_type == "MultiPolygon" else [rounded]


def _ring(ring) -> list[list[float]]:
    return [[float(x), float(y)] for x, y in ring.coords]


def _write_geojson(polygons, path: Path, epsg: int, attributes) -> None:
    rings = [[_ring(p.exterior), *(_ring(r) for r in p.interiors)] for p in polygons]
    geometry = (
        {"type": "Polygon", "coordinates": rings[0]}
        if len(rings) == 1
        else {"type": "MultiPolygon", "coordinates": rings}
    )
    collection: dict[str, Any] = {"type": "FeatureCollection"}
    if epsg != WGS84_EPSG:
        collection["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"}}
    collection["features"] = [{"type": "Feature", "properties": dict(attributes), "geometry": geometry}]
    path.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _dbf_field(name: str, value: str | float | None) -> tuple[str, str, int, int]:
    if isinstance(value, str):
        return name, "C", _DBF_TEXT_WIDTH, 0
    return name, "N", 19, 6


def _dbf_value(value: str | float | None) -> str | float | None:
    if isinstance(value, str):
        return value.encode("utf-8")[:_DBF_TEXT_WIDTH].decode("utf-8", errors="ignore")
    return value


def _write_shapefile(polygons, shp: Path, prj_wkt: str, attributes) -> None:
    """Build the .shp/.shx/.dbf in memory so a failure never leaves an open or half-written file."""
    shapefile = _require("shapefile")
    rings = [list(map(tuple, _ring(r))) for p in polygons for r in (p.exterior, *p.interiors)]
    names = list(attributes)
    streams = {ext: BytesIO() for ext in (".shp", ".shx", ".dbf")}
    writer = shapefile.Writer(
        shp=streams[".shp"],
        shx=streams[".shx"],
        dbf=streams[".dbf"],
        shapeType=shapefile.POLYGON,
        encoding="utf-8",
    )
    try:
        for name in names:
            writer.field(*_dbf_field(name, attributes[name]))
        writer.poly(rings)
        writer.record(*[_dbf_value(attributes[n]) for n in names])
    except BaseException:
        with contextlib.suppress(Exception):
            writer.close()
        raise
    writer.close()
    for ext, stream in streams.items():
        shp.with_suffix(ext).write_bytes(stream.getvalue())
    shp.with_suffix(".prj").write_text(prj_wkt, encoding="utf-8")
    shp.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
