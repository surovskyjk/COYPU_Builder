extends Node
## Autoload `Session`: the client's read-only mirror of backend document state (ADR 0007 — the client
## interpolates baked tables and computes no geometry of its own). T-103 populates project identity and
## alignment summaries from `project.get`/`project.new`/`import.landxml`; T-115 adds the alignment/run
## table caches, the vehicle catalogue, the entity registry and layer state; T-121 adds the
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

## trainset_id -> TrainsetDTO dictionary, as decoded from `trainset.create`/`trainset.get`.
var _trainsets: Dictionary = {}

var _entities := EntityRegistry.new()
var _layers := LayerState.new()


func _ready() -> void:
	_layers.layer_changed.connect(_on_layer_changed)


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


## Coroutine. Imports a `.coypu` archive (its LandXML alignment(s), kinematics runs and stops) into the
## current project, replacing [method alignments] and [method runs]. Returns `false` and emits
## [signal EventBus.backend_error] on failure rather than hanging.
func import_coypu(path: String) -> bool:
	var envelope := await Backend.request("import.coypu", {"path": path})
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
