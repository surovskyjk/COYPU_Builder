extends Node3D
## Deliberately temporary bootstrap scene (T-103): a status overlay showing backend/session state. M4
## replaces the overlay (T-140). Do not extend this file's scope -- see "Out of scope" in
## docs/tasks/task_103_client_app_shell.md.
##
## T-121 adds exactly one thing on top of that: building [TrackCorridor] for the first alignment Session
## knows about, once one exists.
##
## T-123 adds one more: once a kinematics run is available (the `.coypu` import path -- a bare LandXML
## import carries no run), it fetches the run's trainset, builds a [TrainsetNode] and drives it with a
## [PlaybackController] from a paused [TimelineState]. Temporary keyboard bindings stand in for the
## timeline UI (T-141): Space toggles play/pause, Home/End seek to the run's start/end, `[`/`]` halve or
## double the rate.
##
## T-124 replaces T-103's temporary free-look `Camera3D` with [CameraManager] -- three deliberate ways to
## watch the railway (orbit/wayside/cab) instead of the throwaway fly-cam. It is built here in code exactly
## like [TrackCorridor] (no `.tscn` node of its own), wired to the corridor/table and trainset/controller
## as soon as each becomes available, and owns both the one `Camera3D.current` and the one
## `TrackCorridor.update_lod` call per frame from here on -- this scene no longer drives either itself.
##
## T-125 adds the seam scripted screenshots need and nothing more: read-only accessors for the camera
## manager, timeline, playback controller, corridor and status overlay; [signal scene_ready]; and, only when `--capture` is
## on the command line, a [CaptureDriver] child. A launch without `--capture` behaves exactly as before.

## Emitted once, when the corridor is built and -- if the project has a run -- playback is wired.
signal scene_ready()

@onready var _status_label: Label = %StatusLabel

var _project_path := ""

var _corridor: TrackCorridor
var _corridor_alignment_id := ""

var _camera_manager: CameraManager

var _timeline: TimelineState
var _playback: PlaybackController
var _trainset_node: TrainsetNode
var _playback_wired := false

var _corridor_built := false
var _playback_settled := false
var _scene_ready_emitted := false


func _ready() -> void:
	var args := CliArgs.from_cmdline()
	_project_path = args.get("project", "")

	Backend.state_changed.connect(_on_backend_state_changed)
	EventBus.project_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_on_alignments_changed)
	EventBus.runs_changed.connect(_on_runs_changed)

	_corridor = TrackCorridor.new()
	add_child(_corridor)
	Session.layers().define(TrackCorridor.LAYER_ID, "Track")

	_camera_manager = CameraManager.new()
	add_child(_camera_manager)

	Backend.start()
	_refresh_status()

	var capture_scenario: String = args.get("capture", "")
	if not capture_scenario.is_empty():
		var driver := CaptureDriver.new()
		add_child(driver)
		driver.start(self, capture_scenario, args.get("capture_out", ""))


func camera_manager() -> CameraManager:
	return _camera_manager


## Null until a run is wired (a bare LandXML import has none).
func timeline() -> TimelineState:
	return _timeline


## Null until a run is wired.
func playback() -> PlaybackController:
	return _playback


func corridor() -> TrackCorridor:
	return _corridor


## The status text overlay (T-103). [CaptureDriver] hides it while capturing so a screenshot and its
## statistics show only the 3D render.
func status_overlay() -> CanvasItem:
	return _status_label


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
	_camera_manager.bind_corridor(_corridor, table)
	_corridor_built = true
	_check_scene_ready()


## Wires the first run Session knows about, once (T-123) -- `import_coypu`'s `RunSummary` carries the
## `trainset_id` `handle_import_coypu` created for it, so no separate `trainset.create` call is needed.
func _on_runs_changed() -> void:
	if _playback_wired:
		return
	var runs := Session.runs()
	if runs.is_empty():
		return
	_playback_wired = true
	await _wire_playback(runs[0])
	_playback_settled = true
	_check_scene_ready()


## A run's trainset is only wired after `Session.runs()` is populated, which happens before the corridor
## build finishes, so "the project has a run" is already known when the corridor completes.
func _check_scene_ready() -> void:
	if _scene_ready_emitted or not _corridor_built:
		return
	if not Session.runs().is_empty() and not _playback_settled:
		return
	_scene_ready_emitted = true
	scene_ready.emit()


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

	_camera_manager.bind_subject(_trainset_node, _playback)


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
	if event is InputEventKey and event.pressed and not event.echo:
		_handle_playback_key(event.keycode)
