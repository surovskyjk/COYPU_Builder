class_name UvLocator
extends RefCounted
## Locates the `uv` executable used to spawn the backend for integration tests. Lifted out of the old
## `run_ipc_tests.gd` runner unchanged: `where`/`which` first, then the winget install locations.

static func find_uv() -> String:
	var probe_cmd := "where" if OS.get_name() == "Windows" else "which"
	var output := []
	if OS.execute(probe_cmd, ["uv"], output) == OK:
		for chunk in output:
			for candidate in String(chunk).split("\n"):
				candidate = candidate.strip_edges()
				if candidate != "" and FileAccess.file_exists(candidate):
					return candidate

	var local_app_data := OS.get_environment("LOCALAPPDATA")
	if local_app_data == "":
		return ""

	var links_path := local_app_data.path_join("Microsoft/WinGet/Links/uv.exe")
	if FileAccess.file_exists(links_path):
		return links_path

	var packages_dir := local_app_data.path_join("Microsoft/WinGet/Packages")
	var dir := DirAccess.open(packages_dir)
	if dir == null:
		return ""
	dir.list_dir_begin()
	var entry := dir.get_next()
	while entry != "":
		if dir.current_is_dir() and entry.begins_with("astral-sh.uv_"):
			var candidate := packages_dir.path_join(entry).path_join("uv.exe")
			if FileAccess.file_exists(candidate):
				dir.list_dir_end()
				return candidate
		entry = dir.get_next()
	dir.list_dir_end()
	return ""
