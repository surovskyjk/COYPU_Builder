"""Vehicle catalogue domain types.

`VehicleSpec`/`CarSpec` are the geometry half, loaded from `shared/catalogue/vehicles/*.json`
(`io/catalogue/vehicles.py`). `VehicleDynamics`/`TractionBand` are the optional COYPU half — what the run
was simulated with — imported from a COYPU vehicle CSV or a `.coypu` archive (`io/coypu/vehicles.py`) and
attached with `merge_dynamics`. Geometry alone is enough for Phase 1; see
docs/data-contracts/vehicle-catalogue.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from coypu_builder.domain.model.modes import Mode


@dataclass(frozen=True, slots=True)
class CarSpec:
    name: str
    length_m: float
    width_m: float
    height_m: float
    floor_height_m: float
    bogie_pivot_distance_m: float
    bogie_wheelbase_m: float
    wheel_diameter_m: float
    mesh: str | None = None  # catalogue-relative path to a glTF, Phase 2; None = procedural
    color: str = "#808080"  # hex, used by the Phase 1 procedural mesh


@dataclass(frozen=True, slots=True)
class TractionBand:
    v_bottom_ms: float
    v_top_ms: float
    b0: float
    b1: float
    b2: float  # F(v) = b0 + b1*v + b2*v^2, kN, v in m/s


@dataclass(frozen=True, slots=True)
class VehicleDynamics:
    """The COYPU half: what the run was simulated with. Optional -- geometry alone is enough for Phase 1.

    `max_tractive_force_kn`, `davis_a/b/c` are `None` rather than 0.0 when the source (a `.coypu` archive,
    or a CSV missing its `Meta`/`Res` sections) never supplied them -- a fabricated Davis C is
    indistinguishable from a measured one downstream, so absence is preserved rather than papered over.
    This is a deliberate widening of the field types from the task's literal signature (which declared
    these as plain `float`); see the T-113 closing report.
    """

    mass_t: float
    rotating_mass_factor: float
    max_speed_ms: float
    brake_decel_ms2: float
    max_tractive_force_kn: float | None = None
    davis_a: float | None = None
    davis_b: float | None = None
    davis_c: float | None = None
    traction_bands: tuple[TractionBand, ...] = ()


@dataclass(frozen=True, slots=True)
class VehicleSpec:
    key: str  # the catalogue file stem, e.g. "dmu_br650_cd840"
    name: str
    mode: Mode
    gauge_mm: float
    cars: tuple[CarSpec, ...]
    coupling_gap_m: float = 0.0
    aliases: tuple[str, ...] = ()
    dynamics: VehicleDynamics | None = None
    provenance: str = ""

    @property
    def length_m(self) -> float:
        if not self.cars:
            return 0.0
        return sum(car.length_m for car in self.cars) + self.coupling_gap_m * (len(self.cars) - 1)
