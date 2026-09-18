"""`Trainset`: ADR 0006's entity for an assembled consist -- built from a catalogue `VehicleSpec`, flattened
into resolved `CarSpec`s front to back, so T-112 (posing) and T-122 (rendering) never chase a spec reference.
"""

from __future__ import annotations

from dataclasses import dataclass

from coypu_builder.domain.model.ids import EntityId, new_id
from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.model.vehicle import CarSpec, VehicleSpec


@dataclass(frozen=True, slots=True)
class Trainset:
    id: EntityId
    spec_key: str  # VehicleSpec.key
    cars: tuple[CarSpec, ...]  # the resolved, flattened consist, in order front to back
    coupling_gap_m: float
    mode: Mode
    gauge_mm: float
    name: str = ""

    @property
    def length_m(self) -> float:
        if not self.cars:
            return 0.0
        return sum(car.length_m for car in self.cars) + self.coupling_gap_m * (len(self.cars) - 1)

    @classmethod
    def from_spec(cls, spec: VehicleSpec, *, units: int = 1, name: str = "") -> Trainset:
        """`units` repeats the whole spec -- a doubled two-car set gives four cars with a coupling gap
        between units as well as within them, since the same `coupling_gap_m` is reused throughout.
        """
        return cls(
            id=new_id(),
            spec_key=spec.key,
            cars=spec.cars * units,
            coupling_gap_m=spec.coupling_gap_m,
            mode=spec.mode,
            gauge_mm=spec.gauge_mm,
            name=name or spec.name,
        )
