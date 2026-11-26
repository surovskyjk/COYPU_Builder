extends GdUnitTestSuite
## Live backend integration test for TerrainField (T-135, Part A).
##
## The backend's synthetic terrain (backend/tests/fixtures/synthetic/terrain.py) is a test-only text format
## read by a reader that only the backend's own tests register, so a spawned backend cannot import it. This
## suite writes the same analytic surface as a LandXML TIN into user:// (outside the repository), imports it
## with `import.run` (kind "terrain"), and checks the field against `terrain.tile` for the same backend. The
## surface formula below is copied from terrain.py's `surface()`, with the same constants.
##
## Tests run in declaration order and share the backend. Each one starts its own project and field; the
## `after_test` hook frees the field and camera and clears the Session seam.

const _READY_TIMEOUT_SEC := 15.0
const _FIELD_TIMEOUT_SEC := 90.0
const _POLL_STEP_SEC := 0.05
const _SPACING_M := 20.0
const _FILE_A := "user://coypu_terrain_field_a.xml"
const _FILE_B := "user://coypu_terrain_field_b.xml"
## Part B: a synthetic orthophoto over the same box (a PNG with a world file) and a second one, for the re-drape test.
const _FILE_IMAGE := "user://coypu_terrain_field_image.png"
const _FILE_IMAGE_B := "user://coypu_terrain_field_image_b.png"
const _IMAGE_PIXEL_M := 4.0
## KRAV82_BBOX of terrain.py: min_e, min_n, max_e, max_n.
const _BOX := [-747650.0, -1026200.0, -744850.0, -1023800.0]
const _PATCH := [-746800.0, -1025200.0, -746600.0, -1025000.0]
const _BASE_E := -746250.0
const _BASE_N := -1025000.0
const _BASE_H := 178.5
## 1.2 km west of the box: the nearer tiles refine to level 0, the far ones stay at level 1 (see the report).
const _CAMERA_A := Vector3(-2600.0, 150.0, 0.0)
const _CAMERA_B := Vector3(900.0, 150.0, 250.0)
## 40 km west of the box: every listed level-4 tile stays whole, so the selection is the four level-4 tiles.
const _CAMERA_FAR := Vector3(-40000.0, 150.0, 0.0)

## terrain.py constants.
const _GRADIENT := -0.0009
const _HILL_CENTRE := Vector2(-746750.0, -1024750.0)
const _HILL_AMPLITUDE := 4.0
const _HILL_SIGMA := 140.0
const _RIDGE_START := Vector2(-747300.0, -1025900.0)
const _RIDGE_END := Vector2(-746600.0, -1025300.0)
const _RIDGE_AMPLITUDE := 2.5
const _RIDGE_WIDTH := 35.0

var _field: TerrainField
var _camera: Camera3D
var _messages: Array[String] = []  # the status-bar messages since _listen_for_status
var _listing_warnings: Array = []  # the warnings the fake imagery listing answers with
var _listing_mode := "ok"          # the fake imagery listing: "ok", "busy" or "mismatch"
## T-135 Part C (F52): the imagery replies a test holds. `released` is set in `after_test` too, so no held reply outlives
## its test.
var _held: Dictionary = {"released": true}
## F52 test gate: while `closed`, the override holds `terrain.tiles`. `after_test` opens it, so a failing test cannot hang.
var _terrain_gate: Dictionary = {"closed": false}


func before() -> void:
	_write_file(_FILE_A, _surface_landxml(_BOX, 0.0))
	_write_file(_FILE_B, _surface_landxml(_PATCH, 3.0))
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
	Backend.shutdown()
	for path in [_FILE_A, _FILE_B, _FILE_IMAGE, _FILE_IMAGE_B]:
		DirAccess.remove_absolute(ProjectSettings.globalize_path(path))
		DirAccess.remove_absolute(ProjectSettings.globalize_path(path.get_basename() + ".pgw"))


func after_test() -> void:
	_held["released"] = true
	_terrain_gate["closed"] = false
	Session.set_terrain_request_override(Callable())
	if EventBus.status_message.is_connected(_record_status):
		EventBus.status_message.disconnect(_record_status)
	_messages.clear()
	_listing_warnings = []
	_listing_mode = "ok"
	var none: Array[Dictionary] = []
	Session.set_data_sources(none)
	Session.set_source_boundaries(false)
	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, true)
	Session.layers().set_visible(TerrainField.LAYER_ID, true)
	if _field != null:
		_field.queue_free()
		_field = null
	if _camera != null:
		_camera.queue_free()
		_camera = null
	# Last: showing the Imagery layer makes a field that still lives publish the warnings of its last listing again.
	Session.set_imagery_warnings(PackedStringArray())


func test_field_shows_the_expected_tiles_at_a_fixed_camera() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)

	var settled := await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)
	assert_bool(settled).append_failure_message("the selected tiles never all attached").is_true()
	if not settled:
		return

	var expected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	assert_int(_field.attached_count()).append_failure_message(
		"%d tiles attached for %d selected" % [_field.attached_count(), expected.size()]
	).is_equal(expected.size())

	var per_level := [0, 0, 0, 0, 0]
	var meshed := 0
	for key in expected:
		per_level[key.x] += 1
		var tile := _field.tile_at(key)
		assert_object(tile).append_failure_message("tile %s is not attached" % key).is_not_null()
		if tile == null:
			continue
		var info: Dictionary = _field.lists()[key.x][Vector2i(key.y, key.z)]
		assert_vector(tile.position).append_failure_message(
			"tile %s is not placed at its tile_origin" % key
		).is_equal(info["tile_origin"])
		if tile.has_mesh():
			meshed += 1

		var envelope := await Session.fetch_terrain_tile(key.x, key.y, key.z, _field.revision())
		assert_str(envelope.type).append_failure_message("terrain.tile %s failed: %s" % [key, envelope.error]).is_equal(
			"res"
		)
		if envelope.type != "res":
			continue
		var backend_heights: PackedFloat32Array = envelope.blobs["heights"]
		assert_bool(_same_heights(tile.heights, backend_heights)).append_failure_message(
			"tile %s heights differ from terrain.tile" % key
		).is_true()

	print("TERRAIN_FIELD tiles per level L0..L4: %s, meshed: %d, build stats: %s" % [
		str(per_level), meshed, str(_field.build_stats())
	])
	assert_int(meshed).is_greater(0)


func test_stale_revision_after_an_import_keeps_tiles_visible() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	var revision_before := _field.revision()

	var imported := await Backend.request(
		"import.run", {"path": _global(_FILE_B), "kind": "terrain", "settings": _settings(), "name": "Patch"}
	)
	assert_str(imported.type).append_failure_message("second import failed: %s" % imported.error).is_equal("res")

	# The camera moves east, so the field asks for tiles it has not got. Those requests carry the old revision,
	# which the backend answers with E_NOT_FOUND; the field then lists again.
	_camera.position = _CAMERA_B

	var min_attached := _field.attached_count()
	var started := Time.get_ticks_msec()
	var relisted := false
	while Time.get_ticks_msec() - started < _FIELD_TIMEOUT_SEC * 1000.0:
		await get_tree().process_frame
		min_attached = mini(min_attached, _field.attached_count())
		if _field.revision() != revision_before and _selection_attached():
			relisted = true
			break

	assert_bool(relisted).append_failure_message(
		"the field never listed the new revision (still %d)" % revision_before
	).is_true()
	assert_int(min_attached).append_failure_message("no tile was attached while the field re-listed").is_greater(0)


func test_busy_is_retried_with_backoff() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()

	# The first three terrain.tile requests answer E_BUSY; the fourth goes to the backend.
	var calls: Array = []
	var seam := func(method: String, _params: Dictionary) -> IpcEnvelope:
		if method != "terrain.tile":
			return null
		calls.append(Time.get_ticks_msec())
		if calls.size() > 3:
			return null
		var busy := IpcEnvelope.new()
		busy.type = "err"
		busy.method = method
		busy.error = {"code": "E_BUSY", "message": "busy in test"}
		busy.blobs = {}
		return busy
	Session.set_terrain_request_override(seam)

	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(func() -> bool: return calls.size() >= 4, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()

	var expected_ms := [100, 200, 400]
	var gaps := []
	for k in 3:
		gaps.append(calls[k + 1] - calls[k])
	print("TERRAIN_BUSY retry gaps ms: %s (expected about %s)" % [str(gaps), str(expected_ms)])
	for k in 3:
		var gap: int = calls[k + 1] - calls[k]
		assert_int(gap).append_failure_message(
			"retry %d came after %d ms, expected about %d ms" % [k + 1, gap, expected_ms[k]]
		).is_greater_equal(int(expected_ms[k] * 0.9))
		assert_int(gap).is_less(expected_ms[k] + 250)


func test_busy_backoff_doubles_and_caps() -> void:
	assert_float(TerrainField.busy_backoff_sec(0)).is_equal_approx(0.1, 1e-9)
	assert_float(TerrainField.busy_backoff_sec(1)).is_equal_approx(0.2, 1e-9)
	assert_float(TerrainField.busy_backoff_sec(2)).is_equal_approx(0.4, 1e-9)
	assert_float(TerrainField.busy_backoff_sec(4)).is_equal_approx(1.6, 1e-9)
	assert_float(TerrainField.busy_backoff_sec(5)).is_equal_approx(2.0, 1e-9)
	assert_float(TerrainField.busy_backoff_sec(40)).is_equal_approx(2.0, 1e-9)


func test_replaced_tiles_are_hidden_once_their_replacements_are_attached() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_FAR)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_A
	var settled := await _wait_until(_visible_matches_selection, _FIELD_TIMEOUT_SEC)
	var expected := _sorted_keys(TerrainLod.select_tiles(_camera.global_position, _field.lists()))
	var visible := _sorted_keys(_visible_keys())
	assert_bool(settled).append_failure_message(
		"visible tiles never matched the selection: %d visible, %d selected" % [visible.size(), expected.size()]
	).is_true()
	assert_array(visible).is_equal(expected)


func test_no_hole_appears_while_refining() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_FAR)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_A
	var holes := 0
	var frames := 0
	var started := Time.get_ticks_msec()
	while not _visible_matches_selection() and Time.get_ticks_msec() - started < _FIELD_TIMEOUT_SEC * 1000.0:
		await get_tree().process_frame
		frames += 1
		holes += _holes_now()
	assert_int(frames).is_greater(0)
	assert_int(holes).append_failure_message(
		"%d selected tiles had no visible attached ancestor while refining" % holes
	).is_equal(0)


func test_no_hole_appears_while_coarsening() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_visible_matches_selection, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_FAR
	var holes := 0
	var frames := 0
	while not _visible_matches_selection() and frames < 300:
		await get_tree().process_frame
		frames += 1
		holes += _holes_either_way()
	assert_bool(_visible_matches_selection()).append_failure_message(
		"the shown tiles never matched the selection in %d frames" % frames
	).is_true()
	assert_int(holes).append_failure_message("%d holes while coarsening" % holes).is_equal(0)


func test_a_tight_cache_does_not_open_holes_while_refining() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_FAR)
	_field.max_tiles = 8
	assert_bool(await _wait_until(_visible_matches_selection, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_A
	var holes := 0
	var started := Time.get_ticks_msec()
	while not _visible_matches_selection() and Time.get_ticks_msec() - started < 30000:
		await get_tree().process_frame
		holes += _holes_either_way()
	assert_bool(_visible_matches_selection()).append_failure_message("the shown tiles never matched the selection").is_true()
	assert_int(holes).append_failure_message("%d holes with a cache of 8" % holes).is_equal(0)


func test_returning_to_cached_tiles_swaps_the_shown_tiles() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_FAR)
	assert_bool(await _wait_until(_visible_matches_selection, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_A
	assert_bool(await _wait_until(_visible_matches_selection, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_FAR
	var back := await _wait_until(_visible_matches_selection, 5.0)
	assert_bool(back).append_failure_message("the shown tiles did not match the selection within 5 s").is_true()


func test_a_project_without_terrain_clears_the_field() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	var fresh := await Backend.request("project.new", {"crs": "EPSG:5514"})
	assert_str(fresh.type).is_equal("res")
	_field.relist()
	var cleared := await _wait_until(func() -> bool: return _field.attached_count() == 0, 15.0)
	assert_bool(cleared).append_failure_message(
		"%d tiles of the previous project remain" % _field.attached_count()
	).is_true()


func test_a_listing_that_times_out_is_retried() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	var calls := []
	Session.set_terrain_request_override(_timeout_once("terrain.tiles", calls))
	_field = _start_field(_CAMERA_A)
	var ok := await _wait_until(_selection_attached, 20.0)
	assert_bool(ok).append_failure_message(
		"the field never listed after one E_TIMEOUT (revision %d)" % _field.revision()
	).is_true()
	assert_int(calls.size()).append_failure_message("the listing was not retried").is_greater(1)


func test_a_tile_that_times_out_is_retried() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	var calls := []
	Session.set_terrain_request_override(_timeout_once("terrain.tile", calls))
	_field = _start_field(_CAMERA_A)
	var ok := await _wait_until(_selection_attached, 20.0)
	assert_bool(ok).append_failure_message("a tile that timed out once was never fetched again").is_true()
	assert_int(calls.size()).append_failure_message("the tile was not retried").is_greater(1)


func test_ready_jobs_are_attached_two_per_frame() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(func() -> bool: return _field.revision() >= 0, _FIELD_TIMEOUT_SEC)).is_true()
	for i in 3:
		await get_tree().process_frame
	# Stop attaching for 2 s: the paging and the workers keep going, so finished tiles pile up.
	_field.set_process(false)
	await get_tree().create_timer(2.0).timeout
	var before := _field.attached_count()
	_field.set_process(true)
	var worst := 0
	var previous := before
	for i in 40:
		await get_tree().process_frame
		var now := _field.attached_count()
		worst = maxi(worst, now - previous)
		previous = now
	assert_int(previous).append_failure_message("only %d tiles attached in all" % previous).is_greater(4)
	assert_int(worst).append_failure_message("%d tiles attached in one frame" % worst).is_less_equal(2)


func test_stale_revision_replaces_old_tiles() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	var revision_before := _field.revision()
	var imported := await Backend.request(
		"import.run", {"path": _global(_FILE_B), "kind": "terrain", "settings": _settings(), "name": "Patch"}
	)
	assert_str(imported.type).append_failure_message("second import failed: %s" % imported.error).is_equal("res")
	_camera.position = _CAMERA_A + Vector3(300.0, 0.0, 0.0)
	var replaced := func() -> bool:
		if _field.revision() == revision_before or _field.revision() < 0:
			return false
		for key in TerrainLod.select_tiles(_camera.global_position, _field.lists()):
			var tile := _field.tile_at(key)
			if tile == null or tile.revision != _field.revision():
				return false
		return true
	var ok := await _wait_until(replaced, _FIELD_TIMEOUT_SEC)
	assert_bool(ok).append_failure_message("the selected tiles never all reached the new revision").is_true()


func test_cache_limit_frees_unselected_tiles_only() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_B)
	_field.max_tiles = 30
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	_camera.position = _CAMERA_A
	assert_bool(await _wait_until(
		func() -> bool: return _field.pending_count() == 0 and _selection_attached(), _FIELD_TIMEOUT_SEC
	)).is_true()
	for i in 4:
		await get_tree().process_frame
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	assert_int(_field.attached_count()).append_failure_message(
		"%d tiles attached with a limit of 30 and %d selected" % [_field.attached_count(), selected.size()]
	).is_less_equal(maxi(30, selected.size()))
	for key in selected:
		assert_object(_field.tile_at(key)).append_failure_message("selected tile %s was freed" % key).is_not_null()


## A seam that answers the first call for `method` with E_TIMEOUT and passes every later request to the backend.
func _timeout_once(method: String, calls: Array) -> Callable:
	return func(called: String, _params: Dictionary) -> IpcEnvelope:
		if called != method:
			return null
		calls.append(called)
		if calls.size() > 1:
			return null
		var late := IpcEnvelope.new()
		late.type = "err"
		late.method = called
		late.error = {"code": "E_TIMEOUT", "message": "no reply within 30 s"}
		late.blobs = {}
		return late


## The attached tiles that are shown.
func _visible_keys() -> Array:
	var keys := []
	for key in _field.attached_keys():
		if _field.tile_at(key).visible:
			keys.append(key)
	return keys


## True once the shown tiles are exactly the tiles the camera selects.
func _visible_matches_selection() -> bool:
	if _field == null or _camera == null or _field.revision() < 0 or not _selection_attached():
		return false
	var expected := _sorted_keys(TerrainLod.select_tiles(_camera.global_position, _field.lists()))
	return _sorted_keys(_visible_keys()) == expected


## Selected tiles without an attached tile whose nearest attached ancestor is not shown.
func _holes_now() -> int:
	var holes := 0
	for key: Vector3i in TerrainLod.select_tiles(_camera.global_position, _field.lists()):
		if _field.tile_at(key) != null:
			continue
		var covered := false
		for level in range(key.x + 1, TerrainLod.MAX_LEVEL + 1):
			var ancestor := Vector3i(level, key.y >> (level - key.x), key.z >> (level - key.x))
			var tile := _field.tile_at(ancestor)
			if tile != null:
				covered = tile.visible
				break
		if not covered:
			holes += 1
	return holes


## Selected tiles with no tile of their own and neither a shown ancestor nor a shown descendant.
func _holes_either_way() -> int:
	var holes := 0
	var shown := _visible_keys()
	for key: Vector3i in TerrainLod.select_tiles(_camera.global_position, _field.lists()):
		if _field.tile_at(key) != null:
			continue
		var covered := false
		for other: Vector3i in shown:
			if other.x > key.x and (key.y >> (other.x - key.x)) == other.y and (key.z >> (other.x - key.x)) == other.z:
				covered = true
				break
			if other.x < key.x and (other.y >> (key.x - other.x)) == key.y and (other.z >> (key.x - other.x)) == key.z:
				covered = true
				break
		if not covered:
			holes += 1
	return holes


## Keys as sorted strings, so that two lists compare by content.
static func _sorted_keys(keys: Array) -> Array:
	var names := []
	for key in keys:
		names.append(str(key))
	names.sort()
	return names


func _require_ready() -> bool:
	if Backend.state() != Backend.State.READY:
		fail("backend never reached READY")
		return false
	return true


func _await_state(target: Backend.State, timeout_sec: float) -> bool:
	var elapsed := 0.0
	while Backend.state() != target and elapsed < timeout_sec:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	return Backend.state() == target


## Coroutine: `project.new` with the CRS and base point of the synthetic terrain, then one terrain import.
func _open_project_with_terrain(path: String) -> bool:
	var project := await Backend.request("project.new", {
		"crs": "EPSG:5514",
		"base_point": {"easting": _BASE_E, "northing": _BASE_N, "height": _BASE_H},
	})
	if project.type != "res":
		fail("project.new failed: %s" % project.error)
		return false
	var imported := await Backend.request(
		"import.run", {"path": _global(path), "kind": "terrain", "settings": _settings(), "name": "Synthetic"}
	)
	if imported.type != "res":
		fail("import.run failed: %s" % imported.error)
		return false
	return true


func _start_field(camera_position: Vector3) -> TerrainField:
	_camera = Camera3D.new()
	_camera.position = camera_position
	get_tree().root.add_child(_camera)
	var field := TerrainField.new()
	get_tree().root.add_child(field)
	field.set_camera(_camera)
	return field


## True once the field's listing exists and every tile the active camera selects is attached.
func _selection_attached() -> bool:
	if _field == null or _camera == null or _field.revision() < 0:
		return false
	var expected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	if expected.is_empty():
		return false
	for key in expected:
		if _field.tile_at(key) == null:
			return false
	return true


func _wait_until(predicate: Callable, timeout_sec: float) -> bool:
	var started := Time.get_ticks_msec()
	while not predicate.call():
		if Time.get_ticks_msec() - started > timeout_sec * 1000.0:
			return false
		await get_tree().process_frame
	return true


static func _settings() -> Dictionary:
	return {"horizontal_crs": "EPSG:5514"}


## NaN-aware equality: a heights blob holds NaN where there is no data, and NaN != NaN.
static func _same_heights(a: PackedFloat32Array, b: PackedFloat32Array) -> bool:
	if a.size() != b.size():
		return false
	for i in a.size():
		if a[i] != b[i] and not (is_nan(a[i]) and is_nan(b[i])):
			return false
	return true


static func _global(path: String) -> String:
	return ProjectSettings.globalize_path(path)


static func _write_file(path: String, text: String) -> void:
	var file := FileAccess.open(path, FileAccess.WRITE)
	file.store_string(text)
	file.close()


## The synthetic surface as a LandXML TIN over `bbox` (min_e, min_n, max_e, max_n) on a regular grid, with
## every height lifted by `lift`. Points are "north east elevation" in negative Krovak values, which the
## LandXML reader takes as they are.
static func _surface_landxml(bbox: Array, lift: float) -> String:
	var min_e: float = bbox[0]
	var min_n: float = bbox[1]
	var nx := int(round((bbox[2] - min_e) / _SPACING_M)) + 1
	var ny := int(round((bbox[3] - min_n) / _SPACING_M)) + 1
	var lines := PackedStringArray()
	lines.append('<?xml version="1.0" encoding="UTF-8"?>')
	lines.append('<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">')
	lines.append('<Units><Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/></Units>')
	lines.append('<CoordinateSystem name="S-JTSK / Krovak East North" epsgCode="5514"/>')
	lines.append('<Surfaces><Surface name="synthetic"><Definition surfType="TIN"><Pnts>')
	for j in ny:
		for i in nx:
			var e := min_e + i * _SPACING_M
			var n := min_n + j * _SPACING_M
			lines.append('<P id="%d">%.4f %.4f %.4f</P>' % [j * nx + i + 1, n, e, _surface(e, n) + lift])
	lines.append("</Pnts><Faces>")
	for j in ny - 1:
		for i in nx - 1:
			var a := j * nx + i + 1
			var d := a + nx
			lines.append("<F>%d %d %d</F>" % [a, a + 1, d + 1])
			lines.append("<F>%d %d %d</F>" % [a, d + 1, d])
	lines.append("</Faces></Definition></Surface></Surfaces></LandXML>")
	return "\n".join(lines)


static func _surface(e: float, n: float) -> float:
	var plane := _BASE_H + _GRADIENT * (e - _BASE_E) + _GRADIENT * (n - _BASE_N)
	var hill_d2 := (e - _HILL_CENTRE.x) * (e - _HILL_CENTRE.x) + (n - _HILL_CENTRE.y) * (n - _HILL_CENTRE.y)
	var hill := _HILL_AMPLITUDE * exp(-hill_d2 / (2.0 * _HILL_SIGMA * _HILL_SIGMA))
	var dx := _RIDGE_END.x - _RIDGE_START.x
	var dy := _RIDGE_END.y - _RIDGE_START.y
	var t := clampf(((e - _RIDGE_START.x) * dx + (n - _RIDGE_START.y) * dy) / (dx * dx + dy * dy), 0.0, 1.0)
	var px := e - (_RIDGE_START.x + t * dx)
	var py := n - (_RIDGE_START.y + t * dy)
	var ridge := _RIDGE_AMPLITUDE * exp(-(px * px + py * py) / (2.0 * _RIDGE_WIDTH * _RIDGE_WIDTH))
	return plane + hill + ridge


## Part B: the imagery. Each test imports the terrain of `_FILE_A` and a synthetic orthophoto over the same box.


func test_imagery_replaces_the_placeholder() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_field = _start_field(_CAMERA_A)
	var draped := await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)
	assert_bool(draped).append_failure_message("the selected tiles were never draped").is_true()
	if not draped:
		return
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	var draped_count := 0
	for key in selected:
		var tile := _field.tile_at(key)
		if tile.shown_material() is TerrainMaterial:
			draped_count += 1
	assert_int(draped_count).append_failure_message(
		"%d of %d selected tiles show the placeholder" % [selected.size() - draped_count, selected.size()]
	).is_equal(selected.size())
	print("TERRAIN_IMAGERY draped %d of %d selected tiles, imagery revision %d" % [
		draped_count, selected.size(), _field.imagery_revision()
	])


func test_hiding_imagery_shows_the_neutral_material() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())

	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, false)
	await get_tree().process_frame
	var neutral := TerrainTile.neutral_material()
	for key in selected:
		var tile := _field.tile_at(key)
		assert_bool(tile.shown_material() == neutral).append_failure_message(
			"tile %s still shows its imagery with the layer hidden" % key
		).is_true()

	# With the boundaries on and the imagery hidden, a tile shows the flat boundary material, not the placeholder.
	Session.set_source_boundaries(true)
	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, false)
	await get_tree().process_frame
	for key in selected:
		var boundary_shown := _field.tile_at(key).shown_material()
		assert_bool(boundary_shown is TerrainMaterial and (boundary_shown as TerrainMaterial).boundaries_on()).append_failure_message(
			"tile %s shows neither imagery nor its boundaries with the layer hidden" % key
		).is_true()
	Session.set_source_boundaries(false)

	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, true)
	await get_tree().process_frame
	for key in selected:
		assert_bool(_field.tile_at(key).shown_material() is TerrainMaterial).append_failure_message(
			"tile %s did not get its imagery back" % key
		).is_true()


func test_attribution_lists_exactly_the_sources_in_view() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()

	# The sources of the tiles in view: each shown tile's terrain sources, and its imagery sources except relief.
	var expected := {}
	for key in _field.attached_keys():
		var tile := _field.tile_at(key)
		if not tile.visible:
			continue
		for source_id in tile.terrain_source_ids():
			expected[String(source_id)] = true
		if tile.has_imagery():
			for source_id in tile.imagery_source_ids:
				if String(source_id) != "relief":
					expected[String(source_id)] = true
	var expected_ids := _sorted_keys(expected.keys())
	assert_int(expected_ids.size()).is_greater(0)
	var published := await _wait_until(func() -> bool: return _entry_ids() == expected_ids, 5.0)
	assert_bool(published).append_failure_message(
		"the data sources in view are %s, expected %s" % [str(_entry_ids()), str(expected_ids)]
	).is_true()
	for entry in Session.data_sources():
		var source_id := String(entry["source_id"])
		assert_str(String(entry["attribution"])).append_failure_message(
			"source %s carries another attribution than imagery.tiles gave" % source_id
		).is_equal(String(_field.attributions().get(source_id, "")))


func test_an_imagery_only_import_does_not_rebuild_meshes() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	var meshes := {}
	for key in selected:
		meshes[key] = _field.tile_at(key).mesh_instance().mesh
	var terrain_revision := _field.revision()
	var imagery_before := _field.imagery_revision()
	var built := int(_field.build_stats()["count"])

	assert_bool(await _import_imagery(_FILE_IMAGE_B, Color(0.6, 0.5, 0.3), "Second image")).is_true()
	_field.relist()
	var redraped := await _wait_until(
		func() -> bool: return _field.imagery_revision() != imagery_before and _imagery_settled(), _FIELD_TIMEOUT_SEC
	)
	assert_bool(redraped).append_failure_message("the imagery never moved to its new revision").is_true()
	assert_int(_field.revision()).append_failure_message("an imagery-only import changed the terrain revision").is_equal(
		terrain_revision
	)
	assert_int(int(_field.build_stats()["count"])).append_failure_message(
		"%d terrain meshes were rebuilt by an imagery-only import" % (int(_field.build_stats()["count"]) - built)
	).is_equal(built)
	for key in meshes.keys():
		assert_bool(_field.tile_at(key).mesh_instance().mesh == meshes[key]).append_failure_message(
			"tile %s got a new mesh" % key
		).is_true()


## True once the field is settled and every selected tile is draped with the current imagery revision.
func _imagery_settled() -> bool:
	if _field == null or _camera == null or not _field.settled():
		return false
	for key in TerrainLod.select_tiles(_camera.global_position, _field.lists()):
		var tile := _field.tile_at(key)
		if tile == null or not tile.has_imagery() or tile.imagery_revision != _field.imagery_revision():
			return false
	return true


## The source ids the Session holds for the data sources in view, sorted.
func _entry_ids() -> Array:
	var ids := []
	for entry in Session.data_sources():
		ids.append(String(entry["source_id"]))
	ids.sort()
	return ids


## Writes a synthetic orthophoto over the box, 4 m a pixel, with a white "F" near its north-west corner, and its world
## file. The world file gives the box in EPSG:5514 (negative magnitudes) and carries no CRS, so the import names it.
func _import_imagery(path: String, tint: Color, label: String) -> bool:
	_write_imagery(path, tint)
	var imported := await Backend.request(
		"import.run", {"path": _global(path), "kind": "imagery", "settings": _settings(), "name": label}
	)
	if imported.type != "res":
		fail("imagery import failed: %s" % str(imported.error))
		return false
	return true


static func _write_imagery(path: String, tint: Color) -> void:
	var width := int((_BOX[2] - _BOX[0]) / _IMAGE_PIXEL_M)
	var height := int((_BOX[3] - _BOX[1]) / _IMAGE_PIXEL_M)
	var image := Image.create_empty(width, height, false, Image.FORMAT_RGB8)
	image.fill(tint)
	image.fill_rect(Rect2i(20, 20, 12, 60), Color(1.0, 1.0, 1.0))
	image.fill_rect(Rect2i(20, 20, 40, 10), Color(1.0, 1.0, 1.0))
	image.save_png(ProjectSettings.globalize_path(path))
	var centre_e: float = _BOX[0] + _IMAGE_PIXEL_M / 2.0
	var centre_n: float = _BOX[3] - _IMAGE_PIXEL_M / 2.0
	var world := "%s\n0\n0\n-%s\n%s\n%s\n" % [str(_IMAGE_PIXEL_M), str(_IMAGE_PIXEL_M), str(centre_e), str(centre_n)]
	_write_file(path.get_basename() + ".pgw", world)


## Part B, review round: the boundaries, the attribution and the re-listing of imagery, each checked live.


func test_boundaries_show_without_imagery() -> void:
	if not _require_ready():
		return
	# No imagery is imported, so the backend shades every tile with relief: each one is draped, and hiding the imagery
	# layer is what leaves a tile with no imagery shown.
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	Session.set_source_boundaries(true)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, false)
	await get_tree().process_frame
	var visible_count := 0
	for key in _field.attached_keys():
		var tile := _field.tile_at(key)
		if not tile.visible:
			continue
		visible_count += 1
		var shown := tile.shown_material()
		assert_bool(shown is TerrainMaterial and (shown as TerrainMaterial).boundaries_on()).append_failure_message(
			"tile %s has no boundary material with the imagery hidden" % key
		).is_true()
	assert_int(visible_count).is_greater(0)
	Session.set_source_boundaries(false)
	await get_tree().process_frame
	for key in _field.attached_keys():
		var tile := _field.tile_at(key)
		if tile.visible:
			assert_bool(tile.shown_material() == TerrainTile.neutral_material()).append_failure_message(
				"tile %s keeps a boundary material with the toggle off" % key
			).is_true()


func test_boundary_toggle_reaches_every_tile() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	# On before the field exists: every tile it installs must already carry the overlay.
	Session.set_source_boundaries(true)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(_all_visible_overlays(true)).append_failure_message("tiles installed with the toggle on lack the overlay").is_true()
	Session.set_source_boundaries(false)
	await get_tree().process_frame
	assert_bool(_all_visible_overlays(false)).append_failure_message("the toggle off did not reach every tile").is_true()
	Session.set_source_boundaries(true)
	await get_tree().process_frame
	assert_bool(_all_visible_overlays(true)).append_failure_message("the toggle on did not reach every tile").is_true()


func test_attribution_names_the_sources_and_skips_relief() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	var listed := await Backend.request("source.list", {})
	assert_str(listed.type).append_failure_message("source.list failed: %s" % str(listed.error)).is_equal("res")
	var known := {}
	for source in listed.result.get("sources", []):
		known[String(source["id"])] = source

	# Phase 1, terrain and no imagery source: every tile is shaded with relief, and relief must be in the tiles' sources
	# yet credited to nothing. From afar the field selects level-4 tiles, which hold terrain with no imagery over it.
	_field = _start_field(_CAMERA_FAR)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	var relief_in_view := false
	for key in _field.attached_keys():
		var tile := _field.tile_at(key)
		if tile.visible and tile.imagery_source_ids.has("relief"):
			relief_in_view = true
	assert_bool(relief_in_view).append_failure_message("no tile in view is shaded with relief").is_true()
	assert_bool(await _wait_until(func() -> bool: return not Session.data_sources().is_empty(), 5.0)).is_true()
	_assert_named_and_no_relief(known, "terrain only")

	# Phase 2, terrain with the orthophoto over all of it: the credits name the terrain and the imagery, by source.list.
	_field.queue_free()
	_field = null
	_camera.queue_free()
	_camera = null
	Session.set_data_sources([] as Array[Dictionary])
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var listed_again := await Backend.request("source.list", {})
	known = {}
	for source in listed_again.result.get("sources", []):
		known[String(source["id"])] = source
	_field = _start_field(_CAMERA_FAR)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(func() -> bool: return not Session.data_sources().is_empty(), 5.0)).is_true()
	_assert_named_and_no_relief(known, "terrain with imagery")


## Every credited source is a real source of source.list, named and kinded as it is there, and relief is never credited.
func _assert_named_and_no_relief(known: Dictionary, phase: String) -> void:
	assert_bool(Session.data_sources().size() > 0).append_failure_message("%s: nothing is credited" % phase).is_true()
	for entry in Session.data_sources():
		var source_id := String(entry["source_id"])
		assert_bool(source_id != "relief").append_failure_message("%s: relief was credited as a source" % phase).is_true()
		assert_bool(known.has(source_id)).append_failure_message("%s: source %s is not in source.list" % [phase, source_id]).is_true()
		if known.has(source_id):
			assert_str(String(entry["name"])).append_failure_message("%s: wrong name" % phase).is_equal(String(known[source_id]["name"]))
			assert_str(String(entry["kind"])).append_failure_message("%s: wrong kind" % phase).is_equal(String(known[source_id]["kind"]))


func test_imagery_not_found_relists_imagery_only() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	# The first imagery.tile answers E_NOT_FOUND, as a stale imagery revision does; the rest reach the backend.
	var calls: Array = []
	var answered := [false]
	var seam := func(method: String, _params: Dictionary) -> IpcEnvelope:
		calls.append(method)
		if method == "imagery.tile" and not answered[0]:
			answered[0] = true
			var stale := IpcEnvelope.new()
			stale.type = "err"
			stale.method = method
			stale.error = {"code": "E_NOT_FOUND", "message": "imagery revision is out of date (test)"}
			stale.blobs = {}
			return stale
		return null
	Session.set_terrain_request_override(seam)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(answered[0]).is_true()
	assert_int(calls.count("terrain.tiles")).append_failure_message(
		"the terrain was listed again after an imagery E_NOT_FOUND (%d listings)" % calls.count("terrain.tiles")
	).is_equal(TerrainLod.MAX_LEVEL + 1)
	assert_int(calls.count("imagery.tiles")).append_failure_message(
		"the imagery was not listed again after E_NOT_FOUND"
	).is_greater(TerrainLod.MAX_LEVEL + 1)


func test_hidden_terrain_credits_nothing() -> void:
	if not _require_ready():
		return
	# The terrain layer is defined here, as main.gd defines it in the app.
	Session.layers().define(TerrainField.LAYER_ID, "Terrain")
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(func() -> bool: return not Session.data_sources().is_empty(), 5.0)).is_true()
	Session.layers().set_visible(TerrainField.LAYER_ID, false)
	var cleared := await _wait_until(func() -> bool: return Session.data_sources().is_empty(), 5.0)
	assert_bool(cleared).append_failure_message(
		"%d sources are still credited with the terrain hidden" % Session.data_sources().size()
	).is_true()


## The Session's data source entry for `source_id`, or an empty dictionary.
func _entry_of(source_id: String) -> Dictionary:
	for entry in Session.data_sources():
		if String(entry["source_id"]) == source_id:
			return entry
	return {}


## T-135 Part B, review round: terrain sources come in the listing's precedence, and an id the listing does not name last.
func test_terrain_sources_are_ordered_by_precedence() -> void:
	var precedence := PackedStringArray(["fine", "coarse"])
	assert_array(TerrainField.terrain_order(["coarse", "other", "fine"], precedence)).is_equal(["fine", "coarse", "other"])
	assert_array(TerrainField.terrain_order(["zeta", "alpha"], PackedStringArray())).is_equal(["alpha", "zeta"])
	assert_array(TerrainField.terrain_order([], precedence)).is_empty()


## T-135 Part B, review round: the imagery credit comes from the texts `imagery.tiles` gives. The real envelopes for levels 0
## to 4 are patched with a known attribution and notice for the imagery source and returned by the Session seam, so the
## field must copy both and credit the imagery source for the text to reach Session.data_sources().
func test_attribution_pipeline_credits_imagery_from_its_texts() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var listed := await Backend.request("source.list", {})
	var imagery_id := ""
	for source in listed.result.get("sources", []):
		if String(source["kind"]) == "imagery":
			imagery_id = String(source["id"])
	assert_str(imagery_id).is_not_empty()

	var text := "Patched survey text (test)"
	var notice := "Patched licence notice (test)"
	var patched := {}
	for level in range(TerrainLod.MAX_LEVEL + 1):
		var fetched := await Backend.request("imagery.tiles", {"level": level})
		assert_str(fetched.type).append_failure_message("imagery.tiles %d failed: %s" % [level, str(fetched.error)]).is_equal("res")
		var texts: Dictionary = fetched.result.get("attributions", {})
		texts[imagery_id] = text
		fetched.result["attributions"] = texts
		var notices: Dictionary = fetched.result.get("notices", {})
		notices[imagery_id] = PackedStringArray([notice])
		fetched.result["notices"] = notices
		patched[level] = fetched
	var seam := func(method: String, params: Dictionary) -> IpcEnvelope:
		if method == "imagery.tiles":
			return patched[int(params["level"])]
		return null
	Session.set_terrain_request_override(seam)

	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	var credited := await _wait_until(func() -> bool: return not _entry_of(imagery_id).is_empty(), 5.0)
	assert_bool(credited).append_failure_message("the imagery source %s is not credited" % imagery_id).is_true()
	if not credited:
		return
	var entry := _entry_of(imagery_id)
	assert_str(String(entry["kind"])).is_equal("imagery")
	assert_str(String(entry["attribution"])).append_failure_message("the field did not copy the attribution").is_equal(text)
	assert_bool((entry["notices"] as PackedStringArray).has(notice)).append_failure_message(
		"the field did not copy the notice"
	).is_true()
	# Grouped by kind: no terrain source is listed after an imagery one.
	var seen_imagery := false
	for data_entry in Session.data_sources():
		if String(data_entry["kind"]) == "imagery":
			seen_imagery = true
		else:
			assert_bool(seen_imagery).append_failure_message(
				"terrain source %s is listed after an imagery source" % String(data_entry["source_id"])
			).is_false()


## T-135 Part B, review round: an imagery job that finishes after the terrain revision has moved is dropped, not draped, and
## its tile asks for imagery again. At the first imagery fetch the override moves the field's terrain revision, as a re-listing
## would, so the job in flight is stale when it attaches; a dropped job must leave the tile free to fetch again.
func test_stale_imagery_job_is_dropped() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var calls := []
	var moved := [false]
	# The terrain lanes and the imagery lane run apart (F52), so a relist could restore the revision before the stale
	# job is attached, and the job would then be current again. The gate holds terrain listings until that first job
	# has been attached or dropped, so the revision stays stale for it.
	var replied := [false]
	_terrain_gate["closed"] = false
	var seam := func(method: String, params: Dictionary) -> IpcEnvelope:
		if method == "terrain.tiles" and _terrain_gate["closed"]:
			while _terrain_gate["closed"]:
				await get_tree().process_frame
		if method != "imagery.tile":
			return null
		calls.append(method)
		if moved[0]:
			return null
		moved[0] = true
		_terrain_gate["closed"] = true
		_field._revision += 1
		var envelope: IpcEnvelope = await Backend.request(method, params)
		replied[0] = true
		return envelope
	Session.set_terrain_request_override(seam)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(func() -> bool: return replied[0], _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(
		func() -> bool: return _field._jobs.is_empty() and _field._ready_queue.is_empty(), _FIELD_TIMEOUT_SEC
	)).is_true()
	_terrain_gate["closed"] = false
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(moved[0]).is_true()
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	assert_int(calls.size()).append_failure_message(
		"%d imagery fetches for %d tiles: the stale job's tile did not fetch again" % [calls.size(), selected.size()]
	).is_greater(selected.size())
	for key in selected:
		var tile := _field.tile_at(key)
		assert_bool(tile.has_imagery() and tile.imagery_revision == _field.imagery_revision()).append_failure_message(
			"tile %s is not draped with the current imagery" % key
		).is_true()


## T-135 Part B, review round: an imagery tile that does not decode gives one status message per imagery revision, not one per tile.
func test_decode_failure_message_once_per_revision() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	# Every imagery tile answers with bytes that are not a JPEG.
	var seam := func(method: String, _params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		var bad := IpcEnvelope.new()
		bad.type = "res"
		bad.method = method
		bad.result = {"has_alpha": false, "source_ids": []}
		bad.blobs = {"image": PackedByteArray([1, 2, 3, 4])}
		return bad
	var messages := []
	var listener := func(text: String) -> void:
		if text == "Some imagery could not be shown.":
			messages.append(text)
	EventBus.status_message.connect(listener)
	Session.set_terrain_request_override(seam)
	_field = _start_field(_CAMERA_A)
	# The one warning of the revision is expected here; a warning per tile would fail the test as an unexpected error.
	var reported := []
	await assert_error(func() -> void:
		reported.append(await _wait_until(func() -> bool: return not messages.is_empty() and _field.settled(), _FIELD_TIMEOUT_SEC))
		for _frame in 30:
			await get_tree().process_frame
	).is_push_warning("TerrainField: the imagery tile is not a JPEG")
	EventBus.status_message.disconnect(listener)
	assert_bool(reported.size() == 1 and reported[0]).append_failure_message("the decode failure was never reported").is_true()
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	assert_int(selected.size()).append_failure_message("the camera selects one tile, so one tile proves nothing").is_greater(1)
	assert_int(messages.size()).append_failure_message(
		"%d status messages for %d undecodable tiles" % [messages.size(), selected.size()]
	).is_equal(1)


## True when every visible attached tile draws imagery whose boundary overlay is `on`.
func _all_visible_overlays(on: bool) -> bool:
	var checked := 0
	for key in _field.attached_keys():
		var tile := _field.tile_at(key)
		if not tile.visible:
			continue
		var shown := tile.shown_material()
		if not (shown is TerrainMaterial) or (shown as TerrainMaterial).boundaries_on() != on:
			return false
		checked += 1
	return checked > 0


## T-136 Part B: a source imported from the dialog emits `sources_changed`, and the field lists again at once.
func test_sources_changed_relists() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	var listings: Array = []
	Session.set_terrain_request_override(func(method: String, _params: Dictionary) -> IpcEnvelope:
		if method == "terrain.tiles":
			listings.append(method)
		return null
	)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(
		func() -> bool: return listings.size() >= 1 and _field.revision() >= 0, _FIELD_TIMEOUT_SEC
	)).is_true()
	var before := listings.size()
	EventBus.sources_changed.emit("terrain")
	var relisted := await _wait_until(func() -> bool: return listings.size() > before, 10.0)
	assert_bool(relisted).append_failure_message("sources_changed did not make the field list again").is_true()


## T-135 Part C (F52): a held imagery reply does not hold up the terrain. The override holds every imagery.tile until
## the test releases it. While the first one is held the camera moves to a view whose terrain is not attached yet; that
## terrain must still be fetched and attached. Released, the held tile drapes.
func test_a_slow_imagery_reply_does_not_delay_terrain() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	_held = {"released": false, "held": 0, "key": Vector3i(-1, -1, -1)}
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		_held["held"] = int(_held["held"]) + 1
		if int(_held["held"]) == 1:
			_held["key"] = Vector3i(int(params["level"]), int(params["ix"]), int(params["iy"]))
		while not _held["released"]:
			await get_tree().process_frame
		return null
	)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(func() -> bool: return int(_held["held"]) >= 1, _FIELD_TIMEOUT_SEC)).append_failure_message(
		"no imagery request was made"
	).is_true()

	var unattached := 0
	for key: Vector3i in TerrainLod.select_tiles(_CAMERA_B, _field.lists()):
		if _field.tile_at(key) == null:
			unattached += 1
	assert_int(unattached).append_failure_message("the camera move selects no terrain that is not attached").is_greater(0)
	_camera.position = _CAMERA_B
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).append_failure_message(
		"terrain selected while an imagery reply was held was never attached"
	).is_true()
	assert_int(int(_held["held"])).append_failure_message("a second imagery request went out while one was held").is_equal(1)

	var held_key: Vector3i = _held["key"]
	_held["released"] = true
	assert_bool(await _wait_until(func() -> bool: return _has_imagery(held_key), _FIELD_TIMEOUT_SEC)).append_failure_message(
		"the held imagery did not drape after its release"
	).is_true()
	assert_int(_field.tile_at(held_key).imagery_revision).is_equal(_field.imagery_revision())


## T-135 Part C (F52): one imagery request is in flight at a time. The override counts the imagery requests that are
## in flight, holding each for a few frames and then letting it reach the backend; the peak must be 1.
func test_one_imagery_request_is_in_flight_at_a_time() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var counts := {"in_flight": 0, "peak": 0, "requested": 0}
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		counts["in_flight"] = int(counts["in_flight"]) + 1
		counts["requested"] = int(counts["requested"]) + 1
		counts["peak"] = maxi(int(counts["peak"]), int(counts["in_flight"]))
		for _frame in 3:
			await get_tree().process_frame
		var envelope: IpcEnvelope = await Backend.request(method, params)
		counts["in_flight"] = int(counts["in_flight"]) - 1
		return envelope
	)
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	assert_int(int(counts["peak"])).append_failure_message("%d imagery requests were in flight at once" % counts["peak"]).is_equal(1)
	assert_int(int(counts["requested"])).append_failure_message("only %d imagery requests were made" % counts["requested"]).is_greater(1)


## T-135 Part C (F52): an incomplete composition is drawn, then requested again at the same revision after the delay,
## and kept once the retry is complete. The override makes the first reply of one tile incomplete.
func test_incomplete_imagery_is_retried_then_kept() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var state := {"key": Vector3i(-1, -1, -1), "times": [], "revisions": []}
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		var key := Vector3i(int(params["level"]), int(params["ix"]), int(params["iy"]))
		if state["key"] == Vector3i(-1, -1, -1):
			state["key"] = key
		if key != state["key"]:
			return null
		(state["times"] as Array).append(Time.get_ticks_msec())
		(state["revisions"] as Array).append(int(params["revision"]))
		var envelope: IpcEnvelope = await Backend.request(method, params)
		if envelope.type == "res":
			envelope.result["complete"] = (state["times"] as Array).size() >= 2
		return envelope
	)
	_field = _start_field(_CAMERA_A)
	_field.incomplete_retry_start_sec = 0.3
	_field.incomplete_retry_max_sec = 0.6
	assert_bool(await _wait_until(func() -> bool: return (state["times"] as Array).size() >= 2, _FIELD_TIMEOUT_SEC)).append_failure_message(
		"the incomplete tile was never requested again"
	).is_true()
	var times: Array = state["times"]
	var gap: int = times[1] - times[0]
	assert_int(gap).append_failure_message("the retry came %d ms after the first request, expected at least 300 ms" % gap).is_greater_equal(270)
	assert_bool(await _wait_until(_imagery_settled, _FIELD_TIMEOUT_SEC)).is_true()
	for revision: int in state["revisions"]:
		assert_int(revision).append_failure_message("the retry asked for another revision").is_equal(_field.imagery_revision())
	var tile := _field.tile_at(state["key"])
	assert_bool(tile != null and tile.has_imagery()).append_failure_message("the incomplete tile lost its drape").is_true()


## T-135 Part C (F52): a retry stops when its tile leaves the view. Every imagery reply is incomplete. Once the tile
## is draped, the camera moves to a view that does not select it; after its retry delay has passed, it must not be asked
## for again, while other tiles of the new view are retried, which shows that the retry timers are running.
func test_retry_stops_when_the_tile_leaves_the_view() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var asked := {}
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		var key := Vector3i(int(params["level"]), int(params["ix"]), int(params["iy"]))
		asked[key] = int(asked.get(key, 0)) + 1
		var envelope: IpcEnvelope = await Backend.request(method, params)
		if envelope.type == "res":
			envelope.result["complete"] = false
		return envelope
	)
	_field = _start_field(_CAMERA_A)
	_field.incomplete_retry_start_sec = 1.0
	_field.incomplete_retry_max_sec = 2.0
	assert_bool(await _wait_until(func() -> bool: return not asked.is_empty(), _FIELD_TIMEOUT_SEC)).is_true()
	var far := TerrainLod.select_tiles(_CAMERA_FAR, _field.lists())
	var leaver := Vector3i(-1, -1, -1)
	for key: Vector3i in asked.keys():
		if not far.has(key):
			leaver = key
			break
	assert_bool(leaver.x >= 0).append_failure_message("no imagery tile is outside the far view").is_true()
	assert_bool(await _wait_until(func() -> bool: return _has_imagery(leaver), _FIELD_TIMEOUT_SEC)).is_true()
	var due_by := Time.get_ticks_msec() + 1300
	_camera.position = _CAMERA_FAR
	assert_bool(await _wait_until(func() -> bool: return Time.get_ticks_msec() >= due_by, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(func() -> bool: return _retried_other_than(asked, leaver), _FIELD_TIMEOUT_SEC)).append_failure_message(
		"no tile of the far view was retried; the timers did not run"
	).is_true()
	assert_int(int(asked[leaver])).append_failure_message("the tile that left the view was requested again").is_equal(1)


## T-135 Part C review: every selected incomplete tile is asked for a second time. Its first reply is incomplete, its second
## complete, with short delays. This is a whole-path smoke test: it does not by itself catch the loss of the last tile's
## record. `test_a_record_survives_until_its_tile_is_draped` and `test_due_retry_skips_a_pending_tile` cover that.
func test_every_incomplete_tile_is_retried() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var asked := {}
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tile":
			return null
		var key := Vector3i(int(params["level"]), int(params["ix"]), int(params["iy"]))
		asked[key] = int(asked.get(key, 0)) + 1
		var envelope: IpcEnvelope = await Backend.request(method, params)
		if envelope.type == "res":
			envelope.result["complete"] = int(asked[key]) >= 2
		return envelope
	)
	_field = _start_field(_CAMERA_A)
	_field.incomplete_retry_start_sec = 0.3
	_field.incomplete_retry_max_sec = 0.6
	assert_bool(await _wait_until(func() -> bool: return _unretried_count(asked) == 0, 20.0)).is_true()
	assert_int(_unretried_count(asked)).append_failure_message("selected tiles not asked a second time").is_equal(0)


## T-135 Part C review: a level the backend refuses with `E_BAD_PARAMS` (too many tiles) is skipped, the coarser levels
## show, and the listing gives exactly one status message, not a backend error.
func test_a_refused_level_is_skipped_with_one_message() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	var messages := []
	var collect := func(text: String) -> void:
		messages.append(text)
	var errors := []
	var collect_error := func(code: String, _message: String) -> void:
		errors.append(code)
	EventBus.status_message.connect(collect)
	EventBus.backend_error.connect(collect_error)
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "terrain.tiles" or int(params["level"]) != 0:
			return null
		var refused := IpcEnvelope.new()
		refused.type = "err"
		refused.method = method
		refused.error = {"code": "E_BAD_PARAMS", "message": "more than 5000 tiles (test)"}
		refused.blobs = {}
		return refused
	)
	_field = _start_field(_CAMERA_A)
	var attached := await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)
	EventBus.status_message.disconnect(collect)
	EventBus.backend_error.disconnect(collect_error)
	assert_bool(attached).append_failure_message("the coarser levels never attached").is_true()
	assert_int(messages.size()).append_failure_message("status messages: %s" % str(messages)).is_equal(1)
	assert_array(errors).append_failure_message("backend errors for a refused level: %s" % str(errors)).is_empty()
	if messages.size() == 1:
		assert_bool(String(messages[0]).contains("Terrain level 0")).append_failure_message(String(messages[0])).is_true()
	var keys := _field.attached_keys()
	assert_int(keys.size()).is_greater(0)
	for key: Vector3i in keys:
		assert_int(key.x).append_failure_message("a tile of refused level 0 was attached: %s" % key).is_greater_equal(1)


## T-135 Part C review: an imagery level the backend refuses with `E_BAD_PARAMS` (too many tiles) is skipped. Its tiles show
## without imagery and the coarser imagery levels drape. The listing gives one status message and no backend error.
func test_a_refused_imagery_level_is_skipped_with_one_message() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	assert_bool(await _import_imagery(_FILE_IMAGE, Color(0.35, 0.55, 0.3), "Synthetic image")).is_true()
	var messages := []
	var collect := func(text: String) -> void:
		messages.append(text)
	var errors := []
	var collect_error := func(code: String, _message: String) -> void:
		errors.append(code)
	EventBus.status_message.connect(collect)
	EventBus.backend_error.connect(collect_error)
	Session.set_terrain_request_override(func(method: String, params: Dictionary) -> IpcEnvelope:
		if method != "imagery.tiles" or int(params["level"]) != 0:
			return null
		var refused := IpcEnvelope.new()
		refused.type = "err"
		refused.method = method
		refused.error = {"code": "E_BAD_PARAMS", "message": "more than 5000 tiles (test)"}
		refused.blobs = {}
		return refused
	)
	_field = _start_field(_CAMERA_A)
	var settled := await _wait_until(func() -> bool: return _field.settled(), _FIELD_TIMEOUT_SEC)
	EventBus.status_message.disconnect(collect)
	EventBus.backend_error.disconnect(collect_error)
	assert_bool(settled).append_failure_message("the field never settled with a refused imagery level").is_true()
	assert_array(errors).append_failure_message("backend errors for a refused imagery level: %s" % str(errors)).is_empty()
	assert_int(messages.size()).append_failure_message("status messages: %s" % str(messages)).is_equal(1)
	if messages.size() == 1:
		var text := String(messages[0])
		assert_bool(text.contains("Imagery level 0") and text.contains("those tiles show without imagery")).append_failure_message(
			text
		).is_true()
	var fine_tiles := 0
	var coarse_drapes := 0
	for key: Vector3i in _field.attached_keys():
		var tile := _field.tile_at(key)
		if key.x == 0:
			fine_tiles += 1
			assert_bool(tile.has_imagery()).append_failure_message("a tile of refused imagery level 0 was draped: %s" % key).is_false()
		elif tile.has_imagery():
			coarse_drapes += 1
	assert_int(fine_tiles).append_failure_message("no level-0 tile was attached, so the refusal was not exercised").is_greater(0)
	assert_int(coarse_drapes).append_failure_message("no coarser tile was draped").is_greater(0)


## T-135 Part C review: the record of an incomplete reply survives until its tile is draped. The drape job of a tile
## attaches a frame or more after its reply, so a record made at the reply must not be dropped while the tile has no
## current imagery. Deterministic: no timing, the check runs in one call.
func test_a_record_survives_until_its_tile_is_draped() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(func() -> bool: return _field.imagery_revision() >= 0, _FIELD_TIMEOUT_SEC)).is_true()
	# No imagery or terrain fetch in flight, so the nearest tile's fetch cannot be running. The wait comes before the record
	# is made, and nothing yields between the record and `_due_retry`, so the imagery lane cannot retry the tile meanwhile.
	assert_bool(await _wait_until(
		func() -> bool: return _field._imagery_pending.is_empty() and _field._jobs.is_empty(), _FIELD_TIMEOUT_SEC
	)).is_true()
	var key: Vector3i = _field._selected[0]
	var tile: TerrainTile = _field.tile_at(key)
	tile.imagery_revision = -1
	_field.incomplete_retry_start_sec = 0.0
	_field._note_completeness(key, false)
	assert_bool(_field._retry_still_wanted(key)).append_failure_message("the record of an undraped tile was dropped").is_true()
	assert_bool(_field._due_retry() == key).append_failure_message("the due retry did not return the undraped tile").is_true()


## T-135 Part C review: a tile whose imagery fetch is in flight is not retried, and its record is kept. Once the flag is
## cleared, the same record is returned. The flag stands for the fetch, so the check is deterministic and runs in one call.
func test_due_retry_skips_a_pending_tile() -> void:
	if not _require_ready():
		return
	assert_bool(await _open_project_with_terrain(_FILE_A)).is_true()
	_field = _start_field(_CAMERA_A)
	assert_bool(await _wait_until(_selection_attached, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(func() -> bool: return _field.imagery_revision() >= 0, _FIELD_TIMEOUT_SEC)).is_true()
	assert_bool(await _wait_until(
		func() -> bool: return _field._imagery_pending.is_empty() and _field._jobs.is_empty(), _FIELD_TIMEOUT_SEC
	)).is_true()
	var key: Vector3i = _field._selected[0]
	_field.incomplete_retry_start_sec = 0.0
	_field._note_completeness(key, false)
	_field._imagery_pending[key] = true
	assert_bool(_field._due_retry() == null).append_failure_message("a tile whose fetch is in flight was returned for retry").is_true()
	assert_bool(_field._incomplete.has(key)).append_failure_message("the record of a pending tile was dropped").is_true()
	_field._imagery_pending.erase(key)
	assert_bool(_field._due_retry() == key).append_failure_message("the tile was not returned once its fetch was over").is_true()


## The selected tiles not yet asked for a second time, or -1 before a listing exists.
func _unretried_count(asked: Dictionary) -> int:
	var selected := TerrainLod.select_tiles(_camera.global_position, _field.lists())
	if selected.is_empty():
		return -1
	var missing := 0
	for key: Vector3i in selected:
		if int(asked.get(key, 0)) < 2:
			missing += 1
	return missing


func _has_imagery(key: Vector3i) -> bool:
	var tile := _field.tile_at(key)
	return tile != null and tile.has_imagery()


## True once a tile other than `leaver` has been asked for twice, so the retry timers have run.
func _retried_other_than(asked: Dictionary, leaver: Vector3i) -> bool:
	for key: Vector3i in asked.keys():
		if key != leaver and int(asked[key]) >= 2:
			return true
	return false


## T-136 Part C3: the imagery warnings of a listing. Two warnings give one status message (the first, and how many more
## are in Data sources) and both lines in the popover under one heading. The same two with a new countdown give no new
## message but the new text. A listing without warnings clears the popover and emits no message. The listings are fakes
## through the Session seam, and the field lists them directly, so the test needs no backend.
func test_imagery_warnings_show_once_and_clear() -> void:
	Session.set_terrain_request_override(_fake_listing)
	_listen_for_status()
	var shell := await _popover_shell()
	await _start_imagery_field()

	var first := "ČÚZK is unreachable; retrying in 30 s"
	var second := "A second warning"
	_listing_warnings = [first, second]
	await _field._list_imagery()
	assert_array(_messages).append_failure_message("the first listing should give one message").is_equal([
		first + " — 1 more in Data sources",
	])
	assert_array(Array(Session.imagery_warnings())).is_equal([first, second])
	var texts := _label_texts(shell)
	assert_array(texts).append_failure_message("the popover lacks the warnings").contains([
		"Imagery warnings", first, second,
	])

	var countdown := "ČÚZK is unreachable; retrying in 12 s"
	_listing_warnings = [countdown, second]
	await _field._list_imagery()
	assert_int(_messages.size()).append_failure_message("a changed countdown repeated the message").is_equal(1)
	assert_array(Array(Session.imagery_warnings())).is_equal([countdown, second])
	assert_array(_label_texts(shell)).contains([countdown, second])

	_listing_warnings = []
	await _field._list_imagery()
	assert_int(_messages.size()).append_failure_message("clearing the warnings emitted a message").is_equal(1)
	assert_bool(Session.imagery_warnings().is_empty()).is_true()
	assert_bool(_label_texts(shell).has("Imagery warnings")).append_failure_message(
		"the popover still shows the warnings"
	).is_false()


## T-136 Part C3, review: hiding the Imagery layer empties the warnings and shows no message; showing it again brings
## them back, and that is no message either: the status bar reports a listing's warnings, not a layer's.
func test_hiding_imagery_hides_its_warnings_without_a_message() -> void:
	Session.set_terrain_request_override(_fake_listing)
	_listen_for_status()
	await _start_imagery_field()
	var warning := "ČÚZK is unreachable; retrying in 30 s"
	_listing_warnings = [warning]
	await _field._list_imagery()
	assert_int(_messages.size()).is_equal(1)

	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, false)
	await get_tree().process_frame
	assert_bool(Session.imagery_warnings().is_empty()).append_failure_message("hidden imagery kept its warnings").is_true()
	assert_int(_messages.size()).append_failure_message("hiding the layer emitted a message").is_equal(1)

	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, true)
	await get_tree().process_frame
	assert_array(Array(Session.imagery_warnings())).append_failure_message("showing imagery did not bring the warnings back").is_equal([warning])
	assert_int(_messages.size()).append_failure_message("showing the layer emitted a message").is_equal(1)


## T-136 Part C3, review: a listing that fails keeps the warnings it had and emits nothing. An E_BUSY answer and a
## revision that differs between the levels both leave the last listing's warnings in place.
func test_a_failed_imagery_listing_keeps_the_warnings() -> void:
	Session.set_terrain_request_override(_fake_listing)
	_listen_for_status()
	await _start_imagery_field()
	var warning := "ČÚZK is unreachable; retrying in 30 s"
	_listing_warnings = [warning]
	await _field._list_imagery()
	assert_int(_messages.size()).is_equal(1)

	for mode in ["busy", "mismatch"]:
		_listing_mode = mode
		await _field._list_imagery()
		assert_array(Array(Session.imagery_warnings())).append_failure_message(
			"a %s listing changed the warnings" % mode
		).is_equal([warning])
		assert_int(_messages.size()).append_failure_message("a %s listing emitted a message" % mode).is_equal(1)


## The fake `imagery.tiles` answers of the warnings tests: `_listing_warnings`, and `_listing_mode` ("ok", "busy", or
## "mismatch", where the coarsest level has a different revision from the others). Every answer waits a frame, as the
## backend does, so the paging lane never spins without yielding.
func _fake_listing(method: String, params: Dictionary) -> IpcEnvelope:
	await get_tree().process_frame
	if method == "source.list":
		return _listing_envelope([], {"sources": []})
	if method != "imagery.tiles":
		var refused := IpcEnvelope.new()
		refused.type = "err"
		refused.error = {"code": "E_NOT_FOUND", "message": "no such listing in this test"}
		return refused
	if _listing_mode == "busy":
		var busy := IpcEnvelope.new()
		busy.type = "err"
		busy.error = {"code": "E_BUSY", "message": "busy in this test"}
		return busy
	var revision := 1 if _listing_mode == "ok" or int(params["level"]) == TerrainLod.MAX_LEVEL else 2
	return _listing_envelope(_listing_warnings, {"revision": revision})


## A new field over the fake listings, with the Imagery layer shown, settled for two frames.
func _start_imagery_field() -> void:
	_field = TerrainField.new()
	get_tree().root.add_child(_field)
	Session.layers().set_visible(TerrainField.IMAGERY_LAYER_ID, true)
	await get_tree().process_frame
	await get_tree().process_frame


## Records the status-bar messages from now on. The listener is disconnected in [method after_test].
func _listen_for_status() -> void:
	_messages.clear()
	if not EventBus.status_message.is_connected(_record_status):
		EventBus.status_message.connect(_record_status)


func _record_status(text: String) -> void:
	_messages.append(text)


## A 1600 x 900 host with the shell in it, so the Data sources popover is built the way the app builds it.
func _popover_shell() -> UiShell:
	var host := Control.new()
	host.set_anchors_preset(Control.PRESET_TOP_LEFT)
	host.size = Vector2(1600.0, 900.0)
	get_tree().root.add_child(host)
	auto_free(host)
	var shell := (load("res://ui/shell/ui_shell.tscn") as PackedScene).instantiate() as UiShell
	host.add_child(shell)
	await get_tree().process_frame
	await get_tree().process_frame
	return shell


func _label_texts(shell: UiShell) -> Array:
	var texts := []
	for child in shell.data_sources_popover().find_children("*", "Label", true, false):
		texts.append((child as Label).text)
	return texts


## An `imagery.tiles` (or `source.list`) answer with no tiles, the given warnings, and the given extra result fields.
static func _listing_envelope(warnings: Array, extra: Dictionary = {}) -> IpcEnvelope:
	var envelope := IpcEnvelope.new()
	envelope.type = "res"
	envelope.result = {
		"revision": 1, "terrain_revision": -1, "tiles": [], "attributions": {}, "notices": {}, "warnings": warnings,
	}
	envelope.result.merge(extra)
	return envelope
