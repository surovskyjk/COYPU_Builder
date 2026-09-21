class_name TrackCorridor
extends Node3D
## Owns every chunk of one alignment's track mesh (T-120's `alignment.track_mesh`) plus the sleeper fields
## placed from its [AlignmentTable] (ADR 0007). Each chunk's three surface meshes ([TrackChunk]) and its
## one [SleeperField] share a wrapper [Node3D] so LOD can hide or show a whole chunk in one write; the
## wrapper itself stays at the scene origin -- the tile origin lives on each child's own `position`
## (ADR 0004), never on the wrapper, or the translation would be applied twice.
##
## **F17**: the full Kralupy corridor's `alignment.track_mesh` response (`chunk_index: null`, all chunks
## at once) measures 24.62 MB against `IpcWebSocketClient.INBOUND_BUFFER_SIZE` (16 MiB) -- one frame the
## client cannot receive. [method build] therefore never sends `chunk_index: null`; it requests
## `chunk_index` `0, 1, 2, …` one at a time (~337 KB each for a 250 m chunk), instantiating as each page
## arrives. The number of pages is computed client-side from the already-fetched [AlignmentTable]'s
## station bounds with the exact same `ceil((station_end - station_start) / chunk_length_m)` the backend
## uses (`io/mesh/track.py: _chunk_boundaries`) -- both sides start from the same alignment station bounds
## (`bake_stations` always includes them exactly), so this never has to probe past the end and treat a
## normal "no more chunks" as a [signal EventBus.backend_error].

const LAYER_ID := "track"

const CHUNK_LENGTH_M := 250.0
const FRAME_SPACING_M := 1.0
const SLEEPER_SPACING_M := SleeperField.DEFAULT_SPACING_M

## Beyond this distance the sleeper field -- the expensive per-instance geometry -- hides; rails and
## ballast stay visible. Beyond [constant CHUNK_LOD_FAR_M] the whole chunk hides. Both are plain distance
## checks against the chunk's own tile origin, not decimation: T-120 emits a single detail level, and
## Phase 1 does not need a mesh-simplification pipeline on top of it.
const SLEEPER_LOD_NEAR_M := 400.0
const CHUNK_LOD_FAR_M := 3000.0

var _alignment_id := ""
var _wrappers: Array[Node3D] = []
var _sleeper_fields: Array[SleeperField] = []
var _tile_origins := PackedVector3Array()
var _total_vertex_count := 0


func _ready() -> void:
	EventBus.layers_changed.connect(_on_layers_changed)
	_on_layers_changed()


func alignment_id() -> String:
	return _alignment_id


func chunk_count() -> int:
	return _wrappers.size()


## Chunk `i`'s tile origin (Godot axes, base-point-relative) -- the same value every one of that chunk's
## TrackChunk/SleeperField nodes carries as its own `position` (ADR 0004).
func tile_origin_at(i: int) -> Vector3:
	return _tile_origins[i]


## Total vertex count actually instantiated across every surface of every chunk (report-back figure, not
## used by rendering itself).
func total_vertex_count() -> int:
	return _total_vertex_count


## Total sleeper instance count across every chunk's [SleeperField] (report-back figure).
func sleeper_instance_count() -> int:
	var total := 0
	for field in _sleeper_fields:
		total += field.multimesh.instance_count
	return total


## Coroutine. Replaces any previously built corridor with `alignment_id`'s track mesh, paging by
## `chunk_index` (see F17 above) and placing one [SleeperField] per chunk from `table`.
func build(alignment_id: String, table: AlignmentTable) -> void:
	_clear()
	_alignment_id = alignment_id

	var span := table.station_end() - table.station_start()
	var chunk_count_expected := 1
	if span > 0.0:
		chunk_count_expected = maxi(1, int(ceil(span / CHUNK_LENGTH_M)))

	for chunk_index in chunk_count_expected:
		var envelope := await Session.fetch_track_mesh(alignment_id, chunk_index, CHUNK_LENGTH_M, FRAME_SPACING_M)
		if envelope == null:
			break
		_build_chunk_group(envelope, table)

	EventBus.track_mesh_ready.emit(alignment_id)


func set_visible_layer(should_be_visible: bool) -> void:
	visible = should_be_visible


## Distance-based visibility only (see [constant SLEEPER_LOD_NEAR_M]/[constant CHUNK_LOD_FAR_M]) -- no
## decimation, no mesh rebuilding. Called from `_process`, which is legal (ADR 0007: this is local
## arithmetic against already-baked node positions, not an RPC); it must not allocate, so it only indexes
## the packed arrays [method build] already populated.
func update_lod(camera_position: Vector3) -> void:
	for i in _wrappers.size():
		var distance := camera_position.distance_to(_tile_origins[i])
		var chunk_visible := distance <= CHUNK_LOD_FAR_M
		_wrappers[i].visible = chunk_visible
		if chunk_visible:
			_sleeper_fields[i].visible = distance <= SLEEPER_LOD_NEAR_M


func _on_layers_changed() -> void:
	set_visible_layer(Session.layers().is_visible(LAYER_ID))


func _clear() -> void:
	for wrapper in _wrappers:
		if is_instance_valid(wrapper):
			wrapper.queue_free()
	_wrappers.clear()
	_sleeper_fields.clear()
	_tile_origins.clear()
	_total_vertex_count = 0


## One `alignment.track_mesh` page's worth of surfaces (up to 3: rail_left, rail_right, ballast) plus the
## one [SleeperField] that spans the same station range, all parented under one wrapper [Node3D].
func _build_chunk_group(envelope: IpcEnvelope, table: AlignmentTable) -> void:
	var chunks: Array = envelope.result.get("chunks", [])
	if chunks.is_empty():
		return

	var chunk_index := int(chunks[0].get("chunk_index", 0))
	var wrapper := Node3D.new()
	wrapper.name = "Chunk%d" % chunk_index
	add_child(wrapper)

	var tile_origin := Vector3.ZERO
	var station_start := INF
	var station_end := -INF

	for info: Dictionary in chunks:
		var surface: String = info.get("surface", "")
		var suffix := "%d_%s" % [chunk_index, surface]
		var vertices: PackedVector3Array = envelope.blobs.get("vertices_%s" % suffix, PackedVector3Array())
		var normals: PackedVector3Array = envelope.blobs.get("normals_%s" % suffix, PackedVector3Array())
		var uvs := _decode_uvs(envelope.blobs.get("uvs_%s" % suffix, PackedFloat32Array()))
		var indices: PackedInt32Array = envelope.blobs.get("indices_%s" % suffix, PackedInt32Array())

		var mesh_chunk := TrackChunk.build(info, vertices, normals, uvs, indices, TrackMaterials.for_surface(surface))
		wrapper.add_child(mesh_chunk)
		_total_vertex_count += vertices.size()

		var origin_arr: Array = info.get("tile_origin", [0.0, 0.0, 0.0])
		tile_origin = Vector3(origin_arr[0], origin_arr[1], origin_arr[2])
		station_start = minf(station_start, mesh_chunk.station_start())
		station_end = maxf(station_end, mesh_chunk.station_end())

	var sleeper_field := SleeperField.new()
	sleeper_field.name = "Sleepers"
	sleeper_field.populate(table, station_start, station_end, SLEEPER_SPACING_M, tile_origin)
	wrapper.add_child(sleeper_field)

	_wrappers.append(wrapper)
	_sleeper_fields.append(sleeper_field)
	_tile_origins.append(tile_origin)


## `uvs_<i>_<surface>` decodes to a flat `PackedFloat32Array` (`IpcEnvelope._decode_array` only builds a
## `PackedVector3Array` for a 3-column blob) -- this repacks the (n, 2) pairs the wire actually sent.
func _decode_uvs(flat: PackedFloat32Array) -> PackedVector2Array:
	var uvs := PackedVector2Array()
	uvs.resize(flat.size() / 2)
	for i in uvs.size():
		uvs[i] = Vector2(flat[i * 2], flat[i * 2 + 1])
	return uvs
