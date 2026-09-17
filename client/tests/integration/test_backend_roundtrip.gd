extends GdUnitTestSuite
## Live backend round-trip (T-102): ported from the old `run_ipc_tests.gd` `_run_backend_round_trip`
## section. One real backend is spawned for the whole suite (`before`/`after`) since the RPCs are a
## single stateful workflow — `import.landxml` must run before `alignment.frame_table` can use its
## alignment id — exactly as the old runner drove it.
##
## `after()` always calls `BackendFixture.stop()`, even when a test above failed or timed out, so this
## suite cannot leak a backend/uv/python process (acceptance criterion 5).

const FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.xml"

var _fixture: BackendFixture
var _alignment_id := ""


func before() -> void:
	_fixture = await BackendFixture.start()


func after() -> void:
	if _fixture != null:
		_fixture.stop()
		_fixture = null


func _require_fixture() -> bool:
	if _fixture == null:
		fail("backend fixture unavailable — see 'test_backend_starts_and_connects' for why")
		return false
	return true


func test_backend_starts_and_connects() -> void:
	assert_that(_fixture).is_not_null()
	if _fixture == null:
		return
	assert_that(_fixture.client()).is_not_null()
	assert_bool(_fixture.client().is_open()).is_true()

	var protocol_version: int = _fixture.hello_result.get("protocol_version", -1)
	assert_int(protocol_version).is_equal(IpcEnvelope.PROTOCOL_VERSION)


func test_project_new_succeeds() -> void:
	if not _require_fixture():
		return
	var project := await _fixture.request("project.new", {})
	assert_str(project.type).is_equal("res")


func test_import_landxml_returns_exactly_one_alignment() -> void:
	if not _require_fixture():
		return
	var fixture_path := ProjectSettings.globalize_path("res://").path_join(FIXTURE_RELATIVE_PATH)
	var imported := await _fixture.request("import.landxml", {"path": fixture_path})
	assert_str(imported.type).is_equal("res")

	var alignments: Array = imported.result.get("alignments", [])
	assert_int(alignments.size()).is_equal(1)
	if not alignments.is_empty():
		_alignment_id = alignments[0]["alignment_id"]


func test_frame_table_blob_sizes_match_row_count() -> void:
	if not _require_fixture():
		return
	if _alignment_id.is_empty():
		fail("no alignment id available — see 'test_import_landxml_returns_exactly_one_alignment'")
		return

	var frame_table := await _fixture.request(
		"alignment.frame_table", {"alignment_id": _alignment_id, "spacing_m": 25.0}
	)
	assert_str(frame_table.type).is_equal("res")

	var row_count: int = frame_table.result.get("row_count", 0)
	assert_int(row_count).is_greater(0)

	var station: PackedFloat32Array = frame_table.blobs.get("station", PackedFloat32Array())
	var position: PackedVector3Array = frame_table.blobs.get("position", PackedVector3Array())
	var rotation: PackedFloat32Array = frame_table.blobs.get("rotation", PackedFloat32Array())
	assert_int(station.size()).is_equal(row_count)
	assert_int(position.size()).is_equal(row_count)
	assert_int(rotation.size()).is_equal(row_count * 4)


func test_unknown_alignment_id_yields_not_found() -> void:
	if not _require_fixture():
		return
	var missing := await _fixture.request("alignment.frame_table", {"alignment_id": "no-such-id"})
	assert_str(missing.type).is_equal("err")
	assert_str(missing.error.get("code", "")).is_equal("E_NOT_FOUND")
