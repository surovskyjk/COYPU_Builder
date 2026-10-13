"""LAS / LAZ point-cloud reader (ASPRS LAS 1.0-1.4, LAZ through the `lazrs` backend of `laspy`).

Coordinates are `integer * scale + offset` evaluated in float64 by `laspy`; they are never narrowed to float32
on the way to the project CRS (Křovák values sit near -1,025,000 m, where float32 resolves only about
a decimetre). The file is read in chunks, so the memory that is held is the kept points, not the whole sheet.
A ZIP holding one `.las` or `.laz` reads like the bare file.

`laspy` is imported lazily and comes from the optional `gis` extra.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import CRS

from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainPoints
from coypu_builder.io.gis.envelope import GisExtraMissing
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import note, sha256_file
from coypu_builder.io.terrain.archive import open_source, peek

FORMAT_ID = "las"
MEMBER_SUFFIXES = (".las", ".laz")
CHUNK_POINTS = 1_000_000
SAMPLE_POINTS = 5_000_000  # at most this many points are read for the class histogram ...
SAMPLE_BLOCKS = 50  # ... in this many evenly spaced blocks across the file
_MAGIC = b"LASF"


def _laspy() -> Any:
    try:
        return import_module("laspy")
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'laspy' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc


def _crs_string(crs: CRS) -> str:
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg is not None else crs.to_wkt()


class LasReader:
    format_id = FORMAT_ID
    kinds = frozenset({SourceKind.TERRAIN})
    suffixes = (*MEMBER_SUFFIXES, ".zip")
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        try:
            _name, head = peek(path, MEMBER_SUFFIXES, len(_MAGIC))
        except (ValueError, OSError):
            return False
        return head == _MAGIC

    def inspect(self, path: Path) -> InspectReport:
        laspy = _laspy()
        warnings: list[str] = []
        with open_source(path, MEMBER_SUFFIXES) as source:
            try:
                with laspy.open(source.path) as reader:
                    header = reader.header
                    declared = _declared_crs(header, warnings)
                    count = int(header.point_count)
                    extent = (
                        (
                            float(header.mins[0]),
                            float(header.mins[1]),
                            float(header.maxs[0]),
                            float(header.maxs[1]),
                        )
                        if count
                        else None
                    )
                    counts: dict[str, int] = {
                        "points": count,
                        f"point_format_{int(header.point_format.id)}": 1,
                    }
                    if count:
                        sample = _sample_classes(reader, count)
                        codes, hits = np.unique(sample, return_counts=True)
                        counts["sample_points"] = len(sample)
                        for code, hit in zip(codes, hits, strict=True):
                            counts[f"sample_class_{int(code)}"] = int(hit)
                    else:
                        warnings.append("the file holds no points")
            except laspy.errors.LaspyException as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable LAS/LAZ file: {exc}") from exc
        return InspectReport(
            format_id=FORMAT_ID,
            kind=SourceKind.TERRAIN,
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
        laspy = _laspy()
        with open_source(path, MEMBER_SUFFIXES) as source:
            digest = sha256_file(source.path)
            try:
                payload = _load(laspy, source.path, source.stem, settings, transform, clip_polygon)
            except laspy.errors.LaspyException as exc:
                raise ValueError(f"'{Path(path).name}' is not a readable LAS/LAZ file: {exc}") from exc
            key = source.stem
        if payload is not None:
            yield ImportedObject(key, digest, payload)


def _sample_classes(reader: Any, count: int) -> np.ndarray:
    """Classification codes of the whole file when it is small, else of `SAMPLE_BLOCKS` blocks spread evenly
    over it (so a file sorted by class still shows its classes)."""
    if count <= SAMPLE_POINTS:
        return np.concatenate(
            [np.asarray(c.classification, dtype=np.uint8) for c in reader.chunk_iterator(CHUNK_POINTS)]
        )
    block = SAMPLE_POINTS // SAMPLE_BLOCKS
    parts = []
    for start in np.unique(np.linspace(0, count - block, SAMPLE_BLOCKS).astype(np.int64)):
        reader.seek(int(start))
        parts.append(np.asarray(reader.read_points(block).classification, dtype=np.uint8))
    return np.concatenate(parts)


def _declared_crs(header: Any, warnings: list[str]) -> str | None:
    try:
        crs = header.parse_crs()
    except Exception as exc:  # a malformed CRS record must not stop an inspect
        warnings.append(f"the CRS record in the header could not be parsed ({type(exc).__name__}: {exc})")
        return None
    if crs is None:
        return None  # the import framework warns about a source that declares no CRS
    return _crs_string(crs)


def _load(
    laspy: Any, path: Path, name: str, settings: ImportSettings, transform: CrsTransform, clip_polygon: Any
) -> TerrainPoints | None:
    wanted = None if settings.point_classes is None else np.asarray(settings.point_classes, dtype=np.int64)
    if clip_polygon is not None:
        from shapely import contains_xy
    points: list[np.ndarray] = []
    classes: list[np.ndarray] = []
    present: set[int] = set()
    total = selected = valid = 0  # points seen, after the class filter, after nodata
    with laspy.open(path) as reader:
        for chunk in reader.chunk_iterator(CHUNK_POINTS):
            xyz = np.column_stack(
                [np.asarray(axis, dtype=np.float64) for axis in (chunk.x, chunk.y, chunk.z)]
            )
            cls = np.asarray(chunk.classification, dtype=np.uint8)
            total += len(xyz)
            present.update(int(c) for c in np.unique(cls))
            if wanted is not None:
                keep = np.isin(cls, wanted)
                xyz, cls = xyz[keep], cls[keep]
            selected += len(xyz)
            keep = np.isfinite(xyz).all(axis=1)
            if settings.nodata is not None:
                keep &= xyz[:, 2] != settings.nodata
            xyz, cls = xyz[keep], cls[keep]
            valid += len(xyz)
            if not len(xyz):
                continue
            xyz = transform(xyz)
            if clip_polygon is not None:
                inside = contains_xy(clip_polygon, xyz[:, 0], xyz[:, 1])
                xyz, cls = xyz[inside], cls[inside]
            if len(xyz):
                points.append(xyz)
                classes.append(cls)
    if points:
        return TerrainPoints(np.ascontiguousarray(np.vstack(points)), np.concatenate(classes))
    if not total:
        note(f"{name}: the file holds no points")
    elif not selected:
        note(
            f"{name}: no points with classes {tuple(settings.point_classes or ())}; "
            f"this file's classes are {sorted(present)}"
        )
    elif not valid:
        note(f"{name}: every point of the selected classes has the nodata height or is not finite")
    else:
        note(f"{name}: no points inside the clip region")
    return None
