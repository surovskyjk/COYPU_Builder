class_name CameraManager
extends Node3D
## Owns the three camera rigs (T-124) and is the *only* place that flips a `Camera3D.current` or calls
## `TrackCorridor.update_lod` -- both per the task's invariants, so LOD can never be driven by a stale or
## inactive viewpoint. Replaces T-103's temporary free-look `Camera3D` in `scene/main.gd`.
##
## Each [CameraRig] computes its own transform in [method CameraRig.update], called explicitly from this
## node's own `_process` rather than relying on Godot's automatic per-node `_process` on the rigs
## themselves -- that keeps the per-frame ordering (rig transform, then the one `update_lod` call reading
## it) exact and same-frame, and keeps every rig's math callable directly from a test without first adding
## it to a live [SceneTree] (see [CameraRig]'s header).
##
## Mode-switch input (`camera_mode_orbit`/`camera_mode_wayside`/`camera_mode_cab`, defined in
## `project.godot`'s input map, not hard-coded scancodes, so M4 can rebind them) is handled here; any event
## a mode switch didn't consume is forwarded to the active rig alone ([method CameraRig.handle_input]) --
## only [OrbitCamera] currently does anything with it.
##
## Rig construction happens in [method _init], the same reason [CameraRig] builds its own `Camera3D` child
## there: [method set_mode]/[method bind_subject]/[method bind_corridor]/[method focus_station]/
## [method active_camera] all need to work on a freshly-constructed manager without it first being added
## to a live tree, since `client/tests/unit/test_camera_rigs.gd` exercises them that way.

enum Mode { ORBIT, WAYSIDE, CAB }

signal mode_changed(mode: Mode)

var _mode := Mode.ORBIT
var _rigs: Array[CameraRig] = []
var _corridor: TrackCorridor


func _init() -> void:
	var orbit := OrbitCamera.new()
	orbit.name = "OrbitCamera"
	var wayside := WaysideCamera.new()
	wayside.name = "WaysideCamera"
	var cab := CabCamera.new()
	cab.name = "CabCamera"

	_rigs = [orbit, wayside, cab]
	for rig in _rigs:
		add_child(rig)
	_activate(Mode.ORBIT)


func set_mode(mode: Mode) -> void:
	if mode == _mode:
		return
	_activate(mode)
	mode_changed.emit(mode)
	EventBus.camera_changed.emit(mode)


func mode() -> Mode:
	return _mode


func active_camera() -> Camera3D:
	return _rigs[_mode].camera()


## Testability accessor beyond the fixed contract above: lets a test confirm that switching modes leaves
## every *other* rig's `Camera3D.current` false, not just that the active one's is true.
func camera_for(mode: Mode) -> Camera3D:
	return _rigs[mode].camera()


func bind_subject(trainset: TrainsetNode, controller: PlaybackController) -> void:
	for rig in _rigs:
		rig.bind_subject(trainset, controller)


func bind_corridor(corridor: TrackCorridor, table: AlignmentTable) -> void:
	_corridor = corridor
	for rig in _rigs:
		rig.bind_corridor(corridor, table)


## All modes reposition to this station -- see each rig's own `focus_station` for what "reposition" means
## to it (the cab rig has no station concept of its own and treats this as a no-op).
func focus_station(s: float) -> void:
	for rig in _rigs:
		rig.focus_station(s)


func _activate(mode: Mode) -> void:
	_mode = mode
	for i in _rigs.size():
		_rigs[i].camera().current = i == mode
	_rigs[mode].activated()


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("camera_mode_orbit"):
		set_mode(Mode.ORBIT)
	elif event.is_action_pressed("camera_mode_wayside"):
		set_mode(Mode.WAYSIDE)
	elif event.is_action_pressed("camera_mode_cab"):
		set_mode(Mode.CAB)
	else:
		_rigs[_mode].handle_input(event)


func _process(delta: float) -> void:
	var rig := _rigs[_mode]
	rig.update(delta)
	if _corridor != null:
		_corridor.update_lod(rig.camera().global_position)
