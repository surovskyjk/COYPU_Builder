extends SceneTree
## Standalone IPC test runner (no gdUnit4 dependency yet — see client/README.md `tests/`).
## Run headless: `Godot --path client --headless --script res://tests/run_ipc_tests.gd`
## Exits 0 if every assertion passed, 1 otherwise.

const CONNECT_TIMEOUT_SEC := 10.0
const REQUEST_TIMEOUT_SEC := 10.0
const BACKEND_TOKEN := "gdscript-tests"

var _failures: PackedStringArray = []
var _pass_count := 0


func _initialize() -> void:
	print("=== envelope codec unit tests ===")
	_run_codec_unit_tests()
	print("=== live backend round-trip ===")
	await _run_backend_round_trip()
	_finish()


func _check(condition: bool, message: String) -> void:
	if condition:
		_pass_count += 1
	else:
		_failures.append(message)
		printerr("FAIL: ", message)


func _finish() -> void:
	print("--- %d passed, %d failed ---" % [_pass_count, _failures.size()])
	quit(1 if not _failures.is_empty() else 0)


# --- unit: envelope encode/decode, no backend involved -----------------------------------------------


func _run_codec_unit_tests() -> void:
	var request := IpcEnvelope.encode("42", "req", "session.hello", {"client": "gdscript-tests"})
	var decoded_request := IpcEnvelope.decode(request)
	_check(decoded_request != null, "request envelope decodes")
	_check(decoded_request.id == "42", "request id round-trips")
	_check(decoded_request.type == "req", "request type round-trips")
	_check(decoded_request.method == "session.hello", "request method round-trips")
	_check(decoded_request.params.get("client") == "gdscript-tests", "request params round-trip")

	# A synthetic `res` frame built the same way server/codec.py's pack_blobs() lays out the tail:
	# blob byte ranges concatenated in order, each described by a {name, dtype, shape, offset, length}.
	var station := PackedFloat32Array([0.0, 10.0, 20.0])
	var position := PackedFloat32Array([0.0, 0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
	var segment_index := PackedInt32Array([0, 0, 1])
	var station_bytes := station.to_byte_array()
	var position_bytes := position.to_byte_array()
	var segment_bytes := segment_index.to_byte_array()

	var header := {
		"v": 1,
		"id": "7",
		"type": "res",
		"method": "alignment.frame_table",
		"result": {"row_count": 3},
		"blobs": [
			{"name": "station", "dtype": "<f4", "shape": [3], "offset": 0, "length": station_bytes.size()},
			{
				"name": "position",
				"dtype": "<f4",
				"shape": [3, 3],
				"offset": station_bytes.size(),
				"length": position_bytes.size(),
			},
			{
				"name": "segment_index",
				"dtype": "<i4",
				"shape": [3],
				"offset": station_bytes.size() + position_bytes.size(),
				"length": segment_bytes.size(),
			},
		],
	}
	var header_bytes := JSON.stringify(header).to_utf8_buffer()
	var frame := PackedByteArray()
	frame.resize(4)
	frame.encode_u32(0, header_bytes.size())
	frame.append_array(header_bytes)
	frame.append_array(station_bytes)
	frame.append_array(position_bytes)
	frame.append_array(segment_bytes)

	var response := IpcEnvelope.decode(frame)
	_check(response != null, "response envelope decodes")
	_check(response.result.get("row_count") == 3, "response result round-trips")

	var decoded_station: PackedFloat32Array = response.blobs.get("station")
	_check(decoded_station == station, "station blob decodes to the source PackedFloat32Array")

	var decoded_position: PackedVector3Array = response.blobs.get("position")
	_check(decoded_position.size() == 3, "position blob decodes to 3 Vector3s")
	_check(decoded_position[0] == Vector3.ZERO, "position blob row 0 round-trips")
	_check(decoded_position[1] == Vector3(1, 2, 3), "position blob row 1 round-trips")
	_check(decoded_position[2] == Vector3(4, 5, 6), "position blob row 2 round-trips")

	var decoded_segments: PackedInt32Array = response.blobs.get("segment_index")
	_check(decoded_segments == segment_index, "segment_index blob decodes to the source PackedInt32Array")


# --- integration: spawn the real backend and drive it over a real WebSocket --------------------------


func _run_backend_round_trip() -> void:
	var uv := _find_uv()
	if uv.is_empty():
		_check(false, "could not locate the 'uv' executable to spawn the backend")
		return

	var backend_dir := ProjectSettings.globalize_path("res://").path_join("../backend")
	var fixture := ProjectSettings.globalize_path("res://").path_join(
		"../backend/tests/fixtures/kralupy/kralupy_neratovice_092.xml"
	)

	var supervisor := IpcProcessSupervisor.new()
	root.add_child(supervisor)
	var ready_url := await _await_backend_ready(supervisor, uv, backend_dir)
	if ready_url.is_empty():
		supervisor.queue_free()
		return

	var client := IpcWebSocketClient.new()
	root.add_child(client)
	client.connect_to_url(ready_url)
	var connected_ok: bool = await _await_signal_bool(client.connected, CONNECT_TIMEOUT_SEC)
	_check(connected_ok, "client connects to the backend WebSocket")

	if connected_ok:
		await _drive_protocol(client, fixture)

	supervisor.stop()
	client.close()
	client.queue_free()
	supervisor.queue_free()


## GDScript lambdas capture locals by value, so waiting on a signal has to report back through a
## boxed (single-element Array) upvalue rather than by assigning a plain local from the callback.
func _await_backend_ready(supervisor: IpcProcessSupervisor, uv: String, backend_dir: String) -> String:
	var args := PackedStringArray(["run", "--project", backend_dir, "coypu-builder-backend", "serve"])
	supervisor.start(uv, args, BACKEND_TOKEN)

	var url_box := [""]
	var failed_box := [false]
	var reason_box := [""]
	var on_ready := func(u: String) -> void: url_box[0] = u
	var on_failed := func(r: String) -> void:
		failed_box[0] = true
		reason_box[0] = r
	supervisor.backend_ready.connect(on_ready)
	supervisor.backend_failed.connect(on_failed)

	var elapsed := 0.0
	var step := 0.05
	while url_box[0].is_empty() and not failed_box[0] and elapsed < CONNECT_TIMEOUT_SEC:
		await create_timer(step).timeout
		elapsed += step

	supervisor.backend_ready.disconnect(on_ready)
	supervisor.backend_failed.disconnect(on_failed)

	_check(not url_box[0].is_empty(), "backend process starts and reports its port")
	if failed_box[0]:
		_check(false, "backend process did not fail: %s" % reason_box[0])
	return url_box[0]


func _await_signal_bool(sig: Signal, timeout_sec: float) -> bool:
	var fired_box := [false]
	var on_signal := func(_a = null, _b = null) -> void: fired_box[0] = true
	sig.connect(on_signal, CONNECT_ONE_SHOT)
	var elapsed := 0.0
	var step := 0.05
	while not fired_box[0] and elapsed < timeout_sec:
		await create_timer(step).timeout
		elapsed += step
	if sig.is_connected(on_signal):
		sig.disconnect(on_signal)
	return fired_box[0]


func _drive_protocol(client: IpcWebSocketClient, fixture_path: String) -> void:
	var hello := await _request(
		client, "session.hello", {"client": "gdscript-tests", "client_version": "0", "token": BACKEND_TOKEN}
	)
	_check(hello != null and hello.type == "res", "session.hello succeeds")
	if hello == null or hello.type != "res":
		return
	_check(hello.result.get("protocol_version") == IpcEnvelope.PROTOCOL_VERSION, "protocol version matches")

	var project := await _request(client, "project.new", {})
	_check(project != null and project.type == "res", "project.new succeeds")

	var imported := await _request(client, "import.landxml", {"path": fixture_path})
	_check(imported != null and imported.type == "res", "import.landxml succeeds")
	if imported == null or imported.type != "res":
		return
	var alignments: Array = imported.result.get("alignments", [])
	_check(alignments.size() == 1, "import.landxml returns exactly one alignment")
	if alignments.is_empty():
		return
	var alignment_id: String = alignments[0]["alignment_id"]

	var frame_table := await _request(
		client, "alignment.frame_table", {"alignment_id": alignment_id, "spacing_m": 25.0}
	)
	_check(frame_table != null and frame_table.type == "res", "alignment.frame_table succeeds")
	if frame_table == null or frame_table.type != "res":
		return
	var row_count: int = frame_table.result.get("row_count", 0)
	_check(row_count > 0, "frame table has at least one row")

	var station: PackedFloat32Array = frame_table.blobs.get("station", PackedFloat32Array())
	var position: PackedVector3Array = frame_table.blobs.get("position", PackedVector3Array())
	var rotation: PackedFloat32Array = frame_table.blobs.get("rotation", PackedFloat32Array())
	_check(station.size() == row_count, "station blob has row_count entries")
	_check(position.size() == row_count, "position blob decodes to row_count Vector3s")
	_check(rotation.size() == row_count * 4, "rotation blob decodes to row_count quaternions")

	var missing := await _request(client, "alignment.frame_table", {"alignment_id": "no-such-id"})
	_check(missing != null and missing.type == "err", "unknown alignment_id yields an err envelope")
	var missing_code = missing.error.get("code") if missing != null else null
	_check(missing_code == "E_NOT_FOUND", "unknown alignment_id yields E_NOT_FOUND")


## Sends a request and waits (with a timeout) for the envelope whose id matches it.
func _request(client: IpcWebSocketClient, method: String, params: Dictionary) -> IpcEnvelope:
	var request_id := client.send_request(method, params)
	var reply_box := [null]
	var on_envelope := func(envelope: IpcEnvelope) -> void:
		if envelope.id == request_id:
			reply_box[0] = envelope
	client.envelope_received.connect(on_envelope)
	var elapsed := 0.0
	var step := 0.02
	while reply_box[0] == null and elapsed < REQUEST_TIMEOUT_SEC:
		await create_timer(step).timeout
		elapsed += step
	client.envelope_received.disconnect(on_envelope)
	if reply_box[0] == null:
		_check(false, "'%s' replied within %.1fs" % [method, REQUEST_TIMEOUT_SEC])
	return reply_box[0]


func _find_uv() -> String:
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
