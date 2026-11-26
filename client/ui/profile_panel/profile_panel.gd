class_name ProfilePanel
extends Control
## The Profile tab (T-137 B): the ground line under an alignment beside its rail profile, the cut and fill of
## each station, and a cursor that follows playback with a readout. It draws the result of
## [method Session.fetch_ground_profile] (the backend's `alignment.ground_profile`) and computes no geometry of its
## own; the drawing maths are static helpers, so the unit tests need no rendering.
##
## The panel fetches the whole alignment at 10 m only while it is visible, and again when the terrain, the alignments
## or the project change. It shows a window of the line centred on the cursor, at a vertical exaggeration. Cut and fill
## are shaded where their areas are positive, and the no-ground spans are hatched. A profile with no ground anywhere
## draws the rail and says so. The cursor moves when the bound [PlaybackController] does; nothing is redrawn while it
## stands still.

enum State { NO_ALIGNMENT, LOADING, FAILED, READY }

const SPACING_M := 10.0
## The window's width in metres for each choice of the width selector; 0 is the whole line.
const WINDOW_WIDTHS_M := [1000.0, 5000.0, 0.0]
const WINDOW_LABELS := ["1 km", "5 km", "Whole line"]
const DEFAULT_WINDOW_INDEX := 1
const WINDOW_TOOLTIP := "Window width"
## The vertical exaggerations of the selector; 0 is "Fit", which fits the data. "Fit" is the last item, so the three ratios
## keep their indices.
const EXAGGERATIONS := [1.0, 5.0, 10.0, 0.0]
const EXAGGERATION_LABELS := ["1×", "5×", "10×", "Fit"]
const DEFAULT_EXAGGERATION_INDEX := 2
const FIT_INDEX := 3
const EXAGGERATION_TOOLTIP := "Vertical exaggeration"
## The range shown is the data's range plus this share of its span on each side.
const MARGIN_FRACTION := 0.1
## A fitted range this much larger than a chosen ratio's range shows as the factor it gives, in the label beside the selector.
const OVERRIDE_SHARE := 0.05
const MIN_VERTICAL_SPAN_M := 1.0
const FIT_MIN_SPAN_M := 2.0
const READOUT_WIDTH_PX := 250.0
const READOUT_MARGIN_PX := 4.0
const GAP_PX := 8.0
const AXIS_LEFT_PX := 56.0
const AXIS_BOTTOM_PX := 22.0
const UNIT_GAP_PX := 6.0
const HATCH_SPACING_PX := 8.0
const GROUND_WIDTH_PX := 2.0
const RAIL_WIDTH_PX := 3.0
const CURSOR_WIDTH_PX := 1.5
const FORMATION_DEPTH_M := 0.8
const FLAG_NO_GROUND := 1
## Flag 8 comes only with flag 1: the row has no bare ground, but a surface model covers it (T-137 Part C).
const FLAG_SURFACE_MODEL := 8
const SOURCE_NONE := 255
## Row states: what [method row_states] packs into one byte per row, and the run ends [method sample_rows] keeps.
const STATE_GROUND := 1
const STATE_CUT := 2
const STATE_FILL := 4
## A polygon with less area than this, or a vertex this close to the one before it, is dropped (metres for the data, pixels for
## the drawing). At stations of about 13 000 m the drawing's float32 vertices lose slivers thinner than about 1e-4 px, and the engine
## refuses a polygon it cannot triangulate, so the pixel floor is 1e-2 px².
const AREA_MIN_DATA := 1e-6
const AREA_MIN_PIXELS := 1e-2
## A drawn polygon whose area divided by its x-extent (its mean thickness) is under this is a sliver a few millimetres thick at the
## drawing's scale, and is dropped (see [method shading_pixels]).
const THICKNESS_MIN_PIXELS := 1e-3
const EPS_DATA := 1e-9
const EPS_PIXELS := 1e-4
## The no-ground label: its padding inside a span, its gap beside one, its plate's padding and alpha, and the clearance of two labels.
const LABEL_PAD_PX := 6.0
const LABEL_GAP_PX := 6.0
const LABEL_PLATE_PAD_PX := 3.0
const LABEL_PLATE_ALPHA := 0.85
const LABEL_CLEAR_PX := 2.0
## The metre labels are at least this many label heights apart (the step of [method _metre_step]).
const METRE_LABEL_SPACING := 1.2

const NO_ALIGNMENT_TEXT := "Import an alignment to see its ground profile"
const LOADING_TEXT := "Loading the ground profile…"
const FAILED_TEXT := "The ground profile could not be computed"
const NO_TERRAIN_TEXT := "No terrain covers this alignment — import terrain to see the ground"
const SURFACE_ONLY_TEXT := "Only a surface model covers this alignment (%s) — import bare-ground terrain for cut and fill"
const SURFACE_MODEL_FALLBACK_NAME := "surface model"
const NO_GROUND_LABEL := "no ground data (e.g. water)"
const NO_GROUND_READOUT := "no ground data"
const LEGEND_CUT := "Cut"
const LEGEND_FILL := "Fill"
const LEGEND_NO_GROUND := "No ground data"
const LEGEND_NO_GROUND_TOOLTIP := "No ground data, e.g. water under a bridge"
const SURFACE_ONLY_READOUT := "surface model only"
const IDEALISED_TEXT := "Vertical profile is idealised in this dataset"
const RETRY_TEXT := "Retry"
const MINUS := "−"

## Theme colours of the ProfilePanel type; the fallbacks are used when a theme lacks one. The no-ground colour reaches 4.5:1
## against the light panel background.
const _COLOURS := {
	"rail": Color(0.12, 0.16, 0.2),
	"ground": Color(0.6, 0.36, 0.1),
	"cut": Color(0.89, 0.42, 0.18, 0.42),
	"fill": Color(0.18, 0.42, 0.89, 0.42),
	"no_ground": Color(0.3725, 0.4118, 0.4667),
	"grid": Color(0.5, 0.5, 0.5, 0.22),
	"cursor": Color(0.15, 0.39, 0.92),
	"formation": Color(0.3, 0.55, 0.35),
	"background": Color(1.0, 1.0, 1.0),
}

var _controller: PlaybackController
var _has_cursor := false
var _alignment_id := ""
var _profile: Dictionary = {}
var _states := PackedByteArray()
var _error := ""
var _state := State.NO_ALIGNMENT
var _cursor := 0.0
var _drawn_cursor := INF
var _generation := 0
var _dirty := true
var _window_index := DEFAULT_WINDOW_INDEX
var _exaggeration_index := DEFAULT_EXAGGERATION_INDEX
var _alignment_start := 0.0
var _alignment_end := 0.0
var _draw_calls := 0
var _drawn_texts: Array[Rect2] = []
var _drawn_strings := PackedStringArray()
var _drawn_plates: Array[Rect2] = []
## Bumped with each stored profile; the vertical range is memoised on it (see [method _view_vertical]).
var _profile_serial := 0
var _vertical_key: Array = []
var _vertical_memo := Vector2.ZERO
var _vertical_computes := 0
## The vertices the last drawing handed to the engine as shading (see [method shading_vertex_count]).
var _shading_vertices := 0

var _controls: HFlowContainer
var _window_choice: OptionButton
var _exaggeration_choice: OptionButton
var _factor_label: Label
var _legend_items: Array[Control] = []
## The legend's swatches by the theme colour key they show, so a theme change can refresh them.
var _legend_swatches := {}
var _readout: VBoxContainer
var _readout_labels := {}
var _idealised_note: Label
var _status: Label
var _retry: Button


func _ready() -> void:
	theme_type_variation = &"ProfilePanel"
	mouse_filter = Control.MOUSE_FILTER_PASS
	clip_contents = true
	_build()
	resized.connect(_on_resized)
	visibility_changed.connect(_maybe_request)
	_refresh_alignment()
	_layout()
	_maybe_request()


## The EventBus connections live while the panel is in the tree. The shell moves the panel with reparent on a preset switch, which
## runs these two and not _ready again, so connecting in _ready would lose the events after the first switch.
func _enter_tree() -> void:
	if not EventBus.project_changed.is_connected(_on_inputs_changed):
		EventBus.project_changed.connect(_on_inputs_changed)
	if not EventBus.alignments_changed.is_connected(_on_inputs_changed):
		EventBus.alignments_changed.connect(_on_inputs_changed)
	if not EventBus.sources_changed.is_connected(_on_sources_changed):
		EventBus.sources_changed.connect(_on_sources_changed)


func _exit_tree() -> void:
	if EventBus.project_changed.is_connected(_on_inputs_changed):
		EventBus.project_changed.disconnect(_on_inputs_changed)
	if EventBus.alignments_changed.is_connected(_on_inputs_changed):
		EventBus.alignments_changed.disconnect(_on_inputs_changed)
	if EventBus.sources_changed.is_connected(_on_sources_changed):
		EventBus.sources_changed.disconnect(_on_sources_changed)


# --- binding and data -------------------------------------------------------------------------------------------

## Follows `controller`: its alignment is the one shown, and its lead station is the cursor. Null unbinds it; the panel
## then shows the first alignment of the project with the cursor at that alignment's start, and no cursor line.
func bind_playback(controller: PlaybackController) -> void:
	_controller = controller
	_has_cursor = controller != null
	_refresh_alignment()
	_maybe_request()


func alignment_id() -> String:
	return _alignment_id


func state() -> State:
	return _state


func error_text() -> String:
	return _error


## True once the profile is shown, or the fetch has failed: nothing is loading any more.
func settled() -> bool:
	return _state == State.READY or _state == State.FAILED or _state == State.NO_ALIGNMENT


## The fetched profile (as [method Session.fetch_ground_profile] returns it), or an empty dictionary.
func profile() -> Dictionary:
	return _profile


## Shows `profile` for `alignment_id` without fetching it (the fetch itself is [method _request]). Used by the tests and
## by the capture, which read the same data the panel holds.
func show_profile(data: Dictionary, alignment_id: String) -> void:
	_generation += 1
	_alignment_id = alignment_id
	_store_profile(data)
	_error = ""
	_dirty = false
	_state = State.READY if not data.is_empty() else State.NO_ALIGNMENT
	_refresh_bounds()
	_refresh_note()
	_update_status()
	_refresh_readout()
	queue_redraw()


func _store_profile(data: Dictionary) -> void:
	_profile = data
	_profile_serial += 1
	_states = PackedByteArray()
	if not data.is_empty():
		_states = row_states(data["flags"], data["ground_h"], data["cut_area"], data["fill_area"])


## Sets the cursor station (the bound controller does this every frame; without one, a test or the capture does).
func set_cursor(station: float) -> void:
	_has_cursor = true
	_cursor = station
	_refresh_readout()
	queue_redraw()


func cursor_station() -> float:
	return _cursor


func has_cursor_line() -> bool:
	return _has_cursor


func set_window_index(index: int) -> void:
	_window_index = clampi(index, 0, WINDOW_WIDTHS_M.size() - 1)
	_window_choice.select(_window_index)
	_request_draw()


func window_index() -> int:
	return _window_index


func set_exaggeration_index(index: int) -> void:
	_exaggeration_index = clampi(index, 0, EXAGGERATIONS.size() - 1)
	_exaggeration_choice.select(_exaggeration_index)
	_request_draw()


func exaggeration_index() -> int:
	return _exaggeration_index


func exaggeration() -> float:
	return EXAGGERATIONS[_exaggeration_index]


func window_width_m() -> float:
	return WINDOW_WIDTHS_M[_window_index]


func window_choice() -> OptionButton:
	return _window_choice


func exaggeration_choice() -> OptionButton:
	return _exaggeration_choice


func retry_button() -> Button:
	return _retry


func status_label() -> Label:
	return _status


func idealised_note() -> Label:
	return _idealised_note


func readout_label(key: String) -> Label:
	return _readout_labels.get(key)


func readout_column() -> Control:
	return _readout


func factor_label() -> Label:
	return _factor_label


## The legend's items, in order (a swatch and a label each); they sit in the controls row with the selectors.
func legend_items() -> Array:
	return _legend_items


## The row of the selectors and the legend: its height is the height of the rows it wraps to.
func controls_row() -> Control:
	return _controls


## How many times the vertical range was computed: a cursor move and the redraw after it compute it once.
func vertical_computes() -> int:
	return _vertical_computes


## The vertices of the shading the last drawing passed to the engine: the drawn rows, not the window's, bound it.
func shading_vertex_count() -> int:
	return _shading_vertices


## The vertical factor the drawing shows now: the chosen one, or the one the fitted range gives (0 when nothing is shown).
func effective_factor() -> float:
	var plot := plot_rect()
	var extent := _view_vertical()
	var window := _window_bounds()
	return factor_for_span(extent.y - extent.x, window.y - window.x, plot.size.x, plot.size.y)


## How many times [method _draw] has run: the redraws the tests and the capture count.
func draw_count() -> int:
	return _draw_calls


## The rectangles (in the panel's own coordinates) of the text the last drawing put down: the axis labels, the units and the
## no-ground labels. The tests check them against the readout column.
func drawn_texts() -> Array[Rect2]:
	return _drawn_texts


## The plates the no-ground labels sit on in the last drawing (the label's rectangle grown by the plate's padding).
func drawn_plates() -> Array[Rect2]:
	return _drawn_plates


## The strings of [method drawn_texts], in the same order.
func drawn_strings() -> PackedStringArray:
	return _drawn_strings


## The span of the vertical range the drawing shows now, in metres.
func vertical_span_m() -> float:
	var extent := _view_vertical()
	return extent.y - extent.x


## The drawing area in the panel's own coordinates: right of the axis labels, below the controls, left of the readout
## column, and above the station labels.
func plot_rect() -> Rect2:
	if _controls == null:
		return Rect2()
	var top := _controls.size.y + GAP_PX
	var left := AXIS_LEFT_PX
	var right := size.x - READOUT_WIDTH_PX - GAP_PX
	var bottom := size.y - AXIS_BOTTOM_PX
	return Rect2(left, top, maxf(right - left, 0.0), maxf(bottom - top, 0.0))


## What the capture records for a shot: the state, the window, the cursor and the readout at the cursor.
func summary() -> Dictionary:
	var readout := readout_at(_profile, _cursor) if _state == State.READY else {}
	var clean := {}
	for key: Variant in readout:
		var value: Variant = readout[key]
		clean[key] = null if value is float and is_nan(value) else value
	return {
		"alignment_id": _alignment_id,
		"state": int(_state),
		"cursor_station_m": _cursor,
		"window_m": window_width_m(),
		"exaggeration": exaggeration(),
		"effective_factor": effective_factor(),
		"factor_text": _factor_label.text if _factor_label != null else "",
		"idealised_note": _idealised_note.visible,
		"readout": clean,
		"difference_text": _readout_labels["difference"].text if _readout_labels.has("difference") else "",
		"terrain_revision": int(_profile.get("terrain_revision", -1)),
		"cut_volume_m3": _profile.get("cut_volume_m3", null),
		"fill_volume_m3": _profile.get("fill_volume_m3", null),
	}


# --- static drawing maths -----------------------------------------------------------------------------------------

## The window of `width_m` centred on `cursor`, clamped inside [start, end]. A width of 0 (or one at least as wide as the
## line) is the whole line.
static func window_for(cursor: float, start: float, end: float, width_m: float) -> Vector2:
	if width_m <= 0.0 or width_m >= end - start:
		return Vector2(start, end)
	var lo := clampf(cursor - width_m * 0.5, start, end - width_m)
	return Vector2(lo, lo + width_m)


## The rows that a window [lo, hi] draws: the rows inside it, plus exactly one either side, as (first, last) indices.
static func row_range(stations: PackedFloat64Array, lo: float, hi: float) -> Vector2i:
	var count := stations.size()
	if count == 0:
		return Vector2i(0, -1)
	var first := maxi(_first_at_or_after(stations, lo) - 1, 0)
	var last := mini(_first_after(stations, hi), count - 1)
	return Vector2i(first, last)


## The vertical range to show: the data's range plus [constant MARGIN_FRACTION] of its span on each side. The visible span
## of a window `window_m` wide, with the panel `plot_w` x `plot_h` pixels and a vertical exaggeration, is
## `plot_h * window_m / (exaggeration * plot_w)`. The range is that span, or the fitted span when that is larger, centred
## on the data. An exaggeration of 0 is "Fit": the fitted span alone, and at least [constant FIT_MIN_SPAN_M].
static func vertical_range(data_lo: float, data_hi: float, window_m: float, plot_w: float, plot_h: float,
		exaggeration_factor: float) -> Vector2:
	var data_span := maxf(data_hi - data_lo, 0.0)
	var fitted := data_span * (1.0 + 2.0 * MARGIN_FRACTION)
	var fixed := exaggeration_factor > 0.0
	var visible := 0.0
	if fixed and plot_w > 0.0 and window_m > 0.0:
		visible = plot_h * window_m / (exaggeration_factor * plot_w)
	var floor_m := MIN_VERTICAL_SPAN_M if fixed else FIT_MIN_SPAN_M
	var span := maxf(maxf(fitted, visible), floor_m)
	var centre := (data_lo + data_hi) * 0.5
	return Vector2(centre - span * 0.5, centre + span * 0.5)


## The exaggeration a vertical span gives the window `window_m` wide in a plot `plot_w` x `plot_h` pixels (the inverse of the
## visible span in [method vertical_range]); 0 when there is nothing to show.
static func factor_for_span(span: float, window_m: float, plot_w: float, plot_h: float) -> float:
	if span <= 0.0 or window_m <= 0.0 or plot_w <= 0.0:
		return 0.0
	return plot_h * window_m / (span * plot_w)


## The label beside the vertical selector: the effective factor ("≈ 38×") when Fit is chosen (`chosen` 0), or when the range the
## data needs is more than [constant OVERRIDE_SHARE] larger than the chosen ratio gives. Empty otherwise.
static func factor_text(chosen: float, span: float, window_m: float, plot_w: float, plot_h: float) -> String:
	var factor := factor_for_span(span, window_m, plot_w, plot_h)
	if factor <= 0.0:
		return ""
	if chosen > 0.0 and factor >= chosen / (1.0 + OVERRIDE_SHARE):
		return ""
	var number := "%d" % roundi(factor) if factor >= 10.0 else "%.1f" % factor
	return "≈ %s×" % number


## The readout at `station`: the rows either side of it, interpolated linearly. The ground and the difference are NaN, and
## `has_ground` false, unless both rows have ground. The source is that of the nearer row, and `surface_model` says whether that
## row is covered by a surface model only (flag 8), which is what the ground line then reads.
static func readout_at(profile: Dictionary, station: float) -> Dictionary:
	var out := {
		"station": station, "rail_h": NAN, "ground_h": NAN, "difference": NAN, "has_ground": false,
		"cut_area": 0.0, "fill_area": 0.0, "source_name": "", "row": -1, "surface_model": false,
	}
	var stations: PackedFloat64Array = profile.get("station", PackedFloat64Array())
	var count := stations.size()
	if count == 0:
		return out
	var rail: PackedFloat64Array = profile.get("rail_h", PackedFloat64Array())
	var ground: PackedFloat64Array = profile.get("ground_h", PackedFloat64Array())
	var flags: PackedByteArray = profile.get("flags", PackedByteArray())
	var sources: PackedByteArray = profile.get("source", PackedByteArray())
	var cut: PackedFloat64Array = profile.get("cut_area", PackedFloat64Array())
	var fill: PackedFloat64Array = profile.get("fill_area", PackedFloat64Array())
	var names: Array = profile.get("source_names", [])

	var i := clampi(_row_before(stations, station), 0, count - 1)
	var j := mini(i + 1, count - 1)
	var t := 0.0
	if j > i and stations[j] > stations[i]:
		t = clampf((station - stations[i]) / (stations[j] - stations[i]), 0.0, 1.0)
	out["rail_h"] = lerpf(rail[i], rail[j], t)
	out["cut_area"] = lerpf(cut[i], cut[j], t)
	out["fill_area"] = lerpf(fill[i], fill[j], t)
	var ground_ok := not is_nan(ground[i]) and not is_nan(ground[j]) and not _flagged(flags, i) and not _flagged(flags, j)
	if ground_ok:
		out["ground_h"] = lerpf(ground[i], ground[j], t)
		out["difference"] = float(out["ground_h"]) - float(out["rail_h"])
		out["has_ground"] = true
	var nearer := i if t < 0.5 else j
	out["row"] = nearer
	out["surface_model"] = not ground_ok and _surface_flagged(flags, nearer)
	if nearer < sources.size() and sources[nearer] != SOURCE_NONE and sources[nearer] < names.size():
		out["source_name"] = String(names[sources[nearer]])
	return out


## The maximal runs of usable values (a value and no no-ground flag) among the rows `indices`, ascending, as (station, value)
## points. Rows that [method sample_rows] skipped lie inside one state, so a run continues across a skipped stretch.
static func sampled_runs(stations: PackedFloat64Array, values: PackedFloat64Array, flags: PackedByteArray,
		indices: PackedInt32Array) -> Array:
	var runs: Array = []
	var run := PackedVector2Array()
	for i in indices:
		if not is_nan(values[i]) and not _flagged(flags, i):
			run.append(Vector2(stations[i], values[i]))
		elif not run.is_empty():
			runs.append(run)
			run = PackedVector2Array()
	if not run.is_empty():
		runs.append(run)
	return runs


## The state of every row: whether its ground is usable, and whether it has cut or fill.
static func row_states(flags: PackedByteArray, ground: PackedFloat64Array, cut: PackedFloat64Array,
		fill: PackedFloat64Array) -> PackedByteArray:
	var out := PackedByteArray()
	out.resize(ground.size())
	for i in ground.size():
		var state := 0
		if not is_nan(ground[i]) and not _flagged(flags, i):
			state |= STATE_GROUND
		if i < cut.size() and cut[i] > 0.0:
			state |= STATE_CUT
		if i < fill.size() and fill[i] > 0.0:
			state |= STATE_FILL
		out[i] = state
	return out


## The rows of [first, last] where the state changes against a neighbour: the start and the end of each run.
static func run_end_rows(states: PackedByteArray, first: int, last: int) -> PackedInt32Array:
	var out := PackedInt32Array()
	var end := mini(last, states.size() - 1)
	for i in range(maxi(first, 0), end + 1):
		var starts := i > first and states[i] != states[i - 1]
		var ends := i < end and states[i] != states[i + 1]
		if starts or ends:
			out.append(i)
	return out


## The rows the lines draw between `first` and `last`, ascending: every k-th row, with k chosen so that there is about one row
## per pixel column of a plot `plot_w` wide, plus the two ends and the run ends of [method run_end_rows]. The count is at most
## `plot_w + 1` plus the run ends.
static func sample_rows(states: PackedByteArray, first: int, last: int, plot_w: float) -> PackedInt32Array:
	var out := PackedInt32Array()
	if last < first:
		return out
	var count := last - first + 1
	var stride := maxi(ceili(float(count) / maxf(plot_w, 1.0)), 1)
	var mark := PackedByteArray()
	mark.resize(count)
	for k in range(0, count, stride):
		mark[k] = 1
	mark[count - 1] = 1
	for row: int in run_end_rows(states, first, last):
		mark[row - first] = 1
	for k in count:
		if mark[k] != 0:
			out.append(first + k)
	return out


## Drops the repeated vertices of a polygon and returns it, or an empty polygon when fewer than three vertices, or an area below
## `min_area`, are left. The engine refuses a polygon with a repeated vertex or a zero area; nothing degenerate is returned here.
static func clean_polygon(points: PackedVector2Array, min_area: float, eps: float) -> PackedVector2Array:
	var out := PackedVector2Array()
	for point: Vector2 in points:
		if out.is_empty() or point.distance_to(out[out.size() - 1]) > eps:
			out.append(point)
	while out.size() > 1 and out[out.size() - 1].distance_to(out[0]) <= eps:
		out.resize(out.size() - 1)
	if out.size() < 3:
		return PackedVector2Array()
	var twice := 0.0
	for i in out.size():
		var a := out[i]
		var b := out[(i + 1) % out.size()]
		twice += a.x * b.y - b.x * a.y
	if absf(twice) * 0.5 < min_area:
		return PackedVector2Array()
	return out


## The part of a polyline with ascending stations inside [lo, hi], clipped at the two edges (its points keep their order).
static func clip_polyline_x(points: PackedVector2Array, lo: float, hi: float) -> PackedVector2Array:
	var out := PackedVector2Array()
	for i in range(points.size() - 1):
		var p := points[i]
		var q := points[i + 1]
		var t0 := 0.0
		var t1 := 1.0
		var dx := q.x - p.x
		if absf(dx) < 1e-12:
			if p.x < lo or p.x > hi:
				continue
		else:
			var ta := (lo - p.x) / dx
			var tb := (hi - p.x) / dx
			t0 = maxf(0.0, minf(ta, tb))
			t1 = minf(1.0, maxf(ta, tb))
			if t0 > t1:
				continue
		var a := p.lerp(q, t0)
		var b := p.lerp(q, t1)
		if out.is_empty() or out[out.size() - 1].distance_to(a) > EPS_DATA:
			out.append(a)
		out.append(b)
	return out


## A polygon clipped to the stations [lo, hi] (Sutherland-Hodgman on the two vertical edges).
static func clip_polygon_x(polygon: PackedVector2Array, lo: float, hi: float) -> PackedVector2Array:
	return _clip_half_plane(_clip_half_plane(polygon, lo, true), hi, false)


static func _clip_half_plane(polygon: PackedVector2Array, edge: float, keep_above: bool) -> PackedVector2Array:
	var out := PackedVector2Array()
	var count := polygon.size()
	for i in count:
		var p := polygon[i]
		var q := polygon[(i + 1) % count]
		var p_in := (p.x >= edge) if keep_above else (p.x <= edge)
		var q_in := (q.x >= edge) if keep_above else (q.x <= edge)
		if p_in:
			out.append(p)
		if p_in != q_in:
			var t := (edge - p.x) / (q.x - p.x)
			out.append(Vector2(edge, lerpf(p.y, q.y, t)))
	return out


## One polygon per maximal run of the rows `indices` (ascending) where the ground is on the cut side of the formation (`cut`) or
## the fill side, between the ground and the formation `rail - depth`. Consecutive indices are joined by a straight segment, so
## the rows may be a sample of the window (see [method sample_rows]). A run is cut where the ground crosses the formation, so the
## polygon's edges are the ground and the formation between the crossings. Degenerate runs are dropped.
static func band_runs(stations: PackedFloat64Array, ground: PackedFloat64Array, flags: PackedByteArray,
		rail: PackedFloat64Array, depth: float, indices: PackedInt32Array, cut: bool) -> Array:
	var polygons: Array = []
	for span: Array in _band_spans(stations, ground, flags, rail, depth, indices, cut):
		var top: PackedVector2Array = span[0]
		var bottom: PackedVector2Array = span[1]
		var ring := top.duplicate()
		for k in range(bottom.size() - 1, -1, -1):
			ring.append(bottom[k])
		var clean := clean_polygon(ring, AREA_MIN_DATA, EPS_DATA)
		if not clean.is_empty():
			polygons.append(clean)
	return polygons


## The [top, bottom] point lists of each run of [band_runs]: the ground (or the formation, on the fill side) and the formation,
## at the crossings and the rows of the run.
static func _band_spans(stations: PackedFloat64Array, ground: PackedFloat64Array, flags: PackedByteArray,
		rail: PackedFloat64Array, depth: float, indices: PackedInt32Array, cut: bool) -> Array:
	var spans: Array = []
	var top := PackedVector2Array()
	var bottom := PackedVector2Array()
	var side := 1.0 if cut else -1.0
	for k in maxi(indices.size() - 1, 0):
		var i := indices[k]
		var i1 := indices[k + 1]
		var s0 := stations[i]
		var s1 := stations[i1]
		var g0 := ground[i]
		var g1 := ground[i1]
		var f0 := rail[i] - depth
		var f1 := rail[i1] - depth
		if is_nan(g0) or is_nan(g1) or _flagged(flags, i) or _flagged(flags, i1):
			if top.size() >= 2:
				spans.append([top, bottom])
			top = PackedVector2Array()
			bottom = PackedVector2Array()
			continue
		var e0 := (g0 - f0) * side
		var e1 := (g1 - f1) * side
		if e0 > 0.0 and e1 > 0.0:
			if top.is_empty():
				top.append(Vector2(s0, g0))
				bottom.append(Vector2(s0, f0))
			top.append(Vector2(s1, g1))
			bottom.append(Vector2(s1, f1))
		elif e0 > 0.0 or e1 > 0.0:
			var t := e0 / (e0 - e1)
			var sc := lerpf(s0, s1, t)
			var hc := lerpf(f0, f1, t)
			if e0 > 0.0:
				if top.is_empty():
					top.append(Vector2(s0, g0))
					bottom.append(Vector2(s0, f0))
				top.append(Vector2(sc, hc))
				bottom.append(Vector2(sc, hc))
				spans.append([top, bottom])
				top = PackedVector2Array()
				bottom = PackedVector2Array()
			else:
				top = PackedVector2Array([Vector2(sc, hc), Vector2(s1, g1)])
				bottom = PackedVector2Array([Vector2(sc, hc), Vector2(s1, f1)])
		else:
			if top.size() >= 2:
				spans.append([top, bottom])
			top = PackedVector2Array()
			bottom = PackedVector2Array()
	if top.size() >= 2:
		spans.append([top, bottom])
	return spans


## Hatching lines for a rectangle, at 45 degrees, `spacing` pixels apart: pairs of points, each pair one line, clipped
## to the rectangle.
static func hatch_segments(rect: Rect2, spacing: float) -> PackedVector2Array:
	var out := PackedVector2Array()
	if rect.size.x <= 0.0 or rect.size.y <= 0.0 or spacing <= 0.0:
		return out
	var x0 := rect.position.x
	var x1 := rect.end.x
	var y0 := rect.position.y
	var y1 := rect.end.y
	var c := x0 - y1
	while c <= x1 - y0:
		var ya := maxf(y0, x0 - c)
		var yb := minf(y1, x1 - c)
		if yb >= ya:
			out.append(Vector2(ya + c, ya))
			out.append(Vector2(yb + c, yb))
		c += spacing
	return out


## The stations a no-ground span hatches between: the valid station before its first flagged row and the one after its last.
static func hatch_bounds(stations: PackedFloat64Array, span_from: float, span_to: float) -> Vector2:
	var count := stations.size()
	if count == 0:
		return Vector2(span_from, span_to)
	var first := _row_before(stations, span_from)
	var last := _row_before(stations, span_to)
	var before := maxi(first - 1, 0)
	var after := mini(last + 1, count - 1)
	return Vector2(stations[before], stations[after])


## The multiples of `step` between `lo` and `hi`, ascending; empty for a step of zero.
static func ticks_at(lo: float, hi: float, step: float) -> PackedFloat64Array:
	var ticks := PackedFloat64Array()
	if not (step > 0.0) or not (hi > lo) or is_inf(hi - lo):
		return ticks
	var first: float = ceilf(lo / step - 1e-9)
	var k := 0
	while (first + k) * step <= hi + step * 1e-9:
		ticks.append((first + k) * step)
		k += 1
	return ticks


## The finest round step (one of `factors` times a power of ten) whose ticks are at least `min_px` apart, when `lo` to `hi` spans
## `px` pixels. Zero when the range or the pixels are empty.
static func spaced_step(lo: float, hi: float, px: float, min_px: float, factors: Array) -> float:
	var span := hi - lo
	if not (span > 0.0) or is_inf(span) or not (px > 0.0) or not (min_px > 0.0):
		return 0.0
	var magnitude: float = pow(10.0, floorf(log(span * min_px / px) / log(10.0)) - 1.0)
	for _order in 12:
		for factor: float in factors:
			var step: float = factor * magnitude
			if step / span * px >= min_px:
				return step
		magnitude *= 10.0
	return 0.0


## The decimals a tick label needs at a tick step: none for 20 or 25, one for 0.5 or 2.5, two for 0.05. Up to six.
static func tick_decimals(step: float) -> int:
	if not (step > 0.0):
		return 0
	var decimals := 0
	while decimals < 6 and absf(step * pow(10.0, decimals) - roundf(step * pow(10.0, decimals))) > 1e-9:
		decimals += 1
	return decimals


## Decimals of the km labels: 3 at a 1 km window, 2 at about 5 km, 1 for a wider one.
static func km_decimals(window_m: float) -> int:
	if window_m <= 0.0 or window_m > 7000.0:
		return 1
	if window_m <= 1500.0:
		return 3
	return 2


## Whether the alignment `alignment_id` is marked idealised in `entries` (the `AlignmentSummary` dictionaries).
static func is_idealised(entries: Array, alignment_id: String) -> bool:
	for entry: Variant in entries:
		if entry is Dictionary and String((entry as Dictionary).get("alignment_id", "")) == alignment_id:
			return bool((entry as Dictionary).get("profile_idealised", false))
	return false


static func format_km(station: float) -> String:
	return "km %.3f" % (station / 1000.0)


## The ground minus the rail: "+17.36 m" or, with the true minus sign, "−0.40 m".
static func format_difference(difference: float) -> String:
	return ("%+.2f m" % difference).replace("-", MINUS)


static func _row_before(stations: PackedFloat64Array, station: float) -> int:
	var lo := 0
	var hi := stations.size() - 1
	while lo < hi:
		var mid := (lo + hi + 1) >> 1
		if stations[mid] <= station:
			lo = mid
		else:
			hi = mid - 1
	return lo


## The first row whose station is at least `station` (the row count when there is none).
static func _first_at_or_after(stations: PackedFloat64Array, station: float) -> int:
	var lo := 0
	var hi := stations.size()
	while lo < hi:
		var mid := (lo + hi) >> 1
		if stations[mid] < station:
			lo = mid + 1
		else:
			hi = mid
	return lo


## The first row whose station is above `station` (the row count when there is none).
static func _first_after(stations: PackedFloat64Array, station: float) -> int:
	var lo := 0
	var hi := stations.size()
	while lo < hi:
		var mid := (lo + hi) >> 1
		if stations[mid] <= station:
			lo = mid + 1
		else:
			hi = mid
	return lo


static func _flagged(flags: PackedByteArray, index: int) -> bool:
	return index < flags.size() and (flags[index] & FLAG_NO_GROUND) != 0


static func _surface_flagged(flags: PackedByteArray, index: int) -> bool:
	return index < flags.size() and (flags[index] & FLAG_SURFACE_MODEL) != 0


# --- building -----------------------------------------------------------------------------------------------------

func _build() -> void:
	# One flow row for the selectors and the legend: it no wider than the plot, so it wraps to a second row when the plot is narrow
	# and never reaches the readout column.
	_controls = HFlowContainer.new()
	_controls.name = "Controls"
	_controls.add_theme_constant_override(&"h_separation", 8)
	_controls.add_theme_constant_override(&"v_separation", 4)
	add_child(_controls)

	_window_choice = OptionButton.new()
	_window_choice.name = "Window"
	_window_choice.tooltip_text = WINDOW_TOOLTIP
	for i in WINDOW_LABELS.size():
		_window_choice.add_item(WINDOW_LABELS[i], i)
	_window_choice.select(_window_index)
	_window_choice.item_selected.connect(_on_window_selected)
	_controls.add_child(_window_choice)

	_exaggeration_choice = OptionButton.new()
	_exaggeration_choice.name = "Exaggeration"
	_exaggeration_choice.tooltip_text = EXAGGERATION_TOOLTIP
	for i in EXAGGERATION_LABELS.size():
		_exaggeration_choice.add_item(EXAGGERATION_LABELS[i], i)
	_exaggeration_choice.select(_exaggeration_index)
	_exaggeration_choice.item_selected.connect(_on_exaggeration_selected)
	_controls.add_child(_exaggeration_choice)

	_factor_label = Label.new()
	_factor_label.name = "Factor"
	_factor_label.theme_type_variation = &"MutedLabel"
	_factor_label.visible = false
	_controls.add_child(_factor_label)
	# The flow container sorts its children when it is laid out, so its minimum height is final only after that: lay out again then.
	_controls.minimum_size_changed.connect(_on_controls_minimum_changed)

	_legend_items.clear()
	_add_legend_item("cut", LEGEND_CUT, "")
	_add_legend_item("fill", LEGEND_FILL, "")
	_add_legend_item("no_ground", LEGEND_NO_GROUND, LEGEND_NO_GROUND_TOOLTIP)

	_readout = VBoxContainer.new()
	_readout.name = "Readout"
	_readout.add_theme_constant_override(&"separation", 2)
	add_child(_readout)
	for key: String in ["station", "rail", "ground", "difference", "cut", "fill", "source"]:
		var label := Label.new()
		label.name = key.capitalize()
		# A value too long for the column ends in an ellipsis, and its full text is the tooltip (see _set_readout).
		label.clip_text = true
		label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		label.mouse_filter = Control.MOUSE_FILTER_PASS
		_readout.add_child(label)
		_readout_labels[key] = label
	_idealised_note = Label.new()
	_idealised_note.name = "IdealisedNote"
	_idealised_note.theme_type_variation = &"HintLabel"
	_idealised_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_idealised_note.text = IDEALISED_TEXT
	_idealised_note.visible = false
	_readout.add_child(_idealised_note)

	_status = Label.new()
	_status.name = "Status"
	_status.theme_type_variation = &"MutedLabel"
	_status.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_status.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	add_child(_status)
	_retry = Button.new()
	_retry.name = "Retry"
	_retry.text = RETRY_TEXT
	_retry.visible = false
	_retry.pressed.connect(_on_retry_pressed)
	add_child(_retry)


func _add_legend_item(key: String, text: String, tooltip: String) -> void:
	var row := HBoxContainer.new()
	row.name = "Legend" + text.replace(" ", "")
	row.add_theme_constant_override(&"separation", 4)
	row.tooltip_text = tooltip
	var swatch := ColorRect.new()
	swatch.color = _colour(key)
	swatch.custom_minimum_size = Vector2(12.0, 12.0)
	swatch.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	swatch.mouse_filter = Control.MOUSE_FILTER_IGNORE
	row.add_child(swatch)
	_legend_swatches[key] = swatch
	var label := Label.new()
	label.text = text
	row.add_child(label)
	_legend_items.append(row)
	_controls.add_child(row)


## The theme arrives after the panel is built, so the swatches take their colours again with each theme change.
func _notification(what: int) -> void:
	if what == NOTIFICATION_THEME_CHANGED:
		for key: String in _legend_swatches.keys():
			var swatch: ColorRect = _legend_swatches[key]
			swatch.color = _colour(key)


func _on_controls_minimum_changed() -> void:
	call_deferred(&"_layout")


func _colour(item: String) -> Color:
	var name := StringName(item)
	if has_theme_color(name, &"ProfilePanel"):
		return get_theme_color(name, &"ProfilePanel")
	return _COLOURS[item]


func _layout() -> void:
	if not is_node_ready():
		return
	# The controls take the plot's width first: their height is the height of the rows they wrap to at that width.
	var width := maxf(size.x - AXIS_LEFT_PX - READOUT_WIDTH_PX - GAP_PX, 0.0)
	_controls.position = Vector2(AXIS_LEFT_PX, 0.0)
	_controls.size = Vector2(width, 0.0)
	_controls.size = Vector2(width, _controls.get_combined_minimum_size().y)
	_readout.position = Vector2(size.x - READOUT_WIDTH_PX, 0.0)
	_readout.size = Vector2(READOUT_WIDTH_PX, size.y)
	var plot := plot_rect()
	var status_min := _status.get_combined_minimum_size()
	if _state == State.READY:
		_status.position = plot.position + Vector2(GAP_PX, GAP_PX)
		_status.size = Vector2(maxf(plot.size.x - 2.0 * GAP_PX, 0.0), status_min.y)
	else:
		_status.position = plot.position + Vector2(0.0, plot.size.y * 0.5 - status_min.y)
		_status.size = Vector2(plot.size.x, status_min.y)
	_retry.position = _status.position + Vector2((_status.size.x - _retry.get_combined_minimum_size().x) * 0.5, _status.size.y + 6.0)
	_retry.size = _retry.get_combined_minimum_size()
	_refresh_factor_label()
	queue_redraw()


func _on_resized() -> void:
	_layout()


# --- fetching -----------------------------------------------------------------------------------------------------

## A project, alignment or terrain change: whatever was fetched before may be for an input that no longer holds, so a fetch in
## flight is discarded when it returns (the generation changes), and the next one starts from the new inputs.
func _on_inputs_changed() -> void:
	_generation += 1
	_dirty = true
	_refresh_alignment()
	_refresh_note()
	_maybe_request()


func _on_sources_changed(kind: String) -> void:
	if kind == "terrain":
		_on_inputs_changed()


func _on_window_selected(index: int) -> void:
	set_window_index(index)


func _on_exaggeration_selected(index: int) -> void:
	set_exaggeration_index(index)


func _on_retry_pressed() -> void:
	_dirty = true
	_maybe_request()


## The alignment the panel shows: the bound playback's, else the project's first.
func _refresh_alignment() -> void:
	var id := ""
	if _controller != null and is_instance_valid(_controller):
		id = _controller.alignment_id()
	if id.is_empty():
		var alignments := Session.alignments()
		if not alignments.is_empty():
			id = String(alignments[0].get("alignment_id", ""))
	if id == _alignment_id:
		return
	_generation += 1
	_alignment_id = id
	_store_profile({})
	_dirty = true
	_state = State.NO_ALIGNMENT if id.is_empty() else State.LOADING
	_refresh_bounds()
	_refresh_note()
	_update_status()
	_refresh_readout()
	queue_redraw()


## Starts a fetch when the panel is visible and something it shows is out of date (or it has nothing to show yet).
func _maybe_request() -> void:
	if not is_inside_tree() or not is_visible_in_tree():
		return
	if _alignment_id.is_empty():
		_state = State.NO_ALIGNMENT
		_update_status()
		return
	if _dirty or (_profile.is_empty() and _state != State.LOADING):
		_request()


func _request() -> void:
	_generation += 1
	var generation := _generation
	_dirty = false
	_state = State.LOADING
	_update_status()
	var alignment := _alignment_id
	var fetched: Dictionary = await Session.fetch_ground_profile(alignment, SPACING_M)
	# A result is kept only while its inputs are the panel's: the project closed, or the alignment changed, while it ran.
	if not is_instance_valid(self) or generation != _generation or alignment != _alignment_id:
		return
	if fetched.is_empty():
		_store_profile({})
		_error = Session.ground_profile_error()
		_state = State.FAILED
	else:
		_store_profile(fetched)
		_error = ""
		_state = State.READY
	_refresh_bounds()
	_refresh_note()
	_update_status()
	_refresh_readout()
	queue_redraw()


# --- state, readout and cursor ----------------------------------------------------------------------------------

func _refresh_bounds() -> void:
	_alignment_start = 0.0
	_alignment_end = 0.0
	for entry: Variant in Session.alignments():
		if entry is Dictionary and String((entry as Dictionary).get("alignment_id", "")) == _alignment_id:
			_alignment_start = float((entry as Dictionary).get("station_start", 0.0))
			_alignment_end = float((entry as Dictionary).get("station_end", 0.0))
	if _state == State.READY and not _profile.is_empty():
		_alignment_start = float(_profile.get("station_start", _alignment_start))
		_alignment_end = float(_profile.get("station_end", _alignment_end))
	if not _has_cursor:
		_cursor = _alignment_start


func _refresh_note() -> void:
	if _idealised_note != null:
		_idealised_note.visible = is_idealised(Session.alignments(), _alignment_id)


func _update_status() -> void:
	if _status == null:
		return
	var text := ""
	match _state:
		State.NO_ALIGNMENT:
			text = NO_ALIGNMENT_TEXT
		State.LOADING:
			text = LOADING_TEXT
		State.FAILED:
			text = FAILED_TEXT if _error.is_empty() else "%s\n%s" % [FAILED_TEXT, _error]
		State.READY:
			text = _no_bare_ground_text()
	_status.text = text
	_status.visible = not text.is_empty()
	_retry.visible = _state == State.FAILED
	_layout()


## The status of a line with no bare ground in any row: the surface model's name when a surface model covers a row, else the
## "no terrain" message. Empty when some row has bare ground.
func _no_bare_ground_text() -> String:
	var flags: PackedByteArray = _profile.get("flags", PackedByteArray())
	if flags.is_empty():
		return ""
	var surface_row := -1
	for i in flags.size():
		if (flags[i] & FLAG_NO_GROUND) == 0:
			return ""
		if surface_row < 0 and (flags[i] & FLAG_SURFACE_MODEL) != 0:
			surface_row = i
	if surface_row < 0:
		return NO_TERRAIN_TEXT
	return SURFACE_ONLY_TEXT % _source_name_at(surface_row)


## The name of the source of one row, or the fallback when the row names none.
func _source_name_at(row: int) -> String:
	var sources: PackedByteArray = _profile.get("source", PackedByteArray())
	var names: Array = _profile.get("source_names", [])
	if row < sources.size() and sources[row] != SOURCE_NONE and sources[row] < names.size():
		return String(names[sources[row]])
	return SURFACE_MODEL_FALLBACK_NAME


## True when every row is flagged as having no ground, whatever else it carries: the hatching then covers the whole line, so
## it is not drawn (the line is one span).
func _every_row_flagged() -> bool:
	var flags: PackedByteArray = _profile.get("flags", PackedByteArray())
	if flags.is_empty():
		return false
	for flag: int in flags:
		if (flag & FLAG_NO_GROUND) == 0:
			return false
	return true


func _refresh_readout() -> void:
	if _readout_labels.is_empty():
		return
	_set_readout("station", format_km(_cursor))
	if _state != State.READY:
		for key: String in ["rail", "ground", "difference", "cut", "fill", "source"]:
			_set_readout(key, "—")
		_refresh_factor_label()
		return
	var readout := readout_at(_profile, _cursor)
	_set_readout("rail", "Rail %.2f m" % float(readout["rail_h"]))
	if bool(readout["has_ground"]):
		_set_readout("ground", "Ground %.2f m" % float(readout["ground_h"]))
		_set_readout("difference", "Ground − rail %s" % format_difference(float(readout["difference"])))
	elif bool(readout["surface_model"]):
		# The source line names the surface model, as it names every row's source.
		_set_readout("ground", "Ground " + SURFACE_ONLY_READOUT)
		_set_readout("difference", "Ground − rail —")
	else:
		_set_readout("ground", "Ground " + NO_GROUND_READOUT)
		_set_readout("difference", "Ground − rail —")
	# A row with no bare ground has no cut or fill to report, so its areas read "—", not 0.0 m².
	if _flagged(_profile["flags"], int(readout["row"])):
		_set_readout("cut", "Cut —")
		_set_readout("fill", "Fill —")
	else:
		_set_readout("cut", "Cut %.1f m²" % float(readout["cut_area"]))
		_set_readout("fill", "Fill %.1f m²" % float(readout["fill_area"]))
	var source := String(readout["source_name"])
	_set_readout("source", "Source " + (source if not source.is_empty() else "none"))
	tooltip_text = "Terrain revision %d" % int(_profile.get("terrain_revision", -1))
	_refresh_factor_label()


## Sets one readout value and its tooltip, which is its full text: a value the column cuts short is then still readable.
func _set_readout(key: String, text: String) -> void:
	var label: Label = _readout_labels[key]
	label.text = text
	label.tooltip_text = text


## The label beside the vertical selector: the factor the drawing shows, when it is Fit or the data's range overrides the choice.
func _refresh_factor_label() -> void:
	if _factor_label == null:
		return
	var text := ""
	if _state == State.READY and not _profile.is_empty():
		var plot := plot_rect()
		var extent := _view_vertical()
		var window := _window_bounds()
		text = factor_text(exaggeration(), extent.y - extent.x, window.y - window.x, plot.size.x, plot.size.y)
	if _factor_label.text == text and _factor_label.visible == not text.is_empty():
		return
	# The label may wrap the controls onto another row, which moves the plot: lay out again (the second pass finds no change).
	_factor_label.text = text
	_factor_label.visible = not text.is_empty()
	_layout()


## The vertical range the drawing uses now: the rows of the window, at the plot's size. It is memoised on the window, the plot, the
## exaggeration and the stored profile, so the readout's label and the redraw that follows share one computation.
func _view_vertical() -> Vector2:
	var plot := plot_rect()
	if _state != State.READY or plot.size.x <= 0.0 or plot.size.y <= 0.0:
		return Vector2.ZERO
	var window := _window_bounds()
	var key := [window, plot.size, exaggeration(), _profile_serial]
	if key != _vertical_key:
		_vertical_key = key
		_vertical_memo = _vertical_extent(window, row_range(_profile["station"], window.x, window.y), plot)
	return _vertical_memo


func _process(_delta: float) -> void:
	if not is_visible_in_tree():
		return
	if _controller != null and is_instance_valid(_controller):
		_cursor = _controller.lead_station()
	if _state != State.READY:
		return
	if absf(_cursor - _drawn_cursor) >= _metres_per_pixel():
		_drawn_cursor = _cursor
		_refresh_readout()
		queue_redraw()


func _request_draw() -> void:
	_drawn_cursor = INF
	_refresh_readout()
	queue_redraw()


## Metres of the line per pixel of the drawing: a cursor that moves less than this does not need a redraw.
func _metres_per_pixel() -> float:
	var plot := plot_rect()
	if plot.size.x <= 0.0:
		return INF
	var window := _window_bounds()
	return maxf(window.y - window.x, 1.0) / plot.size.x


func _window_bounds() -> Vector2:
	var start := _alignment_start
	var end := _alignment_end
	if _state == State.READY:
		start = float(_profile.get("station_start", start))
		end = float(_profile.get("station_end", end))
	return window_for(_cursor, start, end, window_width_m())


# --- drawing --------------------------------------------------------------------------------------------------------

func _draw() -> void:
	_draw_calls += 1
	_drawn_texts.clear()
	_drawn_strings = PackedStringArray()
	_drawn_plates.clear()
	_shading_vertices = 0
	if _state != State.READY or _profile.is_empty():
		return
	var plot := plot_rect()
	if plot.size.x <= 0.0 or plot.size.y <= 0.0:
		return
	var stations: PackedFloat64Array = _profile["station"]
	var rail: PackedFloat64Array = _profile["rail_h"]
	var ground: PackedFloat64Array = _profile["ground_h"]
	var flags: PackedByteArray = _profile["flags"]
	var window := _window_bounds()
	var rows := row_range(stations, window.x, window.y)
	if rows.y < rows.x:
		return
	var vertical := _view_vertical()
	var depth := float(_formation_template().get("formation_depth_m", FORMATION_DEPTH_M))
	var drawn := sample_rows(_states, rows.x, rows.y, plot.size.x)

	_draw_grid(plot, window, vertical)
	_draw_spans(plot, window, vertical, stations)
	_draw_shading(plot, window, vertical, drawn, rail, depth)
	for run: PackedVector2Array in sampled_runs(stations, ground, flags, drawn):
		_draw_line(run, plot, window, vertical, _colour("ground"), GROUND_WIDTH_PX)
	_draw_line(_line_points(stations, rail, 0.0, drawn), plot, window, vertical, _colour("rail"), RAIL_WIDTH_PX)
	_draw_line(_line_points(stations, rail, -depth, drawn), plot, window, vertical, _colour("formation"), 1.0)
	if _has_cursor:
		var x := _x_of(_cursor, plot, window)
		draw_line(Vector2(x, plot.position.y), Vector2(x, plot.end.y), _colour("cursor"), CURSOR_WIDTH_PX, true)
	_draw_axes(plot, window, vertical)


func _formation_template() -> Dictionary:
	var template: Variant = _profile.get("template", {})
	return template if template is Dictionary else {}


## The range the window's rows need: the rail, the formation and the usable ground of every row drawn, the one either side included,
## so that a line running out of the window stays inside the range too.
func _vertical_extent(window: Vector2, rows: Vector2i, plot: Rect2) -> Vector2:
	_vertical_computes += 1
	var rail: PackedFloat64Array = _profile["rail_h"]
	var ground: PackedFloat64Array = _profile["ground_h"]
	var flags: PackedByteArray = _profile["flags"]
	var depth := float(_formation_template().get("formation_depth_m", FORMATION_DEPTH_M))
	var lo := INF
	var hi := -INF
	for i in range(rows.x, rows.y + 1):
		lo = minf(lo, minf(rail[i], rail[i] - depth))
		hi = maxf(hi, maxf(rail[i], rail[i] - depth))
		if not is_nan(ground[i]) and not _flagged(flags, i):
			lo = minf(lo, ground[i])
			hi = maxf(hi, ground[i])
	if lo > hi:
		lo = 0.0
		hi = 0.0
	return vertical_range(lo, hi, window.y - window.x, plot.size.x, plot.size.y, exaggeration())


static func _x_of(station: float, plot: Rect2, window: Vector2) -> float:
	var span := maxf(window.y - window.x, 1e-9)
	return plot.position.x + (station - window.x) / span * plot.size.x


static func _y_of(height: float, plot: Rect2, vertical: Vector2) -> float:
	var span := maxf(vertical.y - vertical.x, 1e-9)
	return plot.position.y + plot.size.y * (1.0 - (height - vertical.x) / span)


static func _to_pixels(data: PackedVector2Array, plot: Rect2, window: Vector2, vertical: Vector2) -> PackedVector2Array:
	var out := PackedVector2Array()
	out.resize(data.size())
	for i in data.size():
		out[i] = Vector2(_x_of(data[i].x, plot, window), _y_of(data[i].y, plot, vertical))
	return out


## A band polygon of data as the drawing draws it: clipped to the window, in the plot's pixels, and empty when it is degenerate
## there: too little area (see [constant AREA_MIN_PIXELS]) or too thin for its length (see [constant THICKNESS_MIN_PIXELS]).
## What [method _draw_shading] hands to the engine.
static func shading_pixels(polygon: PackedVector2Array, window: Vector2, plot: Rect2, vertical: Vector2) -> PackedVector2Array:
	var clipped := clip_polygon_x(polygon, window.x, window.y)
	var pixels := clean_polygon(_to_pixels(clipped, plot, window, vertical), AREA_MIN_PIXELS, EPS_PIXELS)
	if pixels.is_empty():
		return pixels
	var lo := pixels[0].x
	var hi := pixels[0].x
	for point: Vector2 in pixels:
		lo = minf(lo, point.x)
		hi = maxf(hi, point.x)
	if polygon_area(pixels) < THICKNESS_MIN_PIXELS * (hi - lo):
		return PackedVector2Array()
	return pixels


## The area of a polygon by the shoelace formula, in its own units.
static func polygon_area(points: PackedVector2Array) -> float:
	var twice := 0.0
	for i in points.size():
		var a := points[i]
		var b := points[(i + 1) % points.size()]
		twice += a.x * b.y - b.x * a.y
	return absf(twice) * 0.5


## One polygon per run of cut and per run of fill (see [method band_runs]) over the drawn rows, clipped to the window. The cut is
## drawn first, so a fill never covers it.
func _draw_shading(plot: Rect2, window: Vector2, vertical: Vector2, drawn: PackedInt32Array, rail: PackedFloat64Array,
		depth: float) -> void:
	var stations: PackedFloat64Array = _profile["station"]
	var ground: PackedFloat64Array = _profile["ground_h"]
	var flags: PackedByteArray = _profile["flags"]
	for cut_side: bool in [true, false]:
		var colour := _colour("cut") if cut_side else _colour("fill")
		for polygon: PackedVector2Array in band_runs(stations, ground, flags, rail, depth, drawn, cut_side):
			var pixels := shading_pixels(polygon, window, plot, vertical)
			if not pixels.is_empty():
				_shading_vertices += pixels.size()
				draw_colored_polygon(pixels, colour)


## The points (station, rail height + `offset`) of the rows `indices`, ascending.
static func _line_points(stations: PackedFloat64Array, heights: PackedFloat64Array, offset: float,
		indices: PackedInt32Array) -> PackedVector2Array:
	var out := PackedVector2Array()
	out.resize(indices.size())
	for k in indices.size():
		var i := indices[k]
		out[k] = Vector2(stations[i], heights[i] + offset)
	return out


## Draws a polyline of (station, height) points, clipped to the window's stations, in the drawing's pixels.
func _draw_line(points: PackedVector2Array, plot: Rect2, window: Vector2, vertical: Vector2, colour: Color, width: float) -> void:
	var clipped := clip_polyline_x(points, window.x, window.y)
	if clipped.size() >= 2:
		draw_polyline(_to_pixels(clipped, plot, window, vertical), colour, width, true)


## Spans of no ground in the window, hatched from the valid station before each to the one after it. Nothing is hatched when
## no ground exists anywhere, as then the whole line is one span.
func _draw_spans(plot: Rect2, window: Vector2, vertical: Vector2, stations: PackedFloat64Array) -> void:
	if _every_row_flagged():
		return
	var font := get_theme_default_font()
	var font_size := get_theme_default_font_size()
	var colour := _colour("no_ground")
	var placed: Array[Rect2] = []
	for span: Variant in _profile.get("no_ground_spans", []):
		var pair: Array = span
		var bounds := hatch_bounds(stations, float(pair[0]), float(pair[1]))
		if bounds.y < window.x or bounds.x > window.y:
			continue
		var left := maxf(_x_of(bounds.x, plot, window), plot.position.x)
		var right := minf(_x_of(bounds.y, plot, window), plot.end.x)
		if right <= left:
			continue
		var rect := Rect2(left, plot.position.y, right - left, plot.size.y)
		var lines := hatch_segments(rect, HATCH_SPACING_PX)
		for k in range(0, lines.size(), 2):
			draw_line(lines[k], lines[k + 1], colour, 1.0, true)
		_draw_span_label(plot, rect, font, font_size, colour, placed)


## The no-ground label of a span. Inside the span when the span is wide enough for the text; else beside it, left first, where
## the text fits inside the plot; else not at all (the legend still names the hatching). A label that would overlap one already
## placed is skipped. The label sits on a plate of the panel's background, so the hatching does not run through it.
func _draw_span_label(plot: Rect2, rect: Rect2, font: Font, font_size: int, colour: Color, placed: Array[Rect2]) -> void:
	var text_width := font.get_string_size(NO_GROUND_LABEL, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	var x := 0.0
	if rect.size.x >= text_width + 2.0 * LABEL_PAD_PX:
		x = rect.position.x + LABEL_PAD_PX
	elif rect.position.x - LABEL_GAP_PX - text_width >= plot.position.x:
		x = rect.position.x - LABEL_GAP_PX - text_width
	elif rect.end.x + LABEL_GAP_PX + text_width <= plot.end.x:
		x = rect.end.x + LABEL_GAP_PX
	else:
		return
	var baseline := rect.position.y + font_size + 4.0
	var ascent := font.get_ascent(font_size)
	var box := Rect2(x, baseline - ascent, text_width, font.get_height(font_size))
	for other: Rect2 in placed:
		if other.grow(LABEL_CLEAR_PX).intersects(box):
			return
	placed.append(box)
	var plate := _colour("background")
	draw_rect(box.grow(LABEL_PLATE_PAD_PX), Color(plate.r, plate.g, plate.b, LABEL_PLATE_ALPHA))
	_drawn_plates.append(box.grow(LABEL_PLATE_PAD_PX))
	_draw_text(font, Vector2(x, baseline), NO_GROUND_LABEL, font_size, colour)


## Draws a text at its baseline and records the rectangle it covers, so the tests can check the text against the readout.
func _draw_text(font: Font, baseline: Vector2, text: String, font_size: int, colour: Color) -> void:
	var width := font.get_string_size(text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	_drawn_texts.append(Rect2(baseline.x, baseline.y - font.get_ascent(font_size), width, font.get_height(font_size)))
	_drawn_strings.append(text)
	draw_string(font, baseline, text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size, colour)


## The step of the km ticks of the window, in km: the finest round step whose labels keep [constant LABEL_GAP_PX] between them,
## measured with the widest label the window's decimals give.
func _km_step(window: Vector2, plot: Rect2) -> float:
	var sample := "13." + "0".repeat(km_decimals(window.y - window.x))
	var width := get_theme_default_font().get_string_size(sample, HORIZONTAL_ALIGNMENT_LEFT, -1, get_theme_default_font_size()).x
	return spaced_step(window.x / 1000.0, window.y / 1000.0, plot.size.x, width + LABEL_GAP_PX, [1.0, 2.0, 5.0])


## The step of the metre ticks, in metres: the finest of 1, 2, 2.5 or 5 times a power of ten whose labels are at least 1.2 label
## heights apart. At the 5 km window and 10x in a 150 px plot this gives 20 m or 25 m.
func _metre_step(vertical: Vector2, plot: Rect2) -> float:
	var line := get_theme_default_font().get_height(get_theme_default_font_size())
	return spaced_step(vertical.x, vertical.y, plot.size.y, line * METRE_LABEL_SPACING, [1.0, 2.0, 2.5, 5.0])


func _draw_grid(plot: Rect2, window: Vector2, vertical: Vector2) -> void:
	var colour := _colour("grid")
	for km: float in ticks_at(window.x / 1000.0, window.y / 1000.0, _km_step(window, plot)):
		var x := _x_of(km * 1000.0, plot, window)
		draw_line(Vector2(x, plot.position.y), Vector2(x, plot.end.y), colour, 1.0)
	for value: float in ticks_at(vertical.x, vertical.y, _metre_step(vertical, plot)):
		var y := _y_of(value, plot, vertical)
		draw_line(Vector2(plot.position.x, y), Vector2(plot.end.x, y), colour, 1.0)


func _draw_axes(plot: Rect2, window: Vector2, vertical: Vector2) -> void:
	var font := get_theme_default_font()
	var font_size := get_theme_default_font_size()
	var text_colour := _colour("rail")
	# Each axis has the decimals its tick step needs; the km axis keeps the decimals of its window width as well.
	var km_step := _km_step(window, plot)
	var km_format := "%." + str(maxi(km_decimals(window.y - window.x), tick_decimals(km_step))) + "f"
	# A km label is centred on its tick, so the spacing of the step keeps the labels apart. One that would reach the axis column or
	# the readout column is left out (its grid line stays); none is moved, since a moved label could land on its neighbour.
	var limit := size.x - READOUT_WIDTH_PX - READOUT_MARGIN_PX
	for km: float in ticks_at(window.x / 1000.0, window.y / 1000.0, km_step):
		var x := _x_of(km * 1000.0, plot, window)
		var label := km_format % km
		var width := font.get_string_size(label, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
		var left := x - width * 0.5
		if left < plot.position.x or left + width > limit:
			continue
		_draw_text(font, Vector2(left, plot.end.y + font_size + 4.0), label, font_size, text_colour)
	# The units sit in the axis column, right-aligned like the tick labels: metres at the top, above every tick, and km on the
	# km labels' row, left of the first label. They are placed first, so a metre label that would touch one is left out.
	var line := font.get_height(font_size)
	var ascent := font.get_ascent(font_size)
	var metre_width := font.get_string_size("m", HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	var metre_base := Vector2(plot.position.x - metre_width - UNIT_GAP_PX, font_size + 2.0)
	var km_width := font.get_string_size("km", HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
	var km_base := Vector2(plot.position.x - km_width - UNIT_GAP_PX, plot.end.y + font_size + 4.0)
	var unit_boxes: Array[Rect2] = [
		Rect2(metre_base.x, metre_base.y - ascent, metre_width, line),
		Rect2(km_base.x, km_base.y - ascent, km_width, line),
	]
	var metre_step := _metre_step(vertical, plot)
	var metre_format := "%." + str(tick_decimals(metre_step)) + "f"
	for value: float in ticks_at(vertical.x, vertical.y, metre_step):
		var y := _y_of(value, plot, vertical)
		var label := metre_format % value
		var width := font.get_string_size(label, HORIZONTAL_ALIGNMENT_LEFT, -1, font_size).x
		var baseline := Vector2(plot.position.x - width - UNIT_GAP_PX, y + font_size * 0.35)
		var box := Rect2(baseline.x, baseline.y - ascent, width, line)
		var clear := true
		for unit: Rect2 in unit_boxes:
			clear = clear and not unit.grow(LABEL_CLEAR_PX).intersects(box)
		if clear:
			_draw_text(font, baseline, label, font_size, text_colour)
	_draw_text(font, metre_base, "m", font_size, text_colour)
	_draw_text(font, km_base, "km", font_size, text_colour)

