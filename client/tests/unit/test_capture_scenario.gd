extends GdUnitTestSuite
## [Scenario] parser/validator and the pure statistics in [CaptureDriver] (T-125). No scene tree, no backend,
## no rendering: the capture itself needs a desktop GPU and is exercised by `tools/capture.ps1`.

const _VALID := """
{
  "project": "backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu",
  "resolution": [1280, 720],
  "ready_timeout_s": 45,
  "shots": [
    {"name": "orbit_overview", "camera": "orbit", "time_s": 10.5, "focus_station_m": 9000.0, "settle_frames": 12},
    {"name": "cab_2", "camera": "cab"}
  ],
  "perf": {"camera": "cab", "time_s": 5.0, "rate": 4.0, "duration_s": 10.0}
}
"""


func _shot(fields: String) -> Scenario:
	return Scenario.parse('{"project": "p.coypu", "shots": [%s]}' % fields)


func _assert_error_mentions(scenario: Scenario, fragment: String) -> void:
	var found := false
	for message in scenario.errors():
		if message.contains(fragment):
			found = true
	assert_bool(found).append_failure_message(
		"expected an error containing '%s', got %s" % [fragment, str(scenario.errors())]
	).is_true()
	assert_bool(scenario.is_valid()).is_false()


func test_valid_file_parses_every_field() -> void:
	var scenario := Scenario.parse(_VALID)
	assert_array(scenario.errors()).is_empty()
	assert_bool(scenario.is_valid()).is_true()
	assert_str(scenario.project).is_equal("backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu")
	assert_that(scenario.resolution).is_equal(Vector2i(1280, 720))
	assert_float(scenario.ready_timeout_s).is_equal(45.0)
	assert_int(scenario.shots.size()).is_equal(2)

	var first := scenario.shots[0]
	assert_str(first["name"]).is_equal("orbit_overview")
	assert_str(first["camera"]).is_equal("orbit")
	assert_float(first["time_s"]).is_equal(10.5)
	assert_float(first["focus_station_m"]).is_equal(9000.0)
	assert_int(first["settle_frames"]).is_equal(12)

	assert_bool(scenario.has_perf()).is_true()
	assert_str(scenario.perf["camera"]).is_equal("cab")
	assert_float(scenario.perf["time_s"]).is_equal(5.0)
	assert_float(scenario.perf["rate"]).is_equal(4.0)
	assert_float(scenario.perf["duration_s"]).is_equal(10.0)


func test_defaults_apply_when_optional_fields_are_absent() -> void:
	var scenario := _shot('{"name": "a", "camera": "wayside"}')
	assert_array(scenario.errors()).is_empty()
	assert_that(scenario.resolution).is_equal(Scenario.DEFAULT_RESOLUTION)
	assert_float(scenario.ready_timeout_s).is_equal(Scenario.DEFAULT_READY_TIMEOUT_S)
	assert_int(scenario.shots[0]["settle_frames"]).is_equal(Scenario.DEFAULT_SETTLE_FRAMES)
	assert_bool(scenario.shots[0].has("time_s")).is_false()
	assert_bool(scenario.shots[0].has("focus_station_m")).is_false()
	assert_bool(scenario.has_perf()).is_false()


func test_perf_defaults_time_and_rate() -> void:
	var scenario := Scenario.parse(
		'{"project": "p", "shots": [{"name": "a", "camera": "orbit"}], "perf": {"camera": "orbit", "duration_s": 3}}'
	)
	assert_array(scenario.errors()).is_empty()
	assert_float(scenario.perf["time_s"]).is_equal(0.0)
	assert_float(scenario.perf["rate"]).is_equal(Scenario.DEFAULT_PERF_RATE)


func test_invalid_json_is_an_error_not_a_crash() -> void:
	var scenario := Scenario.parse("{ not json")
	_assert_error_mentions(scenario, "not valid JSON")


func test_top_level_must_be_an_object() -> void:
	_assert_error_mentions(Scenario.parse("[1, 2]"), "top level")


func test_empty_text_is_an_error() -> void:
	_assert_error_mentions(Scenario.parse(""), "not valid JSON")


func test_missing_project_and_shots_are_errors() -> void:
	var scenario := Scenario.parse("{}")
	_assert_error_mentions(scenario, "project is required")
	_assert_error_mentions(scenario, "shots is required")


func test_empty_shot_list_is_an_error() -> void:
	_assert_error_mentions(Scenario.parse('{"project": "p", "shots": []}'), "at least one shot")


func test_unknown_camera_is_an_error() -> void:
	_assert_error_mentions(_shot('{"name": "a", "camera": "drone"}'), "unknown camera")


func test_missing_camera_is_an_error() -> void:
	_assert_error_mentions(_shot('{"name": "a"}'), "camera is required")


func test_duplicate_name_is_an_error() -> void:
	var scenario := _shot('{"name": "a", "camera": "orbit"}, {"name": "a", "camera": "cab"}')
	_assert_error_mentions(scenario, "duplicate name")


func test_malformed_names_are_errors() -> void:
	for bad_name in ["Upper", "has space", "dash-ed", "", "dot.png", "../escape"]:
		_assert_error_mentions(_shot('{"name": "%s", "camera": "orbit"}' % bad_name), "must match [a-z0-9_]+")
	_assert_error_mentions(_shot('{"name": 7, "camera": "orbit"}'), "must match [a-z0-9_]+")
	_assert_error_mentions(_shot('{"camera": "orbit"}'), "name is required")


func test_negative_values_are_errors() -> void:
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "time_s": -1}'), "time_s")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "focus_station_m": -5}'), "focus_station_m")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "settle_frames": -1}'), "settle_frames")
	_assert_error_mentions(Scenario.parse('{"project": "p", "shots": [{"name": "a", "camera": "orbit"}], "ready_timeout_s": 0}'), "ready_timeout_s")
	_assert_error_mentions(Scenario.parse('{"project": "p", "shots": [{"name": "a", "camera": "orbit"}], "resolution": [0, 900]}'), "resolution")


func test_wrong_types_are_errors() -> void:
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "time_s": "soon"}'), "time_s must be a number")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "time_s": true}'), "time_s must be a number")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "settle_frames": 2.5}'), "whole number")
	_assert_error_mentions(Scenario.parse('{"project": "p", "shots": "none"}'), "shots must be an array")
	_assert_error_mentions(Scenario.parse('{"project": 3, "shots": [{"name": "a", "camera": "orbit"}]}'), "project")
	_assert_error_mentions(Scenario.parse('{"project": "p", "shots": [{"name": "a", "camera": "orbit"}], "resolution": [1600]}'), "resolution")


func test_unknown_keys_are_errors_so_typos_do_not_pass_silently() -> void:
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "settle_frame": 5}'), "unknown key 'settle_frame'")
	_assert_error_mentions(
		Scenario.parse('{"project": "p", "shots": [{"name": "a", "camera": "orbit"}], "shot": []}'), "unknown key 'shot'"
	)


func test_orbit_adjustments_parse_and_need_the_orbit_camera() -> void:
	var scenario := _shot('{"name": "a", "camera": "orbit", "orbit_zoom_notches": -3, "orbit_drag_px": [10, -20.5]}')
	assert_array(scenario.errors()).is_empty()
	assert_int(scenario.shots[0]["orbit_zoom_notches"]).is_equal(-3)
	assert_that(scenario.shots[0]["orbit_drag_px"]).is_equal(Vector2(10.0, -20.5))

	_assert_error_mentions(_shot('{"name": "a", "camera": "cab", "orbit_zoom_notches": 2}'), "need the orbit camera")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "orbit_drag_px": [1]}'), "orbit_drag_px must be")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "orbit_drag_px": ["a", 1]}'), "orbit_drag_px[0]")
	_assert_error_mentions(_shot('{"name": "a", "camera": "orbit", "orbit_zoom_notches": 1.5}'), "whole number")


func test_every_problem_is_reported_in_one_pass() -> void:
	var scenario := _shot('{"name": "A", "camera": "drone", "time_s": -1}')
	assert_int(scenario.errors().size()).is_greater_equal(3)


func test_perf_errors() -> void:
	var base := '"project": "p", "shots": [{"name": "a", "camera": "orbit"}]'
	_assert_error_mentions(Scenario.parse('{%s, "perf": {"camera": "orbit"}}' % base), "duration_s is required")
	_assert_error_mentions(Scenario.parse('{%s, "perf": {"camera": "orbit", "duration_s": 0}}' % base), "duration_s")
	_assert_error_mentions(Scenario.parse('{%s, "perf": {"camera": "x", "duration_s": 2}}' % base), "unknown camera")
	_assert_error_mentions(Scenario.parse('{%s, "perf": {"camera": "orbit", "duration_s": 2, "rate": 99}}' % base), "rate")
	_assert_error_mentions(Scenario.parse('{%s, "perf": 5}' % base), "perf must be an object")


func test_time_beyond_the_run_is_an_error_once_the_run_is_known() -> void:
	var scenario := _shot('{"name": "a", "camera": "orbit", "time_s": 600}')
	assert_array(scenario.errors()).is_empty()
	scenario.check_against_run(true, 500.0)
	_assert_error_mentions(scenario, "beyond the run's duration")


func test_time_at_exactly_the_duration_is_allowed() -> void:
	var scenario := _shot('{"name": "a", "camera": "orbit", "time_s": 500}')
	assert_int(scenario.check_against_run(true, 500.0).size()).is_equal(0)
	assert_bool(scenario.is_valid()).is_true()


func test_time_or_cab_without_a_run_is_an_error() -> void:
	var timed := _shot('{"name": "a", "camera": "orbit", "time_s": 1}')
	timed.check_against_run(false, 0.0)
	_assert_error_mentions(timed, "needs a run")

	var cab := _shot('{"name": "a", "camera": "cab"}')
	cab.check_against_run(false, 0.0)
	_assert_error_mentions(cab, "cab camera needs a run")


func test_shots_without_time_are_fine_without_a_run() -> void:
	var scenario := _shot('{"name": "a", "camera": "orbit", "focus_station_m": 10}')
	assert_int(scenario.check_against_run(false, 0.0).size()).is_equal(0)


func test_perf_running_past_the_end_of_the_run_is_an_error() -> void:
	var scenario := Scenario.parse(_VALID)
	scenario.check_against_run(true, 1000.0)
	assert_bool(scenario.is_valid()).is_true()

	scenario = Scenario.parse(_VALID)
	scenario.check_against_run(true, 30.0)
	_assert_error_mentions(scenario, "perf:")


func test_the_shipped_default_scenario_is_valid_and_covers_the_required_views() -> void:
	var text := FileAccess.get_file_as_string("res://tools/capture/scenarios/kralupy_m2.json")
	assert_str(text).is_not_empty()
	var scenario := Scenario.parse(text)
	assert_array(scenario.errors()).is_empty()
	assert_int(scenario.shots.size()).is_greater_equal(5)
	assert_bool(scenario.has_perf()).is_true()
	var cameras := {}
	for shot in scenario.shots:
		cameras[shot["camera"]] = true
	for camera in Scenario.CAMERAS:
		assert_bool(cameras.has(camera)).append_failure_message("no %s shot" % camera).is_true()
	scenario.check_against_run(true, 1159.0)
	assert_array(scenario.errors()).is_empty()


const _BACKGROUND := Color(0.09, 0.1, 0.12)


func _filled(width: int, height: int, color: Color) -> Image:
	var image := Image.create_empty(width, height, false, Image.FORMAT_RGBA8)
	image.fill(color)
	return image


func _assert_finite(value: float) -> void:
	assert_bool(is_nan(value) or is_inf(value)).append_failure_message("value is NaN or infinite").is_false()


## `Image.fill` quantises the clear colour to 8 bits, exactly as the renderer does (23, 26, 31).
func _frame_problem_of(image: Image) -> String:
	var stats := CaptureDriver.luma_stats(image)
	var fraction := CaptureDriver.scene_fraction(image, _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)
	return Scenario.frame_problem(stats.x, stats.y, fraction)


func test_frame_problem_flags_empty_black_overexposed_and_flat_frames() -> void:
	assert_str(Scenario.frame_problem(0.10, 0.015, 0.0032)).is_empty()
	assert_str(Scenario.frame_problem(0.10, 0.0, 0.0)).contains("empty scene")
	assert_str(Scenario.frame_problem(0.10, 0.04, Scenario.SCENE_FRACTION_MIN / 2.0)).contains("empty scene")
	assert_str(Scenario.frame_problem(0.01, 0.05, 1.0)).contains("black")
	assert_str(Scenario.frame_problem(0.99, 0.05, 1.0)).contains("overexposed")
	assert_str(Scenario.frame_problem(0.40, 0.001, 1.0)).contains("flat")
	assert_str(Scenario.frame_problem(NAN, 0.3, 0.5)).is_not_empty()
	assert_str(Scenario.frame_problem(0.4, NAN, 0.5)).is_not_empty()
	assert_str(Scenario.frame_problem(0.4, 0.3, NAN)).is_not_empty()


func test_an_all_background_image_fails_the_frame_check() -> void:
	var image := _filled(1600, 900, _BACKGROUND)
	assert_float(CaptureDriver.scene_fraction(image, _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(0.0)
	assert_str(_frame_problem_of(image)).contains("empty scene")


func test_an_all_black_image_fails_the_frame_check() -> void:
	var image := _filled(64, 64, Color.BLACK)
	assert_float(CaptureDriver.scene_fraction(image, _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(1.0)
	assert_str(_frame_problem_of(image)).contains("black")


func test_an_all_white_image_fails_the_frame_check() -> void:
	assert_str(_frame_problem_of(_filled(64, 64, Color.WHITE))).contains("overexposed")


func test_a_sparse_thin_line_like_the_orbit_overview_passes_the_frame_check() -> void:
	var image := _filled(1600, 900, _BACKGROUND)
	var track := Color(0.45, 0.42, 0.38)
	for x in 1600:
		var y := 700 - int(x * 0.4)
		for dy in 3:
			image.set_pixel(x, y + dy, track)
	var fraction := CaptureDriver.scene_fraction(image, _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)
	assert_float(fraction).is_between(0.003, 0.004)
	assert_str(_frame_problem_of(image)).is_empty()


func test_scene_fraction_ignores_differences_within_the_tolerance() -> void:
	var near := _BACKGROUND + Color(0.01, -0.01, 0.01, 0.0)
	assert_float(CaptureDriver.scene_fraction(_filled(8, 8, near), _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(0.0)
	var far := _BACKGROUND + Color(0.0, 0.0, 0.1, 0.0)
	assert_float(CaptureDriver.scene_fraction(_filled(8, 8, far), _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(1.0)


func test_scene_fraction_counts_the_share_of_differing_pixels() -> void:
	var image := _filled(8, 8, _BACKGROUND)
	image.fill_rect(Rect2i(0, 0, 2, 8), Color.WHITE)
	assert_float(CaptureDriver.scene_fraction(image, _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(0.25)
	assert_float(CaptureDriver.scene_fraction(Image.new(), _BACKGROUND, Scenario.BACKGROUND_TOLERANCE)).is_equal(0.0)


func test_luma_stats_of_flat_and_split_images() -> void:
	var black_stats := CaptureDriver.luma_stats(_filled(8, 8, Color.BLACK))
	_assert_finite(black_stats.x)
	_assert_finite(black_stats.y)
	assert_float(black_stats.x).is_equal_approx(0.0, 1e-6)
	assert_float(black_stats.y).is_equal_approx(0.0, 1e-6)

	var white_stats := CaptureDriver.luma_stats(_filled(8, 8, Color.WHITE))
	_assert_finite(white_stats.x)
	assert_float(white_stats.x).is_equal_approx(1.0, 1e-3)

	var split := _filled(8, 8, Color.BLACK)
	split.fill_rect(Rect2i(0, 0, 4, 8), Color.WHITE)
	var split_stats := CaptureDriver.luma_stats(split)
	_assert_finite(split_stats.x)
	_assert_finite(split_stats.y)
	assert_float(split_stats.x).is_equal_approx(0.5, 1e-3)
	assert_float(split_stats.y).is_equal_approx(0.5, 1e-3)
	assert_str(Scenario.frame_problem(split_stats.x, split_stats.y, 0.5)).is_empty()


func test_luma_stats_of_an_empty_image_is_zero_not_nan() -> void:
	var stats := CaptureDriver.luma_stats(Image.new())
	_assert_finite(stats.x)
	_assert_finite(stats.y)
	assert_that(stats).is_equal(Vector2.ZERO)


func test_luma_stats_weights_green_above_blue() -> void:
	var green := CaptureDriver.luma_stats(_filled(2, 2, Color(0, 1, 0)))
	var blue := CaptureDriver.luma_stats(_filled(2, 2, Color(0, 0, 1)))
	_assert_finite(green.x)
	_assert_finite(blue.x)
	assert_float(green.x).is_equal_approx(0.7152, 1e-3)
	assert_float(blue.x).is_equal_approx(0.0722, 1e-3)


func test_frame_stats_use_nearest_rank_percentiles() -> void:
	var samples := PackedFloat64Array()
	for i in range(1, 101):
		samples.append(float(i))
	var stats := CaptureDriver.frame_stats(samples)
	assert_int(stats["frames"]).is_equal(100)
	assert_float(stats["p50_ms"]).is_equal(50.0)
	assert_float(stats["p95_ms"]).is_equal(95.0)
	assert_float(stats["p99_ms"]).is_equal(99.0)
	assert_float(stats["max_ms"]).is_equal(100.0)
	assert_float(stats["mean_ms"]).is_equal_approx(50.5, 1e-9)
	assert_int(stats["over_16_7_ms"]).is_equal(84)
	assert_int(stats["over_33_3_ms"]).is_equal(67)


func test_frame_stats_of_no_frames_are_zero() -> void:
	var stats := CaptureDriver.frame_stats(PackedFloat64Array())
	assert_int(stats["frames"]).is_equal(0)
	assert_float(stats["max_ms"]).is_equal(0.0)


func test_cli_args_carry_the_capture_flags() -> void:
	var result := CliArgs.parse(
		PackedStringArray(["--project", "p.coypu", "--capture", "s.json", "--capture-out", "D:/out"])
	)
	assert_str(result.get("capture", "")).is_equal("s.json")
	assert_str(result.get("capture_out", "")).is_equal("D:/out")
	assert_str(result.get("project", "")).is_equal("p.coypu")
