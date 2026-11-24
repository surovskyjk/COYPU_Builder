class_name CrsPicker
extends HBoxContainer
## A searchable coordinate-reference-system field (T-136 Part B). The user types a code or words of a name; after
## a 250 ms pause the picker asks the backend (`crs.search`, T-136 Part A) and lists the answers in a dropdown. A
## result the user picks becomes the [method code]; free text never does, so [method code] is "" until a result
## is chosen. A CRS that the file declares as WKT or PROJ text is held verbatim and shown as "As declared in the
## file". The picker never rewrites a code it was given.
##
## Keys in the field: Enter chooses the first listed result while no code is chosen and a list is shown, emits
## [signal submitted] for the owner to act on once a code is chosen, and does nothing otherwise. Down opens the dropdown.
## Every keystroke puts the list back to its idle placeholder until the answer for the new text arrives, so a list is
## never a stale answer. The field takes three parts of the row and the dropdown two, and the dropdown never widens the
## picker (its long names are clipped).

signal code_changed(code: String)
## Enter in the field once a code is chosen: the owner decides what Enter does then.
signal submitted

const DEBOUNCE_SEC := 0.25
const RESULT_LIMIT := 20
const DECLARED_TEXT := "As declared in the file"
const NAME_SEPARATOR := " — "
const PLACEHOLDER_IDLE := "Search results"
const PLACEHOLDER_NONE := "No match — try other words"
const PLACEHOLDER_HIT := "Choose a result"
const FIELD_SHARE := 3.0
const RESULTS_SHARE := 2.0

## `func(method: String, params: Dictionary) -> IpcEnvelope` (a coroutine). Tests replace it with a fake backend.
var requester: Callable = _backend_request

## `func(seconds: float)` (a coroutine): the debounce pause. Tests record the call instead of sleeping.
var wait: Callable = _pause

var _kind := ""
var _code := ""
var _line: LineEdit
var _results: OptionButton
var _typing_serial := 0   # a search runs only when no key was typed after it
var _choice_serial := 0   # a name lookup applies only to the code that is still chosen


func _init() -> void:
	add_theme_constant_override(&"separation", 8)
	_line = LineEdit.new()
	_line.name = "Code"
	_line.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_line.size_flags_stretch_ratio = FIELD_SHARE
	_line.placeholder_text = "Code or name, for example 5514 or Krovak"
	_line.text_changed.connect(_on_text_changed)
	_line.text_submitted.connect(_on_text_submitted)
	_line.gui_input.connect(_on_line_input)
	add_child(_line)

	_results = OptionButton.new()
	_results.name = "Results"
	_results.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_results.size_flags_stretch_ratio = RESULTS_SHARE
	_results.fit_to_longest_item = false
	_results.clip_text = true
	_results.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_results.item_selected.connect(choose_result)
	add_child(_results)
	_show_results([], PLACEHOLDER_IDLE)


## The chosen code (`EPSG:5514`, or the WKT or PROJ text a file declared), or "" when none is chosen.
func code() -> String:
	return _code


## Sets the chosen code without the user's choice. An EPSG code is shown at once and its name is looked up after. The
## dropdown goes back to its idle placeholder, so a list from an earlier search is never shown beside this code.
func set_code(new_code: String) -> void:
	_typing_serial += 1
	_choice_serial += 1
	var choice := _choice_serial
	var changed := _code != new_code
	_code = new_code
	_show_results([], PLACEHOLDER_IDLE)
	if new_code.is_empty():
		_line.text = ""
	elif not _is_epsg(new_code):
		_line.text = DECLARED_TEXT
	else:
		_line.text = new_code
		_lookup_name(new_code, choice)
	if changed:
		code_changed.emit(_code)


## Limits the search to one CRS kind: `projected`, `geographic`, `vertical`, `compound`, `horizontal`, or "" for any.
func set_kind_filter(kind: String) -> void:
	_kind = kind
	if _code.is_empty() and not _line.text.strip_edges().is_empty():
		_on_text_changed(_line.text)


func kind_filter() -> String:
	return _kind


## The field the user types in (tests and the dialog's focus).
func line_edit() -> LineEdit:
	return _line


## The dropdown of results, its first item is the placeholder.
func results_choice() -> OptionButton:
	return _results


## The codes the dropdown offers now, in order (empty while only the placeholder shows).
func result_codes() -> PackedStringArray:
	var codes := PackedStringArray()
	for index in range(_results.item_count):
		var data: Variant = _results.get_item_metadata(index)
		if data is Dictionary:
			codes.append(String(data["code"]))
	return codes


func focus() -> void:
	_line.grab_focus()


## Chooses dropdown item `index`; the placeholder and an out-of-range index change nothing.
func choose_result(index: int) -> void:
	if index < 0 or index >= _results.item_count:
		return
	var data: Variant = _results.get_item_metadata(index)
	if not (data is Dictionary):
		return
	var entry := data as Dictionary
	_typing_serial += 1
	_choice_serial += 1
	_code = String(entry["code"])
	var crs_name := String(entry.get("name", ""))
	_line.text = _code if crs_name.is_empty() else _code + NAME_SEPARATOR + crs_name
	code_changed.emit(_code)


static func _is_epsg(text: String) -> bool:
	return text.begins_with("EPSG:") and text.substr(5).is_valid_int()


func _backend_request(method: String, params: Dictionary) -> IpcEnvelope:
	return await Backend.request(method, params)


func _pause(seconds: float) -> void:
	await get_tree().create_timer(seconds).timeout


func _on_text_changed(text: String) -> void:
	_typing_serial += 1
	_choice_serial += 1
	if not _code.is_empty():
		_code = ""
		code_changed.emit("")
	_show_results([], PLACEHOLDER_IDLE)
	var query := text.strip_edges()
	if query.is_empty():
		return
	var serial := _typing_serial
	await wait.call(DEBOUNCE_SEC)
	if serial != _typing_serial:
		return
	await _search(query, serial)


## Enter with a code chosen goes to the owner. Without one, it chooses the first result of the list on show; with no list
## on show it does nothing.
func _on_text_submitted(_text: String) -> void:
	if not _code.is_empty():
		submitted.emit()
	elif _results.item_count > 1:
		choose_result(1)


func _on_line_input(event: InputEvent) -> void:
	var key := event as InputEventKey
	if key != null and key.pressed and not key.echo and key.keycode == KEY_DOWN:
		_results.show_popup()
		get_viewport().set_input_as_handled()


func _search(query: String, serial: int) -> void:
	var reply: IpcEnvelope = await requester.call(
		"crs.search", {"query": query, "limit": RESULT_LIMIT, "kind": _kind}
	)
	if serial != _typing_serial:
		return
	if reply.type != "res":
		_show_results([], PLACEHOLDER_NONE)
		return
	var entries: Array = reply.result.get("results", [])
	if entries.is_empty():
		_show_results([], PLACEHOLDER_NONE)
	else:
		_show_results(entries, PLACEHOLDER_HIT)


## Fills the dropdown; the placeholder is item 0 and cannot be chosen. The placeholder is selected after the results are
## added, so a listed result never shows as the choice while no code is chosen.
func _show_results(entries: Array, placeholder: String) -> void:
	_results.clear()
	_results.add_item(placeholder)
	_results.set_item_disabled(0, true)
	_results.set_item_metadata(0, null)
	for entry: Variant in entries:
		if not (entry is Dictionary):
			continue
		var data := entry as Dictionary
		var code_text := String(data.get("code", ""))
		var crs_name := String(data.get("name", ""))
		_results.add_item(code_text + NAME_SEPARATOR + crs_name)
		_results.set_item_metadata(_results.item_count - 1, {"code": code_text, "name": crs_name})
	_results.select(0)


## Fills in the name of a chosen EPSG code from the first `crs.search` answer with that code. A lookup that finishes
## after the user chose another code (or typed) changes nothing.
func _lookup_name(code_text: String, choice: int) -> void:
	var reply: IpcEnvelope = await requester.call(
		"crs.search", {"query": code_text, "limit": 1, "kind": _kind}
	)
	if choice != _choice_serial or _code != code_text or reply.type != "res":
		return
	var entries: Array = reply.result.get("results", [])
	if entries.is_empty():
		return
	var first := entries[0] as Dictionary
	if String(first.get("code", "")) != code_text:
		return
	var crs_name := String(first.get("name", ""))
	if crs_name.is_empty():
		return
	_line.text = code_text + NAME_SEPARATOR + crs_name
