class_name TerrainField
extends Node3D
## The terrain scene (T-135, Part A): streams the backend's heightfield display tiles around the active camera,
## builds their meshes off the main thread and shows them under the world root.
##
## Division of labour, per frame and per request (ADR 0007 addendum, no RPC from `_process`):
## - `_process` selects tiles with [TerrainLod] from the listed levels, queues the ones it lacks, attaches up
##   to [constant ATTACH_PER_FRAME] finished tiles and frees the least recently used beyond [constant MAX_TILES].
##   It never sends a request.
## - One paging coroutine ([method _run], started from `_ready`) lists the levels when it is asked to and then
##   fetches the queue one `terrain.tile` at a time, nearest first. A busy backend (`E_BUSY`) is waited out with
##   exponential backoff; a stale revision (`E_NOT_FOUND`) asks for a new listing.
## - A finished fetch goes to a `WorkerThreadPool` task that only computes arrays ([HeightfieldMesh]). The
##   `ArrayMesh` and the node are made on the main thread when the field attaches them ([TerrainTile]).
##
## A revision change keeps the old tiles on screen until their replacements are attached; the old tiles
## whose positions the new listing no longer has are freed only when a replacement is attached, so the field
## never goes blank in the middle of a re-list.
##
## Layer: `Session.layers()` id [constant LAYER_ID] ("Terrain"); hiding it hides the whole field.
##
## Terrain sources change in the backend (an import), which this field learns of through `E_NOT_FOUND` on a
## tile request, through a new project (`EventBus.project_changed`), through a reconnect, or when [method relist]
## is called. There is no import signal on [EventBus] yet (T-136 adds the import dialog and will call [method relist]).

const LAYER_ID := "terrain"
const ATTACH_PER_FRAME := 2
const MAX_TILES := 256
const BUSY_BACKOFF_START_SEC := 0.1
const BUSY_BACKOFF_MAX_SEC := 2.0
const IDLE_POLL_SEC := 0.05

var _camera: Camera3D
var _lists: Dictionary = {}          # level -> {Vector2i(ix, iy) -> {edge_m, tile_origin, valid_fraction}}
var _revision := -1
var _source_ids := PackedStringArray()
var _tiles: Dictionary = {}          # Vector3i(level, ix, iy) -> TerrainTile, attached
var _last_used: Dictionary = {}      # Vector3i -> frame number of the last selection that wanted it
var _pending: Dictionary = {}        # Vector3i -> true from the fetch start until the tile is attached or dropped
var _failed: Dictionary = {}         # Vector3i -> true after a failure that is not retried before a new listing
var _request_queue: Array[Vector3i] = []
var _selected: Array[Vector3i] = []  # the last selection, nearest first
var _visibility_dirty := false
var max_tiles := MAX_TILES           # the cache limit; a test lowers it
var _jobs: Dictionary = {}           # WorkerThreadPool task id -> job, until the task completes
var _ready_queue: Array = []         # finished jobs, in completion order, waiting to attach
var _relist_wanted := false
var _busy_streak := 0
var _frame := 0
var _closing := false
var _built_count := 0
var _build_usec_total := 0


## Backoff before the retry after the `streak`-th consecutive `E_BUSY`: 0.1 s, doubling, capped at 2 s.
static func busy_backoff_sec(streak: int) -> float:
	return minf(BUSY_BACKOFF_MAX_SEC, BUSY_BACKOFF_START_SEC * pow(2.0, streak))


func _ready() -> void:
	EventBus.layers_changed.connect(_on_layers_changed)
	EventBus.project_changed.connect(relist)
	EventBus.backend_state_changed.connect(_on_backend_state_changed)
	_on_layers_changed()
	_relist_wanted = true
	_run()


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


## Asks for a new listing of every level before the next tile request.
func relist() -> void:
	_relist_wanted = true


## The revision of the listing the field works from, or -1 before the first listing.
func revision() -> int:
	return _revision


func source_ids() -> PackedStringArray:
	return _source_ids


## The listing: level -> {Vector2i(ix, iy) -> {edge_m, tile_origin, valid_fraction}}.
func lists() -> Dictionary:
	return _lists


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


## Mean wall time of the worker builds so far, in microseconds, and how many were built.
func build_stats() -> Dictionary:
	var mean := 0.0
	if _built_count > 0:
		mean = float(_build_usec_total) / _built_count
	return {"count": _built_count, "mean_usec": mean}


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
		for key in selected:
			if _tiles.has(key):
				_last_used[key] = _frame
			if _needs_fetch(key):
				_request_queue.append(key)
	_evict()
	if _visibility_dirty:
		_apply_visibility()


func _on_layers_changed() -> void:
	visible = Session.layers().is_visible(LAYER_ID)


func _on_backend_state_changed(state: int) -> void:
	if state == Backend.State.READY:
		_relist_wanted = true


## The one paging coroutine: lists when asked, otherwise fetches one queued tile at a time and idles
## between checks. It ends when the field leaves the tree.
func _run() -> void:
	while not _closing and is_inside_tree():
		if _relist_wanted:
			if Backend.state() == Backend.State.READY:
				await _list_all()
			else:
				await get_tree().create_timer(IDLE_POLL_SEC).timeout
		elif _request_queue.is_empty():
			await get_tree().create_timer(IDLE_POLL_SEC).timeout
		else:
			var key: Vector3i = _request_queue.pop_front()
			await _fetch(key)


## Lists all five levels, coarsest first. Every level must report the same revision; a change in between
## means the sources changed while listing, so the listing starts again.
func _list_all() -> void:
	_relist_wanted = false
	var lists := {}
	var revision := -1
	var source_ids := PackedStringArray()
	for level in range(TerrainLod.MAX_LEVEL, -1, -1):
		var envelope := await Session.fetch_terrain_tiles(level)
		if _closing or not is_inside_tree():
			return
		if envelope.type != "res":
			var code := String(envelope.error.get("code", ""))
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
	_lists = lists
	_revision = revision
	_source_ids = source_ids
	_failed.clear()
	_busy_streak = 0
	_visibility_dirty = true
	if _is_empty(lists):
		_free_all_tiles()


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


func _start_job(key: Vector3i, envelope: IpcEnvelope, revision: int) -> void:
	var edge := float(envelope.result.get("edge_m", 0.0))
	var heights: PackedFloat32Array = envelope.blobs.get("heights", PackedFloat32Array())
	var job := {
		"key": key,
		"revision": revision,
		"origin": _vector3_of(envelope.result.get("tile_origin", [0.0, 0.0, 0.0])),
		"edge_m": edge,
		"heights": heights,
		"skirt_m": TerrainTile.skirt_for(edge),
		"arrays": {},
		"build_usec": 0,
	}
	_pending[key] = true
	var task_id := WorkerThreadPool.add_task(HeightfieldMesh.compute_job.bind(job))
	_jobs[task_id] = job


func _poll_jobs() -> void:
	for task_id: int in _jobs.keys():
		if WorkerThreadPool.is_task_completed(task_id):
			WorkerThreadPool.wait_for_task_completion(task_id)
			var job: Dictionary = _jobs[task_id]
			_jobs.erase(task_id)
			_built_count += 1
			_build_usec_total += int(job["build_usec"])
			_ready_queue.append(job)


func _attach_ready() -> void:
	var attached := 0
	while attached < ATTACH_PER_FRAME and not _ready_queue.is_empty():
		var job: Dictionary = _ready_queue.pop_front()
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
	fresh.set_arrays(job["arrays"])
	add_child(fresh)
	if _tiles.has(key):
		var replaced: TerrainTile = _tiles[key]
		replaced.queue_free()
	_tiles[key] = fresh
	if not _last_used.has(key):
		_last_used[key] = _frame
	_prune_absent()
	_visibility_dirty = true


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


static func _is_empty(lists: Dictionary) -> bool:
	for level: int in lists.keys():
		if not (lists[level] as Dictionary).is_empty():
			return false
	return true


## Shows the selected tiles, and hides every other cached tile once the selected tiles over its area (its
## quadtree ancestor, or its descendants) are all attached. Until then it stays visible, so refining or
## coarsening never leaves a hole, and a finished refinement never leaves the coarse tile drawn over its children.
func _apply_visibility() -> void:
	_visibility_dirty = false
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


func _needs_fetch(key: Vector3i) -> bool:
	if _pending.has(key) or _failed.has(key):
		return false
	if _tiles.has(key):
		var tile: TerrainTile = _tiles[key]
		if tile.revision == _revision:
			return false
	return _listed(key)


func _listed(key: Vector3i) -> bool:
	var level_map: Dictionary = _lists.get(key.x, {})
	return level_map.has(Vector2i(key.y, key.z))


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


static func _vector3_of(value: Variant) -> Vector3:
	var components: Array = value
	return Vector3(float(components[0]), float(components[1]), float(components[2]))
