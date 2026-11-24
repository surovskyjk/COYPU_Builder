class_name TerrainField
extends Node3D
## The terrain scene (T-135): streams the backend's heightfield display tiles and their imagery around the active
## camera, builds the meshes off the main thread, drapes the imagery and shows them under the world root.
##
## Division of labour, per frame and per request (ADR 0007 addendum, no RPC from `_process`):
## - `_process` selects tiles with [TerrainLod] from the listed levels, queues the terrain tiles it lacks and the
##   imagery of the attached tiles it lacks, attaches up to [constant ATTACH_PER_FRAME] finished jobs, frees the
##   least recently used tiles beyond [constant MAX_TILES], and publishes the data sources in view. It never sends
##   a request.
## - Two paging coroutines, started from `_ready`. [method _run] lists the terrain and then the imagery when asked
##   to, and fetches the terrain tiles, nearest first. [method _run_imagery] (the imagery lane, F52) fetches one
##   imagery tile at a time, and the retries of incomplete compositions. The lanes are independent: a slow imagery
##   reply never holds up terrain. A busy backend (`E_BUSY`, `E_TIMEOUT`) is waited out with exponential backoff;
##   a stale revision (`E_NOT_FOUND`) asks for a new listing.
## - A finished fetch goes to a `WorkerThreadPool` task. A terrain task builds the arrays ([HeightfieldMesh]); an
##   imagery task decodes the JPEG and the alpha and source images ([TerrainMaterial.decode_job]). The `ArrayMesh`,
##   the node and the textures are made on the main thread when the field attaches them ([TerrainTile]).
##
## A revision change keeps the old tiles on screen until their replacements are attached; the old tiles
## whose positions the new listing no longer has are freed only when a replacement is attached, so the field
## never goes blank in the middle of a re-list. Imagery has its own revision: an imagery-only import re-drapes the
## tiles and keeps their meshes.
##
## Layers: `Session.layers()` ids [constant LAYER_ID] ("Terrain", hides the whole field) and [constant
## IMAGERY_LAYER_ID] ("Imagery", hiding it shows the neutral placeholder material). Source boundaries come from
## [method Session.source_boundaries]; the attribution in view is published with [method Session.set_data_sources].
##
## Terrain sources change in the backend (an import), which this field learns of through `E_NOT_FOUND` on a
## tile request, through a new project (`EventBus.project_changed`), through a reconnect, or when [method relist]
## is called.

const LAYER_ID := "terrain"
const IMAGERY_LAYER_ID := "imagery"
const ATTACH_PER_FRAME := 2
const MAX_TILES := 256
const BUSY_BACKOFF_START_SEC := 0.1
const BUSY_BACKOFF_MAX_SEC := 2.0
const IDLE_POLL_SEC := 0.05
## An imagery tile the backend composed without every streamed tile (`complete: false`) is requested again after this
## delay, doubling up to the maximum. The two values are the defaults of the test-settable members below.
const INCOMPLETE_RETRY_START_SEC := 30.0
const INCOMPLETE_RETRY_MAX_SEC := 300.0
## The imagery source that stands for shaded relief; it has no attribution entry and adds nothing to the attribution.
const RELIEF_SOURCE := "relief"

var _camera: Camera3D
var _lists: Dictionary = {}          # level -> {Vector2i(ix, iy) -> {edge_m, tile_origin, valid_fraction}}
var _revision := -1
var _source_ids := PackedStringArray()
var _imagery_lists: Dictionary = {}  # level -> {Vector2i(ix, iy) -> PackedStringArray source_ids}
var _imagery_revision := -1
var _imagery_terrain_revision := -1
var _attributions: Dictionary = {}   # source id -> attribution text, from imagery.tiles
var _notices: Dictionary = {}        # source id -> PackedStringArray of licence notices, from imagery.tiles
var _source_names: Dictionary = {}   # source id -> display name, from source.list (read once per terrain listing)
var _decode_reported_revision := -1  # the imagery revision whose decode failure has been shown in the status bar
var _tiles: Dictionary = {}          # Vector3i(level, ix, iy) -> TerrainTile, attached
var _last_used: Dictionary = {}      # Vector3i -> frame number of the last selection that wanted it
var _pending: Dictionary = {}        # Vector3i -> true from the terrain fetch start until attached or dropped
var _failed: Dictionary = {}         # Vector3i -> true after a failure that is not retried before a new listing
var _imagery_pending: Dictionary = {}  # Vector3i -> true from the imagery fetch start until attached or dropped
var _imagery_failed: Dictionary = {}
var _request_queue: Array[Vector3i] = []
var _imagery_queue: Array[Vector3i] = []
var _incomplete: Dictionary = {}     # Vector3i -> {delay_sec, due_msec}: imagery drawn without all its tiles, due for a retry
## The delays of the retry of incomplete imagery. Settable so that a test can run them in fractions of a second.
var incomplete_retry_start_sec := INCOMPLETE_RETRY_START_SEC
var incomplete_retry_max_sec := INCOMPLETE_RETRY_MAX_SEC
var _selected: Array[Vector3i] = []  # the last selection, nearest first
var _visibility_dirty := false
var _sources_dirty := false
var _boundaries := false
var max_tiles := MAX_TILES           # the cache limit; a test lowers it
var _jobs: Dictionary = {}           # WorkerThreadPool task id -> job, until the task completes
var _ready_queue: Array = []         # finished jobs, in completion order, waiting to attach
var _relist_wanted := false
var _imagery_relist_wanted := false
var _listing := false                # true while the paging lane lists the terrain or the imagery
var _busy_streak := 0               # the terrain lane's E_BUSY streak (listings included)
var _imagery_busy_streak := 0       # the imagery lane's E_BUSY streak
var _frame := 0
var _closing := false
var _built_count := 0
var _build_usec_total := 0
var _drapes_total := 0               # imagery drapes attached so far (the capture's perf block counts them)


## Backoff before the retry after the `streak`-th consecutive `E_BUSY`: 0.1 s, doubling, capped at 2 s.
static func busy_backoff_sec(streak: int) -> float:
	return minf(BUSY_BACKOFF_MAX_SEC, BUSY_BACKOFF_START_SEC * pow(2.0, streak))


func _ready() -> void:
	EventBus.layers_changed.connect(_on_layers_changed)
	EventBus.project_changed.connect(relist)
	EventBus.backend_state_changed.connect(_on_backend_state_changed)
	EventBus.source_boundaries_changed.connect(_on_source_boundaries_changed)
	EventBus.sources_changed.connect(_on_sources_changed)
	Session.layers().define(IMAGERY_LAYER_ID, "Imagery")
	_boundaries = Session.source_boundaries()
	_on_layers_changed()
	_relist_wanted = true
	_run()
	_run_imagery()


func _exit_tree() -> void:
	_closing = true
	for task_id: int in _jobs.keys():
		WorkerThreadPool.wait_for_task_completion(task_id)
	_jobs.clear()
	_ready_queue.clear()


## The camera whose position drives the selection. The capture and the scene pass the active rig's camera
## and call this again when the mode changes.
func set_camera(camera: Camera3D) -> void:
	_camera = camera


## Asks for a new listing of the terrain and the imagery before the next tile request.
func relist() -> void:
	_relist_wanted = true
	_imagery_relist_wanted = true


## The revision of the terrain listing the field works from, or -1 before the first listing.
func revision() -> int:
	return _revision


## The revision of the imagery listing, or -1 before the first listing.
func imagery_revision() -> int:
	return _imagery_revision


func source_ids() -> PackedStringArray:
	return _source_ids


## The terrain listing: level -> {Vector2i(ix, iy) -> {edge_m, tile_origin, valid_fraction}}.
func lists() -> Dictionary:
	return _lists


## The imagery listing: level -> {Vector2i(ix, iy) -> PackedStringArray source_ids}.
func imagery_lists() -> Dictionary:
	return _imagery_lists


## Attribution text per source and licence notices per source, as `imagery.tiles` last gave them.
func attributions() -> Dictionary:
	return _attributions


func notices() -> Dictionary:
	return _notices


func attached_count() -> int:
	return _tiles.size()


func attached_keys() -> Array[Vector3i]:
	var keys: Array[Vector3i] = []
	for key: Vector3i in _tiles.keys():
		keys.append(key)
	return keys


## The attached tile for `key` (`Vector3i(level, ix, iy)`), or null.
func tile_at(key: Vector3i) -> TerrainTile:
	return _tiles.get(key)


## Tiles fetched or being built that are not attached yet.
func pending_count() -> int:
	return _pending.size()


## The number of imagery drapes attached so far.
func imagery_drapes() -> int:
	return _drapes_total


## Mean wall time of the terrain worker builds so far, in microseconds, and how many were built.
func build_stats() -> Dictionary:
	var mean := 0.0
	if _built_count > 0:
		mean = float(_build_usec_total) / _built_count
	return {"count": _built_count, "mean_usec": mean}


## True when nothing is in flight on either lane and every selected tile is attached and, with the Imagery layer on,
## draped with the current imagery (or known to have none). The capture driver waits for it before a shot.
func settled() -> bool:
	if _revision < 0 or _camera == null or not _selection_attached():
		return false
	if _listing or _relist_wanted or _imagery_relist_wanted:
		return false
	if not _request_queue.is_empty() or not _imagery_queue.is_empty():
		return false
	if not _pending.is_empty() or not _imagery_pending.is_empty() or not _jobs.is_empty() or not _ready_queue.is_empty():
		return false
	if not _imagery_visible():
		return true
	for key in _selected:
		var tile: TerrainTile = _tiles[key]
		if tile.has_imagery() and tile.imagery_revision == _imagery_revision:
			continue
		if _imagery_failed.has(key) or not _imagery_listed(key):
			continue
		return false
	return true


func _process(_delta: float) -> void:
	_frame += 1
	_poll_jobs()
	_attach_ready()
	if _camera != null and is_instance_valid(_camera):
		var selected := TerrainLod.select_tiles(_camera.global_position, _lists, TerrainLod.DEFAULT_SPLIT_FACTOR)
		if selected != _selected:
			_selected = selected
			_visibility_dirty = true
		_request_queue.clear()
		_imagery_queue.clear()
		for key in selected:
			if _tiles.has(key):
				_last_used[key] = _frame
			if _needs_fetch(key):
				_request_queue.append(key)
			elif _needs_imagery(key):
				_imagery_queue.append(key)
	_evict()
	if _visibility_dirty:
		_apply_visibility()
	if _sources_dirty:
		_sources_dirty = false
		Session.set_data_sources(_data_source_entries())


func _on_layers_changed() -> void:
	visible = Session.layers().is_visible(LAYER_ID)
	var imagery_on := _imagery_visible()
	for key: Vector3i in _tiles.keys():
		(_tiles[key] as TerrainTile).show_imagery(imagery_on)
	_sources_dirty = true


func _on_source_boundaries_changed(on: bool) -> void:
	_boundaries = on
	for key: Vector3i in _tiles.keys():
		(_tiles[key] as TerrainTile).set_boundaries(on)


## A source was imported (T-136): list again, so the new source shows without waiting for a stale revision.
func _on_sources_changed(_kind: String) -> void:
	relist()


func _on_backend_state_changed(state: int) -> void:
	if state == Backend.State.READY:
		relist()


## The terrain paging coroutine (the terrain lane): lists when asked, otherwise fetches one queued terrain tile at a
## time and idles between checks. `_listing` is set for the whole of each listing, so the imagery lane waits. It
## ends when the field leaves the tree.
func _run() -> void:
	while not _closing and is_inside_tree():
		if _relist_wanted:
			if Backend.state() == Backend.State.READY:
				_listing = true
				await _list_terrain()
				_listing = false
			else:
				await get_tree().create_timer(IDLE_POLL_SEC).timeout
		elif _imagery_relist_wanted:
			if Backend.state() == Backend.State.READY:
				_listing = true
				await _list_imagery()
				_listing = false
			else:
				await get_tree().create_timer(IDLE_POLL_SEC).timeout
		elif not _request_queue.is_empty():
			var key: Vector3i = _request_queue.pop_front()
			await _fetch(key)
		else:
			await get_tree().create_timer(IDLE_POLL_SEC).timeout


## The imagery paging coroutine (the imagery lane, F52): fetches one queued imagery tile at a time, so at most one
## `imagery.tile` is in flight, and a slow reply never holds the terrain lane. When nothing is queued it retries an
## incomplete composition whose delay has passed. It starts no request while a listing is wanted or running. It ends
## when the field leaves the tree.
func _run_imagery() -> void:
	while not _closing and is_inside_tree():
		await get_tree().process_frame
		if _closing or not is_inside_tree():
			return
		if _listing or _relist_wanted or _imagery_relist_wanted:
			await get_tree().create_timer(IDLE_POLL_SEC).timeout
		elif not _imagery_queue.is_empty():
			var imagery_key: Vector3i = _imagery_queue.pop_front()
			await _fetch_imagery(imagery_key)
		else:
			var retry: Variant = _due_retry()
			if retry == null:
				await get_tree().create_timer(IDLE_POLL_SEC).timeout
			else:
				await _fetch_imagery(retry, true)


## The first incomplete tile whose retry is due, or null. A record that no longer applies (the tile left the view,
## was replaced, or the imagery revision changed) is dropped here, so it can never be returned again.
func _due_retry() -> Variant:
	var now := Time.get_ticks_msec()
	for key: Vector3i in _incomplete.keys():
		if not _retry_still_wanted(key):
			_incomplete.erase(key)
		elif _imagery_pending.has(key):
			continue
		elif int(_incomplete[key]["due_msec"]) <= now:
			return key
	return null


## A record still applies while its tile is on screen and attached to the current terrain. The imagery revision is not
## checked: a tile whose drape job has not attached yet has no current imagery, and that must not drop its record.
func _retry_still_wanted(key: Vector3i) -> bool:
	if not _tiles.has(key) or not _selected.has(key):
		return false
	var tile: TerrainTile = _tiles[key]
	return tile.revision == _revision


## Records the outcome of an imagery reply for the retry: a complete composition ends any retry; an incomplete one is
## due again after the current delay, which doubles on each attempt, up to the maximum.
func _note_completeness(key: Vector3i, complete: bool) -> void:
	if complete:
		_incomplete.erase(key)
		return
	var delay := incomplete_retry_start_sec
	if _incomplete.has(key):
		delay = minf(incomplete_retry_max_sec, float(_incomplete[key]["delay_sec"]) * 2.0)
	_incomplete[key] = {"delay_sec": delay, "due_msec": Time.get_ticks_msec() + int(delay * 1000.0)}


## Lists the terrain levels, coarsest first. Every level must report the same revision; a change in between
## means the sources changed while listing, so the listing starts again. The imagery follows, since its revision
## moves with the terrain's.
func _list_terrain() -> void:
	_relist_wanted = false
	var lists := {}
	var revision := -1
	var source_ids := PackedStringArray()
	var refused := []
	for level in range(TerrainLod.MAX_LEVEL, -1, -1):
		var envelope := await Session.fetch_terrain_tiles(level)
		if _closing or not is_inside_tree():
			return
		if envelope.type != "res":
			var code := String(envelope.error.get("code", ""))
			if code == "E_BAD_PARAMS":
				lists[level] = {}
				refused.append(level)
				continue
			if code == "E_BUSY" or code == "E_TIMEOUT":
				_relist_wanted = true
				await get_tree().create_timer(busy_backoff_sec(_busy_streak)).timeout
				_busy_streak += 1
			return
		var listed_revision := int(envelope.result.get("revision", 0))
		if revision == -1:
			revision = listed_revision
			for source: Variant in envelope.result.get("source_ids", []):
				source_ids.append(String(source))
		elif listed_revision != revision:
			_relist_wanted = true
			return
		lists[level] = _index_tiles(envelope.result.get("tiles", []))
	if not refused.is_empty():
		EventBus.status_message.emit(_refusal_message("Terrain", refused))
	if revision == -1:
		return
	_lists = lists
	_revision = revision
	_source_ids = source_ids
	_failed.clear()
	_busy_streak = 0
	_visibility_dirty = true
	await _read_source_names()
	if _closing or not is_inside_tree():
		return
	_imagery_relist_wanted = true
	if _is_empty(lists):
		_free_all_tiles()


## Reads `source.list` once per terrain or imagery listing, for the names the data sources are shown with. A failure keeps the
## names read before; a source it does not name is shown by its kind alone.
func _read_source_names() -> void:
	var envelope := await Session.fetch_source_list()
	if envelope.type != "res":
		return
	var names := {}
	for source: Variant in envelope.result.get("sources", []):
		var info: Dictionary = source
		names[String(info.get("id", ""))] = String(info.get("name", ""))
	_source_names = names
	_sources_dirty = true


## Lists the imagery levels, coarsest first, with the same rules as the terrain listing. A listing whose
## terrain revision is not the one the field works from asks for the terrain again.
func _list_imagery() -> void:
	_imagery_relist_wanted = false
	var lists := {}
	var revision := -1
	var terrain_revision := -1
	var attributions := {}
	var notices := {}
	var refused := []
	for level in range(TerrainLod.MAX_LEVEL, -1, -1):
		var envelope := await Session.fetch_imagery_tiles(level)
		if _closing or not is_inside_tree():
			return
		if envelope.type != "res":
			var code := String(envelope.error.get("code", ""))
			if code == "E_BAD_PARAMS":
				lists[level] = {}
				refused.append(level)
				continue
			if code == "E_BUSY" or code == "E_TIMEOUT":
				_imagery_relist_wanted = true
				await get_tree().create_timer(busy_backoff_sec(_busy_streak)).timeout
				_busy_streak += 1
			return
		var listed_revision := int(envelope.result.get("revision", 0))
		if revision == -1:
			revision = listed_revision
			terrain_revision = int(envelope.result.get("terrain_revision", -1))
			var texts: Dictionary = envelope.result.get("attributions", {})
			for source_id: Variant in texts.keys():
				attributions[String(source_id)] = String(texts[source_id])
			var notice_map: Dictionary = envelope.result.get("notices", {})
			for source_id: Variant in notice_map.keys():
				notices[String(source_id)] = PackedStringArray(notice_map[source_id])
		elif listed_revision != revision:
			_imagery_relist_wanted = true
			return
		lists[level] = _index_imagery(envelope.result.get("tiles", []))
	if not refused.is_empty():
		EventBus.status_message.emit(_refusal_message("Imagery", refused))
	if revision == -1:
		return
	_imagery_lists = lists
	if revision != _imagery_revision:
		_incomplete.clear()
	_imagery_revision = revision
	_imagery_terrain_revision = terrain_revision
	_attributions = attributions
	_notices = notices
	_imagery_failed.clear()
	_busy_streak = 0
	_sources_dirty = true
	# An imagery import adds a source the terrain listing has not named, so the names are read again here too.
	await _read_source_names()
	if _closing or not is_inside_tree():
		return
	if terrain_revision != _revision:
		_relist_wanted = true


func _fetch(key: Vector3i) -> void:
	if _pending.has(key):
		return
	_pending[key] = true
	var revision := _revision
	var envelope := await Session.fetch_terrain_tile(key.x, key.y, key.z, revision)
	if _closing or not is_inside_tree():
		return
	_pending.erase(key)
	if envelope.type == "res":
		_busy_streak = 0
		if revision == _revision:
			_start_job(key, envelope, revision)
		return
	var code := String(envelope.error.get("code", ""))
	if code == "E_BUSY" or code == "E_TIMEOUT":
		var delay := busy_backoff_sec(_busy_streak)
		_busy_streak += 1
		await get_tree().create_timer(delay).timeout
	elif code == "E_NOT_FOUND":
		_relist_wanted = true
	else:
		_failed[key] = true


## One imagery request (the imagery lane). `retry` asks again for a tile that is already draped with the current
## imagery, because its composition was incomplete; `_retry_still_wanted` has checked that it still applies.
func _fetch_imagery(key: Vector3i, retry := false) -> void:
	if _imagery_pending.has(key) or not _tiles.has(key):
		return
	var queued_tile: TerrainTile = _tiles[key]
	if not retry and queued_tile.imagery_revision == _imagery_revision:
		return
	_imagery_pending[key] = true
	var revision := _imagery_revision
	var envelope := await Session.fetch_imagery_tile(key.x, key.y, key.z, revision)
	if _closing or not is_inside_tree():
		return
	_imagery_pending.erase(key)
	if envelope.type == "res":
		_imagery_busy_streak = 0
		if revision == _imagery_revision and _tiles.has(key):
			_start_imagery_job(key, envelope)
			_note_completeness(key, bool(envelope.result.get("complete", true)))
		return
	var code := String(envelope.error.get("code", ""))
	if code == "E_BUSY" or code == "E_TIMEOUT":
		var delay := busy_backoff_sec(_imagery_busy_streak)
		_imagery_busy_streak += 1
		await get_tree().create_timer(delay).timeout
	elif revision != _imagery_revision:
		return
	elif code == "E_NOT_FOUND":
		_imagery_relist_wanted = true
	else:
		_imagery_failed[key] = true
		_incomplete.erase(key)


func _start_job(key: Vector3i, envelope: IpcEnvelope, revision: int) -> void:
	var edge := float(envelope.result.get("edge_m", 0.0))
	var heights: PackedFloat32Array = envelope.blobs.get("heights", PackedFloat32Array())
	var job := {
		"kind": "terrain",
		"key": key,
		"revision": revision,
		"origin": _vector3_of(envelope.result.get("tile_origin", [0.0, 0.0, 0.0])),
		"edge_m": edge,
		"heights": heights,
		"skirt_m": TerrainTile.skirt_for(edge),
		"source_ids": PackedStringArray(envelope.result.get("source_ids", [])),
		"source_index": envelope.blobs.get("source_index", PackedByteArray()),
		"arrays": {},
		"build_usec": 0,
	}
	_pending[key] = true
	var task_id := WorkerThreadPool.add_task(_build_terrain_job.bind(job))
	_jobs[task_id] = job


## Worker entry point of a terrain job: the mesh arrays ([HeightfieldMesh]) and the distinct source indices of the
## tile's source map, so the main thread never scans the 16641 samples.
static func _build_terrain_job(job: Dictionary) -> void:
	HeightfieldMesh.compute_job(job)
	var present := PackedByteArray()
	present.resize(256)
	var index: PackedByteArray = job["source_index"]
	for value in index:
		present[value] = 1
	var used := PackedInt32Array()
	for value in 256:
		if present[value] == 1:
			used.append(value)
	job["used_sources"] = used


## The imagery job decodes on a worker. It carries the terrain tile's own source map, which the texture of the
## source boundaries is made from.
func _start_imagery_job(key: Vector3i, envelope: IpcEnvelope) -> void:
	var tile: TerrainTile = _tiles[key]
	var has_alpha := bool(envelope.result.get("has_alpha", false))
	var job := {
		"kind": "imagery",
		"key": key,
		"revision": _imagery_revision,
		"terrain_revision": tile.revision,
		"source_ids": PackedStringArray(envelope.result.get("source_ids", [])),
		"jpeg": envelope.blobs.get("image", PackedByteArray()),
		"alpha": envelope.blobs.get("alpha", PackedByteArray()) if has_alpha else PackedByteArray(),
		"source_index": tile.source_index,
		"decode_error": "",
		"albedo": null,
		"alpha_image": null,
		"source_image": null,
	}
	_imagery_pending[key] = true
	var task_id := WorkerThreadPool.add_task(TerrainMaterial.decode_job.bind(job))
	_jobs[task_id] = job


func _poll_jobs() -> void:
	for task_id: int in _jobs.keys():
		if WorkerThreadPool.is_task_completed(task_id):
			WorkerThreadPool.wait_for_task_completion(task_id)
			var job: Dictionary = _jobs[task_id]
			_jobs.erase(task_id)
			if job["kind"] == "terrain":
				_built_count += 1
				_build_usec_total += int(job["build_usec"])
			_ready_queue.append(job)


func _attach_ready() -> void:
	var attached := 0
	while attached < ATTACH_PER_FRAME and not _ready_queue.is_empty():
		var job: Dictionary = _ready_queue.pop_front()
		if job["kind"] == "imagery":
			_attach_imagery(job)
			attached += 1
			continue
		var key: Vector3i = job["key"]
		_pending.erase(key)
		if int(job["revision"]) != _revision:
			continue
		if (job["arrays"] as Dictionary).is_empty():
			_failed[key] = true
			continue
		_install(job)
		attached += 1


func _install(job: Dictionary) -> void:
	var key: Vector3i = job["key"]
	var fresh := TerrainTile.new()
	fresh.setup(key, job["origin"], job["edge_m"], job["revision"], job["heights"])
	fresh.set_sources(job["source_ids"], job["source_index"], job["used_sources"])
	fresh.set_arrays(job["arrays"])
	fresh.show_imagery(_imagery_visible())
	fresh.set_boundaries(_boundaries)
	add_child(fresh)
	if _tiles.has(key):
		var replaced: TerrainTile = _tiles[key]
		replaced.queue_free()
	_tiles[key] = fresh
	if not _last_used.has(key):
		_last_used[key] = _frame
	_prune_absent()
	_visibility_dirty = true


## Drapes an attached tile with its decoded imagery, when both are still current.
func _attach_imagery(job: Dictionary) -> void:
	var key: Vector3i = job["key"]
	_imagery_pending.erase(key)
	if not _tiles.has(key):
		return
	var tile: TerrainTile = _tiles[key]
	# The job is current only when its imagery revision and its terrain revision still are, and the tile it was
	# fetched for is the tile on screen; otherwise it is dropped rather than draped.
	var terrain_revision := int(job["terrain_revision"])
	if int(job["revision"]) != _imagery_revision or terrain_revision != _revision or tile.revision != terrain_revision:
		return
	if String(job["decode_error"]) != "":
		_imagery_failed[key] = true
		# Once per imagery revision, in the log and on the status bar alike, not once per tile.
		if _decode_reported_revision != _imagery_revision:
			_decode_reported_revision = _imagery_revision
			push_warning("TerrainField: %s" % String(job["decode_error"]))
			EventBus.status_message.emit("Some imagery could not be shown.")
		return
	var material := TerrainMaterial.for_images(job["albedo"], job["alpha_image"], job["source_image"])
	material.set_boundaries(_boundaries)
	tile.set_imagery(material, _imagery_revision, PackedStringArray(job["source_ids"]), _imagery_visible())
	_drapes_total += 1
	_sources_dirty = true


## Frees tiles of an older revision that the current listing does not have. Called only after a replacement
## has been attached, so the field is never empty because of it.
func _prune_absent() -> void:
	for key: Vector3i in _tiles.keys():
		var tile: TerrainTile = _tiles[key]
		if tile.revision != _revision and not _listed(key):
			tile.queue_free()
			_tiles.erase(key)
			_last_used.erase(key)


## Least recently wanted tiles beyond the limit are freed. A tile the current selection wants is never freed, and
## neither is a tile on screen: it may stand in for selected children that are not attached yet, so the cache can
## exceed the limit for a moment.
func _evict() -> void:
	if _tiles.size() <= max_tiles:
		return
	var candidates: Array = []
	for key: Vector3i in _tiles.keys():
		if int(_last_used.get(key, -1)) != _frame and not (_tiles[key] as TerrainTile).visible:
			candidates.append([int(_last_used.get(key, -1)), key])
	candidates.sort_custom(func(a: Array, b: Array) -> bool: return a[0] < b[0])
	for entry: Array in candidates:
		if _tiles.size() <= max_tiles:
			break
		var key: Vector3i = entry[1]
		var tile: TerrainTile = _tiles[key]
		tile.queue_free()
		_tiles.erase(key)
		_last_used.erase(key)
		_visibility_dirty = true


## A new listing without a single tile (another project, or the terrain was removed) has nothing to replace
## what is on screen, so the old tiles go at once.
func _free_all_tiles() -> void:
	for key: Vector3i in _tiles.keys():
		(_tiles[key] as TerrainTile).queue_free()
	_tiles.clear()
	_last_used.clear()
	_visibility_dirty = true


## Shows the selected tiles, and hides every other cached tile once the selected tiles over its area (its
## quadtree ancestor, or its descendants) are all attached. Until then it stays visible, so refining or
## coarsening never leaves a hole, and a finished refinement never leaves the coarse tile drawn over its children.
func _apply_visibility() -> void:
	_visibility_dirty = false
	_sources_dirty = true
	var wanted := {}
	var below := {}  # tile key -> [selected descendants, of which not attached yet]
	for key in _selected:
		wanted[key] = true
		var missing := 0 if _tiles.has(key) else 1
		var ix := key.y
		var iy := key.z
		for level in range(key.x + 1, TerrainLod.MAX_LEVEL + 1):
			ix >>= 1
			iy >>= 1
			var ancestor := Vector3i(level, ix, iy)
			var counts: Array = below.get(ancestor, [0, 0])
			counts[0] += 1
			counts[1] += missing
			below[ancestor] = counts
	for key: Vector3i in _tiles.keys():
		(_tiles[key] as TerrainTile).visible = wanted.has(key) or not _covered(key, wanted, below)


func _covered(key: Vector3i, wanted: Dictionary, below: Dictionary) -> bool:
	var counts: Array = below.get(key, [0, 0])
	if counts[0] > 0:
		return counts[1] == 0
	var ix := key.y
	var iy := key.z
	for level in range(key.x + 1, TerrainLod.MAX_LEVEL + 1):
		ix >>= 1
		iy >>= 1
		var ancestor := Vector3i(level, ix, iy)
		if wanted.has(ancestor):
			return _tiles.has(ancestor)
	return false


## The data sources of the tiles in view: each shown tile's terrain sources, and with the Imagery layer on, its
## imagery sources except relief. Grouped by kind, terrain first, then imagery, each group highest precedence first:
## terrain in the order of the terrain listing's `source_ids`, imagery in the first-seen order across the visible
## tiles. The tiles are read in key order, so the order does not depend on the order they arrived in.
func _data_source_entries() -> Array[Dictionary]:
	var entries: Array[Dictionary] = []
	if not Session.layers().is_visible(LAYER_ID):
		return entries  # a hidden terrain shows nothing, so it credits nothing
	var terrain := {}                   # source id -> true
	var imagery := PackedStringArray()  # imagery source ids, first seen first
	var imagery_on := _imagery_visible()
	var keys: Array = _tiles.keys()
	keys.sort()
	for key: Vector3i in keys:
		var tile: TerrainTile = _tiles[key]
		if not tile.visible:
			continue
		for source_id in tile.terrain_source_ids():
			terrain[source_id] = true
		if imagery_on and tile.has_imagery():
			for source_id in tile.imagery_source_ids:
				if source_id != RELIEF_SOURCE and not imagery.has(source_id):
					imagery.append(source_id)
	for source_id: Variant in terrain_order(terrain.keys(), _source_ids):
		entries.append(_source_entry(String(source_id), "terrain"))
	for source_id in imagery:
		if not terrain.has(source_id):
			entries.append(_source_entry(source_id, "imagery"))
	return entries


func _source_entry(source_id: String, kind: String) -> Dictionary:
	return {
		"source_id": source_id,
		"kind": kind,
		"name": String(_source_names.get(source_id, "")),
		"attribution": String(_attributions.get(source_id, "")),
		"notices": _notices.get(source_id, PackedStringArray()),
	}


## The terrain source ids in precedence order: by their position in `precedence` (the terrain listing's `source_ids`,
## highest first), an id the listing does not name last, and ties by id.
static func terrain_order(ids: Array, precedence: PackedStringArray) -> Array:
	var ordered := ids.duplicate()
	ordered.sort_custom(func(a: Variant, b: Variant) -> bool: return _precedes(String(a), String(b), precedence))
	return ordered


static func _precedes(a: String, b: String, precedence: PackedStringArray) -> bool:
	var rank_a := _rank(a, precedence)
	var rank_b := _rank(b, precedence)
	if rank_a != rank_b:
		return rank_a < rank_b
	return a < b


static func _rank(source_id: String, precedence: PackedStringArray) -> int:
	var rank := precedence.find(source_id)
	return rank if rank >= 0 else precedence.size()


func _imagery_visible() -> bool:
	return Session.layers().is_visible(IMAGERY_LAYER_ID)


func _selection_attached() -> bool:
	if _selected.is_empty():
		return false
	for key in _selected:
		if not _tiles.has(key):
			return false
	return true


func _needs_fetch(key: Vector3i) -> bool:
	if _pending.has(key) or _failed.has(key):
		return false
	if _tiles.has(key):
		var tile: TerrainTile = _tiles[key]
		if tile.revision == _revision:
			return false
	return _listed(key)


func _needs_imagery(key: Vector3i) -> bool:
	if not _tiles.has(key) or _imagery_pending.has(key) or _imagery_failed.has(key):
		return false
	if not _imagery_listed(key):
		return false
	var tile: TerrainTile = _tiles[key]
	return tile.revision == _revision and tile.imagery_revision != _imagery_revision


func _listed(key: Vector3i) -> bool:
	var level_map: Dictionary = _lists.get(key.x, {})
	return level_map.has(Vector2i(key.y, key.z))


func _imagery_listed(key: Vector3i) -> bool:
	var level_map: Dictionary = _imagery_lists.get(key.x, {})
	return level_map.has(Vector2i(key.y, key.z))


## The one status-bar message for the levels a listing refused with `E_BAD_PARAMS` (too many tiles to list). Terrain
## falls back to the coarser levels; imagery's tiles show without imagery, since their terrain still shows.
static func _refusal_message(kind: String, levels: Array) -> String:
	var names := PackedStringArray()
	for level: Variant in levels:
		names.append(str(level))
	var noun := "level" if levels.size() == 1 else "levels"
	var verb := "has" if levels.size() == 1 else "have"
	var fallback := "those tiles show without imagery." if kind == "Imagery" else "showing coarser levels."
	return "%s %s %s %s too many tiles to list; %s" % [kind, noun, ", ".join(names), verb, fallback]


static func _is_empty(lists: Dictionary) -> bool:
	for level: int in lists.keys():
		if not (lists[level] as Dictionary).is_empty():
			return false
	return true


static func _index_tiles(entries: Array) -> Dictionary:
	var map := {}
	for entry: Variant in entries:
		var info: Dictionary = entry
		map[Vector2i(int(info["ix"]), int(info["iy"]))] = {
			"edge_m": float(info["edge_m"]),
			"tile_origin": _vector3_of(info["tile_origin"]),
			"valid_fraction": float(info["valid_fraction"]),
		}
	return map


static func _index_imagery(entries: Array) -> Dictionary:
	var map := {}
	for entry: Variant in entries:
		var info: Dictionary = entry
		var ids := PackedStringArray()
		for source: Variant in info.get("source_ids", []):
			ids.append(String(source))
		map[Vector2i(int(info["ix"]), int(info["iy"]))] = ids
	return map


static func _vector3_of(value: Variant) -> Vector3:
	var components: Array = value
	return Vector3(float(components[0]), float(components[1]), float(components[2]))
