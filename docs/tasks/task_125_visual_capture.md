# T-125 — Visual capture harness: scripted screenshots and frame-time statistics

**Milestone:** Phase 1 workflow tooling · **Depends on:** T-124 · **Blocks:** nothing; it lets the
`coypu-reviewer` agent check screenshots itself

## Context

Visual review has depended on the user taking screenshots by hand. Reviews now run in a reviewer subagent
(`.claude/agents/coypu-reviewer.md`), which can read PNG files but cannot operate a window. This task adds a
scripted capture: the real app with a real backend, driven by a scenario file that lists the shots, writing
one PNG per shot plus a manifest with frame-time statistics.

It also turns two recurring manual checks into evidence: a nearly black scene (the M2 lighting defect looked
exactly like that) and frame-time claims such as "a locked 60 fps".

Capture needs a desktop GPU. It never runs on CI, whose headless dummy renderer produces no pixels.

## Preconditions

- T-124: `CameraManager` with `set_mode(Mode)`, `focus_station(s)` and `active_camera()`.
- T-123: `TimelineState` (`seek`, `play`, `pause`, `set_rate`, `duration`) and `PlaybackController.lead_station()`.
- T-121: `TrackCorridor.build()`, after which `EventBus.track_mesh_ready` fires.
- T-103: `CliArgs` (the flags after `--`) and `main.gd`, which bootstraps from `--project`.
- T-116: `Backend.shutdown()` stops the whole backend process tree on Windows and Linux.
- `main.gd` keeps the camera manager, timeline and corridor in private fields today.

Read before starting: `client/scene/main.gd` (its header comment limits what may be added there),
`client/core/cli_args.gd`, `client/cameras/camera_manager.gd`, `client/playback/timeline_state.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/tools/capture/scenario.gd` | new — pure parser and validator for scenario JSON |
| `client/tools/capture/capture_driver.gd` | new — runs a scenario inside the live app |
| `client/tools/capture/scenarios/kralupy_m2.json` | new — the default scenario |
| `client/core/cli_args.gd` | add the `--capture` and `--capture-out` flags |
| `client/scene/main.gd` | read-only accessors, a `scene_ready` signal, and attaching the driver |
| `tools/capture.ps1` | new — launch, wait, time out, report |
| `client/tests/unit/test_capture_scenario.gd` | new |
| `client/README.md` | a short "Capturing screenshots" section |
| `.gitignore` | `captures/` |

## Contract

### Scenario file

```json
{
  "project": "backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu",
  "resolution": [1600, 900],
  "ready_timeout_s": 90,
  "shots": [
    {"name": "orbit_overview", "camera": "orbit", "time_s": 0.0, "focus_station_m": 9000.0, "settle_frames": 60}
  ],
  "perf": {"camera": "cab", "time_s": 0.0, "rate": 4.0, "duration_s": 10.0}
}
```

- `project` is repo-relative; `tools/capture.ps1` resolves it and passes it as `--project`.
- A shot needs a unique `name` matching `[a-z0-9_]+` and a `camera` of `orbit`, `wayside` or `cab`.
  `time_s` (optional) seeks the timeline and requires a run; `focus_station_m` (optional) calls
  `CameraManager.focus_station`; `settle_frames` (default 30) is how many frames to render after positioning,
  so camera damping, LOD and chunk paging finish before the capture.
- `perf` (optional) plays for `duration_s` at `rate` from `time_s` with the given camera.
- `Scenario.parse(text: String) -> Scenario` returns the parsed scenario or carries a list of human-readable
  errors: unknown camera, duplicate or malformed name, negative values, `time_s` beyond the run's duration
  (checked by the driver once the run is known).

### Main scene

`main.gd` gains `signal scene_ready()`, emitted once when the corridor is built and, if the project has a
run, playback is wired. It also gains `camera_manager() -> CameraManager`, `timeline() -> TimelineState`
(null without a run) and `corridor() -> TrackCorridor`. When `--capture <res:// or absolute path>` is
present, it adds a `CaptureDriver` child. Nothing else in `main.gd` changes, and a launch without `--capture`
behaves exactly as before.

### Driver

1. Wait for `scene_ready`, up to `ready_timeout_s`.
2. For each shot: set the camera mode, seek and pause the timeline, focus the station, render
   `settle_frames` frames, `await RenderingServer.frame_post_draw`, then save
   `get_viewport().get_texture().get_image()` to `<out>/<name>.png`.
3. For `perf`: disable vsync, play, record every frame's delta for `duration_s`, then restore vsync and
   pause.
4. Write `<out>/manifest.json` and quit through `Backend.shutdown()` and `get_tree().quit(code)`. The exit
   code is 0 if every shot was written, otherwise 1. **The capture must leave no backend process behind** —
   quit through `Backend.shutdown()`, never through a bare `quit()`.

### Manifest

```json
{
  "scenario": "kralupy_m2", "godot": "4.7.2", "adapter": "<RenderingServer.get_video_adapter_name()>",
  "resolution": [1600, 900], "git_commit": "<passed in by capture.ps1>",
  "shots": [{"name": "...", "file": "....png", "camera": "cab", "time_s": 420.0, "lead_station_m": 12345.6,
             "luma_mean": 0.41, "luma_std": 0.17}],
  "perf": {"frames": 0, "p50_ms": 0, "p95_ms": 0, "p99_ms": 0, "max_ms": 0, "over_16_7_ms": 0, "over_33_3_ms": 0},
  "errors": []
}
```

`luma_mean` and `luma_std` are computed from each saved image and make a black or empty frame obvious.

### Wrapper

```text
tools\capture.ps1 [-Scenario kralupy_m2 | <path>] [-Out <dir>] [-TimeoutS 300]
```

It reads the scenario, launches the windowed `_console.exe` build with `--resolution` and the user flags,
waits with a timeout, and on timeout kills the process tree (`taskkill /T /F`) and exits 2. It then prints the
output folder and a one-line summary per shot and for `perf`. The default output is
`captures\<yyyyMMdd-HHmmss>-<scenario>\` at the repository root.

## Invariants

- No RPC from `_process`: the driver awaits requests from its own coroutine, as `main.gd` does.
- The driver only observes and drives public API; it adds no behaviour to cameras, timeline or corridor.
- Nothing is written into the repository outside `captures/`.

## Acceptance criteria

1. `tools\capture.ps1` on `kralupy_m2` writes one PNG per shot plus `manifest.json`, exits 0, and afterwards
   no backend process from the run is alive (checked by the script and stated in the report).
2. The default scenario has at least five shots that together show: the corridor from above, a curve close
   up, the train passing the wayside camera, the cab view on a curve, and the train at the end of the run
   (the clamp). Choose the times from the run itself and say why each was chosen.
3. Every default shot has `luma_std` above 0.02 and `luma_mean` between 0.05 and 0.95, so a black, blank or
   overexposed frame fails the run.
4. `perf` reports p50, p95, p99 and max frame time with vsync off; the default scenario's numbers go in the
   report.
5. Parser tests cover a valid file, each error kind, and defaults. An invalid scenario makes the app exit 1
   with the errors printed, without hanging.
6. Launching without `--capture` is unchanged. Both suites are green on Windows and on Linux CI, with no
   previously passing test removed, skipped or weakened.

## Out of scope

- Running captures on CI — not planned; CI runners have no GPU.
- Reference images and pixel-difference regression testing — not planned for Phase 1.
- Any in-app UI for captures — not planned.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario kralupy_m2
```

Open each PNG the capture wrote and confirm it shows what its name claims.

## Report back

State: the shot list with the time chosen for each and why; each shot's `luma_mean` and `luma_std`; the
`perf` statistics and the adapter name; the run's wall-clock duration; and how you confirmed no backend
process survived.
