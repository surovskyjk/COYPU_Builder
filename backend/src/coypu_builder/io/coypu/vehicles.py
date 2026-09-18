"""COYPU vehicle dynamics: the Meta/Param/Res/Trac vehicle CSV and the `.coypu` archive's per-vehicle
settings block (both documented in docs/data-contracts/coypu-kinematics.md). Both key on `vehicleName`;
matching that name against the geometry catalogue (`io/catalogue/vehicles.py`) is `resolve()`'s job -- a
non-match there is a normal outcome, not an error, so this module never raises for it.

Both sources express `trainMaxSpeed`/`maxSpeedKmh` in km/h and the traction bands as a polynomial calibrated
for `v` in km/h (`F(v_kmh) = b0 + b1*v_kmh + b2*v_kmh^2`, kN); `VehicleDynamics` is SI-normalised on import so
nothing downstream has to remember which source a number came from. Substituting `v_kmh = 3.6*v_ms` keeps
`F` identical while re-expressing the polynomial in `v_ms`, which is what `TractionBand` documents:
`b0' = b0`, `b1' = 3.6*b1`, `b2' = 3.6^2*b2`. The Davis resistance coefficients (`davis_a/b/c`) carry no such
documented formula in the data contract, so they are copied through unconverted -- see the T-113 closing
report.
"""

from __future__ import annotations

import csv
import dataclasses
from pathlib import Path

from coypu_builder.domain.model.vehicle import TractionBand, VehicleDynamics, VehicleSpec
from coypu_builder.io.coypu.archive import CoypuProject

KMH_TO_MS = 1.0 / 3.6


def _traction_band_from_kmh(
    v_bottom_kmh: float, v_top_kmh: float, b0: float, b1: float, b2: float
) -> TractionBand:
    return TractionBand(
        v_bottom_ms=v_bottom_kmh * KMH_TO_MS,
        v_top_ms=v_top_kmh * KMH_TO_MS,
        b0=b0,
        b1=b1 * 3.6,
        b2=b2 * 3.6 * 3.6,
    )


def read_vehicle_csv(path: str | Path) -> tuple[str, VehicleDynamics]:
    """Parse the extended vehicle CSV (`Section,Col1,...` rows tagged `Meta`/`Param`/`Res`/`Trac`).

    `Meta` (when present) wins over the values duplicated in `Param`, matching COYPU's own reference reader.
    A CSV missing its `Res` and/or `Trac` rows leaves the corresponding `VehicleDynamics` fields `None`/`()`
    rather than fabricating zeros.
    """
    path = Path(path)
    meta: dict[str, str] = {}
    param_row: list[str] | None = None
    res_row: list[str] | None = None
    trac_rows: list[list[str]] = []

    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if not row or not row[0].strip():
                continue
            section = row[0].strip().lower()
            if section == "section":
                continue
            if section == "meta" and len(row) >= 3:
                meta[row[1].strip()] = row[2].strip()
            elif section == "param" and len(row) >= 4 and param_row is None:
                param_row = row
            elif section == "res" and len(row) >= 5 and res_row is None:
                res_row = row
            elif section == "trac" and len(row) >= 7:
                trac_rows.append(row)

    if "vehicleName" in meta:
        name = meta["vehicleName"]
    elif param_row is not None:
        name = param_row[1].strip()
    else:
        raise ValueError(f"{path}: vehicle CSV has neither a Meta 'vehicleName' row nor a Param row")

    if "massTonnes" in meta:
        mass_t = float(meta["massTonnes"])
    elif param_row is not None:
        mass_t = float(param_row[3])
    else:
        raise ValueError(f"{path}: vehicle CSV supplies no mass (neither Meta 'massTonnes' nor Param)")

    if "rotMassFactor" in meta:
        rotating_mass_factor = float(meta["rotMassFactor"])
    elif param_row is not None:
        rotating_mass_factor = float(param_row[2])
    else:
        raise ValueError(f"{path}: vehicle CSV supplies no rotating-mass factor")

    if "maxSpeedKmh" not in meta:
        raise ValueError(f"{path}: vehicle CSV has no Meta 'maxSpeedKmh' row")
    max_speed_ms = float(meta["maxSpeedKmh"]) * KMH_TO_MS

    if "brakeDecelMs2" not in meta:
        raise ValueError(f"{path}: vehicle CSV has no Meta 'brakeDecelMs2' row")
    brake_decel_ms2 = float(meta["brakeDecelMs2"])

    max_tractive_force_kn = float(meta["maxTractiveForceKN"]) if "maxTractiveForceKN" in meta else None

    if res_row is not None:
        davis_a, davis_b, davis_c = float(res_row[2]), float(res_row[3]), float(res_row[4])
    else:
        davis_a = davis_b = davis_c = None

    traction_bands = tuple(
        _traction_band_from_kmh(float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]))
        for r in trac_rows
    )

    dynamics = VehicleDynamics(
        mass_t=mass_t,
        rotating_mass_factor=rotating_mass_factor,
        max_speed_ms=max_speed_ms,
        brake_decel_ms2=brake_decel_ms2,
        max_tractive_force_kn=max_tractive_force_kn,
        davis_a=davis_a,
        davis_b=davis_b,
        davis_c=davis_c,
        traction_bands=traction_bands,
    )
    return name, dynamics


def dynamics_from_coypu(project: CoypuProject, index: int) -> tuple[str, VehicleDynamics] | None:
    """Read vehicle `index`'s dynamics out of a `.coypu` archive's `vehicleConfiguration.settingsData`.

    `trainParam` is the only piece the Preconditions promise; `trainRes`/`trainTrac`/`trainMaxSpeed`/
    `trainBrakeDecel` live alongside it per vehicle and are used when present. The archive never states a
    `maxTractiveForceKN` (that is a CSV `Meta`-only field), so that field is always `None` here.
    Returns `None` when `index` has no vehicle at all, not when a match to the geometry catalogue fails --
    that is `resolve()`'s concern, and a normal outcome.
    """
    vehicles = project.settings.get("vehicles", [])
    if not (0 <= index < len(vehicles)):
        return None
    entry = vehicles[index]

    train_param = entry.get("trainParam") or []
    if not train_param or not train_param[0]:
        return None
    name, rot_mass, mass_t = train_param[0][0], train_param[0][1], train_param[0][2]

    if "trainMaxSpeed" not in entry or "trainBrakeDecel" not in entry:
        raise ValueError(
            f"vehicle {index} ({name}): archive settings has trainParam but no trainMaxSpeed/trainBrakeDecel"
        )
    max_speed_ms = float(entry["trainMaxSpeed"]) * KMH_TO_MS
    brake_decel_ms2 = float(entry["trainBrakeDecel"])

    res_rows = entry.get("trainRes") or []
    if res_rows and res_rows[0]:
        davis_a, davis_b, davis_c = float(res_rows[0][1]), float(res_rows[0][2]), float(res_rows[0][3])
    else:
        davis_a = davis_b = davis_c = None

    trac_rows = entry.get("trainTrac") or []
    traction_bands = tuple(
        _traction_band_from_kmh(float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        for r in trac_rows
        if r
    )

    dynamics = VehicleDynamics(
        mass_t=float(mass_t),
        rotating_mass_factor=float(rot_mass),
        max_speed_ms=max_speed_ms,
        brake_decel_ms2=brake_decel_ms2,
        max_tractive_force_kn=None,
        davis_a=davis_a,
        davis_b=davis_b,
        davis_c=davis_c,
        traction_bands=traction_bands,
    )
    return str(name), dynamics


def merge_dynamics(spec: VehicleSpec, dynamics: VehicleDynamics) -> VehicleSpec:
    return dataclasses.replace(spec, dynamics=dynamics)
