"""LandXML dialects seen in the COYPU ecosystem and the coordinate-token convention.

Point text in LandXML is "northing easting" per the specification. The Czech Křovák practice (third-party
rail CAD exports, COYPU Feeder with sign stripping, COYPU) writes the positive engineering values
X (southing) and Y (westing): with a Křovák project CRS the tokens (t1, t2) map to E = −t2, N = −t1 when
positive and to E = t2, N = t1 when the file carries true negative EPSG:5514 values. For every other CRS
the default is the specification order (N, E), overridable per file.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.crs import ProjectCRS


@dataclass(frozen=True, slots=True)
class Dialect:
    name: str
    gauge_unit: str = "auto"  # "m" | "mm" | "auto" (value < 10 → metres)
    cant_unit: str = "mm"
    coordinate_order: str = "auto"  # "auto" | "NE" | "EN"


RAIL_EXPORT = Dialect("rail_export")
COYPU_FEEDER = Dialect("coypu_feeder", gauge_unit="m")
COYPU = Dialect("coypu", gauge_unit="mm")
GENERIC = Dialect("generic")


def detect_dialect(application_name: str | None, application_version: str | None = None) -> Dialect:
    name = (application_name or "").strip().lower()
    if "feeder" in name:
        return COYPU_FEEDER
    if name == "coypu":
        return COYPU
    if name == "rail":
        return RAIL_EXPORT
    return GENERIC


def resolve_coordinates(tokens: np.ndarray, crs: ProjectCRS, coordinate_order: str = "auto") -> np.ndarray:
    """(n, 2) raw token pairs (t1, t2) → (n, 2) (E, N) in the project CRS."""
    t = np.asarray(tokens, dtype=np.float64).reshape(-1, 2)
    if crs.is_krovak:
        positive = np.all(t > 0)
        if positive:
            return np.column_stack([-t[:, 1], -t[:, 0]])
        return np.column_stack([t[:, 1], t[:, 0]])
    order = coordinate_order
    if order == "auto":
        order = "NE"
    if order == "NE":
        return np.column_stack([t[:, 1], t[:, 0]])
    return t.copy()


def gauge_to_mm(value: float, unit: str) -> float:
    if unit == "m" or (unit == "auto" and value < 10.0):
        return value * 1000.0
    return value
