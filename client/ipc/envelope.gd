class_name IpcEnvelope
extends RefCounted
## Binary frame codec for the localhost WebSocket IPC (ADR 0003):
## [code][u32 LE header_len][UTF-8 JSON header][blob 0][blob 1]…[/code]
## Mirrors `backend/src/coypu_builder/protocol/messages.py` and `server/codec.py` — this is the only
## place on the client that knows the wire shape.

const PROTOCOL_VERSION := 1  # must match coypu_builder.PROTOCOL_VERSION
const _HEADER_LEN_BYTES := 4

var v: int
var id: String
var type: String
var method: String
var params: Dictionary
var result: Dictionary
var error: Dictionary
## Decoded blob values by name: PackedFloat32Array / PackedFloat64Array / PackedInt32Array for a 1-D
## blob, PackedVector3Array for a float32 blob shaped (n, 3); anything else comes back as raw
## PackedByteArray so callers never crash on an unrecognised dtype.
var blobs: Dictionary


static func encode(id: String, type: String, method: String = "", params: Dictionary = {}) -> PackedByteArray:
	var header := {"v": PROTOCOL_VERSION, "id": id, "type": type, "method": method, "params": params}
	var header_bytes := JSON.stringify(header).to_utf8_buffer()
	var frame := PackedByteArray()
	frame.resize(_HEADER_LEN_BYTES)
	frame.encode_u32(0, header_bytes.size())
	frame.append_array(header_bytes)
	return frame


static func decode(frame: PackedByteArray) -> IpcEnvelope:
	if frame.size() < _HEADER_LEN_BYTES:
		push_error("IPC: frame shorter than the header-length prefix (%d bytes)" % frame.size())
		return null
	var header_len := frame.decode_u32(0)
	var header_bytes := frame.slice(_HEADER_LEN_BYTES, _HEADER_LEN_BYTES + header_len)
	var parsed = JSON.parse_string(header_bytes.get_string_from_utf8())
	if typeof(parsed) != TYPE_DICTIONARY:
		push_error("IPC: frame header is not a JSON object")
		return null

	var envelope := IpcEnvelope.new()
	envelope.v = parsed.get("v", 0)
	envelope.id = parsed.get("id", "")
	envelope.type = parsed.get("type", "")
	envelope.method = parsed.get("method", "")
	envelope.params = parsed.get("params", {})
	envelope.result = parsed.get("result", {})
	envelope.error = parsed.get("error", {})

	var tail := frame.slice(_HEADER_LEN_BYTES + header_len, frame.size())
	envelope.blobs = _decode_blobs(parsed.get("blobs", []), tail)
	return envelope


static func _decode_blobs(blob_refs: Array, tail: PackedByteArray) -> Dictionary:
	var out := {}
	for blob_ref in blob_refs:
		var offset: int = blob_ref["offset"]
		var length: int = blob_ref["length"]
		var raw := tail.slice(offset, offset + length)
		out[blob_ref["name"]] = _decode_array(raw, blob_ref["dtype"], blob_ref["shape"])
	return out


static func _decode_array(raw: PackedByteArray, dtype: String, shape: Array) -> Variant:
	var cols: int = shape[1] if shape.size() > 1 else 1
	match dtype:
		"<f4":
			var flat := raw.to_float32_array()
			if cols != 3:
				return flat
			var vectors := PackedVector3Array()
			vectors.resize(flat.size() / 3)
			for i in vectors.size():
				vectors[i] = Vector3(flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2])
			return vectors
		"<f8":
			return raw.to_float64_array()
		"<i4":
			return raw.to_int32_array()
		_:
			push_warning("IPC: unsupported blob dtype '%s'; returning raw bytes" % dtype)
			return raw
