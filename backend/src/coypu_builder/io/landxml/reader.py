"""LandXML 1.2 reader: raw parse (`read_landxml`) and conversion to domain alignments (`to_alignment`).

Conversion rules
- Positions are taken from the file's Start points; only headings are chained. Line and Curve headings are
  exact from their own geometry; a Spiral inherits the previous element's end heading when it starts on
  that element's end (C1 chain) and otherwise uses its Start→PI tangent (the PI written by some exporters
  is only approximate).
- Element length: `length` attribute if present, else Line chord / arc sweep / spiral `length`; the
  alignment's stations follow `staStart` attributes when present, else run cumulatively.
- Cant `appliedCant` is taken as |mm|; gauge unit is auto-detected (< 10 → metres).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.geometry.arc import CircularArc
from coypu_builder.domain.geometry.cant import CantProfile, RotationPivot
from coypu_builder.domain.geometry.clothoid import Clothoid
from coypu_builder.domain.geometry.horizontal import HorizontalAlignment
from coypu_builder.domain.geometry.line import Line
from coypu_builder.domain.geometry.vertical import VerticalAlignment, VerticalCurve
from coypu_builder.domain.model.modes import Mode
from coypu_builder.io.landxml.dialects import (
    GENERIC,
    Dialect,
    detect_dialect,
    gauge_to_mm,
    resolve_coordinates,
)

CHAIN_TOLERANCE_M = 0.05
"""A spiral starting within this distance of the previous element's computed End inherits its heading."""


@dataclass
class RawElement:
    kind: str  # "Line" | "Curve" | "Spiral"
    sta_start: float | None
    length: float | None
    points: dict[str, tuple[float, float]]  # Start / End / Center / PI raw tokens (t1, t2)
    attrs: dict[str, str]


@dataclass
class RawProfileItem:
    kind: str  # "PVI" | "ParaCurve" | "CircCurve"
    station: float
    elevation: float
    length: float | None = None
    radius: float | None = None


@dataclass
class RawCant:
    stations: list[float] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    gauge: float | None = None
    superelevation_base: float | None = None
    rotation_point: str | None = None
    station_type: str | None = None


@dataclass
class LandXmlAlignment:
    name: str
    sta_start: float
    length: float | None
    elements: list[RawElement]
    profile: list[RawProfileItem]
    cant: RawCant | None
    epsg: int | None
    application_name: str | None
    application_version: str | None
    dialect: Dialect


@dataclass
class ConversionReport:
    max_end_deviation_m: float = 0.0
    max_heading_jump_rad: float = 0.0
    spiral_pi_heading_mismatch_rad: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ConversionResult:
    alignment: Alignment
    report: ConversionReport


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    v = value.strip()
    if v.upper() in ("INF", "INFINITY"):
        return math.inf
    try:
        return float(v)
    except ValueError:
        return None


def _pair(text: str | None) -> tuple[float, float] | None:
    parts = (text or "").split()
    if len(parts) < 2:
        return None
    return float(parts[0]), float(parts[1])


def _parse_epsg(root: ET.Element) -> int | None:
    for el in root.iter():
        if _local(el.tag) != "CoordinateSystem":
            continue
        for key in ("epsgCode", "desc", "name"):
            value = el.get(key) or ""
            digits = "".join(ch for ch in value if ch.isdigit())
            if value.upper().startswith("EPSG") or key == "epsgCode":
                if digits:
                    return int(digits)
    return None


def read_landxml(source: str | Path | bytes) -> list[LandXmlAlignment]:
    root = ET.fromstring(source) if isinstance(source, bytes) else ET.parse(str(source)).getroot()
    app_name = app_version = None
    for el in root:
        if _local(el.tag) == "Application":
            app_name, app_version = el.get("name"), el.get("version")
            break
    dialect = detect_dialect(app_name, app_version)
    epsg = _parse_epsg(root)

    out: list[LandXmlAlignment] = []
    for aln in root.iter():
        if _local(aln.tag) != "Alignment":
            continue
        elements: list[RawElement] = []
        profile: list[RawProfileItem] = []
        cant: RawCant | None = None
        for child in aln:
            tag = _local(child.tag)
            if tag == "CoordGeom":
                for geom in child:
                    kind = _local(geom.tag)
                    if kind not in ("Line", "Curve", "Spiral"):
                        continue
                    points = {}
                    for pt in geom:
                        pair = _pair(pt.text)
                        if pair is not None:
                            points[_local(pt.tag)] = pair
                    elements.append(
                        RawElement(
                            kind,
                            _float(geom.get("staStart")),
                            _float(geom.get("length")),
                            points,
                            dict(geom.attrib),
                        )
                    )
            elif tag == "Profile":
                for prof_align in child:
                    if _local(prof_align.tag) != "ProfAlign":
                        continue
                    for item in prof_align:
                        kind = _local(item.tag)
                        pair = _pair(item.text)
                        if kind in ("PVI", "ParaCurve", "CircCurve") and pair is not None:
                            profile.append(
                                RawProfileItem(
                                    kind,
                                    pair[0],
                                    pair[1],
                                    _float(item.get("length")),
                                    _float(item.get("radius")),
                                )
                            )
            elif tag == "Cant":
                cant = RawCant(
                    gauge=_float(child.get("gauge")),
                    superelevation_base=_float(child.get("superelevationBase")),
                    rotation_point=child.get("rotationPoint"),
                    station_type=child.get("stationType"),
                )
                for st in child:
                    if _local(st.tag) == "CantStation":
                        station, value = _float(st.get("station")), _float(st.get("appliedCant"))
                        if station is not None and value is not None:
                            cant.stations.append(station)
                            cant.values.append(value)
        out.append(
            LandXmlAlignment(
                aln.get("name", ""),
                _float(aln.get("staStart")) or 0.0,
                _float(aln.get("length")),
                elements,
                profile,
                cant,
                epsg,
                app_name,
                app_version,
                dialect,
            )
        )
    return out


def _horizontal(
    raw: LandXmlAlignment, crs: ProjectCRS, coordinate_order: str, report: ConversionReport
) -> HorizontalAlignment:
    segments = []
    bounds: list[float] = []
    running = raw.sta_start
    prev = None
    for i, el in enumerate(raw.elements):
        names = [n for n in ("Start", "End", "Center", "PI") if n in el.points]
        pts = dict(
            zip(
                names,
                resolve_coordinates(np.array([el.points[n] for n in names]), crs, coordinate_order),
                strict=True,
            )
        )
        if "Start" not in pts:
            raise ValueError(f"element {i} ({el.kind}) has no Start point")
        start = pts["Start"]
        sign = 1.0 if el.attrs.get("rot", "ccw").lower() == "ccw" else -1.0

        if el.kind == "Line":
            if "End" not in pts:
                raise ValueError(f"Line {i} has no End point")
            seg = Line.from_points(start, pts["End"])
            if el.length is not None and el.length > 0:
                seg = Line(seg.start_x, seg.start_y, seg.heading_start, el.length)
        elif el.kind == "Curve":
            if "Center" not in pts or "End" not in pts:
                raise ValueError(f"Curve {i} needs Center and End points")
            seg = CircularArc.from_center(
                start, pts["Center"], pts["End"], "ccw" if sign > 0 else "cw", el.length
            )
        else:
            r0, r1 = _float(el.attrs.get("radiusStart")), _float(el.attrs.get("radiusEnd"))
            k0 = 0.0 if r0 in (None, math.inf) or r0 == 0 else sign / r0
            k1 = 0.0 if r1 in (None, math.inf) or r1 == 0 else sign / r1
            length = el.length
            if length is None:
                raise ValueError(f"Spiral {i} has no length")
            pi_heading = None
            if "PI" in pts:
                d = pts["PI"] - start
                pi_heading = float(np.arctan2(d[1], d[0]))
            heading = pi_heading
            if prev is not None and float(np.hypot(*(start - prev.end))) <= CHAIN_TOLERANCE_M:
                heading = prev.heading_end
                if pi_heading is not None:
                    mismatch = abs((heading - pi_heading + np.pi) % (2 * np.pi) - np.pi)
                    report.spiral_pi_heading_mismatch_rad = max(
                        report.spiral_pi_heading_mismatch_rad, float(mismatch)
                    )
            if heading is None:
                raise ValueError(
                    f"Spiral {i} has neither a PI nor a continuous predecessor to take its heading from"
                )
            seg = Clothoid(float(start[0]), float(start[1]), float(heading), float(length), k0, k1)

        if "End" in pts:
            dev = float(np.hypot(*(seg.end - pts["End"])))
            report.max_end_deviation_m = max(report.max_end_deviation_m, dev)
            if dev > 0.01:
                report.warnings.append(
                    f"element {i} ({el.kind}) computed end deviates {dev:.4f} m from file End"
                )

        if el.sta_start is not None:
            if segments and abs(el.sta_start - running) > 1e-3:
                report.warnings.append(
                    f"element {i} staStart {el.sta_start:.4f} differs from running station {running:.4f}"
                )
            running = el.sta_start
        bounds.append(running)
        running += seg.length
        segments.append(seg)
        prev = seg

    end = running
    if raw.length is not None:
        declared_end = raw.sta_start + raw.length
        if abs(declared_end - running) <= 1e-3:
            end = declared_end
        else:
            report.warnings.append(
                f"declared alignment end {declared_end:.4f} differs from geometric end {running:.4f}"
            )
    bounds.append(end)
    station_bounds = np.array(bounds, dtype=np.float64)
    if np.any(np.diff(station_bounds) <= 0):
        report.warnings.append("staStart attributes are not increasing; using geometric stations")
        horizontal = HorizontalAlignment(tuple(segments), station_start=float(station_bounds[0]))
    else:
        horizontal = HorizontalAlignment(tuple(segments), station_bounds=station_bounds)
        scale_dev = float(np.max(np.abs(horizontal.station_scale - 1.0)))
        if scale_dev > 1e-3:
            report.warnings.append(f"element lengths differ from staStart spans by up to {scale_dev:.2%}")
    for j in horizontal.discontinuities():
        report.max_heading_jump_rad = max(report.max_heading_jump_rad, j.heading_jump_rad)
        if j.gap_m > CHAIN_TOLERANCE_M:
            report.warnings.append(f"gap of {j.gap_m:.3f} m before element {j.index}")
    return horizontal


def _vertical(raw: LandXmlAlignment, station_start: float, station_end: float) -> VerticalAlignment:
    if not raw.profile:
        return VerticalAlignment.constant(0.0, station_start, station_end)
    stations: list[float] = []
    elevations: list[float] = []
    curves: list[VerticalCurve] = []
    for item in raw.profile:
        if stations and abs(item.station - stations[-1]) < 1e-6:
            pass  # Feeder writes a PVI and a ParaCurve at the same station; keep one PVI
        else:
            stations.append(item.station)
            elevations.append(item.elevation)
        if item.kind == "ParaCurve" and item.length:
            curves.append(VerticalCurve(item.station, item.length, "parabolic"))
        elif item.kind == "CircCurve":
            length = item.length
            if (length is None or length <= 0) and item.radius and len(stations) >= 2:
                length = None
            if length:
                curves.append(
                    VerticalCurve(item.station, length, "circular", abs(item.radius) if item.radius else None)
                )
    return VerticalAlignment(np.array(stations), np.array(elevations), tuple(curves))


def _cant(raw: LandXmlAlignment, station_start: float, station_end: float, dialect: Dialect) -> CantProfile:
    c = raw.cant
    if c is None or not c.stations:
        return CantProfile.zero(station_start, station_end)
    gauge_mm = gauge_to_mm(c.gauge, dialect.gauge_unit) if c.gauge else 1435.0
    base_mm = gauge_to_mm(c.superelevation_base, "auto") if c.superelevation_base else 1500.0
    return CantProfile(
        np.array(c.stations),
        np.array(c.values),
        gauge_mm=gauge_mm,
        superelevation_base_mm=base_mm,
        pivot=RotationPivot.from_landxml(c.rotation_point),
    )


def to_alignment(
    raw: LandXmlAlignment, crs: ProjectCRS, coordinate_order: str | None = None, mode: Mode = Mode.HEAVY_RAIL
) -> ConversionResult:
    report = ConversionReport()
    dialect = raw.dialect or GENERIC
    order = coordinate_order or dialect.coordinate_order
    horizontal = _horizontal(raw, crs, order, report)
    s0, s1 = float(horizontal.stations[0]), horizontal.station_end
    vertical = _vertical(raw, s0, s1)
    cant = _cant(raw, s0, s1, dialect)
    return ConversionResult(Alignment(horizontal, vertical, cant, name=raw.name, mode=mode), report)
