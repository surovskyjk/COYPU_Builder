extends GdUnitTestSuite
## Live backend round trip for the domain mirror (T-115): `import.coypu` on the Kralupy fixture ->
## `run.list` (cross-checked against `Session.runs()`, which `Session.import_coypu` populates from the
## same `import.coypu` result) -> `Session.fetch_run_table` -> sampling. Drives the real `Backend`
## autoload directly (same convention as `test_connection_lifecycle.gd`), since `Session`'s coroutines
## call `Backend.request` internally rather than a test-owned `BackendFixture`.
##
## Tests run in declaration order and share the project/import set up in the first test (same convention
## as `test_backend_roundtrip.gd`). `after()` always calls `Backend.shutdown()`, even when a test above
## failed or timed out, so this suite cannot leak a backend/uv/python process.

const _READY_TIMEOUT_SEC := 15.0
const _POLL_STEP_SEC := 0.05
const _COYPU_FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu"

var _run_id := ""
var _alignment_id := ""


func before() -> void:
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)


func after() -> void:
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


func test_import_coypu_populates_session_alignments_and_runs() -> void:
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

	# `run.list` returns the same runs `import.coypu` already handed back — Session.import_coypu stores
	# them without a second round trip, so this is a cross-check, not a second source of truth.
	var run_list := await Backend.request("run.list", {})
	assert_str(run_list.type).is_equal("res")
	var wire_runs: Array = run_list.result.get("runs", [])
	assert_int(wire_runs.size()).is_greater(0)

	var session_runs := Session.runs()
	assert_int(session_runs.size()).is_equal(wire_runs.size())
	_run_id = session_runs[0]["run_id"]


func test_fetch_run_table_samples_are_finite_and_monotone_in_the_run_direction() -> void:
	if not _require_ready():
		return
	if _run_id.is_empty():
		fail("no run id available — see 'test_import_coypu_populates_session_alignments_and_runs'")
		return

	var table := await Session.fetch_run_table(_run_id)
	assert_that(table).is_not_null()
	if table == null:
		return

	var direction := table.direction()
	var duration := table.duration()
	assert_float(duration).is_greater(0.0)

	var previous_station := 0.0
	for i in range(10):
		var t: float = duration * float(i) / 9.0
		var station := table.station_at(t)
		var speed := table.speed_at(t)
		assert_bool(is_finite(station)).append_failure_message("t=%s station=%s" % [t, station]).is_true()
		assert_bool(is_finite(speed)).append_failure_message("t=%s speed=%s" % [t, speed]).is_true()

		if i > 0:
			# Dwells hold station flat, so this is monotone, not strictly increasing; a small slack
			# absorbs float32 noise at the boundary between two rows.
			if direction >= 0:
				assert_float(station).append_failure_message(
					"t=%s station=%s previous=%s direction=%s" % [t, station, previous_station, direction]
				).is_greater_equal(previous_station - 0.5)
			else:
				assert_float(station).append_failure_message(
					"t=%s station=%s previous=%s direction=%s" % [t, station, previous_station, direction]
				).is_less_equal(previous_station + 0.5)
		previous_station = station


func test_second_fetch_alignment_table_with_identical_params_is_cached() -> void:
	if not _require_ready():
		return
	if _alignment_id.is_empty():
		fail("no alignment id available — see 'test_import_coypu_populates_session_alignments_and_runs'")
		return

	var first := await Session.fetch_alignment_table(_alignment_id)
	assert_that(first).is_not_null()
	if first == null:
		return
	assert_int(first.row_count()).is_greater(0)

	var second := await Session.fetch_alignment_table(_alignment_id)
	assert_bool(second == first).append_failure_message(
		"expected the cached AlignmentTable instance, got a freshly-baked one"
	).is_true()


func test_fetch_run_table_after_backend_killed_resolves_without_hanging() -> void:
	if not _require_ready():
		return

	var backend_error_box: Array = [false]
	var on_backend_error := func(_code: String, _message: String) -> void: backend_error_box[0] = true
	EventBus.backend_error.connect(on_backend_error)

	Backend.debug_kill_backend_process()
	# Give IpcWebSocketClient a frame to poll the peer and notice the closed connection — issuing the
	# request in the same instant races WebSocketPeer's own state update (is_open() still reads stale
	# OPEN) and hits the peer's raw send() on a dead socket instead of Backend.request()'s clean
	# "not connected" guard. Waiting for "no longer READY" (rather than a specific RECONNECTING/FAILED
	# target) is robust to how fast the backoff state machine moves past that state on its own.
	var elapsed := 0.0
	while Backend.state() == Backend.State.READY and elapsed < 5.0:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC

	var started_msec := Time.get_ticks_msec()
	var table := await Session.fetch_run_table("no-such-run-id-%d" % Time.get_ticks_usec())
	var elapsed_sec := (Time.get_ticks_msec() - started_msec) / 1000.0

	if EventBus.backend_error.is_connected(on_backend_error):
		EventBus.backend_error.disconnect(on_backend_error)

	assert_that(table).is_null()
	assert_bool(backend_error_box[0]).append_failure_message(
		"Session.fetch_run_table did not emit EventBus.backend_error on a dead connection"
	).is_true()
	assert_float(elapsed_sec).append_failure_message(
		"took %.2fs to resolve — looks like it hung instead of failing fast" % elapsed_sec
	).is_less(Backend.REQUEST_TIMEOUT_SEC)
