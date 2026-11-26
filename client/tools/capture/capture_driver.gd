class_name CaptureDriver
extends Node
## Runs a capture [Scenario] inside the live app (T-125): positions the cameras and timeline through their
## public API, saves one PNG per shot, optionally records frame times with vsync off, writes
## `manifest.json` and quits. `scene/main.gd` adds it when `--capture <scenario.json>` is on the command
## line; `tools/capture.ps1` is the normal way to launch it. Needs a desktop GPU -- CI's dummy renderer
## produces no pixels.
##
## Observes and drives only: it adds no behaviour to cameras, timeline or corridor. No RPC from `_process`
## (invariant): `_process` only samples the frame clock, everything else is a coroutine started from
## [method start]. The app always leaves through [method Backend.shutdown] so no backend process outlives
## the capture, and its exit code is 0 only when every shot was written and looked like a real scene.
##
## Per shot: seek and pause the timeline, wait two frames so [PlaybackController] poses the cars there,
## focus the station, then re-activate the camera mode (switching to another mode and back when it is
## already active) so damped rigs snap to the new subject instead of easing in from the previous shot's
## position, apply the optional orbit adjustments as the mouse input a user would give, and render
## `settle_frames` frames. The orbit rig keeps its distance and pitch between shots, so a shot's
## `orbit_zoom_notches` and `orbit_drag_px` are relative to the previous orbit shot.
##
## T-140: before every shot (and the perf run) the driver poses the UI shell through [UiShell]'s public API:
## `"ui": "hidden"` (the default) hides all chrome so the PNG shows only the 3D render, exactly as T-125 captured
## it; `"ui": "visible"` applies the shot's `preset` and `panel_opacity` (neither is saved to the user's
## settings) and optionally opens a `dialog`. For a visible shot the frame statistics are taken over the 3D view
## area only, so "the scene is not empty" keeps its meaning with panels around it. A `perf` block with
## `"baseline": true` is recorded twice in one run, hidden first, and the manifest holds both.

const _OVER_60HZ_MS := 16.7
const _OVER_30HZ_MS := 33.3
## A shot of a scene that imports terrain waits this long at most for the terrain to settle (T-135 Part B).
const TERRAIN_SETTLE_TIMEOUT_S := 60.0

var _main: Node
var _scenario: Scenario
var _out_dir := ""
var _scene_is_ready := false
var _shell: UiShell
var _background := Color.BLACK

var _manifest_shots: Array[Dictionary] = []
var _manifest_perf: Variant = null
var _manifest_perf_baseline: Variant = null
var _errors: Array[String] = []
## Reasons the run did not do something it was asked for, without failing: an unset variable, or an import whose
## provider could not be reached (T-135 Part C). Printed as `skipped: ...` and listed in the manifest.
var _skipped: Array[String] = []

var _recording_frames := false
var _frame_ms := PackedFloat64Array()
var _last_frame_usec := 0


## `main` is the scene that owns the camera manager, timeline and corridor (`scene/main.gd`);
## `scenario_path` is a `res://` or absolute path to the scenario JSON; `out_dir` is where PNGs and the
## manifest go (empty -> `captures/<timestamp>-<scenario>` at the repository root). Starts the run as a
## coroutine and returns immediately.
func start(main: Node, scenario_path: String, out_dir: String) -> void:
	_main = main
	_out_dir = out_dir
	_main.scene_ready.connect(_on_scene_ready)
	_run(scenario_path)


func _on_scene_ready() -> void:
	_scene_is_ready = true


func _process(_delta: float) -> void:
	if not _recording_frames:
		return
	var now := Time.get_ticks_usec()
	if _last_frame_usec != 0:
		_frame_ms.append(float(now - _last_frame_usec) / 1000.0)
	_last_frame_usec = now


func _run(scenario_path: String) -> void:
	await get_tree().process_frame

	var text := FileAccess.get_file_as_string(scenario_path)
	var read_error := FileAccess.get_open_error()
	if read_error != OK:
		_errors.append("cannot read scenario '%s' (%s)" % [scenario_path, error_string(read_error)])
		_scenario = Scenario.new()
	elif text.strip_edges().is_empty():
		_errors.append("scenario '%s' is empty" % scenario_path)
		_scenario = Scenario.new()
	else:
		_scenario = Scenario.parse(text)
	_scenario.scenario_name = scenario_path.get_file().get_basename()
	for problem in _scenario.errors():
		_errors.append(problem)

	if _out_dir.is_empty():
		_out_dir = _default_out_dir(_scenario.scenario_name)
	var made := DirAccess.make_dir_recursive_absolute(_out_dir)
	if made != OK:
		_errors.append("cannot create output folder '%s' (%s)" % [_out_dir, error_string(made)])
		_finish()
		return

	if _errors.is_empty():
		# T-135 Part C: a variable an import path names is read here, so a scenario run without tools/capture.ps1 still
		# expands it. An unset one skips the whole scenario, which exits 0 with its reason.
		var values := {}
		for name in _scenario.path_variables():
			values[name] = OS.get_environment(name).replace("\\", "/") if OS.has_environment(name) else ""
		var unset := _scenario.unset_variables(values)
		if not unset.is_empty():
			for name in unset:
				_skip("%s is not set" % name)
		else:
			_scenario.expand_import_paths(values)
			await _capture_all()
	_finish()


func _capture_all() -> void:
	if not await _run_imports():
		return
	if not await _wait_for_scene():
		return

	var timeline: TimelineState = _main.timeline()
	var playback: PlaybackController = _main.playback()
	if timeline == null and not Session.runs().is_empty():
		_errors.append("playback failed to wire although the project has a run (see the app log)")
		return
	var duration_s: float = timeline.duration() if timeline != null else 0.0
	for problem in _scenario.check_against_run(timeline != null, duration_s):
		_errors.append(problem)
	if not _errors.is_empty():
		return

	var environment_node := _main.find_children("*", "WorldEnvironment", true, false)
	if environment_node.is_empty() or (environment_node[0] as WorldEnvironment).environment == null:
		_errors.append("no WorldEnvironment with an environment on the main scene, so no background colour")
		return
	_background = (environment_node[0] as WorldEnvironment).environment.background_color
	_shell = _main.shell()
	_shell.set_chrome_visible(false)
	await _frames(2)

	# A perf block marked "first" runs before the shots: a shot that frames a whole terrain loads every tile it
	# shows, and the perf block then has nothing left to stream (T-135 Part B).
	var perf_first: bool = _scenario.has_perf() and bool(_scenario.perf.get("first", false))
	if perf_first:
		await _record_perf(_scenario.perf, timeline)
	for shot in _scenario.shots:
		await _capture_shot(shot, timeline, playback)
	if _scenario.has_perf() and not perf_first:
		await _record_perf(_scenario.perf, timeline)


## T-135 Part B: the scenario's `imports`, each run through the calls the app itself makes: `import.inspect` for the
## suggested settings, then `import.run` with the scenario's own `settings` laid over them. Paths are relative to the
## repository and resolved here, so no scenario file carries an absolute path. Afterwards the project is read again,
## as the app reads it after an import. Records the first failure and returns false; true when there is nothing to do.
func _run_imports() -> bool:
	if _scenario.imports.is_empty():
		return true
	var deadline := Time.get_ticks_msec() + int(_scenario.ready_timeout_s * 1000.0)
	while Session.project_id().is_empty():
		if Backend.state() == Backend.State.FAILED or Time.get_ticks_msec() > deadline:
			_errors.append("no project was open when the imports were due")
			return false
		await get_tree().process_frame
	for entry in _scenario.imports:
		var params: Dictionary = entry.duplicate(true)
		if params.has("path"):
			params["path"] = _resolve_path(String(params["path"]))
		if params.has("paths"):
			var resolved: Array = []
			for path: Variant in params["paths"]:
				resolved.append(_resolve_path(String(path)))
			params["paths"] = resolved
		var inspect_params := {}
		for key: String in ["path", "paths", "kind", "format_id", "provider", "query"]:
			if params.has(key):
				inspect_params[key] = params[key]
		var inspected := await Backend.request("import.inspect", inspect_params, ImportDialog.request_timeout_sec("import.inspect"))
		if inspected.type != "res":
			var reason := String(inspected.error.get("message", ""))
			if _skip_unreachable_provider(params, reason, String(inspected.error.get("code", ""))):
				continue
			_errors.append("import of %s: import.inspect failed: %s" % [str(params.get("kind", "?")), reason])
			return false
		var settings: Dictionary = (inspected.result.get("suggested_settings", {}) as Dictionary).duplicate()
		settings.merge(params.get("settings", {}), true)
		params["settings"] = settings
		var ran := await Backend.request("import.run", params, ImportDialog.request_timeout_sec("import.run"))
		if ran.type != "res":
			var run_reason := String(ran.error.get("message", ""))
			if _skip_unreachable_provider(params, run_reason, String(ran.error.get("code", ""))):
				continue
			_errors.append("import of %s: import.run failed: %s" % [str(params.get("kind", "?")), run_reason])
			return false
		print("capture: imported %s as %s" % [str(params.get("kind", "?")), str(ran.result.get("name", ""))])
	var got := await Backend.request("project.get", {})
	if got.type == "res":
		Session.set_project_info(got.result)
	return true


## T-135 Part C: a provider import that failed because its service or the network could not be reached is a skip, not
## an error, so the remaining imports and shots still run. True when the failure was such a skip.
func _skip_unreachable_provider(params: Dictionary, reason: String, code: String) -> bool:
	if not params.has("provider") or not Scenario.is_unreachable(reason, code):
		return false
	_skip("%s could not be reached: %s" % [str(params["provider"]), reason])
	return true


## Prints a skip and records it for the manifest. A skip is not an error: the exit code stays 0.
func _skip(reason: String) -> void:
	print("skipped: %s" % reason)
	_skipped.append(reason)


## A repository-relative path made absolute under the repository root; an absolute path stays as it is.
func _resolve_path(path: String) -> String:
	if path.is_absolute_path():
		return path
	return _repository_root().path_join(path).simplify_path()


## True once `scene_ready` has fired; records why not otherwise (timeout, or the backend gave up).
func _wait_for_scene() -> bool:
	var deadline_msec := Time.get_ticks_msec() + int(_scenario.ready_timeout_s * 1000.0)
	while not _scene_is_ready:
		if Backend.state() == Backend.State.FAILED:
			_errors.append("the backend failed to start, so the scene never became ready")
			return false
		if Time.get_ticks_msec() > deadline_msec:
			_errors.append("the scene was not ready within ready_timeout_s (%.0f s)" % _scenario.ready_timeout_s)
			return false
		await get_tree().process_frame
	return true


func _capture_shot(shot: Dictionary, timeline: TimelineState, playback: PlaybackController) -> void:
	var camera_manager: CameraManager = _main.camera_manager()
	await _pose_ui(shot, String(shot["name"]))
	if shot.has("time_s"):
		timeline.pause()
		timeline.seek(shot["time_s"])
		await _frames(2)
	if shot.has("focus_station_m"):
		camera_manager.focus_station(shot["focus_station_m"])
	_activate_afresh(camera_manager, _mode_of(shot["camera"]))
	if shot.has("orbit_zoom_notches") or shot.has("orbit_drag_px"):
		await _drive_orbit(shot.get("orbit_zoom_notches", 0), shot.get("orbit_drag_px", Vector2.ZERO))
	await _frames(shot["settle_frames"])
	await _wait_for_terrain(shot["name"])
	if shot.get("bottom_tab", "timeline") == "profile":
		await _wait_for_profile(String(shot["name"]))
	await RenderingServer.frame_post_draw

	var entry := {
		"name": shot["name"],
		"file": "%s.png" % shot["name"],
		"camera": shot["camera"],
		"time_s": timeline.time() if timeline != null else null,
		"lead_station_m": playback.lead_station() if playback != null else null,
		"ui": shot["ui"],
		"source_boundaries": _shell.source_boundaries(),
		"attribution": _shell.attribution_text(),
	}
	if shot.get("bottom_tab", "timeline") == "profile":
		entry["bottom_tab"] = "profile"
		entry["profile"] = _shell.profile_panel().summary()
	entry["memory"] = _memory_stats()
	var image := get_viewport().get_texture().get_image()
	var saved: int = ERR_CANT_CREATE if image == null else image.save_png(_out_dir.path_join(entry["file"]))
	if saved != OK:
		_errors.append("shot '%s': could not save %s (%s)" % [shot["name"], entry["file"], error_string(saved)])
		_manifest_shots.append(entry)
		return

	var measured := image
	if shot["ui"] == "visible":
		var view := _view_region(image)
		measured = _without_chrome(image, view)
		entry["preset"] = shot["preset"]
		entry["panel_opacity"] = _shell.panel_opacity()
		entry["view_rect"] = [view.position.x, view.position.y, view.size.x, view.size.y]
		if shot.has("dialog"):
			entry["dialog"] = shot["dialog"]
		if shot.has("overlay_text"):
			entry["overlay_text"] = shot["overlay_text"]
		if shot["preset"] == "sandbox":
			entry["card_luma"] = _card_luma(image)
	var stats := luma_stats(measured)
	var fraction := scene_fraction(measured, _background, Scenario.BACKGROUND_TOLERANCE)
	entry["luma_mean"] = stats.x
	entry["luma_std"] = stats.y
	entry["scene_fraction"] = fraction
	entry["image_size"] = [image.get_width(), image.get_height()]
	if shot["camera"] == "orbit":
		var orbit := camera_manager.camera_for(CameraManager.Mode.ORBIT).get_parent() as OrbitCamera
		entry["orbit_pitch"] = orbit.pitch()
		entry["orbit_distance_m"] = orbit.distance()
		# The camera looks at the focus, so it is above it when its view direction points downward.
		entry["camera_above_focus"] = -orbit.camera().global_transform.basis.z.y < 0.0
	_manifest_shots.append(entry)
	var problem := Scenario.frame_problem(stats.x, stats.y, fraction)
	if not problem.is_empty():
		_errors.append("shot '%s': %s" % [shot["name"], problem])
	if shot.has("scene_fraction_max"):
		entry["scene_fraction_max"] = shot["scene_fraction_max"]
		var too_full := Scenario.scene_fraction_problem(fraction, float(shot["scene_fraction_max"]))
		if not too_full.is_empty():
			_errors.append("shot '%s': %s" % [shot["name"], too_full])
	print(
		"capture: %s  scene_fraction=%.4f luma_mean=%.3f luma_std=%.3f"
		% [entry["file"], fraction, stats.x, stats.y]
	)


func _record_perf(block: Dictionary, timeline: TimelineState) -> void:
	if block["baseline"]:
		await _pose_ui({"ui": "hidden"})
		_manifest_perf_baseline = await _measure_perf(block, timeline, "perf baseline (ui hidden)")
	await _pose_ui(block)
	# T-137 B: a perf block on the Profile tab is timed once the panel has its profile, so the frames show the drawn profile.
	var profile_tab: bool = block.get("bottom_tab", UiShell.BOTTOM_TAB_TIMELINE) == UiShell.BOTTOM_TAB_PROFILE
	if profile_tab:
		await _wait_for_profile("perf")
	_manifest_perf = await _measure_perf(block, timeline, "perf")
	if block["ui"] == "visible":
		_manifest_perf["ui"] = "visible"
		_manifest_perf["preset"] = block["preset"]
		_manifest_perf["panel_opacity"] = _shell.panel_opacity()
	if profile_tab:
		_manifest_perf["bottom_tab"] = "profile"
		_manifest_perf["profile_window_m"] = _shell.profile_panel().window_width_m()


## T-135 Part B: a shot of a scene that imports terrain waits until the terrain field has settled, every selected tile
## attached and draped with the current imagery. Records a failure after [constant TERRAIN_SETTLE_TIMEOUT_S].
func _wait_for_terrain(shot_name: String) -> void:
	if _scenario.imports.is_empty():
		return
	var field := _terrain_field()
	if field == null:
		_errors.append("shot '%s': the scene has no terrain field to wait for" % shot_name)
		return
	var deadline := Time.get_ticks_msec() + int(TERRAIN_SETTLE_TIMEOUT_S * 1000.0)
	while not field.settled():
		if Time.get_ticks_msec() > deadline:
			_errors.append("shot '%s': the terrain did not settle within %.0f s" % [shot_name, TERRAIN_SETTLE_TIMEOUT_S])
			return
		await get_tree().process_frame
	await _frames(3)


## T-137 B: a shot of the Profile tab waits until the panel has its profile, or has failed to get one (a failure is recorded). At
## most the terrain's settle timeout.
func _wait_for_profile(shot_name: String) -> void:
	var panel := _shell.profile_panel()
	var deadline := Time.get_ticks_msec() + int(TERRAIN_SETTLE_TIMEOUT_S * 1000.0)
	while not panel.settled():
		if Time.get_ticks_msec() > deadline:
			_errors.append("shot '%s': the ground profile did not arrive within %.0f s" % [shot_name, TERRAIN_SETTLE_TIMEOUT_S])
			return
		await get_tree().process_frame
	if panel.state() == ProfilePanel.State.FAILED:
		_errors.append("shot '%s': the ground profile could not be computed: %s" % [shot_name, panel.error_text()])
	await _frames(3)


func _terrain_field() -> TerrainField:
	for node: Node in _main.find_children("*", "Node3D", true, false):
		if node is TerrainField:
			return node as TerrainField
	return null


## T-135 Part C (F48): the video memory in use and, per level, the attached terrain tiles and those with imagery drape.
## Read through the field's public accessors only; the counts are of attached tiles, shown or not.
func _memory_stats() -> Dictionary:
	var tiles := {}
	var draped := {}
	var field := _terrain_field()
	if field != null:
		for key: Vector3i in field.attached_keys():
			var level := str(key.x)
			tiles[level] = int(tiles.get(level, 0)) + 1
			if field.tile_at(key).has_imagery():
				draped[level] = int(draped.get(level, 0)) + 1
	return {
		"texture_mem_bytes": RenderingServer.get_rendering_info(RenderingServer.RENDERING_INFO_TEXTURE_MEM_USED),
		"video_mem_bytes": RenderingServer.get_rendering_info(RenderingServer.RENDERING_INFO_VIDEO_MEM_USED),
		"tiles_per_level": tiles,
		"draped_per_level": draped,
	}


## Plays the perf block once and returns its frame-time statistics.
func _measure_perf(block: Dictionary, timeline: TimelineState, label: String) -> Dictionary:
	var camera_manager: CameraManager = _main.camera_manager()
	camera_manager.set_mode(_mode_of(block["camera"]))
	timeline.pause()
	timeline.seek(block["time_s"])
	timeline.set_rate(block["rate"])
	await _frames(30)
	var field := _terrain_field()
	var builds_before := int(field.build_stats()["count"]) if field != null else 0
	var drapes_before := field.imagery_drapes() if field != null else 0

	var vsync_before := DisplayServer.window_get_vsync_mode()
	DisplayServer.window_set_vsync_mode(DisplayServer.VSYNC_DISABLED)
	await _frames(10)

	_frame_ms.clear()
	_last_frame_usec = 0
	_recording_frames = true
	timeline.play()
	var end_msec := Time.get_ticks_msec() + int(float(block["duration_s"]) * 1000.0)
	while Time.get_ticks_msec() < end_msec:
		await get_tree().process_frame
	_recording_frames = false
	timeline.pause()
	DisplayServer.window_set_vsync_mode(vsync_before)

	var stats := frame_stats(_frame_ms)
	# T-135 Part B: the terrain tiles the field built while the frames were timed, so a figure says whether tiles streamed.
	stats["terrain_builds"] = (int(field.build_stats()["count"]) - builds_before) if field != null else 0
	stats["imagery_drapes"] = (field.imagery_drapes() - drapes_before) if field != null else 0
	print(
		"capture: %s %d frames  p50=%.2f p95=%.2f p99=%.2f max=%.2f ms  terrain builds %d  imagery drapes %d"
		% [label, stats["frames"], stats["p50_ms"], stats["p95_ms"], stats["p99_ms"], stats["max_ms"], stats["terrain_builds"], stats["imagery_drapes"]]
	)
	if int(stats["frames"]) == 0:
		_errors.append("%s recorded no frames" % label)
	return stats


## Poses the shell for a shot or perf block: `ui` "hidden" hides the chrome; "visible" applies `preset` and
## `panel_opacity` (the preset's own default when absent) and opens or closes the named `dialog`. The terrain's
## source boundaries are set for every block: off unless its `source_boundaries` is true.
func _pose_ui(block: Dictionary, shot_name := "") -> void:
	_shell.close_dialogs()
	_shell.show_capture_text("")
	_shell.set_source_boundaries(bool(block.get("source_boundaries", false)))
	# T-137 B: every block states its bottom tab, so the Timeline is selected unless the block asks for the Profile.
	_shell.select_bottom_tab(StringName(block.get("bottom_tab", UiShell.BOTTOM_TAB_TIMELINE)))
	if block["ui"] != "visible":
		_shell.set_chrome_visible(false)
		await _frames(2)
		return
	var preset_id := StringName(block["preset"])
	_shell.apply_preset(preset_id, block.get("panel_opacity", UiPreset.builtin(preset_id).panel_opacity))
	_shell.set_chrome_visible(true)
	if block.has("dialog"):
		var offset := Vector2i(block.get("dialog_offset_px", Vector2.ZERO))
		if not _shell.show_dialog(StringName(block["dialog"]), offset):
			_errors.append("unknown dialog '%s'" % block["dialog"])
	if block.has("import_dialog"):
		await _drive_import_dialog(shot_name, block["import_dialog"])
	_shell.show_capture_text(block.get("overlay_text", ""))
	await _frames(3)


## T-136 Part C2: poses the import dialog of a shot the way a user reaches it. The online step only lists the sources. The
## other steps choose the files, press Next (and wait for the inspection), and for the result press Import (and wait for the
## import). `settings` sets the horizontal CRS and the clip, and `crs_query` types a search into the CRS field and opens its
## list. `provider` chooses an online source on the online step. Only the dialog's public seams are used; each wait ends when the dialog is no longer busy, or at the request's own
## timeout.
func _drive_import_dialog(shot_name: String, block: Dictionary) -> void:
	var dialog := _shell.import_dialog()
	if String(block["step"]) == "online":
		dialog.show_online_sources()
		await _wait_for_providers(dialog)
		if block.get("provider") != null:
			var provider := String(block["provider"])
			dialog.choose_provider(provider)
			if dialog.provider_id() != provider:
				_errors.append("shot '%s': the online source '%s' is not listed" % [shot_name, provider])
		await _frames(2)
		if dialog.provider_count() == 0:
			_errors.append("shot '%s': the online source list is empty" % shot_name)
		return
	var files := PackedStringArray()
	for path: Variant in block["files"]:
		files.append(String(path))
	dialog.set_files(files)
	await _advance_dialog(shot_name, dialog, ImportDialog.STEP_INSPECT, "import.inspect")
	if String(block["step"]) == "report":
		if not dialog.error_text().is_empty():
			_errors.append("shot '%s': the inspection failed: %s" % [shot_name, dialog.error_text()])
		return
	await _advance_dialog(shot_name, dialog, ImportDialog.STEP_SETTINGS, "import.inspect")
	var settings: Dictionary = block.get("settings", {})
	if settings.has("horizontal_crs"):
		dialog.horizontal_picker().set_code(String(settings["horizontal_crs"]))
	if settings.has("clip"):
		dialog.choose_clip(ImportDialog.CLIP_NONE)
	if block.get("crs_query") != null:
		var field := dialog.horizontal_picker().line_edit()
		field.text = String(block["crs_query"])
		field.text_changed.emit(field.text)
		await _wait_for_crs_results(shot_name, dialog.horizontal_picker())
		dialog.horizontal_picker().results_choice().show_popup()
		await _frames(2)
	if String(block["step"]) == "result":
		await _advance_dialog(shot_name, dialog, ImportDialog.STEP_RESULT, "import.run")


## Presses the dialog's primary button once and waits while it is busy, at most the timeout of `method`. Records a problem
## when the dialog is not on `expected_step` afterwards.
func _advance_dialog(shot_name: String, dialog: ImportDialog, expected_step: int, method: String) -> void:
	dialog.go_next()
	var deadline := Time.get_ticks_msec() + int(ImportDialog.request_timeout_sec(method) * 1000.0)
	while dialog.is_busy() and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	if dialog.is_busy():
		_errors.append("shot '%s': the import dialog was still busy after %s's timeout" % [shot_name, method])
	await _frames(2)
	if dialog.step() != expected_step:
		_errors.append("shot '%s': the import dialog is on step %d, expected %d" % [shot_name, dialog.step(), expected_step])


## Waits until the dialog has listed the online sources: it asks the backend for them as it opens, so a choice made before the
## answer would be dropped. Gives up at the request timeout; an empty list is then recorded by the driver.
func _wait_for_providers(dialog: ImportDialog) -> void:
	var deadline := Time.get_ticks_msec() + int(Backend.REQUEST_TIMEOUT_SEC * 1000.0)
	while dialog.provider_count() == 0 and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame


## Waits for the CRS search of a typed query to list its results (the picker debounces the typing first).
func _wait_for_crs_results(shot_name: String, picker: CrsPicker) -> void:
	var deadline := Time.get_ticks_msec() + int(ImportDialog.request_timeout_sec("crs.search") * 1000.0)
	while picker.result_codes().is_empty() and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	if picker.result_codes().is_empty():
		_errors.append("shot '%s': the CRS search listed no result" % shot_name)


## The 3D view area of `image` with every chrome rectangle (cards, bars, open dialogs, overlay text) painted in
## the background colour, so the statistics see only the 3D render: an empty scene cannot pass because panels
## happen to differ from the background.
func _without_chrome(image: Image, view: Rect2i) -> Image:
	var region := image.get_region(view)
	var clear := Color(_background.r, _background.g, _background.b, 1.0)
	for rect in _shell.chrome_rects():
		var local := Rect2i(Vector2i(rect.position) - view.position, Vector2i(rect.size.ceil()) + Vector2i(1, 1))
		region.fill_rect(local.intersection(Rect2i(Vector2i.ZERO, region.get_size())), clear)
	return region


## Mean luma (0..1) of a strip of the Layers card (where the track passes behind it) as captured, i.e. the scene seen through the card. A
## lower opacity must make it brighter, which the manifest of the opacity shots shows.
func _card_luma(image: Image) -> float:
	var card := _shell.panel(UiShell.PANEL_LAYERS).get_global_rect()
	var region := image.get_region(Rect2i(Vector2i(card.position) + Vector2i(40, 540), Vector2i(220, 200)).intersection(Rect2i(Vector2i.ZERO, image.get_size())))
	return luma_stats(region).x


## The shell's 3D view area in image pixels, clipped to the image.
func _view_region(image: Image) -> Rect2i:
	var region := Rect2i(_shell.view_rect().position, _shell.view_rect().size)
	return region.intersection(Rect2i(Vector2i.ZERO, image.get_size()))


## Makes `mode` the active camera and runs its `activated()` snap even when it was already active.
func _activate_afresh(camera_manager: CameraManager, mode: CameraManager.Mode) -> void:
	if camera_manager.mode() == mode:
		camera_manager.set_mode(CameraManager.Mode.CAB if mode == CameraManager.Mode.ORBIT else CameraManager.Mode.ORBIT)
	camera_manager.set_mode(mode)


## Wheel notches (positive zooms out) and one left-button drag, sent through `Input` so they take the same
## route as a user's mouse: `CameraManager._unhandled_input` -> the orbit rig's `handle_input`.
func _drive_orbit(zoom_notches: int, drag_px: Vector2) -> void:
	var centre := _shell.view_rect().get_center()
	var wheel := MOUSE_BUTTON_WHEEL_DOWN if zoom_notches > 0 else MOUSE_BUTTON_WHEEL_UP
	for i in absi(zoom_notches):
		_send_button(wheel, true, centre)
		_send_button(wheel, false, centre)
	if drag_px != Vector2.ZERO:
		_send_button(MOUSE_BUTTON_LEFT, true, centre)
		var motion := InputEventMouseMotion.new()
		motion.position = centre + drag_px
		motion.global_position = motion.position
		motion.relative = drag_px
		motion.button_mask = MOUSE_BUTTON_MASK_LEFT
		Input.parse_input_event(motion)
		_send_button(MOUSE_BUTTON_LEFT, false, centre + drag_px)
	await _frames(2)


func _send_button(button: MouseButton, pressed: bool, position: Vector2) -> void:
	var event := InputEventMouseButton.new()
	event.button_index = button
	event.pressed = pressed
	event.position = position
	event.global_position = position
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if button == MOUSE_BUTTON_LEFT and pressed else 0
	Input.parse_input_event(event)


func _frames(count: int) -> void:
	for i in count:
		await get_tree().process_frame


func _finish() -> void:
	if _shell != null and is_instance_valid(_shell):
		_shell.close_dialogs()
		_shell.set_chrome_visible(true)
	for problem in _errors:
		printerr("capture: ERROR %s" % problem)
	var manifest := {
		"scenario": _scenario.scenario_name if _scenario != null else "",
		"godot": _godot_version(),
		"adapter": RenderingServer.get_video_adapter_name(),
		"resolution": [_scenario.resolution.x, _scenario.resolution.y] if _scenario != null else [],
		"git_commit": OS.get_environment("COYPU_CAPTURE_COMMIT"),
		"shots": _manifest_shots,
		"perf": _manifest_perf,
		"perf_baseline": _manifest_perf_baseline,
		"skipped": _skipped,
		"errors": _errors,
	}
	var path := _out_dir.path_join("manifest.json")
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		printerr("capture: ERROR cannot write %s (%s)" % [path, error_string(FileAccess.get_open_error())])
		_errors.append("manifest not written")
	else:
		file.store_string(JSON.stringify(manifest, "\t"))
		file.close()
		print("capture: manifest %s" % path)

	var exit_code := 0 if _errors.is_empty() else 1
	Backend.shutdown()
	get_tree().quit(exit_code)


static func _mode_of(camera: String) -> CameraManager.Mode:
	match camera:
		"wayside":
			return CameraManager.Mode.WAYSIDE
		"cab":
			return CameraManager.Mode.CAB
		_:
			return CameraManager.Mode.ORBIT


static func _godot_version() -> String:
	var info := Engine.get_version_info()
	return "%d.%d.%d" % [info["major"], info["minor"], info["patch"]]


static func _default_out_dir(scenario_name: String) -> String:
	var now := Time.get_datetime_dict_from_system()
	var stamp := "%04d%02d%02d-%02d%02d%02d" % [now["year"], now["month"], now["day"], now["hour"], now["minute"], now["second"]]
	return _repository_root().path_join("captures").path_join("%s-%s" % [stamp, scenario_name])


## The repository root: the folder above `client/`, as an absolute path with forward slashes.
static func _repository_root() -> String:
	return ProjectSettings.globalize_path("res://").path_join("..").simplify_path()


## Mean and standard deviation of the Rec. 709 luma of the image's (gamma-encoded) 8-bit values, scaled to
## 0..1, as a [Vector2] (x = mean, y = std). An empty image is (0, 0), which [method Scenario.frame_problem]
## calls blank.
static func luma_stats(image: Image) -> Vector2:
	var rgb := image.duplicate() as Image
	rgb.convert(Image.FORMAT_RGB8)
	var data := rgb.get_data()
	var pixel_count := data.size() / 3
	if pixel_count == 0:
		return Vector2.ZERO
	var sum := 0.0
	var sum_squares := 0.0
	var i := 0
	while i < data.size():
		var luma := (0.2126 * data[i] + 0.7152 * data[i + 1] + 0.0722 * data[i + 2]) / 255.0
		sum += luma
		sum_squares += luma * luma
		i += 3
	var mean := sum / pixel_count
	var variance := maxf(sum_squares / pixel_count - mean * mean, 0.0)
	return Vector2(mean, sqrt(variance))


## Fraction (0..1) of the image's pixels whose colour differs from `background` by more than `tolerance` in
## any channel (colour components 0..1). 0 for an image that is all background, or empty.
static func scene_fraction(image: Image, background: Color, tolerance: float) -> float:
	var rgb := image.duplicate() as Image
	rgb.convert(Image.FORMAT_RGB8)
	var data := rgb.get_data()
	var pixel_count := data.size() / 3
	if pixel_count == 0:
		return 0.0
	var limit := tolerance * 255.0
	var bg_r := background.r * 255.0
	var bg_g := background.g * 255.0
	var bg_b := background.b * 255.0
	var differing := 0
	var i := 0
	while i < data.size():
		if absf(data[i] - bg_r) > limit or absf(data[i + 1] - bg_g) > limit or absf(data[i + 2] - bg_b) > limit:
			differing += 1
		i += 3
	return float(differing) / pixel_count


## Frame-time statistics over `frame_ms` (one entry per rendered frame, milliseconds). Percentiles are
## nearest-rank on the sorted samples. Empty input gives all zeros.
static func frame_stats(frame_ms: PackedFloat64Array) -> Dictionary:
	var stats := {
		"frames": frame_ms.size(), "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0,
		"over_16_7_ms": 0, "over_33_3_ms": 0,
	}
	if frame_ms.is_empty():
		return stats
	var sorted := frame_ms.duplicate()
	sorted.sort()
	var total := 0.0
	for ms in frame_ms:
		total += ms
		if ms > _OVER_60HZ_MS:
			stats["over_16_7_ms"] += 1
		if ms > _OVER_30HZ_MS:
			stats["over_33_3_ms"] += 1
	stats["mean_ms"] = total / frame_ms.size()
	stats["p50_ms"] = _percentile(sorted, 0.50)
	stats["p95_ms"] = _percentile(sorted, 0.95)
	stats["p99_ms"] = _percentile(sorted, 0.99)
	stats["max_ms"] = sorted[sorted.size() - 1]
	return stats


static func _percentile(sorted: PackedFloat64Array, fraction: float) -> float:
	var rank := clampi(ceili(fraction * sorted.size()), 1, sorted.size())
	return sorted[rank - 1]
