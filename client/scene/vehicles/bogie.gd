class_name Bogie
extends Node3D
## `BogieFront`/`BogieRear`: a rigid frame plus two wheelsets, one sibling of [member Car.body] under
## [Car] (T-122). Posed as a whole by [method Car.apply_pose] every frame -- position, orientation and
## roll are the track frame's own values (`docs/data-contracts/trainset-chain.md` step 2), so nothing here
## ever touches this node's own transform after [method build].
##
## The node skeleton (this node plus its `BogieFrame`/`Wheelset0`/`Wheelset1` children) comes from
## `car.tscn`; [method build] only assigns mesh/material/position to the children that already exist --
## see [CarMeshBuilder] for the rail-head datum every offset here is measured from.

var _frame: MeshInstance3D
var _wheelsets: Array[MeshInstance3D] = []


## `spec` is the same `CarSpecDTO` dictionary (optionally carrying a merged-in `"gauge_mm"`, see
## [CarMeshBuilder]) that [Car] was built from. Called once per bogie at car-assembly time.
func build(spec: Dictionary) -> void:
	_frame = get_node("BogieFrame")
	_wheelsets = [get_node("Wheelset0"), get_node("Wheelset1")]

	_frame.mesh = CarMeshBuilder.bogie_frame_mesh(spec)
	_frame.set_surface_override_material(0, CarMeshBuilder.bogie_material())
	_frame.position = Vector3(0.0, CarMeshBuilder.bogie_frame_offset_m(spec), 0.0)

	var wheel_mesh := CarMeshBuilder.wheel_mesh(spec)
	var wheel_material := CarMeshBuilder.wheel_material()
	var wheel_y := CarMeshBuilder.wheel_offset_m(spec)
	var half_spacing := CarMeshBuilder.wheel_spacing_m(spec) * 0.5
	for i in _wheelsets.size():
		var sign_z := 1.0 if i == 0 else -1.0
		var wheelset := _wheelsets[i]
		wheelset.mesh = wheel_mesh
		wheelset.set_surface_override_material(0, wheel_material)
		wheelset.position = Vector3(0.0, wheel_y, sign_z * half_spacing)
		# CylinderMesh's own axis is local Y; rotating 90 deg around Z lays it across the track (X).
		wheelset.rotation_degrees = Vector3(0.0, 0.0, 90.0)


func frame() -> MeshInstance3D:
	return _frame


func wheelset(i: int) -> MeshInstance3D:
	return _wheelsets[i]


func wheelset_count() -> int:
	return _wheelsets.size()
