# T-123 — Client playback: transport, scrub and the trainset chain

**Milestone:** P1.M2 · **Depends on:** T-112, T-115, T-122 · **Blocks:** T-124, T-141

## Context

This is the task ADR 0007 was written for. The backend has baked everything — frame tables, run tables, the
chain reference and its golden — and now the client must evaluate a train's pose at 60 Hz from those tables
alone, issuing no RPC from `_process`. It is also the moment the Kralupy corridor stops being scenery and
becomes a simulation you can scrub.

The chain algorithm is specified in full in `docs/data-contracts/trainset-chain.md`, written by T-112
precisely so this task can be implemented without reading the Python. **Read that document first; it is the
specification, not background.** `shared/golden/trainset_chain.json` is the reference you are measured
against, and its tram block is the part that proves your cant handling.

## Preconditions

- T-115: `AlignmentTable.sample()`/`position_at()`, `RunTable.station_at()`/`speed_at()`, `GoldenLoader`.
- T-122: `TrainsetNode`, `Car.apply_pose(body, front, rear)`, all three sub-nodes posed in global space.
- T-112: `docs/data-contracts/trainset-chain.md` and `shared/golden/trainset_chain.json`, which now carries
  a nine-sample Kralupy block (all rolls zero) and a five-sample `tram_block` with real cant ramps.
- `client/playback/` is an empty placeholder.
- **F7:** COYPU's kinematics grid overshoots the alignment — `station_m.max()` is `18185.0` against a
  `station_end` of `18184.971666`. The consist *will* run past the end; clamping is not hypothetical.

Read before starting: `docs/data-contracts/trainset-chain.md`, `docs/adr/0007-evaluation-locality.md`,
`client/domain_mirror/*.gd`, `client/scene/vehicles/car.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/playback/timeline_state.gd` | new — time, rate, loop, play/pause; no scene knowledge |
| `client/playback/trainset_kinematics.gd` | new — the chain, ported from the data contract |
| `client/playback/playback_controller.gd` | new — drives `TrainsetNode` from `TimelineState` each frame |
| `client/core/event_bus.gd` | add `playback_state_changed`, `playback_time_changed(t)` |
| `client/scene/main.gd` | wire a consist onto the imported alignment and start paused |
| `client/tests/unit/test_trainset_kinematics.gd` | new — pinned to `trainset_chain.json`, both blocks |
| `client/tests/unit/test_timeline_state.gd` | new |
| `client/tests/integration/test_playback.gd` | new — real backend, import → run → play |
| `client/README.md` | describe `playback/` |

## Contract

### Timeline

```gdscript
class_name TimelineState
extends RefCounted

signal changed()

func set_duration(seconds: float) -> void
func duration() -> float
func time() -> float
func seek(t: float) -> void            # clamped to [0, duration]
func play() -> void
func pause() -> void
func is_playing() -> bool
func set_rate(x: float) -> void        # 0.25 .. 16.0; negative is not supported in Phase 1
func rate() -> float
func set_loop(enabled: bool) -> void
func advance(delta: float) -> void     # time += delta * rate, clamped or wrapped by loop
```

Pure state with a signal. It knows nothing about alignments, trainsets or the scene tree, so it is unit
testable without a backend.

### The chain

```gdscript
class_name TrainsetKinematics
extends RefCounted

class CarPose:
    var body: Transform3D
    var bogie_front: Transform3D
    var bogie_rear: Transform3D

## Poses every car for a lead-vehicle station. `out_poses` is reused across frames — this is the 60 Hz path
## and it must not allocate.
static func pose(table: AlignmentTable, trainset: Dictionary, station_lead: float,
                 direction: int, out_poses: Array) -> bool     # returns `clamped`
```

Implement exactly the layout and orientation rules in `trainset-chain.md`. The parts that the golden actually
pins, and that are easy to get subtly wrong:

- **Station layout.** Front face, rear face, lead pivot, trail pivot and the next car's front face are all
  *stations* — arc length — not chord distances.
- **`forward` is the chord between the two pivot positions**, normalised, with **no extra multiplication by
  `direction`**. The direction sign is already carried by which pivot the layout assigns as lead. This was
  T-112's F11 defect; the golden's `direction = −1` sample exists to catch a reintroduction.
- **`roll` is the mean of the two pivots' rolls**, then `up` is the normalised mean of the two frames' up
  vectors, Gram-Schmidt'd against `forward`, then `left = cross(up, forward)`. That order is contractual.
- Stations outside the alignment clamp, and the call reports `clamped` so the UI can say so rather than
  silently piling cars at the end.

### Controller

```gdscript
class_name PlaybackController
extends Node

func bind(table: AlignmentTable, run: RunTable, trainset_node: TrainsetNode, timeline: TimelineState) -> void
func unbind() -> void
func lead_station() -> float
func current_speed() -> float
```

In `_process(delta)`: advance the timeline, read `station_at(t)` from the run table, pose the consist, apply
the transforms to the `Car` nodes. **No RPC, no `await`, no allocation on this path.** Pre-size the pose
array at `bind()` and reuse it.

Playback is driven by the **run table's time axis**, not by integrating speed yourself — the backend already
did that integration, and re-doing it would drift from the stops.

## Invariants

- **ADR 0007:** per-frame evaluation is client-side from baked tables. Nothing in `_process` may call
  `Backend.request()`, and the backend's `domain/kinematics/trainset.py` is a golden generator, not a
  runtime dependency.
- **ADR 0006:** mode-agnostic. The same code path poses the tram consist on the 1000 mm tram alignment.
- **ADR 0002:** typed GDScript, tabs. The 60 Hz path is allocation-free.
- `shared/golden/*.json` is regenerated by the backend, never hand-edited. A golden you cannot match is a
  finding to report, not a tolerance to widen.

## Acceptance criteria

1. `test_trainset_kinematics.gd` reproduces **every sample in both blocks** of `trainset_chain.json` —
   pivot positions to 1e-3 m, body positions to 1e-3 m, body quaternion components to 1e-5, roll to 1e-5 rad.
2. The `tram_block` samples pass, including the mid-ramp sample where the two pivots carry measurably
   different roll. If they pass only because roll is being ignored, criterion 1 has not been met — assert
   the body roll is non-zero there.
3. The `direction = −1` sample produces a body `forward` 180° from its `direction = +1` counterpart at the
   same station.
4. Clamping: seeking to the final second of the Kralupy run poses the consist with finite transforms and
   reports `clamped = true` (see F7 — the run's last station is past the alignment end).
5. Playing the full Kralupy run holds 60 fps with the corridor and a three-car consist visible; report the
   measured frame time and the per-frame cost of `pose()`.
6. `pose()` allocates nothing across 1000 calls, or you state plainly why that cannot be measured.
7. Bogies stay on the rails: at several stations in the sharpest curve, each bogie's position is within
   1e-3 m of `AlignmentTable.sample(pivot_station).position`.
8. Car bodies visibly cut across curves rather than bending along them — assert the body centre's lateral
   offset from the centreline is non-zero and inside the curve.
9. `seek`, `play`, `pause`, rate change and loop all behave, with `advance` clamped at both ends.
10. Full gdUnit4 suite green on Windows and on Linux CI.

## Out of scope

- Cameras of any kind — T-124, including the cab camera that will follow this consist.
- Timeline **UI**: transport buttons, scrub bar, stop markers are T-141. This task exposes `TimelineState`
  and drives playback; input can be temporary keyboard bindings in `main.gd`.
- Multiple simultaneous consists, or runs on more than one alignment.
- Reverse playback, physics, suspension, sound.
- Recomputing kinematics from `VehicleDynamics`. Builder plays COYPU's results back; it does not re-simulate.

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

State: the measured worst-case deviation from each golden block against its budget; the per-frame cost of
`pose()` and the overall frame time during playback; confirmation that the tram block's non-zero roll is
genuinely exercised; how clamping behaves at the run's end; and anything in `trainset-chain.md` that was
ambiguous when implemented from prose rather than from the Python.
