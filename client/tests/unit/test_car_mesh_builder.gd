extends GdUnitTestSuite
## T-122 unit tests: [CarMeshBuilder], [Car], [Bogie] and [TrainsetNode] against synthetic `CarSpecDTO`/
## `TrainsetDTO` dictionaries -- pure client-side construction, no scene tree required to be live, no
## backend, no network, so this stays fast enough for `-a tests/unit`. The synthetic dictionaries below
## are copied from the real catalogue entries (`shared/catalogue/vehicles/dmu_br650_cd840.json` and
## `tram_generic.json`) so a passing suite here also means the real fixtures build correctly, without this
## suite itself depending on a live backend to fetch them (see `test_trainset_assembly.gd` for that).
##
## The vertical datum throughout is the rail head (see [CarMeshBuilder]'s header): a car's own local
## origin and a bogie's own local origin both represent it, so every offset asserted below is a local Y
## measured up from 0.0, never an absolute world height.

const _POS_TOL := 1e-6
const _DIM_TOL := 1e-6

const _DMU_CAR_SPEC := {
	"name": "RS1 railcar",
	"length_m": 25.5,
	"width_m": 2.93,
	"height_m": 3.73,
	"floor_height_m": 0.6,
	"bogie_pivot_distance_m": 17.5,
	"bogie_wheelbase_m": 1.8,
	"wheel_diameter_m": 0.77,
	"mesh": null,
	"color": "#c8102e",
}
const _DMU_GAUGE_MM := 1435.0

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
const _TRAM_CAR_B := {
	"name": "Tram module B",
	"length_m": 9.0,
	"width_m": 2.4,
	"height_m": 3.4,
	"floor_height_m": 0.35,
	"bogie_pivot_distance_m": 6.5,
	"bogie_wheelbase_m": 1.8,
	"wheel_diameter_m": 0.6,
	"mesh": null,
	"color": "#a0a0a0",
}
const _TRAM_GAUGE_MM := 1000.0


func _with_gauge(spec: Dictionary, gauge_mm: float) -> Dictionary:
	var copy := spec.duplicate()
	copy["gauge_mm"] = gauge_mm
	return copy


func _build_dmu_trainset(car_count: int) -> Dictionary:
	var cars: Array = []
	for i in car_count:
		cars.append(_DMU_CAR_SPEC.duplicate())
	return {
		"trainset_id": "synthetic-dmu",
		"spec_key": "dmu_br650_cd840",
		"name": "Test DMU",
		"mode": "heavy_rail",
		"gauge_mm": _DMU_GAUGE_MM,
		"coupling_gap_m": 0.0,
		"length_m": _DMU_CAR_SPEC["length_m"] * car_count,
		"cars": cars,
	}


func _build_tram_trainset() -> Dictionary:
	var cars: Array = [_TRAM_CAR_A.duplicate(), _TRAM_CAR_B.duplicate(), _TRAM_CAR_A.duplicate()]
	return {
		"trainset_id": "synthetic-tram",
		"spec_key": "tram_generic",
		"name": "Test tram",
		"mode": "light_rail_tram",
		"gauge_mm": _TRAM_GAUGE_MM,
		"coupling_gap_m": 0.3,
		"length_m": _TRAM_CAR_A["length_m"] * 2 + _TRAM_CAR_B["length_m"] + 0.3 * 2,
		"cars": cars,
	}


func _assert_transform_equals(actual: Transform3D, expected: Transform3D, context: String) -> void:
	assert_vector(actual.origin).append_failure_message("%s: origin" % context).is_equal_approx(
		expected.origin, Vector3(_POS_TOL, _POS_TOL, _POS_TOL)
	)
	assert_vector(actual.basis.x).append_failure_message("%s: basis.x" % context).is_equal_approx(
		expected.basis.x, Vector3(_POS_TOL, _POS_TOL, _POS_TOL)
	)
	assert_vector(actual.basis.y).append_failure_message("%s: basis.y" % context).is_equal_approx(
		expected.basis.y, Vector3(_POS_TOL, _POS_TOL, _POS_TOL)
	)
	assert_vector(actual.basis.z).append_failure_message("%s: basis.z" % context).is_equal_approx(
		expected.basis.z, Vector3(_POS_TOL, _POS_TOL, _POS_TOL)
	)


# --- CarMeshBuilder -------------------------------------------------------------------------------------


## `body_mesh` returns an `ArrayMesh` (not a centred `BoxMesh`) precisely so the floor-height offset below
## survives `apply_pose` replacing `CarBody.transform` wholesale -- see [CarMeshBuilder]'s header. Its
## dimensions are therefore asserted from `get_aabb()`, the mesh's own bounding box in mesh-local space.
func test_body_mesh_dimensions_match_car_spec_to_1e6() -> void:
	var mesh := CarMeshBuilder.body_mesh(_DMU_CAR_SPEC)
	assert_vector(mesh.get_aabb().size).is_equal_approx(
		Vector3(_DMU_CAR_SPEC["width_m"], _DMU_CAR_SPEC["height_m"], _DMU_CAR_SPEC["length_m"]),
		Vector3(_DIM_TOL, _DIM_TOL, _DIM_TOL),
	)


## The mesh's own lowest vertex (its AABB's min corner) is the body's underside -- baked directly into the
## geometry, not into a node position that `apply_pose` would later overwrite.
func test_body_mesh_underside_sits_at_floor_height_above_the_rail_head_in_the_mesh_itself() -> void:
	var mesh := CarMeshBuilder.body_mesh(_DMU_CAR_SPEC)
	var underside: float = mesh.get_aabb().position.y
	assert_float(underside).append_failure_message(
		"expected the body mesh's own lowest vertex at floor_height_m=%s, got %s"
		% [_DMU_CAR_SPEC["floor_height_m"], underside]
	).is_equal_approx(_DMU_CAR_SPEC["floor_height_m"], _DIM_TOL)


func test_body_offset_places_the_underside_floor_height_above_the_rail_head() -> void:
	var offset := CarMeshBuilder.body_offset_m(_DMU_CAR_SPEC)
	var underside := offset - float(_DMU_CAR_SPEC["height_m"]) * 0.5
	assert_float(underside).append_failure_message(
		"expected the body's underside at floor_height_m=%s, got %s" % [_DMU_CAR_SPEC["floor_height_m"], underside]
	).is_equal_approx(_DMU_CAR_SPEC["floor_height_m"], _DIM_TOL)


func test_bogie_frame_is_gauge_wide_and_wheelbase_plus_wheel_diameter_long() -> void:
	var spec := _with_gauge(_DMU_CAR_SPEC, _DMU_GAUGE_MM)
	var mesh := CarMeshBuilder.bogie_frame_mesh(spec)
	assert_float(mesh.size.x).append_failure_message("frame width should equal the gauge").is_equal_approx(
		_DMU_GAUGE_MM / 1000.0, _DIM_TOL
	)
	assert_float(mesh.size.z).is_equal_approx(
		float(_DMU_CAR_SPEC["bogie_wheelbase_m"]) + float(_DMU_CAR_SPEC["wheel_diameter_m"]), _DIM_TOL
	)


func test_bogie_frame_falls_back_to_default_gauge_when_spec_carries_none() -> void:
	var mesh := CarMeshBuilder.bogie_frame_mesh(_DMU_CAR_SPEC)
	assert_float(mesh.size.x).append_failure_message(
		"a CarSpecDTO dict has no gauge_mm field; expected the documented DEFAULT_GAUGE_MM fallback"
	).is_equal_approx(CarMeshBuilder.DEFAULT_GAUGE_MM / 1000.0, _DIM_TOL)


func test_wheel_mesh_diameter_matches_spec() -> void:
	var mesh := CarMeshBuilder.wheel_mesh(_DMU_CAR_SPEC)
	var expected_radius: float = float(_DMU_CAR_SPEC["wheel_diameter_m"]) * 0.5
	assert_float(mesh.top_radius).is_equal_approx(expected_radius, _DIM_TOL)
	assert_float(mesh.bottom_radius).is_equal_approx(expected_radius, _DIM_TOL)


func test_wheel_offset_is_half_the_wheel_diameter() -> void:
	assert_float(CarMeshBuilder.wheel_offset_m(_DMU_CAR_SPEC)).is_equal_approx(
		float(_DMU_CAR_SPEC["wheel_diameter_m"]) * 0.5, _DIM_TOL
	)


func test_wheel_spacing_equals_bogie_wheelbase() -> void:
	assert_float(CarMeshBuilder.wheel_spacing_m(_DMU_CAR_SPEC)).is_equal_approx(
		_DMU_CAR_SPEC["bogie_wheelbase_m"], _DIM_TOL
	)


# --- Car / Bogie assembly --------------------------------------------------------------------------------


func test_car_from_spec_builds_a_body_and_two_bogies_each_with_a_frame_and_two_wheelsets() -> void:
	var car: Car = auto_free(Car.from_spec(_with_gauge(_DMU_CAR_SPEC, _DMU_GAUGE_MM), 0))

	assert_that(car.body()).is_not_null()
	var front := car.bogie_front() as Bogie
	var rear := car.bogie_rear() as Bogie
	assert_that(front).is_not_null()
	assert_that(rear).is_not_null()
	assert_int(front.wheelset_count()).is_equal(2)
	assert_int(rear.wheelset_count()).is_equal(2)
	assert_that(front.frame()).is_not_null()
	assert_that(rear.frame()).is_not_null()


func test_car_body_and_bogies_are_siblings_under_car_not_nested() -> void:
	var car: Car = auto_free(Car.from_spec(_DMU_CAR_SPEC, 0))

	assert_object(car.body().get_parent()).append_failure_message(
		"CarBody must be a direct child of Car, not nested under a bogie"
	).is_equal(car)
	assert_object(car.bogie_front().get_parent()).append_failure_message(
		"BogieFront must be a direct child of Car, not nested under CarBody"
	).is_equal(car)
	assert_object(car.bogie_rear().get_parent()).append_failure_message(
		"BogieRear must be a direct child of Car, not nested under CarBody"
	).is_equal(car)
	assert_int(car.body().get_child_count()).append_failure_message(
		"CarBody has children -- a bogie appears to be nested under the body"
	).is_equal(0)


## The body's transform itself must stay at the skeleton's identity -- `apply_pose` (T-123) replaces it
## wholesale every frame, so the floor-height offset has to live in the mesh's own vertices instead (see
## [CarMeshBuilder]'s header). This checks both halves of that: the node transform is untouched, and the
## mesh geometry it carries is the one actually offset.
func test_car_body_dimensions_and_floor_offset_match_spec() -> void:
	var car: Car = auto_free(Car.from_spec(_DMU_CAR_SPEC, 0))
	var body := car.body()

	assert_bool(body.transform.is_equal_approx(Transform3D.IDENTITY)).append_failure_message(
		"CarBody's own transform should stay at the skeleton's identity -- apply_pose owns it, not build()"
	).is_true()

	var aabb := body.mesh.get_aabb()
	assert_vector(aabb.size).is_equal_approx(
		Vector3(_DMU_CAR_SPEC["width_m"], _DMU_CAR_SPEC["height_m"], _DMU_CAR_SPEC["length_m"]),
		Vector3(_DIM_TOL, _DIM_TOL, _DIM_TOL),
	)
	var underside: float = aabb.position.y
	assert_float(underside).append_failure_message(
		"body mesh's lowest vertex sits at y=%s -- looks buried in or floating above the rail head" % underside
	).is_equal_approx(_DMU_CAR_SPEC["floor_height_m"], _DIM_TOL)


func test_wheel_centres_sit_at_half_wheel_diameter_above_the_rail_head_and_are_spaced_by_the_wheelbase() -> void:
	var car: Car = auto_free(Car.from_spec(_DMU_CAR_SPEC, 0))
	var front := car.bogie_front() as Bogie
	var w0 := front.wheelset(0)
	var w1 := front.wheelset(1)
	var expected_y: float = float(_DMU_CAR_SPEC["wheel_diameter_m"]) * 0.5

	assert_float(w0.position.y).append_failure_message(
		"wheel centre at y=%s -- expected wheel_diameter_m/2 above the rail head" % w0.position.y
	).is_equal_approx(expected_y, _DIM_TOL)
	assert_float(w1.position.y).is_equal_approx(expected_y, _DIM_TOL)
	assert_float(absf(w0.position.z - w1.position.z)).append_failure_message(
		"wheelset spacing should equal bogie_wheelbase_m"
	).is_equal_approx(_DMU_CAR_SPEC["bogie_wheelbase_m"], _DIM_TOL)


# --- TrainsetNode assembly (synthetic DTOs -- no backend) ------------------------------------------------


func test_trainset_node_build_three_car_dmu_consist_has_3_cars_6_bogies_12_wheelsets() -> void:
	var trainset: TrainsetNode = auto_free(TrainsetNode.build(_build_dmu_trainset(3)))

	assert_int(trainset.car_count()).is_equal(3)
	var bogie_count := 0
	var wheelset_count := 0
	for i in trainset.car_count():
		var car := trainset.car(i)
		assert_int(car.index()).is_equal(i)
		for bogie in [car.bogie_front() as Bogie, car.bogie_rear() as Bogie]:
			bogie_count += 1
			wheelset_count += bogie.wheelset_count()

	assert_int(bogie_count).is_equal(6)
	assert_int(wheelset_count).is_equal(12)


func test_trainset_node_build_assigns_no_world_transforms() -> void:
	var trainset: TrainsetNode = auto_free(TrainsetNode.build(_build_dmu_trainset(2)))
	for i in trainset.car_count():
		var car := trainset.car(i)
		assert_bool(car.transform.is_equal_approx(Transform3D.IDENTITY)).append_failure_message(
			"Car %d should sit at the identity transform until T-123 poses it" % i
		).is_true()
		assert_bool(car.body().transform.is_equal_approx(Transform3D.IDENTITY)).is_true()
		assert_bool(car.bogie_front().transform.is_equal_approx(Transform3D.IDENTITY)).is_true()
		assert_bool(car.bogie_rear().transform.is_equal_approx(Transform3D.IDENTITY)).is_true()


## ADR 0006: the same builder, no branch on mode, produces the tram from `tram_generic.json`'s shape
## (1000 mm gauge, ~9 m cars, three distinct modules) just as correctly as the DMU above.
func test_trainset_node_build_is_mode_agnostic_for_the_tram_spec_at_1000mm_gauge() -> void:
	var trainset: TrainsetNode = auto_free(TrainsetNode.build(_build_tram_trainset()))

	assert_int(trainset.car_count()).is_equal(3)
	var car0 := trainset.car(0)
	assert_float(car0.length()).is_equal_approx(_TRAM_CAR_A["length_m"], _DIM_TOL)

	var aabb := car0.body().mesh.get_aabb()
	assert_vector(aabb.size).is_equal_approx(
		Vector3(_TRAM_CAR_A["width_m"], _TRAM_CAR_A["height_m"], _TRAM_CAR_A["length_m"]),
		Vector3(_DIM_TOL, _DIM_TOL, _DIM_TOL),
	)

	# The bogie frame is built from the trainset's own 1000 mm gauge, not the 1435 mm rail default --
	# proof that gauge actually reaches the mesh builder rather than silently falling back.
	var frame_mesh := (car0.bogie_front() as Bogie).frame().mesh as BoxMesh
	assert_float(frame_mesh.size.x).append_failure_message(
		"tram bogie frame width should equal the tram's own 1000 mm gauge, not the DMU/rail default"
	).is_equal_approx(_TRAM_GAUGE_MM / 1000.0, _DIM_TOL)


# --- apply_pose -------------------------------------------------------------------------------------------


func test_apply_pose_sets_the_three_transforms_exactly() -> void:
	var car: Car = auto_free(Car.from_spec(_DMU_CAR_SPEC, 0))

	var body_xform := Transform3D(Basis.IDENTITY.rotated(Vector3.UP, 0.3), Vector3(10.0, 2.0, -5.0))
	var front_xform := Transform3D(Basis.IDENTITY.rotated(Vector3.RIGHT, 0.1), Vector3(18.75, 2.1, -5.0))
	var rear_xform := Transform3D(Basis.IDENTITY.rotated(Vector3(0.0, 0.0, 1.0), 0.05), Vector3(1.25, 1.9, -5.0))

	car.apply_pose(body_xform, front_xform, rear_xform)

	_assert_transform_equals(car.body().transform, body_xform, "body")
	_assert_transform_equals(car.bogie_front().transform, front_xform, "bogie_front")
	_assert_transform_equals(car.bogie_rear().transform, rear_xform, "bogie_rear")


## Acceptance criterion 6: apply_pose is on the per-frame path (ADR 0002) and must not allocate. `Transform3D`
## is a value type (like `Vector3`/`Basis`), so a correct implementation -- three plain property writes --
## creates no new engine `Object`s; `Performance.OBJECT_COUNT` is a direct, synchronous counter of those
## (Nodes, RefCounted, Resources, ...), so a 1000-call loop with no growth is a real (not proxy) allocation
## check, not just "ran fast".
func test_apply_pose_allocates_no_objects_across_1000_calls() -> void:
	var car: Car = auto_free(Car.from_spec(_DMU_CAR_SPEC, 0))
	var body_xform := Transform3D(Basis.IDENTITY.rotated(Vector3.UP, 0.3), Vector3(10.0, 2.0, -5.0))
	var front_xform := Transform3D(Basis.IDENTITY.rotated(Vector3.RIGHT, 0.1), Vector3(18.75, 2.1, -5.0))
	var rear_xform := Transform3D(Basis.IDENTITY.rotated(Vector3(0.0, 0.0, 1.0), 0.05), Vector3(1.25, 1.9, -5.0))

	# Warm up once outside the measured window so any one-time lazy init inside apply_pose's call path
	# (there should be none) can't be mistaken for per-call allocation.
	car.apply_pose(body_xform, front_xform, rear_xform)

	var before := Performance.get_monitor(Performance.OBJECT_COUNT)
	for i in 1000:
		car.apply_pose(body_xform, front_xform, rear_xform)
	var after := Performance.get_monitor(Performance.OBJECT_COUNT)

	assert_int(int(after)).append_failure_message(
		"object count grew from %s to %s across 1000 apply_pose calls -- expected zero allocation" % [before, after]
	).is_equal(int(before))
