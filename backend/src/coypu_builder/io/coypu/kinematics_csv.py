"""Readers for the two COYPU kinematics CSV dialects and the stops CSV.

See docs/data-contracts/coypu-kinematics.md for the format specification.

Dialect is detected from the header row's content, never from the filename: the strict-SI batch header, or
the GUI vehicle report header localised into English, Czech or German. Both feed the same domain
normalisation (`coypu_builder.domain.kinematics.normalise_run`); only the unit conversion (km -> m,
km/h -> m/s) happens here, per the "COYPU's km appear only inside io/coypu" invariant.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from coypu_builder.domain.kinematics.run import KinematicsRun, Stop, normalise_run


@dataclass(frozen=True, slots=True)
class _CsvDialect:
    name: str
    header: tuple[str, ...]  # station, time, speed, accel, f_trac, f_brake, f_res
    station_unit: str  # "m" | "km"
    speed_unit: str  # "ms" | "kmh"


_BATCH = _CsvDialect(
    "batch",
    ("stationM", "timeS", "speedMs", "accelMs2", "forceTracKN", "forceBrakeKN", "forceResKN"),
    station_unit="m",
    speed_unit="ms",
)
_GUI_EN = _CsvDialect(
    "gui_en",
    (
        "stationing [km]",
        "Time [s]",
        "Speed [km/h]",
        "Accel [m/s2]",
        "Tractive Force [kN]",
        "Braking Force [kN]",
        "Resistance [kN]",
    ),
    station_unit="km",
    speed_unit="kmh",
)
_GUI_CZ = _CsvDialect(
    "gui_cz",
    (
        "staničení [km]",
        "Čas [s]",
        "Rychlost [km/h]",
        "Accel [m/s2]",
        "Trakční síla [kN]",
        "Brzdná síla [kN]",
        "Jízdní odpor [kN]",
    ),
    station_unit="km",
    speed_unit="kmh",
)
_GUI_DE = _CsvDialect(
    "gui_de",
    (
        "Streckenkilometer [km]",
        "Zeit [s]",
        "Geschwindigkeit [km/h]",
        "Accel [m/s2]",
        "Zugkraft [kN]",
        "Bremskraft [kN]",
        "Widerstand [kN]",
    ),
    station_unit="km",
    speed_unit="kmh",
)
_DIALECTS = (_BATCH, _GUI_EN, _GUI_CZ, _GUI_DE)


def _detect(header: Sequence[str]) -> _CsvDialect:
    normalised = tuple(h.strip() for h in header)
    for dialect in _DIALECTS:
        if normalised == dialect.header:
            return dialect
    known = ", ".join(f"{d.name} {d.header}" for d in _DIALECTS)
    raise ValueError(f"unrecognised kinematics CSV header {normalised!r}; known dialects: {known}")


def _column(raw: Sequence[str]) -> np.ndarray | None:
    if all(v.strip() == "" for v in raw):
        return None
    return np.array([float(v) for v in raw], dtype=np.float64)


def read_kinematics_csv(path: str | Path, *, stops: Sequence[Stop] = ()) -> KinematicsRun:
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"{path}: kinematics CSV is empty") from None
        dialect = _detect(header)
        rows = list(reader)

    if not rows:
        raise ValueError(f"{path}: kinematics CSV has a header but no data rows")

    station_raw, time_raw, speed_raw, accel_raw, trac_raw, brake_raw, res_raw = zip(*rows, strict=True)

    if all(v.strip() == "" for v in time_raw):
        raise ValueError(f"{path}: kinematics CSV has no time data; cannot normalise without a time axis")

    station_m = _column(station_raw)
    speed = _column(speed_raw)
    if dialect.station_unit == "km":
        station_m *= 1000.0
    if dialect.speed_unit == "kmh":
        speed /= 3.6

    return normalise_run(
        station_m,
        _column(time_raw),
        speed,
        _column(accel_raw),
        f_traction_kn=_column(trac_raw),
        f_braking_kn=_column(brake_raw),
        f_resistance_kn=_column(res_raw),
        stops=stops,
        name=path.stem,
    )


def read_stops_csv(path: str | Path) -> tuple[Stop, ...]:
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        stops = []
        for row in reader:
            station_km = float(row["Station"])
            dwell_raw = (row.get("Dwell Time") or "").strip()
            dwell_s = float(dwell_raw) if dwell_raw else 30.0
            name = (row.get("Name") or "").strip()
            stops.append(Stop(station_m=station_km * 1000.0, dwell_s=dwell_s, name=name))
    return tuple(stops)
