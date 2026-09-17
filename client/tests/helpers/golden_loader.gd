class_name GoldenLoader
extends RefCounted
## Loads `shared/golden/<name>.json` from the client (T-102, consumed by T-103/T-115 and every later
## cross-language task — name and signature are binding). `report/godot/push_error` is enabled in
## `project.godot` so `push_error()` here fails the calling test with a clear message instead of the
## caller silently getting an empty Dictionary and a confusing downstream assertion failure.

static func load_golden(name: String) -> Dictionary:
	var path := ProjectSettings.globalize_path("res://").path_join("../shared/golden").path_join(name + ".json")
	if not FileAccess.file_exists(path):
		push_error("GoldenLoader: missing shared/golden/%s.json (looked at %s)" % [name, path])
		return {}

	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		push_error("GoldenLoader: could not open %s: %s" % [path, error_string(FileAccess.get_open_error())])
		return {}

	var parsed = JSON.parse_string(file.get_as_text())
	if typeof(parsed) != TYPE_DICTIONARY:
		push_error("GoldenLoader: %s does not contain a JSON object" % path)
		return {}
	return parsed
