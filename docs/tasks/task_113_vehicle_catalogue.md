# T-113 — Vehicle catalogue: schema, loader, `Trainset`, COYPU vehicle import

**Milestone:** P1.M1 · **Depends on:** nothing · **Blocks:** T-112, T-114, T-122

## Context

`shared/catalogue/vehicles/` holds two hand-written JSON files declaring `"schema":
"coypu-builder.vehicle-spec/1"`, but nothing reads them, nothing validates them, and no document describes
the schema. Meanwhile COYPU carries the *dynamics* half of the same vehicles — mass, rotating-mass factor,
Davis resistance coefficients, tractive-force bands, brake deceleration — in its vehicle CSV sections and in
`.coypu` archives, keyed by the same `vehicleName`.

Phase 1 renders trains and plays back runs, so it needs the geometry half loaded and validated. It should
also be able to absorb the dynamics half, because that is what links a catalogue entry to the run that was
simulated with it — and doing the merge now, while there are two catalogue files, is trivial.

`Trainset` is ADR 0006's entity for an assembled consist. It belongs here rather than in T-110 because it is
built from catalogue specs, and splitting it out lets T-110, T-111 and T-113 run in parallel.

## Preconditions

- `shared/catalogue/vehicles/dmu_br650_cd840.json` and `generic_bemu.json` exist with the field set:
  `schema`, `name`, `aliases`, `mode`, `gauge_mm`, `cars[]`
  (`name`, `length_m`, `width_m`, `height_m`, `floor_height_m`, `bogie_pivot_distance_m`,
  `bogie_wheelbase_m`, `wheel_diameter_m`, `mesh`, `color`), `coupling_gap_m`, `provenance`.
- `io/coypu/archive.py` exposes `CoypuProject.settings["vehicles"]`, where
  `vehicles[i].trainParam = [[name, rotMass, mass_t, length_m]]`.
- `docs/data-contracts/coypu-kinematics.md` documents the COYPU vehicle CSV sections: `Meta`, `Param`,
  `Res` (Davis A, B, C), `Trac` (bands `V_bottom, V_top, b0, b1, b2`).
- `shared/catalogue/cross_sections/` is an empty placeholder — leave it alone.

Read before starting: `docs/adr/0006-multimodal-lrs-model.md`, the vehicle sections of
`docs/data-contracts/coypu-kinematics.md`, both existing catalogue JSON files.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/model/vehicle.py` | new — `VehicleSpec`, `CarSpec`, `VehicleDynamics`, `TractionBand` |
| `backend/src/coypu_builder/domain/model/trainset.py` | new — `Trainset` |
| `backend/src/coypu_builder/domain/model/__init__.py` | re-export (coordinate with T-110 if it runs concurrently) |
| `backend/src/coypu_builder/io/catalogue/__init__.py` | new |
| `backend/src/coypu_builder/io/catalogue/vehicles.py` | new — load, validate, index the catalogue |
| `backend/src/coypu_builder/io/coypu/vehicles.py` | new — COYPU vehicle CSV and `.coypu` dynamics import |
| `shared/catalogue/vehicles/tram_generic.json` | new — a 1000 mm-gauge tram, for T-110's fixture and T-112 |
| `backend/tests/test_vehicle_catalogue.py` | new |
| `backend/tests/test_coypu_vehicle_import.py` | new |
| `docs/data-contracts/vehicle-catalogue.md` | new — the schema reference |

## Contract

### Domain types

```python
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
    mesh: str | None = None          # catalogue-relative path to a glTF, Phase 2; None = procedural
    color: str = "#808080"           # hex, used by the Phase 1 procedural mesh

@dataclass(frozen=True, slots=True)
class TractionBand:
    v_bottom_ms: float
    v_top_ms: float
    b0: float
    b1: float
    b2: float                        # F(v) = b0 + b1·v + b2·v², kN, v in m/s

@dataclass(frozen=True, slots=True)
class VehicleDynamics:
    """The COYPU half: what the run was simulated with. Optional — geometry alone is enough for Phase 1."""
    mass_t: float
    rotating_mass_factor: float
    max_speed_ms: float
    brake_decel_ms2: float
    max_tractive_force_kn: float
    davis_a: float
    davis_b: float
    davis_c: float
    traction_bands: tuple[TractionBand, ...] = ()

@dataclass(frozen=True, slots=True)
class VehicleSpec:
    key: str                         # the catalogue file stem, e.g. "dmu_br650_cd840"
    name: str
    mode: Mode
    gauge_mm: float
    cars: tuple[CarSpec, ...]
    coupling_gap_m: float = 0.0
    aliases: tuple[str, ...] = ()
    dynamics: VehicleDynamics | None = None
    provenance: str = ""

    @property
    def length_m(self) -> float: ...     # sum of car lengths plus coupling gaps between them
```

**Unit discipline.** The catalogue JSON is in the units its field names state (`gauge_mm`, `length_m`).
COYPU's sources are in km/h and tonnes. `VehicleDynamics` is SI-normalised on import — `max_speed_ms`, not
`maxSpeedKmh` — so that nothing downstream has to remember which source a number came from.

### Trainset

```python
@dataclass(frozen=True, slots=True)
class Trainset:
    id: EntityId
    spec_key: str                   # VehicleSpec.key
    cars: tuple[CarSpec, ...]       # the resolved, flattened consist, in order front to back
    coupling_gap_m: float
    mode: Mode
    gauge_mm: float
    name: str = ""

    @property
    def length_m(self) -> float: ...

    @classmethod
    def from_spec(cls, spec: VehicleSpec, *, units: int = 1, name: str = "") -> Trainset: ...
```

`units` repeats the whole spec — a doubled two-car set gives four cars with a coupling gap between units as
well as within them. `Trainset` is what T-112 poses and what T-122 renders; it is deliberately flat, carrying
resolved car specs rather than a reference the renderer would have to chase.

### Catalogue loader

```python
CATALOGUE_SCHEMA = "coypu-builder.vehicle-spec/1"

def load_vehicle_spec(path: Path) -> VehicleSpec: ...
def load_catalogue(directory: Path | None = None) -> dict[str, VehicleSpec]: ...   # keyed by VehicleSpec.key
def resolve(catalogue: Mapping[str, VehicleSpec], key_or_alias_or_name: str) -> VehicleSpec | None: ...
```

`load_catalogue` defaults to `shared/catalogue/vehicles/` found relative to the installed package root; the
path resolution must work both from a source checkout and from a PyInstaller-frozen build (ADR 0008 ships a
frozen backend), so resolve it once in a single helper rather than scattering `Path(__file__).parents[n]`.

Validation is strict and the errors name the file and the field: unknown `schema` value, missing required
field, non-positive dimension, `bogie_pivot_distance_m >= length_m`, unknown `mode`, malformed colour.
A malformed file must not take down the whole catalogue load — collect per-file errors, load the rest, and
expose the failures.

### COYPU import

```python
def read_vehicle_csv(path: Path) -> tuple[str, VehicleDynamics]: ...          # (vehicleName, dynamics)
def dynamics_from_coypu(project: CoypuProject, index: int) -> tuple[str, VehicleDynamics] | None: ...
def merge_dynamics(spec: VehicleSpec, dynamics: VehicleDynamics) -> VehicleSpec: ...
```

Matching a COYPU vehicle to a catalogue entry is by `vehicleName` against `VehicleSpec.name` and `aliases`,
case-insensitively and whitespace-insensitively. No match is a normal outcome, not an error: the run is
still playable with a generic consist, and the caller is told.

The `.coypu` archive's `trainParam` gives only name, rotating-mass factor, mass and length; the Davis and
traction-band data live in the settings alongside it. Take what is present and leave the rest `None`-ish
rather than inventing defaults — a fabricated Davis C is indistinguishable from a measured one downstream.

### Tram catalogue entry

`tram_generic.json`: `mode: "light_rail_tram"`, `gauge_mm: 1000`, two or three short articulated-style cars
(around 8–10 m each), a low floor height, and a `provenance` string that states plainly that the dimensions
are generic placeholders and names no manufacturer or operator. It exists so T-110's tram network and T-112's
mode-agnosticism test have something to run with.

## Invariants

- **`domain/` is pure:** `domain/model/vehicle.py` and `trainset.py` get no file access. All loading lives in
  `io/catalogue/` and `io/coypu/`.
- **ADR 0006:** `Mode` tags semantics only. Nothing in the loader may branch on mode to change geometry
  handling; a 1000 mm tram and a 1435 mm DMU go through the same code.
- **Privacy:** catalogue entries carry generic or publicly-documented dimensions with an honest `provenance`
  string. No operator data, no vendor drawings, no infrastructure-manager sourced figures. Where a number is
  a placeholder, `provenance` must say so — as both existing files already do.
- If T-110 is running concurrently, `domain/model/__init__.py` is a collision point. Keep your edit to
  additive re-exports and mention it in the report.

## Acceptance criteria

1. Both existing catalogue files load and validate unchanged; their field values survive the round trip.
2. `tram_generic.json` loads, and `VehicleSpec.length_m` matches the hand-computed total.
3. A deliberately malformed file (bad `schema`, negative length, pivot distance exceeding car length) is
   rejected with an error naming the file and the field, while the other files still load.
4. `resolve()` finds a spec by key, by alias and by name, case- and whitespace-insensitively.
5. `Trainset.from_spec(spec, units=2)` produces `2 × len(spec.cars)` cars and a length equal to
   `2 × spec.length_m + coupling_gap_m`.
6. The Kralupy `.coypu` vehicle imports: the name is extracted, mass and length match `trainParam`, speeds
   are converted to m/s, and the result either matches a catalogue entry or reports cleanly that it did not.
7. A synthetic COYPU vehicle CSV covering all four sections (`Meta`, `Param`, `Res`, `Trac`) parses into a
   complete `VehicleDynamics` with correct unit conversion; a CSV missing `Res` and `Trac` parses with those
   fields empty rather than zeroed.
8. `load_catalogue()` works with no argument from a plain `uv run` invocation.
9. `docs/data-contracts/vehicle-catalogue.md` documents every field with units and states which are required.
10. `uv run ruff check .` clean, `uv run pytest` green.

## Out of scope

- Any 3-D mesh, glTF loading, or rendering. `CarSpec.mesh` is a declared field that stays `None` in Phase 1.
- Re-simulating kinematics from `VehicleDynamics`. Builder consumes COYPU's results (see T-111).
- A cross-section or track-component catalogue — `shared/catalogue/cross_sections/` stays empty until Phase 2.
- Protocol exposure — `catalogue.vehicles` is T-114.
- Expanding the catalogue beyond the one tram entry this task adds.

## Verification

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

## Report back

State: whether the Kralupy `.coypu` vehicle matched a catalogue entry and on what key; which
`VehicleDynamics` fields the archive actually supplies versus which only the CSV does; the validation rules
you settled on; and how catalogue path resolution will behave in a frozen build.
