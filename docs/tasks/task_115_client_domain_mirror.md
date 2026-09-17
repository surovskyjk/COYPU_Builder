# T-115 — Client domain mirror: alignment table, run table, registries

**Milestone:** P1.M1 · **Depends on:** T-102, T-103, T-114 · **Blocks:** T-121, T-122, T-123, T-142, T-143

## Context

ADR 0007 puts per-frame evaluation in the client: the backend bakes dense tables, and Godot interpolates them
at 60 Hz without ever calling back. The tables now exist on the wire — `alignment.frame_table` has since
Phase 0, `run.get` since T-114 — but nothing on the client holds them or interpolates them.

`client/domain_mirror/` is an empty placeholder. This task fills it with the four objects every later client
task reads from, and pins the interpolation to the backend's float64 reference through
`shared/golden/frame_eval.json` and `shared/golden/run_table.json`.

This is the last task of M1 and the one that makes M2 possible. Its API is consumed by track rendering,
vehicle posing, playback, the layers panel and the inspector, so the contract below is unusually binding.

## Preconditions

- T-103 has landed: `Backend.request()` as a coroutine returning `IpcEnvelope`, `Session`, `Origin`,
  `EventBus`.
- T-102 has landed: gdUnit4, `GoldenLoader.load_golden(name)`, `BackendFixture`.
- T-114 has landed: `run.list`, `run.get` with time-uniform blobs, `catalogue.vehicles`, `trainset.get`.
- `IpcEnvelope` already decodes `<f4` 1-D blobs to `PackedFloat32Array`, `<f4 (n, 3)` to
  `PackedVector3Array`, and `<i4` to `PackedInt32Array`.
- `shared/golden/frame_eval.json` holds Kralupy rows with `station`, `godot_position`,
  `godot_quaternion_xyzw`, `heading`, `roll`, `pitch`, `cant_mm`, `curvature`.
- `shared/golden/run_table.json` holds `dt`, row count, duration and sampled `{t, station, speed, accel}`.

Read before starting: `docs/adr/0007-evaluation-locality.md`, `docs/adr/0004-precision-and-local-origin.md`,
`docs/data-contracts/coordinate-conventions.md`, `docs/protocol/ipc.md`, `client/ipc/envelope.gd`,
`backend/src/coypu_builder/domain/sampling.py`.

## Deliverables

| Path | Action |
|---|---|
| `client/domain_mirror/alignment_table.gd` | new — `AlignmentTable` |
| `client/domain_mirror/frame_sample.gd` | new — `FrameSample` value object |
| `client/domain_mirror/run_table.gd` | new — `RunTable` |
| `client/domain_mirror/entity_registry.gd` | new — `EntityRegistry` |
| `client/domain_mirror/layer_state.gd` | new — `LayerState` |
| `client/core/session.gd` | extend: hold tables, runs, trainsets, the catalogue; fetch through `Backend` |
| `client/core/event_bus.gd` | add the signals listed below |
| `client/tests/unit/test_alignment_table.gd` | new — pinned to `frame_eval.json` |
| `client/tests/unit/test_run_table.gd` | new — pinned to `run_table.json` |
| `client/tests/unit/test_layer_state.gd` | new |
| `client/tests/integration/test_run_import.gd` | new — real backend, `import.coypu` → `run.get` → sample |
| `client/README.md` | describe `domain_mirror/` |

## Contract

### `FrameSample`

```gdscript
class_name FrameSample
extends RefCounted

var station: float
var position: Vector3        # Godot axes, relative to the project base point
var rotation: Quaternion
var roll: float
var pitch: float
var cant_mm: float
var curvature: float
var gradient: float
var elevation: float
var segment_index: int
```

### `AlignmentTable`

```gdscript
class_name AlignmentTable
extends RefCounted

## Built from an alignment.frame_table response: the result Dictionary plus the decoded blobs.
static func from_envelope(envelope: IpcEnvelope) -> AlignmentTable

func alignment_id() -> String
func row_count() -> int
func station_start() -> float
func station_end() -> float

## Interpolated sample at an absolute station, clamped to the table's range.
func sample(station: float) -> FrameSample
## Row index of the last station <= s. Binary search; -1 before the first row.
func index_of(station: float) -> int
## Position only — the hot path for camera follow; avoids building a FrameSample.
func position_at(station: float) -> Vector3
## Raw column access for bulk consumers (track meshing, diagnostics ramps). Do not mutate.
func stations() -> PackedFloat32Array
func positions() -> PackedVector3Array
```

**Interpolation rules — these are what the golden pins:**

- Station spacing is **non-uniform**. `bake_stations` merges a uniform grid with every geometric key station
  and refines curves by chord error, so a linear scan or an assumed constant step is wrong. Use binary search
  (`PackedFloat32Array.bsearch` or your own) and cache the last index, since callers overwhelmingly walk
  forward.
- Scalars (`roll`, `pitch`, `cant_mm`, `curvature`, `gradient`, `elevation`) and `position` interpolate
  **linearly** between the bracketing rows.
- `rotation` interpolates with **`Quaternion.slerp`**, not linearly, and not by interpolating Euler angles.
- `segment_index` takes the **lower** row's value — it is a category, not a quantity.
- Out of range clamps to the first or last row; it never wraps and never extrapolates.

**Tolerances against `frame_eval.json`.** The golden is float64; the wire is float32. Compare positions to
1e-3 m, quaternion components to 1e-5, angles to 1e-5 rad. At exact golden stations the sample must match
within those bounds; at midpoints between golden rows, assert only that the result is bounded by its
neighbours and continuous, since the golden does not define the true value there.

### `RunTable`

```gdscript
class_name RunTable
extends RefCounted

static func from_envelope(envelope: IpcEnvelope) -> RunTable

func run_id() -> String
func dt() -> float
func duration() -> float
func row_count() -> int
func direction() -> int              # +1 forward, -1 reverse
func stops() -> Array[Dictionary]

## O(1): i = t / dt, then lerp with the next row. Clamped at both ends.
func station_at(t: float) -> float
func speed_at(t: float) -> float
func accel_at(t: float) -> float
func has_forces() -> bool
func traction_at(t: float) -> float  # 0.0 when the run carries no force data; check has_forces() first
```

The table is time-uniform by construction (T-114), so there is **no time blob** and no search: reconstruct
`t = i · dt`. During a dwell the station is flat and the speed is zero — the lerp handles it with no special
case, and a test must prove there is no spike.

### `EntityRegistry` and `LayerState`

```gdscript
class_name EntityRegistry
extends RefCounted

func put(entity_id: String, kind: String, data: Dictionary) -> void
func get_entity(entity_id: String) -> Dictionary     # empty Dictionary when absent
func of_kind(kind: String) -> Array[String]
func clear() -> void
```

```gdscript
class_name LayerState
extends RefCounted

signal layer_changed(layer_id: String)

func define(layer_id: String, name: String, visible: bool = true, opacity: float = 1.0) -> void
func is_visible(layer_id: String) -> bool
func opacity(layer_id: String) -> float
func set_visible(layer_id: String, visible: bool) -> void
func set_opacity(layer_id: String, opacity: float) -> void
func layers() -> Array[Dictionary]
```

`LayerState` is pure state with a change signal; it renders nothing and knows nothing about the scene tree.
T-142 builds the panel on top of it, and T-121/T-132 subscribe to apply visibility.

### `Session` extension

```gdscript
func alignment_table(alignment_id: String) -> AlignmentTable          # cached; null when not fetched
func fetch_alignment_table(alignment_id: String, spacing_m: float = 1.0) -> AlignmentTable   # coroutine
func runs() -> Array[Dictionary]
func run_table(run_id: String) -> RunTable
func fetch_run_table(run_id: String, dt: float = 0.05) -> RunTable    # coroutine
func vehicle_catalogue() -> Array[Dictionary]
func fetch_catalogue() -> void                                        # coroutine
func import_coypu(path: String) -> bool                               # coroutine
func entities() -> EntityRegistry
func layers() -> LayerState
```

Caching is by id, and a second fetch of the same id with the same parameters returns the cached table without
a round trip. Every coroutine resolves even on `err` — returning `null` or `false` and emitting
`EventBus.backend_error` — so no caller can hang.

New `EventBus` signals: `alignment_table_ready(alignment_id)`, `runs_changed()`,
`run_table_ready(run_id)`, `catalogue_ready()`, `layers_changed()`.

## Invariants

- **ADR 0001 / ADR 0007:** the client interpolates baked tables and computes no geometry. This mirror does
  arithmetic on numbers the backend produced; it must never construct an alignment, evaluate a clothoid, or
  recompute cant roll.
- **ADR 0004:** positions arriving on the wire are already base-point-relative and in Godot axes. Do **not**
  pass them through `Origin.to_godot` — that would double-apply the mapping. `Origin` is for readouts only.
- **ADR 0002:** typed GDScript, tabs. Hot paths use packed arrays and avoid per-call allocation —
  `position_at` in particular must not allocate a `FrameSample`.
- **Golden files are regenerated by the backend, never hand-edited.** If the client cannot match a golden
  value, that is a finding to report, not a tolerance to widen.

## Acceptance criteria

1. `test_alignment_table.gd` reproduces every row in `shared/golden/frame_eval.json` at its exact station,
   within the tolerances above, for position, quaternion, roll, pitch, cant and curvature.
2. Sampling between golden rows is bounded by the neighbouring rows and continuous; sampling before
   `station_start` and after `station_end` clamps without error.
3. `index_of` is correct at exact row stations, between rows, and at both ends, on the **non-uniform**
   spacing the real table has — construct the test table from a real `alignment.frame_table` response, not
   from a synthetic uniform one.
4. Quaternion interpolation uses slerp: assert that a midpoint sample between two rows with a large heading
   change is a unit quaternion and differs measurably from the normalised linear blend.
5. `test_run_table.gd` reproduces every sample in `shared/golden/run_table.json` — station to 1e-3 m, speed
   to 1e-3 m/s — including the in-dwell sample, where speed is zero and station is flat.
6. `station_at` is clamped and finite for `t < 0`, `t > duration`, and `t` exactly equal to `duration`.
7. `test_run_import.gd` drives a real backend end to end: `import.coypu` on the Kralupy fixture, `run.list`,
   `fetch_run_table`, then samples at ten spread times with finite results and monotone station in the run's
   direction.
8. A second `fetch_alignment_table` with identical parameters issues no second request — assert by counting
   requests or by object identity.
9. A `fetch_*` call against a backend that has been killed resolves with `null`/`false` and emits
   `backend_error`, rather than hanging.
10. Full gdUnit4 suite green; `-a tests/unit` still runs with no backend and in under five seconds.

## Out of scope

- Rendering of any kind: no `Node3D`, no mesh, no material, no scene. This is data only.
- The trainset chain — that is T-123, and it consumes this mirror.
- The layers *panel* (T-142) and the inspector *panel* (T-143); only their state objects live here.
- Editing, mutation, or anything that writes back to the backend.
- Streaming or partial table updates. Span-local deltas are Phase 2 per ADR 0007.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

```bash
cd backend
uv run pytest -q
```

## Report back

State: the measured worst-case deviation from each golden and against which tolerance; how `index_of` handles
the non-uniform spacing and whether the forward-walk cache was worth it; the memory footprint of the Kralupy
frame table at 1 m spacing and of the run table at `dt = 0.05`; and any place where matching the golden
required something this specification did not describe.
