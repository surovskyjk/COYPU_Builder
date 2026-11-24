class_name NumberField
extends LineEdit
## A numeric text field that never changes the number it holds (T-136 Part B, review round 2). The text is the shortest
## text that reads back as exactly the stored float: 18184.9717 shows as written, 250.0 as "250", and
## -3.4028234663852886e38 in full. A value the owner sets, or the user types, is the value that is sent.
##
## Text that is not a number, or lies outside [member minimum] and [member maximum], is not a value: [method is_valid]
## is false and [method value] keeps the last valid one. Nothing is clamped or snapped. Up and Down step the value by
## [member arrow_step] while the field has focus and is [member editable]; the step is rounded to the larger decimal count
## of the text and of the step, so the steps stay decimal. Enter only confirms the number (it shows the exact text
## again); it never presses a button of the owner.

## Emitted when the user's text, or an arrow key, gives a valid value.
signal value_edited(value: float)

## The smallest and the largest value accepted. Infinity is no bound.
var minimum := -INF
var maximum := INF
## When true, `minimum` itself is refused: a unit, a buffer or a resolution must be above 0.
var minimum_exclusive := false
## The step of the Up and Down keys.
var arrow_step := 1.0

const MAX_DIGITS := 17
const FIXED_LOW := 0.0001
const FIXED_HIGH := 1.0e15
const LIMB := 1000000000
const LIMB_DIGITS := 9
## Above this many decimals the arrow sum is not rounded: a power of ten that large is not exact.
const MAX_ROUND_DECIMALS := 15
## Above 2^52 a double has no fraction, so rounding it changes nothing.
const WHOLE_LIMIT := 4503599627370496.0

var _value := 0.0
var _valid := false


func _init() -> void:
	text_changed.connect(_on_text_typed)
	text_submitted.connect(_on_submitted)
	gui_input.connect(_on_gui_input)


## Stores `new_value` exactly and shows it. Nothing is emitted: this is the owner's value, not the user's edit.
func set_value(new_value: float) -> void:
	_value = new_value
	_valid = _in_bounds(new_value)
	text = exact_text(new_value)


## The stored float: the last valid value the user typed or the owner set.
func value() -> float:
	return _value


## Whether the text (or the value set) is a number inside the bounds.
func is_valid() -> bool:
	return _valid


## Whether the text is a number at all, inside the bounds or not. The owner words its reason from it.
func is_number() -> bool:
	return text.strip_edges().is_valid_float()


## The shortest text that reads back as exactly `v`. The digits are the exact decimal digits of the float, rounded
## correctly to the fewest significant digits that read back: 250.0 gives "250", and magnitudes of 1e15 or more, or
## non-zero ones below 1e-4, use scientific notation, as in "-3.4028234663852890e+38". Godot's reader is not value-based
## at 17 digits (a trailing zero can change the float it reads), so a text is tried with its trailing zeros removed
## first, then with them. If none reads back, the 17-digit form is given.
static func exact_text(v: float) -> String:
	if v == 0.0:
		return "0"
	if not is_finite(v):
		return str(v)
	var magnitude := absf(v)
	var sign_text := "-" if v < 0.0 else ""
	var fixed := magnitude >= FIXED_LOW and magnitude < FIXED_HIGH
	var source := _exact_digits(magnitude)
	var digits: String = source[0]
	var last_power: int = source[1]
	var full := ""
	for count in range(1, MAX_DIGITS + 1):
		var rounded := _round_to(digits, last_power, count)
		var whole: String = rounded[0]
		var exponent: int = rounded[1]
		var trimmed := sign_text + _format(whole.rstrip("0"), exponent, fixed)
		if trimmed.to_float() == v:
			return trimmed
		full = sign_text + _format(whole, exponent, fixed)
		if full.to_float() == v:
			return full
	return full


## The exact decimal digits of a positive finite float, and the power of ten of their last digit. A float is M * 2^e,
## so for e >= 0 the digits are M * 2^e, and for e < 0 they are M * 5^-e with the point moved -e places, since
## 2^-j is 5^j / 10^j. The digits are big numbers, kept in limbs of nine decimal digits, little end first.
static func _exact_digits(magnitude: float) -> Array:
	var bytes := PackedByteArray()
	bytes.resize(8)
	bytes.encode_double(0, magnitude)
	var bits := bytes.decode_u64(0)
	var biased := int((bits >> 52) & 0x7FF)
	var fraction_bits := int(bits & 0xFFFFFFFFFFFFF)
	var significand := fraction_bits
	var binary_power := -1074
	if biased != 0:
		significand = fraction_bits | (1 << 52)
		binary_power = biased - 1075
	var limbs: Array[int] = []
	var rest := significand
	while rest > 0:
		limbs.append(rest % LIMB)
		rest /= LIMB
	var last_power := 0
	if binary_power >= 0:
		_multiply_by_power(limbs, 2, binary_power)
	else:
		_multiply_by_power(limbs, 5, -binary_power)
		last_power = binary_power
	var digits := str(limbs[limbs.size() - 1])
	for index in range(limbs.size() - 2, -1, -1):
		digits += str(limbs[index]).lpad(LIMB_DIGITS, "0")
	return [digits, last_power]


## Multiplies the limbs by `base` (2 or 5) raised to `power`. The factors are 2^29 or 5^13, so that every product of a
## limb and a factor stays inside 64 bits.
static func _multiply_by_power(limbs: Array[int], base: int, power: int) -> void:
	var chunk_power := 29 if base == 2 else 13
	var chunk := 1
	for _index in chunk_power:
		chunk *= base
	var remaining := power
	while remaining >= chunk_power:
		_multiply(limbs, chunk)
		remaining -= chunk_power
	var rest := 1
	for _index in remaining:
		rest *= base
	if rest > 1:
		_multiply(limbs, rest)


static func _multiply(limbs: Array[int], factor: int) -> void:
	var carry := 0
	for index in limbs.size():
		var product: int = limbs[index] * factor + carry
		limbs[index] = product % LIMB
		carry = product / LIMB
	while carry > 0:
		limbs.append(carry % LIMB)
		carry /= LIMB


## The first `count` significant digits of `digits * 10^last_power`, rounded half up, as the whole number of those digits
## and the exponent of their leading digit.
static func _round_to(digits: String, last_power: int, count: int) -> Array:
	var head := digits.substr(0, count).rpad(count, "0").to_int()
	var power := digits.length() - count + last_power
	if digits.length() > count and digits[count] >= "5":
		head += 1
	var limit := 1
	for _index in count:
		limit *= 10
	if head >= limit:
		head /= 10
		power += 1
	return [str(head), power + count - 1]


## The text of the digits `whole`, whose leading digit has the power of ten `exponent`: plain for a fixed-range value,
## scientific otherwise. The digits are written as given.
static func _format(whole: String, exponent: int, fixed: bool) -> String:
	if not fixed:
		var mantissa := whole.substr(0, 1)
		if whole.length() > 1:
			mantissa += "." + whole.substr(1)
		return "%se%s%d" % [mantissa, "+" if exponent >= 0 else "-", absi(exponent)]
	if exponent >= 0:
		var integer_digits := whole.substr(0, exponent + 1).rpad(exponent + 1, "0")
		var fraction := whole.substr(exponent + 1)
		return integer_digits if fraction.is_empty() else integer_digits + "." + fraction
	return "0." + "0".repeat(-exponent - 1) + whole


func _in_bounds(v: float) -> bool:
	if not is_finite(v):
		return false
	var above := v > minimum if minimum_exclusive else v >= minimum
	return above and v <= maximum


func _on_text_typed(typed: String) -> void:
	var trimmed := typed.strip_edges()
	_valid = false
	if not trimmed.is_valid_float():
		return
	var parsed := trimmed.to_float()
	if not _in_bounds(parsed):
		return
	_value = parsed
	_valid = true
	value_edited.emit(parsed)


## Enter confirms the number: a valid value is shown in its exact text again.
func _on_submitted(_submitted_text: String) -> void:
	if _valid:
		text = exact_text(_value)


## The number of decimal places a text shows, an exponent counted: "0.3048" has 4, "1e-7" has 7, "1.5e+3" has 0.
static func decimal_places(text: String) -> int:
	var body := text.strip_edges()
	var exponent := 0
	var split := body.findn("e")
	if split >= 0:
		exponent = body.substr(split + 1).to_int()
		body = body.substr(0, split)
	var point := body.find(".")
	var fraction := 0 if point < 0 else body.length() - point - 1
	return maxi(0, fraction - exponent)


## `v` rounded to `decimals` decimal places, which removes the binary noise of a sum such as 0.3048 + 0.01.
static func _rounded(v: float, decimals: int) -> float:
	if decimals > MAX_ROUND_DECIMALS:
		return v
	var scale := pow(10.0, decimals)
	if absf(v * scale) >= WHOLE_LIMIT:
		return v
	return roundf(v * scale) / scale


## Up and Down step from the number on show (or from the last valid one, when the text is not a number), rounded to the
## larger decimal count of that text and of [member arrow_step]. The step is then typed like any edit, so every listener
## sees it. A field that is not editable ignores the arrows.
func _on_gui_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key == null or not key.pressed or key.echo or not editable:
		return
	if key.keycode != KEY_UP and key.keycode != KEY_DOWN:
		return
	var direction := 1.0 if key.keycode == KEY_UP else -1.0
	var base := _value
	var shown := exact_text(_value)
	if is_number():
		base = text.strip_edges().to_float()
		shown = text.strip_edges()
	var decimals := maxi(decimal_places(shown), decimal_places(exact_text(arrow_step)))
	text = exact_text(_rounded(base + direction * arrow_step, decimals))
	text_changed.emit(text)
	get_viewport().set_input_as_handled()
