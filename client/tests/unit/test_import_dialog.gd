extends GdUnitTestSuite
## T-136 Part B: the five-step import dialog against a fake backend. No backend runs; each request is answered from a
## queue the test fills (the last reply of a queue repeats), and the busy backoff is recorded, not slept.
## Every assertion compares with a literal, never with a widget's own value, and the numbers the wire carries are fed
## in JSON shape (floats). The live half is `integration/test_import_dialog_live.gd` (Part C).

const _FILE_LAS := "/tmp/coypu_test/a.las"
const _FILE_TIF := "/tmp/coypu_test/dem.tif"
const _NODATA_FLOAT32 := -3.4028234663852886e38
## The shortest text that reads back as _NODATA_FLOAT32 in Godot: its 17 digits, as the reader gives them.
const _NODATA_TEXT := "-3.4028234663852890e+38"
const _TIMEOUT_TEXT := "The import is taking longer than expected — it may still finish; check the data sources before importing again"
const _NOT_FOUND_TEXT := "A file could not be found — choose it again"
const _EMPTY_TEXT := "Nothing of this data lies inside the clip — widen the clip or choose none"
const _NO_PROJECT_TEXT := "Open or create a project first — imported data belongs to a project"
const _TITLE_BAR_PX := 36
const _BUFFER_MAX_TEXT := "The buffer must be at most 5000 m"


## A pause or a reply that the test releases with `fired.emit()`.
class Tick extends RefCounted:
	signal fired


## The fake backend: `request` answers from `replies[method]` in order, and `gates[method]` holds a method until the
## test releases it. Every call is recorded in `calls`.
class FakeBackend extends RefCounted:
	var calls: Array = []
	var delays: Array = []
	var replies: Dictionary = {}
	var gates: Dictionary = {}

	func reply(method: String, envelopes: Array) -> void:
		replies[method] = envelopes

	func request(method: String, params: Dictionary) -> IpcEnvelope:
		calls.append({"method": method, "params": params.duplicate(true)})
		if gates.has(method):
			await (gates[method] as Tick).fired
		var queue: Array = replies.get(method, [])
		if queue.is_empty():
			var missing := IpcEnvelope.new()
			missing.type = "err"
			missing.error = {"code": "E_INTERNAL", "message": "no fake reply for %s" % method}
			return missing
		if queue.size() == 1:
			return queue[0] as IpcEnvelope
		return queue.pop_front() as IpcEnvelope

	func record_delay(seconds: float) -> void:
		delays.append(seconds)

	func params_of(method: String) -> Array:
		var found: Array = []
		for call: Dictionary in calls:
			if call["method"] == method:
				found.append(call["params"])
		return found

	func count(method: String) -> int:
		return params_of(method).size()


static func _envelope(type: String, error: Dictionary, result: Dictionary) -> IpcEnvelope:
	var envelope := IpcEnvelope.new()
	envelope.type = type
	envelope.error = error
	envelope.result = result
	return envelope


static func _res(result: Dictionary) -> IpcEnvelope:
	return _envelope("res", {}, result)


static func _err(code: String, message: String = "") -> IpcEnvelope:
	return _envelope("err", {"code": code, "message": message}, {})


static func _formats() -> IpcEnvelope:
	return _res({
		"readers": [
			{"format_id": "las", "kinds": ["terrain"], "suffixes": [".las", ".laz", ".zip"]},
			{"format_id": "geotiff", "kinds": ["terrain", "imagery"], "suffixes": [".tif", ".tiff"]},
		],
		"providers": [],
	})


static func _crs_search() -> IpcEnvelope:
	return _res({"results": [
		{"code": "EPSG:5514", "name": "S-JTSK / Krovak East North", "kind": "projected", "area": null, "deprecated": false},
		{"code": "EPSG:5513", "name": "S-JTSK / Krovak", "kind": "projected", "area": null, "deprecated": false},
	]})


static func _las_report(declared: Variant = "EPSG:5514") -> IpcEnvelope:
	return _res({
		"format_id": "las",
		"kind": "terrain",
		"declared_crs": declared,
		"extent": [-746900.0, -1025200.0, -746500.0, -1025000.0],
		"counts": {"points": 1200, "sample_points": 1000, "sample_class_2": 600, "sample_class_6": 300, "sample_class_9": 100},
		"suggested_settings": {
			"horizontal_crs": declared, "vertical_crs": null, "coordinate_order": "auto", "linear_unit_m": 1.0,
			"clip": null, "point_classes": null, "display_resolution_m": null, "nodata": null,
		},
		"licence_hint": "Open survey data",
		"warnings": ["the CRS record in the header could not be parsed"],
	})


## A GeoTIFF terrain with the given no-data suggestion (null for none) and a display resolution of 2.0 suggested.
static func _geotiff_report(nodata: Variant) -> IpcEnvelope:
	return _res({
		"format_id": "geotiff",
		"kind": "terrain",
		"declared_crs": "EPSG:5514",
		"extent": [-746900.0, -1025200.0, -746500.0, -1025000.0],
		"counts": {"width": 4000, "height": 3000, "bands": 1},
		"suggested_settings": {
			"horizontal_crs": "EPSG:5514", "vertical_crs": null, "coordinate_order": "auto", "linear_unit_m": 1.0,
			"clip": null, "point_classes": null, "display_resolution_m": 2.0, "nodata": nodata,
		},
		"licence_hint": null,
		"warnings": ["resolution: cell size 0.5 x 0.5 (source units)"],
	})


static func _raster_report() -> IpcEnvelope:
	return _res({
		"format_id": "geotiff",
		"kind": "imagery",
		"declared_crs": "EPSG:5514",
		"extent": [-746900.0, -1025200.0, -746500.0, -1025000.0],
		"counts": {"width": 400, "height": 300, "bands": 1},
		"suggested_settings": {
			"horizontal_crs": "EPSG:5514", "vertical_crs": null, "coordinate_order": "auto", "linear_unit_m": 1.0,
			"clip": null, "point_classes": null, "display_resolution_m": 2.0, "nodata": null,
		},
		"licence_hint": null,
		"warnings": ["resolution: cell size 2 x 2 (source units)"],
	})


static func _run_ok(added: int = 3) -> IpcEnvelope:
	return _res({
		"source_id": "src-1", "name": "Kralupy DMR", "effective_crs": "EPSG:5514",
		"transformation": "no transformation needed", "transformation_accuracy_m": null,
		"added": added, "changed": 0, "unchanged": 0, "removed": 0, "warnings": [],
	})


static func _one_alignment(station_start: float, station_end: float) -> Array[Dictionary]:
	var alignments: Array[Dictionary] = [{"alignment_id": "al-1", "name": "Track", "station_start": station_start, "station_end": station_end}]
	return alignments


func before_test() -> void:
	Session.set_project_info({"project_id": "p-test", "crs": "EPSG:5514"})


func after_test() -> void:
	Session.set_project_info({})


func _backend() -> FakeBackend:
	var backend := FakeBackend.new()
	backend.reply("import.formats", [_formats()])
	backend.reply("crs.search", [_crs_search()])
	backend.reply("source.get", [_err("E_NOT_FOUND", "no such source")])
	return backend


func _dialog(backend: FakeBackend) -> ImportDialog:
	var dialog := ImportDialog.new()
	add_child(dialog)
	auto_free(dialog)
	dialog.requester = backend.request
	dialog.wait = backend.record_delay
	return dialog


## Opens the dialog on `files`, inspects them and moves to Settings.
func _to_settings(dialog: ImportDialog, files: PackedStringArray = PackedStringArray([_FILE_LAS])) -> void:
	dialog.prepare([])
	dialog.set_files(files)
	await dialog.go_next()
	await dialog.go_next()


func _frames(count: int) -> void:
	for i in count:
		await get_tree().process_frame


## Types `text` into a number field the way a user does: the text is shown, then the edit is reported.
func _type(field: NumberField, text: String) -> void:
	field.text = text
	field.text_changed.emit(text)


func _press(keycode: Key) -> void:
	var press := InputEventKey.new()
	press.keycode = keycode
	press.physical_keycode = keycode
	press.pressed = true
	get_viewport().push_input(press)
	var release := press.duplicate() as InputEventKey
	release.pressed = false
	get_viewport().push_input(release)


func _assert_disabled_with_reason(dialog: ImportDialog, reason: String) -> void:
	var primary := dialog.primary_button()
	assert_bool(primary.disabled).append_failure_message("the primary button is enabled, expected: %s" % reason).is_true()
	assert_str(dialog.hint_label().text).is_equal(reason)
	assert_str(primary.tooltip_text).is_equal(reason)


## The window lies inside a 1280 x 720 screen, buttons included (the bottom edge of the window is the bottom of the buttons),
## and its 36 px title bar, drawn above the window, is on the screen too.
func _assert_inside_1280_by_720(dialog: ImportDialog, where: String) -> void:
	var right := dialog.position.x + dialog.size.x
	var bottom := dialog.position.y + dialog.size.y
	assert_bool(dialog.position.x >= 0 and dialog.position.y >= _TITLE_BAR_PX and right <= 1280 and bottom <= 720) \
		.append_failure_message("%s: the window is at %s with size %s, outside 1280 x 720 or under its 36 px title bar" % [where, dialog.position, dialog.size]) \
		.is_true()
	# Centred on the screen after the step change (the re-centre), not left where an earlier, smaller page put it.
	var centred := Vector2i((1280 - dialog.size.x) / 2, (720 - dialog.size.y) / 2)
	assert_bool(dialog.position == centred) \
		.append_failure_message("%s: the window is at %s, not centred at %s" % [where, dialog.position, centred]) \
		.is_true()
	# The window is at least as tall as its contents, so nothing is cut off at the bottom.
	assert_bool(dialog.size.y >= dialog.get_contents_minimum_size().y) \
		.append_failure_message("%s: the window (%d px) is shorter than its contents (%d px)" % [where, dialog.size.y, dialog.get_contents_minimum_size().y]) \
		.is_true()


func test_no_crs_report_keeps_import_disabled_until_a_crs_is_chosen() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report(null)])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	assert_array(Array(dialog.report_texts())).append_failure_message("the no-CRS notice is missing") \
		.contains([ImportDialog.NO_CRS_NOTICE])
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	_assert_disabled_with_reason(dialog, "Choose a coordinate system for this data")

	dialog.horizontal_picker().set_code("EPSG:5514")
	assert_bool(dialog.primary_button().disabled).is_false()
	assert_str(dialog.hint_label().text).is_empty()
	await dialog.go_next()
	assert_int(backend.count("import.run")).is_equal(1)


func test_suggested_settings_prefill_every_field() -> void:
	var backend := _backend()
	var report := _las_report("EPSG:5514")
	(report.result["suggested_settings"] as Dictionary).merge({
		"vertical_crs": "EPSG:5705",
		"coordinate_order": "NE",
		"linear_unit_m": 0.3048,
		"clip": {"type": "corridor", "buffer_m": 120.0, "alignment_ids": [], "station_from": null, "station_to": null},
		"point_classes": [2.0, 9.0],
	}, true)
	backend.reply("import.inspect", [report])
	var dialog := _dialog(backend)
	await _to_settings(dialog)

	assert_str(dialog.horizontal_picker().code()).is_equal("EPSG:5514")
	assert_str(dialog.vertical_picker().code()).is_equal("EPSG:5705")
	assert_int(dialog.order_choice().selected).is_equal(2)
	assert_float(dialog.unit_field().value()).is_equal(0.3048)
	assert_int(dialog.clip_choice().selected).is_equal(ImportDialog.CLIP_CORRIDOR)
	assert_float(dialog.buffer_field().value()).is_equal(120.0)
	assert_bool(dialog.station_check().button_pressed).is_false()
	var states := []
	for check in dialog.class_checks():
		states.append([int(check.get_meta(&"class_code")), check.button_pressed])
	assert_array(states).contains_exactly([[2, true], [6, false], [9, true]])
	assert_str(dialog.name_field().text).is_equal("a")
	assert_bool(dialog.resolution_row().visible).is_false()

	var classes: Array = dialog.settings_snapshot()["point_classes"]
	assert_int(typeof(classes[0])).append_failure_message("the class codes are sent as floats").is_equal(TYPE_INT)
	assert_int(typeof(classes[1])).append_failure_message("the class codes are sent as floats").is_equal(TYPE_INT)
	assert_bool(dialog.settings_snapshot() == {
		"horizontal_crs": "EPSG:5514",
		"vertical_crs": "EPSG:5705",
		"coordinate_order": "NE",
		"linear_unit_m": 0.3048,
		"clip": {"type": "corridor", "buffer_m": 120.0, "alignment_ids": [], "station_from": null, "station_to": null},
		"point_classes": [2, 9],
		"display_resolution_m": null,
		"nodata": null,
	}).is_true()


func test_edits_are_sent_verbatim() -> void:
	# A GeoTIFF, corridor with a station range that reaches past the alignment's start, a display resolution and a
	# no-data value that is left alone, then unticked and ticked again.
	var raster_backend := _backend()
	raster_backend.reply("import.inspect", [_geotiff_report(_NODATA_FLOAT32)])
	raster_backend.reply("import.run", [_run_ok()])
	var raster := _dialog(raster_backend)
	raster.prepare(_one_alignment(12345.6789, 18184.9717))
	raster.set_files(PackedStringArray([_FILE_TIF]))
	await raster.go_next()
	await raster.go_next()

	raster.horizontal_picker().set_code("EPSG:5513")
	raster.vertical_picker().set_code("EPSG:5705")
	raster.order_choice().select(2)
	raster.order_choice().item_selected.emit(2)
	_type(raster.unit_field(), "0.3048006096")
	_type(raster.buffer_field(), "120")
	raster.station_check().button_pressed = true
	_type(raster.station_from_field(), "12000.25")
	_type(raster.station_to_field(), "15500.5")
	raster.resolution_check().button_pressed = true
	_type(raster.resolution_field(), "0.125")
	assert_str(raster.nodata_field().text).is_equal(_NODATA_TEXT)
	raster.nodata_check().button_pressed = false
	assert_bool(raster.settings_snapshot()["nodata"] == null).is_true()
	raster.nodata_check().button_pressed = true
	assert_float(raster.settings_snapshot()["nodata"]).is_equal(_NODATA_FLOAT32)
	await raster.go_next()

	var raster_sent := raster_backend.params_of("import.run")[0] as Dictionary
	assert_bool(raster_sent["settings"] == {
		"horizontal_crs": "EPSG:5513",
		"vertical_crs": "EPSG:5705",
		"coordinate_order": "NE",
		"linear_unit_m": 0.3048006096,
		"clip": {"type": "corridor", "buffer_m": 120.0, "alignment_ids": [], "station_from": 12000.25, "station_to": 15500.5},
		"point_classes": null,
		"display_resolution_m": 0.125,
		"nodata": _NODATA_FLOAT32,
	}).append_failure_message("the raster settings sent differ from the settings entered: %s" % str(raster_sent["settings"])).is_true()
	assert_str(String(raster_sent["name"])).is_equal("dem")

	# A LAS: a bounding box of literals, the vertical CRS, the East-North order, one class unticked, and a name.
	var las_backend := _backend()
	las_backend.reply("import.inspect", [_las_report("EPSG:5514")])
	las_backend.reply("import.run", [_run_ok()])
	var las := _dialog(las_backend)
	await _to_settings(las)
	las.vertical_picker().set_code("EPSG:5705")
	las.order_choice().select(1)
	las.order_choice().item_selected.emit(1)
	_type(las.unit_field(), "0.3048006096")
	las.clip_choice().select(ImportDialog.CLIP_BBOX)
	las.clip_choice().item_selected.emit(ImportDialog.CLIP_BBOX)
	_type(las.bbox_fields()[0], "-746900")
	_type(las.bbox_fields()[1], "-1025200")
	_type(las.bbox_fields()[2], "-746500")
	_type(las.bbox_fields()[3], "-1025000")
	las.class_checks()[1].button_pressed = false
	las.name_field().text = "Kralupy DMR"
	await las.go_next()

	var las_sent := las_backend.params_of("import.run")[0] as Dictionary
	assert_bool(las_sent["settings"] == {
		"horizontal_crs": "EPSG:5514",
		"vertical_crs": "EPSG:5705",
		"coordinate_order": "EN",
		"linear_unit_m": 0.3048006096,
		"clip": {"type": "bbox", "min_e": -746900.0, "min_n": -1025200.0, "max_e": -746500.0, "max_n": -1025000.0},
		"point_classes": [2, 9],
		"display_resolution_m": null,
		"nodata": null,
	}).append_failure_message("the LAS settings sent differ from the settings entered: %s" % str(las_sent["settings"])).is_true()
	assert_array(las_sent["paths"]).contains_exactly([_FILE_LAS])
	assert_str(String(las_sent["name"])).is_equal("Kralupy DMR")


func test_busy_retries_with_backoff() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var busy := _err("E_BUSY", "busy")
	backend.reply("import.run", [busy, busy, busy, busy, _run_ok()])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	await dialog.go_next()

	assert_array(backend.delays).contains_exactly([0.5, 1.0, 2.0, 4.0])
	assert_int(backend.count("import.run")).is_equal(5)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)


func test_crs_required_returns_to_settings_and_focuses_the_crs_field() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_err("E_CRS_REQUIRED", "no CRS")])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)
	await dialog.go_next()

	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	assert_str(dialog.error_text()).is_equal("This file declares no coordinate system — choose one")
	assert_bool(dialog.horizontal_picker().line_edit().has_focus()) \
		.append_failure_message("the CRS field did not take the focus").is_true()


func test_file_filters_come_from_import_formats() -> void:
	var backend := _backend()
	var dialog := _dialog(backend)
	dialog.prepare([])
	assert_array(backend.params_of("import.formats")).has_size(1)
	assert_array(Array(dialog.file_filters())).contains_exactly([
		"*.las, *.laz, *.zip, *.tif, *.tiff ; All supported",
		"*.las, *.laz, *.zip ; LAS point cloud",
		"*.tif, *.tiff ; GeoTIFF raster",
	])

	backend.reply("import.formats", [_res({"readers": [
		{"format_id": "raster-image", "kinds": ["imagery"], "suffixes": [".png"]},
	], "providers": []})])
	dialog.prepare([])
	assert_array(Array(dialog.file_filters())).contains_exactly([
		"*.png ; All supported",
		"*.png ; Georeferenced image",
	])


func test_escape_closes_and_enter_presses_the_primary_button() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)

	# Enter in a text field (the name, a LineEdit) presses the enabled Import button.
	dialog.name_field().grab_focus()
	await _frames(1)
	_press(KEY_ENTER)
	await _frames(3)
	assert_int(backend.count("import.run")) \
		.append_failure_message("Enter in a text field did not press the primary button").is_equal(1)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)

	_press(KEY_ESCAPE)
	await _frames(2)
	assert_bool(dialog.visible).append_failure_message("Escape did not close the dialog").is_false()


func test_a_successful_import_emits_sources_changed() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	var got: Array = []
	var handler := func(kind: String) -> void: got.append(kind)
	EventBus.sources_changed.connect(handler)
	await _to_settings(dialog)
	await dialog.go_next()
	EventBus.sources_changed.disconnect(handler)
	assert_array(got).contains_exactly(["terrain"])


func test_rasters_show_resolution_and_nodata_others_do_not() -> void:
	var las_backend := _backend()
	las_backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var las_dialog := _dialog(las_backend)
	await _to_settings(las_dialog)
	assert_bool(las_dialog.resolution_row().visible).is_false()
	assert_bool(las_dialog.nodata_row().visible).is_false()

	var raster_backend := _backend()
	raster_backend.reply("import.inspect", [_geotiff_report(null)])
	var raster_dialog := _dialog(raster_backend)
	await _to_settings(raster_dialog, PackedStringArray([_FILE_TIF]))
	assert_bool(raster_dialog.resolution_row().visible).is_true()
	assert_bool(raster_dialog.nodata_row().visible).is_true()
	assert_bool(raster_dialog.resolution_check().button_pressed).is_true()
	assert_float(raster_dialog.resolution_field().value()).is_equal(2.0)
	assert_bool(raster_dialog.nodata_check().button_pressed).is_false()
	assert_bool(raster_dialog.settings_snapshot()["nodata"] == null).is_true()
	assert_str(raster_dialog.nodata_field().text).is_equal("-9999")


func test_a_reader_with_one_kind_shows_the_kind_as_text() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	assert_bool(dialog.kind_choice().visible).is_false()
	assert_str(dialog.kind_text().text).is_equal("Terrain")
	assert_bool(dialog.kind_text().visible).is_true()


func test_all_classes_checked_keeps_the_suggestion_and_unchecking_sends_the_list() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)

	assert_int(dialog.class_checks().size()).is_equal(3)
	assert_bool(dialog.settings_snapshot()["point_classes"] == null) \
		.append_failure_message("every class ticked should keep the suggestion (null), not a list").is_true()

	dialog.class_checks()[1].button_pressed = false
	assert_bool(dialog.settings_snapshot()["point_classes"] == [2, 9]).is_true()

	dialog.class_checks()[1].button_pressed = true
	assert_bool(dialog.settings_snapshot()["point_classes"] == null).is_true()


func test_point_classes_rule() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	# No list suggested, every class ticked: the suggestion (null).
	assert_bool(dialog.settings_snapshot()["point_classes"] == null).is_true()
	# One unticked: the sorted list of the ticked codes.
	dialog.class_checks()[1].button_pressed = false
	assert_bool(dialog.settings_snapshot()["point_classes"] == [2, 9]).is_true()
	# Ticked again: every class, so null.
	dialog.class_checks()[1].button_pressed = true
	assert_bool(dialog.settings_snapshot()["point_classes"] == null).is_true()

	# A list suggested as JSON floats, with a code the sample lacks (77): unchanged ticks send it verbatim, as ints.
	var report := _las_report("EPSG:5514")
	(report.result["suggested_settings"] as Dictionary)["point_classes"] = [2.0, 9.0, 77.0]
	backend.reply("import.inspect", [report])
	var listed := _dialog(backend)
	await _to_settings(listed)
	var sent: Array = listed.settings_snapshot()["point_classes"]
	assert_array(sent).contains_exactly([2, 9, 77])
	assert_int(typeof(sent[2])).is_equal(TYPE_INT)
	# Every box ticked: null.
	listed.class_checks()[1].button_pressed = true
	assert_bool(listed.settings_snapshot()["point_classes"] == null).is_true()
	# Back to the suggestion's ticks: the suggestion again.
	listed.class_checks()[1].button_pressed = false
	assert_bool(listed.settings_snapshot()["point_classes"] == [2, 9, 77]).is_true()
	# Another choice (class 9 unticked): the sorted list of the ticked codes.
	listed.class_checks()[2].button_pressed = false
	assert_bool(listed.settings_snapshot()["point_classes"] == [2]).is_true()


func test_corridor_is_the_default_only_with_an_alignment() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var plain := _dialog(backend)
	await _to_settings(plain)
	assert_int(plain.clip_choice().selected).is_equal(ImportDialog.CLIP_NONE)
	assert_bool(plain.settings_snapshot()["clip"] == null).is_true()

	var dialog := _dialog(backend)
	dialog.prepare(_one_alignment(0.0, 1000.0))
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	await dialog.go_next()
	assert_int(dialog.clip_choice().selected).is_equal(ImportDialog.CLIP_CORRIDOR)
	assert_float(dialog.buffer_field().value()).is_equal(250.0)
	assert_bool(dialog.settings_snapshot()["clip"] == {
		"type": "corridor", "buffer_m": 250.0, "alignment_ids": [], "station_from": null, "station_to": null,
	}).is_true()
	assert_bool(dialog.station_row().visible).is_true()
	assert_bool(dialog.station_check().button_pressed).is_false()


func test_inspect_and_run_send_paths_and_the_reported_format() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_raster_report()])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	var files := PackedStringArray([_FILE_TIF, "/tmp/coypu_test/dem2.tif"])
	await _to_settings(dialog, files)
	assert_bool(dialog.kind_choice().disabled).append_failure_message("a reader with both kinds keeps Kind locked").is_false()
	dialog.name_field().text = ""
	await dialog.go_next()

	var inspect := backend.params_of("import.inspect")
	assert_bool(inspect[0] == {"paths": [_FILE_TIF, "/tmp/coypu_test/dem2.tif"]}) \
		.append_failure_message("import.inspect sent %s" % str(inspect[0])).is_true()
	var run := backend.params_of("import.run")[0] as Dictionary
	assert_array(run["paths"]).contains_exactly([_FILE_TIF, "/tmp/coypu_test/dem2.tif"])
	assert_str(String(run["format_id"])).is_equal("geotiff")
	assert_str(String(run["kind"])).is_equal("imagery")
	assert_bool(run.has("name")).append_failure_message("an empty name must send no name").is_false()


func test_import_as_sends_the_kind_to_inspect() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_raster_report()])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.import_as_choice().select(2)
	dialog.import_as_choice().item_selected.emit(2)
	dialog.set_files(PackedStringArray([_FILE_TIF]))
	await dialog.go_next()
	var first := backend.params_of("import.inspect")[0] as Dictionary
	assert_bool(first.has("kind")).append_failure_message("the chosen kind was not sent to import.inspect").is_true()
	assert_str(String(first.get("kind", ""))).is_equal("imagery")

	# Detect sends no kind, and changing the choice dropped the report, so Next inspects again.
	dialog.go_back()
	dialog.import_as_choice().select(0)
	dialog.import_as_choice().item_selected.emit(0)
	assert_int(dialog.report_texts().size()).is_equal(0)
	await dialog.go_next()
	var second := backend.params_of("import.inspect")
	assert_int(second.size()).is_equal(2)
	assert_bool((second[1] as Dictionary).has("kind")).append_failure_message("Detect sent a kind").is_false()


func test_inspect_retries_busy() -> void:
	var backend := _backend()
	var busy := _err("E_BUSY", "busy")
	backend.reply("import.inspect", [busy, busy, _las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()

	assert_array(backend.delays).contains_exactly([0.5, 1.0])
	assert_int(backend.count("import.inspect")).is_equal(3)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	assert_array(Array(dialog.report_texts())).contains(["LAS point cloud"])


func test_busy_gives_up_after_eight_retries_with_one_sentence() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_err("E_BUSY", "busy")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	await dialog.go_next()

	assert_array(backend.delays).contains_exactly([0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0, 4.0])
	assert_int(backend.count("import.run")).is_equal(9)
	var sentence := "The backend stayed busy — try again in a moment."
	assert_str(dialog.error_text()).is_equal(sentence)
	assert_bool(sentence.contains("E_")).is_false()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)


func test_a_timed_out_import_says_it_may_still_finish() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_err("E_TIMEOUT", "no reply to 'import.run' within 1800.0s")])
	var dialog := _dialog(backend)
	var got: Array = []
	var handler := func(kind: String) -> void: got.append(kind)
	EventBus.sources_changed.connect(handler)
	await _to_settings(dialog)
	await dialog.go_next()
	EventBus.sources_changed.disconnect(handler)

	assert_str(dialog.error_text()).is_equal(_TIMEOUT_TEXT)
	assert_array(got).append_failure_message("a timed-out import did not say the sources may have changed").contains_exactly(["terrain"])
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)


func test_inspect_clears_an_old_settings_error() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_err("E_EMPTY", "")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	await dialog.go_next()
	assert_str(dialog.error_text()).is_equal(_EMPTY_TEXT)
	assert_bool(dialog.settings_error_shown()).is_true()

	dialog.go_back()
	dialog.go_back()
	dialog.set_files(PackedStringArray(["/tmp/coypu_test/b.las"]))
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	assert_str(dialog.error_text()).is_empty()
	assert_bool(dialog.settings_error_shown()).append_failure_message("the old Settings error is still shown").is_false()


func test_back_and_next_on_the_same_files_keeps_the_settings() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	dialog.name_field().text = "my source"
	dialog.order_choice().select(2)
	dialog.order_choice().item_selected.emit(2)

	dialog.go_back()
	dialog.go_back()
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	assert_int(backend.count("import.inspect")).append_failure_message("the same files were inspected again").is_equal(1)
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	assert_str(dialog.name_field().text).is_equal("my source")
	assert_str(String(dialog.settings_snapshot()["coordinate_order"])).is_equal("NE")
	assert_int(dialog.order_choice().selected).is_equal(2)


func test_no_project_disables_next_with_a_reason() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	Session.set_project_info({})
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	_assert_disabled_with_reason(dialog, _NO_PROJECT_TEXT)
	assert_bool(dialog.hint_label().visible).append_failure_message("the no-project reason is not in the hint line").is_true()
	await dialog.go_next()
	assert_int(backend.count("import.inspect")).append_failure_message("Next inspected without a project").is_equal(0)

	Session.set_project_info({"project_id": "p-test", "crs": "EPSG:5514"})
	assert_bool(dialog.primary_button().disabled).append_failure_message("opening a project did not enable Next").is_false()
	assert_bool(dialog.hint_label().visible).append_failure_message("the hint still shows once the project is open").is_false()


func test_every_step_fits_a_1280_by_720_screen() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report(null)])
	var dialog := _dialog(backend)
	dialog.screen_rect = Rect2i(0, 0, 1280, 720)
	dialog.popup_centered()
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 1")

	await dialog.go_next()
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 2, the LAS report")

	await dialog.go_next()
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 3, the LAS settings")

	dialog.go_back()
	dialog.go_back()
	backend.reply("import.inspect", [_geotiff_report(_NODATA_FLOAT32)])
	dialog.set_files(PackedStringArray([_FILE_TIF]))
	await dialog.go_next()
	await dialog.go_next()
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 3, the GeoTIFF settings")

	dialog.clip_choice().select(ImportDialog.CLIP_BBOX)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_BBOX)
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 3, the GeoTIFF settings with a box")

	dialog.clip_choice().select(ImportDialog.CLIP_NONE)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_NONE)
	backend.reply("import.run", [_run_ok()])
	await dialog.go_next()
	await _frames(2)
	_assert_inside_1280_by_720(dialog, "step 5, the result")


func test_nodata_keeps_a_float32_value_through_a_toggle() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_geotiff_report(_NODATA_FLOAT32)])
	var dialog := _dialog(backend)
	await _to_settings(dialog, PackedStringArray([_FILE_TIF]))
	assert_float(dialog.settings_snapshot()["nodata"]).is_equal(_NODATA_FLOAT32)
	assert_str(dialog.nodata_field().text).is_equal(_NODATA_TEXT)

	dialog.nodata_check().button_pressed = false
	assert_bool(dialog.settings_snapshot()["nodata"] == null).is_true()
	dialog.nodata_check().button_pressed = true
	assert_float(dialog.settings_snapshot()["nodata"]).is_equal(_NODATA_FLOAT32)

	# An edited value is kept by a re-tick; text that is not a number disables Import and keeps the last number.
	_type(dialog.nodata_field(), "-12.5")
	assert_float(dialog.settings_snapshot()["nodata"]).is_equal(-12.5)
	dialog.nodata_check().button_pressed = false
	dialog.nodata_check().button_pressed = true
	assert_float(dialog.settings_snapshot()["nodata"]).is_equal(-12.5)
	_type(dialog.nodata_field(), "abc")
	_assert_disabled_with_reason(dialog, "Enter a number for no data")
	assert_float(dialog.settings_snapshot()["nodata"]).is_equal(-12.5)
	_type(dialog.nodata_field(), "-12.5")
	assert_bool(dialog.primary_button().disabled).is_false()


func test_result_page_shows_every_field() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_res({
		"source_id": "src-1", "name": "Kralupy DMR", "effective_crs": "EPSG:5514",
		"transformation": "EPSG:5514 to WGS 84", "transformation_accuracy_m": 1.5,
		"added": 3, "changed": 2, "unchanged": 7, "removed": 1,
		"warnings": ["3 points had no height and were skipped"],
	})])
	backend.reply("source.get", [_res({"licence": "CC BY 4.0", "attribution": "© ČÚZK"})])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	await dialog.go_next()

	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	assert_array(Array(dialog.result_texts())).contains([
		"Imported \"Kralupy DMR\"",
		"Added", "3",
		"Changed", "2",
		"Unchanged", "7",
		"Removed", "1",
		"Effective CRS", "EPSG:5514",
		"Transformation", "EPSG:5514 to WGS 84, accurate to 1.50 m",
		"Licence", "CC BY 4.0",
		"Attribution", "© ČÚZK",
		"3 points had no height and were skipped",
	])


func test_report_page_shows_every_field() -> void:
	assert_str(ImportDialog.extent_text([-746900.0, -1025200.0, -746500.0, -1025000.0])) \
		.is_equal("E -746900.0 to -746500.0, N -1025200.0 to -1025000.0")
	assert_str(ImportDialog.extent_text([14.2, 50.0, 14.3, 50.1])) \
		.is_equal("E 14.200000 to 14.300000, N 50.000000 to 50.100000")

	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()

	assert_array(Array(dialog.report_texts())).contains([
		"Format", "LAS point cloud",
		"Files", "1 file",
		"Kind", "Terrain",
		"Declared CRS", "EPSG:5514",
		"Extent", "E -746900.0 to -746500.0, N -1025200.0 to -1025000.0",
		"Counts", "1200 points",
		"Licence", "Open survey data",
		"Point class", "Share of the sampled points",
		"2 Ground", "60.0 %",
		"6 Building", "30.0 %",
		"9 Water", "10.0 %",
		"the CRS record in the header could not be parsed",
	])


func test_bbox_prefill_follows_the_horizontal_crs() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	dialog.clip_choice().select(ImportDialog.CLIP_BBOX)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_BBOX)

	# The project's CRS: the report's extent fills the box.
	assert_bool(dialog.settings_snapshot()["clip"] == {
		"type": "bbox", "min_e": -746900.0, "min_n": -1025200.0, "max_e": -746500.0, "max_n": -1025000.0,
	}).is_true()

	# Another CRS: the box starts at zero, since the extent means another place there.
	dialog.horizontal_picker().set_code("EPSG:5513")
	assert_bool(dialog.settings_snapshot()["clip"] == {
		"type": "bbox", "min_e": 0.0, "min_n": 0.0, "max_e": 0.0, "max_n": 0.0,
	}).is_true()

	dialog.horizontal_picker().set_code("EPSG:5514")
	assert_float(dialog.bbox_fields()[0].value()).is_equal(-746900.0)

	# An edited box stays as the user left it.
	_type(dialog.bbox_fields()[0], "-746000")
	dialog.horizontal_picker().set_code("EPSG:5513")
	assert_float(dialog.bbox_fields()[0].value()).is_equal(-746000.0)
	assert_float(dialog.settings_snapshot()["clip"]["min_e"]).is_equal(-746000.0)


func test_not_found_returns_to_source_and_empty_returns_to_settings() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_err("E_NOT_FOUND", "gone")])
	var dialog := _dialog(backend)
	await _to_settings(dialog)
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SOURCE)
	assert_str(dialog.error_text()).is_equal(_NOT_FOUND_TEXT)

	backend.reply("import.run", [_err("E_EMPTY", "")])
	dialog.set_files(PackedStringArray(["/tmp/coypu_test/b.las"]))
	await dialog.go_next()
	await dialog.go_next()
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	assert_str(dialog.error_text()).is_equal(_EMPTY_TEXT)


func test_a_failure_while_hidden_reports_in_the_status_bar() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var gate := Tick.new()
	backend.gates["import.run"] = gate
	backend.reply("import.run", [_err("E_EMPTY", "")])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)

	var messages: Array = []
	var handler := func(text: String) -> void: messages.append(text)
	EventBus.status_message.connect(handler)
	dialog.go_next()
	await _frames(2)
	assert_bool(dialog.is_busy()).is_true()
	dialog.hide()
	gate.fired.emit()
	await _frames(3)
	EventBus.status_message.disconnect(handler)

	assert_array(messages).contains_exactly([_EMPTY_TEXT])
	assert_bool(dialog.is_busy()).is_false()


func test_reopening_while_an_import_runs_shows_step_four() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var gate := Tick.new()
	backend.gates["import.run"] = gate
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)

	dialog.go_next()
	await _frames(2)
	dialog.hide()
	dialog.prepare([])
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_IMPORT)
	assert_bool(dialog.is_busy()).is_true()

	gate.fired.emit()
	await _frames(3)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	assert_bool(dialog.is_busy()).is_false()


func test_closing_during_import_reports_in_the_status_bar() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var gate := Tick.new()
	backend.gates["import.run"] = gate
	backend.reply("import.run", [_run_ok(12)])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)

	var messages: Array = []
	var handler := func(text: String) -> void: messages.append(text)
	EventBus.status_message.connect(handler)
	dialog.go_next()
	await _frames(2)
	assert_bool(dialog.is_busy()).is_true()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_IMPORT)
	dialog.hide()
	gate.fired.emit()
	await _frames(3)
	EventBus.status_message.disconnect(handler)

	assert_array(messages).contains_exactly(["Imported 1 terrain source \"Kralupy DMR\" (12 objects added)"])
	assert_bool(dialog.is_busy()).is_false()


func test_every_disabled_primary_has_a_reason() -> void:
	var backend := _backend()
	var dialog := _dialog(backend)
	dialog.prepare([])
	_assert_disabled_with_reason(dialog, "Choose one or more files first")

	backend.reply("import.inspect", [_err("E_BAD_PARAMS", "")])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	_assert_disabled_with_reason(dialog, "The inspection failed — go back and choose other files")
	dialog.go_back()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SOURCE)
	assert_bool(dialog.primary_button().disabled).is_false()

	backend.reply("import.inspect", [_las_report(null)])
	await dialog.go_next()
	await dialog.go_next()
	_assert_disabled_with_reason(dialog, "Choose a coordinate system for this data")
	dialog.horizontal_picker().set_code("EPSG:5514")

	for check in dialog.class_checks():
		check.button_pressed = false
	_assert_disabled_with_reason(dialog, "Keep at least one point class")
	for check in dialog.class_checks():
		check.button_pressed = true

	dialog.clip_choice().select(ImportDialog.CLIP_BBOX)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_BBOX)
	_type(dialog.bbox_fields()[0], "10")
	_type(dialog.bbox_fields()[2], "5")
	_assert_disabled_with_reason(dialog, "Enter a box whose minimum is below its maximum")
	dialog.clip_choice().select(ImportDialog.CLIP_NONE)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_NONE)
	assert_bool(dialog.primary_button().disabled).is_false()

	# A station range on a single alignment, From not below To.
	var station_backend := _backend()
	station_backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var station := _dialog(station_backend)
	station.prepare(_one_alignment(0.0, 1000.0))
	station.set_files(PackedStringArray([_FILE_LAS]))
	await station.go_next()
	await station.go_next()
	station.clip_choice().select(ImportDialog.CLIP_CORRIDOR)
	station.clip_choice().item_selected.emit(ImportDialog.CLIP_CORRIDOR)
	station.station_check().button_pressed = true
	_type(station.station_from_field(), "800")
	_type(station.station_to_field(), "200")
	_assert_disabled_with_reason(station, "Enter a station range whose From is smaller than To")

	# A no-data value that is not a number.
	var raster_backend := _backend()
	raster_backend.reply("import.inspect", [_geotiff_report(_NODATA_FLOAT32)])
	var raster := _dialog(raster_backend)
	await _to_settings(raster, PackedStringArray([_FILE_TIF]))
	_type(raster.nodata_field(), "x")
	_assert_disabled_with_reason(raster, "Enter a number for no data")

	assert_str(dialog.reason_for_step(ImportDialog.STEP_IMPORT)).is_equal("The import is running")
	assert_str(dialog.reason_for_step(ImportDialog.STEP_RESULT)).is_empty()


func test_busy_backoff_doubles_and_caps() -> void:
	assert_float(ImportDialog.busy_backoff_sec(0)).is_equal_approx(0.5, 1e-9)
	assert_float(ImportDialog.busy_backoff_sec(1)).is_equal_approx(1.0, 1e-9)
	assert_float(ImportDialog.busy_backoff_sec(2)).is_equal_approx(2.0, 1e-9)
	assert_float(ImportDialog.busy_backoff_sec(3)).is_equal_approx(4.0, 1e-9)
	assert_float(ImportDialog.busy_backoff_sec(9)).is_equal_approx(4.0, 1e-9)


func test_every_error_code_is_one_plain_sentence() -> void:
	for code in ["E_CRS_REQUIRED", "E_BAD_PARAMS", "E_NO_PROJECT", "E_NOT_FOUND", "E_EMPTY", "E_BUSY", "E_INTERNAL", "E_DISCONNECTED", "E_TIMEOUT", "E_WHATEVER"]:
		var sentence := ImportDialog.plain_error(code, "")
		assert_str(sentence).is_not_empty()
		assert_bool(sentence.contains("E_")).append_failure_message("%s leaks its code" % code).is_false()
	assert_str(ImportDialog.plain_error("E_BAD_PARAMS", "buffer_m must be > 0")).is_equal("buffer_m must be > 0")
	assert_str(ImportDialog.format_label("geotiff")).is_equal("GeoTIFF raster")
	assert_str(ImportDialog.format_label("cuzk-ortho-sheet")).contains("ČÚZK")
	assert_str(ImportDialog.format_label("wmts-descriptor")).is_equal("ČÚZK orthophoto service (.wmts)")
	assert_str(ImportDialog.class_label(2)).is_equal("2 Ground")
	assert_str(ImportDialog.class_label(64)).is_equal("64")
	# LAS 1.4 reserves codes 8 and 12: they show only their number.
	assert_str(ImportDialog.class_label(8)).is_equal("8")
	assert_str(ImportDialog.class_label(12)).is_equal("12")
	assert_str(ImportDialog.class_label(9)).is_equal("9 Water")


func test_enter_in_a_number_field_does_not_start_the_import() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	dialog.prepare(_one_alignment(0.0, 1000.0))
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	await dialog.go_next()
	assert_bool(dialog.corridor_box().visible).is_true()

	# Enter in the buffer and in the unit only confirms the number: Import waits for the button.
	for field: NumberField in [dialog.buffer_field(), dialog.unit_field()]:
		field.grab_focus()
		await _frames(1)
		_press(KEY_ENTER)
		await _frames(3)
		assert_int(backend.count("import.run")) \
			.append_failure_message("Enter in a number field started the import").is_equal(0)
		assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	assert_str(dialog.buffer_field().text).is_equal("250")


func test_request_timeouts_per_method() -> void:
	assert_float(ImportDialog.request_timeout_sec("import.run")).is_equal(1800.0)
	assert_float(ImportDialog.request_timeout_sec("import.inspect")).is_equal(300.0)
	assert_float(ImportDialog.request_timeout_sec("crs.search")).is_equal(30.0)


func test_every_invalid_number_disables_import_with_its_name() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_geotiff_report(_NODATA_FLOAT32)])
	var dialog := _dialog(backend)
	dialog.prepare(_one_alignment(12345.6789, 18184.9717))
	dialog.set_files(PackedStringArray([_FILE_TIF]))
	await dialog.go_next()
	await dialog.go_next()
	assert_int(dialog.clip_choice().selected).is_equal(ImportDialog.CLIP_CORRIDOR)
	assert_bool(dialog.primary_button().disabled).is_false()

	_type(dialog.unit_field(), "x")
	_assert_disabled_with_reason(dialog, "Enter a number for the linear unit")
	_type(dialog.unit_field(), "0")
	_assert_disabled_with_reason(dialog, "The linear unit must be above 0")
	_type(dialog.unit_field(), "1")

	_type(dialog.buffer_field(), "abc")
	_assert_disabled_with_reason(dialog, "Enter a number for the buffer")
	_type(dialog.buffer_field(), "-5")
	_assert_disabled_with_reason(dialog, "The buffer must be above 0")
	_type(dialog.buffer_field(), "250")

	dialog.station_check().button_pressed = true
	_type(dialog.station_from_field(), "")
	_assert_disabled_with_reason(dialog, "Enter a number for the station From")
	_type(dialog.station_from_field(), "12000.25")
	_type(dialog.station_to_field(), "x")
	_assert_disabled_with_reason(dialog, "Enter a number for the station To")
	_type(dialog.station_to_field(), "15500.5")
	dialog.station_check().button_pressed = false

	_type(dialog.resolution_field(), "0")
	_assert_disabled_with_reason(dialog, "The display resolution must be above 0")
	_type(dialog.resolution_field(), "x")
	_assert_disabled_with_reason(dialog, "Enter a number for the display resolution")
	_type(dialog.resolution_field(), "0.5")

	_type(dialog.nodata_field(), "x")
	_assert_disabled_with_reason(dialog, "Enter a number for no data")
	_type(dialog.nodata_field(), "-12.5")
	assert_bool(dialog.primary_button().disabled).is_false()

	dialog.clip_choice().select(ImportDialog.CLIP_BBOX)
	dialog.clip_choice().item_selected.emit(ImportDialog.CLIP_BBOX)
	_type(dialog.bbox_fields()[2], "x")
	_assert_disabled_with_reason(dialog, "Enter a number for Max E")
	_type(dialog.bbox_fields()[2], "-746500")
	assert_bool(dialog.primary_button().disabled).is_false()

	# The buffer is not sent under a box, so its text does not block Import.
	_type(dialog.buffer_field(), "x")
	assert_bool(dialog.primary_button().disabled).is_false()


func test_warnings_come_before_the_class_table() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report(null)])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_INSPECT)

	var texts := Array(dialog.report_texts())
	var table := texts.find("Point class")
	assert_int(table).append_failure_message("the class table is not in the report").is_greater_equal(0)
	for line: String in [ImportDialog.NO_CRS_NOTICE, "the CRS record in the header could not be parsed"]:
		var at := texts.find(line)
		assert_int(at).append_failure_message("%s is not in the report" % line).is_greater_equal(0)
		assert_int(at).append_failure_message("%s is below the class table" % line).is_less(table)


## A report too long for either screen, so its scroll is capped and the cap is what this test measures.
func test_report_scroll_follows_the_screen() -> void:
	var backend := _backend()
	var long_report := _las_report(null)
	var warnings: Array = long_report.result["warnings"]
	for index in 40:
		warnings.append("warning %d: a line long enough to take its own row in the report" % index)
	backend.reply("import.inspect", [long_report])
	var dialog := _dialog(backend)
	dialog.prepare([])
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	var report := dialog.find_child("ReportScroll", true, false) as ScrollContainer

	dialog.screen_rect = Rect2i(0, 0, 1280, 720)
	dialog.popup_centered()
	await _frames(4)
	var at_720 := report.custom_minimum_size.y
	assert_int(dialog.size.y).append_failure_message("the report window is taller than 90 % of 720").is_less_equal(648)

	dialog.screen_rect = Rect2i(0, 0, 1600, 900)
	dialog.go_back()
	await dialog.go_next()
	await _frames(4)
	var at_900 := report.custom_minimum_size.y
	assert_float(at_900) \
		.append_failure_message("the report cap did not grow with a taller screen (%s at 720, %s at 900)" % [at_720, at_900]) \
		.is_greater(at_720)


func test_short_pages_are_sized_to_their_content() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.screen_rect = Rect2i(0, 0, 1600, 900)
	dialog.popup_centered()
	await _to_settings(dialog)
	await dialog.go_next()
	await _frames(4)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	var result := dialog.find_child("ResultScroll", true, false) as ScrollContainer
	assert_float(result.size.y) \
		.append_failure_message("the Result page is %s px tall at 1600 x 900, not under half the screen" % result.size.y) \
		.is_less(450.0)
	dialog.hide()

	var report_backend := _backend()
	report_backend.reply("import.inspect", [_las_report(null)])
	var report_dialog := _dialog(report_backend)
	report_dialog.screen_rect = Rect2i(0, 0, 1280, 720)
	report_dialog.popup_centered()
	report_dialog.prepare([])
	report_dialog.set_files(PackedStringArray([_FILE_LAS]))
	await report_dialog.go_next()
	await _frames(4)
	assert_int(report_dialog.step()).is_equal(ImportDialog.STEP_INSPECT)
	_assert_inside_1280_by_720(report_dialog, "step 2, the LAS report")
	var report := report_dialog.find_child("ReportScroll", true, false) as ScrollContainer
	var content := report.get_child(0) as Control
	assert_float(content.get_combined_minimum_size().y) \
		.append_failure_message("the LAS report needs a scroll at 1280 x 720 (content %s, page %s)" % [content.get_combined_minimum_size().y, report.size.y]) \
		.is_less_equal(report.size.y)
	var warning := "the CRS record in the header could not be parsed"
	var found := false
	for node in report.find_children("*", "Label", true, false):
		var label := node as Label
		if label.text == warning:
			found = true
			assert_float(label.get_global_rect().end.y) \
				.append_failure_message("the warning is below the visible part of the report") \
				.is_less_equal(report.get_global_rect().end.y)
	assert_bool(found).append_failure_message("the warning is not on the report page").is_true()


func test_the_result_page_shows_only_close() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)
	await dialog.go_next()
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	assert_str(dialog.primary_button().text).is_equal("Close")
	assert_bool(dialog.back_button().visible).append_failure_message("Back shows on the Result page").is_false()
	assert_bool(dialog.get_cancel_button().visible).append_failure_message("Cancel shows on the Result page").is_false()

	dialog.hide()
	dialog.prepare([])
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SOURCE)
	assert_bool(dialog.back_button().visible).append_failure_message("Back is hidden after reopening").is_true()
	assert_bool(dialog.get_cancel_button().visible).append_failure_message("Cancel is hidden after reopening").is_true()


func test_enter_in_the_crs_field_with_a_code_presses_the_primary_button() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report(null)])
	backend.reply("import.run", [_run_ok()])
	var dialog := _dialog(backend)
	dialog.horizontal_picker().wait = func(_seconds: float) -> void: pass
	await _to_settings(dialog)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_SETTINGS)
	_assert_disabled_with_reason(dialog, "Choose a coordinate system for this data")

	var picker := dialog.horizontal_picker()
	picker.line_edit().text = "krovak"
	picker.line_edit().text_changed.emit("krovak")
	await _frames(2)
	picker.choose_result(1)
	assert_str(picker.code()).is_equal("EPSG:5514")
	assert_bool(dialog.primary_button().disabled).is_false()

	picker.line_edit().text_submitted.emit(picker.line_edit().text)
	await _frames(3)
	assert_int(backend.count("import.run")) \
		.append_failure_message("Enter in the CRS field with a chosen code did not press Import") \
		.is_equal(1)


func test_buffer_above_5000_disables_import_with_a_reason() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	var dialog := _dialog(backend)
	dialog.prepare(_one_alignment(0.0, 1000.0))
	dialog.set_files(PackedStringArray([_FILE_LAS]))
	await dialog.go_next()
	await dialog.go_next()
	assert_int(dialog.clip_choice().selected).is_equal(ImportDialog.CLIP_CORRIDOR)

	_type(dialog.buffer_field(), "5000")
	assert_bool(dialog.primary_button().disabled).is_false()
	_type(dialog.buffer_field(), "5000.5")
	_assert_disabled_with_reason(dialog, _BUFFER_MAX_TEXT)
	_type(dialog.buffer_field(), "6000")
	_assert_disabled_with_reason(dialog, _BUFFER_MAX_TEXT)
	_type(dialog.buffer_field(), "-5")
	_assert_disabled_with_reason(dialog, "The buffer must be above 0")
	_type(dialog.buffer_field(), "5000")
	assert_bool(dialog.primary_button().disabled).is_false()
	assert_float(dialog.settings_snapshot()["clip"]["buffer_m"]).is_equal(5000.0)


func test_hiding_during_the_source_lookup_reopens_at_step_four() -> void:
	var backend := _backend()
	backend.reply("import.inspect", [_las_report("EPSG:5514")])
	backend.reply("import.run", [_run_ok()])
	var gate := Tick.new()
	backend.gates["source.get"] = gate
	var dialog := _dialog(backend)
	dialog.popup_centered()
	await _to_settings(dialog)

	dialog.go_next()
	await _frames(2)
	assert_int(backend.count("source.get")).append_failure_message("the source lookup did not start").is_equal(1)
	assert_bool(dialog.is_busy()).is_true()
	dialog.hide()
	dialog.prepare([])
	assert_int(dialog.step()).append_failure_message("reopening during the lookup did not show step 4").is_equal(ImportDialog.STEP_IMPORT)
	assert_bool(dialog.is_busy()).is_true()

	gate.fired.emit()
	await _frames(3)
	assert_int(dialog.step()).is_equal(ImportDialog.STEP_RESULT)
	assert_bool(dialog.is_busy()).is_false()
