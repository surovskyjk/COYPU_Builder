class_name IpcWebSocketClient
extends Node
## Owns one [WebSocketPeer] connection to the backend and turns wire frames into [IpcEnvelope] signals
## (ADR 0003). Polls the peer every frame; call [method connect_to_url] to start.

signal connected
signal disconnected(code: int, reason: String)
signal envelope_received(envelope: IpcEnvelope)

## A frame table blob tail can run well past WebSocketPeer's 64 KiB default for a long or densely
## sampled corridor (ADR 0003); 16 MiB comfortably covers Phase 0/1 corridors.
const INBOUND_BUFFER_SIZE := 16 * 1024 * 1024

var _peer := WebSocketPeer.new()
var _was_open := false
var _next_request_id := 0


## A [WebSocketPeer] refuses to [method WebSocketPeer.connect_to_url] again until it reaches
## `STATE_CLOSED`, which a reconnect after a backend crash cannot guarantee (ADR 0003's restart-on-loss
## path calls this the moment a new backend is ready, with no wait for the old peer to finish closing).
## Constructing a fresh peer sidesteps that entirely — the old one is simply dropped and collected — but
## `_was_open` must be reset here too, or a stale `true` from the discarded peer either fires a spurious
## `disconnected` for a peer nobody is polling anymore, or (worse) suppresses the real `connected` signal
## for the new one.
func connect_to_url(url: String) -> Error:
	_peer = WebSocketPeer.new()
	_was_open = false
	_peer.inbound_buffer_size = INBOUND_BUFFER_SIZE
	var err := _peer.connect_to_url(url)
	if err != OK:
		push_error("IPC: connect_to_url(%s) failed: %s" % [url, error_string(err)])
	return err


func close(code: int = 1000, reason: String = "") -> void:
	_peer.close(code, reason)
	_was_open = false


func is_open() -> bool:
	return _peer.get_ready_state() == WebSocketPeer.STATE_OPEN


func next_request_id() -> String:
	_next_request_id += 1
	return str(_next_request_id)


## Encodes and sends a `req` envelope; returns the request id so the caller can match the `res`/`err`
## reply from [signal envelope_received].
func send_request(method: String, params: Dictionary = {}) -> String:
	var id := next_request_id()
	var err := _peer.send(IpcEnvelope.encode(id, "req", method, params), WebSocketPeer.WRITE_MODE_BINARY)
	if err != OK:
		push_error("IPC: send(%s) failed: %s" % [method, error_string(err)])
	return id


func _process(_delta: float) -> void:
	_peer.poll()
	var state := _peer.get_ready_state()
	if state == WebSocketPeer.STATE_OPEN:
		if not _was_open:
			_was_open = true
			connected.emit()
		while _peer.get_available_packet_count() > 0:
			var envelope := IpcEnvelope.decode(_peer.get_packet())
			if envelope != null:
				envelope_received.emit(envelope)
	elif state == WebSocketPeer.STATE_CLOSED and _was_open:
		_was_open = false
		disconnected.emit(_peer.get_close_code(), _peer.get_close_reason())
