class_name UserSettings
extends RefCounted
## Per-user client preferences (T-140): the interface style and the floating-panel opacity. Stored in a
## [ConfigFile] at [code]user://settings.cfg[/code]; never project data, never part of a saved project.
##
## A missing, unreadable or hand-damaged file gives the defaults and never an error dialog: every value is
## validated on load and anything that is not what it should be is ignored. An instance built with an empty
## path is memory-only ([method save] is a no-op), which is what the capture driver and the tests use so
## that they never touch the real file.
##
## Opacity is clamped to [constant UiPreset.OPACITY_MIN] .. [constant UiPreset.OPACITY_MAX]. Until the user
## picks one, [method panel_opacity] answers with the current preset's own default.

const DEFAULT_PATH := "user://settings.cfg"
const SECTION := "interface"

signal changed()

var _path := ""
var _preset_id: StringName = UiPreset.DEFAULT_ID
var _opacity := -1.0  # < 0: not chosen


## `path` is the file to read now and to write on [method save]; empty means memory-only.
func _init(path: String = "") -> void:
	_path = path
	if not path.is_empty():
		_load()


## The settings of the current user, read from [constant DEFAULT_PATH].
static func for_user() -> UserSettings:
	return UserSettings.new(DEFAULT_PATH)


func path() -> String:
	return _path


func preset_id() -> StringName:
	return _preset_id


## An unknown id is ignored (the previous value stays).
func set_preset_id(id: StringName) -> void:
	if not UiPreset.is_known(id) or id == _preset_id:
		return
	_preset_id = id
	changed.emit()


## The chosen opacity, or the current preset's default when none was chosen.
func panel_opacity() -> float:
	if _opacity < 0.0:
		return UiPreset.builtin(_preset_id).panel_opacity
	return _opacity


func has_panel_opacity() -> bool:
	return _opacity >= 0.0


func set_panel_opacity(x: float) -> void:
	var clamped := UiPreset.clamp_opacity(x)
	if clamped == _opacity:
		return
	_opacity = clamped
	changed.emit()


## Writes the file. False when memory-only or when the write fails (the caller may carry on: a preference
## that cannot be stored is not worth interrupting the user for).
func save() -> bool:
	if _path.is_empty():
		return false
	var config := ConfigFile.new()
	config.set_value(SECTION, "preset", String(_preset_id))
	if _opacity >= 0.0:
		config.set_value(SECTION, "panel_opacity", _opacity)
	var err := config.save(_path)
	if err != OK:
		push_warning("UserSettings: could not write %s (%s)" % [_path, error_string(err)])
	return err == OK


## Reads the file by hand instead of through [ConfigFile.load]: a damaged file makes ConfigFile print an engine
## error, and the contract here is that it never does anything visible. The file ConfigFile writes is plain
## `key = value` lines under `[interface]`; lines that are not that are skipped.
func _load() -> void:
	if not FileAccess.file_exists(_path):
		return
	var in_section := false
	for raw_line in FileAccess.get_file_as_string(_path).split("\n"):
		var line := raw_line.strip_edges()
		if line.begins_with("["):
			in_section = line == "[%s]" % SECTION
			continue
		if not in_section or line.is_empty() or line.begins_with(";"):
			continue
		var equals := line.find("=")
		if equals < 1:
			continue
		var key := line.substr(0, equals).strip_edges()
		var value := line.substr(equals + 1).strip_edges()
		if key == "preset" and value.length() >= 2 and value.begins_with("\"") and value.ends_with("\""):
			var candidate := StringName(value.substr(1, value.length() - 2))
			if UiPreset.is_known(candidate):
				_preset_id = candidate
		elif key == "panel_opacity" and value.is_valid_float():
			var number := value.to_float()
			if is_finite(number):
				_opacity = UiPreset.clamp_opacity(number)
