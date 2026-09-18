extends GdUnitTestSuite
## Connection lifecycle tests for the real `Backend` autoload (T-103): handshake, heartbeat liveness,
## crash recovery and clean shutdown. Unlike `test_backend_roundtrip.gd`, which drives its own
## `BackendFixture` to test the IPC round trip, this suite exercises the actual state machine in
## `client/core/backend.gd` — no `--backend-url` is present when gdUnit4 runs the suite, so `start()`
## spawns a real backend the same way a bare `godot --path client` launch does.
##
## Tests run in declaration order and share one `Backend` session opened in `before()` (same convention
## as `test_backend_roundtrip.gd`): each disruptive test restores READY before the next test runs.
## `after()` always calls `Backend.shutdown()`, even when a test above failed or timed out, so this
## suite cannot leak a backend/uv/python process into the rest of the run (acceptance criterion 8).

const _READY_TIMEOUT_SEC := 15.0
const _RECONNECT_TIMEOUT_SEC := 20.0
const _POLL_STEP_SEC := 0.05

const _FIXTURE_RELATIVE_PATH := "../backend/tests/fixtures/kralupy/kralupy_neratovice_092.xml"
## Fine enough to bake ~9M rows on the ~18.2 km Kralupy fixture: measured at ~8 s of pure CPU-bound
## baking (see the comment on the in-flight test below for why that number is what makes it safe).
const _SLOW_FRAME_TABLE_SPACING_M := 0.002
const _KILL_DELAY_SEC := 0.1


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
		fail("backend never reached READY — see 'test_start_reaches_ready_with_real_server_info'")
		return false
	return true


func test_start_reaches_ready_with_real_server_info() -> void:
	assert_int(Backend.state()).is_equal(Backend.State.READY)
	assert_str(Backend.server_version()).is_not_empty()
	assert_int(Backend.protocol_version()).is_equal(IpcEnvelope.PROTOCOL_VERSION)


func test_unknown_method_resolves_with_unknown_method_error_not_a_timeout() -> void:
	if not _require_ready():
		return
	var started_msec := Time.get_ticks_msec()
	var reply := await Backend.request("no.such.method", {})
	var elapsed_sec := (Time.get_ticks_msec() - started_msec) / 1000.0

	assert_str(reply.type).is_equal("err")
	assert_str(reply.error.get("code", "")).is_equal("E_UNKNOWN_METHOD")
	assert_float(elapsed_sec).append_failure_message(
		"took %.1fs — looks like it timed out instead of getting a prompt error reply" % elapsed_sec
	).is_less(Backend.REQUEST_TIMEOUT_SEC)


func test_heartbeat_keeps_the_connection_ready_across_multiple_intervals() -> void:
	if not _require_ready():
		return
	await get_tree().create_timer(Backend.HEARTBEAT_INTERVAL_SEC * 2.5).timeout
	assert_int(Backend.state()).is_equal(Backend.State.READY)


## F5 (2026-09-18 review): this used to race a 0.01 s kill timer against `session.ping` — the cheapest
## call in the protocol, round-tripping in ~1-5 ms — so the real `res` reliably won and the test failed
## 3/3 runs. Fixed by issuing a request that cannot possibly reply within the kill window *by
## construction*, not by chance:
##
## `alignment.frame_table` bakes the whole table, packs the blobs and encodes the frame — entirely
## synchronously, on the backend's single asyncio thread — before it ever calls `ws.send()` (see
## `server/app.py: _dispatch` and `server/handlers.py: handle_alignment_frame_table`). At
## `_SLOW_FRAME_TABLE_SPACING_M`, that bake alone measures ~8 s of CPU time on the machine this was
## written on. Killing the process `_KILL_DELAY_SEC` (0.1 s) after sending is therefore not "probably
## before the reply" — no reply can *exist* yet, because the handler hasn't returned. The ~80x margin
## between the two numbers is what makes this survive a machine ten times faster (bake ~0.8 s, still
## dwarfing a 0.1 s kill) or ten times slower (bake ~80 s, and we never wait around for it — we kill at
## 0.1 s either way and assert on the elapsed time below, not on how long the bake would have taken).
func test_in_flight_request_resolves_with_disconnected_when_backend_is_killed() -> void:
	if not _require_ready():
		return

	await Backend.request("project.new", {})
	var fixture_path := ProjectSettings.globalize_path("res://").path_join(_FIXTURE_RELATIVE_PATH)
	var imported := await Backend.request("import.landxml", {"path": fixture_path})
	if imported.type != "res":
		fail("setup failed: import.landxml returned %s" % str(imported.error))
		return
	var alignments: Array = imported.result.get("alignments", [])
	if alignments.is_empty():
		fail("setup failed: import.landxml returned no alignments")
		return
	var alignment_id: String = alignments[0]["alignment_id"]

	# Armed before the request is sent: this SceneTreeTimer ticks on the same per-frame processing as
	# `request()`'s own poll loop, independently of it, so it fires ~_KILL_DELAY_SEC after this line runs
	# regardless of what the awaited coroutine below is doing.
	get_tree().create_timer(_KILL_DELAY_SEC).timeout.connect(Backend.debug_kill_backend_process)

	var started_msec := Time.get_ticks_msec()
	var reply := await Backend.request(
		"alignment.frame_table", {"alignment_id": alignment_id, "spacing_m": _SLOW_FRAME_TABLE_SPACING_M}
	)
	var elapsed_sec := (Time.get_ticks_msec() - started_msec) / 1000.0

	assert_str(reply.type).is_equal("err")
	assert_str(reply.error.get("code", "")).is_equal("E_DISCONNECTED")
	# Resolving in a couple hundred ms — nowhere near the ~8 s a real bake takes — is the positive proof
	# that this was genuinely still in flight when killed, not that it happened to finish first.
	assert_float(elapsed_sec).append_failure_message(
		"took %.2fs to resolve — too slow to be the kill; the request may have completed for real" % elapsed_sec
	).is_less(2.0)

	var ready_again := await _await_state(Backend.State.READY, _RECONNECT_TIMEOUT_SEC)
	assert_bool(ready_again).append_failure_message(
		"never returned to READY after respawn; state=%d" % Backend.state()
	).is_true()


func test_killing_the_backend_process_reconnects_and_returns_to_ready() -> void:
	if not _require_ready():
		return
	var pid_before := Backend.debug_backend_pid()
	assert_int(pid_before).is_greater(0)

	Backend.debug_kill_backend_process()

	var reconnecting := await _await_state(Backend.State.RECONNECTING, 5.0)
	assert_bool(reconnecting).append_failure_message(
		"never observed RECONNECTING; state=%d" % Backend.state()
	).is_true()

	var ready_again := await _await_state(Backend.State.READY, _RECONNECT_TIMEOUT_SEC)
	assert_bool(ready_again).append_failure_message(
		"never returned to READY after respawn; state=%d" % Backend.state()
	).is_true()

	var pid_after := Backend.debug_backend_pid()
	assert_int(pid_after).is_greater(0)
	assert_int(pid_after).is_not_equal(pid_before)


func test_shutdown_leaves_no_orphaned_backend_process() -> void:
	if not _require_ready():
		return
	var pid := Backend.debug_backend_pid()
	assert_int(pid).is_greater(0)

	Backend.shutdown()
	await get_tree().create_timer(0.5).timeout

	assert_int(Backend.state()).is_equal(Backend.State.DISCONNECTED)
	assert_bool(OS.is_process_running(pid)).append_failure_message(
		"pid %d is still running after shutdown()" % pid
	).is_false()

	# Restore a live backend so `after()` (and any later suite reusing the same autoload) sees a clean,
	# stopped Backend rather than one mid-reconnect.
	Backend.start()
	await _await_state(Backend.State.READY, _READY_TIMEOUT_SEC)
