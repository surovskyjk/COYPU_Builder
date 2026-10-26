class_name TerrainTile
extends Node3D
## One terrain display tile (T-135, Part A): a node placed at its backend `tile_origin` (ADR 0004) with one
## `MeshInstance3D` whose vertices are tile-local (see [HeightfieldMesh]). The node keeps the heights it was
## built from, so the integration test can compare them with `terrain.tile` without reading the renderer.
##
## The `ArrayMesh` is created here, on the main thread, from the arrays a worker built (see
## [TerrainField]); nothing in this class runs on a worker.

const SKIRT_MIN_M := 0.5
const SKIRT_DIVISOR := 64.0

static var _neutral: StandardMaterial3D

var level := 0
var ix := 0
var iy := 0
var edge_m := 0.0
var revision := 0
var heights := PackedFloat32Array()

var _mesh_instance: MeshInstance3D


## Skirt depth for a tile of this edge: `max(0.5, edge / 64)` metres (T-135 contract).
static func skirt_for(edge: float) -> float:
	return maxf(SKIRT_MIN_M, edge / SKIRT_DIVISOR)


## The one shared placeholder material for Part A: grey-green, fully rough.
static func neutral_material() -> StandardMaterial3D:
	if _neutral == null:
		_neutral = StandardMaterial3D.new()
		_neutral.albedo_color = Color(0.46, 0.52, 0.44)
		_neutral.roughness = 1.0
	return _neutral


func setup(key: Vector3i, tile_origin: Vector3, edge: float, rev: int, samples: PackedFloat32Array) -> void:
	level = key.x
	ix = key.y
	iy = key.z
	edge_m = edge
	revision = rev
	heights = samples
	position = tile_origin


func key() -> Vector3i:
	return Vector3i(level, ix, iy)


func tile_origin() -> Vector3:
	return position


## Main thread only. `arrays` is the [method HeightfieldMesh.build] result; an empty one leaves the tile
## without a mesh rather than failing.
func set_arrays(arrays: Dictionary) -> void:
	if arrays.is_empty():
		return
	var indices: PackedInt32Array = arrays["indices"]
	if indices.is_empty():
		return
	var surface: Array = []
	surface.resize(Mesh.ARRAY_MAX)
	surface[Mesh.ARRAY_VERTEX] = arrays["vertices"]
	surface[Mesh.ARRAY_NORMAL] = arrays["normals"]
	surface[Mesh.ARRAY_TEX_UV] = arrays["uvs"]
	surface[Mesh.ARRAY_INDEX] = indices
	var mesh := ArrayMesh.new()
	mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, surface)

	_mesh_instance = MeshInstance3D.new()
	_mesh_instance.name = "Surface"
	_mesh_instance.mesh = mesh
	_mesh_instance.material_override = neutral_material()
	add_child(_mesh_instance)


func has_mesh() -> bool:
	return _mesh_instance != null
