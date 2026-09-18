"""Load, validate and index `shared/catalogue/vehicles/*.json` (schema `coypu-builder.vehicle-spec/1`).

`catalogue_root()` is the one place that resolves `shared/catalogue/` from either a source checkout (this
module lives at `backend/src/coypu_builder/io/catalogue/`, five levels under the repository root) or a
PyInstaller-frozen build (ADR 0008: `coypu-builder-backend.exe` ships with `shared/` alongside it) -- it
walks up from the running module/executable looking for a `shared/catalogue` directory rather than counting
a fixed number of `.parent`s, so it tolerates either layout.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.model.vehicle import CarSpec, VehicleSpec

CATALOGUE_SCHEMA = "coypu-builder.vehicle-spec/1"

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_CAR_REQUIRED_FIELDS = (
    "name",
    "length_m",
    "width_m",
    "height_m",
    "floor_height_m",
    "bogie_pivot_distance_m",
    "bogie_wheelbase_m",
    "wheel_diameter_m",
)
_CAR_POSITIVE_FIELDS = (
    "length_m",
    "width_m",
    "height_m",
    "floor_height_m",
    "bogie_pivot_distance_m",
    "bogie_wheelbase_m",
    "wheel_diameter_m",
)


class VehicleSpecError(ValueError):
    """A catalogue file failed validation or failed to parse; the message names the file and the field."""


def catalogue_root() -> Path:
    """The `shared/catalogue/` directory, in a source checkout or next to a frozen executable."""
    anchor = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()
    for candidate in (anchor, *anchor.parents):
        shared = candidate / "shared" / "catalogue"
        if shared.is_dir():
            return shared
    raise RuntimeError(f"could not locate 'shared/catalogue' above {anchor}")


def _fail(path: Path, message: str) -> None:
    raise VehicleSpecError(f"{path}: {message}")


def _require(condition: bool, path: Path, message: str) -> None:
    if not condition:
        _fail(path, message)


def _require_number(value: object, path: Path, field: str, *, allow_zero: bool = False) -> float:
    is_number = isinstance(value, int | float) and not isinstance(value, bool)
    _require(is_number, path, f"{field} must be a number, got {value!r}")
    number = float(value)  # type: ignore[arg-type]
    if allow_zero:
        _require(number >= 0, path, f"{field} must be >= 0, got {number!r}")
    else:
        _require(number > 0, path, f"{field} must be a positive number, got {number!r}")
    return number


def _parse_car(raw: object, index: int, path: Path) -> CarSpec:
    _require(isinstance(raw, dict), path, f"cars[{index}] must be an object")
    assert isinstance(raw, dict)
    for field in _CAR_REQUIRED_FIELDS:
        _require(field in raw, path, f"cars[{index}] is missing required field '{field}'")

    dims = {
        field: _require_number(raw[field], path, f"cars[{index}].{field}") for field in _CAR_POSITIVE_FIELDS
    }

    pivot, length = dims["bogie_pivot_distance_m"], dims["length_m"]
    _require(
        pivot < length,
        path,
        f"cars[{index}].bogie_pivot_distance_m ({pivot}) must be less than length_m ({length})",
    )

    color = raw.get("color", "#808080")
    _require(
        isinstance(color, str) and bool(_HEX_COLOR_RE.match(color)),
        path,
        f"cars[{index}].color {color!r} is not a '#RRGGBB' hex colour",
    )

    mesh = raw.get("mesh")
    _require(mesh is None or isinstance(mesh, str), path, f"cars[{index}].mesh must be a string or null")

    return CarSpec(
        name=str(raw["name"]),
        length_m=dims["length_m"],
        width_m=dims["width_m"],
        height_m=dims["height_m"],
        floor_height_m=dims["floor_height_m"],
        bogie_pivot_distance_m=dims["bogie_pivot_distance_m"],
        bogie_wheelbase_m=dims["bogie_wheelbase_m"],
        wheel_diameter_m=dims["wheel_diameter_m"],
        mesh=mesh,
        color=color,
    )


def load_vehicle_spec(path: Path) -> VehicleSpec:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VehicleSpecError(f"{path}: could not read file: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VehicleSpecError(f"{path}: invalid JSON: {exc}") from exc

    _require(isinstance(raw, dict), path, "root must be a JSON object")
    assert isinstance(raw, dict)

    schema = raw.get("schema")
    _require(schema == CATALOGUE_SCHEMA, path, f"unknown schema {schema!r}; expected {CATALOGUE_SCHEMA!r}")

    for field in ("name", "mode", "gauge_mm", "cars"):
        _require(field in raw, path, f"missing required field '{field}'")

    mode_raw = raw["mode"]
    try:
        mode = Mode(mode_raw)
    except ValueError:
        known = [m.value for m in Mode]
        raise VehicleSpecError(f"{path}: unknown mode {mode_raw!r}; expected one of {known}") from None

    gauge_mm = _require_number(raw["gauge_mm"], path, "gauge_mm")

    cars_raw = raw["cars"]
    _require(isinstance(cars_raw, list) and len(cars_raw) > 0, path, "cars must be a non-empty list")
    assert isinstance(cars_raw, list)
    cars = tuple(_parse_car(c, i, path) for i, c in enumerate(cars_raw))

    coupling_gap_m = _require_number(raw.get("coupling_gap_m", 0.0), path, "coupling_gap_m", allow_zero=True)

    aliases_raw = raw.get("aliases", [])
    _require(
        isinstance(aliases_raw, list) and all(isinstance(a, str) for a in aliases_raw),
        path,
        "aliases must be a list of strings",
    )

    return VehicleSpec(
        key=path.stem,
        name=str(raw["name"]),
        mode=mode,
        gauge_mm=gauge_mm,
        cars=cars,
        coupling_gap_m=coupling_gap_m,
        aliases=tuple(aliases_raw),
        dynamics=None,
        provenance=str(raw.get("provenance", "")),
    )


class Catalogue(dict[str, VehicleSpec]):
    """`dict[str, VehicleSpec]` keyed by `VehicleSpec.key`, plus the per-file errors collected while
    loading -- a malformed file must not take down the whole catalogue load.
    """

    def __init__(self) -> None:
        super().__init__()
        self.errors: tuple[VehicleSpecError, ...] = ()


def load_catalogue(directory: Path | None = None) -> Catalogue:
    directory = Path(directory) if directory is not None else catalogue_root() / "vehicles"
    catalogue = Catalogue()
    errors: list[VehicleSpecError] = []
    for path in sorted(directory.glob("*.json")):
        try:
            spec = load_vehicle_spec(path)
        except VehicleSpecError as exc:
            errors.append(exc)
            continue
        catalogue[spec.key] = spec
    catalogue.errors = tuple(errors)
    return catalogue


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()


def resolve(catalogue: Mapping[str, VehicleSpec], key_or_alias_or_name: str) -> VehicleSpec | None:
    if key_or_alias_or_name in catalogue:
        return catalogue[key_or_alias_or_name]
    needle = _normalise(key_or_alias_or_name)
    for spec in catalogue.values():
        if _normalise(spec.key) == needle or _normalise(spec.name) == needle:
            return spec
        if any(_normalise(alias) == needle for alias in spec.aliases):
            return spec
    return None
