extends GdUnitTestSuite
## "Export corridor envelope..." (T-140), the parts that need no backend: request params, file names, the
## plain-language error sentences, and the dialog's handling of E_BUSY / E_INTERNAL replies through a fake
## requester. The live-backend half is `integration/test_envelope_export_live.gd`.


func _error_envelope(code: String, message: String) -> IpcEnvelope:
	var envelope := IpcEnvelope.new()
	envelope.type = "err"
	envelope.error = {"code": code, "message": message}
	return envelope


func test_params_default_to_the_whole_alignment_set() -> void:
	var built := EnvelopeExportDialog.build_params({"path": "C:/out/a.geojson", "buffer_m": 250.0, "format": &"geojson"})
	assert_bool(built["ok"]).is_true()
	var params: Dictionary = built["params"]
	assert_str(params["path"]).is_equal("C:/out/a.geojson")
	assert_float(params["buffer_m"]).is_equal(250.0)
	assert_bool(params.has("alignment_ids")).is_false()
	assert_bool(params.has("epsg")).is_false()
	assert_bool(params.has("overwrite")).is_false()


func test_params_carry_station_range_crs_and_format_suffix() -> void:
	var built := EnvelopeExportDialog.build_params({
		"path": "C:/out/a.geojson", "buffer_m": 100.0, "format": &"shapefile", "station_enabled": true,
		"station_from": 500.0, "station_to": 4000.0, "epsg_text": " 5514 ", "alignment_id": "al-1",
	})
	assert_bool(built["ok"]).is_true()
	var params: Dictionary = built["params"]
	assert_str(params["path"]).is_equal("C:/out/a.shp")
	assert_int(params["epsg"]).is_equal(5514)
	assert_float(params["station_from"]).is_equal(500.0)
	assert_float(params["station_to"]).is_equal(4000.0)
	assert_array(params["alignment_ids"]).contains_exactly(["al-1"])


func test_params_reject_bad_input_with_a_sentence() -> void:
	var base := {"path": "C:/out/a.geojson", "buffer_m": 250.0, "format": &"geojson"}
	for bad: Dictionary in [
		{"path": ""},
		{"buffer_m": 0.5},
		{"buffer_m": 5001.0},
		{"epsg_text": "web mercator"},
		{"station_enabled": true, "alignment_id": ""},
		{"station_enabled": true, "alignment_id": "a", "station_from": 9.0, "station_to": 3.0},
	]:
		var form := base.duplicate()
		form.merge(bad, true)
		var built := EnvelopeExportDialog.build_params(form)
		assert_bool(built["ok"]).is_false()
		assert_str(built["error"]).is_not_empty()


func test_buffer_limits_are_inclusive() -> void:
	for buffer_m in [1.0, 5000.0]:
		var built := EnvelopeExportDialog.build_params({"path": "C:/o/a.geojson", "buffer_m": buffer_m})
		assert_bool(built["ok"]).is_true()


func test_normalized_path_and_output_files() -> void:
	assert_str(EnvelopeExportDialog.normalized_path("a", &"geojson")).is_equal("a.geojson")
	assert_str(EnvelopeExportDialog.normalized_path("a.json", &"geojson")).is_equal("a.json")
	assert_str(EnvelopeExportDialog.normalized_path("a.shp", &"geojson")).is_equal("a.geojson")
	assert_str(EnvelopeExportDialog.normalized_path("a.geojson", &"shapefile")).is_equal("a.shp")
	assert_array(EnvelopeExportDialog.output_files("x/a.geojson")).contains_exactly(["x/a.geojson"])
	assert_array(EnvelopeExportDialog.output_files("x/a.shp")).contains_exactly(
		["x/a.shp", "x/a.shx", "x/a.dbf", "x/a.prj", "x/a.cpg"]
	)


func test_backend_errors_become_plain_sentences() -> void:
	for code in ["E_BUSY", "E_INTERNAL", "E_DISCONNECTED", "E_TIMEOUT", "E_NO_PROJECT", "E_EMPTY", "E_CRS_REQUIRED", "E_BAD_PARAMS", "E_WHATEVER"]:
		var sentence := EnvelopeExportDialog.plain_error(code, "")
		assert_str(sentence).is_not_empty()
		assert_bool(sentence.contains("E_")).is_false()
		assert_bool(sentence.ends_with(".")).is_true()
	assert_str(EnvelopeExportDialog.plain_error("E_BAD_PARAMS", "path must be outside the repository (D:/x): 'D:/x/a'")) \
		.contains("outside the project folder")
	assert_str(EnvelopeExportDialog.plain_error("E_INTERNAL", "install it with uv sync --extra gis")).contains("uv sync")
	assert_str(EnvelopeExportDialog.plain_error("E_BUSY", "")).contains("busy")


func test_overwrite_refusal_is_recognised() -> void:
	assert_bool(EnvelopeExportDialog.is_overwrite_refusal(
		"E_BAD_PARAMS", "cannot write x: refusing to overwrite existing file(s): a.geojson; set overwrite"
	)).is_true()
	assert_bool(EnvelopeExportDialog.is_overwrite_refusal("E_BAD_PARAMS", "buffer_m must be > 0")).is_false()


func test_summary_names_files_area_and_vertices() -> void:
	var summary := EnvelopeExportDialog.summary_of({
		"files": ["C:/o/a.geojson"], "area_m2": 12500000.0, "vertex_count": 321,
	})
	assert_str(summary).contains("a.geojson")
	assert_str(summary).contains("12.50 km")
	assert_str(summary).contains("321 vertices")


func test_busy_and_internal_replies_show_in_the_dialog_without_closing_it() -> void:
	var dialog := EnvelopeExportDialog.new()
	add_child(dialog)
	auto_free(dialog)
	dialog.prepare([])
	dialog.path_field().text = OS.get_temp_dir().path_join("coypu_t140_never.geojson")
	for code in ["E_BUSY", "E_INTERNAL"]:
		dialog.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope: return _error_envelope(code, "x")
		var outcome: Dictionary = await dialog.run_export()
		assert_bool(outcome["ok"]).is_false()
		assert_bool(dialog.error_label().visible).is_true()
		assert_str(dialog.error_label().text).is_equal(EnvelopeExportDialog.plain_error(code, "x"))
		assert_bool(dialog.is_busy()).is_false()


func test_a_file_that_appears_late_goes_through_the_overwrite_question_once() -> void:
	var dialog := EnvelopeExportDialog.new()
	add_child(dialog)
	auto_free(dialog)
	dialog.prepare([])
	dialog.path_field().text = OS.get_temp_dir().path_join("coypu_t140_late.geojson")
	var calls: Array = []
	dialog.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		calls.append(params.get("overwrite", false))
		if not params.get("overwrite", false):
			return _error_envelope("E_BAD_PARAMS", "cannot write x: refusing to overwrite existing file(s): a.geojson")
		var ok := IpcEnvelope.new()
		ok.type = "res"
		ok.result = {"files": ["x/a.geojson"], "area_m2": 1.0e6, "vertex_count": 5}
		return ok
	var asked: Array = []
	dialog.overwrite_decider = func(names: PackedStringArray) -> bool:
		asked.append(names)
		return true
	var outcome: Dictionary = await dialog.run_export()
	assert_bool(outcome["ok"]).is_true()
	assert_array(calls).contains_exactly([false, true])
	assert_int(asked.size()).is_equal(1)
