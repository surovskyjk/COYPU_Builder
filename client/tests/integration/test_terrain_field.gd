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
	Session.set_terrain_request_override(Callable())
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
	var seam := func(method: String, _params: Dictionary) -> IpcEnvelope:
		if method == "imagery.tile":
			calls.append(method)
			if not moved[0]:
				moved[0] = true
				_field._revision += 1
		return null
	Session.set_terrain_request_override(seam)
	_field = _start_field(_CAMERA_A)
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
