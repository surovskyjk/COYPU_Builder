class_name CarMeshBuilder
extends RefCounted
## `CarSpec` -> `ArrayMesh`/primitive `Mesh` resources (T-122, Phase 1: `CarSpec.mesh` stays `null`, so
## every car is boxes and cylinders sized from its dimensions; a populated `mesh` is Phase 2).
##
## **Vertical datum: the rail head.** `lrs.frames` puts a bogie's own origin at the track-plane centre at
## its pivot station under `RotationPivot.LOW_RAIL` (`docs/data-contracts/trainset-chain.md` step 2), and
## the body's origin at the midpoint of its two bogies' positions (step 3) -- both are, for meshing
## purposes, the rail head. Every `_offset_m` function below returns a local Y measured up from that
## datum (0.0), never an absolute Y.
##
## **Why [method body_mesh] bakes its offset into vertices instead of a node position.** `CarBody` is one
## of the three siblings [method Car.apply_pose] poses every frame by *replacing* its `transform` wholesale
## (`_body.transform = body_xform`) -- a `position` set once at build time would be silently wiped the
## first time T-123 poses the car, leaving the body centred exactly on the rail head (uniformly "buried",
## per the task's own warning). [method body_mesh] therefore returns a plain `BoxMesh`'s own arrays
## (`PrimitiveMesh.get_mesh_arrays()`) translated up by [method body_offset_m] and rebuilt as an
## `ArrayMesh`, so the offset survives being posed. `BogieFrame`/`Wheelset0`/`Wheelset1` need no such
## treatment: they are children of `BogieFront`/`BogieRear`, which apply_pose poses instead of them, so a
## plain node `position` set once in [Bogie]`.build()` is never overwritten -- [method bogie_frame_mesh]
## and [method wheel_mesh] stay ordinary centred primitives for exactly that reason.
##
## **Ambiguity flagged in the closing report:** `gauge_mm` lives on `VehicleSpecDTO`/`TrainsetDTO`, not on
## `CarSpecDTO` -- there is no such field in the schema `docs/data-contracts/vehicle-catalogue.md` documents
## for `cars[]`. The bogie frame's width is nonetheless specified as "the gauge wide", so a caller that has
## the trainset-level value (`TrainsetNode.build`) folds it into the per-car dictionary as `"gauge_mm"`
## before calling [method Car.from_spec] -- `Car.from_spec`'s own signature is untouched (still one
## `Dictionary` plus an index), only the dictionary's contents gain one key. A spec missing `"gauge_mm"`
## (e.g. a bare `CarSpecDTO` dict in a unit test) falls back to [constant DEFAULT_GAUGE_MM] rather than
## failing, so every function here stays usable standalone.

## Standard-gauge fallback (mm) for a spec dictionary with no `"gauge_mm"` key.
const DEFAULT_GAUGE_MM := 1435.0

## Bogie frame height as a fraction of `wheel_diameter_m` -- Phase 1 has no dimension for this beyond
## "flattened", so it is expressed relative to the one wheel dimension every spec already carries.
const _FRAME_HEIGHT_FACTOR := 0.4

## Wheel tread width (the cylinder's own height before it is rotated onto its side) -- a fixed Phase 1
## placeholder; the schema carries no per-vehicle tread-width field to size this from.
const _WHEEL_TREAD_WIDTH_M := 0.15

const _DEFAULT_BODY_COLOR := "#808080"
const _BOGIE_COLOR := "#303030"
const _WHEEL_COLOR := "#1a1a1a"

static var _bogie_material: StandardMaterial3D
static var _wheel_material: StandardMaterial3D


## `spec` is a `CarSpecDTO` dictionary. Body box, `length_m x width_m x height_m` (Godot X/Y/Z respectively
## -- width across the track, height up, length along it), its vertices already translated so the
## underside sits [method body_offset_m] above the rail head -- see the class header for why this is an
## `ArrayMesh` with baked-in vertices rather than a centred `BoxMesh` plus a node position.
static func body_mesh(spec: Dictionary) -> ArrayMesh:
	var box := BoxMesh.new()
	box.size = Vector3(float(spec["width_m"]), float(spec["height_m"]), float(spec["length_m"]))
	return _translated_y(box, body_offset_m(spec))


## Local Y the body's underside must sit at, above the rail head.
static func body_offset_m(spec: Dictionary) -> float:
	return float(spec["floor_height_m"]) + float(spec["height_m"]) * 0.5


## Copies `primitive`'s own geometry (`PrimitiveMesh.get_mesh_arrays()`) into a new `ArrayMesh` with every
## vertex shifted up by `offset_y` -- the one way to move a `PrimitiveMesh`'s centre off its own origin,
## since none of `BoxMesh`/`CylinderMesh` exposes an off-centre pivot directly.
static func _translated_y(primitive: PrimitiveMesh, offset_y: float) -> ArrayMesh:
	var arrays := primitive.get_mesh_arrays()
	var vertices: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
	var offset := Vector3(0.0, offset_y, 0.0)
	for i in vertices.size():
		vertices[i] += offset
	arrays[Mesh.ARRAY_VERTEX] = vertices

	var array_mesh := ArrayMesh.new()
	array_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	return array_mesh


## Bogie frame box: `bogie_wheelbase_m + wheel_diameter_m` long, the gauge wide, flattened in height. See
## the class header for where `gauge_mm` comes from when it is not on `spec` itself.
static func bogie_frame_mesh(spec: Dictionary) -> BoxMesh:
	var wheel_diameter_m := float(spec["wheel_diameter_m"])
	var gauge_m := float(spec.get("gauge_mm", DEFAULT_GAUGE_MM)) / 1000.0
	var mesh := BoxMesh.new()
	mesh.size = Vector3(
		gauge_m, wheel_diameter_m * _FRAME_HEIGHT_FACTOR, float(spec["bogie_wheelbase_m"]) + wheel_diameter_m
	)
	return mesh


## Local Y the bogie frame's node must sit at -- centred at the same height as the wheelsets so the frame
## visually wraps them.
static func bogie_frame_offset_m(spec: Dictionary) -> float:
	return float(spec["wheel_diameter_m"]) * 0.5


## One wheel's cylinder, `wheel_diameter_m` across. `CylinderMesh`'s own axis is local Y; the caller rotates
## the instance onto the track-crossing (X) axis -- this function only sizes it.
static func wheel_mesh(spec: Dictionary) -> CylinderMesh:
	var radius := float(spec["wheel_diameter_m"]) * 0.5
	var mesh := CylinderMesh.new()
	mesh.top_radius = radius
	mesh.bottom_radius = radius
	mesh.height = _WHEEL_TREAD_WIDTH_M
	return mesh


## Local Y a wheelset's node must sit at: its centre, `wheel_diameter_m / 2` above the rail head.
static func wheel_offset_m(spec: Dictionary) -> float:
	return float(spec["wheel_diameter_m"]) * 0.5


## Local Z separation between the bogie's two wheelsets.
static func wheel_spacing_m(spec: Dictionary) -> float:
	return float(spec["bogie_wheelbase_m"])


## Fresh material per car (not cached): `CarSpecDTO.color` varies per spec, unlike the fixed bogie/wheel
## looks below. A consist is small (tens of nodes, per the task contract), so this is not worth caching.
static func body_material(spec: Dictionary) -> StandardMaterial3D:
	return _build_material("CarBody", str(spec.get("color", _DEFAULT_BODY_COLOR)))


## Cached: every bogie frame in every car shares the same placeholder look.
static func bogie_material() -> StandardMaterial3D:
	if _bogie_material == null:
		_bogie_material = _build_material("BogieFrame", _BOGIE_COLOR)
	return _bogie_material


## Cached: every wheel in every car shares the same placeholder look.
static func wheel_material() -> StandardMaterial3D:
	if _wheel_material == null:
		_wheel_material = _build_material("Wheel", _WHEEL_COLOR)
	return _wheel_material


static func _build_material(res_name: String, hex_color: String) -> StandardMaterial3D:
	var mat := StandardMaterial3D.new()
	mat.resource_name = "CarMeshMaterial_%s" % res_name
	mat.albedo_color = Color(hex_color)
	return mat
