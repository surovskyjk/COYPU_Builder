# T-122 — Client vehicle scene: procedural cars and consist assembly

**Milestone:** P1.M2 · **Depends on:** T-113, T-115 · **Blocks:** T-123

## Context

T-113 put vehicle geometry in the catalogue (`VehicleSpec`, `CarSpec`, `Trainset`) and T-114 put it on the
wire (`catalogue.vehicles`, `trainset.create`, `trainset.get`). Nothing renders it. This task builds the
scene tree that T-123 will drive: a car as a body plus two bogies, assembled into a consist.

The split matters. **Bogies are separate nodes because they are posed independently** — T-112's chain puts
each bogie on the alignment at its own pivot station and the body rigidly between them. A car modelled as one
rigid node cannot express that, and the difference is visible in every curve.

Phase 1 is procedural: boxes and cylinders from `CarSpec` dimensions. `CarSpec.mesh` is a declared field that
stays `null`; glTF loading is Phase 2.

## Preconditions

- T-115 has landed: `Session.fetch_catalogue()`, `Session.vehicle_catalogue()`, `EntityRegistry`,
  `LayerState`, `EventBus`.
- T-114 exposes `catalogue.vehicles` (a `VehicleSpecDTO` list with `cars`, `gauge_mm`, `coupling_gap_m`),
  `trainset.create(spec_key, units, name)` and `trainset.get(trainset_id)`.
- `CarSpec` fields: `name`, `length_m`, `width_m`, `height_m`, `floor_height_m`, `bogie_pivot_distance_m`,
  `bogie_wheelbase_m`, `wheel_diameter_m`, `mesh` (null in Phase 1), `color` (hex).
- `client/scene/vehicles/` is an empty placeholder.

Read before starting: `docs/data-contracts/vehicle-catalogue.md`, `docs/data-contracts/trainset-chain.md`
(for what T-123 will ask of these nodes), `client/core/session.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/scene/vehicles/car.gd` | new — `Car`: `CarBody` + `BogieFront` + `BogieRear` |
| `client/scene/vehicles/car.tscn` | new — the node skeleton, no logic |
| `client/scene/vehicles/bogie.gd` | new — `Bogie`: frame plus wheelsets |
| `client/scene/vehicles/car_mesh_builder.gd` | new — `CarSpec` → `ArrayMesh`/primitives |
| `client/scene/vehicles/trainset_node.gd` | new — `TrainsetNode`: an ordered set of `Car`s |
| `client/core/session.gd` | add `fetch_trainset()` / `create_trainset()` and their cache |
| `client/core/event_bus.gd` | add `trainset_ready(trainset_id)` |
| `client/tests/unit/test_car_mesh_builder.gd` | new |
| `client/tests/integration/test_trainset_assembly.gd` | new — real backend, real catalogue |
| `client/README.md` | describe `scene/vehicles/` |

## Contract

### Node structure

```
TrainsetNode (Node3D)
└── Car (Node3D)                  one per car, front to back
    ├── CarBody (MeshInstance3D)  box, posed from the body chord
    ├── BogieFront (Node3D)       posed from its own pivot station
    │   ├── BogieFrame (MeshInstance3D)
    │   └── Wheelset ×2 (MeshInstance3D, cylinder)
    └── BogieRear  (Node3D)       same
```

`CarBody`, `BogieFront` and `BogieRear` are **siblings under `Car`, not nested**, and T-123 sets each of the
three transforms independently in global space. Nesting the bogies under the body would make a bogie's pose
depend on the body's, which inverts the actual relationship — the body follows the bogies.

```gdscript
class_name Car
extends Node3D

static func from_spec(spec: Dictionary, index: int) -> Car   # spec = one CarSpecDTO dictionary

func index() -> int
func length() -> float
func pivot_distance() -> float
func body() -> MeshInstance3D
func bogie_front() -> Node3D
func bogie_rear() -> Node3D
## Sets the three transforms. T-123 calls this every frame; it must not allocate.
func apply_pose(body_xform: Transform3D, front_xform: Transform3D, rear_xform: Transform3D) -> void
```

```gdscript
class_name TrainsetNode
extends Node3D

static func build(trainset: Dictionary) -> TrainsetNode   # trainset = TrainsetDTO
func car_count() -> int
func car(i: int) -> Car
func trainset_id() -> String
func total_length() -> float
```

### Mesh construction

From `CarSpec`, with the body box sitting **above the track plane**, not centred on it:

- Body: `length_m × width_m × height_m`, its underside at `floor_height_m` above the rail head, so the
  box's local centre is at `+(floor_height_m + height_m/2)` on the frame's up axis.
- Bogie frame: a flattened box roughly `bogie_wheelbase_m + wheel_diameter_m` long by the gauge wide.
- Wheelsets: two cylinders of `wheel_diameter_m`, axis across the track, spaced `bogie_wheelbase_m` apart,
  centred at `wheel_diameter_m / 2` above the rail head.
- Albedo from `CarSpec.color`; body, bogie and wheel materials differ so the articulation is legible.

The vertical datum is the **rail head**, because that is where `lrs.frames` puts the track-plane centre under
`RotationPivot.LOW_RAIL`. Getting this wrong buries the train in the ballast or floats it above the rails, and
the error is uniform so it looks deliberate — state the datum in a comment.

A consist is small (tens of nodes), so per-car `MeshInstance3D`s are fine. Do not reach for `MultiMesh`.

### Assembly

`TrainsetNode.build` creates cars front to back in `TrainsetDTO` order. It assigns **no world transforms** —
every car sits at the origin until T-123 poses it. Assembly and posing are separate concerns, and mixing them
is how the chain ends up half-implemented in two places.

## Invariants

- **ADR 0007:** no RPC from `_process`. Fetching a trainset happens once, at build time.
- **ADR 0006:** mode-agnostic. The same builder must produce the tram from `tram_generic.json` (1000 mm
  gauge, ~9 m cars) without a branch on `mode`. Test it.
- **ADR 0002:** typed GDScript, tabs, no logic in `.tscn`. `apply_pose` is on the per-frame path and must not
  allocate.
- Do not reimplement any part of the trainset chain here. Station layout, pivot stations and the body chord
  belong to T-123, against `docs/data-contracts/trainset-chain.md`.

## Acceptance criteria

1. A three-car `dmu_br650_cd840` consist builds with 3 `Car` nodes, 6 bogies and 12 wheelsets.
2. Body box dimensions equal the `CarSpec` values to 1e-6, and the body underside sits exactly
   `floor_height_m` above the car's local origin.
3. Wheel centres sit at `wheel_diameter_m / 2` above the local origin, and the wheelset spacing equals
   `bogie_wheelbase_m`.
4. The tram spec builds correctly at 1000 mm gauge with its own car count and dimensions.
5. `apply_pose` with three known transforms places the three nodes exactly; a unit test asserts the global
   transforms afterwards.
6. `apply_pose` allocates nothing — call it 1000 times in a test and assert object count is unchanged, or
   state plainly why that cannot be measured in gdUnit4.
7. The integration test fetches the real catalogue from a live backend, creates a trainset via
   `trainset.create`, and builds the node tree from the returned DTO.
8. Full gdUnit4 suite green on Windows and on Linux CI.

## Out of scope

- Posing, motion, playback, the chain — T-123.
- glTF meshes, textures, liveries, interiors, pantographs, couplers as geometry.
- Cameras, including the cab camera's eye point — T-124.
- Collision shapes, physics, sound.
- Any vehicle UI or selection.

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

State: the node counts for a three-car DMU and for the tram; the vertical datum you used and how you verified
a car sits on the rails rather than in them; whether `apply_pose` could be proven allocation-free; and
anything in `vehicle-catalogue.md` that was ambiguous when turned into geometry.
