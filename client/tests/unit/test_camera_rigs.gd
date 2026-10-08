extends GdUnitTestSuite
## Camera rig unit tests (T-124): pure client-side construction and transform math, no backend, no
## network, matching `test_car_mesh_builder.gd`'s convention of copying synthetic dictionaries straight
## from the real catalogue/fixture sources they exercise so this suite doubles as a check that those
## sources still build correctly.
##
## `_TRAM_CAR_A` is `shared/catalogue/vehicles/tram_generic.json`'s first module, verbatim. The roll used
## by the cant-inheritance test below is computed from `backend/tests/fixtures/synthetic/tram_loop.py`'s
## own `SUPERELEVATION_BASE_MM` (1100 mm) and one of its `CantProfile` values (40 mm), via CLAUDE.md's
## `roll = -sign(kappa) * asin(D / base)` -- the synthetic tram network itself has no LandXML/`.coypu`
## export this client can import, so this is the closest honest reproduction of "the tram alignment"
## reachable without a live backend (see the task's closing report).

const _TOL := 1e-6

const _TRAM_CAR_A := {
	"name": "Tram module A",
	"length_m": 8.5,
	"width_m": 2.4,
	"height_m": 3.4,
	"floor_height_m": 0.35,
	"bogie_pivot_distance_m": 6.0,
	"bogie_wheelbase_m": 1.8,
	"wheel_diameter_m": 0.6,
	"mesh": null,
	"color": "#a0a0a0",
}


func _build_straight_table(station_end: float) -> AlignmentTable:
	var envelope := IpcEnvelope.new()
	envelope.result = {
		"alignment_id": "unit-test-straight",
		"row_count": 2,
		"station_start": 0.0,
		"station_end": station_end,
	}
	envelope.blobs = {
		"station": PackedFloat32Array([0.0, station_end]),
		"position": PackedVector3Array([Vector3.ZERO, Vector3(0.0, 0.0, -station_end)]),
		"rotation": PackedFloat32Array([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]),
		"roll": PackedFloat32Array([0.0, 0.0]),
		"pitch": PackedFloat32Array([0.0, 0.0]),
		"cant_mm": PackedFloat32Array([0.0, 0.0]),
		"curvature": PackedFloat32Array([0.0, 0.0]),
		"gradient": PackedFloat32Array([0.0, 0.0]),
		"elevation": PackedFloat32Array([0.0, 0.0]),
	}
	return AlignmentTable.from_envelope(envelope)


## `dt == duration_s` with two rows so `station_at`/`speed_at` ramp linearly across the whole run instead
## of hitting the last-row clamp partway through (see `RunTable._sample`'s `i >= n - 1` branch).
func _build_run_table(duration: float, station_end: float) -> RunTable:
	var envelope := IpcEnvelope.new()
	envelope.result = {
		"run_id": "unit-test-run",
		"dt": duration,
		"row_count": 2,
		"duration_s": duration,
		"direction": 1,
	}
	envelope.blobs = {
		"station": PackedFloat32Array([0.0, station_end]),
		"speed": PackedFloat32Array([10.0, 10.0]),
	}
	return RunTable.from_envelope(envelope)


func _bound_playback(duration: float, station_end: float) -> PlaybackController:
	var table := _build_straight_table(station_end)
	var run := _build_run_table(duration, station_end)
	var trainset_node: TrainsetNode = auto_free(TrainsetNode.build({"cars": []}))
	var timeline := TimelineState.new()
	var playback: PlaybackController = auto_free(PlaybackController.new())
	playback.bind(table, run, trainset_node, timeline)
	timeline.play()
	return playback


func _wheel_event(up: bool) -> InputEventMouseButton:
	var event := InputEventMouseButton.new()
	event.button_index = MOUSE_BUTTON_WHEEL_UP if up else MOUSE_BUTTON_WHEEL_DOWN
	event.pressed = true
	return event


func _mouse_button_event(index: MouseButton, pressed: bool) -> InputEventMouseButton:
	var event := InputEventMouseButton.new()
	event.button_index = index
	event.pressed = pressed
	return event


func _mouse_motion(relative: Vector2) -> InputEventMouseMotion:
	var event := InputEventMouseMotion.new()
	event.relative = relative
	return event


## `Node3D.global_transform`/`global_position`/`look_at` all require the node to actually be inside a
## live [SceneTree] (Godot 4 errors and returns identity otherwise) -- [CameraRig]'s `_init`-time
## construction only guarantees the *local* parent/child structure exists without a tree, so any test that
## reads a rig's global transform must add it here first. `auto_free` still handles cleanup.
func _in_tree(node: Node) -> Node:
	add_child(node)
	auto_free(node)
	return node


# ---------------------------------------------------------------------------------------------------
# CameraRig (shared base)
# ---------------------------------------------------------------------------------------------------


func test_rig_builds_its_own_camera_with_the_documented_near_far() -> void:
	var rig: CameraRig = auto_free(CameraRig.new())
	assert_that(rig.camera()).is_not_null()
	assert_float(rig.camera().near).is_equal_approx(CameraRig.NEAR_M, _TOL)
	assert_float(rig.camera().far).is_equal_approx(CameraRig.FAR_M, _TOL)


func test_damp_vector_reaches_the_target_only_in_the_limit() -> void:
	var current := Vector3.ZERO
	var target := Vector3(10.0, 0.0, 0.0)
	var stepped := CameraRig.damp_vector(current, target, 0.1, 0.4)
	assert_float(stepped.x).append_failure_message(
		"one small step should move partway, not snap"
	).is_between(0.0001, 9.9999)
	var at_zero_delta := CameraRig.damp_vector(current, target, 0.0, 0.4)
	assert_vector(at_zero_delta).is_equal_approx(current, Vector3(_TOL, _TOL, _TOL))


# ---------------------------------------------------------------------------------------------------
# OrbitCamera
# ---------------------------------------------------------------------------------------------------


func test_orbit_focus_station_places_the_focus_at_the_tables_start() -> void:
	var orbit: OrbitCamera = _in_tree(OrbitCamera.new())
	var table := _build_straight_table(1000.0)
	orbit.bind_corridor(null, table)
	orbit.update(0.0)

	assert_float(orbit.camera().global_position.distance_to(Vector3.ZERO)).append_failure_message(
		"camera should sit exactly `distance()` away from the focus point"
	).is_equal_approx(OrbitCamera.DEFAULT_DISTANCE_M, 1e-3)


func test_orbit_zoom_in_clamps_at_the_minimum_distance() -> void:
	var orbit: OrbitCamera = auto_free(OrbitCamera.new())
	for _i in 200:
		orbit.handle_input(_wheel_event(true))
	assert_float(orbit.distance()).is_equal_approx(OrbitCamera.MIN_DISTANCE_M, 1e-3)


func test_orbit_zoom_out_clamps_at_the_maximum_distance() -> void:
	var orbit: OrbitCamera = auto_free(OrbitCamera.new())
	for _i in 400:
		orbit.handle_input(_wheel_event(false))
	assert_float(orbit.distance()).is_equal_approx(OrbitCamera.MAX_DISTANCE_M, 1e-3)


## AC2: no gimbal flip at the pole, and (F29) never below the focus plane -- the elevation is clamped to
## [constant OrbitCamera.MIN_ELEVATION] .. [constant OrbitCamera._PITCH_LIMIT] no matter how much drag input
## arrives.
func test_orbit_rotate_clamps_elevation_between_the_horizon_and_the_pole() -> void:
	var orbit: OrbitCamera = auto_free(OrbitCamera.new())
	orbit.handle_input(_mouse_button_event(MOUSE_BUTTON_LEFT, true))
	for _i in 500:
		orbit.handle_input(_mouse_motion(Vector2(0.0, 1000.0)))
	assert_float(orbit.pitch()).append_failure_message(
		"elevation must stay at or above the focus plane"
	).is_between(OrbitCamera.MIN_ELEVATION - 1e-6, OrbitCamera.MIN_ELEVATION + 1e-6)

	for _i in 1000:
		orbit.handle_input(_mouse_motion(Vector2(0.0, -1000.0)))
	assert_float(orbit.pitch()).append_failure_message(
		"elevation must stay short of +PI/2 or look_at's up hint degenerates"
	).is_between(OrbitCamera._PITCH_LIMIT - 1e-6, 1.5)


## F29 AC1: on activation with default settings the camera sits above the focus, by `distance * sin(0.35)`.
func test_orbit_default_camera_is_above_the_focus() -> void:
	var orbit: OrbitCamera = _in_tree(OrbitCamera.new())
	orbit.bind_corridor(null, _build_straight_table(1000.0))
	orbit.activated()
	orbit.update(0.0)

	var focus := Vector3.ZERO
	assert_float(orbit.camera().global_position.y - focus.y).append_failure_message(
		"the default orbit view must be above the focus, looking down at the track"
	).is_equal_approx(OrbitCamera.DEFAULT_DISTANCE_M * sin(0.35), 1e-4)
	assert_float(orbit.camera().global_transform.basis.z.y).append_failure_message(
		"a camera above its focus looks down: its -Z axis has a negative y"
	).is_greater(0.0)


## F29 AC2: dragging up (negative `motion.y`) raises the camera, dragging down lowers it but never below the
## elevation clamp. Asserted on the camera's world position, not on `pitch()`.
func test_orbit_drag_up_raises_and_drag_down_lowers_but_never_below_the_clamp() -> void:
	var orbit: OrbitCamera = _in_tree(OrbitCamera.new())
	orbit.bind_corridor(null, _build_straight_table(1000.0))
	orbit.update(0.0)
	var start_y := orbit.camera().global_position.y
	orbit.handle_input(_mouse_button_event(MOUSE_BUTTON_LEFT, true))

	orbit.handle_input(_mouse_motion(Vector2(0.0, -40.0)))
	orbit.update(0.0)
	var raised_y := orbit.camera().global_position.y
	assert_float(raised_y).append_failure_message("dragging up must raise the camera").is_greater(start_y)

	orbit.handle_input(_mouse_motion(Vector2(0.0, 80.0)))
	orbit.update(0.0)
	var lowered_y := orbit.camera().global_position.y
	assert_float(lowered_y).append_failure_message("dragging down must lower the camera").is_less(raised_y)

	for _i in 200:
		orbit.handle_input(_mouse_motion(Vector2(0.0, 1000.0)))
	orbit.update(0.0)
	var floor_y := orbit.distance() * sin(OrbitCamera.MIN_ELEVATION)
	assert_float(orbit.camera().global_position.y).append_failure_message(
		"the camera must stop at the elevation clamp, above the focus plane"
	).is_equal_approx(floor_y, 1e-4)
	assert_float(orbit.camera().global_position.y).is_greater(0.0)


## AC2: no precision breakdown at the far end of the corridor -- zoomed all the way out to
## [constant OrbitCamera.MAX_DISTANCE_M] (beyond the ~18 km Kralupy corridor), the camera position must
## still be finite and sit exactly `distance()` from the focus, not smeared by float error.
func test_orbit_holds_precision_at_maximum_distance() -> void:
	var orbit: OrbitCamera = _in_tree(OrbitCamera.new())
	var table := _build_straight_table(18000.0)
	orbit.bind_corridor(null, table)
	orbit.focus_station(18000.0)
	for _i in 400:
		orbit.handle_input(_wheel_event(false))
	orbit.update(0.016)

	var pos := orbit.camera().global_position
	assert_bool(is_finite(pos.x) and is_finite(pos.y) and is_finite(pos.z)).is_true()
	assert_float(pos.distance_to(Vector3(0.0, 0.0, -18000.0))).is_equal_approx(
		OrbitCamera.MAX_DISTANCE_M, 0.5
	)


# ---------------------------------------------------------------------------------------------------
# WaysideCamera
# ---------------------------------------------------------------------------------------------------


func test_wayside_places_the_observer_offset_from_the_alignment_frame() -> void:
	var wayside: WaysideCamera = _in_tree(WaysideCamera.new())
	var table := _build_straight_table(1000.0)
	wayside.bind_corridor(null, table)
	wayside.update(0.016)

	# Identity rotation at station 0: left = (-1,0,0), up = (0,1,0).
	var expected := Vector3.ZERO + Vector3(-1, 0, 0) * WaysideCamera.LATERAL_OFFSET_M + Vector3(
		0, 1, 0
	) * WaysideCamera.HEIGHT_OFFSET_M
	assert_vector(wayside.camera().global_position).is_equal_approx(expected, Vector3(_TOL, _TOL, _TOL))


func test_wayside_look_at_eases_toward_the_lead_instead_of_snapping() -> void:
	var wayside: WaysideCamera = _in_tree(WaysideCamera.new())
	var table := _build_straight_table(2000.0)
	var playback := _bound_playback(200.0, 2000.0)

	wayside.bind_corridor(null, table)
	wayside.bind_subject(null, playback)
	wayside.update(0.016)  # first sample: snaps (nothing to ease from yet)
	assert_vector(wayside.damped_look_at()).is_equal_approx(Vector3.ZERO, Vector3(_TOL, _TOL, _TOL))

	playback._process(1.0)  # lead_station = 2000 * 1/200 = 10.0 -- well short of the reseat trigger
	wayside.update(0.1)

	var eased := wayside.damped_look_at()
	assert_float(eased.z).append_failure_message(
		"look-at should ease partway toward -10.0, not snap straight to it"
	).is_between(-9.999, -0.001)
	assert_float(wayside.station()).append_failure_message(
		"no reseat should have fired yet"
	).is_equal(0.0)


func test_wayside_reseats_ahead_once_the_train_has_passed() -> void:
	var wayside: WaysideCamera = _in_tree(WaysideCamera.new())
	var table := _build_straight_table(2000.0)
	var playback := _bound_playback(200.0, 2000.0)

	wayside.bind_corridor(null, table)
	wayside.bind_subject(null, playback)
	wayside.update(0.016)

	playback._process(60.0)  # lead_station = 2000 * 60/200 = 600.0, well past the reseat trigger
	wayside.update(0.1)

	assert_float(wayside.station()).append_failure_message(
		"observer should have re-seated %s m ahead of its previous station" % WaysideCamera.RESEAT_AHEAD_M
	).is_equal_approx(WaysideCamera.RESEAT_AHEAD_M, 1e-3)


# ---------------------------------------------------------------------------------------------------
# CabCamera
# ---------------------------------------------------------------------------------------------------


func test_cab_eye_offset_matches_floor_height_plus_1_6m_and_car_dimensions() -> void:
	var offset := CabCamera.eye_offset(_TRAM_CAR_A)
	# length=8.5 -> forward = 8.5/2 - 0.6 = 3.65; width=2.4 -> lateral = 2.4/2 - 0.3 = 0.9;
	# floor=0.35 -> up = 0.35 + 1.6 = 1.95.
	assert_vector(offset).is_equal_approx(Vector3(0.9, 1.95, -3.65), Vector3(_TOL, _TOL, _TOL))


## AC5: zeroing the child's transform must leave the parent exactly on the car body -- the whole XR-split
## claim (`CabCamera`'s header) rests on the parent never being touched by the eye offset.
func test_cab_zeroing_the_child_transform_leaves_the_parent_exactly_on_the_car_body() -> void:
	var trainset_node: TrainsetNode = _in_tree(TrainsetNode.build({"cars": [_TRAM_CAR_A]}))
	var car := trainset_node.car(0)
	var body_xform := Transform3D(Basis(Vector3.RIGHT, 0.1), Vector3(5.0, 0.5, -20.0))
	car.body().transform = body_xform

	var cab: CabCamera = _in_tree(CabCamera.new())
	cab.bind_subject(trainset_node, null)
	cab.update(0.016)

	cab.camera().transform = Transform3D.IDENTITY
	assert_bool(cab.global_transform.is_equal_approx(body_xform)).append_failure_message(
		"zeroing the child's local transform should leave the parent exactly on the car body"
	).is_true()


## AC4: the cab rig rolls with cant, verified against the real tram fixture's own cant/superelevation-base
## parameters (see this file's header for why a synthetic body pose stands in for a live import).
func test_cab_inherits_roll_consistent_with_the_tram_fixtures_cant() -> void:
	var trainset_node: TrainsetNode = _in_tree(TrainsetNode.build({"cars": [_TRAM_CAR_A]}))
	var car := trainset_node.car(0)

	var roll := -asin(0.040 / 1.1)  # tram_loop.py: SUPERELEVATION_BASE_MM=1100mm, cant=40mm
	var rolled_basis := Basis(Vector3(0.0, 0.0, 1.0), roll)  # rotate about the forward/back axis
	car.body().transform = Transform3D(rolled_basis, Vector3(10.0, 0.35, -5.0))

	var cab: CabCamera = _in_tree(CabCamera.new())
	cab.bind_subject(trainset_node, null)
	cab.update(0.016)

	assert_vector(cab.global_transform.basis.y).append_failure_message(
		"cab rig's up vector should match the rolled car body exactly (whole transform is copied)"
	).is_equal_approx(rolled_basis.y, Vector3(_TOL, _TOL, _TOL))

	var measured_roll := Vector3.UP.angle_to(cab.global_transform.basis.y)
	assert_float(measured_roll).append_failure_message(
		"cab view should tilt by the tram's own cant-derived roll (%.5f rad)" % absf(roll)
	).is_equal_approx(absf(roll), 1e-6)


# ---------------------------------------------------------------------------------------------------
# CameraManager
# ---------------------------------------------------------------------------------------------------


func test_set_mode_leaves_exactly_one_camera_current() -> void:
	var manager: CameraManager = _in_tree(CameraManager.new())
	var all_modes := [CameraManager.Mode.ORBIT, CameraManager.Mode.WAYSIDE, CameraManager.Mode.CAB]
	for mode in [CameraManager.Mode.WAYSIDE, CameraManager.Mode.CAB, CameraManager.Mode.ORBIT]:
		manager.set_mode(mode)
		assert_int(manager.mode()).is_equal(mode)
		for other in all_modes:
			var expected_current: bool = other == mode
			assert_bool(manager.camera_for(other).current).append_failure_message(
				"mode=%d: camera_for(%d).current should be %s" % [mode, other, expected_current]
			).is_equal(expected_current)


func test_set_mode_emits_mode_changed_and_event_bus_camera_changed() -> void:
	var manager: CameraManager = _in_tree(CameraManager.new())
	var monitored := monitor_signals(manager)
	# `false`: EventBus is the shared autoload singleton, not a test-owned instance -- it must not be
	# auto_free()'d when the test ends (see `test_origin.gd`'s identical note).
	var bus_monitored := monitor_signals(EventBus, false)
	manager.set_mode(CameraManager.Mode.CAB)
	await assert_signal(monitored).is_emitted("mode_changed", [CameraManager.Mode.CAB])
	await assert_signal(bus_monitored).is_emitted("camera_changed", [CameraManager.Mode.CAB])


func test_bind_and_focus_station_do_not_crash_and_every_mode_stays_finite() -> void:
	var manager: CameraManager = _in_tree(CameraManager.new())
	var table := _build_straight_table(500.0)
	manager.bind_corridor(null, table)
	manager.bind_subject(null, null)
	manager.focus_station(250.0)

	for mode in [CameraManager.Mode.ORBIT, CameraManager.Mode.WAYSIDE, CameraManager.Mode.CAB]:
		manager.set_mode(mode)
		manager._process(0.016)
		var pos := manager.active_camera().global_position
		assert_bool(is_finite(pos.x) and is_finite(pos.y) and is_finite(pos.z)).append_failure_message(
			"mode %d produced a non-finite camera position" % mode
		).is_true()
