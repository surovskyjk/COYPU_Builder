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
	for path in [_FILE_A, _FILE_B]:
		DirAccess.remove_absolute(ProjectSettings.globalize_path(path))


func after_test() -> void:
	Session.set_terrain_request_override(Callable())
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
