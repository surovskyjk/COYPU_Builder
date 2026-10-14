class_name EnvelopeExportDialog
extends ConfirmationDialog
## "Export corridor envelope..." (T-140): asks for the buffer half-width, the format, an optional station
## range, an optional output CRS and a save path, then sends `alignment.envelope` (T-138) and reports the
## result. The backend writes the file; the client never builds geometry (ADR 0001).
##
## Rules this dialog enforces:
## - The target is checked on the client first: if it exists the user is asked, and `overwrite: true` is sent
##   only after a "yes". It is never sent silently. (A file that appears between the check and the request is
##   caught by the backend's own refusal and goes through the same question once.)
## - The request runs from this dialog's coroutine ([method run_export]); nothing here is in `_process`.
## - Every failure is shown as one plain sentence with the next step ([method plain_error]); raw backend
##   messages and codes never reach the user.
## - The save path comes from Godot's native [FileDialog] (or is typed). A path inside the repository is
##   refused by the backend (ADR 0010) and shown as "choose a folder outside the project folder".

## Emitted after a successful export. `summary` is the one line the status bar shows.
signal exported(result: Dictionary, summary: String)

const FORMAT_GEOJSON := &"geojson"
const FORMAT_SHAPEFILE := &"shapefile"
const BUFFER_MIN_M := 1.0
const BUFFER_MAX_M := 5000.0
const BUFFER_DEFAULT_M := 250.0
## Millimetre steps: a coarser step would round an alignment's end (18184.9717 m) past it, and the backend refuses a
## range beyond the alignment (1e-6 m of tolerance).
const STATION_STEP_M := 0.001
const SHAPEFILE_PARTS: Array[String] = ["shp", "shx", "dbf", "prj", "cpg"]

## `func(method: String, params: Dictionary) -> IpcEnvelope` (a coroutine). Tests replace it to talk to a
## fixture backend instead of the `Backend` autoload.
var requester: Callable = func(method: String, params: Dictionary) -> IpcEnvelope:
	return await Backend.request(method, params)

## Optional `func(existing_names: PackedStringArray) -> bool` (a coroutine allowed). When valid it answers the
## overwrite question instead of the confirmation window, which is how tests answer it.
var overwrite_decider: Callable

var _alignments: Array[Dictionary] = []
var _alignment_choice: OptionButton
var _alignment_row: Array[Control] = []
var _buffer: SpinBox
var _format: OptionButton
var _station_check: CheckBox
var _station_from: SpinBox
var _station_to: SpinBox
var _station_row: Control
var _epsg: LineEdit
var _path: LineEdit
var _browse: Button
var _error: Label
var _hint: Label
var _file_dialog: FileDialog
var _overwrite_dialog: ConfirmationDialog
var _overwrite_answer := 0  # 0 pending, 1 yes, -1 no
var _busy := false


func _init() -> void:
	title = "Export corridor envelope"
	ok_button_text = "Export"
	cancel_button_text = "Cancel"
	dialog_hide_on_ok = false
	min_size = Vector2i(760, 0)
	exclusive = true
	_build()
	confirmed.connect(_on_confirmed)


# --- static helpers (pure; unit-tested) ------------------------------------------------------------------

## Validates `form` and turns it into `alignment.envelope` params. Keys of `form`: `path`, `buffer_m`, `format`
## (&"geojson" / &"shapefile"), `station_enabled`, `station_from`, `station_to`, `epsg_text`, `alignment_id`
## ("" = every alignment), `overwrite`. Returns `{"ok": true, "params": {...}}` or `{"ok": false, "error": "..."}`.
static func build_params(form: Dictionary) -> Dictionary:
	var path := String(form.get("path", "")).strip_edges()
	if path.is_empty():
		return {"ok": false, "error": "Choose where to save the file first."}
	var format: StringName = form.get("format", FORMAT_GEOJSON)
	var buffer_m := float(form.get("buffer_m", BUFFER_DEFAULT_M))
	if not (buffer_m >= BUFFER_MIN_M and buffer_m <= BUFFER_MAX_M):
		return {"ok": false, "error": "The buffer half-width must be between %d and %d m." % [int(BUFFER_MIN_M), int(BUFFER_MAX_M)]}

	var params := {"path": normalized_path(path, format), "buffer_m": buffer_m}

	var epsg_text := String(form.get("epsg_text", "")).strip_edges()
	if not epsg_text.is_empty():
		if not epsg_text.is_valid_int() or int(epsg_text) <= 0:
			return {"ok": false, "error": "Enter the output CRS as an EPSG number such as 4326, or leave it empty for the default."}
		params["epsg"] = int(epsg_text)

	var alignment_id := String(form.get("alignment_id", ""))
	if bool(form.get("station_enabled", false)):
		if alignment_id.is_empty():
			return {"ok": false, "error": "Choose a single alignment to limit the station range."}
		var from_s := float(form.get("station_from", 0.0))
		var to_s := float(form.get("station_to", 0.0))
		if not (from_s < to_s):
			return {"ok": false, "error": "The From station must be smaller than the To station."}
		params["station_from"] = from_s
		params["station_to"] = to_s
	if not alignment_id.is_empty():
		params["alignment_ids"] = [alignment_id]
	if bool(form.get("overwrite", false)):
		params["overwrite"] = true
	return {"ok": true, "params": params}


## `path` with the extension the format needs: `.geojson` (or an existing `.json`) / `.shp`.
static func normalized_path(path: String, format: StringName) -> String:
	var extension := path.get_extension().to_lower()
	if format == FORMAT_SHAPEFILE:
		if extension == "shp":
			return path
		if extension in ["geojson", "json"]:
			return path.get_basename() + ".shp"
		return path + ".shp"
	if extension in ["geojson", "json"]:
		return path
	if extension == "shp":
		return path.get_basename() + ".geojson"
	return path + ".geojson"


## The files an export to `path` would write: the one file, or all five Shapefile parts.
static func output_files(path: String) -> PackedStringArray:
	var files := PackedStringArray()
	if path.get_extension().to_lower() == "shp":
		for part in SHAPEFILE_PARTS:
			files.append(path.get_basename() + "." + part)
	else:
		files.append(path)
	return files


## The subset of [method output_files] that exists on disk now.
static func existing_outputs(path: String) -> PackedStringArray:
	var existing := PackedStringArray()
	for file in output_files(path):
		if FileAccess.file_exists(file):
			existing.append(file)
	return existing


## True when the backend's refusal means "that file already exists".
static func is_overwrite_refusal(code: String, message: String) -> bool:
	return code == "E_BAD_PARAMS" and message.contains("refusing to overwrite")


## One plain sentence, with the next step, for a backend error. The raw message is only used to tell causes
## apart; it is never shown.
static func plain_error(code: String, message: String) -> String:
	var lower := message.to_lower()
	match code:
		"E_DISCONNECTED":
			return "The backend is not running, so nothing was exported. Wait for it to reconnect, then try again."
		"E_TIMEOUT", "E_CLIENT_TIMEOUT":
			return "The backend did not answer in time. Try again, with a smaller buffer or a station range if the corridor is long."
		"E_BUSY":
			return "The backend is busy with other work. Wait a moment, then export again."
		"E_NO_SESSION", "E_NO_PROJECT":
			return "There is no project open yet. Import an alignment first, then export its envelope."
		"E_EMPTY":
			return "The project has no alignments to export. Import an alignment first."
		"E_CRS_REQUIRED":
			return "The project has no coordinate system. Import an alignment that defines one, then try again."
		"E_NOT_FOUND":
			return "That alignment is no longer in the project. Close this window, reopen it and choose again."
		"E_INTERNAL":
			if lower.contains("extra gis") or lower.contains("shapely") or lower.contains("pyshp"):
				return "The backend is missing its GIS add-on. Run \"uv sync --extra gis\" in the backend folder, restart, then try again."
			return "The backend hit an unexpected problem and did not write the file. Try again; if it repeats, check the backend log."
		"E_BAD_PARAMS":
			if lower.contains("outside the repository"):
				return "The file must be saved outside the project folder. Choose another folder, for example Documents."
			if lower.contains("must be absolute"):
				return "Enter the full path of the file, including its folder."
			if lower.contains("refusing to overwrite"):
				return "A file with that name already exists. Choose another name, or export again and confirm replacing it."
			if lower.contains("cannot write"):
				return "The file could not be written there. Check that the folder exists and can be written to, or choose another folder."
			if lower.contains("buffer"):
				return "The buffer half-width must be between %d and %d m." % [int(BUFFER_MIN_M), int(BUFFER_MAX_M)]
			if lower.contains("station") or lower.contains("range"):
				return "The station range is reversed or outside the alignment. Use a From smaller than To, within the alignment."
			if lower.contains("epsg") or lower.contains("crs"):
				return "That output CRS cannot be used. Enter a projected or geographic 2D EPSG code such as 4326 or 5514, or leave it empty."
			return "The backend did not accept those settings. Check the values and try again."
	return "The export failed. Try again; if it repeats, check the backend log."


## The status-bar line for a successful `alignment.envelope` result.
static func summary_of(result: Dictionary) -> String:
	var names := PackedStringArray()
	var folder := ""
	for file: Variant in result.get("files", []):
		names.append(String(file).get_file())
		folder = String(file).get_base_dir()
	var area_km2 := float(result.get("area_m2", 0.0)) / 1.0e6
	var shown := "no files"
	if names.size() == 1:
		shown = names[0]
	elif names.size() > 1:
		shown = "%s and %d more files" % [names[0], names.size() - 1]
	return "Envelope written: %s (%s) | %.2f km² | %d vertices" % [shown, folder, area_km2, int(result.get("vertex_count", 0))]


# --- dialog -----------------------------------------------------------------------------------------------

func buffer_field() -> SpinBox:
	return _buffer


func path_field() -> LineEdit:
	return _path


func error_label() -> Label:
	return _error


func format_choice() -> OptionButton:
	return _format


func station_check() -> CheckBox:
	return _station_check


func station_from_field() -> SpinBox:
	return _station_from


func station_to_field() -> SpinBox:
	return _station_to


func is_busy() -> bool:
	return _busy


## Fills the form from the project (`alignments` are `AlignmentSummary` dictionaries), resets the error and
## opens the window centred.
func open(alignments: Array[Dictionary]) -> void:
	prepare(alignments)
	popup_centered()


## [method open] without showing the window (tests, and the capture driver which shows it itself).
func prepare(alignments: Array[Dictionary]) -> void:
	_alignments = alignments
	_error.text = ""
	_error.visible = false
	_alignment_choice.clear()
	_alignment_choice.add_item("All alignments")
	_alignment_choice.set_item_metadata(0, "")
	for entry in alignments:
		_alignment_choice.add_item(String(entry.get("name", "Alignment")))
		_alignment_choice.set_item_metadata(_alignment_choice.item_count - 1, String(entry.get("alignment_id", "")))
	var several := alignments.size() > 1
	for control in _alignment_row:
		control.visible = several
	_alignment_choice.select(0)
	if _path.text.is_empty():
		_path.text = _default_path()
	_refresh_station_defaults()
	_refresh_enabled()


## Reads the widgets into the dictionary [method build_params] takes.
func collect_form() -> Dictionary:
	return {
		"path": _path.text,
		"buffer_m": _buffer.value,
		"format": _selected_format(),
		"station_enabled": _station_check.button_pressed and _station_check.visible,
		"station_from": _station_from.value,
		"station_to": _station_to.value,
		"epsg_text": _epsg.text,
		"alignment_id": _selected_alignment_id(),
		"overwrite": false,
	}


## Coroutine: validates the form, asks about an existing target, sends the request and reports. Resolves to
## `{"ok": bool, "cancelled": bool, "message": String, "result": Dictionary}` and, on success, emits
## [signal exported] and closes the window; on failure it keeps the window open with the sentence shown.
func run_export() -> Dictionary:
	if _busy:
		return {"ok": false, "cancelled": true, "message": "", "result": {}}
	_show_error("")
	var built := build_params(collect_form())
	if not bool(built["ok"]):
		_show_error(String(built["error"]))
		return {"ok": false, "cancelled": false, "message": String(built["error"]), "result": {}}
	var params: Dictionary = built["params"]
	_path.text = String(params["path"])

	var existing := existing_outputs(String(params["path"]))
	if not existing.is_empty():
		if not await _ask_overwrite(existing):
			return {"ok": false, "cancelled": true, "message": "", "result": {}}
		params["overwrite"] = true

	_set_busy(true)
	var reply: IpcEnvelope = await requester.call("alignment.envelope", params)
	if reply.type != "res" and not bool(params.get("overwrite", false)) \
			and is_overwrite_refusal(String(reply.error.get("code", "")), String(reply.error.get("message", ""))):
		_set_busy(false)
		if await _ask_overwrite(PackedStringArray([String(params["path"]).get_file()])):
			params["overwrite"] = true
			_set_busy(true)
			reply = await requester.call("alignment.envelope", params)
		else:
			return {"ok": false, "cancelled": true, "message": "", "result": {}}
	_set_busy(false)

	if reply.type != "res":
		var sentence := plain_error(String(reply.error.get("code", "")), String(reply.error.get("message", "")))
		_show_error(sentence)
		return {"ok": false, "cancelled": false, "message": sentence, "result": {}}

	var summary := summary_of(reply.result)
	hide()
	exported.emit(reply.result, summary)
	return {"ok": true, "cancelled": false, "message": summary, "result": reply.result}


func _on_confirmed() -> void:
	run_export()


## Asks "replace?" for `names`; true only on an explicit yes.
func _ask_overwrite(names: PackedStringArray) -> bool:
	if overwrite_decider.is_valid():
		return bool(await overwrite_decider.call(names))
	var shown := "\n".join(names)
	_overwrite_dialog.dialog_text = "These files already exist:\n\n%s\n\nReplace them?" % shown
	_overwrite_answer = 0
	_overwrite_dialog.popup_centered()
	while _overwrite_answer == 0:
		await get_tree().process_frame
	return _overwrite_answer > 0


func _build() -> void:
	var column := VBoxContainer.new()
	column.add_theme_constant_override(&"separation", 10)
	add_child(column)

	_hint = Label.new()
	_hint.text = "The envelope is a buffer polygon around the track. Load it in the ČÚZK Geoprohlížeč to find the terrain and imagery sheets that cover your corridor."
	_hint.theme_type_variation = &"HintLabel"
	_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_hint.custom_minimum_size = Vector2(720, 0)
	column.add_child(_hint)

	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override(&"h_separation", 12)
	grid.add_theme_constant_override(&"v_separation", 8)
	column.add_child(grid)

	var alignment_label := _add_label(grid, "Alignment")
	_alignment_choice = OptionButton.new()
	_alignment_choice.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_alignment_choice.item_selected.connect(_on_alignment_selected)
	grid.add_child(_alignment_choice)
	_alignment_row = [alignment_label, _alignment_choice]

	_add_label(grid, "Buffer half-width")
	_buffer = SpinBox.new()
	_buffer.min_value = BUFFER_MIN_M
	_buffer.max_value = BUFFER_MAX_M
	_buffer.step = 1.0
	_buffer.value = BUFFER_DEFAULT_M
	_buffer.suffix = "m"
	_buffer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	grid.add_child(_buffer)

	_add_label(grid, "Format")
	_format = OptionButton.new()
	_format.add_item("GeoJSON (.geojson)")
	_format.set_item_metadata(0, FORMAT_GEOJSON)
	_format.add_item("Shapefile (.shp)")
	_format.set_item_metadata(1, FORMAT_SHAPEFILE)
	_format.item_selected.connect(_on_format_selected)
	grid.add_child(_format)

	_add_label(grid, "Station range")
	_station_check = CheckBox.new()
	_station_check.text = "Limit to part of the alignment"
	_station_check.toggled.connect(func(_on: bool) -> void: _refresh_enabled())
	grid.add_child(_station_check)

	_add_label(grid, "")
	_station_row = HBoxContainer.new()
	_station_row.add_theme_constant_override(&"separation", 8)
	var from_label := Label.new()
	from_label.text = "From"
	_station_row.add_child(from_label)
	_station_from = _make_station_box()
	_station_row.add_child(_station_from)
	var to_label := Label.new()
	to_label.text = "To"
	_station_row.add_child(to_label)
	_station_to = _make_station_box()
	_station_row.add_child(_station_to)
	grid.add_child(_station_row)

	_add_label(grid, "Output CRS")
	_epsg = LineEdit.new()
	_epsg.placeholder_text = "EPSG code; empty = 4326 (GeoJSON) or the project CRS (Shapefile)"
	_epsg.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	grid.add_child(_epsg)

	_add_label(grid, "Save to")
	var path_row := HBoxContainer.new()
	path_row.add_theme_constant_override(&"separation", 6)
	_path = LineEdit.new()
	_path.placeholder_text = "Choose a file; it must be outside the project folder"
	_path.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	path_row.add_child(_path)
	_browse = Button.new()
	_browse.text = "Browse…"
	_browse.pressed.connect(_on_browse_pressed)
	path_row.add_child(_browse)
	grid.add_child(path_row)

	_error = Label.new()
	_error.theme_type_variation = &"ErrorLabel"
	_error.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_error.custom_minimum_size = Vector2(720, 0)
	_error.visible = false
	column.add_child(_error)

	_file_dialog = FileDialog.new()
	_file_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_file_dialog.file_mode = FileDialog.FILE_MODE_SAVE_FILE
	_file_dialog.use_native_dialog = true
	_file_dialog.title = "Save corridor envelope"
	_file_dialog.file_selected.connect(_on_file_picked)
	add_child(_file_dialog)

	_overwrite_dialog = ConfirmationDialog.new()
	_overwrite_dialog.title = "Replace existing file?"
	_overwrite_dialog.ok_button_text = "Replace"
	_overwrite_dialog.cancel_button_text = "Keep the existing file"
	_overwrite_dialog.confirmed.connect(func() -> void: _overwrite_answer = 1)
	_overwrite_dialog.canceled.connect(func() -> void: _overwrite_answer = -1)
	add_child(_overwrite_dialog)


func _make_station_box() -> SpinBox:
	var box := SpinBox.new()
	box.min_value = -1.0e7
	box.max_value = 1.0e7
	box.step = STATION_STEP_M
	box.suffix = "m"
	box.custom_minimum_size = Vector2(150, 0)
	return box


func _add_label(grid: GridContainer, text: String) -> Label:
	var label := Label.new()
	label.text = text
	label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	grid.add_child(label)
	return label


func _selected_format() -> StringName:
	return _format.get_item_metadata(_format.selected)


func _selected_alignment_id() -> String:
	if _alignments.size() == 1:
		return String(_alignments[0].get("alignment_id", ""))
	if _alignment_choice.selected < 0:
		return ""
	return String(_alignment_choice.get_item_metadata(_alignment_choice.selected))


func _default_path() -> String:
	var documents := OS.get_system_dir(OS.SYSTEM_DIR_DOCUMENTS)
	var extension := "shp" if _selected_format() == FORMAT_SHAPEFILE else "geojson"
	return documents.path_join("corridor_envelope." + extension)


func _on_format_selected(_index: int) -> void:
	if not _path.text.strip_edges().is_empty():
		_path.text = normalized_path(_path.text.strip_edges(), _selected_format())


func _on_alignment_selected(_index: int) -> void:
	_refresh_station_defaults()
	_refresh_enabled()


func _refresh_station_defaults() -> void:
	var chosen := _selected_alignment_id()
	for entry in _alignments:
		if String(entry.get("alignment_id", "")) == chosen:
			var start_m := float(entry.get("station_start", 0.0))
			var end_m := float(entry.get("station_end", 0.0))
			# The exact range first: Range rounds a value to the step grid and then clamps to max, so the
			# alignment's own end survives as the "To" default even though it is not a multiple of the step.
			for box in [_station_from, _station_to]:
				box.min_value = start_m
				box.max_value = end_m
			_station_from.value = start_m
			_station_to.value = end_m
			return
	for box in [_station_from, _station_to]:
		box.min_value = -1.0e7
		box.max_value = 1.0e7
	_station_from.value = 0.0
	_station_to.value = 0.0


func _refresh_enabled() -> void:
	var single := not _selected_alignment_id().is_empty()
	_station_check.visible = true
	if not single:
		_station_check.button_pressed = false
	_station_check.disabled = not single
	_station_check.tooltip_text = "" if single else "A station range applies to one alignment: choose it above."
	var ranged := _station_check.button_pressed and single
	_station_from.editable = ranged
	_station_to.editable = ranged


func _on_browse_pressed() -> void:
	var current := _path.text.strip_edges()
	if not current.is_empty():
		_file_dialog.current_dir = current.get_base_dir()
		_file_dialog.current_file = current.get_file()
	_file_dialog.clear_filters()
	if _selected_format() == FORMAT_SHAPEFILE:
		_file_dialog.add_filter("*.shp", "Shapefile")
	else:
		_file_dialog.add_filter("*.geojson, *.json", "GeoJSON")
	_file_dialog.popup_centered_ratio(0.6)


func _on_file_picked(picked: String) -> void:
	_path.text = normalized_path(picked, _selected_format())


func _show_error(sentence: String) -> void:
	_error.text = sentence
	_error.visible = not sentence.is_empty()
	reset_size()


func _set_busy(busy: bool) -> void:
	_busy = busy
	get_ok_button().disabled = busy
	get_ok_button().text = "Exporting…" if busy else "Export"
