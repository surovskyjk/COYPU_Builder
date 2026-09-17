# COYPU Builder client (Godot 4.7)

Typed-GDScript Godot project: UI, 2D/3D viewports, cameras, playback and tools. All geometry comes from the
backend as baked tables (ADR 0007); the client never computes alignments itself.

```
core/           autoloads: Backend (IPC), Session (document mirror), Origin (base point + axis mapping), EventBus
ipc/            websocket_client, envelope codec, rpc ids/timeouts, process_supervisor
domain_mirror/  alignment_table (frame table + interpolation), run_table (t→s), entity_registry, layer_state
scene/          main.tscn; world/, track/, vehicles/ (Car = CarBody + BogieFront + BogieRear), terrain/, context/, gizmos/
playback/       playback_controller, trainset_kinematics, timeline_state
cameras/        camera_manager, orbit_camera, wayside_camera, cab_camera (XR-ready rig)
ui/             theme, top_bar, bottom_dock, layers_panel, inspector_panel, timeline_bar, dialogs, widgets
view_modes/     view_mode_controller + materials (realistic, wireframe, xray, diagnostics)
tools/          (Phase 2) placement_tool, snapping, ghost_preview, plan_view (SubViewport + ortho Camera3D)
tests/          gdUnit4 — unit/, integration/, helpers/ (see below)
assets/         placeholder materials/icons; vehicle glTF later
```

Open with `tools\godot\Godot_v4.7.2-stable_win64.exe --path client --editor` after `tools\install_godot.ps1`.

## Tests

`addons/gdUnit4` is vendored at **v6.2.1** (from
[godot-gdunit-labs/gdUnit4](https://github.com/godot-gdunit-labs/gdUnit4), the project MikeSchulze/gdUnit4
now redirects to). gdUnit4's own compatibility table has a gap: the last release declaring 4.4 support
(v5.1.1) fails to compile against 4.7.2 (`FileAccess.get_as_text()` argument mismatch), and nothing after
it declares 4.4 again — v6.0+ moved its floor to 4.5. v6.2.1 is the newest release, compiles cleanly with
zero errors against 4.7.2, and its own suite passes; that empirical check is what picked it, not the
published table.

```
client/tests/
  unit/          no backend, no network — must run in under 5 seconds total
  integration/   spawns the real backend through helpers/backend_fixture.gd
  helpers/       shared test utilities (BackendFixture, GoldenLoader, UvLocator) — not test suites
```

gdUnit4's CLI tool always requires `--ignoreHeadlessMode` when run with `--headless`, even though none
of our suites do anything UI-dependent — otherwise it refuses to start:

```powershell
# fast local iteration: unit tests only, no backend spawned
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests/unit --ignoreHeadlessMode

# full suite (unit + integration), same as CI
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

Run the editor-import step first (`--headless --path client --editor --quit`) after any change that adds
or renames a `class_name` type — it rebuilds `client/.godot/global_script_class_cache.cfg`, without which
the headless script runner can't resolve cross-file types.
