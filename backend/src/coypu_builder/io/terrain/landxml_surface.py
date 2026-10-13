"""LandXML `<Surface>` TIN reader (Civil 3D, OpenRoads and similar exports).

Each `<Surface>` is one object: its `<Pnts>/<P>` point list ("northing easting elevation" in the
specification's order, resolved through `io/landxml/dialects.py` like every other coordinate token) and its
`<Faces>/<F>` triangles, which refer to the points by `id`. Faces marked invisible (`i="1"`) are dropped and
every kept face is made counter-clockwise in plan view. The file is parsed as a stream with `defusedxml`
(DTDs and entity expansion are refused), and `P`/`F` elements are released as they are read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import CRS
from pyproj.exceptions import CRSError

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainTin, triangle_areas2
from coypu_builder.io.gis.envelope import GisExtraMissing
from coypu_builder.io.landxml.dialects import resolve_coordinates
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import note, sha256_file
from coypu_builder.io.terrain.archive import open_source, peek

FORMAT_ID = "landxml-surface"
MEMBER_SUFFIXES = (".xml", ".landxml")
_SNIFF_HEAD = 4 << 20  # how much of the file sniffing looks at: the LandXML root and a <Surface> within it
_ROOT_HEAD = 8192

# linearUnit attribute (LandXML 1.2 `Metric` / `Imperial`) -> metres
_UNITS_M = {
    "meter": 1.0,
    "metre": 1.0,
    "millimeter": 0.001,
    "centimeter": 0.01,
    "kilometer": 1000.0,
    "foot": 0.3048,
    "ussurveyfoot": 1200.0 / 3937.0,
    "inch": 0.0254,
}


@dataclass
class _Surface:
    index: int
    name: str | None
    point_ids: list[str | None] = field(default_factory=list)
    point_text: list[str] = field(default_factory=list)
    face_text: list[str] = field(default_factory=list)
    face_visible: list[bool] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.name if self.name else str(self.index)


@dataclass
class _Document:
    surfaces: list[_Surface] = field(default_factory=list)
    declared_crs: str | None = None
    linear_unit_m: float | None = None
    unknown_unit: str | None = None
    warnings: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _defused() -> Any:
    try:
        import_module("defusedxml")
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'defusedxml' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc
    from defusedxml import ElementTree as safe_et

    return safe_et


_SURFACE_TAG = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?Surface[\s>/]")
_FIRST_INTEGER = re.compile(r"\d+")


def _declared_crs(attributes: dict[str, str]) -> tuple[str | None, str | None]:
    """(CRS string or None, warning or None) from the attributes of a `<CoordinateSystem>`: `epsgCode`, else
    an `EPSG:` code in `desc` or `name` (the first integer in it). A declaration pyproj does not understand
    is no declaration, with a warning, so `horizontal_crs` can still rescue the file."""
    candidates = [attributes.get("epsgCode", "").strip()]
    candidates += [
        v for v in (attributes.get(k, "").strip() for k in ("desc", "name")) if v.upper().startswith("EPSG")
    ]
    for value in candidates:
        found = _FIRST_INTEGER.search(value)
        if not found:
            continue
        text = f"EPSG:{int(found.group())}"
        try:
            CRS.from_user_input(text)
        except CRSError:
            return None, f"the file declares {text}, which is not a coordinate reference system pyproj knows"
        return text, None
    return None, None


def _parse(path: Path) -> _Document:
    safe_et = _defused()
    document = _Document()
    stack: list[str] = []
    current: _Surface | None = None
    container: Any = None  # the open <Pnts> or <Faces>, emptied as its children are read
    try:
        for event, element in safe_et.iterparse(str(path), events=("start", "end"), forbid_dtd=True):
            tag = _local(element.tag)
            if event == "start":
                stack.append(tag)
                if tag == "Surface":
                    current = _Surface(len(document.surfaces), element.get("name"))
                elif tag in ("Pnts", "Faces") and current is not None:
                    container = element
                continue
            stack.pop()
            parent = stack[-1] if stack else ""
            if tag == "P" and current is not None and parent == "Pnts":
                current.point_ids.append(element.get("id"))
                current.point_text.append(element.text or "")
                container.clear()
            elif tag == "F" and current is not None and parent == "Faces":
                current.face_text.append(element.text or "")
                current.face_visible.append(str(element.get("i", "0")).strip().lower() not in ("1", "true"))
                container.clear()
            elif tag == "Surface" and current is not None:
                document.surfaces.append(current)
                current = None
                element.clear()
            elif tag == "CoordinateSystem" and document.declared_crs is None:
                document.declared_crs, problem = _declared_crs(dict(element.attrib))
                if problem:
                    document.warnings.append(problem)
            elif tag in ("Metric", "Imperial") and parent == "Units":
                unit = (element.get("linearUnit") or "").strip()
                scale = _UNITS_M.get(unit.lower())
                if scale is None:
                    document.unknown_unit = unit or tag
                else:
                    document.linear_unit_m = scale
    except safe_et.ParseError as exc:
        raise ValueError(f"'{path.name}' is not well-formed XML: {exc}") from exc
    return document


def _tokens(surface: _Surface) -> np.ndarray:
    """(n, 3) float64 raw point tokens in file order."""
    rows = [text.split() for text in surface.point_text]
    if any(len(row) != 3 for row in rows):
        raise ValueError(
            f"surface '{surface.key}': every <P> needs exactly three numbers (north east elevation)"
        )
    try:
        return np.array(rows, dtype=np.float64).reshape(-1, 3)
    except ValueError as exc:
        raise ValueError(f"surface '{surface.key}': a <P> holds a value that is not a number") from exc


def _point_index(surface: _Surface) -> tuple[np.ndarray | dict[str, int], np.ndarray | None]:
    """How face references map to point rows: (sorted integer ids, their rows), or an id dictionary."""
    ids = surface.point_ids
    if all(i is None for i in ids):
        return np.arange(1, len(ids) + 1, dtype=np.int64), None
    if any(i is None for i in ids):
        raise ValueError(f"surface '{surface.key}': some <P> elements have an id and some do not")
    try:
        numeric = np.array([int(i) for i in ids], dtype=np.int64)  # type: ignore[arg-type]
    except ValueError:
        lookup = {str(i): row for row, i in enumerate(ids)}
        if len(lookup) != len(ids):
            raise ValueError(f"surface '{surface.key}': duplicate point ids") from None
        return lookup, None
    order = np.argsort(numeric, kind="stable")
    if len(numeric) > 1 and np.any(np.diff(numeric[order]) == 0):
        raise ValueError(f"surface '{surface.key}': duplicate point ids")
    return numeric[order], order


def _faces(surface: _Surface) -> np.ndarray:
    """(m, 3) int32 point rows of the visible faces, in the order given."""
    visible = np.flatnonzero(np.asarray(surface.face_visible, dtype=bool))
    if not len(visible):
        return np.empty((0, 3), dtype=np.int32)
    texts = [surface.face_text[i] for i in visible]
    rows_text = [text.split() for text in texts]
    if any(len(row) != 3 for row in rows_text):
        raise ValueError(f"surface '{surface.key}': every <F> needs exactly three point ids")
    values = [v for row in rows_text for v in row]
    index, order = _point_index(surface)
    if isinstance(index, dict):
        try:
            rows = np.array([index[v] for v in values], dtype=np.int64)
        except KeyError as exc:
            raise ValueError(f"surface '{surface.key}': a face refers to unknown point {exc}") from None
    else:
        try:
            refs = np.array([int(v) for v in values], dtype=np.int64)
        except ValueError:
            raise ValueError(f"surface '{surface.key}': a face refers to a non-numeric point id") from None
        position = np.searchsorted(index, refs)
        position = np.minimum(position, max(len(index) - 1, 0))
        if len(index) == 0 or np.any(index[position] != refs):
            raise ValueError(f"surface '{surface.key}': a face refers to an unknown point id")
        rows = position if order is None else order[position]
    return rows.reshape(-1, 3).astype(np.int32)


def _extent(tokens: np.ndarray, declared: str | None) -> tuple[float, float, float, float]:
    crs = ProjectCRS(declared) if declared else None
    if crs is not None and crs.is_krovak:
        en = resolve_coordinates(tokens[:, :2], crs, "auto", "NE")
    else:
        en = tokens[:, [1, 0]]
    return float(en[:, 0].min()), float(en[:, 1].min()), float(en[:, 0].max()), float(en[:, 1].max())


class LandXmlSurfaceReader:
    format_id = FORMAT_ID
    kinds = frozenset({SourceKind.TERRAIN})
    suffixes = (*MEMBER_SUFFIXES, ".zip")
    native_order = "NE"

    def sniff(self, path: Path) -> bool:
        """A LandXML root and a `<Surface>` within the first 4 MiB; a file that lists its surfaces later
        needs `format_id`. Nothing is extracted."""
        try:
            _name, head = peek(path, MEMBER_SUFFIXES, _SNIFF_HEAD)
        except (ValueError, OSError):
            return False
        return b"<landxml" in head[:_ROOT_HEAD].lower() and _SURFACE_TAG.search(head) is not None

    def inspect(self, path: Path) -> InspectReport:
        with open_source(path, MEMBER_SUFFIXES) as source:
            document = _parse(source.path)
        warnings: list[str] = list(document.warnings)
        if not document.surfaces:
            raise ValueError(f"'{Path(path).name}' holds no <Surface> element")
        points = faces = invisible = 0
        extents = []
        for surface in document.surfaces:
            tokens = _tokens(surface)
            points += len(tokens)
            faces += len(surface.face_text)
            invisible += len(surface.face_text) - sum(surface.face_visible)
            if not surface.face_text:
                warnings.append(f"surface '{surface.key}' has no faces and will be skipped")
            if len(tokens):
                extents.append(_extent(tokens, document.declared_crs))
        extent = (
            (
                min(e[0] for e in extents),
                min(e[1] for e in extents),
                max(e[2] for e in extents),
                max(e[3] for e in extents),
            )
            if extents
            else None
        )
        if document.unknown_unit is not None:
            warnings.append(
                f"the linear unit '{document.unknown_unit}' is not recognised; "
                "set linear_unit_m if it is not metres"
            )
        counts = {"surfaces": len(document.surfaces), "points": points, "faces": faces}
        if invisible:
            counts["invisible_faces"] = invisible
        return InspectReport(
            format_id=FORMAT_ID,
            kind=SourceKind.TERRAIN,
            declared_crs=document.declared_crs,
            extent=extent,
            counts=counts,
            suggested=ImportSettings(
                horizontal_crs=document.declared_crs, linear_unit_m=document.linear_unit_m or 1.0
            ),
            licence_hint=None,
            warnings=tuple(warnings),
        )

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> Iterable[ImportedObject]:
        with open_source(path, MEMBER_SUFFIXES) as source:
            digest = sha256_file(source.path)
            document = _parse(source.path)
            stem = source.stem
        for surface in document.surfaces:
            tin = _build(f"{stem}/{surface.key}", surface, settings, transform, clip_polygon)
            if tin is not None:
                yield ImportedObject(f"{stem}/{surface.key}", digest, tin)


def _build(
    label: str, surface: _Surface, settings: ImportSettings, transform: CrsTransform, polygon: Any
) -> TerrainTin | None:
    triangles = _faces(surface)
    if not len(triangles):
        note(f"{label}: the surface has no visible faces")
        return None
    tokens = _tokens(surface)
    if settings.nodata is not None:
        triangles = triangles[~(tokens[:, 2] == settings.nodata)[triangles].any(axis=1)]
    points = transform(tokens)
    if polygon is not None:
        from shapely import contains_xy

        inside = contains_xy(polygon, points[:, 0], points[:, 1])
        triangles = triangles[inside[triangles].any(axis=1)]
    if not len(triangles):
        note(f"{label}: no triangle has a vertex inside the clip region (or all touch nodata heights)")
        return None
    clockwise = triangle_areas2(points, triangles) < 0.0
    triangles[clockwise] = triangles[clockwise][:, [0, 2, 1]]
    used, inverse = np.unique(triangles, return_inverse=True)
    return TerrainTin(np.ascontiguousarray(points[used]), inverse.reshape(-1, 3).astype(np.int32))
