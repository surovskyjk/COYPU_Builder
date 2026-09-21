class_name Car
extends Node3D
## One consist car: [method body] (a box) rigidly spanning [method bogie_front]/[method bogie_rear]
## (T-122). The three are **siblings**, not nested -- `docs/data-contracts/trainset-chain.md` step 3 has
## the body follow the bogies (it chords between their two pivot positions), never the reverse, so nesting
## a bogie under the body would invert that dependency. T-123 poses all three every frame via
## [method apply_pose]; nothing here computes a pose itself (ADR 0007).
##
## Built once, at consist-assembly time, from a `CarSpecDTO` dictionary -- Phase 1 turns its dimensions
## into boxes and cylinders ([CarMeshBuilder]); `CarSpec.mesh` stays `null` until Phase 2.

const _SCENE := preload("res://scene/vehicles/car.tscn")

var _index := 0
var _length_m := 0.0
var _pivot_distance_m := 0.0
var _body: MeshInstance3D
var _bogie_front: Bogie
var _bogie_rear: Bogie


## `spec` is one `CarSpecDTO` dictionary (optionally carrying a merged-in `"gauge_mm"` -- see
## [CarMeshBuilder]'s header for why that field is not part of the schema itself). `index` is this car's
## position in the consist, front to back, used only for the node's own name.
static func from_spec(spec: Dictionary, index: int) -> Car:
	var car := _SCENE.instantiate() as Car
	car.name = "Car%d" % index
	car._index = index
	car._length_m = float(spec.get("length_m", 0.0))
	car._pivot_distance_m = float(spec.get("bogie_pivot_distance_m", 0.0))

	car._body = car.get_node("CarBody")
	car._bogie_front = car.get_node("BogieFront")
	car._bogie_rear = car.get_node("BogieRear")

	# CarMeshBuilder.body_mesh already bakes the floor-height offset into the mesh's own vertices (see its
	# header) -- CarBody's own transform is left at the skeleton's identity, since apply_pose replaces it
	# wholesale every frame and a position set here would be silently lost the first time that happens.
	car._body.mesh = CarMeshBuilder.body_mesh(spec)
	car._body.set_surface_override_material(0, CarMeshBuilder.body_material(spec))

	car._bogie_front.build(spec)
	car._bogie_rear.build(spec)

	return car


func index() -> int:
	return _index


func length() -> float:
	return _length_m


func pivot_distance() -> float:
	return _pivot_distance_m


func body() -> MeshInstance3D:
	return _body


func bogie_front() -> Node3D:
	return _bogie_front


func bogie_rear() -> Node3D:
	return _bogie_rear


## Sets the three siblings' transforms directly -- T-123 calls this once per visible car per frame. Plain
## `Transform3D` property writes only, no construction of any kind, so this allocates nothing.
func apply_pose(body_xform: Transform3D, front_xform: Transform3D, rear_xform: Transform3D) -> void:
	_body.transform = body_xform
	_bogie_front.transform = front_xform
	_bogie_rear.transform = rear_xform
