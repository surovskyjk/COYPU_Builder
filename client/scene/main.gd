extends Node3D
## Deliberately temporary bootstrap scene (T-103): a free-look camera and a status overlay showing
## backend/session state. M2 replaces the camera (T-124); M4 replaces the overlay (T-140). Do not extend
## this file's scope — see "Out of scope" in docs/tasks/task_103_client_app_shell.md.
##
## T-121 adds exactly one thing on top of that: building [TrackCorridor] for the first alignment Session
## knows about and driving its LOD from this scene's (still temporary) free-look camera every frame.
##
## T-123 adds one more: once a kinematics run is available (the `.coypu` import path -- a bare LandXML
## import carries no run), it fetches the run's trainset, builds a [TrainsetNode] and drives it with a
## [PlaybackController] from a paused [TimelineState]. Temporary keyboard bindings stand in for the
## timeline UI (T-141): Space toggles play/pause, Home/End seek to the run's start/end, `[`/`]` halve or
## double the rate.

const _LOOK_SPEED := 0.005
const _MOVE_SPEED := 12.0
const _MOVE_SPEED_FAST := 40.0

@onready var _status_label: Label = %StatusLabel
@onready var _camera: Camera3D = %FreeLookCamera

var _project_path := ""
var _look_active := false
var _yaw := 0.0
var _pitch := 0.0

var _corridor: TrackCorridor
var _corridor_alignment_id := ""

var _timeline: TimelineState
var _playback: PlaybackController
var _trainset_node: TrainsetNode
var _playback_wired := false


func _ready() -> void:
	var args := CliArgs.from_cmdline()
	_project_path = args.get("project", "")

	var euler := _camera.rotation
	_yaw = euler.y
	_pitch = euler.x

	Backend.state_changed.connect(_on_backend_state_changed)
	EventBus.project_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_on_alignments_changed)
	EventBus.runs_changed.connect(_on_runs_changed)

	_corridor = TrackCorridor.new()
	add_child(_corridor)
	Session.layers().define(TrackCorridor.LAYER_ID, "Track")

	Backend.start()
	_refresh_status()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		Backend.shutdown()
		get_tree().quit()


func _on_backend_state_changed(state: Backend.State) -> void:
	_refresh_status()
	if state == Backend.State.READY:
		_bootstrap_session()


## Only feature work this bootstrap scene is allowed: when `--project` names a `.xml` file, open a new
## project and import it so the overlay has real alignment summaries to show; a `.coypu` archive additionally
## carries kinematics runs (T-123 needs one to wire playback), so it goes through `Session.import_coypu`
## instead. Otherwise just mirror whatever project the backend already has open (there may be none yet).
func _bootstrap_session() -> void:
	var lower_path := _project_path.to_lower()
	if lower_path.ends_with(".xml"):
		await Backend.request("project.new", {})
		var imported := await Backend.request("import.landxml", {"path": _project_path})
		if imported.type != "res":
			push_error("main: import.landxml failed: %s" % str(imported.error))
	elif lower_path.ends_with(".coypu"):
		await Backend.request("project.new", {})
		var imported := await Session.import_coypu(_project_path)
		if not imported:
			push_error("main: import.coypu failed for '%s'" % _project_path)
	var got := await Backend.request("project.get", {})
	if got.type == "res":
		Session.set_project_info(got.result)
	_refresh_status()


## Builds [TrackCorridor] for the first alignment Session knows about, once (T-121). A later
## `alignments_changed` naming a different first alignment replaces it; the same id is a no-op, since
## `Session.alignments_changed` can fire for reasons unrelated to the corridor (e.g. a rename).
func _on_alignments_changed() -> void:
	var alignments := Session.alignments()
	if alignments.is_empty():
		return
	var alignment_id: String = alignments[0].get("alignment_id", "")
	if alignment_id.is_empty() or alignment_id == _corridor_alignment_id:
		return
	_corridor_alignment_id = alignment_id
	_build_track_corridor(alignment_id)


func _build_track_corridor(alignment_id: String) -> void:
	var table := await Session.fetch_alignment_table(alignment_id)
	if table == null:
		return
	await _corridor.build(alignment_id, table)


## Wires the first run Session knows about, once (T-123) -- `import_coypu`'s `RunSummary` carries the
## `trainset_id` `handle_import_coypu` created for it, so no separate `trainset.create` call is needed.
func _on_runs_changed() -> void:
	if _playback_wired:
		return
	var runs := Session.runs()
	if runs.is_empty():
		return
	_playback_wired = true
	_wire_playback(runs[0])


func _wire_playback(run_summary: Dictionary) -> void:
	var alignment_id: String = run_summary.get("alignment_id", "")
	var run_id: String = run_summary.get("run_id", "")
	var trainset_id: String = run_summary.get("trainset_id", "")
	if alignment_id.is_empty() or run_id.is_empty() or trainset_id.is_empty():
		return

	var table := await Session.fetch_alignment_table(alignment_id)
	var run := await Session.fetch_run_table(run_id)
	var trainset_dto: Variant = await Session.fetch_trainset(trainset_id)
	if table == null or run == null or trainset_dto == null:
		return

	_trainset_node = TrainsetNode.build(trainset_dto as Dictionary)
	add_child(_trainset_node)

	_timeline = TimelineState.new()
	_playback = PlaybackController.new()
	add_child(_playback)
	_playback.bind(table, run, _trainset_node, _timeline)
	_timeline.pause()


func _handle_playback_key(keycode: Key) -> void:
	if _timeline == null:
		return
	match keycode:
		KEY_SPACE:
			if _timeline.is_playing():
				_timeline.pause()
			else:
				_timeline.play()
		KEY_HOME:
			_timeline.seek(0.0)
		KEY_END:
			_timeline.seek(_timeline.duration())
		KEY_BRACKETLEFT:
			_timeline.set_rate(_timeline.rate() * 0.5)
		KEY_BRACKETRIGHT:
			_timeline.set_rate(_timeline.rate() * 2.0)


func _refresh_status() -> void:
	var lines := PackedStringArray()
	lines.append("backend: %s" % Backend.State.keys()[Backend.state()])
	lines.append("server: %s (protocol %d)" % [Backend.server_version(), Backend.protocol_version()])

	var project_id := Session.project_id()
	lines.append("project: %s" % (project_id if not project_id.is_empty() else "(none)"))
	var crs := Session.crs()
	lines.append("crs: %s" % (crs if not crs.is_empty() else "(none)"))

	var alignments := Session.alignments()
	lines.append("alignments: %d" % alignments.size())
	for alignment: Dictionary in alignments:
		lines.append(
			" - %s [%s] %.1f m" % [
				alignment.get("name", "?"), alignment.get("mode", "?"), alignment.get("length", 0.0)
			]
		)

	_status_label.text = "\n".join(lines)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_RIGHT:
		_look_active = event.pressed
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED if _look_active else Input.MOUSE_MODE_VISIBLE
	elif event is InputEventMouseMotion and _look_active:
		_yaw -= event.relative.x * _LOOK_SPEED
		_pitch = clampf(_pitch - event.relative.y * _LOOK_SPEED, -1.5, 1.5)
		_camera.rotation = Vector3(_pitch, _yaw, 0.0)
	elif event is InputEventKey and event.pressed and not event.echo:
		_handle_playback_key(event.keycode)


func _process(delta: float) -> void:
	_corridor.update_lod(_camera.global_position)

	if not _look_active:
		return
	var input_dir := Vector3.ZERO
	if Input.is_key_pressed(KEY_W):
		input_dir.z -= 1.0
	if Input.is_key_pressed(KEY_S):
		input_dir.z += 1.0
	if Input.is_key_pressed(KEY_A):
		input_dir.x -= 1.0
	if Input.is_key_pressed(KEY_D):
		input_dir.x += 1.0
	if Input.is_key_pressed(KEY_Q):
		input_dir.y -= 1.0
	if Input.is_key_pressed(KEY_E):
		input_dir.y += 1.0
	if input_dir == Vector3.ZERO:
		return
	var speed := _MOVE_SPEED_FAST if Input.is_key_pressed(KEY_SHIFT) else _MOVE_SPEED
	_camera.translate(input_dir.normalized() * speed * delta)
