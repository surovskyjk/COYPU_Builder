extends GdUnitTestSuite
## T-126 acceptance 6-8: the session token reaches the client through the backend's startup line or its
## discovery record only, never appears in the client's output, and attach mode works through the discovery
## record. Real backends are spawned; the tokens are the real, generated ones. The backend's stderr is
## checked by `backend/tests/test_ipc_security.py::test_cli_serve_end_to_end`: a pipe read in Godot blocks
## when the backend has written nothing, so the client cannot drain it safely.

const _READY_TIMEOUT_SEC := 20.0
const _POLL_STEP_SEC := 0.05

var _fixture: BackendFixture
var _logger: CaptureLogger


## Records every line the engine prints or logs while it is installed with [method OS.add_logger].
class CaptureLogger:
	extends Logger

	var lines: PackedStringArray = PackedStringArray()
	var _mutex := Mutex.new()

	func _log_message(message: String, _error: bool) -> void:
		_mutex.lock()
		lines.append(message)
		_mutex.unlock()

	func _log_error(
		function: String,
		file: String,
		line: int,
		code: String,
		rationale: String,
		_editor_notify: bool,
		_error_type: int,
		_script_backtraces: Array[ScriptBacktrace]
	) -> void:
		_mutex.lock()
		lines.append("%s %s:%d %s %s" % [function, file, line, code, rationale])
		_mutex.unlock()

	func text() -> String:
		_mutex.lock()
		var joined := "\n".join(lines)
		_mutex.unlock()
		return joined


func before_test() -> void:
	_logger = CaptureLogger.new()
	OS.add_logger(_logger)


func after_test() -> void:
	if _logger != null:
		OS.remove_logger(_logger)
		_logger = null
	Backend.shutdown()
	if _fixture != null:
		_fixture.stop()
		_fixture = null


func _await_state(target: Backend.State) -> bool:
	var elapsed := 0.0
	while Backend.state() != target and elapsed < _READY_TIMEOUT_SEC:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	return Backend.state() == target


func test_the_token_never_appears_in_client_output() -> void:
	print("token-hygiene canary")
	Backend.start()
	assert_bool(await _await_state(Backend.State.READY)).is_true()
	await get_tree().create_timer(Backend.HEARTBEAT_INTERVAL_SEC * 1.5).timeout

	var token := Backend._debug_session_token()
	assert_int(token.length()).is_greater_equal(43)

	var unknown := await Backend.request("no.such.method", {})
	assert_str(unknown.error.get("code", "")).is_equal("E_UNKNOWN_METHOD")

	var client_output := _logger.text()
	assert_bool(client_output.contains("token-hygiene canary")).is_true()
	assert_bool(client_output.contains(token)).is_false()


func test_a_wrong_token_is_refused_and_the_real_one_is_not_echoed() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	var token := _fixture.session_token()
	assert_int(token.length()).is_greater_equal(43)

	for wrong in ["", "wrong", token.substr(0, token.length() - 1)]:
		var reply := await _fixture.request(
			"session.hello", {"client": "gdunit-tests", "client_version": "0", "token": wrong}
		)
		assert_str(reply.type).is_equal("err")
		assert_str(reply.error.get("code", "")).is_equal("E_UNAUTHORIZED")
		assert_bool(str(reply.error).contains(token)).is_false()

	var missing := await _fixture.request(
		"session.hello", {"client": "gdunit-tests", "client_version": "0"}
	)
	assert_str(missing.error.get("code", "")).is_equal("E_UNAUTHORIZED")
	assert_bool(_logger.text().contains(token)).is_false()


func test_the_discovery_record_matches_the_running_backend() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	var port := int(_fixture.backend_url.get_slice(":", 2))
	var record := IpcDiscovery.find(port)
	assert_bool(record.is_empty()).is_false()
	assert_int(record["port"]).is_equal(port)
	var same_token: bool = record["token"] == _fixture.session_token()
	assert_bool(same_token).is_true()
	assert_str(record["protocol"]).is_equal(str(IpcEnvelope.PROTOCOL_VERSION))
	assert_str(record["backend_version"]).is_equal(_fixture.hello_result.get("server_version", "?"))


func test_attach_mode_connects_through_the_discovery_record_without_spawning() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	var token := _fixture.session_token()

	Backend.start({"attach": true, "backend_url": _fixture.backend_url})
	assert_bool(await _await_state(Backend.State.READY)).is_true()
	assert_int(Backend.debug_backend_pid()).is_equal(-1)
	var same_token: bool = Backend._debug_session_token() == token
	assert_bool(same_token).is_true()
	assert_int(Backend.protocol_version()).is_equal(IpcEnvelope.PROTOCOL_VERSION)
	assert_bool(_logger.text().contains(token)).is_false()

	Backend.shutdown()
	# Attach mode never owns the backend: it is still serving the fixture afterwards.
	var ping := await _fixture.request("session.ping", {})
	assert_str(ping.type).is_equal("res")


func test_attach_without_a_url_picks_the_newest_live_record() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	Backend.start({"attach": true})
	assert_bool(await _await_state(Backend.State.READY)).is_true()
	assert_int(Backend.debug_backend_pid()).is_equal(-1)


const _REFUSED_MESSAGE := "Backend: refusing to attach: the URL must be ws://127.0.0.1:<port>"


## Every shape that is not exactly `ws://127.0.0.1:<port>` is refused before any record is read, so the token
## cannot be steered to another host (userinfo tricks) or to a default port (missing port).
func test_attach_refuses_every_url_that_is_not_plain_loopback_with_a_port() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	var port := int(_fixture.backend_url.get_slice(":", 2))
	var refused: Array[String] = [
		"ws://192.0.2.1:9",
		"ws://127.0.0.1:%d@attacker.example:443" % port,
		"ws://user:pw@127.0.0.1:%d" % port,
		"ws://127.0.0.1",
		"ws://127.0.0.1/",
		"ws://localhost:%d" % port,
		"ws://[::1]:%d" % port,
		"ws://127.0.0.1:%d/path" % port,
		"ws://127.0.0.1:%d/?x=1" % port,
		"ws://127.0.0.1:%d?x=1" % port,
		"ws://127.0.0.1:%d#frag" % port,
		"ws://127.0.0.1:%d:1" % port,
		"wss://127.0.0.1:%d" % port,
		"http://127.0.0.1:%d" % port,
		"127.0.0.1:%d" % port,
		"ws://127.0.0.1:0",
		"ws://127.0.0.1:99999",
	]
	for url in refused:
		await assert_error(func() -> void: Backend.start({"backend_url": url})).is_push_error(_REFUSED_MESSAGE)
		assert_int(Backend.state()).is_equal(Backend.State.FAILED)


func test_stopping_a_spawned_backend_removes_its_discovery_record() -> void:
	_fixture = await BackendFixture.start()
	assert_object(_fixture).is_not_null()
	var port := int(_fixture.backend_url.get_slice(":", 2))
	var record := IpcDiscovery.find(port)
	assert_bool(record.is_empty()).is_false()
	var pid := int(record["pid"])
	assert_int(_fixture.backend_pid()).is_equal(pid)
	var record_path := IpcDiscovery.runtime_dir().path_join("backend-%d.json" % pid)
	assert_bool(FileAccess.file_exists(record_path)).is_true()

	_fixture.stop()
	_fixture = null
	await get_tree().create_timer(0.5).timeout
	assert_bool(FileAccess.file_exists(record_path)).is_false()


func test_attach_waits_when_no_backend_has_that_port() -> void:
	await assert_error(
		func() -> void: Backend.start({"attach": true, "backend_url": "ws://127.0.0.1:1"})
	).is_push_warning(
		"Backend: no running backend found to attach to (waiting for its discovery record)"
	)
	assert_int(Backend.state()).is_equal(Backend.State.RECONNECTING)
