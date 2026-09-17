class_name BackendFixture
extends RefCounted
## Spawns a real backend for integration tests (T-102): locates `uv`, starts it through
## [IpcProcessSupervisor], awaits the port line, connects an [IpcWebSocketClient] and completes
## `session.hello`. [method stop] always kills the process tree, so a caller that invokes it from
## `after_test()` cannot leak a backend process even when the test that called [method start] or
## [method request] failed or timed out first.

const TOKEN := "gdunit-tests"

const _CONNECT_TIMEOUT_SEC := 10.0
const _REQUEST_TIMEOUT_SEC := 10.0
const _POLL_STEP_SEC := 0.02

## The `result` dict of the `session.hello` reply `start()` completes internally, kept around so
## callers can assert on it (e.g. `protocol_version`) without `start()` having to hand back the
## envelope itself.
var hello_result: Dictionary

var _supervisor: IpcProcessSupervisor
var _client: IpcWebSocketClient
var _tree: SceneTree


## Returns null when `uv` cannot be located, the backend fails to start, the WebSocket fails to
## connect, or `session.hello` does not succeed — in every case the process tree is already killed
## by the time this returns.
static func start() -> BackendFixture:
	var uv := UvLocator.find_uv()
	if uv.is_empty():
		return null

	var fixture := BackendFixture.new()
	fixture._tree = Engine.get_main_loop() as SceneTree

	var backend_dir := ProjectSettings.globalize_path("res://").path_join("../backend")
	fixture._supervisor = IpcProcessSupervisor.new()
	fixture._tree.root.add_child(fixture._supervisor)

	var ready_url := await fixture._await_backend_ready(uv, backend_dir)
	if ready_url.is_empty():
		fixture.stop()
		return null

	fixture._client = IpcWebSocketClient.new()
	fixture._tree.root.add_child(fixture._client)
	fixture._client.connect_to_url(ready_url)

	var connected := await fixture._await_signal_bool(fixture._client.connected, _CONNECT_TIMEOUT_SEC)
	if not connected:
		fixture.stop()
		return null

	var hello := await fixture.request(
		"session.hello", {"client": "gdunit-tests", "client_version": "0", "token": TOKEN}
	)
	if hello.type != "res":
		fixture.stop()
		return null
	fixture.hello_result = hello.result

	return fixture


func client() -> IpcWebSocketClient:
	return _client


## Sends a request and waits (with a timeout) for the matching envelope; on timeout returns a
## synthetic `err` envelope rather than blocking the suite forever or crashing it.
func request(method: String, params: Dictionary) -> IpcEnvelope:
	var request_id := _client.send_request(method, params)
	var reply_box: Array = [null]
	var on_envelope := func(envelope: IpcEnvelope) -> void:
		if envelope.id == request_id:
			reply_box[0] = envelope
	_client.envelope_received.connect(on_envelope)

	var elapsed := 0.0
	while reply_box[0] == null and elapsed < _REQUEST_TIMEOUT_SEC:
		await _tree.create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	if _client.envelope_received.is_connected(on_envelope):
		_client.envelope_received.disconnect(on_envelope)

	if reply_box[0] != null:
		return reply_box[0]

	var timeout_envelope := IpcEnvelope.new()
	timeout_envelope.id = request_id
	timeout_envelope.type = "err"
	timeout_envelope.method = method
	timeout_envelope.error = {
		"code": "E_CLIENT_TIMEOUT",
		"message": "no reply to '%s' within %.1fs" % [method, _REQUEST_TIMEOUT_SEC],
	}
	timeout_envelope.blobs = {}
	return timeout_envelope


## Kills the backend process tree (if running) and frees the client/supervisor nodes. Safe to call
## more than once and safe to call when [method start] never completed.
func stop() -> void:
	if _client != null:
		_client.close()
		_client.queue_free()
		_client = null
	if _supervisor != null:
		_supervisor.stop()
		_supervisor.queue_free()
		_supervisor = null


func _await_backend_ready(uv: String, backend_dir: String) -> String:
	var args := PackedStringArray(["run", "--project", backend_dir, "coypu-builder-backend", "serve"])

	var url_box: Array = [""]
	var failed_box: Array = [false]
	var on_ready := func(u: String) -> void: url_box[0] = u
	var on_failed := func(_reason: String) -> void: failed_box[0] = true
	_supervisor.backend_ready.connect(on_ready)
	_supervisor.backend_failed.connect(on_failed)

	_supervisor.start(uv, args, TOKEN)

	var elapsed := 0.0
	while url_box[0].is_empty() and not failed_box[0] and elapsed < _CONNECT_TIMEOUT_SEC:
		await _tree.create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC

	_supervisor.backend_ready.disconnect(on_ready)
	_supervisor.backend_failed.disconnect(on_failed)
	return url_box[0]


## Waits (with a timeout) for a one-shot boolean signal such as [signal IpcWebSocketClient.connected].
func _await_signal_bool(sig: Signal, timeout_sec: float) -> bool:
	var fired_box: Array = [false]
	var on_signal := func(_a: Variant = null, _b: Variant = null) -> void: fired_box[0] = true
	sig.connect(on_signal, CONNECT_ONE_SHOT)

	var elapsed := 0.0
	while not fired_box[0] and elapsed < timeout_sec:
		await _tree.create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	if sig.is_connected(on_signal):
		sig.disconnect(on_signal)
	return fired_box[0]
