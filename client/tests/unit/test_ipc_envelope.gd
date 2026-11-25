extends GdUnitTestSuite
## Envelope codec unit tests (T-102): ported from the old `run_ipc_tests.gd` codec section, one-to-one
## with its assertions. No backend, no network — must stay fast enough for `-a tests/unit`.

const CLIENT_NAME := "gdscript-tests"


func test_request_envelope_round_trips() -> void:
	var request := IpcEnvelope.encode("42", "req", "session.hello", {"client": CLIENT_NAME})
	var decoded := IpcEnvelope.decode(request)

	assert_that(decoded).is_not_null()
	assert_str(decoded.id).is_equal("42")
	assert_str(decoded.type).is_equal("req")
	assert_str(decoded.method).is_equal("session.hello")
	assert_str(decoded.params.get("client")).is_equal(CLIENT_NAME)


## A synthetic `res` frame built the same way server/codec.py's pack_blobs() lays out the tail: blob
## byte ranges concatenated in order, each described by a {name, dtype, shape, offset, length}.
func _build_frame_table_response() -> PackedByteArray:
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
	return frame


func test_response_envelope_decodes_result_and_blobs() -> void:
	var response := IpcEnvelope.decode(_build_frame_table_response())
	assert_that(response).is_not_null()

	# The header round-trips through JSON, so a plain int in the source Dictionary comes back as a
	# float (Godot's JSON always decodes numbers as float) — coerce via a typed local, same as the
	# real protocol responses the backend sends.
	var row_count: int = response.result.get("row_count", 0)
	assert_int(row_count).is_equal(3)


func test_float32_1d_blob_decodes_to_source_array() -> void:
	var response := IpcEnvelope.decode(_build_frame_table_response())
	var station: PackedFloat32Array = response.blobs.get("station")

	assert_that(station).is_equal(PackedFloat32Array([0.0, 10.0, 20.0]))


func test_float32_n3_blob_decodes_to_vector3_array() -> void:
	var response := IpcEnvelope.decode(_build_frame_table_response())
	var position: PackedVector3Array = response.blobs.get("position")

	assert_int(position.size()).is_equal(3)
	assert_vector(position[0]).is_equal(Vector3.ZERO)
	assert_vector(position[1]).is_equal(Vector3(1, 2, 3))
	assert_vector(position[2]).is_equal(Vector3(4, 5, 6))


func test_int32_blob_decodes_to_source_array() -> void:
	var response := IpcEnvelope.decode(_build_frame_table_response())
	var segment_index: PackedInt32Array = response.blobs.get("segment_index")

	assert_that(segment_index).is_equal(PackedInt32Array([0, 0, 1]))


func test_uint8_blob_decodes_to_its_raw_bytes_without_a_warning() -> void:
	var raw := PackedByteArray([0, 1, 2, 255, 4, 5])
	var header := {
		"v": 1,
		"id": "8",
		"type": "res",
		"method": "terrain.tile",
		"result": {},
		"blobs": [{"name": "source_index", "dtype": "<u1", "shape": [2, 3], "offset": 0, "length": raw.size()}],
	}
	var header_bytes := JSON.stringify(header).to_utf8_buffer()
	var frame := PackedByteArray()
	frame.resize(4)
	frame.encode_u32(0, header_bytes.size())
	frame.append_array(header_bytes)
	frame.append_array(raw)
	var box := []
	await assert_error(func() -> void: box.append(IpcEnvelope.decode(frame))).is_success()
	var response: IpcEnvelope = box[0]

	assert_that(response).is_not_null()
	var index: PackedByteArray = response.blobs.get("source_index")
	assert_that(index).is_equal(raw)
	assert_int(index.size()).is_equal(6)


## F51, encode side. Each value is built from its IEEE-754 bits: Godot's decimal parser does not round correctly, so a
## round trip through text would prove nothing. -FLT_MAX is the float32 no-data value; 0.3048006096 is the US survey foot in
## metres, a linear unit the settings carry.
static func _nodata_value() -> float:
	return PackedByteArray([0x00, 0x00, 0x00, 0xe0, 0xff, 0xff, 0xef, 0xc7]).decode_double(0)


static func _foot_value() -> float:
	return PackedByteArray([0x70, 0x1a, 0x82, 0x6a, 0xda, 0x81, 0xd3, 0x3f]).decode_double(0)


static func _header_text(frame: PackedByteArray) -> String:
	return frame.slice(4, 4 + frame.decode_u32(0)).get_string_from_utf8()


## The number token written after `"key":` in a header.
static func _token_after(header: String, key: String) -> String:
	var found := RegEx.create_from_string('"%s":(-?[0-9.eE+\\-]+)' % key).search(header)
	return "" if found == null else found.get_string(1)


## The header keeps each value as an exact text: `-3.4028234663852886e+38` and `0.3048006096` are the shortest decimal texts
## that a correctly rounded parser maps back to the same float64 (checked with Python's float()). Godot's default text for
## the first is `-340282346638528860000000000000000000000.0`, which is not that form.
func test_encoded_header_keeps_full_precision_for_float32_nodata() -> void:
	var frame := IpcEnvelope.encode("9", "req", "import.run", {"settings": {"nodata": _nodata_value(), "linear_unit_m": _foot_value()}})
	var header := _header_text(frame)

	assert_str(_token_after(header, "nodata")).is_equal("-3.4028234663852886e+38")
	assert_str(_token_after(header, "linear_unit_m")).is_equal("0.3048006096")


## Review decision 13: a third is the one double whose default text differs from its full-precision text. Its bits are
## 0x3fd5555555555555 (checked with Python: struct.pack('<d', 1/3)); the shortest text that reads back to them is 0.3333333333333333.
static func _third_value() -> float:
	return PackedByteArray([0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xd5, 0x3f]).decode_double(0)


func test_encoded_header_keeps_full_precision_for_one_third() -> void:
	var third := _third_value()
	var frame := IpcEnvelope.encode("10", "req", "import.run", {"settings": {"linear_unit_m": third}})
	var header := _header_text(frame)

	assert_str(_token_after(header, "linear_unit_m")).is_equal("0.3333333333333333")
	assert_str(JSON.stringify({"value": third})).is_not_equal("{\"value\":0.3333333333333333}")
