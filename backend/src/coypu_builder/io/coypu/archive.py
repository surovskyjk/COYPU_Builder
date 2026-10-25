"""Reader for COYPU's native `.coypu` project archive (ZIP: project.json, karto/dataset.json, assets/*.xml).

numpy arrays are stored as {"__ndarray__": true, "dtype": ..., "values": [...]}; everything else is JSON.
Format versions 1 and 2 are read (version 2 adds the line, the stop records and the stopping patterns, see
docs/data-contracts/coypu-kinematics.md, "Archive format versions"). Only three kinds of entry are read, and
each is size-checked from its ZIP directory entry before anything is read. Entries are inflated in chunks,
so a header that understates an entry's size cannot inflate past its limit.
"""

from __future__ import annotations

import dataclasses
import json
import math
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.kinematics.run import KinematicsRun, Stop, normalise_run

NDARRAY_MARKER = "__ndarray__"

SUPPORTED_FORMAT_VERSIONS = (1, 2)

PROJECT_JSON_NAME = "project.json"
KARTO_ENTRY_NAME = "karto/dataset.json"
ASSET_PREFIX = "assets/"
ASSET_SUFFIX = ".xml"

PROJECT_JSON_MAX_BYTES = 256 * 1024 * 1024
KARTO_DATASET_MAX_BYTES = 64 * 1024 * 1024
ASSETS_MAX_BYTES = 256 * 1024 * 1024

KARTO_SCHEMA_VERSION = 1

LINE_CATEGORIES = ("", "regional", "national")
PATTERN_KEYS = ("Os", "Sp", "Ex")
DEFAULT_ACTIVE_PATTERN = "Os"
DEFAULT_DWELL_S = 30.0

CHAINAGE_BASELINE_KEY = "chainageMapBaselineKm"
CHAINAGE_ACTIVE_KEY = "chainageMapActiveKm"
CHAINAGE_IDENTITY_TOL_KM = 1e-6
NON_MONOTONIC_CHAINAGE_WARNING = "chainage map is not monotonic; the run is left on COYPU's active chainage"
MISMATCHED_CHAINAGE_WARNING = (
    "chainage map arrays differ in length; the run is left on COYPU's active chainage"
)
OPTIMISED_CHAINAGE_WARNING = (
    "COYPU optimised this alignment after import: the embedded LandXML is the original geometry, so the "
    "run's stations were mapped from the optimised chainage back to it (largest shift {d:.2f} m). "
    "Positions follow the original track."
)


def decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get(NDARRAY_MARKER):
            return np.array(value.get("values", []), dtype=np.dtype(value.get("dtype", "float64")))
        return {k: decode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    return value


@dataclass(frozen=True)
class CoypuLine:
    number: str = ""
    name: str = ""
    category: str = ""  # "", "regional" or "national"; anything else is kept and warned about


@dataclass(frozen=True)
class CoypuStopRecord:
    stop_id: str
    name: str
    station_km: float
    dwell_s: float
    municipality: str
    population: int | None
    patterns: tuple[str, ...]  # subset of ("Os", "Sp", "Ex"), in that order

    def to_stop(self) -> Stop:
        return Stop(
            station_m=self.station_km * 1000.0,
            dwell_s=self.dwell_s,
            name=self.name,
            stop_id=self.stop_id,
            patterns=self.patterns,
        )


@dataclass
class CoypuProject:
    format_version: int
    metadata: dict[str, Any]
    landxml: dict[str, Any]  # parsed geometry arrays (stations in km, coordinates raw tokens)
    landxml_derived: dict[str, Any]  # cant/speed results from the geometry engine
    data_storage: dict[str, Any]  # kinematics arrays: kinematicsStationM_{i}, kinematicsTimeS_{i}, ...
    settings: dict[str, Any]  # vehicles, stops, norm tables
    stops: list[Any]
    source_segments: list[dict[str, Any]]
    raw_assets: dict[str, str] = field(default_factory=dict)
    line: CoypuLine = CoypuLine()
    stop_records: tuple[CoypuStopRecord, ...] = ()
    active_stop_pattern: str = DEFAULT_ACTIVE_PATTERN
    run_reversed: bool = False
    direction_labels: tuple[dict[str, str], ...] = ()
    karto: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    @property
    def epsg(self) -> int | None:
        value = str(self.metadata.get("epsgCode", "")) or ""
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None

    @property
    def vehicle_count(self) -> int:
        return int(self.data_storage.get("num_vehicles", len(self.settings.get("vehicles", [])) or 0))

    @property
    def chainage_map(self) -> tuple[np.ndarray, np.ndarray] | None:
        """(baseline_km, active_km) when the map must be applied to the runs, None otherwise."""
        return _applicable_chainage(_chainage_arrays(self.landxml_derived, self.landxml))

    def record_stops(self) -> tuple[Stop, ...]:
        """Every stop record in the order the archive stores them, each with its pattern membership."""
        return tuple(record.to_stop() for record in self.stop_records)

    def pattern_stops(self) -> tuple[Stop, ...]:
        """The records of the active pattern in station order: the stops a run is simulated with."""
        selected = [r.to_stop() for r in self.stop_records if self.active_stop_pattern in r.patterns]
        return tuple(sorted(selected, key=lambda stop: stop.station_m))

    def kinematics(self, index: int) -> dict[str, np.ndarray]:
        keys = {
            "station_m": f"kinematicsStationM_{index}",
            "time_s": f"kinematicsTimeS_{index}",
            "speed_ms": f"kinematicsSpeedM_{index}",
            "accel_ms2": f"kinematicsAcceleration_{index}",
            "f_trac_kn": f"kinematicsForceTractionKN_{index}",
            "f_brake_kn": f"kinematicsForceBrakingKN_{index}",
            "f_res_kn": f"kinematicsForceResistanceKN_{index}",
            "dwell_s": f"kinematicsDwellTimesS_{index}",
        }
        return {
            name: np.asarray(self.data_storage[key]) for name, key in keys.items() if key in self.data_storage
        }

    def kinematics_run(self, index: int, *, stops: Sequence[Stop] = ()) -> KinematicsRun:
        raw = self.kinematics(index)
        required = ("station_m", "time_s", "speed_ms", "accel_ms2")
        missing = [key for key in required if key not in raw]
        if missing:
            raise ValueError(f"vehicle {index}: archive kinematics is missing required arrays: {missing}")
        station_m = raw["station_m"]
        chainage = self.chainage_map
        if chainage is not None:
            station_m = _active_to_baseline_m(np.asarray(station_m, dtype=float), *chainage)
        run = normalise_run(
            station_m,
            raw["time_s"],
            raw["speed_ms"],
            raw["accel_ms2"],
            f_traction_kn=raw.get("f_trac_kn"),
            f_braking_kn=raw.get("f_brake_kn"),
            f_resistance_kn=raw.get("f_res_kn"),
            dwell_s=raw.get("dwell_s"),
            stops=stops,
            vehicle_index=index,
        )
        if self.stop_records:
            return dataclasses.replace(run, stops=self.pattern_stops())
        return run

    def kinematics_runs(self, *, stops: Sequence[Stop] = ()) -> tuple[KinematicsRun, ...]:
        return tuple(self.kinematics_run(i, stops=stops) for i in range(self.vehicle_count))


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _population(value: Any) -> int | None:
    number = _finite(value)
    return None if number is None else int(round(number))


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name} is not allowed")


def _stop_records(raw_records: Any) -> tuple[tuple[CoypuStopRecord, ...], int]:
    records = []
    skipped = 0
    for raw in raw_records or []:
        station_km = _finite(raw.get("stationingKm")) if isinstance(raw, dict) else None
        if station_km is None:
            skipped += 1
            continue
        dwell_s = _finite(raw.get("dwellTimeS"))
        patterns = tuple(key for key in PATTERN_KEYS if bool(raw.get(f"stopIn{key}", key == "Os")))
        records.append(
            CoypuStopRecord(
                stop_id=_text(raw.get("stopId")),
                name=_text(raw.get("stopName")),
                station_km=station_km,
                dwell_s=DEFAULT_DWELL_S if dwell_s is None else dwell_s,
                municipality=_text(raw.get("municipalityName")),
                population=_population(raw.get("population")),
                patterns=patterns,
            )
        )
    return tuple(records), skipped


def _direction_labels(vehicle: Any) -> dict[str, str]:
    raw = _mapping(vehicle).get("directionLabels")
    if not isinstance(raw, dict):
        return {}
    return {key: _text(raw.get(key)) for key in ("forward", "reverse")}


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"non-finite number {text} is not allowed")
    return value


def _parse_karto(text: bytes) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(text.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError) as exc:
        return None, f"{KARTO_ENTRY_NAME} ignored: {exc}"
    if not isinstance(data, dict):
        return None, f"{KARTO_ENTRY_NAME} ignored: it is not a JSON object"
    version = data.get("schemaVersion")
    if type(version) is not int or version != KARTO_SCHEMA_VERSION:
        return None, f"{KARTO_ENTRY_NAME} ignored: schemaVersion {version!r} is not {KARTO_SCHEMA_VERSION}"
    return data, None


def _chainage_arrays(
    landxml_derived: dict[str, Any], landxml: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray] | None:
    """(baseline_km, active_km) of the optimisation map. COYPU keeps it with the calculated results
    (`calculationCache.landXmlDerived`); the parsed LandXML is checked as well."""
    for source in (landxml_derived, landxml):
        baseline = source.get(CHAINAGE_BASELINE_KEY)
        active = source.get(CHAINAGE_ACTIVE_KEY)
        if baseline is not None and active is not None:
            return np.asarray(baseline, dtype=float), np.asarray(active, dtype=float)
    return None


def _is_identity(baseline_km: np.ndarray, active_km: np.ndarray) -> bool:
    return baseline_km.shape == active_km.shape and bool(
        np.all(np.abs(active_km - baseline_km) <= CHAINAGE_IDENTITY_TOL_KM)
    )


def _applicable_chainage(
    arrays: tuple[np.ndarray, np.ndarray] | None,
) -> tuple[np.ndarray, np.ndarray] | None:
    if arrays is None:
        return None
    baseline_km, active_km = arrays
    if baseline_km.size < 2 or baseline_km.shape != active_km.shape or _is_identity(baseline_km, active_km):
        return None
    if not np.all(np.diff(active_km) > 0.0):
        return None
    return arrays


def _chainage_warnings(arrays: tuple[np.ndarray, np.ndarray] | None) -> list[str]:
    if arrays is None:
        return []
    baseline_km, active_km = arrays
    if baseline_km.size < 2 or _is_identity(baseline_km, active_km):
        return []
    if baseline_km.shape != active_km.shape:
        return [MISMATCHED_CHAINAGE_WARNING]
    if not np.all(np.diff(active_km) > 0.0):
        return [NON_MONOTONIC_CHAINAGE_WARNING]
    shift_m = float(np.max(np.abs(active_km - baseline_km))) * 1000.0
    return [OPTIMISED_CHAINAGE_WARNING.format(d=shift_m)]


def _refuse_if_over(name: str, size: int, limit: int) -> None:
    if size > limit:
        raise ValueError(f"{name} is {size} bytes, over the {limit}-byte limit")


def _refuse_if_assets_over(asset_infos: Sequence[zipfile.ZipInfo]) -> None:
    total = 0
    for info in asset_infos:
        total += info.file_size
        if total > ASSETS_MAX_BYTES:
            raise ValueError(
                f"{info.filename} is {info.file_size} bytes; the assets/*.xml entries together reach "
                f"{total} bytes, over the {ASSETS_MAX_BYTES}-byte limit"
            )


READ_CHUNK_BYTES = 1 << 20


def _read_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    """Read one entry in chunks, so a header that understates its size cannot inflate past `limit`."""
    chunks: list[bytes] = []
    total = 0
    with archive.open(info) as stream:
        while chunk := stream.read(READ_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"{info.filename} inflates past the {limit}-byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _active_to_baseline_m(
    station_m: np.ndarray, baseline_km: np.ndarray, active_km: np.ndarray
) -> np.ndarray:
    """Stations on COYPU's active chainage mapped back to the baseline; beyond the map's ends the shift of
    the nearest end is kept (unit slope), so stations never collapse onto one value."""
    station_km = station_m / 1000.0
    mapped_km = np.interp(station_km, active_km, baseline_km)
    below = station_km < active_km[0]
    above = station_km > active_km[-1]
    mapped_km[below] = baseline_km[0] + (station_km[below] - active_km[0])
    mapped_km[above] = baseline_km[-1] + (station_km[above] - active_km[-1])
    return mapped_km * 1000.0


def _format_version(payload: dict[str, Any]) -> int:
    version = payload.get("formatVersion")
    if type(version) is not int or version < 1:
        raise ValueError("not a COYPU project file")
    return version


def read_coypu(path: str | Path) -> CoypuProject:
    try:
        return _read_coypu(path)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
        raise ValueError("not a COYPU project file") from exc


def _read_coypu(path: str | Path) -> CoypuProject:
    with zipfile.ZipFile(Path(path), "r") as archive:
        entries = archive.infolist()
        by_name = {info.filename: info for info in entries}
        if PROJECT_JSON_NAME not in by_name:
            raise ValueError("not a COYPU project file")
        _refuse_if_over(PROJECT_JSON_NAME, by_name[PROJECT_JSON_NAME].file_size, PROJECT_JSON_MAX_BYTES)
        karto_info = by_name.get(KARTO_ENTRY_NAME)
        if karto_info is not None:
            _refuse_if_over(KARTO_ENTRY_NAME, karto_info.file_size, KARTO_DATASET_MAX_BYTES)
        asset_infos = [
            info
            for info in entries
            if info.filename.startswith(ASSET_PREFIX) and info.filename.endswith(ASSET_SUFFIX)
        ]
        _refuse_if_assets_over(asset_infos)

        project_bytes = _read_entry(archive, by_name[PROJECT_JSON_NAME], PROJECT_JSON_MAX_BYTES)
        karto_bytes = (
            _read_entry(archive, karto_info, KARTO_DATASET_MAX_BYTES) if karto_info is not None else None
        )
        raw_assets = {}
        budget = ASSETS_MAX_BYTES
        for info in asset_infos:
            data = _read_entry(archive, info, budget)
            budget -= len(data)
            raw_assets[info.filename] = data.decode("utf-8", errors="replace")

    try:
        payload = json.loads(project_bytes.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("not a COYPU project file") from exc
    if not isinstance(payload, dict):
        raise ValueError("not a COYPU project file")
    format_version = _format_version(payload)

    warnings: list[str] = []
    if format_version > max(SUPPORTED_FORMAT_VERSIONS):
        warnings.append(
            f"written by a newer COYPU (format {format_version}); only the parts this version knows are read"
        )

    metadata = _mapping(payload.get("projectMetadata"))
    line = CoypuLine(
        number=_text(metadata.get("lineNumber")),
        name=_text(metadata.get("lineName")),
        category=_text(metadata.get("lineCategory")),
    )
    if line.category not in LINE_CATEGORIES:
        warnings.append(f"unknown line category '{line.category}' kept as written")

    stops_data = _mapping(payload.get("stopsData"))
    active_pattern = stops_data.get("activeStopPattern", DEFAULT_ACTIVE_PATTERN)
    if active_pattern not in PATTERN_KEYS:
        warnings.append(f"unknown activeStopPattern '{active_pattern}'; using {DEFAULT_ACTIVE_PATTERN}")
        active_pattern = DEFAULT_ACTIVE_PATTERN
    stop_records, skipped = _stop_records(decode_value(stops_data.get("stopRecords", [])))
    if skipped:
        warnings.append(f"{skipped} stop record(s) without a usable stationingKm skipped")

    alignments = _mapping(payload.get("alignmentsData"))
    cache = _mapping(payload.get("calculationCache"))
    settings = _mapping(decode_value(_mapping(payload.get("vehicleConfiguration")).get("settingsData", {})))
    direction_labels = tuple(_direction_labels(vehicle) for vehicle in settings.get("vehicles") or [])

    landxml = decode_value(alignments.get("landXml", {}))
    landxml_derived = decode_value(cache.get("landXmlDerived", {}))
    warnings.extend(_chainage_warnings(_chainage_arrays(landxml_derived, landxml)))

    karto = None
    if karto_bytes is not None:
        karto, karto_warning = _parse_karto(karto_bytes)
        if karto_warning is not None:
            warnings.append(karto_warning)

    return CoypuProject(
        format_version=format_version,
        metadata=metadata,
        landxml=landxml,
        landxml_derived=landxml_derived,
        data_storage=decode_value(cache.get("dataStorage", {})),
        settings=settings,
        stops=decode_value(stops_data.get("trainStops", [])),
        source_segments=decode_value(alignments.get("sourceSegments", [])),
        raw_assets=raw_assets,
        line=line,
        stop_records=stop_records,
        active_stop_pattern=active_pattern,
        run_reversed=bool(settings.get("runReversed", False)),
        direction_labels=direction_labels,
        karto=karto,
        warnings=tuple(warnings),
    )
