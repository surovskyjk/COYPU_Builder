class_name Scenario
extends RefCounted
## Capture scenario (T-125): pure parser and validator for the JSON file `tools/capture.ps1` hands to
## [CaptureDriver]. No scene tree and no backend, so it is unit-testable (`test_capture_scenario.gd`).
##
## [method parse] never throws and never returns null: a bad file yields a [Scenario] whose [method errors]
## lists every problem found, in human-readable form, so one run reports all of them. Checks that need the
## loaded run (a `time_s` beyond its duration) are [method check_against_run], called by the driver once
## the run is known.

const CAMERAS: Array[String] = ["orbit", "wayside", "cab"]

const DEFAULT_RESOLUTION := Vector2i(1600, 900)
const DEFAULT_READY_TIMEOUT_S := 90.0
const DEFAULT_SETTLE_FRAMES := 30
const DEFAULT_PERF_RATE := 1.0

## Frame checks, applied to the 3D render alone (the driver hides the status overlay while capturing).
## The environment's clear colour has luma of about 0.098, so a luma bound cannot tell an empty scene from a
## real one; [constant SCENE_FRACTION_MIN] does. The limits come from the Kralupy default scenario, whose
## thin-line corridor views are the sparsest legitimate frames (overlay-free, measured over its five shots:
## scene_fraction 0.0032 to 0.2883, luma_std 0.0148 to 0.0704, luma_mean 0.099 to 0.139). Each limit sits at
## 1/6 to 1/3 of the sparsest shot, while an empty frame (scene_fraction 0), a black frame (luma_mean 0) or a
## flat fill (luma_std ~0) cannot pass.
## A pixel is "scene" when some channel differs from the background by more than [constant BACKGROUND_TOLERANCE]
## (0..1; the clear colour renders to within 1/255 of the environment's `background_color`).
const BACKGROUND_TOLERANCE := 0.02
const SCENE_FRACTION_MIN := 0.0005
const LUMA_STD_MIN := 0.005
const LUMA_MEAN_MIN := 0.05
const LUMA_MEAN_MAX := 0.95

const _TOP_KEYS: Array[String] = ["project", "resolution", "ready_timeout_s", "shots", "perf"]
const _SHOT_KEYS: Array[String] = [
	"name", "camera", "time_s", "focus_station_m", "settle_frames", "orbit_zoom_notches", "orbit_drag_px"
]
const _PERF_KEYS: Array[String] = ["camera", "time_s", "rate", "duration_s"]

## Basename of the scenario file; set by the caller (the text itself carries no name).
var scenario_name := ""
var project := ""
var resolution := DEFAULT_RESOLUTION
var ready_timeout_s := DEFAULT_READY_TIMEOUT_S
## Normalised shot dictionaries: `name`, `camera`, `settle_frames` always present; `time_s`,
## `focus_station_m`, `orbit_zoom_notches` (int) and `orbit_drag_px` ([Vector2]) only when the file gave them.
var shots: Array[Dictionary] = []
## Empty when the file has no `perf` block; otherwise `camera`, `time_s`, `rate`, `duration_s`.
var perf: Dictionary = {}

var _errors := PackedStringArray()


static func parse(text: String) -> Scenario:
	var scenario := Scenario.new()
	var json := JSON.new()
	if json.parse(text) != OK:
		scenario._errors.append("not valid JSON: %s (line %d)" % [json.get_error_message(), json.get_error_line()])
		return scenario
	if not (json.data is Dictionary):
		scenario._errors.append("the top level must be a JSON object")
		return scenario
	scenario._read(json.data as Dictionary)
	return scenario


func is_valid() -> bool:
	return _errors.is_empty()


func errors() -> PackedStringArray:
	return _errors


func has_perf() -> bool:
	return not perf.is_empty()


## Checks that depend on what the project turned out to contain. Returns the new errors (also appended to
## [method errors]): any `time_s` or the cab camera without a run, and any `time_s` beyond the run's duration.
func check_against_run(has_run: bool, duration_s: float) -> PackedStringArray:
	var found := PackedStringArray()
	for shot in shots:
		var label := "shot '%s'" % shot["name"]
		found.append_array(_run_problems(label, shot, has_run, duration_s))
	if has_perf():
		found.append_array(_run_problems("perf", perf, has_run, duration_s))
		if not has_run:
			found.append("perf needs a run to play")
		else:
			var end_s: float = perf["time_s"] + perf["duration_s"] * perf["rate"]
			if end_s > duration_s:
				found.append(
					"perf: playing %.1f s at rate %s from %.1f s reaches %.1f s, beyond the run's duration %.3f"
					% [perf["duration_s"], str(perf["rate"]), perf["time_s"], end_s, duration_s]
				)
	_errors.append_array(found)
	return found


## Empty when the image statistics look like a real scene, otherwise why they do not. `mean` and `std` are
## of the luma in 0..1; `scene_fraction` is the share of pixels that differ from the background.
static func frame_problem(mean: float, std: float, scene_fraction: float) -> String:
	if is_nan(mean) or is_nan(std) or is_nan(scene_fraction):
		return "frame statistics are NaN"
	if scene_fraction < SCENE_FRACTION_MIN:
		return "empty scene (scene_fraction %.5f < %.4f)" % [scene_fraction, SCENE_FRACTION_MIN]
	if mean < LUMA_MEAN_MIN:
		return "nearly black (luma_mean %.4f < %.2f)" % [mean, LUMA_MEAN_MIN]
	if mean > LUMA_MEAN_MAX:
		return "overexposed (luma_mean %.4f > %.2f)" % [mean, LUMA_MEAN_MAX]
	if std <= LUMA_STD_MIN:
		return "flat frame (luma_std %.4f <= %.3f)" % [std, LUMA_STD_MIN]
	return ""


func _run_problems(label: String, entry: Dictionary, has_run: bool, duration_s: float) -> PackedStringArray:
	var found := PackedStringArray()
	if entry.has("time_s"):
		var t: float = entry["time_s"]
		if not has_run:
			found.append("%s: time_s needs a run, but the project has none" % label)
		elif t > duration_s:
			found.append("%s: time_s %.3f is beyond the run's duration %.3f" % [label, t, duration_s])
	if entry.get("camera", "") == "cab" and not has_run:
		found.append("%s: the cab camera needs a run, but the project has none" % label)
	return found


func _read(root: Dictionary) -> void:
	_reject_unknown_keys("scenario", root, _TOP_KEYS)

	if not root.has("project"):
		_errors.append("project is required")
	elif not (root["project"] is String) or (root["project"] as String).strip_edges().is_empty():
		_errors.append("project must be a non-empty string")
	else:
		project = root["project"]

	if root.has("resolution"):
		_read_resolution(root["resolution"])

	if root.has("ready_timeout_s"):
		var timeout: Variant = _number("ready_timeout_s", root["ready_timeout_s"])
		if timeout != null:
			if timeout <= 0.0:
				_errors.append("ready_timeout_s must be positive, got %s" % str(root["ready_timeout_s"]))
			else:
				ready_timeout_s = timeout

	if not root.has("shots"):
		_errors.append("shots is required")
	elif not (root["shots"] is Array):
		_errors.append("shots must be an array")
	elif (root["shots"] as Array).is_empty():
		_errors.append("shots must list at least one shot")
	else:
		_read_shots(root["shots"] as Array)

	if root.has("perf"):
		if root["perf"] is Dictionary:
			_read_perf(root["perf"] as Dictionary)
		else:
			_errors.append("perf must be an object")


func _read_resolution(value: Variant) -> void:
	if not (value is Array) or (value as Array).size() != 2:
		_errors.append("resolution must be [width, height]")
		return
	var width: Variant = _integer("resolution width", value[0])
	var height: Variant = _integer("resolution height", value[1])
	if width == null or height == null:
		return
	if width <= 0 or height <= 0:
		_errors.append("resolution must be positive, got [%d, %d]" % [width, height])
		return
	resolution = Vector2i(width, height)


func _read_shots(raw_shots: Array) -> void:
	var seen := {}
	var name_pattern := RegEx.create_from_string("^[a-z0-9_]+$")
	for i in raw_shots.size():
		var label := "shot %d" % i
		if not (raw_shots[i] is Dictionary):
			_errors.append("%s must be an object" % label)
			continue
		var raw: Dictionary = raw_shots[i]
		_reject_unknown_keys(label, raw, _SHOT_KEYS)

		var shot_name := ""
		if not raw.has("name"):
			_errors.append("%s: name is required" % label)
		elif not (raw["name"] is String) or name_pattern.search(raw["name"] as String) == null:
			_errors.append("%s: name must match [a-z0-9_]+, got %s" % [label, JSON.stringify(raw["name"])])
		else:
			shot_name = raw["name"]
			label = "shot '%s'" % shot_name
			if seen.has(shot_name):
				_errors.append("%s: duplicate name" % label)
			seen[shot_name] = true

		var shot := {"name": shot_name, "settle_frames": DEFAULT_SETTLE_FRAMES}
		var camera := _read_camera(label, raw)
		if camera != "":
			shot["camera"] = camera
		if raw.has("time_s"):
			var time_s: Variant = _non_negative(label + ": time_s", raw["time_s"])
			if time_s != null:
				shot["time_s"] = time_s
		if raw.has("focus_station_m"):
			var station: Variant = _non_negative(label + ": focus_station_m", raw["focus_station_m"])
			if station != null:
				shot["focus_station_m"] = station
		if raw.has("orbit_zoom_notches"):
			var notches: Variant = _integer(label + ": orbit_zoom_notches", raw["orbit_zoom_notches"])
			if notches != null:
				shot["orbit_zoom_notches"] = notches
		if raw.has("orbit_drag_px"):
			var drag: Variant = _read_drag(label, raw["orbit_drag_px"])
			if drag != null:
				shot["orbit_drag_px"] = drag
		if (raw.has("orbit_zoom_notches") or raw.has("orbit_drag_px")) and camera != "orbit":
			_errors.append("%s: orbit_zoom_notches and orbit_drag_px need the orbit camera" % label)
		if raw.has("settle_frames"):
			var frames: Variant = _integer(label + ": settle_frames", raw["settle_frames"])
			if frames != null:
				if frames < 0:
					_errors.append("%s: settle_frames must not be negative, got %d" % [label, frames])
				else:
					shot["settle_frames"] = frames
		shots.append(shot)


## `[dx, dy]` in pixels as a [Vector2], or null after recording why it is not one.
func _read_drag(label: String, value: Variant) -> Variant:
	if not (value is Array) or (value as Array).size() != 2:
		_errors.append("%s: orbit_drag_px must be [dx, dy]" % label)
		return null
	var dx: Variant = _number(label + ": orbit_drag_px[0]", value[0])
	var dy: Variant = _number(label + ": orbit_drag_px[1]", value[1])
	if dx == null or dy == null:
		return null
	return Vector2(dx, dy)


func _read_perf(raw: Dictionary) -> void:
	_reject_unknown_keys("perf", raw, _PERF_KEYS)
	var block := {"time_s": 0.0, "rate": DEFAULT_PERF_RATE}
	var camera := _read_camera("perf", raw)
	if camera != "":
		block["camera"] = camera
	if raw.has("time_s"):
		var time_s: Variant = _non_negative("perf: time_s", raw["time_s"])
		if time_s != null:
			block["time_s"] = time_s
	if raw.has("rate"):
		var rate: Variant = _number("perf: rate", raw["rate"])
		if rate != null:
			if rate < TimelineState.MIN_RATE or rate > TimelineState.MAX_RATE:
				_errors.append(
					"perf: rate must be within %s..%s, got %s"
					% [TimelineState.MIN_RATE, TimelineState.MAX_RATE, str(raw["rate"])]
				)
			else:
				block["rate"] = rate
	if not raw.has("duration_s"):
		_errors.append("perf: duration_s is required")
	else:
		var duration: Variant = _number("perf: duration_s", raw["duration_s"])
		if duration != null:
			if duration <= 0.0:
				_errors.append("perf: duration_s must be positive, got %s" % str(raw["duration_s"]))
			else:
				block["duration_s"] = duration
	perf = block


## Returns the camera name, or "" after recording why it is unusable.
func _read_camera(label: String, raw: Dictionary) -> String:
	if not raw.has("camera"):
		_errors.append("%s: camera is required (one of %s)" % [label, ", ".join(CAMERAS)])
		return ""
	if not (raw["camera"] is String) or not CAMERAS.has(raw["camera"]):
		_errors.append(
			"%s: unknown camera %s (expected one of %s)" % [label, JSON.stringify(raw["camera"]), ", ".join(CAMERAS)]
		)
		return ""
	return raw["camera"]


func _reject_unknown_keys(label: String, raw: Dictionary, allowed: Array[String]) -> void:
	for key: Variant in raw.keys():
		if not allowed.has(str(key)):
			_errors.append("%s: unknown key '%s'" % [label, str(key)])


## A finite JSON number as a float, or null after recording why it is not one. Booleans are not numbers.
func _number(label: String, value: Variant) -> Variant:
	if (value is float or value is int) and is_finite(float(value)):
		return float(value)
	_errors.append("%s must be a number, got %s" % [label, JSON.stringify(value)])
	return null


func _non_negative(label: String, value: Variant) -> Variant:
	var number: Variant = _number(label, value)
	if number != null and number < 0.0:
		_errors.append("%s must not be negative, got %s" % [label, str(value)])
		return null
	return number


## A whole-valued JSON number as an int, or null after recording why it is not one.
func _integer(label: String, value: Variant) -> Variant:
	var number: Variant = _number(label, value)
	if number == null:
		return null
	if number != floorf(number):
		_errors.append("%s must be a whole number, got %s" % [label, str(value)])
		return null
	return int(number)
