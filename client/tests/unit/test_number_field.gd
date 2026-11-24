extends GdUnitTestSuite
## T-136 Part B, review round 2: the numeric field keeps the exact float. Every literal is written out, and the reads
## go through the field's own text, as a user's typing does.


func _field() -> NumberField:
	var field := NumberField.new()
	add_child(field)
	auto_free(field)
	return field


func _type(field: NumberField, text: String) -> void:
	field.text = text
	field.text_changed.emit(text)


func _frames(count: int) -> void:
	for i in count:
		await get_tree().process_frame


func _key(field: NumberField, keycode: Key) -> void:
	field.grab_focus()
	var press := InputEventKey.new()
	press.keycode = keycode
	press.physical_keycode = keycode
	press.pressed = true
	get_viewport().push_input(press)


## The length of the shortest fixed-point text that reads back as `v`, or a large number when none does.
func _shortest_fixed_length(v: float) -> int:
	for decimals in range(18):
		var text := String.num(v, decimals)
		if text.to_float() == v:
			return text.length()
	return 999


## The double whose bit pattern is `bits`. Godot's own reader of a literal is not correctly rounded at every value, so the
## boundary values are named by their bits.
func _from_bits(bits: int) -> float:
	var bytes := PackedByteArray()
	bytes.resize(8)
	bytes.encode_u64(0, bits)
	return bytes.decode_double(0)


## Whether a scientific text with fewer significant digits than `literal` reads back as `v`. The nearest text with `count`
## digits is the first `count` digits of the literal or those digits plus one in the last place, so only those two are tried.
func _shorter_reads_back(literal: String, v: float) -> bool:
	var sign_text := "-" if literal.begins_with("-") else ""
	var body := literal.trim_prefix("-")
	var split := body.find("e")
	var exponent := body.substr(split + 1).to_int()
	var digits := body.substr(0, split).replace(".", "")
	for count in range(1, digits.length()):
		var place := exponent - count + 1
		var head := digits.substr(0, count).to_int()
		for head_value in [head, head + 1]:
			var candidate := "%s%de%d" % [sign_text, head_value, place]
			if candidate.to_float() == v:
				return true
	return false


## The significant digits of a text: the digits of its mantissa, without the sign, the point and the leading zeros.
func _significant_digits(text: String) -> int:
	var split := text.findn("e")
	var mantissa := text if split < 0 else text.substr(0, split)
	return mantissa.replace("-", "").replace(".", "").lstrip("0").length()


func test_exact_text_reads_back_exactly() -> void:
	var cases := [
		[0.3048006096, "0.3048006096"],
		[12345.6789, "12345.6789"],
		[18184.971666, "18184.971666"],
		[250.0, "250"],
		[-746900.0, "-746900"],
		[1e-7, "1e-7"],
		[0.0, "0"],
		[-3.4028234663852886e38, "-3.4028234663852890e+38"],
	]
	for case: Array in cases:
		var v: float = case[0]
		var text := NumberField.exact_text(v)
		assert_str(text).append_failure_message("the text of %s" % str(v)).is_equal(String(case[1]))
		assert_float(text.to_float()).append_failure_message("%s does not read back as %s" % [text, str(v)]).is_equal(v)
		if text.contains("e"):
			assert_bool(_shorter_reads_back(text, v)) \
				.append_failure_message("%s has a shorter text that reads back as %s" % [text, str(v)]) \
				.is_false()
		else:
			assert_int(text.length()) \
				.append_failure_message("%s is longer than the shortest text of %s" % [text, str(v)]) \
				.is_less_equal(_shortest_fixed_length(v))


## The boundaries and extremes. Each row is the double, its exact text, and whether that text reads back as the double in
## Godot. A text that does not read back is the correctly rounded 17-digit text, as the spec of [method exact_text] says.
## The neighbours are the doubles one bit either side of 1e15, 1e-4 and -1947302.4602381494, and 999999999999999.9 is the
## double nearest to it; Godot reads "999999999999999.9" as 1e15, so its 17-digit text is the answer.
func test_exact_text_boundaries_and_extremes() -> void:
	var cases := [
		[_from_bits(0x430c6bf526340000), "1e+15", true],
		[_from_bits(0x430c6bf526340001), "1.0000000000000001e+15", false],
		[_from_bits(0x430c6bf52633ffff), "999999999999999.88", true],
		[_from_bits(0x3f1a36e2eb1c432d), "0.0001", true],
		[_from_bits(0x3f1a36e2eb1c432c), "9.9999999999999991e-5", false],
		[_from_bits(0x3f1a36e2eb1c432e), "0.00010000000000000002", false],
		[_from_bits(-0x7fffffffffffffff - 1), "0", true],
		[_from_bits(1), "4.9406564584124654e-324", false],
		[_from_bits(0x7fefffffffffffff), "1.7976931348623157e+308", true],
		[-_from_bits(0x413db6a675d22ad8), "-1947302.4602381494", false],
	]
	for case: Array in cases:
		var v: float = case[0]
		var expected := String(case[1])
		var text := NumberField.exact_text(v)
		assert_str(text).append_failure_message("the text of the double %s" % str(v)).is_equal(expected)
		if bool(case[2]):
			assert_float(text.to_float()).append_failure_message("%s does not read back" % text).is_equal(v)
			if text.contains("e"):
				assert_bool(_shorter_reads_back(text, v)) \
					.append_failure_message("%s has a shorter text that reads back" % text) \
					.is_false()
			else:
				assert_int(text.length()) \
					.append_failure_message("%s is longer than the shortest text" % text) \
					.is_less_equal(_shortest_fixed_length(v))
		else:
			assert_bool(text.to_float() == v).append_failure_message("%s reads back now: move it to the reading cases" % text).is_false()
			assert_int(_significant_digits(text)) \
				.append_failure_message("%s is not the correctly rounded 17-digit text" % text) \
				.is_equal(17)


func test_arrow_steps_are_decimal() -> void:
	var field := _field()
	field.arrow_step = 0.01
	field.set_value(0.3048)
	await _frames(1)
	_key(field, KEY_UP)
	await _frames(2)
	assert_str(field.text).is_equal("0.3148")
	assert_float(field.value()).is_equal(3148.0 / 10000.0)
	_key(field, KEY_UP)
	await _frames(2)
	assert_str(field.text).is_equal("0.3248")
	assert_float(field.value()).is_equal(3248.0 / 10000.0)

	field.arrow_step = 0.1
	field.set_value(0.7)
	await _frames(1)
	_key(field, KEY_UP)
	await _frames(2)
	assert_str(field.text).is_equal("0.8")
	assert_float(field.value()).is_equal(8.0 / 10.0)
	_key(field, KEY_DOWN)
	await _frames(2)
	assert_str(field.text).is_equal("0.7")


func test_a_read_only_field_ignores_the_arrows() -> void:
	var field := _field()
	field.set_value(1.0)
	field.editable = false
	var press := InputEventKey.new()
	press.keycode = KEY_UP
	press.physical_keycode = KEY_UP
	press.pressed = true
	# The signal is emitted directly, so the test does not depend on the field taking focus.
	field.gui_input.emit(press)
	assert_float(field.value()).append_failure_message("a read-only field stepped").is_equal(1.0)
	assert_str(field.text).is_equal("1")

	field.editable = true
	field.gui_input.emit(press)
	assert_float(field.value()).append_failure_message("the same key does not step an editable field").is_equal(2.0)


func test_an_untouched_field_returns_the_value_it_was_given() -> void:
	var field := _field()
	field.set_value(-3.4028234663852886e38)
	assert_float(field.value()).is_equal(-3.4028234663852886e38)
	assert_bool(field.is_valid()).is_true()
	assert_str(field.text).is_equal("-3.4028234663852890e+38")

	field.set_value(0.3048006096)
	assert_float(field.value()).is_equal(0.3048006096)
	assert_str(field.text).is_equal("0.3048006096")


func test_invalid_and_out_of_bounds_text_is_not_a_value() -> void:
	var field := _field()
	field.minimum = 0.0
	field.minimum_exclusive = true
	field.maximum = 100.0
	var edits: Array = []
	field.value_edited.connect(func(value: float) -> void: edits.append(value))
	field.set_value(5.0)

	_type(field, "abc")
	assert_bool(field.is_valid()).append_failure_message("text that is no number is a value").is_false()
	assert_bool(field.is_number()).is_false()
	assert_float(field.value()).is_equal(5.0)

	_type(field, "150")
	assert_bool(field.is_valid()).append_failure_message("150 above the maximum is a value").is_false()
	assert_bool(field.is_number()).is_true()
	assert_str(field.text).append_failure_message("out-of-bounds text was changed").is_equal("150")
	assert_float(field.value()).is_equal(5.0)

	_type(field, "0")
	assert_bool(field.is_valid()).append_failure_message("the exclusive minimum 0 is a value").is_false()
	assert_float(field.value()).is_equal(5.0)

	_type(field, "100")
	assert_bool(field.is_valid()).append_failure_message("the inclusive maximum is refused").is_true()
	assert_float(field.value()).is_equal(100.0)

	_type(field, "0.5")
	assert_float(field.value()).is_equal(0.5)
	assert_array(edits).append_failure_message("value_edited fired for text that was no value").contains_exactly([100.0, 0.5])


func test_up_and_down_step_by_the_arrow_step() -> void:
	var field := _field()
	field.arrow_step = 0.5
	field.set_value(1.0)
	await _frames(1)

	_key(field, KEY_UP)
	await _frames(2)
	assert_float(field.value()).is_equal(1.5)
	assert_str(field.text).is_equal("1.5")

	_key(field, KEY_DOWN)
	await _frames(2)
	_key(field, KEY_DOWN)
	await _frames(2)
	assert_float(field.value()).is_equal(0.5)
	assert_str(field.text).is_equal("0.5")
	assert_bool(field.is_valid()).is_true()


func test_enter_shows_the_exact_text_of_the_number() -> void:
	var field := _field()
	field.set_value(250.0)
	_type(field, "250.00")
	field.text_submitted.emit("250.00")
	assert_str(field.text).is_equal("250")
	assert_float(field.value()).is_equal(250.0)
