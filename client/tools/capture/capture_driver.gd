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

const _OVER_60HZ_MS := 16.7
const _OVER_30HZ_MS := 33.3

var _main: Node
var _scenario: Scenario
var _out_dir := ""
var _scene_is_ready := false
var _overlay: CanvasItem
var _background := Color.BLACK

var _manifest_shots: Array[Dictionary] = []
var _manifest_perf: Variant = null
var _errors: Array[String] = []

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
		await _capture_all()
	_finish()


func _capture_all() -> void:
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

	var environment_node := _main.find_children("*", "WorldEnvironment", false, false)
	if environment_node.is_empty() or (environment_node[0] as WorldEnvironment).environment == null:
		_errors.append("no WorldEnvironment with an environment on the main scene, so no background colour")
		return
	_background = (environment_node[0] as WorldEnvironment).environment.background_color
	_overlay = _main.status_overlay()
	_overlay.visible = false
	await _frames(2)

	for shot in _scenario.shots:
		await _capture_shot(shot, timeline, playback)
	if _scenario.has_perf():
		await _record_perf(_scenario.perf, timeline)


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
	await RenderingServer.frame_post_draw

	var entry := {
		"name": shot["name"],
		"file": "%s.png" % shot["name"],
		"camera": shot["camera"],
		"time_s": timeline.time() if timeline != null else null,
		"lead_station_m": playback.lead_station() if playback != null else null,
	}
	var image := get_viewport().get_texture().get_image()
	var saved: int = ERR_CANT_CREATE if image == null else image.save_png(_out_dir.path_join(entry["file"]))
	if saved != OK:
		_errors.append("shot '%s': could not save %s (%s)" % [shot["name"], entry["file"], error_string(saved)])
		_manifest_shots.append(entry)
		return

	var stats := luma_stats(image)
	var fraction := scene_fraction(image, _background, Scenario.BACKGROUND_TOLERANCE)
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
	print(
		"capture: %s  scene_fraction=%.4f luma_mean=%.3f luma_std=%.3f"
		% [entry["file"], fraction, stats.x, stats.y]
	)


func _record_perf(block: Dictionary, timeline: TimelineState) -> void:
	var camera_manager: CameraManager = _main.camera_manager()
	camera_manager.set_mode(_mode_of(block["camera"]))
	timeline.pause()
	timeline.seek(block["time_s"])
	timeline.set_rate(block["rate"])
	await _frames(30)

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

	_manifest_perf = frame_stats(_frame_ms)
	var stats: Dictionary = _manifest_perf
	print(
		"capture: perf %d frames  p50=%.2f p95=%.2f p99=%.2f max=%.2f ms"
		% [stats["frames"], stats["p50_ms"], stats["p95_ms"], stats["p99_ms"], stats["max_ms"]]
	)
	if int(stats["frames"]) == 0:
		_errors.append("perf recorded no frames")


## Makes `mode` the active camera and runs its `activated()` snap even when it was already active.
func _activate_afresh(camera_manager: CameraManager, mode: CameraManager.Mode) -> void:
	if camera_manager.mode() == mode:
		camera_manager.set_mode(CameraManager.Mode.CAB if mode == CameraManager.Mode.ORBIT else CameraManager.Mode.ORBIT)
	camera_manager.set_mode(mode)


## Wheel notches (positive zooms out) and one left-button drag, sent through `Input` so they take the same
## route as a user's mouse: `CameraManager._unhandled_input` -> the orbit rig's `handle_input`.
func _drive_orbit(zoom_notches: int, drag_px: Vector2) -> void:
	var centre := get_viewport().get_visible_rect().size * 0.5
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
	if _overlay != null and is_instance_valid(_overlay):
		_overlay.visible = true
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
	var repo_root := ProjectSettings.globalize_path("res://").path_join("..").simplify_path()
	return repo_root.path_join("captures").path_join("%s-%s" % [stamp, scenario_name])


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
