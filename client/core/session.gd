extends Node
## Autoload `Session`: the client's read-only mirror of backend document state (ADR 0007 — the client
## interpolates baked tables and computes no geometry of its own). T-103 populates project identity and
## alignment summaries from `project.get`/`project.new`/`import.landxml`; T-115 adds the alignment/run
## table caches, the vehicle catalogue, the entity registry and layer state; T-135 adds the terrain tile
## calls [TerrainField] pages through (no cache: the revision moves when the sources change, so the field
## owns what it has built); T-121 adds the
## `alignment.track_mesh` page cache [TrackCorridor] pages through; T-122 adds the trainset cache
## [TrainsetNode] builds from.
##
## Every `fetch_*` coroutine resolves even on `err` — returning `null`/`false` and emitting
## [signal EventBus.backend_error] — so no caller can hang waiting on a dead backend. Table caching is by
## id: a second fetch of the same id with the same parameters returns the cached table without a round
## trip to the backend.

var _project_id := ""
var _crs := ""
var _alignments: Array[Dictionary] = []
var _runs: Array[Dictionary] = []
var _catalogue: Array[Dictionary] = []

## alignment_id -> {"spacing_m": float, "table": AlignmentTable}
var _alignment_tables: Dictionary = {}
## run_id -> {"dt": float, "table": RunTable}
var _run_tables: Dictionary = {}

## alignment_id -> {page_key (int; a real chunk_index, or -1 for "all chunks") -> {"chunk_length_m": float,
## "spacing_m": float, "envelope": IpcEnvelope}}
var _track_mesh_pages: Dictionary = {}
## alignment_id -> {"chunk_length_m": float, "spacing_m": float, "chunks": Array[Dictionary]} -- the
## metadata-only chunk list (F18), cached alongside the pages it indexes.
var _track_mesh_chunk_lists: Dictionary = {}

## trainset_id -> TrainsetDTO dictionary, as decoded from `trainset.create`/`trainset.get`.
var _trainsets: Dictionary = {}

## Test seam (T-135): `(method: String, params: Dictionary) -> IpcEnvelope or null`. When valid, it is asked
## first for every terrain request; a returned envelope is used instead of the backend, and `null` lets the
## request go to the backend. The override may also be a coroutine (T-135 Part C, F52): it is awaited, so it can hold
## a reply until a test releases it, and then return the envelope or `null`. A plain function keeps working. Nothing
## in the application sets it.
var _terrain_request_override := Callable()

## Whether the terrain's source boundaries are drawn (T-135 Part B): set by the shell from the user's setting, read
## by [TerrainField].
var _source_boundaries := false
## The data sources of the terrain and imagery in view, published by [TerrainField] (T-135 Part B).
var _data_sources: Array[Dictionary] = []
## The imagery warnings of the last listing, published by [TerrainField] (T-136 Part C3); empty while none apply.
var _imagery_warnings := PackedStringArray()

## "alignment_id|spacing_m" -> the decoded `alignment.ground_profile` result (T-137 B). Cleared when the project, the
## alignments or the terrain change: the client does not track the terrain revision, so these events stand in for it.
var _ground_profiles: Dictionary = {}
## Bumped on every clear, so a fetch that was in flight when the cache was cleared is not stored.
var _ground_profile_generation := 0
var _ground_profile_error := ""
## Test seam (T-137 B): `(params: Dictionary) -> IpcEnvelope or null`, asked first for `alignment.ground_profile`, as the
## terrain seam is. Nothing in the application sets it.
var _ground_profile_override := Callable()

var _entities := EntityRegistry.new()
var _layers := LayerState.new()


func _ready() -> void:
	_layers.layer_changed.connect(_on_layer_changed)
	EventBus.project_changed.connect(_clear_ground_profiles)
	EventBus.alignments_changed.connect(_clear_ground_profiles)
	EventBus.sources_changed.connect(_on_ground_profile_sources_changed)


func project_id() -> String:
	return _project_id


func crs() -> String:
	return _crs


## `AlignmentSummary` dictionaries, as they arrive on the wire (alignment_id, name, mode, station_start,
## station_end, length, warnings).
func alignments() -> Array[Dictionary]:
	return _alignments


## `result` is a `ProjectInfoResult` dictionary (project.new/project.get/import.landxml's enclosing
## project). Also updates Origin's base point and emits EventBus signals.
func set_project_info(result: Dictionary) -> void:
	_project_id = result.get("project_id", "")
	var crs_value: Variant = result.get("crs")
	_crs = crs_value if crs_value is String else ""

	_alignments.clear()
	for entry: Variant in result.get("alignments", []):
		if entry is Dictionary:
			_alignments.append(entry)

	var base: Variant = result.get("base_point")
	if base is Dictionary:
		Origin.set_base_point(
			base.get("easting", 0.0), base.get("northing", 0.0), base.get("height", 0.0)
		)

	EventBus.project_changed.emit()
	EventBus.alignments_changed.emit()


## `RunSummary` dictionaries (run_id, name, alignment_id, trainset_id, direction, station_start,
## station_end, duration_s, sample_count, stop_count), populated by [method import_coypu].
func runs() -> Array[Dictionary]:
	return _runs


## `VehicleSpecDTO` dictionaries, populated by [method fetch_catalogue].
func vehicle_catalogue() -> Array[Dictionary]:
	return _catalogue


func entities() -> EntityRegistry:
	return _entities


func layers() -> LayerState:
	return _layers


## Cached table for `alignment_id`, or `null` when it has never been fetched. Use
## [method fetch_alignment_table] to populate it.
func alignment_table(alignment_id: String) -> AlignmentTable:
	if not _alignment_tables.has(alignment_id):
		return null
	return _alignment_tables[alignment_id]["table"]


## Coroutine. Bakes (or returns the cached) frame table for `alignment_id` at `spacing_m`. Resolves to
## `null` and emits [signal EventBus.backend_error] on failure rather than hanging.
func fetch_alignment_table(alignment_id: String, spacing_m: float = 1.0) -> AlignmentTable:
	if _alignment_tables.has(alignment_id):
		var cached: Dictionary = _alignment_tables[alignment_id]
		if cached["spacing_m"] == spacing_m:
			return cached["table"]

	var envelope := await Backend.request(
		"alignment.frame_table", {"alignment_id": alignment_id, "spacing_m": spacing_m}
	)
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return null

	var table := AlignmentTable.from_envelope(envelope)
	_alignment_tables[alignment_id] = {"spacing_m": spacing_m, "table": table}
	EventBus.alignment_table_ready.emit(alignment_id)
	return table


## Cached table for `run_id`, or `null` when it has never been fetched. Use [method fetch_run_table] to
## populate it.
func run_table(run_id: String) -> RunTable:
	if not _run_tables.has(run_id):
		return null
	return _run_tables[run_id]["table"]


## Coroutine. Resamples (or returns the cached) run table for `run_id` at `dt`. Resolves to `null` and
## emits [signal EventBus.backend_error] on failure rather than hanging.
func fetch_run_table(run_id: String, dt: float = 0.05) -> RunTable:
	if _run_tables.has(run_id):
		var cached: Dictionary = _run_tables[run_id]
		if cached["dt"] == dt:
			return cached["table"]

	var envelope := await Backend.request("run.get", {"run_id": run_id, "dt": dt})
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return null

	var table := RunTable.from_envelope(envelope)
	_run_tables[run_id] = {"dt": dt, "table": table}
	EventBus.run_table_ready.emit(run_id)
	return table


## Coroutine. Fetches (or returns the cached) `alignment.track_mesh` page for `alignment_id`.
## `chunk_index` selects one chunk's three surfaces; `null` requests every chunk in a single response and
## must only be used for a short alignment -- [TrackCorridor] never passes `null` for a real corridor,
## since a long one's all-chunks response can exceed
## `IpcWebSocketClient.INBOUND_BUFFER_SIZE` (see F17 in docs/tasks/task_121_client_track_scene.md and
## docs/data-contracts/track-mesh.md). Resolves to `null` and emits [signal EventBus.backend_error] on
## failure rather than hanging. Returns the raw envelope: `result.chunks` carries the per-(chunk_index,
## surface) metadata, `blobs` the geometry, named per docs/data-contracts/track-mesh.md.
func fetch_track_mesh(
	alignment_id: String, chunk_index: Variant = null, chunk_length_m: float = 250.0, spacing_m: float = 1.0
) -> IpcEnvelope:
	var page_key: int = chunk_index if chunk_index != null else -1
	var pages: Dictionary = _track_mesh_pages.get(alignment_id, {})
	if pages.has(page_key):
		var cached: Dictionary = pages[page_key]
		if cached["chunk_length_m"] == chunk_length_m and cached["spacing_m"] == spacing_m:
			return cached["envelope"]

	var params := {"alignment_id": alignment_id, "chunk_length_m": chunk_length_m, "spacing_m": spacing_m}
	if chunk_index != null:
		params["chunk_index"] = chunk_index

	var envelope := await Backend.request("alignment.track_mesh", params)
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return null

	pages[page_key] = {"chunk_length_m": chunk_length_m, "spacing_m": spacing_m, "envelope": envelope}
	_track_mesh_pages[alignment_id] = pages
	return envelope


## Coroutine. Fetches (or returns the cached) chunk list for `alignment_id` -- every
## `TrackMeshChunkInfo` dictionary the backend would produce for these `chunk_length_m`/`spacing_m`, with no
## blobs (`alignment.track_mesh` with `metadata_only: true`, F18). The backend owns how an alignment is
## chunked, so this list -- never client-side arithmetic -- is what a caller pages [method fetch_track_mesh]
## over. Resolves to an empty array and emits [signal EventBus.backend_error] on failure rather than hanging.
func fetch_track_mesh_chunks(
	alignment_id: String, chunk_length_m: float = 250.0, spacing_m: float = 1.0
) -> Array:
	var cached: Dictionary = _track_mesh_chunk_lists.get(alignment_id, {})
	if not cached.is_empty() and cached["chunk_length_m"] == chunk_length_m and cached["spacing_m"] == spacing_m:
		return cached["chunks"]

	var params := {
		"alignment_id": alignment_id,
		"chunk_length_m": chunk_length_m,
		"spacing_m": spacing_m,
		"metadata_only": true,
	}
	var envelope := await Backend.request("alignment.track_mesh", params)
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return []

	var chunks: Array = envelope.result.get("chunks", [])
	_track_mesh_chunk_lists[alignment_id] = {
		"chunk_length_m": chunk_length_m, "spacing_m": spacing_m, "chunks": chunks
	}
	return chunks


## Test seam (T-135): see [member _terrain_request_override]. An invalid Callable restores the backend.
func set_terrain_request_override(override: Callable) -> void:
	_terrain_request_override = override


## Coroutine. `terrain.tiles` for one level (0 to 4): the `IpcEnvelope` as the backend answered it, so the
## caller can read `result` (`revision`, `tiles`, `source_ids`) on `res` and the code on `err`. `E_BUSY` and
## `E_NOT_FOUND` are the caller's to handle (back off, re-list), and `E_NO_PROJECT` and `E_NO_SESSION` are the
## expected state before a project opens (`project_changed` re-lists); none of the four is reported as a backend
## error, and neither is `E_BAD_PARAMS` from `terrain.tiles` or `imagery.tiles` (too many tiles to list): the field shows
## its own status message for it. Any other failure emits [signal EventBus.backend_error]. Always resolves.
func fetch_terrain_tiles(level: int) -> IpcEnvelope:
	var envelope := await _terrain_request("terrain.tiles", {"level": level})
	_report_terrain_failure(envelope)
	return envelope


## Coroutine. `terrain.tile` for one listed tile of `revision`: the `IpcEnvelope` as the backend answered it.
## On `res`, `result` holds `tile_origin`, `edge_m`, `valid_fraction` and `source_ids`, and `blobs` holds
## `heights` (`PackedFloat32Array`, 129 x 129, row-major) and `source_index` (`PackedByteArray`). A stale
## revision or an unlisted tile is `E_NOT_FOUND`, which the caller answers by listing again. Error reporting
## is as for [method fetch_terrain_tiles].
func fetch_terrain_tile(level: int, ix: int, iy: int, revision: int) -> IpcEnvelope:
	var params := {"level": level, "ix": ix, "iy": iy, "revision": revision}
	var envelope := await _terrain_request("terrain.tile", params)
	_report_terrain_failure(envelope)
	return envelope


func _terrain_request(method: String, params: Dictionary) -> IpcEnvelope:
	if _terrain_request_override.is_valid():
		var replaced: Variant = await _terrain_request_override.call(method, params)
		if replaced is IpcEnvelope:
			return replaced
	return await Backend.request(method, params)


func _report_terrain_failure(envelope: IpcEnvelope) -> void:
	if envelope.type == "res":
		return
	var code := String(envelope.error.get("code", "E_INTERNAL"))
	if code == "E_BUSY" or code == "E_NOT_FOUND" or code == "E_NO_PROJECT" or code == "E_NO_SESSION":
		return
	# A listing refused for too many tiles is one status message from the field, not a backend error (T-135 Part C).
	if code == "E_BAD_PARAMS" and (envelope.method == "terrain.tiles" or envelope.method == "imagery.tiles"):
		return
	EventBus.backend_error.emit(code, envelope.error.get("message", ""))


## Coroutine. `imagery.tiles` for one level (0 to 4): the `IpcEnvelope` as the backend answered it, so the caller can
## read `result` (`revision`, `terrain_revision`, `tiles`, `attributions`, `notices`) on `res`. The error rules are
## those of [method fetch_terrain_tiles]. Always resolves.
func fetch_imagery_tiles(level: int) -> IpcEnvelope:
	var envelope := await _terrain_request("imagery.tiles", {"level": level})
	_report_terrain_failure(envelope)
	return envelope


## Coroutine. `source.list` (T-135 Part B): the `IpcEnvelope` as the backend answered it; on `res`, `result.sources`
## holds each source's `id`, `kind`, `name` and `attribution`. The terrain field reads it once per listing, to name the
## sources in view. Error reporting is as for [method fetch_terrain_tiles].
func fetch_source_list() -> IpcEnvelope:
	var envelope := await _terrain_request("source.list", {})
	_report_terrain_failure(envelope)
	return envelope


## Coroutine. `imagery.tile` for one listed tile of `revision` (T-135 Part B): on `res`, `result` holds `has_alpha` and
## `source_ids`, and `blobs` holds `image` (the JPEG) and, only when `has_alpha`, `alpha`. Error reporting is as for
## [method fetch_terrain_tile].
func fetch_imagery_tile(level: int, ix: int, iy: int, revision: int) -> IpcEnvelope:
	var params := {"level": level, "ix": ix, "iy": iy, "revision": revision}
	var envelope := await _terrain_request("imagery.tile", params)
	_report_terrain_failure(envelope)
	return envelope


## Whether the terrain's source boundaries are drawn (T-135 Part B).
func source_boundaries() -> bool:
	return _source_boundaries


## Records the boundary overlay state and tells the terrain scene. Emits only on a change.
func set_source_boundaries(on: bool) -> void:
	if on == _source_boundaries:
		return
	_source_boundaries = on
	EventBus.source_boundaries_changed.emit(on)


## The data sources of the terrain and imagery in view: dictionaries of `source_id`, `attribution` (which may be
## empty) and `notices`, sorted by source id.
func data_sources() -> Array[Dictionary]:
	return _data_sources


## Replaces the data sources in view (called by [TerrainField]) and emits [signal EventBus.data_sources_changed]
## when they differ from the ones held.
func set_data_sources(entries: Array[Dictionary]) -> void:
	if entries == _data_sources:
		return
	var copy: Array[Dictionary] = []
	for entry: Dictionary in entries:
		copy.append(entry.duplicate(true))
	_data_sources = copy
	EventBus.data_sources_changed.emit()


## The imagery warnings in view, as `imagery.tiles` gave them: plain sentences that name no source (T-136 Part C3).
func imagery_warnings() -> PackedStringArray:
	return _imagery_warnings


## Replaces the imagery warnings (called by [TerrainField]). Stores a copy, and emits [signal EventBus.data_sources_changed]
## only when the list changed.
func set_imagery_warnings(warnings: PackedStringArray) -> void:
	if warnings == _imagery_warnings:
		return
	_imagery_warnings = PackedStringArray(warnings)
	EventBus.data_sources_changed.emit()


## Coroutine. Replaces [method vehicle_catalogue] from `catalogue.vehicles`. Leaves the previous
## catalogue in place and emits [signal EventBus.backend_error] on failure rather than hanging.
func fetch_catalogue() -> void:
	var envelope := await Backend.request("catalogue.vehicles", {})
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return

	_catalogue.clear()
	for entry: Variant in envelope.result.get("vehicles", []):
		if entry is Dictionary:
			_catalogue.append(entry)
	EventBus.catalogue_ready.emit()


## Cached `TrainsetDTO` dictionary for `trainset_id`, or `null` when it has never been fetched or created.
## Use [method fetch_trainset] or [method create_trainset] to populate it.
func trainset(trainset_id: String) -> Variant:
	return _trainsets.get(trainset_id)


## Coroutine. Returns the cached `TrainsetDTO` dictionary for `trainset_id`, fetching it via `trainset.get`
## on a cache miss. Resolves to `null` and emits [signal EventBus.backend_error] on failure rather than
## hanging.
func fetch_trainset(trainset_id: String) -> Variant:
	if _trainsets.has(trainset_id):
		return _trainsets[trainset_id]

	var envelope := await Backend.request("trainset.get", {"trainset_id": trainset_id})
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return null

	_trainsets[trainset_id] = envelope.result
	EventBus.trainset_ready.emit(trainset_id)
	return envelope.result


## Coroutine. Creates a new trainset from catalogue entry `spec_key` via `trainset.create` (`units` repeats
## the whole spec back to back; `name` overrides the default), caching and returning the resulting
## `TrainsetDTO` dictionary. Resolves to `null` and emits [signal EventBus.backend_error] on failure rather
## than hanging.
func create_trainset(spec_key: String, units: int = 1, name: String = "") -> Variant:
	var envelope := await Backend.request(
		"trainset.create", {"spec_key": spec_key, "units": units, "name": name}
	)
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return null

	var trainset_id := str(envelope.result.get("trainset_id", ""))
	_trainsets[trainset_id] = envelope.result
	EventBus.trainset_ready.emit(trainset_id)
	return envelope.result


## Coroutine. `alignment.ground_profile` (T-137 B) at `spacing_m` for one alignment: the ground line, cut and fill per
## station and the volumes, as a dictionary of the result's fields (`terrain_revision`, `template`, `source_names`,
## `attributions`, `no_ground_spans`, ...) plus the decoded blobs: `station`, `rail_h`, `ground_h`, `cut_area` and `fill_area`
## as [PackedFloat64Array], `source` and `flags` as [PackedByteArray]. Cached by alignment and spacing until the project,
## the alignments or the terrain change. Resolves to `{}` and emits [signal EventBus.backend_error] on failure, and the
## sentence of that failure is then [method ground_profile_error].
func fetch_ground_profile(alignment_id: String, spacing_m: float = 10.0) -> Dictionary:
	var key := "%s|%s" % [alignment_id, str(spacing_m)]
	if _ground_profiles.has(key):
		return _ground_profiles[key]

	var generation := _ground_profile_generation
	var envelope := await _ground_profile_request({"alignment_id": alignment_id, "spacing_m": spacing_m})
	if envelope.type != "res":
		var message := String(envelope.error.get("message", ""))
		_ground_profile_error = message
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), message)
		return {}

	_ground_profile_error = ""
	var profile := _decode_ground_profile(envelope)
	if generation == _ground_profile_generation:
		_ground_profiles[key] = profile
	return profile


## The sentence of the last failed [method fetch_ground_profile], or "" after a success.
func ground_profile_error() -> String:
	return _ground_profile_error


## Test seam (T-137 B): see [member _ground_profile_override]. An invalid Callable restores the backend.
func set_ground_profile_request_override(override: Callable) -> void:
	_ground_profile_override = override


func _ground_profile_request(params: Dictionary) -> IpcEnvelope:
	if _ground_profile_override.is_valid():
		var replaced: Variant = await _ground_profile_override.call(params)
		if replaced is IpcEnvelope:
			return replaced
	return await Backend.request("alignment.ground_profile", params)


func _decode_ground_profile(envelope: IpcEnvelope) -> Dictionary:
	var profile: Dictionary = envelope.result.duplicate(true)
	for blob_name: String in ["station", "rail_h", "ground_h", "cut_area", "fill_area"]:
		profile[blob_name] = envelope.blobs.get(blob_name, PackedFloat64Array())
	profile["source"] = envelope.blobs.get("source", PackedByteArray())
	profile["flags"] = envelope.blobs.get("flags", PackedByteArray())
	return profile


func _clear_ground_profiles() -> void:
	_ground_profiles.clear()
	_ground_profile_generation += 1


func _on_ground_profile_sources_changed(kind: String) -> void:
	if kind == "terrain":
		_clear_ground_profiles()


## Coroutine. Imports a `.coypu` archive (its LandXML alignment(s), kinematics runs and stops) into the
## current project, replacing [method alignments] and [method runs]. `params` adds to `{"path": path}` (T-137 B:
## `profile_idealised`). Returns `false` and emits [signal EventBus.backend_error] on failure rather than hanging.
func import_coypu(path: String, params: Dictionary = {}) -> bool:
	var request := params.duplicate()
	request["path"] = path
	var envelope := await Backend.request("import.coypu", request)
	if envelope.type != "res":
		EventBus.backend_error.emit(envelope.error.get("code", "E_INTERNAL"), envelope.error.get("message", ""))
		return false

	_alignments.clear()
	for entry: Variant in envelope.result.get("alignments", []):
		if entry is Dictionary:
			_alignments.append(entry)
	_runs.clear()
	for entry: Variant in envelope.result.get("runs", []):
		if entry is Dictionary:
			_runs.append(entry)

	EventBus.alignments_changed.emit()
	EventBus.runs_changed.emit()
	return true


func _on_layer_changed(_layer_id: String) -> void:
	EventBus.layers_changed.emit()
