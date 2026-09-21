class_name TrainsetKinematics
extends RefCounted
## Poses every car and bogie of a trainset along an [AlignmentTable] for one lead station (T-123),
## client-side re-implementation of `docs/data-contracts/trainset-chain.md` (ADR 0007: this runs from
## `_process` every frame; `backend/.../domain/kinematics/trainset.py` is the golden generator, never a
## runtime dependency of this file). Pinned to `shared/golden/trainset_chain.json`, both blocks, by
## `client/tests/unit/test_trainset_kinematics.gd`.
##
## Every position/rotation this reads from [AlignmentTable] already arrives in Godot axes (ADR 0004), and
## the domain-to-Godot axis map is orientation-preserving (`vector_to_godot`'s matrix has determinant +1),
## so every vector operation below (subtraction, cross product, Gram-Schmidt) commutes with that mapping —
## step 3's formulas run unmodified on the already-Godot-mapped frame data, no re-derivation needed.

## One car's pose for one frame: three plain [Transform3D]s, handed straight to [method Car.apply_pose].
class CarPose:
	var body := Transform3D.IDENTITY
	var bogie_front := Transform3D.IDENTITY
	var bogie_rear := Transform3D.IDENTITY

## Guards the degenerate zero-pivot-distance case (trainset-chain.md §4) by chord length, not just
## `bogie_pivot_distance_m == 0`, so a clamp that collapses both pivots onto the same alignment endpoint is
## also caught.
const _CHORD_EPSILON_M := 1e-9


## Poses every car of `trainset` (a `TrainsetDTO` dictionary: `cars` front-to-back, `coupling_gap_m`) for
## lead station `station_lead` and `direction` (+1/-1). `out_poses` is reused across frames -- grown or
## shrunk to `trainset.cars.size()` only when the car count itself changes, so the steady-state 60 Hz path
## touches no [Array]/[Object] beyond what [method AlignmentTable.sample] itself allocates. Returns `true`
## iff any car's front/rear face or either bogie pivot needed clamping into the alignment's station range.
static func pose(
	table: AlignmentTable, trainset: Dictionary, station_lead: float, direction: int, out_poses: Array
) -> bool:
	var cars: Array = trainset.get("cars", [])
	var coupling_gap: float = float(trainset.get("coupling_gap_m", 0.0))
	var station_start := table.station_start()
	var station_end := table.station_end()
	var d := float(direction)

	_ensure_capacity(out_poses, cars.size())

	var clamped := false
	var front_face := station_lead
	for i in cars.size():
		var car: Dictionary = cars[i]
		var length_m := float(car.get("length_m", 0.0))
		var pivot_distance_m := float(car.get("bogie_pivot_distance_m", 0.0))

		var rear_face := front_face - d * length_m
		var lead_pivot := front_face - d * (length_m - pivot_distance_m) * 0.5
		var trail_pivot := lead_pivot - d * pivot_distance_m

		var lead_pivot_c := clampf(lead_pivot, station_start, station_end)
		var trail_pivot_c := clampf(trail_pivot, station_start, station_end)
		if (
			clampf(front_face, station_start, station_end) != front_face
			or clampf(rear_face, station_start, station_end) != rear_face
			or lead_pivot_c != lead_pivot
			or trail_pivot_c != trail_pivot
		):
			clamped = true

		var lead_frame := table.sample(lead_pivot_c)
		var trail_frame := table.sample(trail_pivot_c)
		var out: CarPose = out_poses[i]
		_pose_car(out, lead_frame, trail_frame, d)

		# Next car's front face uses the *unclamped* rear face (trainset-chain.md §1): clamping the chain
		# arithmetic itself, rather than only the values handed to the frame lookup, would pile every car
		# past the end onto the same station instead of letting them overlap the endpoint in sequence.
		front_face = rear_face - d * coupling_gap

	return clamped


static func _pose_car(out: CarPose, lead_frame: FrameSample, trail_frame: FrameSample, d: float) -> void:
	out.bogie_front = Transform3D(Basis(lead_frame.rotation), lead_frame.position)
	out.bogie_rear = Transform3D(Basis(trail_frame.rotation), trail_frame.position)

	var chord := lead_frame.position - trail_frame.position
	var chord_len := chord.length()

	var origin: Vector3
	var forward: Vector3
	var up: Vector3

	if chord_len < _CHORD_EPSILON_M:
		# §4: both pivots collapsed onto one station (zero pivot distance, or a clamp-induced collapse).
		# There is no chord to carry direction's sign, so -- unlike the two-pivot case below -- `forward`
		# is built from the frame's own tangent multiplied by `d` explicitly.
		origin = lead_frame.position
		var tangent := lead_frame.rotation * Vector3.FORWARD
		forward = tangent * d
		up = lead_frame.rotation * Vector3.UP
	else:
		origin = (lead_frame.position + trail_frame.position) * 0.5
		# The chord already carries direction's sign via which pivot is "lead" vs "trail" (F11: do NOT
		# multiply by `d` again here -- that squares the sign back out and makes `forward` direction-
		# independent).
		forward = chord / chord_len
		var lead_up := lead_frame.rotation * Vector3.UP
		var trail_up := trail_frame.rotation * Vector3.UP
		var up_raw := (lead_up + trail_up).normalized()
		up = (up_raw - up_raw.dot(forward) * forward).normalized()

	# left = cross(up, forward), then the body basis columns (right, up, back) mirror how the backend's
	# `basis_to_godot` builds a Godot rotation from (tangent, left, up): right = -left, back = -forward.
	var left := up.cross(forward)
	out.body = Transform3D(Basis(-left, up, -forward), origin)


static func _ensure_capacity(out_poses: Array, count: int) -> void:
	while out_poses.size() < count:
		out_poses.append(CarPose.new())
	if out_poses.size() > count:
		out_poses.resize(count)
