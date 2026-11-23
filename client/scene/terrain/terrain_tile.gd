class_name TerrainTile
extends Node3D
## One terrain display tile (T-135): a node placed at its backend `tile_origin` (ADR 0004) with one
## `MeshInstance3D` whose vertices are tile-local (see [HeightfieldMesh]). The node keeps the heights and the
## source map it was built from, so the integration test can compare them with `terrain.tile` without reading the
## renderer.
##
## Part B adds the imagery: until a [TerrainMaterial] is attached (see [method set_imagery]) the tile shows the
## neutral placeholder material from Part A. [method show_imagery] switches back to that placeholder when the
## Imagery layer is hidden, without dropping the material, so showing it again costs nothing.
##
## The `ArrayMesh` and the materials are made on the main thread from what a worker built (see [TerrainField]);
## nothing in this class runs on a worker.

const SKIRT_MIN_M := 0.5
const SKIRT_DIVISOR := 64.0

static var _neutral: StandardMaterial3D

var level := 0
var ix := 0
var iy := 0
var edge_m := 0.0
var revision := 0
var heights := PackedFloat32Array()
## The terrain tile's source map (T-133): `source_index` per sample, 255 = none, indexes `source_ids`.
var source_index := PackedByteArray()
var source_ids := PackedStringArray()
## The imagery revision this tile was draped with, and the imagery sources that won a pixel ("relief" included).
var imagery_revision := -1
var imagery_source_ids := PackedStringArray()

var _terrain_sources := PackedStringArray()
var _material: TerrainMaterial
var _imagery_shown := true
## Whether the source boundaries are drawn (T-135 Part B). A tile without shown imagery then gets its own
## boundary-only material, built from its source map (see [method _apply_material]).
var _boundaries := false
var _boundary_material: TerrainMaterial
var _mesh_instance: MeshInstance3D


## Skirt depth for a tile of this edge: `max(0.5, edge / 64)` metres (T-135 contract).
static func skirt_for(edge: float) -> float:
	return maxf(SKIRT_MIN_M, edge / SKIRT_DIVISOR)


## The one shared placeholder material of Part A: grey-green, fully rough.
static func neutral_material() -> StandardMaterial3D:
	if _neutral == null:
		_neutral = StandardMaterial3D.new()
		_neutral.albedo_color = TerrainMaterial.NEUTRAL_COLOUR
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


## The source map of the terrain tile: `source_ids`, the per-sample `source_index`, and `used`, the distinct indices
## the samples hold (computed on the worker, see [method TerrainField]), in ascending order. Keeps the source ids
## those indices name, for the attribution.
func set_sources(ids: PackedStringArray, index: PackedByteArray, used: PackedInt32Array) -> void:
	source_ids = ids
	source_index = index
	_terrain_sources = PackedStringArray()
	for value in used:
		if value == TerrainMaterial.NONE_INDEX or value >= ids.size():
			continue
		_terrain_sources.append(ids[value])


## The terrain sources this tile's samples come from (255 = none excluded).
func terrain_source_ids() -> PackedStringArray:
	return _terrain_sources


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
	add_child(_mesh_instance)
	_apply_material()


func has_mesh() -> bool:
	return _mesh_instance != null


func mesh_instance() -> MeshInstance3D:
	return _mesh_instance


## Drapes the tile with `material` (main thread), recording the imagery revision and the sources that won a pixel.
## `shown` is whether the Imagery layer is visible.
func set_imagery(material: TerrainMaterial, imagery_rev: int, ids: PackedStringArray, shown: bool) -> void:
	_material = material
	_material.set_boundaries(_boundaries)
	imagery_revision = imagery_rev
	imagery_source_ids = ids
	_imagery_shown = shown
	_apply_material()


func has_imagery() -> bool:
	return _material != null


## The imagery material, or null before any imagery arrived.
func imagery_material() -> TerrainMaterial:
	return _material


## Shows the imagery when `on`, the neutral placeholder otherwise. The material is kept either way.
func show_imagery(on: bool) -> void:
	_imagery_shown = on
	_apply_material()


## Turns the source boundaries on or off for this tile, whichever material it shows.
func set_boundaries(on: bool) -> void:
	_boundaries = on
	if _material != null:
		_material.set_boundaries(on)
	_apply_material()


## The material the mesh draws with right now, or null when the tile has no mesh.
func shown_material() -> Material:
	if _mesh_instance == null:
		return null
	return _mesh_instance.material_override


## What the tile draws: its imagery when that is shown; otherwise, while the boundaries are on, a flat material with
## the tile's own source map; otherwise the shared neutral placeholder.
func _apply_material() -> void:
	if _mesh_instance == null:
		return
	if _material != null and _imagery_shown:
		_mesh_instance.material_override = _material
	elif _boundaries and not source_index.is_empty():
		if _boundary_material == null:
			_boundary_material = TerrainMaterial.for_boundaries(source_index)
		_mesh_instance.material_override = _boundary_material
	else:
		_mesh_instance.material_override = neutral_material()
