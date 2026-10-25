"""The optimised alignment of a `.coypu` archive (T-129), rebuilt from COYPU's own element arrays.

COYPU's optimiser re-geometrises the horizontal alignment and re-stations it onto an active chainage, and the
archive keeps the optimised per-type element arrays (`alignmentsData.landXml`). `optimised_alignment` rebuilds
the alignment from those arrays, in the raw token order of the embedded LandXML, and gates the result against
COYPU's dense axis. The profile and the cant then come from the original file or from COYPU's calculation,
re-stationed onto the active chainage. See docs/data-contracts/coypu-kinematics.md, "Optimised alignments".
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np
from scipy.spatial import cKDTree

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.io.coypu.archive import CoypuProject, baseline_to_active_m
from coypu_builder.io.landxml.reader import LandXmlAlignment, RawCant, RawElement, to_alignment

CONTINUITY_TOLERANCE_M = 1e-3
DENSE_TOLERANCE_M = 20e-3
DENSE_AXIS_SAMPLE_M = 0.1

GATE_SEVERAL_ALIGNMENTS = "the archive embeds several alignments"
GATE_ARRAYS = "the per-type element arrays are missing or do not match the element count"
GATE_CONTINUITY = "the elements do not chain end to start within 1 mm"
GATE_DENSE = "the rebuilt axis does not match COYPU's dense axis within 20 mm"
GATE_CRS = "the archive names no coordinate system, or one that is not known"
GATE_CONVERSION = "the optimised elements cannot be converted to an axis"
GATE_CHAINAGE = "the chainage map is not strictly increasing on the baseline"
CONVERSION_ERRORS = (ArithmeticError, IndexError, KeyError, TypeError, ValueError)

FALLBACK_WARNING = "optimised geometry not used: {gate}; the run was mapped back to the original geometry"
OPTIMISED_WARNING = (
    "COYPU optimised this alignment: Builder uses the optimised geometry from the archive ({n} elements; "
    "largest deviation from COYPU's dense axis {d:.1f} mm), with the original file's profile re-stationed "
    "onto it and {cant}"
)
CALCULATED_CANT_LABEL = "COYPU's calculated cant"
RESTATIONED_CANT_LABEL = "the original cant re-stationed onto it"

SIMULATED_STOPS_KEY = "simulatedTrainStops"
STOP_TOLERANCE_MM = 1.0
SIMULATED_STOPS_WARNING = (
    "the stops Builder maps onto the optimised chainage differ from the stops COYPU simulated by up to "
    "{d:.3f} mm"
)
SIMULATED_STOPS_COUNT_WARNING = (
    "COYPU simulated {m} stop(s) but the archive schedules {n}; "
    "the stops Builder maps are not those simulated"
)

ELEMENT_KINDS = ("Line", "Spiral", "Curve")

# Point children of each element kind, as (LandXML name, archive X key, archive Y key)
POINT_ARRAYS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "Line": (("Start", "lineStartX", "lineStartY"), ("End", "lineEndX", "lineEndY")),
    "Spiral": (
        ("Start", "spiralStartX", "spiralStartY"),
        ("PI", "spiralPIX", "spiralPIY"),
        ("End", "spiralEndX", "spiralEndY"),
    ),
    "Curve": (
        ("Start", "curveStartX", "curveStartY"),
        ("Center", "curveCenterX", "curveCenterY"),
        ("End", "curveEndX", "curveEndY"),
    ),
}

# Per-type arrays that are not points: the element stations and the attributes an element is built from
ATTRIBUTE_ARRAYS: dict[str, tuple[str, ...]] = {
    "Line": ("lineStationStart",),
    "Spiral": (
        "spiralStationStart",
        "spiralLength",
        "spiralRadiusStart",
        "spiralRadiusEnd",
        "spiralRot",
        "spiralType",
    ),
    "Curve": ("curveStationStart", "curveRadius", "curveRot"),
}

CALCULATED_CANT_STATIONS = "stationCantPossible"
CALCULATED_CANT_VALUES = "cantPossible"
DENSE_AXIS_KEY = "denseAlignment"


def _attribute_text(value: Any) -> str:
    number = float(value)
    if math.isinf(number):
        return "INF"
    return repr(number)


def _element_attributes(kind: str, index: int, arrays: dict[str, np.ndarray]) -> dict[str, str]:
    if kind == "Spiral":
        return {
            "radiusStart": _attribute_text(arrays["spiralRadiusStart"][index]),
            "radiusEnd": _attribute_text(arrays["spiralRadiusEnd"][index]),
            "rot": str(arrays["spiralRot"][index]),
            "spiType": str(arrays["spiralType"][index]),
        }
    if kind == "Curve":
        return {
            "radius": _attribute_text(arrays["curveRadius"][index]),
            "rot": str(arrays["curveRot"][index]),
        }
    return {}


def _geometric_length_m(kind: str, index: int, arrays: dict[str, np.ndarray]) -> float | None:
    """The length an element is built with, as the embedded file gives it: a spiral's `length` attribute, and
    for a line or a curve none, so the length follows from the chord or the swept arc, as COYPU's dense
    sampling does. The station span is not used: on the Kralupy fixture it is 0.36 mm off a curve's arc
    length, which would move the rebuilt positions by that much."""
    if kind == "Spiral":
        return float(arrays["spiralLength"][index])
    return None


def _element_kinds(landxml: dict[str, Any]) -> list[str] | None:
    """One kind per element, from the two-entries-per-element `geometryType`; None when the pairs disagree,
    the stations do not pair up with them, or a kind is unknown."""
    geometry_type = landxml.get("geometryType")
    station_horizontal = landxml.get("stationHorizontal")
    if geometry_type is None or station_horizontal is None:
        return None
    entries = [str(kind) for kind in np.asarray(geometry_type)]
    if not entries or len(entries) % 2 or len(np.asarray(station_horizontal)) != len(entries):
        return None
    kinds = entries[::2]
    if any(entries[2 * i + 1] != kind for i, kind in enumerate(kinds)):
        return None
    return kinds if all(kind in ELEMENT_KINDS for kind in kinds) else None


def elements_from_arrays(
    project: CoypuProject, original: LandXmlAlignment
) -> tuple[LandXmlAlignment | None, str | None]:
    """The horizontal alignment COYPU's element arrays describe, with the header of `original`, and the first
    gate it fails (None when gates 1 and 2 pass). Gate 1 is the per-type arrays, gate 2 the continuity of the
    elements; the dense axis is gate 3 and is checked by `optimised_alignment`.

    The element of type T at position i takes entry i of the type's arrays, the way
    `landxml_exporter.buildElementTable` and `geometry_engine.buildElementList` count them."""
    landxml = project.landxml
    kinds = _element_kinds(landxml)
    if kinds is None:
        return None, GATE_ARRAYS
    stations_km = np.asarray(landxml["stationHorizontal"], dtype=float)
    if not np.all(np.isfinite(stations_km)):
        return None, GATE_ARRAYS

    arrays: dict[str, np.ndarray] = {}
    for kind in ELEMENT_KINDS:
        count = kinds.count(kind)
        keys = [key for _, x_key, y_key in POINT_ARRAYS[kind] for key in (x_key, y_key)]
        keys += list(ATTRIBUTE_ARRAYS[kind])
        for key in keys:
            if key not in landxml or len(np.asarray(landxml[key])) != count:
                return None, GATE_ARRAYS
            arrays[key] = np.asarray(landxml[key])

    for i in range(len(kinds) - 1):
        if abs(stations_km[2 * i + 2] - stations_km[2 * i + 1]) * 1000.0 > CONTINUITY_TOLERANCE_M:
            return None, GATE_CONTINUITY

    counters = dict.fromkeys(ELEMENT_KINDS, 0)
    elements: list[RawElement] = []
    for element_index, kind in enumerate(kinds):
        type_index = counters[kind]
        counters[kind] += 1
        start_km = float(stations_km[2 * element_index])
        points = {
            name: (float(arrays[x_key][type_index]), float(arrays[y_key][type_index]))
            for name, x_key, y_key in POINT_ARRAYS[kind]
        }
        elements.append(
            RawElement(
                kind=kind,
                sta_start=start_km * 1000.0,
                length=_geometric_length_m(kind, type_index, arrays),
                points=points,
                attrs=_element_attributes(kind, type_index, arrays),
            )
        )

    for previous, following in zip(elements, elements[1:], strict=False):
        gap_m = float(np.hypot(*(np.asarray(previous.points["End"]) - np.asarray(following.points["Start"]))))
        if gap_m > CONTINUITY_TOLERANCE_M:
            return None, GATE_CONTINUITY

    rebuilt = dataclasses.replace(
        original,
        sta_start=float(stations_km[0]) * 1000.0,
        length=float(stations_km[-1] - stations_km[0]) * 1000.0,
        elements=elements,
        profile=[],
        cant=None,
    )
    return rebuilt, None


def _sampled_axis(horizontal_point, station_start: float, station_end: float) -> np.ndarray:
    """The rebuilt axis as a polyline, at most DENSE_AXIS_SAMPLE_M apart, ends included."""
    count = int(math.ceil((station_end - station_start) / DENSE_AXIS_SAMPLE_M)) + 1
    stations = np.linspace(station_start, station_end, count)
    return np.asarray(horizontal_point(stations), dtype=float)


def _distance_to_polyline_m(points: np.ndarray, polyline: np.ndarray) -> np.ndarray:
    """Distance from each point to the polyline: the nearest vertices are found with a k-d tree, and the
    distance is taken to the segments that touch them, so a point between two samples is not overstated."""
    tree = cKDTree(polyline)
    _, nearest = tree.query(points, k=min(3, len(polyline)))
    nearest = np.atleast_2d(nearest).reshape(len(points), -1)
    best = np.full(len(points), np.inf)
    last = len(polyline) - 1
    for column in range(nearest.shape[1]):
        for first in (nearest[:, column] - 1, nearest[:, column]):
            first = np.clip(first, 0, max(last - 1, 0))
            second = np.minimum(first + 1, last)
            a = polyline[first]
            b = polyline[second]
            span = b - a
            length_sq = np.einsum("ij,ij->i", span, span)
            moving = length_sq > 0.0
            t = np.zeros(len(points))
            offset = np.einsum("ij,ij->i", points[moving] - a[moving], span[moving])
            t[moving] = np.clip(offset / length_sq[moving], 0.0, 1.0)
            closest = a + span * t[:, None]
            best = np.minimum(best, np.hypot(*(points - closest).T))
    return best


def _dense_deviation_m(project: CoypuProject, rebuilt: LandXmlAlignment, crs: ProjectCRS) -> float | None:
    """Largest distance from COYPU's dense points to the rebuilt axis. The points carry (station km, lat, lon)
    in WGS 84, as `readfile.alignmentCoordinates` writes them, and are taken back to the project CRS with the
    same always-xy transform COYPU used forwards. None when the archive holds no usable dense axis.

    The station column is not compared: on the Kralupy fixture every sample of a curve carries the station of
    the curve's start (16 curves, 784 points), while the positions lie on the axis, so station-matched
    positions would be up to 657 m off. The distance to the axis is what the gate checks."""
    dense = project.landxml.get(DENSE_AXIS_KEY)
    if dense is None:
        return None
    points = np.asarray(dense, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) == 0 or not np.all(np.isfinite(points)):
        return None
    east, north = crs.from_crs("EPSG:4326", points[:, 2], points[:, 1])
    dense_en = np.column_stack([np.asarray(east, dtype=float), np.asarray(north, dtype=float)])
    alignment = to_alignment(rebuilt, crs).alignment
    polyline = _sampled_axis(alignment.horizontal.point, alignment.station_start, alignment.station_end)
    return float(np.max(_distance_to_polyline_m(dense_en, polyline)))


def _profile(original: LandXmlAlignment, baseline_km: np.ndarray, active_km: np.ndarray) -> list:
    """PVIs and vertical curves of the original file, re-stationed. Elevations, curve lengths and radii are
    kept as the file has them."""
    if not original.profile:
        return []
    stations = np.array([item.station for item in original.profile], dtype=float)
    mapped = baseline_to_active_m(stations, baseline_km, active_km)
    return [
        dataclasses.replace(item, station=float(station))
        for item, station in zip(original.profile, mapped, strict=True)
    ]


def _calculated_cant(project: CoypuProject) -> tuple[np.ndarray, np.ndarray] | None:
    """COYPU's calculated cant (km, mm) on the optimised geometry, or None when the archive stores none."""
    derived = project.landxml_derived
    stations_km = derived.get(CALCULATED_CANT_STATIONS)
    values = derived.get(CALCULATED_CANT_VALUES)
    if stations_km is None or values is None:
        return None
    stations_km = np.asarray(stations_km, dtype=float)
    values = np.asarray(values, dtype=float)
    if len(stations_km) == 0 or len(values) != len(stations_km):
        return None
    if not (np.all(np.isfinite(stations_km)) and np.all(np.isfinite(values))):
        return None
    return stations_km, values


def _cant(
    project: CoypuProject, original: LandXmlAlignment, baseline_km: np.ndarray, active_km: np.ndarray
) -> RawCant | None:
    """The cant of COYPU's calculation on the optimised geometry, which is what its simulation used: the
    `stationCantPossible` and `cantPossible` arrays, already on the active chainage, with the gauge and the
    rotation point of the original file. When the archive holds no calculation, the original cant is
    re-stationed onto the active chainage instead."""
    calculated = _calculated_cant(project)
    if calculated is not None:
        stations_km, values = calculated
        base = original.cant if original.cant is not None else RawCant()
        return dataclasses.replace(
            base,
            stations=[float(station) * 1000.0 for station in stations_km],
            values=[float(value) for value in values],
        )
    if original.cant is None or not original.cant.stations:
        return original.cant
    mapped = baseline_to_active_m(np.asarray(original.cant.stations, dtype=float), baseline_km, active_km)
    return dataclasses.replace(original.cant, stations=[float(station) for station in mapped])


def simulated_stops_warning(project: CoypuProject) -> str | None:
    """COYPU stores the scheduled stops it projected onto the active chainage with its simulation
    (`calculationCache.dataStorage.simulatedTrainStops`, km). The stops Builder maps must agree with them
    within 1 mm; the warning names the difference when they do not. None when there are no simulated stops."""
    simulated = project.data_storage.get(SIMULATED_STOPS_KEY)
    chainage = project.chainage_map
    if simulated is None or chainage is None:
        return None
    scheduled = [float(row[0]) for row in project.settings.get("trainStops") or []]
    simulated_km = [float(row[0]) for row in simulated]
    if len(scheduled) != len(simulated_km):
        return SIMULATED_STOPS_COUNT_WARNING.format(n=len(scheduled), m=len(simulated_km))
    if not scheduled:
        return None
    baseline_km, active_km = chainage
    mapped_km = baseline_to_active_m(np.array(scheduled) * 1000.0, baseline_km, active_km) / 1000.0
    difference_mm = float(np.max(np.abs(mapped_km - np.array(simulated_km)))) * 1e6
    if difference_mm > STOP_TOLERANCE_MM:
        return SIMULATED_STOPS_WARNING.format(d=difference_mm)
    return None


def optimised_alignment(
    project: CoypuProject, original: LandXmlAlignment
) -> tuple[LandXmlAlignment | None, tuple[str, ...]]:
    """The optimised alignment rebuilt from the archive's element arrays, with the original file's profile
    re-stationed onto the active chainage and COYPU's calculated cant, plus warnings. Returns (None, ()) when
    the archive is not optimised (no applicable chainage map), and (None, fallback warning) when a gate fails,
    so that the caller maps the run back to the original geometry."""
    chainage = project.chainage_map
    if chainage is None:
        return None, ()
    baseline_km, active_km = chainage
    if not np.all(np.diff(baseline_km) > 0.0):
        return None, (FALLBACK_WARNING.format(gate=GATE_CHAINAGE),)

    try:
        rebuilt, gate = elements_from_arrays(project, original)
    except CONVERSION_ERRORS:
        return None, (FALLBACK_WARNING.format(gate=GATE_CONVERSION),)
    if rebuilt is None:
        return None, (FALLBACK_WARNING.format(gate=gate),)
    epsg = project.epsg if project.epsg is not None else original.epsg
    if epsg is None:
        return None, (FALLBACK_WARNING.format(gate=GATE_CRS),)
    try:
        crs = ProjectCRS(f"EPSG:{epsg}")
    except RuntimeError:  # pyproj's CRSError
        return None, (FALLBACK_WARNING.format(gate=GATE_CRS),)
    try:
        deviation_m = _dense_deviation_m(project, rebuilt, crs)
    except CONVERSION_ERRORS:
        return None, (FALLBACK_WARNING.format(gate=GATE_CONVERSION),)
    if deviation_m is None or deviation_m > DENSE_TOLERANCE_M:
        return None, (FALLBACK_WARNING.format(gate=GATE_DENSE),)

    alignment = dataclasses.replace(
        rebuilt,
        profile=_profile(original, baseline_km, active_km),
        cant=_cant(project, original, baseline_km, active_km),
    )
    cant_label = CALCULATED_CANT_LABEL if _calculated_cant(project) is not None else RESTATIONED_CANT_LABEL
    notice = OPTIMISED_WARNING.format(n=len(rebuilt.elements), d=deviation_m * 1000.0, cant=cant_label)
    return alignment, (notice,)
