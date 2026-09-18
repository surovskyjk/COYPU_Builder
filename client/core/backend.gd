extends Node
## Autoload `Backend`: owns one [IpcProcessSupervisor] and one [IpcWebSocketClient] and is the only
## object in the client that talks to them (ADR 0003).
##
## Lifecycle: [method start] spawns (or attaches, when `--backend-url` was given) -> connects ->
## `session.hello` -> READY. A [Timer] heartbeats `session.ping` every [constant HEARTBEAT_INTERVAL_SEC]
## once READY; three consecutive misses, a socket close or a supervisor crash all mean the connection is
## lost. Reconnection backs off 1s, 2s, 4s, 8s, 10s, then FAILED — except in attach mode, which retries
## forever at the last step, since the developer may be restarting the backend under a debugger.

enum State { DISCONNECTED, SPAWNING, CONNECTING, HANDSHAKING, READY, RECONNECTING, FAILED }

signal state_changed(state: State)
signal event_received(envelope: IpcEnvelope)       # server-push 'evt' envelopes

const HEARTBEAT_INTERVAL_SEC := 2.0                # ADR 0003
const HEARTBEAT_MISS_LIMIT := 3
const REQUEST_TIMEOUT_SEC := 30.0

const _CONNECT_TIMEOUT_SEC := 10.0
const _POLL_STEP_SEC := 0.02
const _RECONNECT_BACKOFF_SEC: Array[float] = [1.0, 2.0, 4.0, 8.0, 10.0]
const _CLIENT_NAME := "coypu-builder-client"
const _CLIENT_VERSION := "0.1.0"

var _state: State = State.DISCONNECTED
var _attach_mode := false
var _attach_url := ""
var _token := ""
var _shutting_down := false
var _reconnect_attempt := 0

var _server_version := ""
var _protocol_version := 0

var _client: IpcWebSocketClient
var _supervisor: IpcProcessSupervisor

var _heartbeat_timer: Timer
var _heartbeat_misses := 0
var _heartbeat_in_flight := false
var _connect_timeout_timer: Timer
var _reconnect_timer: Timer

## request_id (String) -> {"resolved": bool, "envelope": IpcEnvelope}. Polled by [method request]'s
## await loop and resolved early by [method _fail_all_pending] on disconnect.
var _pending_requests := {}


func _ready() -> void:
	_client = IpcWebSocketClient.new()
	add_child(_client)
	_client.connected.connect(_on_client_connected)
	_client.disconnected.connect(_on_client_disconnected)
	_client.envelope_received.connect(_on_envelope_received)

	_heartbeat_timer = Timer.new()
	_heartbeat_timer.wait_time = HEARTBEAT_INTERVAL_SEC
	add_child(_heartbeat_timer)
	_heartbeat_timer.timeout.connect(_on_heartbeat_timeout)

	_connect_timeout_timer = Timer.new()
	_connect_timeout_timer.one_shot = true
	_connect_timeout_timer.wait_time = _CONNECT_TIMEOUT_SEC
	add_child(_connect_timeout_timer)
	_connect_timeout_timer.timeout.connect(_on_connect_timeout)

	_reconnect_timer = Timer.new()
	_reconnect_timer.one_shot = true
	add_child(_reconnect_timer)
	_reconnect_timer.timeout.connect(_on_reconnect_timeout)


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		shutdown()


func state() -> State:
	return _state


func server_version() -> String:
	return _server_version


func protocol_version() -> int:
	return _protocol_version


## Test-only hook (`test_connection_lifecycle.gd`): the spawned backend process id, or -1 if none is
## running (attach mode, or spawn mode before/after the process is alive).
func debug_backend_pid() -> int:
	return _supervisor.pid() if _supervisor != null else -1


## Test-only hook (`test_connection_lifecycle.gd`): kills the spawned backend process the same way an
## external crash would, exercising the real crash-recovery path. No-op in attach mode.
func debug_kill_backend_process() -> void:
	if _supervisor != null:
		_supervisor.debug_kill()


func start() -> void:
	if _state != State.DISCONNECTED and _state != State.FAILED:
		return
	_shutting_down = false
	_reconnect_attempt = 0
	var args := CliArgs.from_cmdline()
	_attach_url = args.get("backend_url", "")
	_token = args.get("backend_token", "")
	_attach_mode = not _attach_url.is_empty()
	if not _attach_mode and _token.is_empty():
		# We choose the token for our own spawned process either way; an empty command-line argument is
		# worth avoiding regardless of its cause, so pick a random one instead of "" when none was given
		# (e.g. a bare `godot --path client` launch with no `--backend-token`).
		_token = "%d-%d" % [Time.get_ticks_usec(), randi()]
	if _attach_mode:
		_connect_to(_attach_url)
	else:
		_spawn_and_connect()


func shutdown() -> void:
	_shutting_down = true
	_heartbeat_timer.stop()
	_connect_timeout_timer.stop()
	_reconnect_timer.stop()
	_fail_all_pending()
	_client.close()
	if _supervisor != null:
		_supervisor.stop()
	_set_state(State.DISCONNECTED)


## Coroutine. Awaits the matching 'res'/'err' envelope. On timeout or a dead connection it returns a
## locally synthesised 'err' envelope with code "E_TIMEOUT" / "E_DISCONNECTED" so that callers have
## exactly one failure shape to handle and never await forever.
func request(method: String, params: Dictionary = {}) -> IpcEnvelope:
	if not _client.is_open():
		return _error_envelope("", method, "E_DISCONNECTED", "not connected to backend")

	var request_id := _client.send_request(method, params)
	var pending := {"resolved": false, "envelope": null}
	_pending_requests[request_id] = pending

	var elapsed := 0.0
	while not bool(pending["resolved"]) and elapsed < REQUEST_TIMEOUT_SEC:
		await get_tree().create_timer(_POLL_STEP_SEC).timeout
		elapsed += _POLL_STEP_SEC
	_pending_requests.erase(request_id)

	if pending["resolved"]:
		return pending["envelope"]
	return _error_envelope(
		request_id, method, "E_TIMEOUT", "no reply to '%s' within %.1fs" % [method, REQUEST_TIMEOUT_SEC]
	)


func _error_envelope(id: String, method: String, code: String, message: String) -> IpcEnvelope:
	var envelope := IpcEnvelope.new()
	envelope.id = id
	envelope.type = "err"
	envelope.method = method
	envelope.error = {"code": code, "message": message}
	envelope.blobs = {}
	return envelope


func _on_envelope_received(envelope: IpcEnvelope) -> void:
	if envelope.type == "evt":
		event_received.emit(envelope)
		return
	if _pending_requests.has(envelope.id):
		var pending: Dictionary = _pending_requests[envelope.id]
		pending["resolved"] = true
		pending["envelope"] = envelope


func _spawn_and_connect() -> void:
	_set_state(State.SPAWNING)
	if _supervisor == null:
		_supervisor = IpcProcessSupervisor.new()
		add_child(_supervisor)
		_supervisor.backend_ready.connect(_on_backend_ready)
		_supervisor.backend_failed.connect(_on_backend_failed)
		_supervisor.backend_crashed.connect(_on_backend_crashed)

	var uv := _locate_uv()
	if uv.is_empty():
		push_error("Backend: could not locate the 'uv' executable")
		_handle_connection_lost()
		return

	var backend_dir := ProjectSettings.globalize_path("res://").path_join("../backend")
	var args := PackedStringArray(["run", "--project", backend_dir, "coypu-builder-backend", "serve"])
	_supervisor.start(uv, args, _token)


func _on_backend_ready(url: String) -> void:
	_connect_to(url)


func _on_backend_failed(reason: String) -> void:
	push_error("Backend: %s" % reason)
	EventBus.backend_error.emit("E_SPAWN_FAILED", reason)
	_handle_connection_lost()


func _on_backend_crashed() -> void:
	if _shutting_down:
		return
	_handle_connection_lost()


func _connect_to(url: String) -> void:
	_set_state(State.CONNECTING)
	var err := _client.connect_to_url(url)
	if err != OK:
		_handle_connection_lost()
		return
	_connect_timeout_timer.start()


func _on_client_connected() -> void:
	_connect_timeout_timer.stop()
	_set_state(State.HANDSHAKING)
	_do_handshake()


func _on_connect_timeout() -> void:
	if _state != State.CONNECTING:
		return
	_client.close()
	_handle_connection_lost()


func _on_client_disconnected(_code: int, _reason: String) -> void:
	if _shutting_down:
		return
	_handle_connection_lost()


func _do_handshake() -> void:
	var hello := await request(
		"session.hello", {"client": _CLIENT_NAME, "client_version": _CLIENT_VERSION, "token": _token}
	)
	if _shutting_down:
		return
	if hello.type != "res":
		push_error("Backend: session.hello failed: %s" % str(hello.error))
		EventBus.backend_error.emit(hello.error.get("code", "E_INTERNAL"), hello.error.get("message", ""))
		_client.close()
		_handle_connection_lost()
		return

	_server_version = hello.result.get("server_version", "")
	_protocol_version = int(hello.result.get("protocol_version", 0))
	_reconnect_attempt = 0
	_heartbeat_misses = 0
	_set_state(State.READY)
	_heartbeat_timer.start()


func _on_heartbeat_timeout() -> void:
	if _state != State.READY or _heartbeat_in_flight:
		return
	_heartbeat_in_flight = true
	var reply := await request("session.ping", {"client_time_ms": Time.get_ticks_msec()})
	_heartbeat_in_flight = false
	if _state != State.READY:
		return  # connection already handled elsewhere while we were awaiting the reply
	if reply.type == "res":
		_heartbeat_misses = 0
		return
	_heartbeat_misses += 1
	if _heartbeat_misses >= HEARTBEAT_MISS_LIMIT:
		_handle_connection_lost()


func _handle_connection_lost() -> void:
	# A single real failure can be reported twice (the socket closing and the supervisor's crash
	# watchdog both fire for the same dead process); once we're already retrying, a second report of
	# the same event must not double-count against the backoff/failure budget.
	if _shutting_down or _state == State.FAILED or _state == State.RECONNECTING:
		return
	_heartbeat_timer.stop()
	_connect_timeout_timer.stop()
	_fail_all_pending()

	if not _attach_mode and _supervisor != null:
		_supervisor.stop()

	if not _attach_mode and _reconnect_attempt >= _RECONNECT_BACKOFF_SEC.size():
		_set_state(State.FAILED)
		return

	_set_state(State.RECONNECTING)
	var idx: int = min(_reconnect_attempt, _RECONNECT_BACKOFF_SEC.size() - 1)
	_reconnect_attempt += 1
	_reconnect_timer.wait_time = _RECONNECT_BACKOFF_SEC[idx]
	_reconnect_timer.start()


func _on_reconnect_timeout() -> void:
	if _shutting_down:
		return
	if _attach_mode:
		_connect_to(_attach_url)
	else:
		_spawn_and_connect()


func _fail_all_pending() -> void:
	for id: String in _pending_requests.keys():
		var pending: Dictionary = _pending_requests[id]
		if not pending["resolved"]:
			pending["resolved"] = true
			pending["envelope"] = _error_envelope(id, "", "E_DISCONNECTED", "connection lost")


func _set_state(new_state: State) -> void:
	if _state == new_state:
		return
	_state = new_state
	state_changed.emit(new_state)
	EventBus.backend_state_changed.emit(new_state)


## Duplicated from `client/tests/helpers/uv_locator.gd` (test-only, must not be depended on by
## production code) rather than shared, since Deliverables does not include promoting it out of tests/.
func _locate_uv() -> String:
	var probe_cmd := "where" if OS.get_name() == "Windows" else "which"
	var output := []
	if OS.execute(probe_cmd, ["uv"], output) == OK:
		for chunk in output:
			for candidate in String(chunk).split("\n"):
				candidate = candidate.strip_edges()
				if candidate != "" and FileAccess.file_exists(candidate):
					return candidate

	var local_app_data := OS.get_environment("LOCALAPPDATA")
	if local_app_data == "":
		return ""

	var links_path := local_app_data.path_join("Microsoft/WinGet/Links/uv.exe")
	if FileAccess.file_exists(links_path):
		return links_path

	var packages_dir := local_app_data.path_join("Microsoft/WinGet/Packages")
	var dir := DirAccess.open(packages_dir)
	if dir == null:
		return ""
	dir.list_dir_begin()
	var entry := dir.get_next()
	while entry != "":
		if dir.current_is_dir() and entry.begins_with("astral-sh.uv_"):
			var candidate := packages_dir.path_join(entry).path_join("uv.exe")
			if FileAccess.file_exists(candidate):
				dir.list_dir_end()
				return candidate
		entry = dir.get_next()
	dir.list_dir_end()
	return ""
