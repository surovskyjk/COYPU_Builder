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
tests/          gdUnit4
assets/         placeholder materials/icons; vehicle glTF later
```

Open with `tools\godot\Godot_v4.7.2-stable_win64.exe --path client --editor` after `tools\install_godot.ps1`.
