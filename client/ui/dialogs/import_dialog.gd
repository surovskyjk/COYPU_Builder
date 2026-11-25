class_name ImportDialog
extends ConfirmationDialog
## "Import terrain or imagery..." (T-136 Part B): the five steps of ADR 0009, source, inspect, settings, import and
## result. The backend reads and checks each file (`import.inspect`) and imports it with exactly the settings the user
## confirmed (`import.run`); the client only shows the reports and sends what was entered. It never parses a data file.
##
## Rules this dialog enforces:
## - Every request is a coroutine that the UI awaits; nothing blocks. An `E_BUSY` reply is retried with backoff
##   ([method busy_backoff_sec]) through the [member wait] seam, at most [constant MAX_BUSY_RETRIES] times.
## - A setting the user did not touch is sent as the backend suggested it, `null` included. The settings dictionary
##   is kept whole and each field writes only its own key. A number is never rounded, snapped or clamped on the way
##   through: every number is a [NumberField], which keeps the exact float.
## - The primary button is disabled only with a stated reason ([method reason_for_step]), shown as its tooltip and in
##   the hint line. Enter in the name field, or in a chosen CRS, presses the enabled primary button; Enter in a number
##   only confirms it.
## - The window fits the screen: after each step change it is resized to its contents and centred, never taller than
##   [constant SCREEN_SHARE] of the screen. Long reports and settings scroll.
## - A closed window does not stop an import: its outcome is reported in the status bar ([signal EventBus.status_message]).
## - Every failure is one plain sentence ([method plain_error]); raw backend codes never reach the user.

const STEP_SOURCE := 1
const STEP_INSPECT := 2
const STEP_SETTINGS := 3
const STEP_IMPORT := 4
const STEP_RESULT := 5
const STEP_NAMES: Array[String] = ["Source", "Inspect", "Settings", "Import", "Result"]

const ORDER_CHOICES: Array[String] = ["auto", "EN", "NE"]
const ORDER_TITLES: Array[String] = ["Auto", "East-North", "North-East"]
const CLIP_CORRIDOR := 0
const CLIP_BBOX := 1
const CLIP_NONE := 2
const CLIP_TITLES: Array[String] = ["Corridor", "Bounding box", "None"]
const KIND_CHOICES: Array[String] = ["terrain", "imagery"]
const KIND_TITLES: Array[String] = ["Terrain", "Imagery"]
## "Import as" on step 1: the empty kind is Detect, which sends no `kind` to `import.inspect`.
const IMPORT_AS_CHOICES: Array[String] = ["", "terrain", "imagery"]
const IMPORT_AS_TITLES: Array[String] = ["Detect from the files", "Terrain (heights)", "Imagery (orthophoto)"]
const IMPORT_AS_NOTE := "Detect reads the files and finds their kind. Choose a kind only when that is wrong."
const KIND_ONE_READER_NOTE := "This reader takes only one kind. To import these files as the other kind, go back to the Source step."

const MAX_BUSY_RETRIES := 8
const BUSY_BACKOFF_START_SEC := 0.5
const BUSY_BACKOFF_MAX_SEC := 4.0
const BUSY_RETRY_TEXT := "Backend busy, retrying…"
const INSPECT_TIMEOUT_SEC := 300.0
const IMPORT_TIMEOUT_SEC := 1800.0
const CORRIDOR_DEFAULT_M := 250.0
const UNIT_SUFFIX := "m"
const RESOLUTION_DEFAULT_M := 1.0
const NODATA_DEFAULT := -9999.0
const NO_CRS_NOTICE := "No coordinate system declared — choose one in Settings"
const NO_PROJECT_REASON := "Open or create a project first — imported data belongs to a project"
const NO_SOURCE_REASON := "Choose one or more files, or an online source, first"
const NO_CRS_PROJECT_TEXT := "The project has no coordinate system yet — import an alignment first"
const ONLINE_ALIGNMENT_REASON := "An online source is cut to a corridor around an alignment — import an alignment first"
const NO_EARLIER_SOURCE_REASON := "No earlier source of this kind and format"
const NOTHING_CHOSEN := "Nothing chosen yet."
const PROVIDERS_FAILED_TEXT := "The online sources could not be listed — the backend did not answer."
const PROVIDERS_NONE_TEXT := "The backend lists no online sources."
const NODATA_REASON := "Enter a number for no data"
const TIMEOUT_IMPORT_TEXT := "The import is taking longer than expected — it may still finish; check the data sources before importing again"
const NOT_STATED := "Not stated"
## The backend's transformation for a source already in the project's CRS (`io/sources/runner.py`): none was applied.
const NO_TRANSFORMATION := "none"
const SCREEN_SHARE := 0.9
## The least a scrolling page part is given, however little room the screen leaves.
const PAGE_MIN_PX := 120
## The most fit passes one fit makes, a frame apart: the first measures, a second settles the wrapped labels.
const PAGE_SETTLE_PASSES := 3
const SETTINGS_PAGE_WIDTH := 740
## The title of an online source, in the size of the theme's panel titles, so it reads above its muted description.
const PROVIDER_TITLE_PX := 15
## The unchecked radio of an online source: a ring in this grey, which shows on the light and the dark theme alike (the
## theme's own radio is dark, so it disappears on the dark one).
const RADIO_RING_COLOUR := Color(0.6039, 0.6549, 0.7098)
const RADIO_RING_PX := 18
const LABEL_COLUMN_PX := 170
const WRAP_MIN_PX := 320
const NUMBER_MIN_PX := 120
const NODATA_TEXT_MIN_PX := 320
const UNIT_REASON := "The linear unit must be above 0"
const BUFFER_REASON := "The buffer must be above 0"
## The largest buffer the client accepts, as `domain/sources.py` does.
const BUFFER_MAX_M := 5000.0
const BUFFER_MAX_REASON := "The buffer must be at most 5000 m"
const RESOLUTION_REASON := "The display resolution must be above 0"
const MAX_TOTAL_REASON := "The download limit must be above 0 MB"
const ATTRIBUTION_HINT := "Suggested from the file names; check it."
const ATTRIBUTION_NOTE := "Shown in Data sources."
const LICENCE_NOTE := "The terms the data is shared under."
const CLIP_NOTE := "The part of the file that is imported. None imports everything the file holds."
## The backend's placeholder for a source whose licence is not known (`io/sources/runner.py: UNKNOWN_LICENCE`). It is
## shown as an empty field, so it is never sent back as a licence.
const UNKNOWN_LICENCE := "unknown"
const NO_RE_IMPORT := "— none —"
const RE_IMPORT_NOTE := "Choose a source to refresh it: objects that match keep their identifiers, and the source keeps its id."
const PROVIDER_KIND_NOTE := "The online source supplies this kind of data."
const PROVIDER_CLIP_NOTE := "An online source is always clipped: a corridor or a box, not the whole file."
const DOWNLOAD_NONE_NOTE := "The inspection did not count the download."
const NODATA_NOTE := "Rasters only: cells that hold this value have no height."
const BOX_CAPTIONS: Array[String] = ["Min E", "Min N", "Max E", "Max N"]
const FORMAT_LABELS := {
	"las": "LAS point cloud",
	"geotiff": "GeoTIFF raster",
	"landxml-surface": "LandXML surface",
	"raster-image": "Georeferenced image",
	"wmts-descriptor": "ČÚZK orthophoto service (.wmts)",
	"cuzk-ortho-sheet": "ČÚZK orthophoto sheet",
}
## The standard classification names of the LAS 1.4 specification (ASPRS). These are display labels only. Codes 8 and 12
## are reserved in LAS 1.4 and have no name, so they show only their number.
const CLASS_NAMES := {
	0: "Created, never classified",
	1: "Unclassified",
	2: "Ground",
	3: "Low Vegetation",
	4: "Medium Vegetation",
	5: "High Vegetation",
	6: "Building",
	7: "Low Point (noise)",
	9: "Water",
	10: "Rail",
	11: "Road Surface",
	13: "Wire - Guard (Shield)",
	14: "Wire - Conductor (Phase)",
	15: "Transmission Tower",
	16: "Wire-structure Connector",
	17: "Bridge Deck",
	18: "High Noise",
}

## `func(method: String, params: Dictionary) -> IpcEnvelope` (a coroutine). Tests replace it with a fake backend; the
## two pickers get the same callable.
var requester: Callable = _backend_request:
	set(value):
		requester = value
		_share_requester()

## `func(seconds: float)` (a coroutine): the backoff between retries. Tests record the calls instead of sleeping.
var wait: Callable = _pause

## The rectangle the window must fit. Empty means the root viewport's visible rectangle; the tests set 1280 x 720.
var screen_rect := Rect2i()

var _alignments: Array[Dictionary] = []
var _readers: Array[Dictionary] = []
var _files := PackedStringArray()
var _step := STEP_SOURCE
var _report: Dictionary = {}
var _inspect_error := ""
var _source_error := ""
var _settings: Dictionary = {}
var _suggested_classes: Variant = null
var _initial_ticks: Array[int] = []
var _format_id := ""
var _kind := "terrain"
var _auto_name := ""
var _busy := false
var _import_running := false
var _busy_text := ""
var _elapsed_sec := 0.0
var _settings_error := ""
var _outcome_unseen := false
var _result: Dictionary = {}
var _result_licence := ""
var _result_attribution := ""
var _populating := false
var _bbox_edited := false
var _nodata_suggested: Variant = null
var _nodata_edited := false
var _fit_queued := false
## The scrolling page parts (report, Settings, result), whose heights [method _fit_now] sets from the screen.
var _scroll_pages: Array[ScrollContainer] = []
## The online sources of `import.formats`, and the one chosen ("" when the source is files).
var _providers: Array[Dictionary] = []
var _provider_id := ""
var _provider_title := ""
var _provider_query: Dictionary = {}
## The existing sources of this kind and format (`source.list`), and the re-import chosen from them ("" for none).
var _sources: Array[Dictionary] = []
var _reimport_of := ""
## Set when the user types in a field: a typed value wins over the prefill of a re-import or a hint.
var _attribution_edited := false
var _licence_edited := false
## Set when the user types in Name: a typed name wins over the re-import source's name and the automatic one.
var _name_edited := false
## Set when `import.formats` failed, so the online list says it could not be read rather than that none exist.
var _providers_failed := false
## The one radio group of the online list: one source is chosen at a time.
var _provider_group := ButtonGroup.new()

var _column: VBoxContainer
var _pages: Array[Control] = []
var _step_labels: Array[Label] = []
var _hint: Label
var _back: Button
var _online: Button
var _import_as: OptionButton
var _file_dialog: FileDialog
var _files_label: Label
var _source_error_label: Label
var _inspect_busy: Label
var _report_box: VBoxContainer
var _import_busy: Label
var _result_box: VBoxContainer
var _settings_error_label: Label
var _horizontal: CrsPicker
var _vertical: CrsPicker
var _order: OptionButton
var _order_note: Label
var _unit: NumberField
var _clip: OptionButton
var _corridor_box: VBoxContainer
var _buffer: NumberField
var _row_station: HBoxContainer
var _station_check: CheckBox
var _station_from: NumberField
var _station_to: NumberField
var _bbox_box: VBoxContainer
var _bbox: Array[NumberField] = []
var _bbox_note: Label
var _row_classes: HBoxContainer
var _class_box: VBoxContainer
var _class_checks: Array[CheckBox] = []
var _row_resolution: HBoxContainer
var _resolution_check: CheckBox
var _resolution: NumberField
var _row_nodata: HBoxContainer
var _nodata_check: CheckBox
var _nodata: NumberField
var _kind_choice: OptionButton
var _kind_text: Label
var _kind_note: Label
var _name_field: LineEdit
var _import_as_row: HBoxContainer
var _provider_scroll: ScrollContainer
var _provider_box: VBoxContainer
## Provider id -> its toggle button, and -> the notices shown while it is the chosen one.
var _provider_rows: Dictionary = {}
var _provider_notices: Dictionary = {}
var _reimport: OptionButton
var _reimport_row: HBoxContainer
var _attribution: LineEdit
var _attribution_row: HBoxContainer
var _licence: LineEdit
var _licence_row: HBoxContainer
var _max_total: NumberField
var _row_max_total: HBoxContainer
var _max_total_note: Label
var _clip_note: Label
var _reimport_note: Label
var _attribution_note: Label
var _licence_note: Label


func _init() -> void:
	title = "Import terrain or imagery"
	ok_button_text = "Next"
	cancel_button_text = "Cancel"
	dialog_hide_on_ok = false
	min_size = Vector2i(780, 0)
	exclusive = true
	set_process(false)
	_build()
	confirmed.connect(_on_confirmed)
	custom_action.connect(_on_custom_action)
	EventBus.project_changed.connect(_refresh_buttons)


func _process(delta: float) -> void:
	_elapsed_sec += delta
	_update_busy_labels()


# --- static helpers (pure; unit-tested) ------------------------------------------------------------------------

## Backoff before the retry after the `attempt`-th (0-based) `E_BUSY`: 0.5 s, doubling, capped at 4 s.
static func busy_backoff_sec(attempt: int) -> float:
	return minf(BUSY_BACKOFF_MAX_SEC, BUSY_BACKOFF_START_SEC * pow(2.0, attempt))


## One plain sentence, with the next step, for a backend error. The raw message is used only for the one case
## whose text the backend wrote for the user (`E_BAD_PARAMS`).
static func plain_error(code: String, message: String) -> String:
	match code:
		"E_CRS_REQUIRED":
			return "This file declares no coordinate system — choose one"
		"E_BAD_PARAMS":
			if message.strip_edges().is_empty():
				return "The backend did not accept those settings — check them and try again."
			return message.strip_edges()
		"E_NO_PROJECT":
			return "Open or create a project first"
		"E_NOT_FOUND":
			return "A file could not be found — choose it again"
		"E_EMPTY":
			return "Nothing of this data lies inside the clip — widen the clip or choose none"
		"E_BUSY":
			return "The backend stayed busy — try again in a moment."
		"E_INTERNAL":
			return "Something went wrong in the backend — the log has the details"
		"E_DISCONNECTED":
			return "The backend is not running — wait for it to reconnect, then try again"
		"E_TIMEOUT", "E_CLIENT_TIMEOUT":
			return "The backend did not answer in time — try again, or import fewer files"
	return "The import failed — try again; if it repeats, check the backend log"


## The label of a reader's format for people, falling back to its id in capitals.
static func format_label(format_id: String) -> String:
	if FORMAT_LABELS.has(format_id):
		return String(FORMAT_LABELS[format_id])
	return format_id.to_upper()


## What "Auto" means for a format: LandXML is read as North, East; the other readers as East, North.
static func auto_order_text(format_id: String) -> String:
	var order := "North, East" if format_id.begins_with("landxml") else "East, North"
	return "Auto reads %s as %s." % [format_label(format_id), order]


## A point class for people: its code and its standard name, or only its code when the name is not standard.
static func class_label(code: int) -> String:
	if CLASS_NAMES.has(code):
		return "%d %s" % [code, String(CLASS_NAMES[code])]
	return str(code)


## The extent as E and N ranges. It has no unit: the file's own coordinate system decides it (metres or degrees).
## Values of 1,000 or more show one decimal; smaller ones (degrees) show six.
static func extent_text(extent: Variant) -> String:
	if not (extent is Array) or (extent as Array).size() != 4:
		return "Not known"
	var values: Array = extent
	return "E %s to %s, N %s to %s" % [
		_coordinate_text(float(values[0])), _coordinate_text(float(values[2])),
		_coordinate_text(float(values[1])), _coordinate_text(float(values[3])),
	]


## The status-bar line for a successful import.
static func summary_of(result: Dictionary, kind: String) -> String:
	var added := int(result.get("added", 0))
	return "Imported 1 %s source \"%s\" (%d %s added)" % [
		kind, String(result.get("name", "")), added, "object" if added == 1 else "objects",
	]


## A copy of the backend's suggested settings with every key present and the types the wire expects: point classes
## are whole numbers (JSON numbers arrive as floats), and the optional numbers are `null` when unset.
static func normalised_settings(raw: Dictionary) -> Dictionary:
	return {
		"horizontal_crs": _text_or_null(raw.get("horizontal_crs")),
		"vertical_crs": _text_or_null(raw.get("vertical_crs")),
		"coordinate_order": String(raw.get("coordinate_order", "auto")),
		"linear_unit_m": float(raw.get("linear_unit_m", 1.0)),
		"clip": raw.get("clip"),
		"point_classes": _whole_numbers_or_null(raw.get("point_classes")),
		"display_resolution_m": _number_or_null(raw.get("display_resolution_m")),
		"nodata": _number_or_null(raw.get("nodata")),
	}


static func _coordinate_text(value: float) -> String:
	return "%.1f" % value if absf(value) >= 1000.0 else "%.6f" % value


## A licence as people read it: the backend's placeholder for an unknown licence is no licence at all.
static func _known_licence(licence: String) -> String:
	return "" if licence == UNKNOWN_LICENCE else licence


static func _text_or_null(value: Variant) -> Variant:
	if value == null or String(value).is_empty():
		return null
	return String(value)


static func _number_or_null(value: Variant) -> Variant:
	if value == null:
		return null
	return float(value)


static func _whole_numbers_or_null(value: Variant) -> Variant:
	if not (value is Array):
		return null
	var codes: Array[int] = []
	for code: Variant in value:
		codes.append(int(code))
	return codes


# --- accessors for the tests and the capture driver --------------------------------------------------------------

## The step shown now, 1 to 5 (see [constant STEP_NAMES]).
func step() -> int:
	return _step


func primary_button() -> Button:
	return get_ok_button()


func back_button() -> Button:
	return _back


func hint_label() -> Label:
	return _hint


func online_button() -> Button:
	return _online


func import_as_choice() -> OptionButton:
	return _import_as


func horizontal_picker() -> CrsPicker:
	return _horizontal


func vertical_picker() -> CrsPicker:
	return _vertical


func order_choice() -> OptionButton:
	return _order


func unit_field() -> NumberField:
	return _unit


func clip_choice() -> OptionButton:
	return _clip


func corridor_box() -> Control:
	return _corridor_box


func bbox_box() -> Control:
	return _bbox_box


func buffer_field() -> NumberField:
	return _buffer


func station_row() -> Control:
	return _row_station


func station_check() -> CheckBox:
	return _station_check


func station_from_field() -> NumberField:
	return _station_from


func station_to_field() -> NumberField:
	return _station_to


## The four bounding-box fields in the order min E, min N, max E, max N.
func bbox_fields() -> Array[NumberField]:
	return _bbox


func class_checks() -> Array[CheckBox]:
	return _class_checks


func classes_row() -> Control:
	return _row_classes


func resolution_row() -> Control:
	return _row_resolution


func resolution_check() -> CheckBox:
	return _resolution_check


func resolution_field() -> NumberField:
	return _resolution


func nodata_row() -> Control:
	return _row_nodata


func nodata_check() -> CheckBox:
	return _nodata_check


## The no-data value: the exact float, edited by hand when the user types.
func nodata_field() -> NumberField:
	return _nodata


func kind_choice() -> OptionButton:
	return _kind_choice


## The kind as text, shown in place of the choice where the reader takes only one kind.
func kind_text() -> Label:
	return _kind_text


func name_field() -> LineEdit:
	return _name_field


## The kind the source is imported as: the report's, or the online source's.
func kind() -> String:
	return _kind


## The chosen online source's id, or "" when the source is files.
func provider_id() -> String:
	return _provider_id


## The query the online source is inspected with: its default query, with the download limit as the user has set it. The
## clip is merged in only when the import runs, from the Settings clip ([method _run_query]).
func provider_query() -> Dictionary:
	return _provider_query.duplicate(true)


func import_as_row() -> Control:
	return _import_as_row


## The toggle that chooses online source `provider_id` on the source step, or null when there is no such source.
func provider_button(provider_id_text: String) -> Button:
	return _provider_rows.get(provider_id_text) as Button


func provider_scroll() -> Control:
	return _provider_scroll


## How many online sources `import.formats` listed. Zero when the list failed or was empty.
func provider_count() -> int:
	return _providers.size()


## Whether the notices of online source `provider_id_text` are shown: true only while it is the chosen one.
func provider_notices_visible(provider_id_text: String) -> bool:
	var notices: Variant = _provider_notices.get(provider_id_text)
	return notices is Control and (notices as Control).visible


## The text of every label in the online list, in order: the sources' descriptions and licences, or the one sentence that
## says the list is empty or could not be read.
func provider_list_texts() -> PackedStringArray:
	var texts := PackedStringArray()
	_collect_texts(_provider_box, texts)
	return texts


func clip_note() -> Label:
	return _clip_note


func reimport_note() -> Label:
	return _reimport_note


func attribution_note() -> Label:
	return _attribution_note


func licence_note() -> Label:
	return _licence_note


## The Source step's line under its buttons: the chosen files or online source, or "Nothing chosen yet.".
func source_summary() -> String:
	return _files_label.text


func reimport_choice() -> OptionButton:
	return _reimport


func reimport_row() -> Control:
	return _reimport_row


func attribution_field() -> LineEdit:
	return _attribution


func attribution_row() -> Control:
	return _attribution_row


func licence_field() -> LineEdit:
	return _licence


func licence_row() -> Control:
	return _licence_row


## The download limit of an online source (`max_total_mb`), in MB.
func max_total_field() -> NumberField:
	return _max_total


func max_total_row() -> Control:
	return _row_max_total


func max_total_note() -> Label:
	return _max_total_note


func is_busy() -> bool:
	return _busy or _import_running


## The text of every label in the inspection report, in order (the no-CRS notice among them).
func report_texts() -> PackedStringArray:
	var texts := PackedStringArray()
	_collect_texts(_report_box, texts)
	return texts


## The text of every label on the result page, in order.
func result_texts() -> PackedStringArray:
	var texts := PackedStringArray()
	_collect_texts(_result_box, texts)
	return texts


## The one sentence the dialog shows for the failure of the current step, or "".
func error_text() -> String:
	if not _settings_error.is_empty():
		return _settings_error
	if not _inspect_error.is_empty():
		return _inspect_error
	return _source_error


## Whether the Settings page shows its error line.
func settings_error_shown() -> bool:
	return _settings_error_label.visible


## The settings as they would be sent now.
func settings_snapshot() -> Dictionary:
	return _settings.duplicate(true)


## The file filters of the native dialog: one per reader, then "All supported", from `import.formats`.
func file_filters() -> PackedStringArray:
	var filters := PackedStringArray()
	var every := PackedStringArray()
	for reader in _readers:
		var patterns := PackedStringArray()
		var suffixes: Array = reader.get("suffixes", [])
		for suffix: Variant in suffixes:
			var pattern := "*" + String(suffix)
			patterns.append(pattern)
			if not every.has(pattern):
				every.append(pattern)
		if not patterns.is_empty():
			filters.append("%s ; %s" % [", ".join(patterns), format_label(String(reader.get("format_id", "")))])
	if not every.is_empty():
		filters.insert(0, "%s ; All supported" % ", ".join(every))
	return filters


## Chooses the files (the native dialog's `files_selected` calls this). Paths are absolute. A different list clears the
## report, so the next Next inspects again; the same list keeps it.
func set_files(paths: PackedStringArray) -> void:
	if paths != _files or not _provider_id.is_empty():
		_clear_report()
	_provider_id = ""
	_provider_title = ""
	_provider_query = {}
	_files = paths.duplicate()
	_source_error = ""
	_source_error_label.text = ""
	_source_error_label.visible = false
	_refresh_source_page()


## Chooses online source `provider_id` from `import.formats` (the toggle of its row does the same). Its kind and default
## query are taken from the source, and any files or report it replaces are dropped. Choosing the source already chosen
## changes nothing.
func choose_provider(provider_id_text: String) -> void:
	var info := _provider_info(provider_id_text)
	if info.is_empty() or provider_id_text == _provider_id:
		_refresh_provider_rows()
		return
	_clear_report()
	_provider_id = provider_id_text
	_provider_title = String(info.get("title", provider_id_text))
	var default_query: Variant = info.get("default_query", {})
	_provider_query = (default_query as Dictionary).duplicate(true) if default_query is Dictionary else {}
	var kinds: Array = info.get("kinds", [])
	_kind = String(kinds[0]) if not kinds.is_empty() else "terrain"
	_files = PackedStringArray()
	_source_error = ""
	_source_error_label.text = ""
	_source_error_label.visible = false
	_refresh_source_page()


## Shows the list of online sources (the Source step's "Online source" button does the same).
func show_online_sources() -> void:
	_provider_scroll.visible = true
	_fit_later()


## Chooses the existing source at `index` of the re-import selector (0 is "— none —") and prefills the attribution and the
## licence from it, unless the user has typed in them.
func choose_reimport(index: int) -> void:
	var choice := clampi(index, 0, _sources.size())
	_reimport.select(choice)
	_reimport_of = String(_sources[choice - 1].get("id", "")) if choice > 0 else ""
	_prefill_name()
	_prefill_text_fields()
	_refresh_buttons()


## Chooses the clip at `index` of the Clip selector, as the user would.
func choose_clip(index: int) -> void:
	_clip.select(index)
	_on_clip_selected(index)


## Shows the Source step's controls for the current source: the file names or the online source's title, and the Import as
## choice, which an online source does not take.
func _refresh_source_page() -> void:
	_files_label.text = _describe_source()
	_files_label.tooltip_text = "\n".join(_files)
	_import_as_row.visible = _provider_id.is_empty()
	_refresh_provider_rows()
	_refresh_buttons()
	_fit_later()


## Why the primary button of `step` is disabled, or "" when it is enabled.
func reason_for_step(step_number: int) -> String:
	match step_number:
		STEP_SOURCE:
			if Session.project_id().is_empty():
				return NO_PROJECT_REASON
			if Session.crs().is_empty():
				return NO_CRS_PROJECT_TEXT
			if not _provider_id.is_empty():
				# An online source is cut to a corridor, which needs an alignment (no box is offered for that case).
				return "" if not _alignments.is_empty() else ONLINE_ALIGNMENT_REASON
			return "" if not _files.is_empty() else NO_SOURCE_REASON
		STEP_INSPECT:
			if _busy:
				return "The inspection is still running"
			if _report.is_empty():
				return "The inspection failed — go back and choose other files"
		STEP_SETTINGS:
			if _text_of(_settings, "horizontal_crs").is_empty():
				return "Choose a coordinate system for this data"
			if not _class_checks.is_empty() and _checked_classes().is_empty():
				return "Keep at least one point class"
			var blocked := _number_reason()
			if not blocked.is_empty():
				return blocked
			if _clip.selected == CLIP_BBOX and not _bbox_is_valid():
				return "Enter a box whose minimum is below its maximum"
			if _station_range_active() and _station_from.value() >= _station_to.value():
				return "Enter a station range whose From is smaller than To"
		STEP_IMPORT:
			return "The import is running"
	return ""


## The reason of the first numeric setting that the settings would send and that is not a number in range, or "". Only
## the fields in use are checked: the buffer under a corridor, the station range when it is on, the box under a box, the
## resolution when it is set, and the no-data value when it is ticked.
func _number_reason() -> String:
	var reason := number_reason(_unit, "the linear unit", UNIT_REASON)
	if reason.is_empty() and _clip.selected == CLIP_CORRIDOR:
		reason = _buffer_reason()
	if reason.is_empty() and _station_range_active():
		reason = number_reason(_station_from, "the station From", "")
	if reason.is_empty() and _station_range_active():
		reason = number_reason(_station_to, "the station To", "")
	if reason.is_empty() and _clip.selected == CLIP_BBOX:
		for index in 4:
			reason = number_reason(_bbox[index], BOX_CAPTIONS[index], "")
			if not reason.is_empty():
				break
	if reason.is_empty() and _row_resolution.visible and _resolution_check.button_pressed:
		reason = number_reason(_resolution, "the display resolution", RESOLUTION_REASON)
	if reason.is_empty() and _nodata_check.button_pressed:
		reason = number_reason(_nodata, "no data", NODATA_REASON)
	if reason.is_empty() and _row_max_total.visible:
		reason = number_reason(_max_total, "the download limit", MAX_TOTAL_REASON)
	return reason


## The reason the buffer blocks Import: the maximum when the typed number is above it, the minimum rule otherwise.
func _buffer_reason() -> String:
	if _buffer.is_valid():
		return ""
	if _buffer.is_number() and _buffer.text.strip_edges().to_float() > BUFFER_MAX_M:
		return BUFFER_MAX_REASON
	return number_reason(_buffer, "the buffer", BUFFER_REASON)


## The reason one numeric field blocks Import: `bound_text` when the text is a number outside its bounds, otherwise
## "Enter a number for" the field's name. "" when the field holds a value.
static func number_reason(field: NumberField, name_text: String, bound_text: String) -> String:
	if field.is_valid():
		return ""
	if field.is_number() and not bound_text.is_empty():
		return bound_text
	return "Enter a number for %s" % name_text


## Opens the dialog on the first step, with the file filters loaded from the backend.
func open(alignments: Array[Dictionary]) -> void:
	prepare(alignments)
	popup_centered()
	_fit_later()


## [method open] without showing the window (tests, and the capture driver which shows it itself).
func prepare(alignments: Array[Dictionary]) -> void:
	_alignments = alignments
	_load_formats()
	if _busy or _import_running:
		_enter_step(_step)
	elif _outcome_unseen:
		_outcome_unseen = false
	else:
		_enter_step(STEP_SOURCE)
	_refresh_buttons()


## Coroutine, the primary button: step 1 inspects the files (or goes on with the report it has), step 2 opens Settings,
## step 3 starts the import, and step 5 closes the window. A disabled step does nothing.
func go_next() -> void:
	if _step == STEP_RESULT:
		hide()
		return
	if not reason_for_step(_step).is_empty():
		return
	match _step:
		STEP_SOURCE:
			if _report.is_empty():
				await _inspect()
			else:
				_enter_step(STEP_INSPECT)
		STEP_INSPECT:
			_enter_step(STEP_SETTINGS)
		STEP_SETTINGS:
			await _run_import()


## The Back button: from Inspect to Source, from Settings to Inspect. What was entered is kept.
func go_back() -> void:
	if _busy or _import_running:
		return
	if _step == STEP_INSPECT:
		_enter_step(STEP_SOURCE)
	elif _step == STEP_SETTINGS:
		_enter_step(STEP_INSPECT)
	_refresh_buttons()


# --- requests -----------------------------------------------------------------------------------------------------

func _backend_request(method: String, params: Dictionary) -> IpcEnvelope:
	return await Backend.request(method, params, request_timeout_sec(method))


## How long the default requester waits for a reply to `method`: an import may take half an hour, an inspection five
## minutes, and every other method the client's default. The backend answers pings meanwhile (ADR 0003 addendum), so a
## long wait is not a dead connection.
static func request_timeout_sec(method: String) -> float:
	match method:
		"import.run":
			return IMPORT_TIMEOUT_SEC
		"import.inspect":
			return INSPECT_TIMEOUT_SEC
	return Backend.REQUEST_TIMEOUT_SEC


func _pause(seconds: float) -> void:
	await get_tree().create_timer(seconds).timeout


func _share_requester() -> void:
	if _horizontal != null:
		_horizontal.requester = requester
	if _vertical != null:
		_vertical.requester = requester


func _load_formats() -> void:
	var reply: IpcEnvelope = await requester.call("import.formats", {})
	_providers.clear()
	if reply.type != "res":
		_providers_failed = true
		_rebuild_provider_rows()
		return
	_providers_failed = false
	_readers.clear()
	var readers: Array = reply.result.get("readers", [])
	for entry: Variant in readers:
		if entry is Dictionary:
			_readers.append(entry as Dictionary)
	var providers: Array = reply.result.get("providers", [])
	for entry: Variant in providers:
		if entry is Dictionary:
			_providers.append(entry as Dictionary)
	_file_dialog.filters = file_filters()
	_rebuild_provider_rows()


## Coroutine: `import.inspect` for the chosen files, with a busy indicator, retries and the report. Inspecting clears the
## Settings error of an earlier import.
func _inspect() -> void:
	_enter_step(STEP_INSPECT)
	_clear_settings_error()
	var busy_text := "Inspecting the online source…" if not _provider_id.is_empty() else "Inspecting the files…"
	_set_busy(true, busy_text)
	var reply: IpcEnvelope = await _request_with_retry("import.inspect", _inspect_params(), busy_text)
	if reply.type != "res":
		_set_busy(false, "")
		_report = {}
		_inspect_error = _error_sentence(_code_of(reply), _message_of(reply))
		_render_report()
		_refresh_buttons()
		return
	_format_id = String(reply.result.get("format_id", ""))
	_kind = String(reply.result.get("kind", "terrain"))
	# The existing sources are listed while the dialog still counts as busy, so Next cannot pass them by.
	await _load_sources()
	_set_busy(false, "")
	_inspect_error = ""
	_report = reply.result
	_auto_name = _provider_title if not _provider_id.is_empty() else _files[0].get_file().get_basename()
	_attribution_edited = false
	_licence_edited = false
	var suggested: Dictionary = reply.result.get("suggested_settings", {})
	_settings = normalised_settings(suggested)
	_suggested_classes = _settings["point_classes"]
	_populate_settings()
	_render_report()
	_refresh_buttons()


## What `import.inspect` is asked: the online source and its query, or the files, with the kind when the user chose one.
func _inspect_params() -> Dictionary:
	if not _provider_id.is_empty():
		return {"provider": _provider_id, "query": _provider_query.duplicate(true)}
	var params := {"paths": _path_list()}
	var kind := _import_as_kind()
	if not kind.is_empty():
		params["kind"] = kind
	return params


## Coroutine: the existing sources of this kind and format (`source.list`) for the re-import selector. A failed list leaves
## the selector at "— none —"; a re-import is never required.
func _load_sources() -> void:
	_sources.clear()
	var reply: IpcEnvelope = await _request_with_retry("source.list", {}, _busy_text)
	if reply.type == "res":
		for entry: Variant in reply.result.get("sources", []):
			if entry is Dictionary and _is_reimport_candidate(entry as Dictionary):
				_sources.append(entry as Dictionary)
	_reimport_of = ""
	_reimport.clear()
	_reimport.add_item(NO_RE_IMPORT)
	for source in _sources:
		var objects := int(source.get("object_count", 0))
		_reimport.add_item("%s · %d %s" % [String(source.get("name", "")), objects, "object" if objects == 1 else "objects"])
	_reimport.select(0)
	_reimport.disabled = _sources.is_empty()
	_reimport_note.text = NO_EARLIER_SOURCE_REASON if _sources.is_empty() else RE_IMPORT_NOTE


## Whether a listed source can be re-imported by this import: the same kind and the same format.
func _is_reimport_candidate(source: Dictionary) -> bool:
	return String(source.get("kind", "")) == _kind and String(source.get("format_id", "")) == _format_id


## Coroutine: `import.run` with the settings exactly as they stand, then the outcome.
func _run_import() -> void:
	var params := _import_params()
	_clear_settings_error()
	_import_running = true
	_enter_step(STEP_IMPORT)
	_set_busy(true, "Importing…")
	var reply: IpcEnvelope = await _request_with_retry("import.run", params, "Importing…")
	_set_busy(false, "")
	# The flag stays up until the outcome is shown, source lookup included, so a window reopened meanwhile shows step 4.
	if reply.type != "res":
		_fail_import(reply)
	else:
		await _finish_import(reply.result)
	_import_running = false
	_refresh_buttons()


## Coroutine: a request that waits out `E_BUSY` with [method busy_backoff_sec], at most [constant MAX_BUSY_RETRIES]
## times. `E_BUSY` is answered before the backend does any work, so the retry is safe.
func _request_with_retry(method: String, params: Dictionary, busy_text: String) -> IpcEnvelope:
	var reply: IpcEnvelope = await requester.call(method, params)
	var retries := 0
	while _is_busy_reply(reply) and retries < MAX_BUSY_RETRIES:
		_busy_text = BUSY_RETRY_TEXT
		_update_busy_labels()
		await wait.call(busy_backoff_sec(retries))
		retries += 1
		_busy_text = busy_text
		_update_busy_labels()
		reply = await requester.call(method, params)
	return reply


func _fail_import(reply: IpcEnvelope) -> void:
	var code := _code_of(reply)
	var sentence := _error_sentence(code, _message_of(reply))
	if code == "E_TIMEOUT":
		# The backend may still be importing: the sources may change, so the terrain lists them again.
		sentence = TIMEOUT_IMPORT_TEXT
		EventBus.sources_changed.emit(_kind)
	if code == "E_NOT_FOUND" or _is_project_crs_error(code):
		# Neither is fixed in Settings: a file that is gone is chosen again, and a project without a CRS needs an alignment.
		if code == "E_NOT_FOUND":
			_report = {}
		_source_error = sentence
		_source_error_label.text = sentence
		_source_error_label.visible = true
		_enter_step(STEP_SOURCE)
	else:
		_settings_error = sentence
		_settings_error_label.text = sentence
		_settings_error_label.visible = true
		_enter_step(STEP_SETTINGS)
		if code == "E_CRS_REQUIRED":
			_horizontal.focus()
	_outcome_unseen = not visible
	if not visible:
		EventBus.status_message.emit(sentence)


func _finish_import(result: Dictionary) -> void:
	_result = result
	_result_licence = _text_or_default(_report.get("licence_hint"), "")
	_result_attribution = ""
	var detail: IpcEnvelope = await requester.call("source.get", {"source_id": String(result.get("source_id", ""))})
	if detail.type == "res":
		_result_licence = _known_licence(String(detail.result.get("licence", _result_licence)))
		_result_attribution = String(detail.result.get("attribution", ""))
	EventBus.sources_changed.emit(_kind)
	_outcome_unseen = not visible
	_enter_step(STEP_RESULT)
	_render_result()
	# The report is spent: the next Next inspects again, so "Re-import of" lists the source this import has just made.
	_clear_report()
	if not visible:
		EventBus.status_message.emit(summary_of(result, _kind))


func _is_busy_reply(reply: IpcEnvelope) -> bool:
	return reply.type != "res" and _code_of(reply) == "E_BUSY"


func _code_of(reply: IpcEnvelope) -> String:
	return String(reply.error.get("code", ""))


func _message_of(reply: IpcEnvelope) -> String:
	return String(reply.error.get("message", ""))


## The sentence for a failed request: the project's sentence for the project's `E_CRS_REQUIRED`, else [method plain_error].
func _error_sentence(code: String, message: String) -> String:
	return NO_CRS_PROJECT_TEXT if _is_project_crs_error(code) else plain_error(code, message)


## Whether `code` is `E_CRS_REQUIRED` about the project: the project has no coordinate system, or a horizontal CRS is already
## chosen, so Settings cannot change the answer. Only a file with no CRS and no choice sends the user to Settings.
func _is_project_crs_error(code: String) -> bool:
	return code == "E_CRS_REQUIRED" and (Session.crs().is_empty() or not _text_of(_settings, "horizontal_crs").is_empty())


## What `import.run` is sent. The settings go exactly as they stand. A file import takes the format of its report and the
## typed attribution and licence, and an empty field is not sent (the backend then keeps the previous values). An online
## source takes its query, with the clip from the settings, and no file values (the backend refuses them for a provider).
func _import_params() -> Dictionary:
	var params := {"kind": _kind, "settings": _settings.duplicate(true)}
	if _provider_id.is_empty():
		params["paths"] = _path_list()
		params["format_id"] = _format_id
		var attribution := _attribution.text.strip_edges()
		if not attribution.is_empty():
			params["attribution"] = attribution
		var licence := _licence.text.strip_edges()
		if not licence.is_empty():
			params["licence"] = licence
	else:
		params["provider"] = _provider_id
		params["query"] = _run_query()
	var chosen_name := _name_field.text.strip_edges()
	if not chosen_name.is_empty():
		params["name"] = chosen_name
	if not _reimport_of.is_empty():
		params["reimport_of"] = _reimport_of
	return params


## The online source's query for the import: its default query and download limit, with the clip the settings hold, so the
## fetch selects what the settings say.
func _run_query() -> Dictionary:
	var query := _provider_query.duplicate(true)
	var clip: Variant = _settings.get("clip")
	if clip is Dictionary:
		query["clip"] = (clip as Dictionary).duplicate(true)
	return query


func _path_list() -> Array:
	var paths: Array = []
	for file in _files:
		paths.append(file)
	return paths


func _import_as_kind() -> String:
	return IMPORT_AS_CHOICES[_import_as.selected]


# --- steps --------------------------------------------------------------------------------------------------------

func _enter_step(step_number: int) -> void:
	_step = step_number
	for index in _pages.size():
		_pages[index].visible = index + 1 == step_number
	for index in _step_labels.size():
		_step_labels[index].theme_type_variation = &"PanelTitle" if index + 1 == step_number else &"MutedLabel"
	_update_busy_labels()
	_refresh_buttons()
	_fit_later()


func _refresh_buttons() -> void:
	var primary := get_ok_button()
	var reason := reason_for_step(_step)
	match _step:
		STEP_SETTINGS, STEP_IMPORT:
			primary.text = "Import"
		STEP_RESULT:
			primary.text = "Close"
		_:
			primary.text = "Next"
	primary.disabled = not reason.is_empty()
	primary.tooltip_text = reason
	var hint_shown := _hint.visible
	var hint_text := _hint.text
	_hint.text = reason
	_hint.visible = not reason.is_empty()
	# A hint line that appears or changes makes the window taller, so the window is fitted again.
	if _hint.visible != hint_shown or _hint.text != hint_text:
		_fit_later()
	_back.disabled = _busy or _import_running or _step == STEP_SOURCE or _step == STEP_RESULT
	# The Result step has one button, Close: Back and Cancel would both only close the window.
	_back.visible = _step != STEP_RESULT
	get_cancel_button().visible = _step != STEP_RESULT


func _set_busy(on: bool, text: String) -> void:
	_busy = on
	_busy_text = text
	_elapsed_sec = 0.0
	set_process(on)
	_update_busy_labels()
	_refresh_buttons()


func _update_busy_labels() -> void:
	var shown := "%s  %d s" % [_busy_text, int(_elapsed_sec)]
	_inspect_busy.text = shown
	_import_busy.text = shown
	_inspect_busy.visible = _busy and _step == STEP_INSPECT
	_import_busy.visible = _busy and _step == STEP_IMPORT


func _clear_settings_error() -> void:
	_settings_error = ""
	_settings_error_label.text = ""
	_settings_error_label.visible = false


## Drops the report, so that the next Next inspects the files again. The settings go with it when they are replaced.
func _clear_report() -> void:
	_report = {}
	_inspect_error = ""
	_render_report()


func _on_confirmed() -> void:
	go_next()


func _on_custom_action(action: String) -> void:
	if action == "back":
		go_back()


## Enter in the name field or a chosen CRS: presses the primary button when it is enabled. Numbers do not use it.
func _on_field_submitted(_text: String = "") -> void:
	if not get_ok_button().disabled:
		go_next()


## Any typed number, valid or not, refreshes the buttons: an invalid one disables Import with its reason.
func _on_number_typed(_text: String) -> void:
	_refresh_buttons()


func _on_import_as_selected(_index: int) -> void:
	_clear_report()
	_refresh_buttons()


## A typed attribution or licence is the user's: a later re-import or hint does not replace it.
func _on_attribution_typed(_text: String) -> void:
	if not _populating:
		_attribution_edited = true
		_refresh_text_notes()


func _on_licence_typed(_text: String) -> void:
	if not _populating:
		_licence_edited = true
		_refresh_text_notes()


func _on_reimport_selected(index: int) -> void:
	choose_reimport(index)


## The download limit the user sets is the `max_total_mb` the import sends.
func _on_max_total_changed(value: float) -> void:
	if _populating:
		return
	_provider_query["max_total_mb"] = value
	_refresh_buttons()


func _on_provider_toggled(on: bool, provider_id_text: String) -> void:
	if on:
		choose_provider(provider_id_text)
	else:
		# The chosen online source cannot be un-chosen by its toggle: it is pressed again.
		_refresh_provider_rows()


## The `import.formats` entry of online source `provider_id_text`, or an empty dictionary.
func _provider_info(provider_id_text: String) -> Dictionary:
	for info: Dictionary in _providers:
		if String(info.get("provider_id", "")) == provider_id_text:
			return info
	return {}


## One row per online source: a radio choice with its title (the stronger line), its description and licence, and its
## notices in a box while it is chosen. With no source listed, the list says why in one sentence.
func _rebuild_provider_rows() -> void:
	_clear(_provider_box)
	_provider_rows.clear()
	_provider_notices.clear()
	if _providers.is_empty():
		_provider_box.add_child(_wrapped_label(PROVIDERS_FAILED_TEXT if _providers_failed else PROVIDERS_NONE_TEXT, &"HintLabel"))
	var ring := _radio_ring()
	for info: Dictionary in _providers:
		var provider_key := String(info.get("provider_id", ""))
		var row := VBoxContainer.new()
		row.name = "Provider_" + provider_key.replace("-", "_")
		row.add_theme_constant_override(&"separation", 4)
		var pick := CheckBox.new()
		pick.name = "Pick"
		pick.button_group = _provider_group
		pick.text = String(info.get("title", provider_key))
		pick.add_theme_font_size_override(&"font_size", PROVIDER_TITLE_PX)
		pick.add_theme_icon_override(&"radio_unchecked", ring)
		pick.toggled.connect(_on_provider_toggled.bind(provider_key))
		row.add_child(pick)
		row.add_child(_wrapped_label(String(info.get("description", "")), &"MutedLabel"))
		var licence := String(info.get("licence", "")).strip_edges()
		row.add_child(_wrapped_label("Licence: %s" % (licence if not licence.is_empty() else NOT_STATED), &"MutedLabel"))
		var notice_texts: Array = info.get("notices", [])
		if not notice_texts.is_empty():
			var notices := PanelContainer.new()
			notices.name = "Notices"
			var column := VBoxContainer.new()
			column.add_theme_constant_override(&"separation", 4)
			for notice: Variant in notice_texts:
				column.add_child(_wrapped_label(String(notice)))
			notices.add_child(column)
			row.add_child(notices)
			_provider_notices[provider_key] = notices
		_provider_box.add_child(row)
		_provider_rows[provider_key] = pick
	_refresh_provider_rows()
	_fit_later()


## The ring drawn for an unchecked radio of an online source (see [constant RADIO_RING_COLOUR]).
static func _radio_ring() -> ImageTexture:
	var image := Image.create(RADIO_RING_PX, RADIO_RING_PX, false, Image.FORMAT_RGBA8)
	var centre := Vector2(RADIO_RING_PX, RADIO_RING_PX) / 2.0
	for y in RADIO_RING_PX:
		for x in RADIO_RING_PX:
			var distance := Vector2(x + 0.5, y + 0.5).distance_to(centre)
			image.set_pixel(x, y, RADIO_RING_COLOUR if distance >= 6.0 and distance <= 8.0 else Color(0.0, 0.0, 0.0, 0.0))
	return ImageTexture.create_from_image(image)


## Shows the chosen online source's radio pressed and its notices, and the others unpressed and closed. Setting the state
## does not emit the toggle, so no source is chosen by this.
func _refresh_provider_rows() -> void:
	for provider_key: String in _provider_rows:
		var chosen := provider_key == _provider_id
		(_provider_rows[provider_key] as CheckBox).set_pressed_no_signal(chosen)
		if _provider_notices.has(provider_key):
			(_provider_notices[provider_key] as Control).visible = chosen


# --- window size -----------------------------------------------------------------------------------------------------

## Asks for a fit at the end of this frame, once the new page has its size. Several requests in one frame make one call.
func _fit_later() -> void:
	if _fit_queued or not visible:
		return
	_fit_queued = true
	call_deferred(&"_fit_now")


## Resizes the window to its contents and centres it on the screen. Its height never exceeds [constant SCREEN_SHARE] of
## the screen. A scrolling page part is as tall as its content, up to the room the screen leaves under the dialog's own
## chrome. Wrapped labels take their final height only once their width is set, so the content is measured again in the
## next frame while it still changes, at most [constant PAGE_SETTLE_PASSES] times. Growing or shrinking a page moves the
## window back into the middle, so no button falls off the bottom.
func _fit_now() -> void:
	_fit_queued = false
	var previous := -1.0
	for pass_number in PAGE_SETTLE_PASSES:
		if not visible:
			return
		var content := _fit_pass()
		if content == previous or pass_number == PAGE_SETTLE_PASSES - 1:
			return
		previous = content
		await get_tree().process_frame


## One pass of the fit. Returns the content height measured for the visible scrolling page part, or -1 when no page part
## scrolls. The cap is the screen's room under the dialog's chrome, never less than [constant PAGE_MIN_PX].
func _fit_pass() -> float:
	var area := _screen_area()
	var limit := Vector2i(area.size.x, int(floorf(float(area.size.y) * SCREEN_SHARE)))
	_set_scroll_heights(0)
	reset_size()
	var cap := maxi(PAGE_MIN_PX, limit.y - size.y)
	var scroll := _visible_scroll()
	var content := -1.0
	if scroll != null:
		content = (scroll.get_child(0) as Control).get_combined_minimum_size().y
		scroll.custom_minimum_size.y = ceilf(minf(content, float(cap)))
	reset_size()
	var fitted := Vector2i(mini(size.x, limit.x), mini(size.y, limit.y))
	size = fitted
	var origin := area.position + (area.size - fitted) / 2
	position = Vector2i(maxi(origin.x, area.position.x), maxi(origin.y, area.position.y))
	return content


func _visible_scroll() -> ScrollContainer:
	for scroll in _scroll_pages:
		if scroll.is_visible_in_tree():
			return scroll
	return null


func _set_scroll_heights(height: int) -> void:
	for scroll in _scroll_pages:
		scroll.custom_minimum_size.y = height


func _screen_area() -> Rect2i:
	if screen_rect.size != Vector2i.ZERO:
		return screen_rect
	var rect := get_tree().root.get_visible_rect()
	return Rect2i(Vector2i(rect.position), Vector2i(rect.size))


# --- settings handlers ----------------------------------------------------------------------------------------------

func _on_horizontal_changed(code: String) -> void:
	if _populating:
		return
	_settings["horizontal_crs"] = _text_or_null(code)
	_refresh_bbox_prefill()
	_refresh_buttons()


func _on_vertical_changed(code: String) -> void:
	if _populating:
		return
	_settings["vertical_crs"] = _text_or_null(code)


func _on_order_selected(index: int) -> void:
	if _populating:
		return
	_settings["coordinate_order"] = ORDER_CHOICES[index]


func _on_unit_changed(value: float) -> void:
	if _populating:
		return
	_settings["linear_unit_m"] = value


func _on_clip_selected(index: int) -> void:
	if _populating:
		return
	_settings["clip"] = _clip_for(index)
	_refresh_clip_visibility()
	_refresh_buttons()


func _on_corridor_changed(_value: Variant = null) -> void:
	if _populating or _clip.selected != CLIP_CORRIDOR:
		return
	_settings["clip"] = _corridor_clip()
	_refresh_buttons()


## A box the user edits is theirs: the pre-fill from the extent no longer replaces it.
func _on_bbox_changed(_value: float) -> void:
	if _populating:
		return
	_bbox_edited = true
	if _clip.selected == CLIP_BBOX:
		_settings["clip"] = _bbox_clip()
	_refresh_buttons()


func _on_class_toggled(_on: bool) -> void:
	if _populating:
		return
	_settings["point_classes"] = _class_selection()
	_refresh_buttons()


func _on_resolution_changed(_value: Variant = null) -> void:
	if _populating:
		return
	_settings["display_resolution_m"] = _resolution.value() if _resolution_check.button_pressed else null


func _on_nodata_toggled(on: bool) -> void:
	if _populating:
		return
	if on and not _nodata_edited:
		_nodata.set_value(float(_nodata_suggested) if _nodata_suggested != null else NODATA_DEFAULT)
	_nodata.editable = on
	_apply_nodata()
	_refresh_buttons()


func _on_nodata_text_changed(_text: String) -> void:
	if _populating:
		return
	_nodata_edited = true
	_apply_nodata()
	_refresh_buttons()


func _on_kind_selected(index: int) -> void:
	_kind = KIND_CHOICES[index]


## Writes the no-data value: `null` while unticked, the exact number while ticked. Text that is not a number keeps the
## last number, and the reason on the primary button says why Import waits.
func _apply_nodata() -> void:
	if not _nodata_check.button_pressed:
		_settings["nodata"] = null
	elif _nodata.is_valid():
		_settings["nodata"] = _nodata.value()


## The point classes. Ticks unchanged from how they were filled send the suggestion as it is; otherwise every box ticked
## sends `null` (every class), and any other choice sends the sorted list of the ticked codes.
func _class_selection() -> Variant:
	var checked := _checked_classes()
	if checked == _initial_ticks:
		return _suggested_classes
	if checked.size() == _class_checks.size():
		return null
	return checked


func _checked_classes() -> Array[int]:
	var checked: Array[int] = []
	for check in _class_checks:
		if check.button_pressed:
			checked.append(int(check.get_meta(&"class_code")))
	checked.sort()
	return checked


func _clip_for(index: int) -> Variant:
	match index:
		CLIP_CORRIDOR:
			return _corridor_clip()
		CLIP_BBOX:
			return _bbox_clip()
	return null


func _corridor_clip() -> Dictionary:
	var ranged := _station_range_active()
	return {
		"type": "corridor",
		"buffer_m": _buffer.value(),
		"alignment_ids": [],
		"station_from": _station_from.value() if ranged else null,
		"station_to": _station_to.value() if ranged else null,
	}


func _bbox_clip() -> Dictionary:
	return {
		"type": "bbox",
		"min_e": _bbox[0].value(),
		"min_n": _bbox[1].value(),
		"max_e": _bbox[2].value(),
		"max_n": _bbox[3].value(),
	}


func _station_range_active() -> bool:
	return _clip.selected == CLIP_CORRIDOR and _row_station.visible and _station_check.button_pressed


func _bbox_is_valid() -> bool:
	return _bbox[0].value() < _bbox[2].value() and _bbox[1].value() < _bbox[3].value()


## Re-fills the box from the extent while the user has not edited it. The extent is the answer only when the horizontal
## CRS is the project's CRS; otherwise the box starts at zero and has to be entered.
func _refresh_bbox_prefill() -> void:
	if _bbox_edited:
		return
	_populating = true
	_set_bbox_values(_extent_or_zero())
	_populating = false
	if _clip.selected == CLIP_BBOX:
		_settings["clip"] = _bbox_clip()


# --- population -----------------------------------------------------------------------------------------------------

func _populate_settings() -> void:
	_populating = true
	_horizontal.set_code(_text_of(_settings, "horizontal_crs"))
	_vertical.set_code(_text_of(_settings, "vertical_crs"))
	_order.select(maxi(0, ORDER_CHOICES.find(String(_settings["coordinate_order"]))))
	_order_note.text = auto_order_text(_format_id)
	_unit.set_value(float(_settings["linear_unit_m"]))
	_populate_clip()
	_populate_download()
	_attribution_row.visible = _provider_id.is_empty()
	_licence_row.visible = _provider_id.is_empty()
	_populate_classes()
	_populate_raster()
	_populate_kind()
	_name_edited = false
	_prefill_name()
	_prefill_text_fields()
	_populating = false
	_refresh_buttons()
	_fit_later()


## The Name of the import: the re-import source's name, else the automatic name (the file's name, or the online source's title).
## A name the user has typed is kept.
func _prefill_name() -> void:
	if _name_edited:
		return
	var was_populating := _populating
	_populating = true
	var from_source := String(_reimport_entry().get("name", "")).strip_edges()
	_name_field.text = from_source if not from_source.is_empty() else _auto_name
	_populating = was_populating


## A typed name is the user's: a later re-import does not replace it.
func _on_name_typed(_text: String) -> void:
	if not _populating:
		_name_edited = true


## The attribution and the licence of the import: the re-import source's values, else the file names' hints, else empty. A
## field the user has typed in keeps what the user typed. The placeholder licence of a source is shown as empty.
func _prefill_text_fields() -> void:
	var was_populating := _populating
	_populating = true
	if not _attribution_edited:
		_attribution.text = _attribution_prefill()
	if not _licence_edited:
		_licence.text = _licence_prefill()
	_populating = was_populating
	_refresh_text_notes()


## The note under Attribution and under Licence. The file-name suggestion is named only while a hint fills the field the user
## has not typed in; otherwise Attribution says where it is shown, and Licence says what it means.
func _refresh_text_notes() -> void:
	_attribution_note.text = ATTRIBUTION_HINT if _attribution_from_hint() else ATTRIBUTION_NOTE
	_licence_note.text = ATTRIBUTION_HINT if _licence_from_hint() else LICENCE_NOTE


func _attribution_from_hint() -> bool:
	if _attribution_edited or not String(_reimport_entry().get("attribution", "")).strip_edges().is_empty():
		return false
	return not _text_or_default(_report.get("attribution_hint"), "").is_empty()


func _licence_from_hint() -> bool:
	if _licence_edited or not _licence_from_source().is_empty():
		return false
	return not _text_or_default(_report.get("licence_hint"), "").is_empty()


func _attribution_prefill() -> String:
	var from_source := String(_reimport_entry().get("attribution", "")).strip_edges()
	if not from_source.is_empty():
		return from_source
	return _text_or_default(_report.get("attribution_hint"), "")


func _licence_prefill() -> String:
	var from_source := _licence_from_source()
	if not from_source.is_empty():
		return from_source
	return _text_or_default(_report.get("licence_hint"), "")


## The licence of the re-import source, or "" when it has none or only the unknown-licence placeholder.
func _licence_from_source() -> String:
	var from_source := String(_reimport_entry().get("licence", "")).strip_edges()
	return "" if from_source == UNKNOWN_LICENCE else from_source


## The existing source the re-import selector chose, or an empty dictionary.
func _reimport_entry() -> Dictionary:
	for source in _sources:
		if String(source.get("id", "")) == _reimport_of:
			return source
	return {}


## The download limit of an online source that has one (ČÚZK sources do, as `max_total_mb`), with the total the inspection
## reported beside it.
func _populate_download() -> void:
	var limited := not _provider_id.is_empty() and _provider_query.has("max_total_mb")
	_row_max_total.visible = limited
	if not limited:
		return
	_max_total.set_value(float(_provider_query["max_total_mb"]))
	_max_total_note.text = _download_text()


## The download the inspection counted, in plain words, or that it counted none.
func _download_text() -> String:
	var counts: Dictionary = _report.get("counts", {})
	if not counts.has("bytes"):
		return DOWNLOAD_NONE_NOTE
	return "At inspection, the clip needed %.1f MB to download. The import is refused above this limit." % (float(counts["bytes"]) / 1000000.0)


func _populate_clip() -> void:
	# An online source is always clipped, so its "None" is disabled and a missing clip becomes its corridor.
	var online := not _provider_id.is_empty()
	if _settings["clip"] == null and (online or not _alignments.is_empty()):
		_settings["clip"] = _default_corridor()
	var clip: Variant = _settings["clip"]
	var clip_dict: Dictionary = clip if clip is Dictionary else {}
	var choice := CLIP_NONE
	match String(clip_dict.get("type", "")):
		"corridor":
			choice = CLIP_CORRIDOR
		"bbox":
			choice = CLIP_BBOX
	_clip.set_item_disabled(CLIP_NONE, online)
	_clip_note.text = PROVIDER_CLIP_NOTE if online else CLIP_NOTE
	if online and choice == CLIP_NONE:
		choice = CLIP_CORRIDOR
	_bbox_edited = choice == CLIP_BBOX
	_buffer.set_value(float(clip_dict.get("buffer_m", CORRIDOR_DEFAULT_M)))
	var single := _alignments.size() == 1
	_row_station.visible = single
	if single:
		var start_m := float(_alignments[0].get("station_start", 0.0))
		var end_m := float(_alignments[0].get("station_end", 0.0))
		var from_value: Variant = clip_dict.get("station_from")
		var to_value: Variant = clip_dict.get("station_to")
		_station_from.set_value(float(from_value) if from_value != null else start_m)
		_station_to.set_value(float(to_value) if to_value != null else end_m)
	_station_check.button_pressed = clip_dict.get("station_from") != null and single
	if choice == CLIP_BBOX:
		_set_bbox_values([clip_dict.get("min_e", 0.0), clip_dict.get("min_n", 0.0), clip_dict.get("max_e", 0.0), clip_dict.get("max_n", 0.0)])
	else:
		_set_bbox_values(_extent_or_zero())
	_clip.select(choice)
	_refresh_clip_visibility()


func _default_corridor() -> Dictionary:
	return {"type": "corridor", "buffer_m": CORRIDOR_DEFAULT_M, "alignment_ids": [], "station_from": null, "station_to": null}


func _set_bbox_values(values: Array) -> void:
	for index in 4:
		_bbox[index].set_value(float(values[index]))
	_bbox_note.text = "Box in the project's coordinate system%s." % (
		" (%s)" % Session.crs() if not Session.crs().is_empty() else ""
	)


## The report's extent when the horizontal CRS is the project's CRS (the only case where the box means the same place);
## otherwise zeros, so the box has to be entered.
func _extent_or_zero() -> Array:
	var extent: Variant = _report.get("extent")
	if extent is Array and _text_of(_settings, "horizontal_crs") == Session.crs() and not Session.crs().is_empty():
		return extent
	return [0.0, 0.0, 0.0, 0.0]


func _refresh_clip_visibility() -> void:
	_corridor_box.visible = _clip.selected == CLIP_CORRIDOR
	_bbox_box.visible = _clip.selected == CLIP_BBOX


func _populate_classes() -> void:
	_clear(_class_box)
	_class_checks.clear()
	var classes := _sample_classes()
	_row_classes.visible = not classes.is_empty()
	var listed: Variant = _settings["point_classes"]
	var total := _sample_total_points()
	for pair: Vector2i in classes:
		var share := 100.0 * float(pair.y) / float(maxi(1, total))
		var check := CheckBox.new()
		check.text = "%s · %.1f %% of the sample" % [class_label(pair.x), share]
		check.set_meta(&"class_code", pair.x)
		check.button_pressed = listed == null or (listed as Array).has(pair.x)
		check.toggled.connect(_on_class_toggled)
		_class_box.add_child(check)
		_class_checks.append(check)
	_initial_ticks = _checked_classes()


func _populate_raster() -> void:
	var raster := _is_raster()
	_row_resolution.visible = raster
	# Imagery records its no-data value but never applies it (C1 review decision 8), so the row is shown for terrain only.
	_row_nodata.visible = raster and _kind != "imagery"
	var resolution: Variant = _settings["display_resolution_m"]
	_resolution_check.button_pressed = resolution != null
	_resolution.set_value(float(resolution) if resolution != null else RESOLUTION_DEFAULT_M)
	var nodata: Variant = _settings["nodata"]
	_nodata_suggested = nodata
	_nodata_edited = false
	_nodata_check.button_pressed = nodata != null
	_nodata.editable = nodata != null
	_nodata.set_value(float(nodata) if nodata != null else NODATA_DEFAULT)


func _populate_kind() -> void:
	var kinds := _reader_kinds(_format_id)
	# An online source supplies its own kind, so the kind is shown as text there, as for a reader that takes one kind.
	var fixed := not _provider_id.is_empty()
	var both := not fixed and kinds.has("terrain") and kinds.has("imagery")
	var shown := maxi(0, KIND_CHOICES.find(_kind))
	_kind_choice.select(shown)
	_kind_choice.visible = both
	_kind_choice.disabled = not both
	_kind_text.text = KIND_TITLES[shown]
	_kind_text.visible = not both
	_kind_note.text = PROVIDER_KIND_NOTE if fixed else KIND_ONE_READER_NOTE
	_kind_note.visible = not both


func _reader_kinds(format_id: String) -> PackedStringArray:
	for reader in _readers:
		if String(reader.get("format_id", "")) == format_id:
			var kinds := PackedStringArray()
			var listed: Array = reader.get("kinds", [])
			for kind: Variant in listed:
				kinds.append(String(kind))
			return kinds
	return PackedStringArray()


func _is_raster() -> bool:
	var counts: Dictionary = _report.get("counts", {})
	return counts.has("width") and counts.has("height")


## The number of points the inspection sampled, the base of every class share.
func _sample_total_points() -> int:
	var counts: Dictionary = _report.get("counts", {})
	return int(counts.get("sample_points", 0))


## The `sample_class_<N>` counts as (code, points), sorted by code.
func _sample_classes() -> Array[Vector2i]:
	var counts: Dictionary = _report.get("counts", {})
	var classes: Array[Vector2i] = []
	for key: Variant in counts.keys():
		var key_name := String(key)
		if key_name.begins_with("sample_class_") and key_name.trim_prefix("sample_class_").is_valid_int():
			classes.append(Vector2i(int(key_name.trim_prefix("sample_class_")), int(counts[key])))
	classes.sort_custom(func(a: Vector2i, b: Vector2i) -> bool: return a.x < b.x)
	return classes


# --- the report and the result --------------------------------------------------------------------------------------

func _render_report() -> void:
	_clear(_report_box)
	if not _inspect_error.is_empty():
		_report_box.add_child(_wrapped_label(_inspect_error, &"ErrorLabel"))
	elif not _report.is_empty():
		_fill_report()
	_fit_later()


func _fill_report() -> void:
	var declared: Variant = _report.get("declared_crs")
	var grid := _pair_grid()
	_add_pair(grid, "Format", format_label(_format_id))
	if _provider_id.is_empty():
		_add_pair(grid, "Files", "%d %s" % [_files.size(), "file" if _files.size() == 1 else "files"])
	else:
		_add_pair(grid, "Source", _provider_title)
		_add_pair(grid, "Attribution", String(_provider_info(_provider_id).get("attribution", "")))
	_add_pair(grid, "Kind", KIND_TITLES[maxi(0, KIND_CHOICES.find(_kind))])
	_add_pair(grid, "Declared CRS", String(declared) if declared != null else "None declared")
	_add_pair(grid, "Extent", extent_text(_report.get("extent")))
	_add_pair(grid, "Counts", _counts_text())
	_add_pair(grid, "Licence", _text_or_default(_report.get("licence_hint"), NOT_STATED))
	_report_box.add_child(grid)
	for notice: Variant in _provider_info(_provider_id).get("notices", []):
		_report_box.add_child(_note(String(notice)))
	if declared == null:
		_report_box.add_child(_wrapped_label(NO_CRS_NOTICE, &"ErrorLabel"))
	var warnings: Array = _report.get("warnings", [])
	for warning: Variant in warnings:
		_report_box.add_child(_wrapped_label(String(warning)))
	if not _sample_classes().is_empty():
		_report_box.add_child(_class_table())


func _render_result() -> void:
	_clear(_result_box)
	_result_box.add_child(_wrapped_label("Imported \"%s\"" % String(_result.get("name", "")), &"PanelTitle"))
	var grid := _pair_grid()
	_add_pair(grid, "Added", str(int(_result.get("added", 0))))
	_add_pair(grid, "Changed", str(int(_result.get("changed", 0))))
	_add_pair(grid, "Unchanged", str(int(_result.get("unchanged", 0))))
	_add_pair(grid, "Removed", str(int(_result.get("removed", 0))))
	_add_pair(grid, "Effective CRS", String(_result.get("effective_crs", "")))
	_add_pair(grid, "Transformation", _transformation_text())
	_add_pair(grid, "Licence", _text_or_default(_result_licence, NOT_STATED))
	_add_pair(grid, "Attribution", _text_or_default(_result_attribution, "Not recorded"))
	_result_box.add_child(grid)
	var warnings: Array = _result.get("warnings", [])
	for warning: Variant in warnings:
		_result_box.add_child(_wrapped_label(String(warning)))
	_fit_later()


func _transformation_text() -> String:
	var transformation := String(_result.get("transformation", ""))
	if transformation.is_empty() or transformation == NO_TRANSFORMATION:
		return "None needed"
	var accuracy: Variant = _result.get("transformation_accuracy_m")
	if accuracy == null:
		return "%s (accuracy not stated)" % transformation
	return "%s, accurate to %.2f m" % [transformation, float(accuracy)]


func _counts_text() -> String:
	var counts: Dictionary = _report.get("counts", {})
	if _is_raster():
		return "%d × %d pixels, %d band(s)" % [int(counts["width"]), int(counts["height"]), int(counts.get("bands", 1))]
	if counts.has("points"):
		var points := int(counts["points"])
		return "%d %s" % [points, "point" if points == 1 else "points"]
	if counts.has("sheets"):
		var sheets := int(counts["sheets"])
		var text := "%d %s" % [sheets, "sheet" if sheets == 1 else "sheets"]
		if counts.has("bytes"):
			text += ", %.1f MB to download" % (float(counts["bytes"]) / 1000000.0)
		return text
	if counts.has("tiles"):
		var tiles := int(counts["tiles"])
		return "%d %s" % [tiles, "tile" if tiles == 1 else "tiles"]
	return "Not counted"


func _class_table() -> GridContainer:
	var grid := _pair_grid()
	_add_pair(grid, "Point class", "Share of the sampled points")
	var total := _sample_total_points()
	for pair: Vector2i in _sample_classes():
		_add_pair(grid, class_label(pair.x), "%.1f %%" % (100.0 * float(pair.y) / float(maxi(1, total))))
	return grid


func _describe_source() -> String:
	if not _provider_id.is_empty():
		return "Online source: %s" % _provider_title
	if _files.is_empty():
		return NOTHING_CHOSEN
	var names := PackedStringArray()
	for file in _files:
		names.append(file.get_file())
	return "Chosen: %s" % ", ".join(names)


# --- building the dialog ---------------------------------------------------------------------------------------------

func _build() -> void:
	_populating = true
	_column = VBoxContainer.new()
	_column.add_theme_constant_override(&"separation", 12)
	add_child(_column)
	_column.add_child(_build_steps())
	_pages = [_build_source_page(), _build_inspect_page(), _build_settings_page(), _build_import_page(), _build_result_page()]
	for page in _pages:
		_column.add_child(page)
	_hint = _wrapped_label("", &"HintLabel")
	_hint.name = "Hint"
	_hint.visible = false
	_column.add_child(_hint)

	_file_dialog = FileDialog.new()
	_file_dialog.access = FileDialog.ACCESS_FILESYSTEM
	_file_dialog.file_mode = FileDialog.FILE_MODE_OPEN_FILES
	_file_dialog.use_native_dialog = true
	_file_dialog.title = "Choose files to import"
	_file_dialog.files_selected.connect(set_files)
	add_child(_file_dialog)

	_back = add_button("Back", false, "back")
	_share_requester()
	_populating = false
	_enter_step(STEP_SOURCE)


func _build_steps() -> Control:
	var row := HBoxContainer.new()
	row.name = "Steps"
	row.add_theme_constant_override(&"separation", 8)
	for index in STEP_NAMES.size():
		if index > 0:
			var dot := Label.new()
			dot.text = "·"
			dot.theme_type_variation = &"MutedLabel"
			row.add_child(dot)
		var label := Label.new()
		label.text = "%d %s" % [index + 1, STEP_NAMES[index]]
		row.add_child(label)
		_step_labels.append(label)
	return row


func _build_source_page() -> Control:
	var page := _page("SourcePage")
	page.add_child(_wrapped_label("Choose files to import, or an online source. Several files of one format, such as the sheets of one survey, form one source."))
	var buttons := HBoxContainer.new()
	buttons.add_theme_constant_override(&"separation", 8)
	var files := Button.new()
	files.name = "Files"
	files.text = "Files…"
	files.pressed.connect(_on_files_pressed)
	buttons.add_child(files)
	_online = Button.new()
	_online.name = "OnlineSource"
	_online.text = "Online source"
	_online.tooltip_text = "Download a source from one of the online services listed below"
	_online.pressed.connect(show_online_sources)
	buttons.add_child(_online)
	page.add_child(buttons)
	_files_label = _wrapped_label(NOTHING_CHOSEN, &"MutedLabel")
	page.add_child(_files_label)
	_provider_box = VBoxContainer.new()
	_provider_box.name = "Providers"
	_provider_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_provider_box.add_theme_constant_override(&"separation", 12)
	_provider_scroll = _capped_scroll(_provider_box, "ProviderScroll")
	_provider_scroll.visible = false
	page.add_child(_provider_scroll)
	_source_error_label = _wrapped_label("", &"ErrorLabel")
	_source_error_label.visible = false
	page.add_child(_source_error_label)
	_import_as_row = _row("Import as", _build_import_as(), [_note(IMPORT_AS_NOTE)])
	page.add_child(_import_as_row)
	return page


func _build_import_as() -> OptionButton:
	_import_as = OptionButton.new()
	_import_as.name = "ImportAs"
	for choice_title in IMPORT_AS_TITLES:
		_import_as.add_item(choice_title)
	_import_as.item_selected.connect(_on_import_as_selected)
	return _import_as


func _build_inspect_page() -> Control:
	var page := _page("InspectPage")
	_inspect_busy = _wrapped_label("")
	_inspect_busy.visible = false
	page.add_child(_inspect_busy)
	_report_box = VBoxContainer.new()
	_report_box.name = "Report"
	_report_box.add_theme_constant_override(&"separation", 8)
	page.add_child(_capped_scroll(_report_box, "ReportScroll"))
	return page


func _build_settings_page() -> Control:
	var page := _page("SettingsPage")
	_settings_error_label = _wrapped_label("", &"ErrorLabel")
	_settings_error_label.visible = false
	page.add_child(_settings_error_label)

	var form := VBoxContainer.new()
	form.name = "Form"
	form.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	form.add_theme_constant_override(&"separation", 12)
	page.add_child(_capped_scroll(form, "SettingsScroll"))

	_horizontal = CrsPicker.new()
	_horizontal.name = "HorizontalCrs"
	_horizontal.set_kind_filter("horizontal")
	_horizontal.code_changed.connect(_on_horizontal_changed)
	_horizontal.submitted.connect(_on_field_submitted)
	form.add_child(_row("Horizontal CRS", _horizontal, [_note("Required. The coordinate system the data is in, found by code or name.")]))

	_vertical = CrsPicker.new()
	_vertical.name = "VerticalCrs"
	_vertical.set_kind_filter("vertical")
	_vertical.code_changed.connect(_on_vertical_changed)
	_vertical.submitted.connect(_on_field_submitted)
	form.add_child(_row("Vertical CRS", _vertical, [_note("Recorded only — heights are not transformed.")]))

	_order = OptionButton.new()
	_order.name = "CoordinateOrder"
	for choice_title in ORDER_TITLES:
		_order.add_item(choice_title)
	_order.item_selected.connect(_on_order_selected)
	_order_note = _note("")
	form.add_child(_row("Coordinate order", _order, [_order_note]))

	_unit = _number_field("LinearUnit", 0.0, true, 0.01)
	_unit.value_edited.connect(_on_unit_changed)
	form.add_child(_row("Linear unit", _with_unit(_unit, UNIT_SUFFIX), [_note("Metres per source unit: 1 for metres, 0.3048 for feet.")]))

	form.add_child(_build_clip_row())
	form.add_child(_build_download_row())
	form.add_child(_build_classes_row())
	form.add_child(_build_raster_rows())
	form.add_child(_build_kind_row())
	form.add_child(_build_reimport_row())
	form.add_child(_build_attribution_rows())

	_name_field = LineEdit.new()
	_name_field.name = "SourceName"
	_name_field.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_name_field.text_submitted.connect(_on_field_submitted)
	_name_field.text_changed.connect(_on_name_typed)
	form.add_child(_row("Name", _name_field, [_note("The name the source has in the project. Leave it empty for no name.")]))
	return page


## The download limit of an online source (`max_total_mb`): a number in MB, with the download the inspection counted.
func _build_download_row() -> Control:
	_max_total = _number_field("DownloadLimit", 0.0, true, 10.0)
	_max_total.value_edited.connect(_on_max_total_changed)
	_max_total_note = _note("")
	_row_max_total = _row("Download limit", _with_unit(_max_total, "MB"), [_max_total_note])
	return _row_max_total


## The existing sources a re-import can refresh. "— none —" is the first choice, so the selector is never empty.
func _build_reimport_row() -> Control:
	_reimport = OptionButton.new()
	_reimport.name = "ReimportOf"
	_reimport.add_item(NO_RE_IMPORT)
	_reimport.item_selected.connect(_on_reimport_selected)
	_reimport_note = _note(RE_IMPORT_NOTE)
	_reimport_row = _row("Re-import of", _reimport, [_reimport_note])
	return _reimport_row


## Attribution and licence of a file import, recorded on the source (F53). An online source's come from the provider, so
## these rows are hidden for one.
func _build_attribution_rows() -> Control:
	var group := VBoxContainer.new()
	group.name = "AttributionFields"
	group.add_theme_constant_override(&"separation", 12)
	_attribution = LineEdit.new()
	_attribution.name = "AttributionField"
	_attribution.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_attribution.text_changed.connect(_on_attribution_typed)
	_attribution_note = _note(ATTRIBUTION_NOTE)
	_attribution_row = _row("Attribution", _attribution, [_attribution_note])
	group.add_child(_attribution_row)
	_licence = LineEdit.new()
	_licence.name = "LicenceField"
	_licence.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_licence.text_changed.connect(_on_licence_typed)
	_licence_note = _note(LICENCE_NOTE)
	_licence_row = _row("Licence", _licence, [_licence_note])
	group.add_child(_licence_row)
	return group


func _build_kind_row() -> Control:
	var field := VBoxContainer.new()
	field.name = "KindField"
	field.add_theme_constant_override(&"separation", 4)
	_kind_choice = OptionButton.new()
	_kind_choice.name = "Kind"
	for choice_title in KIND_TITLES:
		_kind_choice.add_item(choice_title)
	_kind_choice.item_selected.connect(_on_kind_selected)
	field.add_child(_kind_choice)
	_kind_text = Label.new()
	_kind_text.name = "KindText"
	field.add_child(_kind_text)
	_kind_note = _note(KIND_ONE_READER_NOTE)
	field.add_child(_kind_note)
	return _row("Kind", field, [_note("Terrain is a height surface; imagery is a picture laid on it.")])


func _build_clip_row() -> Control:
	var group := VBoxContainer.new()
	group.name = "ClipGroup"
	group.add_theme_constant_override(&"separation", 8)
	_clip = OptionButton.new()
	_clip.name = "Clip"
	for choice_title in CLIP_TITLES:
		_clip.add_item(choice_title)
	_clip.item_selected.connect(_on_clip_selected)
	group.add_child(_clip)

	_corridor_box = VBoxContainer.new()
	_corridor_box.name = "CorridorClip"
	_corridor_box.add_theme_constant_override(&"separation", 8)
	_buffer = _number_field("Buffer", 0.0, true, 1.0)
	_buffer.maximum = BUFFER_MAX_M
	_buffer.set_value(CORRIDOR_DEFAULT_M)
	_buffer.value_edited.connect(_on_corridor_changed)
	_corridor_box.add_child(_row("Buffer half-width", _with_unit(_buffer, UNIT_SUFFIX), [_note("The corridor around the alignments, in metres.")]))
	_row_station = HBoxContainer.new()
	_row_station.name = "StationRange"
	_row_station.add_theme_constant_override(&"separation", 8)
	_station_check = CheckBox.new()
	_station_check.text = "Only part of the alignment"
	_station_check.toggled.connect(_on_corridor_changed)
	_row_station.add_child(_station_check)
	_station_from = _station_box("StationFrom")
	_station_to = _station_box("StationTo")
	_row_station.add_child(_caption("From"))
	_row_station.add_child(_with_unit(_station_from, UNIT_SUFFIX))
	_row_station.add_child(_caption("To"))
	_row_station.add_child(_with_unit(_station_to, UNIT_SUFFIX))
	_corridor_box.add_child(_row_station)
	group.add_child(_corridor_box)

	_bbox_box = VBoxContainer.new()
	_bbox_box.name = "BoundingBoxClip"
	_bbox_box.add_theme_constant_override(&"separation", 8)
	_bbox.clear()
	for index in 4:
		var box := _number_field("Bbox" + BOX_CAPTIONS[index].replace(" ", ""), -INF, false, 1.0)
		box.value_edited.connect(_on_bbox_changed)
		_bbox.append(box)
	var first := HBoxContainer.new()
	first.add_theme_constant_override(&"separation", 8)
	first.add_child(_caption("Min E"))
	first.add_child(_with_unit(_bbox[0], UNIT_SUFFIX))
	first.add_child(_caption("Min N"))
	first.add_child(_with_unit(_bbox[1], UNIT_SUFFIX))
	_bbox_box.add_child(first)
	var second := HBoxContainer.new()
	second.add_theme_constant_override(&"separation", 8)
	second.add_child(_caption("Max E"))
	second.add_child(_with_unit(_bbox[2], UNIT_SUFFIX))
	second.add_child(_caption("Max N"))
	second.add_child(_with_unit(_bbox[3], UNIT_SUFFIX))
	_bbox_box.add_child(second)
	_bbox_note = _note("")
	_bbox_box.add_child(_bbox_note)
	group.add_child(_bbox_box)
	_clip_note = _note(CLIP_NOTE)
	return _row("Clip", group, [_clip_note])


func _build_classes_row() -> Control:
	_class_box = VBoxContainer.new()
	_class_box.name = "PointClasses"
	_class_box.add_theme_constant_override(&"separation", 4)
	_row_classes = _row("Point classes", _class_box, [_note("Every class is kept unless you untick it. Shares come from a sample of the points.")])
	return _row_classes


func _build_raster_rows() -> Control:
	var group := VBoxContainer.new()
	group.name = "RasterSettings"
	group.add_theme_constant_override(&"separation", 8)

	var resolution_field := HBoxContainer.new()
	resolution_field.add_theme_constant_override(&"separation", 8)
	_resolution_check = CheckBox.new()
	_resolution_check.text = "Set the display resolution"
	_resolution_check.toggled.connect(_on_resolution_changed)
	resolution_field.add_child(_resolution_check)
	_resolution = _number_field("DisplayResolution", 0.0, true, 0.1)
	_resolution.value_edited.connect(_on_resolution_changed)
	resolution_field.add_child(_with_unit(_resolution, UNIT_SUFFIX))
	_row_resolution = _row("Display resolution", resolution_field, [_note("Rasters only: the cell size the display tiles aim for.")])
	group.add_child(_row_resolution)

	var nodata_field := HBoxContainer.new()
	nodata_field.add_theme_constant_override(&"separation", 8)
	_nodata_check = CheckBox.new()
	_nodata_check.text = "Treat a value as no data"
	_nodata_check.toggled.connect(_on_nodata_toggled)
	nodata_field.add_child(_nodata_check)
	_nodata = _number_field("NoData", -INF, false, 1.0)
	_nodata.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_nodata.custom_minimum_size = Vector2(NODATA_TEXT_MIN_PX, 0)
	_nodata.text_changed.connect(_on_nodata_text_changed)
	nodata_field.add_child(_nodata)
	_row_nodata = _row("No data", nodata_field, [_note(NODATA_NOTE)])
	group.add_child(_row_nodata)
	return group


func _build_import_page() -> Control:
	var page := _page("ImportPage")
	_import_busy = _wrapped_label("")
	_import_busy.visible = false
	page.add_child(_import_busy)
	return page


func _build_result_page() -> Control:
	var page := _page("ResultPage")
	_result_box = VBoxContainer.new()
	_result_box.name = "Result"
	_result_box.add_theme_constant_override(&"separation", 8)
	page.add_child(_capped_scroll(_result_box, "ResultScroll"))
	return page


func _page(page_name: String) -> VBoxContainer:
	var page := VBoxContainer.new()
	page.name = page_name
	page.add_theme_constant_override(&"separation", 10)
	return page


## A page part that scrolls, so that no page makes the window taller than the screen. Its height is the room the screen
## leaves under the dialog's own chrome; [method _fit_now] sets it.
func _capped_scroll(content: Control, scroll_name: String) -> ScrollContainer:
	var scroll := ScrollContainer.new()
	scroll.name = scroll_name
	scroll.custom_minimum_size = Vector2(SETTINGS_PAGE_WIDTH, 0)
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll.add_child(content)
	_scroll_pages.append(scroll)
	return scroll


func _on_files_pressed() -> void:
	_provider_scroll.visible = false
	_fit_later()
	_file_dialog.filters = file_filters()
	_file_dialog.popup_centered_ratio(0.6)


func _row(caption_text: String, field: Control, notes: Array = []) -> HBoxContainer:
	var row := HBoxContainer.new()
	row.name = caption_text.replace(" ", "")
	row.add_theme_constant_override(&"separation", 12)
	var caption := _caption(caption_text)
	caption.custom_minimum_size = Vector2(LABEL_COLUMN_PX, 0)
	row.add_child(caption)
	var column := VBoxContainer.new()
	column.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	column.add_theme_constant_override(&"separation", 4)
	field.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	column.add_child(field)
	for note: Variant in notes:
		column.add_child(note as Control)
	row.add_child(column)
	return row


func _caption(text: String) -> Label:
	var label := Label.new()
	label.text = text
	label.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	return label


func _note(text: String) -> Label:
	return _wrapped_label(text, &"HintLabel")


## A numeric field that keeps the exact float (see [NumberField]). Its bound is `minimum_value`, refused when `exclusive`;
## the Up and Down keys step by `arrow`. Its typing refreshes the buttons. Enter in it only confirms the number, so it is
## not wired to the primary button.
func _number_field(field_name: String, minimum_value: float, exclusive: bool, arrow: float) -> NumberField:
	var field := NumberField.new()
	field.name = field_name
	field.minimum = minimum_value
	field.minimum_exclusive = exclusive
	field.arrow_step = arrow
	field.custom_minimum_size = Vector2(NUMBER_MIN_PX, 0)
	field.text_changed.connect(_on_number_typed)
	return field


## A number with its unit beside it: the unit is a label, not part of the number's text.
func _with_unit(field: NumberField, unit: String) -> HBoxContainer:
	var box := HBoxContainer.new()
	box.add_theme_constant_override(&"separation", 6)
	box.add_child(field)
	box.add_child(_caption(unit))
	return box


func _station_box(box_name: String) -> NumberField:
	var field := _number_field(box_name, -INF, false, 1.0)
	field.value_edited.connect(_on_corridor_changed)
	return field


## An autowrapped label. Its minimum width is set, so that its height is computed for a real line length; without it
## a wrapped label asks for one word per line and the window grows out of the screen.
func _wrapped_label(text: String, variation: StringName = &"") -> Label:
	var label := Label.new()
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.custom_minimum_size = Vector2(WRAP_MIN_PX, 0)
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if variation != &"":
		label.theme_type_variation = variation
	return label


func _pair_grid() -> GridContainer:
	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override(&"h_separation", 16)
	grid.add_theme_constant_override(&"v_separation", 6)
	return grid


func _add_pair(grid: GridContainer, caption: String, value: String) -> void:
	var key := Label.new()
	key.text = caption
	key.theme_type_variation = &"MutedLabel"
	key.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	grid.add_child(key)
	grid.add_child(_wrapped_label(value))


## Removes and frees the children of `box` now: a child queued for freeing would outlive the call it was cleared in.
func _clear(box: Container) -> void:
	for child in box.get_children():
		box.remove_child(child)
		child.free()


func _collect_texts(node: Node, texts: PackedStringArray) -> void:
	for child in node.get_children():
		if child is Label:
			texts.append((child as Label).text)
		_collect_texts(child, texts)


static func _text_of(settings: Dictionary, key: String) -> String:
	var value: Variant = settings.get(key)
	return "" if value == null else String(value)


static func _text_or_default(value: Variant, fallback: String) -> String:
	if value == null or String(value).is_empty():
		return fallback
	return String(value)
