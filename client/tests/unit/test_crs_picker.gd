extends GdUnitTestSuite
## T-136 Part B: the CRS picker with a fake requester, so no backend runs. The debounce and the late replies are driven
## by hand: the picker's `wait` seam hands out a tick per pause, and the test releases the tick it wants.


## A pause or a reply that the test releases with `fired.emit()`.
class Tick extends RefCounted:
	signal fired


func _ok(result: Dictionary) -> IpcEnvelope:
	var envelope := IpcEnvelope.new()
	envelope.type = "res"
	envelope.result = result
	return envelope


func _crs_result(rows: Array) -> Dictionary:
	var results: Array = []
	for row: Array in rows:
		results.append({"code": row[0], "name": row[1], "kind": "projected", "area": null, "deprecated": false})
	return {"results": results}


func _type(picker: CrsPicker, text: String) -> void:
	picker.line_edit().text = text
	picker.line_edit().text_changed.emit(text)


func _frames(count: int) -> void:
	for i in count:
		await get_tree().process_frame


func _new_picker() -> CrsPicker:
	var picker := CrsPicker.new()
	add_child(picker)
	auto_free(picker)
	return picker


func test_typing_debounces_and_shows_only_the_latest_answer() -> void:
	var picker := _new_picker()
	var ticks: Array = []
	picker.wait = func(_seconds: float) -> void:
		var tick := Tick.new()
		ticks.append(tick)
		await tick.fired
	var queries: Array = []
	var late := Tick.new()
	picker.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		queries.append(String(params["query"]))
		if params["query"] == "kr":
			await late.fired
			return _ok(_crs_result([["EPSG:5513", "S-JTSK / Krovak"]]))
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))

	_type(picker, "k")
	_type(picker, "kr")
	(ticks[0] as Tick).fired.emit()
	await _frames(2)
	assert_array(queries).append_failure_message("a superseded pause still searched").is_empty()

	(ticks[1] as Tick).fired.emit()
	await _frames(2)
	assert_array(queries).contains_exactly(["kr"])

	_type(picker, "kro")
	(ticks[2] as Tick).fired.emit()
	await _frames(2)
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:5514"])

	late.fired.emit()
	await _frames(2)
	assert_array(Array(picker.result_codes())) \
		.append_failure_message("the late answer for 'kr' replaced the answer for 'kro'") \
		.contains_exactly(["EPSG:5514"])
	assert_array(queries).contains_exactly(["kr", "kro"])


func test_free_text_is_not_a_code() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))

	_type(picker, "5514")
	await _frames(2)
	assert_str(picker.code()).is_empty()
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:5514"])

	picker.choose_result(1)
	assert_str(picker.code()).is_equal("EPSG:5514")
	assert_str(picker.line_edit().text).is_equal("EPSG:5514 — S-JTSK / Krovak East North")

	_type(picker, "EPSG:5514 typed by hand")
	assert_str(picker.code()).append_failure_message("typed text left a code behind").is_empty()


func test_kind_filter_is_sent() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	var kinds: Array = []
	picker.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		kinds.append(String(params["kind"]))
		return _ok(_crs_result([]))
	picker.set_kind_filter("vertical")
	_type(picker, "baltic")
	await _frames(2)
	assert_array(kinds).contains_exactly(["vertical"])
	assert_str(picker.kind_filter()).is_equal("vertical")


func test_a_declared_wkt_is_kept_verbatim() -> void:
	var picker := _new_picker()
	var calls := []
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		calls.append(1)
		return _ok(_crs_result([]))
	var wkt := 'PROJCRS["S-JTSK / Krovak East North",BASEGEOGCRS["S-JTSK"],CONVERSION["Krovak"]]'
	picker.set_code(wkt)
	await _frames(2)
	assert_str(picker.code()).is_equal(wkt)
	assert_str(picker.line_edit().text).is_equal("As declared in the file")
	assert_array(calls).append_failure_message("a WKT was looked up as a code").is_empty()


func test_set_code_fills_the_name() -> void:
	var picker := _new_picker()
	var lookups: Array = []
	picker.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		lookups.append(params)
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))
	picker.set_code("EPSG:5514")
	await _frames(2)
	assert_str(picker.code()).is_equal("EPSG:5514")
	assert_str(picker.line_edit().text).is_equal("EPSG:5514 — S-JTSK / Krovak East North")
	assert_int(lookups.size()).is_equal(1)
	assert_str(String(lookups[0]["query"])).is_equal("EPSG:5514")
	assert_int(int(lookups[0]["limit"])).is_equal(1)


func test_a_late_name_lookup_does_not_replace_a_chosen_code() -> void:
	var picker := _new_picker()
	var slow := Tick.new()
	picker.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		if params["query"] == "EPSG:5514":
			await slow.fired
			return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))
		return _ok(_crs_result([["EPSG:5513", "S-JTSK / Krovak"]]))

	picker.set_code("EPSG:5514")
	picker.set_code("EPSG:5513")
	await _frames(2)
	slow.fired.emit()
	await _frames(2)
	assert_str(picker.code()).is_equal("EPSG:5513")
	assert_str(picker.line_edit().text) \
		.append_failure_message("the late name for 5514 replaced the chosen 5513") \
		.is_equal("EPSG:5513 — S-JTSK / Krovak")


func test_enter_chooses_the_first_result() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"], ["EPSG:5513", "S-JTSK / Krovak"]]))
	var submits: Array = []
	picker.submitted.connect(func() -> void: submits.append(1))

	_type(picker, "krovak")
	await _frames(2)
	picker.line_edit().text_submitted.emit("krovak")
	assert_str(picker.code()).is_equal("EPSG:5514")
	assert_str(picker.line_edit().text).is_equal("EPSG:5514 — S-JTSK / Krovak East North")
	assert_array(submits).append_failure_message("Enter chose a result and also pressed the owner's button").is_empty()

	picker.line_edit().text_submitted.emit("krovak")
	assert_array(submits).append_failure_message("Enter with a chosen code did not reach the owner").contains_exactly([1])


func test_set_code_clears_an_earlier_list() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))
	_type(picker, "krovak")
	await _frames(2)
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:5514"])

	picker.set_code("EPSG:5513")
	assert_array(Array(picker.result_codes())) \
		.append_failure_message("set_code kept the list of an earlier search beside the new code") \
		.is_empty()
	assert_str(picker.code()).is_equal("EPSG:5513")


func test_long_names_do_not_widen_the_picker() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.size = Vector2(500.0, 36.0)
	await _frames(2)
	var idle_width := picker.get_combined_minimum_size().x

	var long_name := "KRG_1990 / Krovak " + "very long name that goes on and on ".repeat(6)
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:102065", long_name]]))
	_type(picker, "krovak")
	await _frames(2)
	picker.choose_result(1)
	await _frames(2)
	assert_float(picker.get_combined_minimum_size().x) \
		.append_failure_message("a long name widened the picker") \
		.is_less_equal(idle_width)
	assert_float(picker.line_edit().size.x) \
		.append_failure_message("the field is not wider than the dropdown (3 : 2)") \
		.is_greater(picker.results_choice().size.x)


func test_down_opens_the_results() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"], ["EPSG:5513", "S-JTSK / Krovak"]]))
	_type(picker, "krovak")
	await _frames(2)
	picker.line_edit().grab_focus()
	await _frames(1)
	assert_bool(picker.results_choice().get_popup().visible).append_failure_message("the results were open before Down").is_false()

	var down := InputEventKey.new()
	down.keycode = KEY_DOWN
	down.physical_keycode = KEY_DOWN
	down.pressed = true
	get_viewport().push_input(down)
	await _frames(2)
	assert_bool(picker.results_choice().get_popup().visible) \
		.append_failure_message("Down in the field did not open the results") \
		.is_true()


func test_listed_results_leave_the_placeholder_selected() -> void:
	var picker := _new_picker()
	picker.wait = func(_seconds: float) -> void: pass
	picker.requester = func(_method: String, _params: Dictionary) -> IpcEnvelope:
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"], ["EPSG:5513", "S-JTSK / Krovak"]]))
	assert_str(picker.results_choice().get_item_text(0)).is_equal("Search results")
	assert_int(picker.results_choice().text_overrun_behavior).is_equal(TextServer.OVERRUN_TRIM_ELLIPSIS)
	_type(picker, "krovak")
	await _frames(2)
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:5514", "EPSG:5513"])
	assert_int(picker.results_choice().selected) \
		.append_failure_message("the first listed result shows as chosen while no code is chosen") \
		.is_equal(0)
	assert_str(picker.code()).is_empty()


func test_enter_ignores_an_out_of_date_list() -> void:
	var picker := _new_picker()
	var ticks: Array = []
	picker.wait = func(_seconds: float) -> void:
		var tick := Tick.new()
		ticks.append(tick)
		await tick.fired
	picker.requester = func(_method: String, params: Dictionary) -> IpcEnvelope:
		if params["query"] == "k":
			return _ok(_crs_result([["EPSG:2000", "Stale answer for k"]]))
		return _ok(_crs_result([["EPSG:5514", "S-JTSK / Krovak East North"]]))
	var submits: Array = []
	picker.submitted.connect(func() -> void: submits.append(1))

	_type(picker, "k")
	(ticks[0] as Tick).fired.emit()
	await _frames(2)
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:2000"])

	_type(picker, "krovak")
	assert_array(Array(picker.result_codes())) \
		.append_failure_message("the list for 'k' stayed on show while 'krovak' was typed") \
		.is_empty()
	picker.line_edit().text_submitted.emit("krovak")
	assert_str(picker.code()).append_failure_message("Enter chose a result of the list for 'k'").is_empty()
	assert_array(submits).append_failure_message("Enter with no code and no list reached the owner").is_empty()

	(ticks[1] as Tick).fired.emit()
	await _frames(2)
	assert_array(Array(picker.result_codes())).contains_exactly(["EPSG:5514"])
	picker.line_edit().text_submitted.emit("krovak")
	assert_str(picker.code()).is_equal("EPSG:5514")
