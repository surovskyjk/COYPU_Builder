class_name CabCamera
extends CameraRig
## Cab rig (T-124), structured so Phase 4 can swap in OpenXR with zero change to the follow logic:
##
##   CabCamera (Node3D)   <- becomes XROrigin3D in Phase 4; posed from the lead car's body every frame
##   `- Camera3D          <- becomes XRCamera3D in Phase 4; local offset only, never touched by follow
##
## [method update] writes the lead car's *whole* body transform (position and rotation -- cant roll
## included, since that is already baked into the pose [TrainsetKinematics] wrote) straight onto this
## node's own `global_transform`. That single line is the entirety of "vehicle following", and it lives
## here alone. The eye offset -- forward to the cab end, laterally toward the driver's side, up to eye
## height -- lives entirely on `camera()`'s own local `position`, computed once when a subject binds and
## never touched again by [method update]. That split is the whole XR-readiness claim: in Phase 4 the
## parent is retargeted to `XROrigin3D` and the child is replaced by the headset's own pose, and not one
## line of the vehicle-following code above changes. **No OpenXR dependency, XR interface, or `xr_`
## setting is added now** -- ADR 0002 reserves XR as a Phase 4 escape hatch, not a Phase 1 feature.
##
## Because the parent copies the body's rotation wholesale, the rig inherits cant roll for free -- the
## same roll [TrainsetKinematics] already baked into that transform tilts the cab view exactly as it tilts
## the car body. `client/tests/unit/test_camera_rigs.gd` checks this against the tram catalogue's own
## cant/superelevation-base parameters (`backend/tests/fixtures/synthetic/tram_loop.py`), since that
## synthetic tram network has no LandXML/`.coypu` export this client can import end to end -- see the
## task's closing report for why that gap is not this task's to close.

## How far above `floor_height_m`'s own datum the driver's eyes sit (task-specified figure).
const EYE_HEIGHT_ABOVE_FLOOR_M := 1.6

## How far back from the car's own front face (local `-length_m/2`, matching
## `CarMeshBuilder.body_mesh`'s box) the driver's eye sits -- enough that the eye is not literally at the
## windshield glass.
const CAB_FRONT_SETBACK_M := 0.6

## How far in from the car's own side wall (local `+-width_m/2`) the driver's eye sits.
const CAB_LATERAL_INSET_M := 0.3

## `CarSpecDTO` (`docs/data-contracts/vehicle-catalogue.md`) carries no field recording which side a
## driver's console is on. Phase 1 fixes it to the car's local `+X` side; flagged here and in the task's
## closing report as an assumption, the same way `CarMeshBuilder`'s own header flags its schema gaps.
const CAB_SIDE_SIGN := 1.0

var _trainset: TrainsetNode


func bind_subject(trainset: TrainsetNode, _controller: PlaybackController) -> void:
	_trainset = trainset
	_apply_eye_offset()


func focus_station(_s: float) -> void:
	# Invariant: cameras read T-123's poses, never re-derive a station from a position. The cab has no
	# station concept of its own -- it always rides the lead car -- so a station-jump command is a no-op.
	pass


func activated() -> void:
	_apply_eye_offset()


func update(_delta: float) -> void:
	if _trainset == null or _trainset.car_count() == 0:
		return
	global_transform = _trainset.car(0).body().global_transform


func _apply_eye_offset() -> void:
	_camera.position = _eye_offset_for_lead_car()
	_camera.rotation = Vector3.ZERO


func _eye_offset_for_lead_car() -> Vector3:
	if _trainset == null or _trainset.car_count() == 0:
		return Vector3.ZERO
	var dto: Variant = Session.trainset(_trainset.trainset_id())
	if not (dto is Dictionary):
		return Vector3.ZERO
	var cars: Array = (dto as Dictionary).get("cars", [])
	if cars.is_empty():
		return Vector3.ZERO
	return eye_offset(cars[0] as Dictionary)


## Pure and testable without a bound trainset or a live [Session] cache: `spec` is one `CarSpecDTO`
## dictionary. The body's local axes match `CarMeshBuilder.body_mesh` exactly (width across X, height up
## Y, length along Z, front face at `-length_m/2`), so the offsets below land inside the car's own shell.
static func eye_offset(spec: Dictionary) -> Vector3:
	var length_m := float(spec.get("length_m", 0.0))
	var width_m := float(spec.get("width_m", 0.0))
	var floor_height_m := float(spec.get("floor_height_m", 0.0))

	var forward_m := maxf(length_m * 0.5 - CAB_FRONT_SETBACK_M, 0.0)
	var lateral_m := maxf(width_m * 0.5 - CAB_LATERAL_INSET_M, 0.0) * CAB_SIDE_SIGN
	var up_m := floor_height_m + EYE_HEIGHT_ABOVE_FLOOR_M
	return Vector3(lateral_m, up_m, -forward_m)
