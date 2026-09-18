"""Reader for COYPU's native `.coypu` project archive (ZIP: project.json + assets/*.xml).

numpy arrays are stored as {"__ndarray__": true, "dtype": ..., "values": [...]}; everything else is JSON.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.kinematics.run import KinematicsRun, Stop, normalise_run

NDARRAY_MARKER = "__ndarray__"


def decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get(NDARRAY_MARKER):
            return np.array(value.get("values", []), dtype=np.dtype(value.get("dtype", "float64")))
        return {k: decode_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode_value(v) for v in value]
    return value


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

    @property
    def epsg(self) -> int | None:
        value = str(self.metadata.get("epsgCode", "")) or ""
        digits = "".join(ch for ch in value if ch.isdigit())
        return int(digits) if digits else None

    @property
    def vehicle_count(self) -> int:
        return int(self.data_storage.get("num_vehicles", len(self.settings.get("vehicles", [])) or 0))

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
        return normalise_run(
            raw["station_m"],
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

    def kinematics_runs(self, *, stops: Sequence[Stop] = ()) -> tuple[KinematicsRun, ...]:
        return tuple(self.kinematics_run(i, stops=stops) for i in range(self.vehicle_count))


def read_coypu(path: str | Path) -> CoypuProject:
    with zipfile.ZipFile(Path(path), "r") as archive:
        payload = json.loads(archive.read("project.json").decode("utf-8"))
        raw_assets = {
            name: archive.read(name).decode("utf-8", errors="replace")
            for name in archive.namelist()
            if name.startswith("assets/")
        }
    alignments = payload.get("alignmentsData", {})
    cache = payload.get("calculationCache", {})
    return CoypuProject(
        format_version=int(payload.get("formatVersion", 0)),
        metadata=payload.get("projectMetadata", {}),
        landxml=decode_value(alignments.get("landXml", {})),
        landxml_derived=decode_value(cache.get("landXmlDerived", {})),
        data_storage=decode_value(cache.get("dataStorage", {})),
        settings=decode_value(payload.get("vehicleConfiguration", {}).get("settingsData", {})),
        stops=decode_value(payload.get("stopsData", {}).get("trainStops", [])),
        source_segments=decode_value(alignments.get("sourceSegments", [])),
        raw_assets=raw_assets,
    )
