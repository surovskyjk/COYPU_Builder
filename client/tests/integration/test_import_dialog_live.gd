extends GdUnitTestSuite
## T-136 Part C2: the import dialog against the live backend, through the real `Backend` autoload. The synthetic samples are
## written by `tools/write_import_samples.py` into a temporary folder outside the repository, and `after()` shuts the backend
## down and removes that folder. The online step stops before an inspect, which would reach the network (Part C2
## clarification 2): the provider flow is covered by the unit tests and by the Kralupy capture.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _TERRAIN_5514 := "sample_terrain_5514.tif"
const _TERRAIN_NOCRS := "sample_terrain_nocrs.tif"
const _NOCRS_NOTICE := "No coordinate system declared — choose one in Settings"
const _SCRIPT_RELATIVE_PATH := "../tools/write_import_samples.py"
const _BACKEND_RELATIVE_PATH := "../backend"

var _temp_dir := ""
var _samples_written := false


func before() -> void:
	_temp_dir = OS.get_temp_dir().path_join("coypu_t136c2_%d" % Time.get_ticks_usec())
	DirAccess.make_dir_recursive_absolute(_temp_dir)
	_samples_written = _write_samples()
	Backend.start()
	var elapsed := 0.0
	while Backend.state() != Backend.State.READY and elapsed < _READY_TIMEOUT_SEC:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	if Backend.state() == Backend.State.READY:
		await Backend.request("project.new", {"crs": "EPSG:5514"})
		# The dialog reads the open project from Session, as the app does after a project change.
		var opened := await Backend.request("project.get", {})
		if opened.type == "res":
			Session.set_project_info(opened.result)


func after() -> void:
	Session.set_project_info({})
	Backend.shutdown()
	if not _temp_dir.is_empty():
		for file in DirAccess.get_files_at(_temp_dir):
			DirAccess.remove_absolute(_temp_dir.path_join(file))
		DirAccess.remove_absolute(_temp_dir)


## Runs the sample writer through the backend's environment (`uv run --directory backend`), so rasterio is there.
func _write_samples() -> bool:
	var uv := UvLocator.find_uv()
	if uv.is_empty():
		return false
	var root := ProjectSettings.globalize_path("res://")
	var script := root.path_join(_SCRIPT_RELATIVE_PATH).simplify_path()
	var backend_dir := root.path_join(_BACKEND_RELATIVE_PATH).simplify_path()
	var output: Array = []
	var code := OS.execute(uv, ["run", "--directory", backend_dir, "python", "-X", "utf8", script, _temp_dir], output, true)
	return code == 0


func _sample(file_name: String) -> String:
	return _temp_dir.path_join(file_name)


func _dialog() -> ImportDialog:
	var dialog := ImportDialog.new()
	add_child(dialog)
	return auto_free(dialog)


## Inspects the chosen files, sets the horizontal CRS, the clip and the name on Settings, and imports: the path a user takes.
func _import_as_5514(dialog: ImportDialog, source_name: String) -> void:
	await dialog.go_next()
	await dialog.go_next()
	dialog.horizontal_picker().set_code("EPSG:5514")
	dialog.choose_clip(ImportDialog.CLIP_NONE)
	dialog.name_field().text = source_name
	await dialog.go_next()


func _source_list() -> Array:
	var reply: IpcEnvelope = await Backend.request("source.list", {})
	if reply.type != "res":
		return []
	return reply.result.get("sources", [])


func _source_id_named(source_name: String) -> String:
	var sources: Array = await _source_list()
	for entry: Variant in sources:
		if entry is Dictionary and String((entry as Dictionary).get("name", "")) == source_name:
			return String((entry as Dictionary)["id"])
	return ""


## The object ids of a source, in the order the source holds them.
func _object_ids(source_id: String) -> Array:
	var reply: IpcEnvelope = await Backend.request("source.get", {"source_id": source_id})
	var ids: Array = []
	if reply.type == "res":
		for entry: Variant in reply.result.get("objects", []):
			ids.append(String((entry as Dictionary)["id"]))
	return ids


## The index of the re-import choice whose label starts with `source_name`, or -1.
func _reimport_index(dialog: ImportDialog, source_name: String) -> int:
	var choice := dialog.reimport_choice()
	for index in choice.item_count:
		if choice.get_item_text(index).begins_with(source_name):
			return index
	return -1


func test_files_inspect_set_the_crs_and_import_a_source() -> void:
	if not _samples_written:
		fail("the sample files could not be written by tools/write_import_samples.py")
		return
	var dialog := _dialog()
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_sample(_TERRAIN_NOCRS)]))

	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	assert_array(Array(dialog.report_texts())).contains([_NOCRS_NOTICE])
	await dialog.go_next()
	assert_bool(dialog.primary_button().disabled).is_true()
	dialog.horizontal_picker().set_code("EPSG:5514")
	dialog.choose_clip(ImportDialog.CLIP_NONE)
	dialog.name_field().text = "C2 without a CRS"
	assert_bool(dialog.primary_button().disabled).is_false()
	var before := (await _source_list()).size()

	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	var sources := await _source_list()
	assert_int(sources.size()).is_equal(before + 1)
	assert_str(await _source_id_named("C2 without a CRS")).is_not_empty()
	assert_array(Array(dialog.result_texts())).contains(["Imported \"C2 without a CRS\""])


func test_a_second_run_as_a_reimport_keeps_the_guids() -> void:
	if not _samples_written:
		fail("the sample files could not be written by tools/write_import_samples.py")
		return
	var first := _dialog()
	first.prepare([])
	first.set_files(PackedStringArray([_sample(_TERRAIN_5514)]))
	await _import_as_5514(first, "C2 terrain")
	assert_int(first.step()).is_equal(ImportDialog.STEP_RESULT)

	var source_id := await _source_id_named("C2 terrain")
	assert_str(source_id).is_not_empty()
	var before_ids := await _object_ids(source_id)
	assert_int(before_ids.size()).is_greater(0)
	var count_before := (await _source_list()).size()

	var second := _dialog()
	second.prepare([])
	second.set_files(PackedStringArray([_sample(_TERRAIN_5514)]))
	await second.go_next()
	var index := _reimport_index(second, "C2 terrain")
	assert_int(index).is_greater(0)
	second.choose_reimport(index)
	await second.go_next()
	await second.go_next()
	assert_int(second.step()).is_equal(ImportDialog.STEP_RESULT)

	var texts := second.result_texts()
	assert_int(texts.find("Unchanged")).is_greater_equal(0)
	assert_str(texts[texts.find("Unchanged") + 1]).is_equal(str(before_ids.size()))
	assert_str(texts[texts.find("Added") + 1]).is_equal("0")
	assert_array(await _object_ids(source_id)).is_equal(before_ids)
	assert_int((await _source_list()).size()).is_equal(count_before)


func test_the_online_step_lists_the_live_providers() -> void:
	var formats: IpcEnvelope = await Backend.request("import.formats", {})
	assert_str(formats.type).is_equal("res")
	var providers: Array = formats.result.get("providers", [])
	assert_int(providers.size()).is_greater(1)
	var dialog := _dialog()
	dialog.prepare([])
	dialog.show_online_sources()

	var first := providers[0] as Dictionary
	var deadline := Time.get_ticks_msec() + int(_READY_TIMEOUT_SEC * 1000.0)
	while dialog.provider_button(String(first["provider_id"])) == null and Time.get_ticks_msec() < deadline:
		await get_tree().process_frame
	for entry: Variant in providers:
		var provider := entry as Dictionary
		assert_object(dialog.provider_button(String(provider["provider_id"]))) \
			.append_failure_message("the online step does not list %s" % provider["provider_id"]).is_not_null()

	var chosen := providers[0] as Dictionary
	dialog.choose_provider(String(chosen["provider_id"]))
	assert_str(dialog.provider_id()).is_equal(String(chosen["provider_id"]))
	assert_str(dialog.kind()).is_equal(String((chosen["kinds"] as Array)[0]))
	assert_dict(dialog.provider_query()).is_equal(chosen["default_query"])
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SOURCE)
