class_name TrackChunk
extends Node3D
## One `(chunk_index, surface)` mesh from T-120's `alignment.track_mesh` blobs, wrapping a single child
## `MeshInstance3D` built from them (ADR 0001/0007: the client assembles geometry the backend already
## computed, it never derives it). Vertices/normals/uvs/indices arrive already tile-local and in Godot
## axes -- they go into the `ArrayMesh` unchanged.
##
## **ADR 0004**: the tile origin goes on this node's own `position`; nothing here routes the incoming
## arrays through [Origin] or folds the origin into the vertices -- doing either would double-apply (or
## reintroduce) exactly the float32 shimmer tile-local rendering exists to prevent.

var _station_start := 0.0
var _station_end := 0.0
var _surface := ""


## `info` is one `TrackMeshChunkInfo` dictionary, as decoded from the `alignment.track_mesh` envelope's
## `result.chunks` entries. `vertices`/`normals`/`uvs`/`indices` are the matching
## `vertices_<i>_<surface>`/`normals_<i>_<surface>`/`uvs_<i>_<surface>`/`indices_<i>_<surface>` blobs,
## already decoded to packed arrays by the caller (uvs need converting from the flat float blob to
## `PackedVector2Array` first -- see `TrackCorridor._decode_uvs`).
static func build(
	info: Dictionary,
	vertices: PackedVector3Array,
	normals: PackedVector3Array,
	uvs: PackedVector2Array,
	indices: PackedInt32Array,
	material: Material
) -> TrackChunk:
	var chunk := TrackChunk.new()
	var chunk_index := int(info.get("chunk_index", 0))
	chunk._surface = str(info.get("surface", ""))
	chunk._station_start = float(info.get("station_start", 0.0))
	chunk._station_end = float(info.get("station_end", 0.0))
	chunk.name = "Chunk%d_%s" % [chunk_index, chunk._surface]

	var origin: Array = info.get("tile_origin", [0.0, 0.0, 0.0])
	chunk.position = Vector3(origin[0], origin[1], origin[2])

	var arrays: Array = []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_NORMAL] = normals
	arrays[Mesh.ARRAY_TEX_UV] = uvs
	arrays[Mesh.ARRAY_INDEX] = indices

	var array_mesh := ArrayMesh.new()
	array_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	array_mesh.surface_set_material(0, material)

	var mesh_instance := MeshInstance3D.new()
	mesh_instance.name = "Mesh"
	mesh_instance.mesh = array_mesh
	chunk.add_child(mesh_instance)

	return chunk


func station_start() -> float:
	return _station_start


func station_end() -> float:
	return _station_end


func surface() -> String:
	return _surface
