extends GdUnitTestSuite
## Golden-pinned mapping tests for `Origin` (T-103): reproduces every point and frame in
## `shared/golden/origin_mapping.json` and checks the to_godot/from_godot round trip. No backend, no
## network — must stay fast enough for `-a tests/unit`.
##
## Tolerance: both sides of every comparison below are Godot [Vector3]/[Basis] values, i.e. already
## rounded to the engine's single-precision `real_t` by the time they are compared. `Origin` does its
## own arithmetic in GDScript's 64-bit `float` and only rounds once, at the point a Vector3 is
## constructed — the same point the golden fixture itself was generated at (`points_to_godot(...,
## dtype=np.float64)` in `tools/make_golden.py`) — so both sides round the same underlying double to the
## same nearest `real_t` value and land on it exactly; 1e-9 is therefore met with room to spare, not by
## loosening the check.

const _TOLERANCE := Vector3(1e-9, 1e-9, 1e-9)

var _golden: Dictionary


func before() -> void:
	_golden = GoldenLoader.load_golden("origin_mapping")


func before_test() -> void:
	Origin.set_base_point(0.0, 0.0, 0.0)


func test_golden_fixture_loaded() -> void:
	assert_bool(_golden.is_empty()).is_false()


func _set_base_point_from_golden() -> void:
	var base: Dictionary = _golden.get("base_point", {})
	Origin.set_base_point(base.get("easting", 0.0), base.get("northing", 0.0), base.get("height", 0.0))


func test_to_godot_reproduces_every_golden_point() -> void:
	_set_base_point_from_golden()
	var points: Array = _golden.get("points", [])
	assert_int(points.size()).is_greater(0)

	for entry: Dictionary in points:
		var enh: Array = entry["enh"]
		var expected_godot: Array = entry["godot"]
		var computed := Origin.to_godot(enh[0], enh[1], enh[2])
		var expected := Vector3(expected_godot[0], expected_godot[1], expected_godot[2])
		assert_vector(computed).append_failure_message(
			"enh=%s expected=%s computed=%s" % [enh, expected, computed]
		).is_equal_approx(expected, _TOLERANCE)


func test_from_godot_reproduces_every_golden_point() -> void:
	_set_base_point_from_golden()
	var points: Array = _golden.get("points", [])

	for entry: Dictionary in points:
		var enh: Array = entry["enh"]
		var godot_xyz: Array = entry["godot"]
		var local := Vector3(godot_xyz[0], godot_xyz[1], godot_xyz[2])
		var computed := Origin.from_godot(local)
		var expected := Vector3(enh[0], enh[1], enh[2])
		assert_vector(computed).append_failure_message(
			"godot=%s expected(enh)=%s computed=%s" % [godot_xyz, expected, computed]
		).is_equal_approx(expected, _TOLERANCE)


func test_round_trip_from_godot_of_to_godot_matches_source_point() -> void:
	_set_base_point_from_golden()
	var points: Array = _golden.get("points", [])

	for entry: Dictionary in points:
		var enh: Array = entry["enh"]
		var round_tripped := Origin.from_godot(Origin.to_godot(enh[0], enh[1], enh[2]))
		var expected := Vector3(enh[0], enh[1], enh[2])
		assert_vector(round_tripped).append_failure_message(
			"enh=%s round_tripped=%s" % [enh, round_tripped]
		).is_equal_approx(expected, _TOLERANCE)


func test_basis_from_frame_reproduces_every_golden_frame() -> void:
	var frames: Array = _golden.get("frames", [])
	assert_int(frames.size()).is_greater(0)

	for entry: Dictionary in frames:
		var tangent_arr: Array = entry["tangent"]
		var left_arr: Array = entry["left"]
		var up_arr: Array = entry["up"]
		var columns: Array = entry["godot_basis_columns"]

		var basis := Origin.basis_from_frame(
			Vector3(tangent_arr[0], tangent_arr[1], tangent_arr[2]),
			Vector3(left_arr[0], left_arr[1], left_arr[2]),
			Vector3(up_arr[0], up_arr[1], up_arr[2]),
		)

		var expected_right := Vector3(columns[0][0], columns[0][1], columns[0][2])
		var expected_up := Vector3(columns[1][0], columns[1][1], columns[1][2])
		var expected_back := Vector3(columns[2][0], columns[2][1], columns[2][2])

		assert_vector(basis.x).append_failure_message("right column").is_equal_approx(
			expected_right, _TOLERANCE
		)
		assert_vector(basis.y).append_failure_message("up column").is_equal_approx(expected_up, _TOLERANCE)
		assert_vector(basis.z).append_failure_message("back column").is_equal_approx(
			expected_back, _TOLERANCE
		)


func test_set_base_point_marks_it_present_and_base_point_reads_it_back() -> void:
	Origin.set_base_point(1.0, 2.0, 3.0)
	assert_bool(Origin.has_base_point()).is_true()
	assert_vector(Origin.base_point()).is_equal_approx(Vector3(1.0, 2.0, 3.0), Vector3(1e-4, 1e-4, 1e-4))


func test_set_base_point_emits_base_point_changed() -> void:
	# `false`: Origin is the shared autoload singleton, not a test-owned instance — it must not be
	# auto_free()'d when the test ends.
	var monitored := monitor_signals(Origin, false)
	Origin.set_base_point(5.0, 6.0, 7.0)
	await assert_signal(monitored).is_emitted("base_point_changed", [5.0, 6.0, 7.0])
