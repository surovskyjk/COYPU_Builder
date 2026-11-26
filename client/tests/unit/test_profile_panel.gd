extends GdUnitTestSuite
## The Profile tab (T-137 B): the panel's drawing maths and readout, its window, exaggeration and states, the idealised
## note, the fetch cache and visibility, and the bottom tabs of the shell in both presets. A canned backend answer stands
## in for `alignment.ground_profile` (see [method _canned]), so no test reaches a backend. Nodes are freed with
## [method auto_free]; the Session override and the project state are restored in [method after_test].

const _SHELL_SCENE := "res://ui/shell/ui_shell.tscn"
const _ALIGNMENT := "profile_test_line"

## Which canned reply the override gives: "ok", "err" or "all_no_ground".
var _reply := "ok"
var _override_calls := 0
## The seconds a reply waits, for the tests where a fetch is still running when the inputs change (see [method _delayed]).
var _delay_s := 0.0


## A playback that reports the station and the alignment the test sets, in place of a bound timeline.
class FakePlayback extends PlaybackController:
	var station := 13000.0
	var id := "profile_test_line"

	func lead_station() -> float:
		return station

	func alignment_id() -> String:
		return id


func before_test() -> void:
	_reply = "ok"
	_override_calls = 0
	_delay_s = 0.0
	Session.set_ground_profile_request_override(Callable(self, "_canned"))


func after_test() -> void:
	# While the override still answers: a panel that is still alive refetches from it, not from a backend.
	Session.set_project_info({})
	Session.set_ground_profile_request_override(Callable())
	EventBus.project_changed.emit()
	Session.set_data_sources([] as Array[Dictionary])


# --- canned data ------------------------------------------------------------------------------------------------------

## Five rows at 10 m from 13 000 m. Row 2 has no ground (flag 1), so the fake line has one gap; the cut is 18 m² on the
## ground rows with ground above formation, and no fill.
func _result_and_blobs(reply: String) -> Array:
	var stations := PackedFloat64Array([13000.0, 13010.0, 13020.0, 13030.0, 13040.0])
	var rail := PackedFloat64Array([175.0, 175.0, 175.0, 175.0, 175.0])
	var ground := PackedFloat64Array([192.0, 192.0, NAN, 190.0, 190.0])
	var flags := PackedByteArray([0, 0, 1, 0, 0])
	var source := PackedByteArray([0, 0, 255, 0, 0])
	var cut := PackedFloat64Array([18.0, 18.0, 0.0, 16.0, 16.0])
	var fill := PackedFloat64Array([0.0, 0.0, 0.0, 0.0, 0.0])
	if reply == "all_no_ground":
		ground = PackedFloat64Array([NAN, NAN, NAN, NAN, NAN])
		flags = PackedByteArray([1, 1, 1, 1, 1])
		source = PackedByteArray([255, 255, 255, 255, 255])
		cut = PackedFloat64Array([0.0, 0.0, 0.0, 0.0, 0.0])
	var result := {
		"alignment_id": _ALIGNMENT,
		"row_count": stations.size(),
		"station_start": 13000.0,
		"station_end": 13040.0,
		"cut_volume_m3": 1.0,
		"fill_volume_m3": 0.0,
		"no_ground_spans": [[13020.0, 13020.0]],
		"terrain_revision": 7,
		"template": {"formation_width_m": 6.0, "formation_depth_m": 0.8, "cut_slope": 1.5, "fill_slope": 1.5,
			"sample_spacing_m": 0.5, "max_half_width_m": 60.0},
		"source_ids": ["dmr"],
		"source_names": ["ČÚZK DMR 5G"],
		"attributions": {"dmr": "© ČÚZK"},
		"warnings": [],
	}
	var blobs := {
		"station": stations, "rail_h": rail, "ground_h": ground, "source": source,
		"cut_area": cut, "fill_area": fill, "flags": flags,
	}
	return [result, blobs]


func _canned(params: Dictionary) -> IpcEnvelope:
	_override_calls += 1
	var envelope := IpcEnvelope.new()
	envelope.method = "alignment.ground_profile"
	envelope.blobs = {}
	if _reply == "err":
		envelope.type = "err"
		envelope.error = {"code": "E_BAD_PARAMS", "message": "the work budget is exceeded: use a coarser spacing_m"}
		return envelope
	var parts := _result_and_blobs(_reply)
	envelope.type = "res"
	envelope.result = parts[0]
	envelope.blobs = parts[1]
	return envelope


## [method _canned] after `_delay_s` seconds: a fetch that is still running while the test changes its inputs.
func _delayed(params: Dictionary) -> IpcEnvelope:
	await get_tree().create_timer(_delay_s).timeout
	return _canned(params)


## A profile of `count` rows at 10 m from 13 000 m: the rail is level, the ground rises and falls about it, so the cut and the fill
## alternate many times, and rows `gap_from` to `gap_to` have no ground (a bridge, as DMR 5G has over water).
func _wide_profile(count: int, gap_from: int, gap_to: int) -> Dictionary:
	var stations := PackedFloat64Array()
	var rail := PackedFloat64Array()
	var ground := PackedFloat64Array()
	var flags := PackedByteArray()
	var source := PackedByteArray()
	var cut := PackedFloat64Array()
	var fill := PackedFloat64Array()
	for i in count:
		var gap := i >= gap_from and i <= gap_to
		var height := 175.0 + 2.0 * sin(float(i) * 0.37)
		stations.append(13000.0 + 10.0 * i)
		rail.append(175.0)
		ground.append(NAN if gap else height)
		flags.append(1 if gap else 0)
		source.append(255 if gap else 0)
		cut.append(0.0 if gap else maxf(height - 174.2, 0.0))
		fill.append(0.0 if gap else maxf(174.2 - height, 0.0))
	return {
		"station": stations, "rail_h": rail, "ground_h": ground, "flags": flags, "source": source,
		"cut_area": cut, "fill_area": fill, "station_start": 13000.0, "station_end": 13000.0 + 10.0 * (count - 1),
		"row_count": count, "no_ground_spans": [[13000.0 + 10.0 * gap_from, 13000.0 + 10.0 * gap_to]],
		"source_names": ["dmr"], "terrain_revision": 3, "cut_volume_m3": 1.0, "fill_volume_m3": 1.0,
		"template": {"formation_width_m": 6.0, "formation_depth_m": 0.8, "cut_slope": 1.5, "fill_slope": 1.5},
	}


## Relative luminance and the contrast ratio of two colours, as WCAG 2 defines them.
func _contrast(a: Color, b: Color) -> float:
	var la := _luminance(a)
	var lb := _luminance(b)
	return (maxf(la, lb) + 0.05) / (minf(la, lb) + 0.05)


func _luminance(c: Color) -> float:
	return 0.2126 * _linear(c.r) + 0.7152 * _linear(c.g) + 0.0722 * _linear(c.b)


func _linear(channel: float) -> float:
	return channel / 12.92 if channel <= 0.03928 else pow((channel + 0.055) / 1.055, 2.4)


## The decoded dictionary the panel draws, as `Session.fetch_ground_profile` returns it.
func _profile() -> Dictionary:
	var parts := _result_and_blobs("ok")
	var profile: Dictionary = (parts[0] as Dictionary).duplicate(true)
	var blobs: Dictionary = parts[1]
	for key: String in ["station", "rail_h", "ground_h", "cut_area", "fill_area"]:
		profile[key] = blobs[key]
	profile["source"] = blobs["source"]
	profile["flags"] = blobs["flags"]
	return profile


func _make_panel() -> ProfilePanel:
	var panel := ProfilePanel.new()
	panel.custom_minimum_size = Vector2(900.0, 240.0)
	auto_free(panel)
	add_child(panel)
	return panel


func _alignment_entry(idealised: bool) -> Array[Dictionary]:
	return [{
		"alignment_id": _ALIGNMENT, "name": "Test line", "mode": "rail", "station_start": 13000.0,
		"station_end": 13040.0, "length": 40.0, "warnings": [], "profile_idealised": idealised,
	}]


func _set_project(idealised: bool) -> void:
	Session.set_project_info({"project_id": "t", "alignments": _alignment_entry(idealised)})


func _make_shell(window_size: Vector2 = Vector2(1600.0, 900.0)) -> UiShell:
	var host := Control.new()
	host.set_anchors_preset(Control.PRESET_TOP_LEFT)
	host.size = window_size
	add_child(host)
	auto_free(host)
	var shell := (load(_SHELL_SCENE) as PackedScene).instantiate() as UiShell
	host.add_child(shell)
	await get_tree().process_frame
	await get_tree().process_frame
	return shell


# --- pure maths ---------------------------------------------------------------------------------------------------------

func test_window_is_centred_on_the_cursor_and_clamped() -> void:
	assert_vector(ProfilePanel.window_for(15000.0, 12700.0, 17500.0, 1000.0)).is_equal(Vector2(14500.0, 15500.0))
	assert_vector(ProfilePanel.window_for(12800.0, 12700.0, 17500.0, 1000.0)).is_equal(Vector2(12700.0, 13700.0))
	assert_vector(ProfilePanel.window_for(17400.0, 12700.0, 17500.0, 1000.0)).is_equal(Vector2(16500.0, 17500.0))
	assert_vector(ProfilePanel.window_for(15000.0, 12700.0, 17500.0, 0.0)).is_equal(Vector2(12700.0, 17500.0))
	assert_vector(ProfilePanel.window_for(15000.0, 12700.0, 17500.0, 10000.0)).is_equal(Vector2(12700.0, 17500.0))


func test_readout_interpolates_between_rows() -> void:
	var profile := {
		"station": PackedFloat64Array([10.0, 20.0]),
		"rail_h": PackedFloat64Array([100.0, 110.0]),
		"ground_h": PackedFloat64Array([101.0, 113.0]),
		"flags": PackedByteArray([0, 0]),
		"source": PackedByteArray([0, 0]),
		"cut_area": PackedFloat64Array([2.0, 4.0]),
		"fill_area": PackedFloat64Array([0.0, 0.0]),
		"source_names": ["ČÚZK DMR 5G"],
	}
	var middle := ProfilePanel.readout_at(profile, 15.0)
	assert_float(middle["rail_h"]).is_equal_approx(105.0, 1e-9)
	assert_float(middle["ground_h"]).is_equal_approx(107.0, 1e-9)
	assert_float(middle["difference"]).is_equal_approx(2.0, 1e-9)
	assert_float(middle["cut_area"]).is_equal_approx(3.0, 1e-9)
	assert_bool(middle["has_ground"]).is_true()
	assert_str(middle["source_name"]).is_equal("ČÚZK DMR 5G")
	var quarter := ProfilePanel.readout_at(profile, 12.0)
	assert_float(quarter["ground_h"]).is_equal_approx(103.4, 1e-9)
	assert_float(quarter["difference"]).is_equal_approx(1.4, 1e-9)
	var at_row := ProfilePanel.readout_at(profile, 20.0)
	assert_float(at_row["rail_h"]).is_equal_approx(110.0, 1e-9)
	assert_str(ProfilePanel.format_difference(17.36)).is_equal("+17.36 m")
	assert_str(ProfilePanel.format_km(13000.0)).is_equal("km 13.000")


func test_no_ground_rows_read_no_ground_and_are_not_drawn_as_ground() -> void:
	var stations := PackedFloat64Array([0.0, 10.0, 20.0, 30.0, 40.0])
	var ground := PackedFloat64Array([5.0, NAN, 7.0, 8.0, 9.0])
	var flags := PackedByteArray([0, 1, 0, 0, 0])
	var runs := ProfilePanel.valid_runs(stations, ground, flags, 0, 4)
	assert_int(runs.size()).is_equal(2)
	assert_int((runs[0] as PackedVector2Array).size()).is_equal(1)
	assert_int((runs[1] as PackedVector2Array).size()).is_equal(3)
	# The flag, not the NaN, makes the gap: a flagged row that holds a height is not drawn as ground either.
	var flagged_with_height := ProfilePanel.valid_runs(stations, PackedFloat64Array([5.0, 6.0, 7.0, 8.0, 9.0]), flags, 0, 4)
	assert_int(flagged_with_height.size()).append_failure_message("a flagged row with a height is drawn as ground").is_equal(2)
	assert_int((flagged_with_height[0] as PackedVector2Array).size()).is_equal(1)
	for run: Variant in runs:
		for point: Vector2 in run:
			assert_bool(is_nan(point.y)).is_false()
	var profile := {"station": stations, "rail_h": PackedFloat64Array([5.0, 5.0, 5.0, 5.0, 5.0]),
		"ground_h": ground, "flags": flags, "source": PackedByteArray([0, 255, 0, 0, 0]),
		"cut_area": PackedFloat64Array([0.0, 0.0, 0.0, 0.0, 0.0]), "fill_area": PackedFloat64Array([0.0, 0.0, 0.0, 0.0, 0.0]),
		"source_names": ["dmr"]}
	var gap := ProfilePanel.readout_at(profile, 10.0)
	assert_bool(gap["has_ground"]).is_false()
	assert_bool(is_nan(gap["ground_h"])).is_true()
	assert_bool(is_nan(gap["difference"])).is_true()
	assert_bool(ProfilePanel.readout_at(profile, 5.0)["has_ground"]).append_failure_message(
		"a span between a ground row and a flagged row reads as ground"
	).is_false()


func test_cut_and_fill_are_shaded_where_their_areas_are_positive() -> void:
	var cut_only := ProfilePanel.band_polygons(0.0, 2.0, 10.0, 2.0, 0.0, 0.0, true)
	assert_int(cut_only.size()).is_equal(1)
	assert_int(ProfilePanel.band_polygons(0.0, 2.0, 10.0, 2.0, 0.0, 0.0, false).size()).is_equal(0)
	var fill_only := ProfilePanel.band_polygons(0.0, -1.0, 10.0, -1.0, 0.0, 0.0, false)
	assert_int(fill_only.size()).is_equal(1)
	assert_int(ProfilePanel.band_polygons(0.0, -1.0, 10.0, -1.0, 0.0, 0.0, true).size()).is_equal(0)
	var cut_piece: PackedVector2Array = ProfilePanel.band_polygons(0.0, 1.0, 10.0, -1.0, 0.0, 0.0, true)[0]
	assert_float(cut_piece[2].x).is_equal_approx(5.0, 1e-9)
	assert_float(cut_piece[1].y).is_equal_approx(1.0, 1e-9)
	assert_int(ProfilePanel.band_polygons(0.0, 1.0, 10.0, -1.0, 0.0, 0.0, false).size()).is_equal(1)


func test_exaggeration_scales_the_vertical_range() -> void:
	var at_one := ProfilePanel.vertical_range(10.0, 11.0, 5000.0, 1000.0, 200.0, 1.0)
	var at_ten := ProfilePanel.vertical_range(10.0, 11.0, 5000.0, 1000.0, 200.0, 10.0)
	var span_one := at_one.y - at_one.x
	var span_ten := at_ten.y - at_ten.x
	assert_float(span_one).is_equal_approx(1000.0, 1e-6)
	assert_float(span_ten).is_equal_approx(100.0, 1e-6)
	assert_float(span_one / span_ten).is_equal_approx(10.0, 1e-6)
	assert_float((at_ten.x + at_ten.y) * 0.5).is_equal_approx(10.5, 1e-9)
	var fitted := ProfilePanel.vertical_range(0.0, 20.0, 1000.0, 1000.0, 160.0, 10.0)
	assert_bool(fitted.x <= 0.0 and fitted.y >= 20.0).append_failure_message(
		"the range %s does not fit the data 0..20 with its margin" % str(fitted)
	).is_true()


func test_row_range_takes_one_row_either_side_of_the_window() -> void:
	var stations := PackedFloat64Array([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
	# The rows inside [12, 27] are 20 (index 2); one row either side of them is 10 (index 1) and 30 (index 3).
	assert_vector(Vector2(ProfilePanel.row_range(stations, 12.0, 27.0))).is_equal(Vector2(1.0, 3.0))
	assert_vector(Vector2(ProfilePanel.row_range(stations, 20.0, 20.0))).is_equal(Vector2(1.0, 3.0))


func test_ticks_hatching_and_hatch_bounds() -> void:
	var ticks := ProfilePanel.nice_ticks(12.7, 17.5, 6)
	assert_array(Array(ticks)).is_equal([13.0, 14.0, 15.0, 16.0, 17.0])
	var metres := ProfilePanel.nice_ticks(0.0, 1000.0, 6)
	assert_int(metres.size()).is_equal(6)
	assert_float(metres[1] - metres[0]).is_equal_approx(200.0, 1e-9)
	assert_int(ProfilePanel.km_decimals(1000.0)).is_equal(3)
	assert_int(ProfilePanel.km_decimals(5000.0)).is_equal(2)
	assert_int(ProfilePanel.km_decimals(0.0)).is_equal(1)

	var rect := Rect2(10.0, 20.0, 100.0, 50.0)
	var lines := ProfilePanel.hatch_segments(rect, 8.0)
	assert_int(lines.size() % 2).is_equal(0)
	assert_int(lines.size()).is_greater(0)
	for point: Vector2 in lines:
		assert_bool(rect.grow(0.001).has_point(point)).append_failure_message("hatch point %s is outside the rect" % point).is_true()

	var stations := PackedFloat64Array([0.0, 10.0, 20.0, 30.0, 40.0])
	assert_vector(ProfilePanel.hatch_bounds(stations, 20.0, 20.0)).is_equal(Vector2(10.0, 30.0))
	assert_bool(ProfilePanel.is_idealised([{"alignment_id": "a", "profile_idealised": true}], "a")).is_true()
	assert_bool(ProfilePanel.is_idealised([{"alignment_id": "a"}], "a")).is_false()
	assert_bool(ProfilePanel.is_idealised([{"alignment_id": "a", "profile_idealised": true}], "b")).is_false()


# --- the panel, its states and its fetch -------------------------------------------------------------------------------

func test_cursor_moves_the_readout_and_there_is_no_line_without_playback() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	assert_bool(panel.has_cursor_line()).is_false()
	panel.set_cursor(13005.0)
	assert_bool(panel.has_cursor_line()).is_true()
	assert_str(panel.readout_label("station").text).is_equal("km 13.005")
	assert_str(panel.readout_label("rail").text).is_equal("Rail 175.00 m")
	assert_str(panel.readout_label("ground").text).is_equal("Ground 192.00 m")
	assert_str(panel.readout_label("difference").text).is_equal("Ground − rail +17.00 m")
	assert_str(panel.readout_label("source").text).is_equal("Source ČÚZK DMR 5G")
	panel.set_cursor(13015.0)
	assert_str(panel.readout_label("ground").text).is_equal("Ground no ground data")
	panel.set_cursor(13020.0)
	assert_str(panel.readout_label("ground").text).is_equal("Ground no ground data")
	assert_str(panel.readout_label("difference").text).is_equal("Ground − rail —")
	assert_str(panel.readout_label("source").text).is_equal("Source none")


func test_no_alignment_and_no_terrain_messages() -> void:
	Session.set_project_info({})
	var panel := _make_panel()
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.NO_ALIGNMENT)
	assert_str(panel.status_label().text).is_equal(ProfilePanel.NO_ALIGNMENT_TEXT)
	assert_bool(panel.retry_button().visible).is_false()

	_reply = "all_no_ground"
	_set_project(false)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)
	assert_str(panel.status_label().text).is_equal(ProfilePanel.NO_TERRAIN_TEXT)
	assert_bool(panel.status_label().visible).is_true()


func test_idealised_note_follows_the_alignment_flag() -> void:
	_set_project(true)
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	assert_bool(panel.idealised_note().visible).is_true()
	assert_str(panel.idealised_note().text).is_equal("Vertical profile is idealised in this dataset")

	_set_project(false)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_bool(panel.idealised_note().visible).is_false()


func test_profile_cache_clears_on_terrain_import() -> void:
	var first := await Session.fetch_ground_profile(_ALIGNMENT, 10.0)
	assert_bool(first.is_empty()).is_false()
	assert_int(_override_calls).is_equal(1)
	assert_int(first["terrain_revision"]).is_equal(7)
	assert_array(Array(first["source_names"])).is_equal(["ČÚZK DMR 5G"])

	await Session.fetch_ground_profile(_ALIGNMENT, 10.0)
	assert_int(_override_calls).append_failure_message("a second fetch of the same profile went to the backend").is_equal(1)

	EventBus.sources_changed.emit("imagery")
	await Session.fetch_ground_profile(_ALIGNMENT, 10.0)
	assert_int(_override_calls).append_failure_message("an imagery import cleared the profile cache").is_equal(1)

	EventBus.sources_changed.emit("terrain")
	await Session.fetch_ground_profile(_ALIGNMENT, 10.0)
	assert_int(_override_calls).append_failure_message("a terrain import did not clear the profile cache").is_equal(2)


func test_a_failed_fetch_shows_the_error_and_a_retry() -> void:
	_reply = "err"
	_set_project(false)
	var panel := _make_panel()
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.FAILED)
	assert_bool(panel.retry_button().visible).is_true()
	assert_bool(panel.status_label().text.begins_with(ProfilePanel.FAILED_TEXT)).is_true()
	assert_bool(panel.status_label().text.contains("work budget")).is_true()
	assert_bool(Session.ground_profile_error().contains("work budget")).is_true()

	_reply = "ok"
	panel.retry_button().pressed.emit()
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)
	assert_bool(panel.retry_button().visible).is_false()


func test_the_panel_fetches_only_while_it_is_visible() -> void:
	_set_project(false)
	var host := Control.new()
	host.visible = false
	add_child(host)
	auto_free(host)
	var panel := ProfilePanel.new()
	host.add_child(panel)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(_override_calls).append_failure_message("a hidden profile tab fetched the profile").is_equal(0)

	host.visible = true
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(_override_calls).is_equal(1)
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)


func test_window_and_exaggeration_selectors_redraw_and_keep_their_values() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	assert_int(panel.window_index()).is_equal(ProfilePanel.DEFAULT_WINDOW_INDEX)
	assert_float(panel.exaggeration()).is_equal_approx(10.0, 1e-9)
	panel.window_choice().item_selected.emit(0)
	assert_float(panel.window_width_m()).is_equal_approx(1000.0, 1e-9)
	panel.exaggeration_choice().item_selected.emit(1)
	assert_float(panel.exaggeration()).is_equal_approx(5.0, 1e-9)
	assert_str(panel.exaggeration_choice().get_item_text(2)).is_equal("10×")


# --- the shell's bottom tabs, in both presets --------------------------------------------------------------------------

func test_tabs_switch_and_profile_grows_the_bottom_slot() -> void:
	var shell := await _make_shell()
	var timeline := shell.panel(UiShell.PANEL_TIMELINE)
	for preset_id: StringName in [UiPreset.ENGINEERING, UiPreset.SANDBOX]:
		shell.apply_preset(preset_id)
		shell.select_bottom_tab(&"timeline")
		await get_tree().process_frame
		await get_tree().process_frame
		var timeline_h := timeline.size.y
		var expected_timeline := UiShell.DOCK_BOTTOM_H if preset_id == UiPreset.ENGINEERING else UiShell.FLOAT_PILL_H
		assert_float(timeline_h).append_failure_message("%s: the timeline tab is %f px" % [preset_id, timeline_h]).is_equal_approx(expected_timeline, 0.5)
		assert_bool(shell.profile_panel().is_visible_in_tree()).is_false()

		shell.select_bottom_tab(&"profile")
		await get_tree().process_frame
		await get_tree().process_frame
		assert_float(timeline.size.y).append_failure_message("%s: the profile tab is %f px" % [preset_id, timeline.size.y]).is_equal_approx(UiShell.PROFILE_H, 0.5)
		assert_bool(shell.profile_panel().is_visible_in_tree()).is_true()
		assert_bool(timeline.get_node("Column/Header").visible).is_false()
		assert_str(String(shell.bottom_tab())).is_equal("profile")
	shell.select_bottom_tab(&"timeline")


func test_the_profile_drawing_is_at_least_150_px_tall_and_the_readout_does_not_cover_it() -> void:
	var shell := await _make_shell()
	shell.profile_panel().show_profile(_profile(), _ALIGNMENT)
	for preset_id: StringName in [UiPreset.ENGINEERING, UiPreset.SANDBOX]:
		shell.apply_preset(preset_id)
		shell.select_bottom_tab(&"profile")
		await get_tree().process_frame
		await get_tree().process_frame
		var panel := shell.profile_panel()
		var plot := panel.plot_rect()
		assert_float(plot.size.y).append_failure_message("%s: the drawing is %f px tall" % [preset_id, plot.size.y]).is_greater_equal(150.0)
		var global_plot := Rect2(panel.get_global_rect().position + plot.position, plot.size)
		var readout := panel.readout_column().get_global_rect()
		assert_bool(global_plot.intersects(readout)).append_failure_message("%s: the readout covers the drawing" % preset_id).is_false()
	shell.select_bottom_tab(&"timeline")


func test_the_profile_colours_come_from_both_themes() -> void:
	var shell := await _make_shell()
	for preset_id: StringName in [UiPreset.ENGINEERING, UiPreset.SANDBOX]:
		shell.apply_preset(preset_id)
		await get_tree().process_frame
		for item: String in ["rail", "ground", "cut", "fill", "no_ground", "grid", "cursor", "formation", "background"]:
			assert_bool(shell.profile_panel().has_theme_color(StringName(item), &"ProfilePanel")).append_failure_message(
				"%s has no ProfilePanel colour %s" % [preset_id, item]
			).is_true()
		# The tab bar's selected colour comes from its own theme type. A colour the theme does not give is the engine's white, so
		# this asks the theme, not the value.
		assert_bool(shell.bottom_tabs().has_theme_color(&"font_selected_color", &"TabContainer")).append_failure_message(
			"%s: the tab bar's selected colour comes from no theme" % preset_id
		).is_true()


# --- the review decisions (T-137 B fix round) -----------------------------------------------------------------------------

## Decision 1: the panel keeps hearing its events after the shell moves it with a preset switch.
func test_the_panel_hears_events_after_a_preset_switch() -> void:
	var shell := await _make_shell()
	_set_project(false)
	shell.select_bottom_tab(&"profile")
	await get_tree().process_frame
	await get_tree().process_frame
	var panel := shell.profile_panel()
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)

	shell.apply_preset(UiPreset.SANDBOX)
	await get_tree().process_frame
	var before := _override_calls
	EventBus.sources_changed.emit("terrain")
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(_override_calls).append_failure_message("after the switch to Sandbox, a terrain import did not refetch").is_equal(before + 1)

	shell.apply_preset(UiPreset.ENGINEERING)
	await get_tree().process_frame
	before = _override_calls
	EventBus.sources_changed.emit("terrain")
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(_override_calls).append_failure_message("after the switch back, a terrain import did not refetch").is_equal(before + 1)

	Session.set_project_info({"project_id": "t2", "alignments": [_entry("second_line")]})
	await get_tree().process_frame
	assert_str(panel.alignment_id()).append_failure_message("a new project after a switch did not change the alignment").is_equal("second_line")
	shell.select_bottom_tab(&"timeline")


## Decision 2: a fetch that ends after the project has closed is not drawn.
func test_a_fetch_that_ends_after_the_project_closes_is_dropped() -> void:
	_delay_s = 0.3
	Session.set_ground_profile_request_override(Callable(self, "_delayed"))
	_set_project(false)
	var panel := _make_panel()
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.LOADING)
	Session.set_project_info({})
	await get_tree().create_timer(0.6).timeout
	assert_int(panel.state()).append_failure_message("the profile of a closed project was drawn").is_equal(ProfilePanel.State.NO_ALIGNMENT)
	assert_bool(panel.profile().is_empty()).is_true()


## Decision 2: a fetch for the alignment the project replaced is not drawn under the new alignment's id.
func test_a_fetch_for_a_replaced_alignment_is_not_drawn_under_the_new_id() -> void:
	_delay_s = 0.3
	Session.set_ground_profile_request_override(Callable(self, "_delayed"))
	_set_project(false)
	var host := Control.new()
	add_child(host)
	auto_free(host)
	var panel := ProfilePanel.new()
	panel.custom_minimum_size = Vector2(900.0, 240.0)
	host.add_child(panel)
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.LOADING)
	# Hidden, so the replacement starts no fetch of its own; the first fetch is still running when the alignment changes.
	host.visible = false
	await get_tree().process_frame
	Session.set_project_info({"project_id": "t2", "alignments": [_entry("second_line")]})
	await get_tree().create_timer(0.6).timeout
	assert_str(panel.alignment_id()).is_equal("second_line")
	assert_bool(panel.profile().is_empty()).append_failure_message("the old alignment's profile is drawn under the new id").is_true()
	assert_bool(panel.state() != ProfilePanel.State.READY).is_true()


## Decision 3: the controls, the legend and every drawn text stay clear of the readout column, in both presets, and the drawing
## keeps its height at 1600 x 900 (150 px) and at 1280 x 720 (110 px).
func test_the_controls_and_labels_stay_clear_of_the_readout_at_1600_by_900() -> void:
	await _check_readout_clearance(Vector2(1600.0, 900.0), 150.0)


func test_the_controls_and_labels_stay_clear_of_the_readout_at_1280_by_720() -> void:
	await _check_readout_clearance(Vector2(1280.0, 720.0), 110.0)


func _check_readout_clearance(window_size: Vector2, min_plot_h: float) -> void:
	var shell := await _make_shell(window_size)
	var panel := shell.profile_panel()
	panel.show_profile(_wide_profile(1800, 100, 120), _ALIGNMENT)
	panel.set_window_index(0)
	panel.set_cursor(14100.0)
	for preset_id: StringName in [UiPreset.ENGINEERING, UiPreset.SANDBOX]:
		shell.apply_preset(preset_id)
		shell.select_bottom_tab(&"profile")
		await get_tree().process_frame
		await get_tree().process_frame
		await get_tree().process_frame
		var origin := panel.get_global_rect().position
		var readout := panel.readout_column().get_global_rect()
		var controls: Array[Rect2] = [panel.window_choice().get_global_rect(), panel.exaggeration_choice().get_global_rect()]
		if panel.factor_label().visible:
			controls.append(panel.factor_label().get_global_rect())
		for item: Control in panel.legend_items():
			controls.append(item.get_global_rect())
		for rect: Rect2 in controls:
			assert_bool(rect.intersects(readout)).append_failure_message("%s: a control at %s reaches the readout column" % [preset_id, rect]).is_false()
		var texts := panel.drawn_texts()
		var strings := panel.drawn_strings()
		assert_int(texts.size()).is_greater(0)
		for k in texts.size():
			var global := Rect2(origin + texts[k].position, texts[k].size)
			assert_bool(global.intersects(readout)).append_failure_message("%s: the text '%s' reaches the readout column" % [preset_id, strings[k]]).is_false()
			for other in range(k + 1, texts.size()):
				assert_bool(texts[k].grow(1.0).intersects(texts[other])).append_failure_message(
					"%s: the texts '%s' and '%s' overlap" % [preset_id, strings[k], strings[other]]
				).is_false()
		var plot := panel.plot_rect()
		assert_float(plot.size.y).append_failure_message("%s: the drawing is %f px tall" % [preset_id, plot.size.y]).is_greater_equal(min_plot_h)
	var third: Control = panel.legend_items()[2]
	assert_str(third.tooltip_text).is_equal(ProfilePanel.LEGEND_NO_GROUND_TOOLTIP)
	assert_str((third.get_child(1) as Label).text).is_equal("No ground data")
	shell.select_bottom_tab(&"timeline")


## Decision 4: about one row per pixel column of the plot, with the run ends kept.
func test_sampled_rows_are_about_one_per_column_and_keep_the_run_ends() -> void:
	var profile := _wide_profile(1800, 100, 120)
	var states := ProfilePanel.row_states(profile["flags"], profile["ground_h"], profile["cut_area"], profile["fill_area"])
	var ends := ProfilePanel.run_end_rows(states, 0, 1799)
	assert_int(ends.size()).is_greater(0)
	for plot_w: float in [300.0, 1000.0]:
		var drawn := ProfilePanel.sample_rows(states, 0, 1799, plot_w)
		var bound := int(plot_w) + 1 + ends.size()
		assert_int(drawn.size()).append_failure_message("%d rows drawn for a %d px plot" % [drawn.size(), int(plot_w)]).is_less_equal(bound)
		assert_int(drawn[0]).is_equal(0)
		assert_int(drawn[drawn.size() - 1]).is_equal(1799)
		for row: int in ends:
			assert_bool(drawn.has(row)).append_failure_message("the run end %d was not kept" % row).is_true()
	# A window of 101 rows in 1000 px is drawn whole.
	assert_int(ProfilePanel.sample_rows(states, 500, 600, 1000.0).size()).is_equal(101)


## Decision 4: one polygon per run of cut, not per segment.
func test_each_run_of_cut_is_one_polygon() -> void:
	var profile := _wide_profile(1800, 100, 120)
	var stations: PackedFloat64Array = profile["station"]
	var ground: PackedFloat64Array = profile["ground_h"]
	var flags: PackedByteArray = profile["flags"]
	var expected := 0
	var inside := false
	for i in stations.size():
		var cut_here := flags[i] == 0 and ground[i] > 175.0 - 0.8
		if cut_here and not inside:
			expected += 1
		inside = cut_here
	assert_int(expected).is_greater_equal(20)
	var polygons := ProfilePanel.band_runs(stations, ground, flags, profile["rail_h"], 0.8, 0, stations.size() - 1, true)
	assert_int(polygons.size()).append_failure_message("%d polygons for %d runs of cut" % [polygons.size(), expected]).is_equal(expected)


## Decision 5: a formation crossing within 0.1 m of a row gives no polygon the engine would refuse: each triangulates, and none
## has a repeated vertex or a sliver of area.
func test_degenerate_crossings_give_no_polygon_the_engine_refuses() -> void:
	var offsets := [0.001, -1.0, 0.5, -0.5, 0.0002, 1.0, -0.00005, 0.8, -0.3, 0.00001, 0.4, -0.2, 0.0, 0.6]
	var formation := 175.0 - 0.8
	var stations := PackedFloat64Array()
	var rail := PackedFloat64Array()
	var ground := PackedFloat64Array()
	var flags := PackedByteArray()
	for i in offsets.size():
		stations.append(10.0 * i)
		rail.append(175.0)
		ground.append(formation + float(offsets[i]))
		flags.append(0)
	var checked := 0
	for i in offsets.size() - 1:
		for cut: bool in [true, false]:
			for piece: Variant in ProfilePanel.band_polygons(stations[i], ground[i], stations[i + 1], ground[i + 1], formation, formation, cut):
				_assert_drawable(piece)
				checked += 1
	for cut: bool in [true, false]:
		for polygon: Variant in ProfilePanel.band_runs(stations, ground, flags, rail, 0.8, 0, offsets.size() - 1, cut):
			_assert_drawable(polygon)
			checked += 1
	assert_int(checked).is_greater(0)


func _assert_drawable(polygon: PackedVector2Array) -> void:
	assert_int(Geometry2D.triangulate_polygon(polygon).size()).append_failure_message("the engine would refuse %s" % str(polygon)).is_greater(0)
	var twice := 0.0
	for k in polygon.size():
		var a := polygon[k]
		var b := polygon[(k + 1) % polygon.size()]
		assert_bool(a.distance_to(b) > 0.0).append_failure_message("a repeated vertex in %s" % str(polygon)).is_true()
		twice += a.x * b.y - b.x * a.y
	assert_bool(absf(twice) * 0.5 >= 1e-6).append_failure_message("a sliver of area in %s" % str(polygon)).is_true()


## Decision 6: the panel takes the bound playback's alignment, follows its station, and redraws only for a move of a pixel or more.
func test_the_panel_follows_a_bound_playback_and_redraws_only_for_a_pixel() -> void:
	Session.set_project_info({"project_id": "t", "alignments": [_entry(_ALIGNMENT), _entry("second_line")]})
	var playback := FakePlayback.new()
	auto_free(playback)
	playback.id = "second_line"
	playback.station = 13005.0
	var panel := _make_panel()
	await get_tree().process_frame
	assert_str(panel.alignment_id()).is_equal(_ALIGNMENT)
	panel.bind_playback(playback)
	assert_str(panel.alignment_id()).append_failure_message("the panel did not take the playback's alignment").is_equal("second_line")
	await get_tree().process_frame
	await get_tree().process_frame
	await get_tree().process_frame
	assert_float(panel.cursor_station()).is_equal_approx(13005.0, 1e-9)
	assert_str(panel.readout_label("station").text).is_equal("km 13.005")

	var draws := panel.draw_count()
	playback.station = 13005.01
	await get_tree().process_frame
	await get_tree().process_frame
	assert_int(panel.draw_count()).append_failure_message("a move of 0.01 m redrew the profile").is_equal(draws)
	assert_str(panel.readout_label("station").text).is_equal("km 13.005")

	playback.station = 13009.0
	await get_tree().process_frame
	await get_tree().process_frame
	assert_str(panel.readout_label("station").text).append_failure_message("the readout did not follow the playback").is_equal("km 13.009")
	assert_int(panel.draw_count()).is_greater(draws)


## Decision 7: "Fit" fits the data with a margin, and the label beside the selector names the factor it gives.
func test_fit_fits_the_data_with_a_margin_and_its_factor_is_labelled() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	panel.set_exaggeration_index(ProfilePanel.FIT_INDEX)
	await get_tree().process_frame
	assert_float(panel.exaggeration()).is_equal_approx(0.0, 1e-9)
	# The data run from the formation at 174.2 m to the ground at 192 m: 17.8 m, with a tenth of that as margin on each side.
	assert_float(panel.vertical_span_m()).is_equal_approx(17.8 * 1.2, 1e-6)
	var plot := panel.plot_rect()
	assert_bool(panel.factor_label().visible).is_true()
	assert_str(panel.factor_label().text).is_equal(ProfilePanel.factor_text(0.0, 17.8 * 1.2, 40.0, plot.size.x, plot.size.y))
	assert_bool(panel.factor_label().text.begins_with("≈")).is_true()
	# At 10x the fitted range is the larger one, so the label shows the factor that applies.
	panel.set_exaggeration_index(ProfilePanel.DEFAULT_EXAGGERATION_INDEX)
	await get_tree().process_frame
	assert_bool(panel.factor_label().visible).is_true()
	assert_str(panel.factor_label().text).is_equal(ProfilePanel.factor_text(10.0, 17.8 * 1.2, 40.0, plot.size.x, plot.size.y))


func test_fit_span_is_at_least_two_metres_and_the_label_names_only_an_override() -> void:
	assert_float(ProfilePanel.vertical_range(174.2, 175.0, 40.0, 500.0, 200.0, 0.0).y - ProfilePanel.vertical_range(174.2, 175.0, 40.0, 500.0, 200.0, 0.0).x).is_equal_approx(2.0, 1e-9)
	# 10x over a 5 km window of a 1000 px plot 200 px high shows a range of 100 m; a fitted range of 100 m or less is not labelled.
	assert_str(ProfilePanel.factor_text(10.0, 100.0, 5000.0, 1000.0, 200.0)).is_equal("")
	assert_str(ProfilePanel.factor_text(10.0, 110.0, 5000.0, 1000.0, 200.0)).is_equal("≈ 9.1×")
	assert_str(ProfilePanel.factor_text(0.0, 110.0, 5000.0, 1000.0, 200.0)).is_equal("≈ 9.1×")
	assert_str(ProfilePanel.factor_text(0.0, 0.0, 5000.0, 1000.0, 200.0)).is_equal("")


func test_the_selectors_have_their_tooltips_and_fit_is_offered() -> void:
	var panel := _make_panel()
	assert_str(panel.window_choice().tooltip_text).is_equal("Window width")
	assert_str(panel.exaggeration_choice().tooltip_text).is_equal("Vertical exaggeration")
	assert_str(panel.exaggeration_choice().get_item_text(ProfilePanel.FIT_INDEX)).is_equal("Fit")


## Decision 8: each axis takes the decimals its tick step needs.
func test_tick_decimals_follow_the_step() -> void:
	assert_int(ProfilePanel.tick_decimals(20.0)).is_equal(0)
	assert_int(ProfilePanel.tick_decimals(0.5)).is_equal(1)
	assert_int(ProfilePanel.tick_decimals(0.05)).is_equal(2)
	assert_int(ProfilePanel.tick_decimals(1000.0)).is_equal(0)
	assert_int(ProfilePanel.tick_decimals(2.5)).append_failure_message("a 2.5 m step shows 2.5, one decimal").is_equal(1)
	assert_int(ProfilePanel.tick_decimals(25.0)).append_failure_message("a 25 m step shows 25, no decimal").is_equal(0)
	assert_float(ProfilePanel.tick_step(160.0, 280.0, 6)).is_equal_approx(20.0, 1e-9)
	assert_int(ProfilePanel.tick_decimals(ProfilePanel.tick_step(180.0, 183.0, 6))).is_equal(1)
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	panel.set_exaggeration_index(ProfilePanel.FIT_INDEX)
	await get_tree().process_frame
	# Every metre label carries the decimals of the one step: a step of 2.5 m shows "172.5", never "172".
	var labels := _metre_labels(panel)
	assert_int(labels.size()).append_failure_message("the metre labels are %s" % str(labels)).is_greater_equal(4)
	var decimals := -1
	for label: String in labels:
		var here := label.length() - label.find(".") - 1 if label.contains(".") else 0
		assert_int(decimals if decimals >= 0 else here).append_failure_message("mixed decimals in %s" % str(labels)).is_equal(here)
		decimals = here


## The metre labels of the last drawing: the numeric strings whose rectangles lie left of the plot (the km labels lie below it).
func _metre_labels(panel: ProfilePanel) -> PackedStringArray:
	var out := PackedStringArray()
	var plot := panel.plot_rect()
	var texts := panel.drawn_texts()
	var strings := panel.drawn_strings()
	for k in strings.size():
		if strings[k].is_valid_float() and texts[k].position.x < plot.position.x:
			out.append(strings[k])
	return out


## Item 11: at the 5 km window and 10x, a plot 150 px tall shows at least four metre labels (the step is 20 m or 25 m, not 50 m).
func test_the_metre_axis_has_at_least_four_labels_in_a_150_px_plot() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_wide_profile(1800, 100, 120), _ALIGNMENT)
	panel.set_window_index(1)
	panel.set_exaggeration_index(ProfilePanel.DEFAULT_EXAGGERATION_INDEX)
	await get_tree().process_frame
	await get_tree().process_frame
	var plot := panel.plot_rect()
	var height := panel.size.y + 150.0 - plot.size.y
	panel.custom_minimum_size = Vector2(panel.custom_minimum_size.x, height)
	panel.size = Vector2(panel.size.x, height)
	await get_tree().process_frame
	await get_tree().process_frame
	await get_tree().process_frame
	plot = panel.plot_rect()
	assert_float(plot.size.y).append_failure_message("the plot is %f px tall, not 150" % plot.size.y).is_equal_approx(150.0, 1.0)
	var labels := _metre_labels(panel)
	assert_int(labels.size()).append_failure_message("the metre labels are %s" % str(labels)).is_greater_equal(4)


## Decision 8: the metre unit sits clear of the top tick label.
func test_the_metre_unit_does_not_touch_the_top_tick() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_profile(), _ALIGNMENT)
	panel.set_exaggeration_index(ProfilePanel.FIT_INDEX)
	await get_tree().process_frame
	var strings := panel.drawn_strings()
	var texts := panel.drawn_texts()
	var unit := strings.find("m")
	assert_int(unit).is_greater_equal(0)
	for k in strings.size():
		if strings[k].is_valid_float():
			assert_bool(texts[unit].grow(2.0).intersects(texts[k])).append_failure_message("the unit touches the tick %s" % strings[k]).is_false()


## Decision 9: the difference reads with a true minus sign.
func test_the_difference_reads_with_a_true_minus() -> void:
	assert_str(ProfilePanel.format_difference(-0.4)).is_equal("−0.40 m")
	assert_str(ProfilePanel.format_difference(17.36)).is_equal("+17.36 m")
	var profile := _profile()
	profile["ground_h"] = PackedFloat64Array([174.6, 192.0, NAN, 190.0, 190.0])
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(profile, _ALIGNMENT)
	panel.set_cursor(13000.0)
	assert_str(panel.readout_label("difference").text).is_equal("Ground − rail −0.40 m")
	assert_str(str(panel.summary()["difference_text"])).is_equal("Ground − rail −0.40 m")


## Decision 9: the no-ground colour reaches 4.5:1 on the Engineering panel background.
func test_the_no_ground_colour_reaches_4_5_to_1_on_the_engineering_background() -> void:
	var theme := load("res://ui/theme/engineering_theme.tres") as Theme
	assert_bool(theme.has_color(&"no_ground", &"ProfilePanel")).is_true()
	var ink := theme.get_color(&"no_ground", &"ProfilePanel")
	var paper := theme.get_color(&"background", &"ProfilePanel")
	var ratio := _contrast(ink, paper)
	assert_float(ratio).append_failure_message("the no-ground text is %.2f:1 on the panel" % ratio).is_greater_equal(4.5)


## Decision 9: a readout value the column cuts short ends in an ellipsis, and its full text is the tooltip.
func test_long_readout_values_end_in_an_ellipsis_with_a_tooltip() -> void:
	var profile := _profile()
	var long_name := "ČÚZK DMR 5G terrain surface for the whole Kralupy corridor, including the cutting near kilometre thirteen"
	profile["source_names"] = [long_name]
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(profile, _ALIGNMENT)
	panel.set_cursor(13005.0)
	var label := panel.readout_label("source")
	assert_int(label.text_overrun_behavior).is_equal(TextServer.OVERRUN_TRIM_ELLIPSIS)
	assert_str(label.tooltip_text).is_equal("Source " + long_name)


## Decision 2: a terrain import while the Profile tab is hidden drops the fetch in flight, so the result of the old terrain is not
## drawn; the tab fetches again when it is shown.
func test_a_terrain_import_while_hidden_drops_the_fetch_in_flight() -> void:
	_delay_s = 0.3
	Session.set_ground_profile_request_override(Callable(self, "_delayed"))
	_set_project(false)
	var host := Control.new()
	add_child(host)
	auto_free(host)
	var panel := ProfilePanel.new()
	panel.custom_minimum_size = Vector2(900.0, 240.0)
	host.add_child(panel)
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.LOADING)
	host.visible = false
	await get_tree().process_frame
	EventBus.sources_changed.emit("terrain")
	await get_tree().create_timer(0.6).timeout
	assert_bool(panel.profile().is_empty()).append_failure_message("the fetch of the old terrain was drawn").is_true()
	host.visible = true
	await get_tree().create_timer(0.6).timeout
	assert_int(panel.state()).is_equal(ProfilePanel.State.READY)
	assert_int(_override_calls).is_equal(2)


## Decision 2: binding a playback of another alignment, while the panel is hidden and a fetch runs, drops that fetch.
func test_binding_another_alignment_drops_a_fetch_in_flight() -> void:
	_delay_s = 0.3
	Session.set_ground_profile_request_override(Callable(self, "_delayed"))
	Session.set_project_info({"project_id": "t", "alignments": [_entry(_ALIGNMENT), _entry("second_line")]})
	var host := Control.new()
	add_child(host)
	auto_free(host)
	var panel := ProfilePanel.new()
	panel.custom_minimum_size = Vector2(900.0, 240.0)
	host.add_child(panel)
	await get_tree().process_frame
	assert_int(panel.state()).is_equal(ProfilePanel.State.LOADING)
	host.visible = false
	await get_tree().process_frame
	var playback := FakePlayback.new()
	auto_free(playback)
	playback.id = "second_line"
	panel.bind_playback(playback)
	await get_tree().create_timer(0.6).timeout
	assert_str(panel.alignment_id()).is_equal("second_line")
	assert_bool(panel.profile().is_empty()).append_failure_message("the first alignment's profile is drawn for the bound one").is_true()
	assert_bool(panel.state() != ProfilePanel.State.READY).is_true()


## Decision 3: a no-ground label too wide for its span is placed beside the span, and never runs past it into the readout. Here the
## span, 150 m, ends at the window's right edge, about 90 px wide: the label (about 190 px) does not fit inside it.
func test_a_span_label_too_wide_for_its_span_is_placed_beside_it() -> void:
	var shell := await _make_shell(Vector2(1600.0, 900.0))
	var panel := shell.profile_panel()
	panel.show_profile(_wide_profile(1800, 135, 150), _ALIGNMENT)
	panel.set_window_index(0)
	panel.set_cursor(14000.0)
	shell.apply_preset(UiPreset.ENGINEERING)
	shell.select_bottom_tab(&"profile")
	await get_tree().process_frame
	await get_tree().process_frame
	await get_tree().process_frame
	var origin := panel.get_global_rect().position
	var readout := panel.readout_column().get_global_rect()
	var strings := panel.drawn_strings()
	var texts := panel.drawn_texts()
	var found := 0
	for k in strings.size():
		if strings[k] == ProfilePanel.NO_GROUND_LABEL:
			found += 1
			var global := Rect2(origin + texts[k].position, texts[k].size)
			assert_bool(global.intersects(readout)).append_failure_message("the no-ground label runs into the readout column").is_false()
	assert_int(found).is_greater(0)
	shell.select_bottom_tab(&"timeline")


## Decision 3: each no-ground label sits on a plate of the panel's background, and the plate covers the label's text.
func test_each_no_ground_label_sits_on_a_plate() -> void:
	var panel := _make_panel()
	await get_tree().process_frame
	panel.show_profile(_wide_profile(1800, 100, 120), _ALIGNMENT)
	panel.set_window_index(0)
	panel.set_cursor(14100.0)
	await get_tree().process_frame
	await get_tree().process_frame
	var strings := panel.drawn_strings()
	var texts := panel.drawn_texts()
	var plates := panel.drawn_plates()
	var found := 0
	for k in strings.size():
		if strings[k] == ProfilePanel.NO_GROUND_LABEL:
			found += 1
			var covered := false
			for plate: Rect2 in plates:
				covered = covered or plate.encloses(texts[k])
			assert_bool(covered).append_failure_message("no plate covers the no-ground label").is_true()
	assert_int(found).append_failure_message("no no-ground label was drawn").is_greater(0)


func _entry(alignment_id: String) -> Dictionary:
	return {
		"alignment_id": alignment_id, "name": alignment_id, "mode": "rail", "station_start": 13000.0,
		"station_end": 13040.0, "length": 40.0, "warnings": [], "profile_idealised": false,
	}
