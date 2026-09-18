# Vehicle catalogue (T-113)

`shared/catalogue/vehicles/*.json`, schema `coypu-builder.vehicle-spec/1`. Loaded, validated and indexed by
`backend/src/coypu_builder/io/catalogue/vehicles.py`; the domain types are
`backend/src/coypu_builder/domain/model/vehicle.py` (pure — no I/O) and
`backend/src/coypu_builder/domain/model/trainset.py`.

A catalogue entry is the **geometry** half of a vehicle: car composition and body dimensions, used to
render and clash-check a consist. It optionally carries the **dynamics** half — what a COYPU run was
simulated with (mass, resistance, tractive effort) — imported separately from a COYPU vehicle CSV or a
`.coypu` archive and attached with `merge_dynamics`. Geometry alone is enough for Phase 1; nothing in
`domain/lrs.py` or the renderer requires `dynamics` to be present.

## JSON schema

```json
{
  "schema": "coypu-builder.vehicle-spec/1",
  "name": "string",
  "aliases": ["string", "..."],
  "mode": "heavy_rail | light_rail_tram | trolleybus | road_service",
  "gauge_mm": 1435.0,
  "cars": [
    {
      "name": "string",
      "length_m": 25.5,
      "width_m": 2.93,
      "height_m": 3.73,
      "floor_height_m": 0.6,
      "bogie_pivot_distance_m": 17.5,
      "bogie_wheelbase_m": 1.8,
      "wheel_diameter_m": 0.77,
      "mesh": null,
      "color": "#c8102e"
    }
  ],
  "coupling_gap_m": 0.0,
  "provenance": "string"
}
```

| Field | Unit | Required | Notes |
|---|---|---|---|
| `schema` | — | yes | must equal `coypu-builder.vehicle-spec/1`; any other value is rejected |
| `name` | — | yes | display name; also matched against a COYPU `vehicleName` during dynamics import |
| `aliases` | — | no, default `[]` | alternate names/keys `resolve()` will also match |
| `mode` | — | yes | one of `Mode`'s four values (`domain/model/modes.py`); tags semantics only, never branches geometry handling |
| `gauge_mm` | mm | yes | must be `> 0` |
| `cars` | — | yes | non-empty list, front to back |
| `cars[].name` | — | yes | |
| `cars[].length_m` | m | yes | must be `> 0` |
| `cars[].width_m` | m | yes | must be `> 0` |
| `cars[].height_m` | m | yes | must be `> 0` |
| `cars[].floor_height_m` | m | yes | must be `> 0` |
| `cars[].bogie_pivot_distance_m` | m | yes | must be `> 0` and `< length_m` |
| `cars[].bogie_wheelbase_m` | m | yes | must be `> 0` |
| `cars[].wheel_diameter_m` | m | yes | must be `> 0` |
| `cars[].mesh` | — | no, default `null` | catalogue-relative path to a glTF; Phase 2. `null` = procedural mesh (the only value Phase 1 produces) |
| `cars[].color` | — | no, default `#808080` | `#RRGGBB` hex, used by the Phase 1 procedural mesh |
| `coupling_gap_m` | m | no, default `0.0` | must be `>= 0`; gap between consecutive cars, reused between repeated units by `Trainset.from_spec` |
| `provenance` | — | no, default `""` | free text; must say plainly where the numbers came from, and if a dimension is a placeholder, say so. Never names a vendor, operator or infrastructure manager (privacy invariant, `CLAUDE.md`) |

`key` is not a JSON field — it is the catalogue file's stem (`dmu_br650_cd840.json` → key
`"dmu_br650_cd840"`), assigned by the loader.

`VehicleSpec.length_m` (computed, not stored) = sum of `cars[].length_m` + `coupling_gap_m` × (number of
cars − 1).

## Validation

`load_vehicle_spec` raises `VehicleSpecError` (a `ValueError`) whose message is `"{path}: {detail}"`, naming
both the file and the offending field, for:

- an unreadable file or invalid JSON
- `schema` missing or not equal to `coypu-builder.vehicle-spec/1`
- a missing required field (`name`, `mode`, `gauge_mm`, `cars`, or any required `cars[]` field)
- a non-positive dimension (`gauge_mm`, or any `cars[]` length/width/height/floor/bogie/wheel field)
- `bogie_pivot_distance_m >= length_m`
- an unknown `mode` value
- a malformed `color` (anything not matching `^#[0-9A-Fa-f]{6}$`)
- a negative `coupling_gap_m`
- `aliases` present but not a list of strings

`load_catalogue(directory=None)` loads every `*.json` file in the directory (default:
`shared/catalogue/vehicles/`, resolved by `catalogue_root()` — see below). A malformed file does not fail
the whole load: it is skipped, and its `VehicleSpecError` is collected on the returned `Catalogue` (a
`dict[str, VehicleSpec]` subclass) as `catalogue.errors`, a `tuple[VehicleSpecError, ...]`. Valid files are
returned keyed by `VehicleSpec.key` regardless of any sibling file's failure.

## Path resolution (source checkout vs. frozen build)

`catalogue_root()` (`io/catalogue/vehicles.py`) is the single place that locates `shared/catalogue/`. It
walks upward from `sys.executable` (when `sys.frozen`, i.e. the PyInstaller build from ADR 0008) or from
this module's own `__file__` (a source checkout), looking for the first ancestor directory that contains a
`shared/catalogue/` subdirectory, and returns that subdirectory. This works both from
`backend/src/coypu_builder/io/catalogue/` under the repository root and from a frozen
`coypu-builder-backend.exe` that ships with `shared/` alongside it, without hard-coding a parent-directory
count that would break if either layout's depth changes.

## Resolving a spec: `resolve()`

```python
def resolve(catalogue: Mapping[str, VehicleSpec], key_or_alias_or_name: str) -> VehicleSpec | None: ...
```

Tries an exact key lookup first, then a case-folded, whitespace-collapsed comparison against every spec's
`key`, `name`, and each of its `aliases`. Returns `None` on no match — this is the same function used to
match a COYPU `vehicleName` to a catalogue entry; no match is a normal, expected outcome (the run stays
playable with a generic consist), not an error.

## `Trainset`

`domain/model/trainset.py`. The resolved, flattened consist that T-112 poses and T-122 renders — carries
`CarSpec`s directly rather than a `VehicleSpec` reference:

```python
Trainset.from_spec(spec, units=2)   # -> 2 * len(spec.cars) cars, length == 2 * spec.length_m + spec.coupling_gap_m
```

`units` repeats the whole spec back-to-back; the same `coupling_gap_m` is used both within a unit and
between units.

## COYPU dynamics import

`io/coypu/vehicles.py`. Two sources, both documented in `docs/data-contracts/coypu-kinematics.md`:

- `read_vehicle_csv(path) -> (vehicleName, VehicleDynamics)` — the extended vehicle CSV
  (`Section,Col1,...` rows tagged `Meta`/`Param`/`Res`/`Trac`). `Meta` wins over the redundant values in
  `Param` when both are present, matching COYPU's own reader.
- `dynamics_from_coypu(project, index) -> (vehicleName, VehicleDynamics) | None` — vehicle `index` from a
  `.coypu` archive's `vehicleConfiguration.settingsData.vehicles[index]`. Returns `None` only when there is
  no vehicle at that index; a catalogue match failure is `resolve()`'s concern, not this function's.

### `VehicleDynamics` — what each source actually supplies

| Field | Source | Notes |
|---|---|---|
| `mass_t` | `Param`/`trainParam[2]`, or `Meta.massTonnes` | always present in both sources |
| `rotating_mass_factor` | `Param`/`trainParam[1]`, or `Meta.rotMassFactor` | always present in both sources |
| `max_speed_ms` | `Meta.maxSpeedKmh` (CSV) or `trainMaxSpeed` (archive), ÷3.6 | always present in both sources |
| `brake_decel_ms2` | `Meta.brakeDecelMs2` (CSV) or `trainBrakeDecel` (archive) | always present in both sources; already SI |
| `max_tractive_force_kn` | `Meta.maxTractiveForceKN` (CSV only) | **`None` from a `.coypu` archive** — the archive settings never carry this field, and it is not derived from the traction curve to avoid presenting a computed value as a measured one |
| `davis_a/b/c` | `Res`/`trainRes` | `None`/`None`/`None` when the `Res` section (CSV) or `trainRes` (archive) is absent, never `0.0` |
| `traction_bands` | `Trac`/`trainTrac` | `()` when absent |

**Unit discipline.** `max_speed_ms` is SI-converted from the source's km/h. The traction-band polynomial
`F(v) = b0 + b1·v + b2·v²` (kN) is calibrated by COYPU for `v` in km/h; on import it is re-expressed for `v`
in m/s by substituting `v_kmh = 3.6·v_ms`, giving `b0' = b0`, `b1' = 3.6·b1`, `b2' = 3.6²·b2` — this keeps
`F` numerically identical, it only changes which unit the stored coefficients expect `v` in, matching
`TractionBand`'s documented "`v` in m/s" contract. The Davis resistance coefficients (`davis_a/b/c`) carry no
formula in the data contract (`docs/data-contracts/coypu-kinematics.md` only names them, and re-simulating
resistance from them is out of scope for T-113 — see T-111), so they are copied through **unconverted**;
downstream code that evaluates them must know they were calibrated for `v` in km/h, same as COYPU itself.

### A note on the field types

The T-113 task contract declared `max_tractive_force_kn`, `davis_a`, `davis_b`, `davis_c` as plain
(non-optional) `float`. Since a `.coypu` archive never supplies `max_tractive_force_kn`, and a CSV missing
its `Res` section supplies no Davis coefficients at all, honouring "never invent a dynamics value the source
did not supply" required widening those four fields to `float | None = None`. This is flagged here and in
the T-113 closing report rather than applied silently.
