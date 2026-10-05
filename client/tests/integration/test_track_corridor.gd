extends GdUnitTestSuite
## Live backend integration test for TrackCorridor (T-121): imports the Kralupy `.coypu` fixture, bakes
## its AlignmentTable, and builds a real corridor against `alignment.track_mesh` -- paging by
## `chunk_index` the whole way (F17), never `chunk_index: null`. Drives the real Backend/Session
## autoloads directly, same convention as `test_run_import.gd`, since `TrackCorridor.build` calls
## `Session.fetch_track_mesh` internally rather than a test-owned `BackendFixture`.
##
## Tests run in declaration order and share the project/corridor built in the first test (same convention
## as `test_run_import.gd`). `after()` always calls `Backend.shutdown()`, even when a test above failed or
## timed out, so this suite cannot leak a backend process.

const _READY_TIMEOUT_SEC := 15.0
const _BUILD_TIMEOUT_SEC := 60.0
const _POLL_STEP_SEC := 0.05
const _COYPU_FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu"
const _MAX_RESPONSE_BYTES := 8 * 1024 * 1024  # task_121 AC8 / F17
const _MAX_METADATA_BYTES := 64 * 1024  # task_121 F18 AC1
const _CORRIDOR_SCRIPT_PATH := "res://scene/track/track_corridor.gd"
## A chunk length at which the retired `ceil(span / 250)` client formula would stop 18 chunks short (F18).
const _TRUNCATION_PROOF_CHUNK_LENGTH_M := 200.0
const _KRALUPY_CHUNKS := 73
const _KRALUPY_VERTICES := 440448
const _KRALUPY_SLEEPERS := 30333

var _alignment_id := ""
var _table: AlignmentTable
var _corridor: TrackCorridor


func before() -> void:
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)
	Session.layers().define(TrackCorridor.LAYER_ID, "Track")


func after() -> void:
	Session.layers().set_visible(TrackCorridor.LAYER_ID, true)
	if _corridor != null:
		_corridor.queue_free()
	Backend.shutdown()


func _await_state(target: Backend.State, timeout_sec: float) -> bool:
	var elapsed := 0.0
	while Backend.state() != target and elapsed < timeout_sec:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	return Backend.state() == target


func _require_ready() -> bool:
	if Backend.state() != Backend.State.READY:
		fail("backend never reached READY")
		return false
	return true


func test_build_produces_a_continuous_corridor_with_distinct_tile_origins() -> void:
	if not _require_ready():
		return

	var project := await Backend.request("project.new", {})
	assert_str(project.type).is_equal("res")

	var fixture_path := ProjectSettings.globalize_path("res://").path_join(_COYPU_FIXTURE_RELATIVE_PATH)
	var imported := await Session.import_coypu(fixture_path)
	assert_bool(imported).append_failure_message("Session.import_coypu failed").is_true()
	if not imported:
		return

	var alignments := Session.alignments()
	assert_int(alignments.size()).is_greater(0)
	_alignment_id = alignments[0]["alignment_id"]

	var table := await Session.fetch_alignment_table(_alignment_id)
	assert_that(table).is_not_null()
	if table == null:
		return
	_table = table

	_corridor = TrackCorridor.new()
	get_tree().root.add_child(_corridor)

	var started_msec := Time.get_ticks_msec()
	await _corridor.build(_alignment_id, table)
	var elapsed_sec := (Time.get_ticks_msec() - started_msec) / 1000.0
	assert_float(elapsed_sec).append_failure_message(
		"corridor build took %.1fs against a real backend -- looks stuck" % elapsed_sec
	).is_less(_BUILD_TIMEOUT_SEC)

	var chunk_count := _corridor.chunk_count()
	assert_int(chunk_count).append_failure_message("no chunks were built").is_greater(0)
	assert_int(_corridor.total_vertex_count()).is_greater(0)
	assert_int(_corridor.sleeper_instance_count()).is_greater(0)

	# F18 AC3: the corridor still renders complete -- these are the figures T-121 reported.
	assert_int(chunk_count).is_equal(_KRALUPY_CHUNKS)
	assert_int(_corridor.total_vertex_count()).is_equal(_KRALUPY_VERTICES)
	assert_int(_corridor.sleeper_instance_count()).is_equal(_KRALUPY_SLEEPERS)

	# task_121 AC7: every chunk node's position (its tile origin) is non-zero and distinct.
	var seen := {}
	for i in chunk_count:
		var origin := _corridor.tile_origin_at(i)
		assert_vector(origin).append_failure_message("chunk %d has a zero tile origin" % i).is_not_equal(
			Vector3.ZERO
		)
		var key := "%.3f,%.3f,%.3f" % [origin.x, origin.y, origin.z]
		assert_bool(seen.has(key)).append_failure_message(
			"chunk %d shares its tile origin %s with an earlier chunk" % [i, origin]
		).is_false()
		seen[key] = true


func test_chunk_count_comes_from_the_backend_chunk_list() -> void:
	if not _require_ready():
		return
	if _corridor == null or _alignment_id.is_empty():
		fail("no corridor built -- see the corridor-build test above")
		return

	var listed := await Session.fetch_track_mesh_chunks(
		_alignment_id, TrackCorridor.CHUNK_LENGTH_M, TrackCorridor.FRAME_SPACING_M
	)
	assert_int(listed.size()).append_failure_message("metadata-only response listed no chunks").is_greater(0)
	var listed_count := _distinct_chunk_indices(listed).size()
	assert_int(listed_count).append_failure_message(
		"corridor built %d chunks but the backend listed %d" % [_corridor.chunk_count(), listed_count]
	).is_equal(_corridor.chunk_count())

	# F18 AC1: "a few KB" -- the JSON text of the list is a faithful proxy for the response frame, which
	# carries no blobs; the backend suite asserts the raw frame size too.
	var metadata_bytes := JSON.stringify(listed).to_utf8_buffer().size()
	assert_int(metadata_bytes).append_failure_message(
		"metadata-only chunk list is %d bytes, over the %d byte budget" % [metadata_bytes, _MAX_METADATA_BYTES]
	).is_less(_MAX_METADATA_BYTES)


func test_corridor_script_contains_no_chunk_count_arithmetic() -> void:
	# F18 AC2: the count is the response length; the client holds no second expression of the chunking.
	var source := FileAccess.get_file_as_string(_CORRIDOR_SCRIPT_PATH)
	assert_str(source).is_not_empty()
	var arithmetic := RegEx.create_from_string("ceil[fi]?\\s*\\(|/\\s*(CHUNK_LENGTH_M|chunk_length_m)")
	var found := arithmetic.search(source)
	assert_bool(found == null).append_failure_message(
		"track_corridor.gd contains `%s` -- chunk counts must come from the backend (F18)"
		% (found.get_string() if found != null else "")
	).is_true()


func test_no_single_track_mesh_response_exceeds_the_inbound_buffer_budget() -> void:
	if not _require_ready():
		return
	if _corridor == null or _alignment_id.is_empty():
		fail("no corridor built -- see the corridor-build test above")
		return

	# task_121 AC8: assert this directly against the wire response, independent of any future
	# TrackCorridor.CHUNK_LENGTH_M change, so raising it can never silently reintroduce F17.
	# Session.fetch_track_mesh's cache means this reuses the corridor build's own responses rather than
	# re-fetching from the backend.
	for i in _corridor.chunk_count():
		var envelope := await Session.fetch_track_mesh(
			_alignment_id, i, TrackCorridor.CHUNK_LENGTH_M, TrackCorridor.FRAME_SPACING_M
		)
		assert_that(envelope).is_not_null()
		if envelope == null:
			continue
		var bytes := _envelope_blob_bytes(envelope)
		assert_int(bytes).append_failure_message(
			"chunk_index=%d response is %d bytes, over the %d byte budget (F17)" % [i, bytes, _MAX_RESPONSE_BYTES]
		).is_less(_MAX_RESPONSE_BYTES)


func test_truncation_is_impossible_when_the_backend_chunks_differently() -> void:
	if not _require_ready():
		return
	if _table == null or _alignment_id.is_empty():
		fail("no alignment table -- see the corridor-build test above")
		return

	# F18 AC4: at 200 m the backend produces more chunks than the retired `ceil(span / 250)` would have
	# fetched, so a client that counted for itself would stop short and silently draw a truncated corridor.
	var span := _table.station_end() - _table.station_start()
	var naive_count := maxi(1, int(ceil(span / TrackCorridor.CHUNK_LENGTH_M)))

	var listed := await Session.fetch_track_mesh_chunks(
		_alignment_id, _TRUNCATION_PROOF_CHUNK_LENGTH_M, TrackCorridor.FRAME_SPACING_M
	)
	var backend_count := _distinct_chunk_indices(listed).size()
	assert_int(backend_count).append_failure_message(
		"test setup: the backend's %d chunks must exceed the naive count %d for this to prove anything"
		% [backend_count, naive_count]
	).is_greater(naive_count)

	var corridor := TrackCorridor.new()
	get_tree().root.add_child(corridor)
	await corridor.build(_alignment_id, _table, _TRUNCATION_PROOF_CHUNK_LENGTH_M)

	assert_int(corridor.chunk_count()).append_failure_message(
		"corridor built %d chunks; the backend listed %d (naive formula would give %d)"
		% [corridor.chunk_count(), backend_count, naive_count]
	).is_equal(backend_count)

	# The listed chunks cover the whole station range, and the corridor holds one tile per listed chunk.
	var covered_end := -INF
	for info: Dictionary in listed:
		covered_end = maxf(covered_end, float(info["station_end"]))
	assert_float(covered_end).is_equal_approx(_table.station_end(), 1e-6)

	corridor.queue_free()


func test_corridor_builds_exactly_the_chunks_the_backend_listed() -> void:
	if not _require_ready():
		return
	if _table == null or _alignment_id.is_empty():
		fail("no alignment table -- see the corridor-build test above")
		return

	# F18 AC4, strengthened: seed the chunk list for the exact parameters build() will request with a
	# subset (0, 1 and 5) that no function of span and chunk length can produce, and the corridor must
	# build those chunks and no others.
	var real_list := await Session.fetch_track_mesh_chunks(
		_alignment_id, TrackCorridor.CHUNK_LENGTH_M, TrackCorridor.FRAME_SPACING_M
	)
	var wanted := [0, 1, 5]
	var subset: Array = []
	var origins := {}
	for info: Dictionary in real_list:
		var index := int(info["chunk_index"])
		if wanted.has(index):
			subset.append(info)
			var origin_arr: Array = info["tile_origin"]
			origins[index] = Vector3(origin_arr[0], origin_arr[1], origin_arr[2])
	assert_int(origins.size()).append_failure_message("test setup: chunks 0, 1 and 5 must exist").is_equal(
		wanted.size()
	)

	Session._track_mesh_chunk_lists[_alignment_id] = {
		"chunk_length_m": TrackCorridor.CHUNK_LENGTH_M,
		"spacing_m": TrackCorridor.FRAME_SPACING_M,
		"chunks": subset,
	}
	var corridor := TrackCorridor.new()
	get_tree().root.add_child(corridor)
	await corridor.build(_alignment_id, _table)
	Session._track_mesh_chunk_lists[_alignment_id] = {
		"chunk_length_m": TrackCorridor.CHUNK_LENGTH_M,
		"spacing_m": TrackCorridor.FRAME_SPACING_M,
		"chunks": real_list,
	}

	assert_int(corridor.chunk_count()).append_failure_message(
		"corridor built %d chunks from a 3-chunk backend list" % corridor.chunk_count()
	).is_equal(wanted.size())
	for i in corridor.chunk_count():
		assert_vector(corridor.tile_origin_at(i)).is_equal(origins[wanted[i]])

	corridor.queue_free()


func test_layer_state_visibility_hides_and_restores_the_corridor() -> void:
	if not _require_ready():
		return
	if _corridor == null:
		fail("no corridor built -- see the corridor-build test above")
		return

	Session.layers().set_visible(TrackCorridor.LAYER_ID, false)
	assert_bool(_corridor.visible).append_failure_message(
		"LayerState.set_visible('track', false) did not hide the corridor"
	).is_false()

	Session.layers().set_visible(TrackCorridor.LAYER_ID, true)
	assert_bool(_corridor.visible).append_failure_message(
		"LayerState.set_visible('track', true) did not restore the corridor"
	).is_true()


## Distinct `chunk_index` values in a metadata-only chunk list (one entry per surface per chunk).
func _distinct_chunk_indices(listed: Array) -> Dictionary:
	var indices := {}
	for info: Dictionary in listed:
		indices[int(info["chunk_index"])] = true
	return indices


## Sums decoded blob byte sizes at their wire widths (float32 vectors/scalars, int32 indices) as a proxy
## for the response's actual frame size -- the blob tail dominates the frame, the JSON header is tiny.
func _envelope_blob_bytes(envelope: IpcEnvelope) -> int:
	var total := 0
	for key: String in envelope.blobs.keys():
		var blob: Variant = envelope.blobs[key]
		if blob is PackedVector3Array:
			total += (blob as PackedVector3Array).size() * 12
		elif blob is PackedVector2Array:
			total += (blob as PackedVector2Array).size() * 8
		elif blob is PackedFloat32Array:
			total += (blob as PackedFloat32Array).size() * 4
		elif blob is PackedFloat64Array:
			total += (blob as PackedFloat64Array).size() * 8
		elif blob is PackedInt32Array:
			total += (blob as PackedInt32Array).size() * 4
		elif blob is PackedByteArray:
			total += (blob as PackedByteArray).size()
	return total
