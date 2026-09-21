extends GdUnitTestSuite
## SleeperField unit tests (T-121): pure client-side placement, no scene tree, no backend -- must stay
## fast enough for `-a tests/unit`.
##
## Assertions go against [method SleeperField.transform_for_station] directly, not against
## `multimesh.get_instance_transform()` after [method SleeperField.populate]: under Godot's headless
## dummy renderer (`--headless`, what both `-a tests` locally and Linux CI run), `MultiMesh` instance
## transforms never round-trip through the RenderingServer -- `get_instance_transform` reads back
## `Transform3D.IDENTITY` regardless of what was set, verified directly against a minimal reproduction
## before writing these tests this way. `transform_for_station` is the pure function `populate` calls for
## every instance, so this still exercises the real placement logic, just without depending on
## engine storage `--headless` cannot provide.
##
## The [AlignmentTable] here is built from three hand-picked rows, not a golden fixture: a flat span, a
## canted span (roll only) and a climbing span (pitch only). `shared/golden/frame_eval.json`'s Kralupy
## rows are all zero-cant/zero-gradient by construction (see `tools/make_golden.py: tram_block`'s
## docstring — the Kralupy LandXML's cant is "a documented zero placeholder"), so they cannot exercise
## task_121's acceptance criterion 4 (non-zero roll on a canted span) on their own.

const _POS_TOL := 1e-3
const _BASIS_TOL := 1e-5

## Rotating the identity basis around the world Z axis models cant: the tangent (-Z, unchanged) keeps
## heading straight down the track while "up" tilts by the roll angle, exactly like real superelevation.
const _ROLL_RAD := 0.35
## Rotating around the world X axis models a climbing gradient: "back" picks up a -Y component, so the
## tangent (-back) points slightly upward while still moving generally in -Z.
const _PITCH_RAD := 0.1

var _flat_basis := Basis.IDENTITY
var _canted_basis := Basis.IDENTITY.rotated(Vector3(0.0, 0.0, 1.0), _ROLL_RAD)
var _pitched_basis := Basis.IDENTITY.rotated(Vector3(1.0, 0.0, 0.0), _PITCH_RAD)

var _table: AlignmentTable


func before_test() -> void:
	_table = _build_synthetic_table()


func _build_synthetic_table() -> AlignmentTable:
	var stations := PackedFloat32Array([0.0, 50.0, 100.0])
	var positions := PackedVector3Array([Vector3(0.0, 0.0, 0.0), Vector3(0.0, 0.0, -50.0), Vector3(0.0, 5.0, -100.0)])
	var rotations := PackedFloat32Array()
	for basis: Basis in [_flat_basis, _canted_basis, _pitched_basis]:
		var q := basis.get_rotation_quaternion()
		rotations.append(q.x)
		rotations.append(q.y)
		rotations.append(q.z)
		rotations.append(q.w)

	var envelope := IpcEnvelope.new()
	envelope.result = {
		"alignment_id": "synthetic-canted-span",
		"row_count": stations.size(),
		"station_start": stations[0],
		"station_end": stations[stations.size() - 1],
	}
	envelope.blobs = {"station": stations, "position": positions, "rotation": rotations}
	return AlignmentTable.from_envelope(envelope)


func _expected_transform(station: float, tile_origin: Vector3) -> Transform3D:
	var sample := _table.sample(station)
	var basis := Basis(sample.rotation)
	var drop := SleeperField.SLEEPER_SIZE.y * 0.5 + SleeperField.RAIL_FOOT_DEPTH_M
	var local_position := (sample.position - tile_origin) - basis.y * drop
	return Transform3D(basis, local_position)


func _assert_transform_matches(actual: Transform3D, expected: Transform3D, context: String) -> void:
	assert_vector(actual.origin).append_failure_message("%s: position" % context).is_equal_approx(
		expected.origin, Vector3(_POS_TOL, _POS_TOL, _POS_TOL)
	)
	assert_vector(actual.basis.x).append_failure_message("%s: basis.x" % context).is_equal_approx(
		expected.basis.x, Vector3(_BASIS_TOL, _BASIS_TOL, _BASIS_TOL)
	)
	assert_vector(actual.basis.y).append_failure_message("%s: basis.y" % context).is_equal_approx(
		expected.basis.y, Vector3(_BASIS_TOL, _BASIS_TOL, _BASIS_TOL)
	)
	assert_vector(actual.basis.z).append_failure_message("%s: basis.z" % context).is_equal_approx(
		expected.basis.z, Vector3(_BASIS_TOL, _BASIS_TOL, _BASIS_TOL)
	)


func test_populate_creates_one_instance_per_spacing_step_across_the_span() -> void:
	var field: SleeperField = auto_free(SleeperField.new())
	field.populate(_table, 0.0, 100.0, 10.0)
	# floor(100/10) + 1 = 11 stations: 0, 10, 20, ..., 100.
	assert_int(field.multimesh.instance_count).is_equal(11)


func test_populate_sets_the_field_node_position_to_the_tile_origin() -> void:
	var field: SleeperField = auto_free(SleeperField.new())
	var tile_origin := Vector3(120.0, 0.0, -300.0)
	field.populate(_table, 0.0, 40.0, 20.0, tile_origin)
	assert_vector(field.position).is_equal_approx(tile_origin, Vector3(1e-6, 1e-6, 1e-6))


func test_transform_for_station_matches_alignment_table_sample_on_a_flat_span() -> void:
	var tile_origin := Vector3(0.0, 0.0, -20.0)
	for station in [0.0, 20.0, 40.0]:
		var actual := SleeperField.transform_for_station(_table, station, tile_origin)
		var expected := _expected_transform(station, tile_origin)
		_assert_transform_matches(actual, expected, "station=%s" % station)


func test_sleeper_sits_below_the_track_plane_by_the_documented_rail_foot_depth() -> void:
	var tile_origin := Vector3.ZERO
	var xform := SleeperField.transform_for_station(_table, 0.0, tile_origin)

	var sample := _table.sample(0.0)
	var basis := Basis(sample.rotation)
	var drop := SleeperField.SLEEPER_SIZE.y * 0.5 + SleeperField.RAIL_FOOT_DEPTH_M
	var delta := xform.origin - (sample.position - tile_origin)

	assert_float(delta.dot(basis.x)).append_failure_message("lateral drift").is_equal_approx(0.0, _POS_TOL)
	assert_float(delta.dot(basis.z)).append_failure_message("longitudinal drift").is_equal_approx(0.0, _POS_TOL)
	assert_float(delta.dot(basis.y)).append_failure_message("vertical drop").is_equal_approx(-drop, _POS_TOL)


## task_121 acceptance criterion 4: sleepers roll with cant. A canted sleeper's "up" axis tilts away from
## world-up by exactly the roll angle, taken straight from AlignmentTable.sample()'s rotation.
func test_sleeper_rolls_with_cant_on_the_canted_span() -> void:
	var xform := SleeperField.transform_for_station(_table, 50.0, Vector3.ZERO)

	var tilt := xform.basis.y.angle_to(Vector3.UP)
	assert_float(tilt).append_failure_message(
		"expected the canted sleeper's up axis to tilt by the roll angle, got %s rad" % tilt
	).is_equal_approx(_ROLL_RAD, _BASIS_TOL)
	assert_float(tilt).append_failure_message("cant produced no roll at all").is_greater(1e-3)


## task_121 acceptance criterion 4: sleepers pitch with gradient.
func test_sleeper_pitches_with_gradient_on_the_climbing_span() -> void:
	var xform := SleeperField.transform_for_station(_table, 100.0, Vector3.ZERO)

	var tangent := -xform.basis.z
	assert_float(tangent.y).append_failure_message(
		"expected the pitched sleeper's tangent to climb, got tangent=%s" % tangent
	).is_greater(1e-3)


## ADR 0004, the non-negotiable this task exists to prove: the tile origin must be subtracted from the
## instance position, never folded into it. At station 50 `sample.position` is exactly `tile_origin`, so
## a correct instance origin is just the small downward drop to the rail foot; a regression that skips
## the subtraction (or applies it twice) would show up here as an origin off by ~50 m instead of ~0.25 m.
func test_transform_is_tile_local_not_world_absolute() -> void:
	var tile_origin := Vector3(0.0, 0.0, -50.0)
	var xform := SleeperField.transform_for_station(_table, 50.0, tile_origin)

	var drop := SleeperField.SLEEPER_SIZE.y * 0.5 + SleeperField.RAIL_FOOT_DEPTH_M
	assert_float(xform.origin.length()).append_failure_message(
		"instance origin=%s -- looks like tile_origin leaked into (or out of) the instance transform"
		% xform.origin
	).is_less(drop + _POS_TOL)
