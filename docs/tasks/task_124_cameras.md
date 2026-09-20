# T-124 — Cameras: manager, orbit, wayside and an XR-ready cab rig

**Milestone:** P1.M2 · **Depends on:** T-121, T-123 · **Closes:** P1.M2

## Context

T-121 put the corridor on screen and T-123 put a train on it, both still viewed through the throwaway
free-look camera T-103 left behind. This task replaces it and closes M2: three deliberate ways to watch a
railway, and the rig that makes OpenXR a Phase 4 configuration change rather than a rewrite.

Cameras are where the sandbox-game feel of the product either arrives or doesn't. Movement should be
damped and continuous rather than snapping, because a camera that teleports makes a smooth 60 fps simulation
look broken.

## Preconditions

- T-121: `TrackCorridor`, `AlignmentTable`, and `update_lod(camera_position)` waiting for a real camera.
- T-123: `PlaybackController.lead_station()`, `TimelineState`, a posed `TrainsetNode` whose `Car` nodes carry
  `body()`, `bogie_front()`, `bogie_rear()`.
- T-122: `CarSpec.floor_height_m` and `height_m`, which locate a driver's eye point.
- T-103: `main.tscn` with its temporary `Camera3D` and status overlay — **remove the camera here**; the
  overlay stays until M4.
- `client/cameras/` is an empty placeholder.

Read before starting: `docs/adr/0002-client-language.md` (XR is a reserved escape hatch, not built now),
`docs/data-contracts/coordinate-conventions.md` (Godot axes, `−Z` forward), `client/scene/track/`,
`client/playback/playback_controller.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/cameras/camera_manager.gd` | new — owns the rigs, switches, drives LOD |
| `client/cameras/orbit_camera.gd` | new |
| `client/cameras/wayside_camera.gd` | new |
| `client/cameras/cab_camera.gd` | new — XR-ready rig |
| `client/cameras/camera_rig.gd` | new — the shared base |
| `client/scene/main.gd` | replace the temporary camera with `CameraManager` |
| `client/scene/main.tscn` | remove the temporary `Camera3D` |
| `client/core/event_bus.gd` | add `camera_changed(mode)` |
| `client/tests/unit/test_camera_rigs.gd` | new |
| `client/tests/integration/test_camera_follow.gd` | new — headless, follows a real run |
| `client/README.md` | describe `cameras/` |

## Contract

```gdscript
class_name CameraManager
extends Node3D

enum Mode { ORBIT, WAYSIDE, CAB }

signal mode_changed(mode: Mode)

func set_mode(mode: Mode) -> void
func mode() -> Mode
func active_camera() -> Camera3D
func bind_subject(trainset: TrainsetNode, controller: PlaybackController) -> void
func bind_corridor(corridor: TrackCorridor, table: AlignmentTable) -> void
func focus_station(s: float) -> void          # all modes reposition to this station
```

Exactly one `Camera3D` is `current` at a time. `CameraManager` calls `corridor.update_lod(position)` once per
frame for the active camera only — **it is the single owner of that call**, so LOD can never be driven by a
stale or inactive viewpoint.

**Orbit** — the default. Pivots about a focus point, mouse-drag to rotate, wheel to zoom, middle-drag to pan.
Distance-scaled pan and zoom speed, so it stays usable from 5 m and from 5 km. Optionally follows the consist
with damping. This is the camera a user spends most of their time in; it should feel like a sandbox game, not
a CAD viewport.

**Wayside** — a fixed observer beside the track at a chosen station, tracking the train as it passes, with
the look-at damped so it eases rather than snaps. Offset from the alignment via `AlignmentTable.sample()`
plus a lateral offset along the frame's `left` and a height along its `up`. Re-seats to a station ahead of
the consist once the train has passed.

**Cab** — inside the lead car, at a driver's eye point. Position from the lead `Car`'s body transform plus a
local offset: forward to the cab end, laterally toward the driver's side, and `floor_height_m + ~1.6 m` up.
The rig's structure is what matters:

```
CabCamera (Node3D)          <- XROrigin3D in Phase 4; posed from the car body
└── Camera3D                <- XRCamera3D in Phase 4; local offset only
```

Keep the eye offset entirely in the **child's** local transform and the vehicle-following entirely in the
**parent's** global transform. That separation is the whole XR-readiness claim: in Phase 4 the parent becomes
`XROrigin3D` and the child is replaced by the headset pose, with no change to the following logic. Write that
down in the file header. **Do not add OpenXR, an XR interface, or any `xr_` setting now** — ADR 0002 reserves
XR as an escape hatch for Phase 4.

The cab camera inherits the body's **roll**, so cant is felt. That is the point of the mode, and it is also a
free visual check on T-123's roll handling.

### Shared behaviour

`camera_rig.gd` carries what all three need: damped position and look-at (exponential smoothing with a
documented time constant), near/far planes chosen for a corridor at this scale, and the fact that **all
positions are already base-point-relative** — nothing here calls `Origin.to_godot`, and the readout of a
camera's `(E, N, H)` is the one legitimate use of `Origin.from_godot`.

Input bindings are defined in `project.godot`'s input map, not hard-coded scancodes, so M4 can rebind them.

## Invariants

- **ADR 0004:** cameras move in base-point-relative Godot space. Never reconstruct absolute coordinates for
  positioning; use `Origin.from_godot` only for display.
- **ADR 0007:** `_process` does local arithmetic only. No RPC, no `await`, no per-frame allocation.
- **ADR 0002:** typed GDScript, tabs, no logic in `.tscn`, no XR dependency added.
- **ADR 0006:** the cab rig reads dimensions from `CarSpec`; nothing may assume a heavy-rail cab. The tram
  consist must work.
- T-123's poses are the source of truth for where the train is. Cameras read them; they never re-derive a
  station from a position.

## Acceptance criteria

1. Switching between the three modes at runtime leaves exactly one camera `current`, with no frame where the
   viewport is black or the scene jumps.
2. Orbit: rotate, zoom and pan behave at 5 m and at 5 km from the focus, with no gimbal flip at the poles and
   no precision breakdown at the far end of the corridor.
3. Wayside: the train passes through frame smoothly; the look-at is visibly damped, not snapping; the rig
   re-seats ahead after the consist passes.
4. Cab: the view sits inside the lead car at a plausible driver's eye height, **rolls with cant** — verified
   on the tram alignment where cant is non-zero — and shows the corridor ahead rather than the car interior.
5. The cab rig's parent holds the vehicle pose and the child holds only the eye offset; a unit test asserts
   that zeroing the child's transform leaves the parent exactly on the car body.
6. `corridor.update_lod` is called once per frame, for the active camera only, from `CameraManager` alone.
7. Frame time during playback with the corridor, a three-car consist and each camera mode stays under
   16.6 ms; report all three measurements.
8. `main.tscn` no longer contains the temporary `Camera3D`.
9. The headless integration test runs a real playback and asserts each mode produces finite, changing camera
   transforms that track the consist.
10. Full gdUnit4 suite green on Windows and on Linux CI.

## Out of scope

- OpenXR, any XR interface, headset support, stereo rendering — Phase 4.
- Camera UI: a mode switcher widget, bookmarks, saved viewpoints — M4.
- Cinematic paths, fly-throughs, recording, screenshots-as-a-feature.
- Picking, selection or hover highlighting.
- Terrain-aware collision or ground clamping — there is no terrain until M3.
- Minimap or plan view (Phase 2's `SubViewport` work).

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
tools\run_dev.ps1
```

```bash
cd backend
uv run pytest -q
```

## Report back

State: measured frame time in each mode; the damping time constants and how you chose them; confirmation
that the cab view rolls with cant on the tram alignment, with a screenshot from each of the three modes; and
whether the XR split (parent = vehicle pose, child = eye offset) survived contact with the follow logic.

**This task closes M2.** Include a short assessment of whether the M2 exit criteria are met end to end: the
Kralupy corridor rendering without shimmer at any zoom, a three-car consist playing back at a locked 60 fps
with bogies on the rails and bodies chording the curves, and the client chain matching
`shared/golden/trainset_chain.json`.
