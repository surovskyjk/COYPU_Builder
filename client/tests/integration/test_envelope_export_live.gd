extends GdUnitTestSuite
## "Export corridor envelope..." (T-140) against the live backend through the real `Backend` autoload: a GeoJSON is
## written to a temporary folder outside the repository, an existing file triggers the overwrite question and is
## replaced only after a yes, and a path inside the repository is refused with a sentence that says so.
##
## `after()` always shuts the backend down and removes the temporary folder.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _XML_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.xml"

var _temp_dir := ""
var _alignments: Array[Dictionary] = []


func before() -> void:
	_temp_dir = OS.get_temp_dir().path_join("coypu_t140_%d" % Time.get_ticks_usec())
	DirAccess.make_dir_recursive_absolute(_temp_dir)
	Backend.start()
	var elapsed := 0.0
	while Backend.state() != Backend.State.READY and elapsed < _READY_TIMEOUT_SEC:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	if Backend.state() == Backend.State.READY:
		await Backend.request("project.new", {})
		var xml := ProjectSettings.globalize_path("res://").path_join(_XML_RELATIVE_PATH).simplify_path()
		var imported := await Backend.request("import.landxml", {"path": xml})
		if imported.type == "res":
			for entry: Variant in imported.result.get("alignments", []):
				_alignments.append(entry)


func after() -> void:
	Backend.shutdown()
	if not _temp_dir.is_empty():
		for file in DirAccess.get_files_at(_temp_dir):
			DirAccess.remove_absolute(_temp_dir.path_join(file))
		DirAccess.remove_absolute(_temp_dir)


func _dialog() -> EnvelopeExportDialog:
	var dialog := EnvelopeExportDialog.new()
	add_child(dialog)
	dialog.prepare(_alignments)
	return auto_free(dialog)


func test_export_writes_a_geojson_outside_the_repository() -> void:
	if _alignments.is_empty():
		fail("the Kralupy alignment could not be imported")
		return
	var dialog := _dialog()
	var out := _temp_dir.path_join("corridor.geojson")
	dialog.path_field().text = out
	var exported_box: Array = [""]
	dialog.exported.connect(func(_result: Dictionary, summary: String) -> void: exported_box[0] = summary)

	var outcome: Dictionary = await dialog.run_export()

	assert_bool(outcome["ok"]).is_true()
	assert_bool(FileAccess.file_exists(out)).is_true()
	var parsed: Variant = JSON.parse_string(FileAccess.get_file_as_string(out))
	assert_bool(parsed is Dictionary).is_true()
	assert_str(exported_box[0]).contains("corridor.geojson")
	assert_str(exported_box[0]).contains("km")
	assert_float(outcome["result"]["area_m2"]).is_greater(0.0)
	assert_float(float(outcome["result"]["vertex_count"])).is_greater(3.0)


func test_existing_file_is_replaced_only_after_confirmation() -> void:
	if _alignments.is_empty():
		fail("the Kralupy alignment could not be imported")
		return
	var dialog := _dialog()
	var out := _temp_dir.path_join("replace_me.geojson")
	dialog.path_field().text = out
	var first: Dictionary = await dialog.run_export()
	assert_bool(first["ok"]).is_true()
	var original := FileAccess.get_file_as_string(out)

	var asked: Array = []
	dialog.buffer_field().value = 600.0
	dialog.overwrite_decider = func(names: PackedStringArray) -> bool:
		asked.append(names)
		return false
	var refused: Dictionary = await dialog.run_export()
	assert_bool(refused["ok"]).is_false()
	assert_bool(refused["cancelled"]).is_true()
	assert_int(asked.size()).is_equal(1)
	assert_str(FileAccess.get_file_as_string(out)).is_equal(original)

	dialog.overwrite_decider = func(names: PackedStringArray) -> bool:
		asked.append(names)
		return true
	var replaced: Dictionary = await dialog.run_export()
	assert_bool(replaced["ok"]).is_true()
	assert_int(asked.size()).is_equal(2)
	assert_str(FileAccess.get_file_as_string(out)).is_not_equal(original)


func test_path_inside_the_repository_is_refused_in_plain_language() -> void:
	if _alignments.is_empty():
		fail("the Kralupy alignment could not be imported")
		return
	var dialog := _dialog()
	var inside := ProjectSettings.globalize_path("res://").path_join("../captures/t140_must_not_exist.geojson").simplify_path()
	dialog.path_field().text = inside
	var outcome: Dictionary = await dialog.run_export()
	assert_bool(outcome["ok"]).is_false()
	assert_str(dialog.error_label().text).contains("outside the project folder")
	assert_bool(FileAccess.file_exists(inside)).is_false()


func test_untouched_station_range_defaults_export_successfully() -> void:
	if _alignments.is_empty():
		fail("the Kralupy alignment could not be imported")
		return
	var dialog := _dialog()
	var out := _temp_dir.path_join("ranged.geojson")
	dialog.path_field().text = out
	dialog.station_check().button_pressed = true
	assert_float(dialog.station_from_field().value).is_equal(float(_alignments[0]["station_start"]))
	assert_float(dialog.station_to_field().value).is_equal(float(_alignments[0]["station_end"]))

	var outcome: Dictionary = await dialog.run_export()

	assert_bool(outcome["ok"]).append_failure_message(str(outcome["message"])).is_true()
	assert_bool(FileAccess.file_exists(out)).is_true()
