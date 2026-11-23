class_name UiShell
extends Control
## The application frame (T-140): the ribbon, three dock slots (layers left, inspector right, timeline at the
## bottom), a status bar, a floating layer and the 3D view area. It owns each [DockPanel] and the [Ribbon]
## exactly once and moves them between the dock slots and the floating layer when the interface style changes:
## [method Node.reparent] keeps the same instances alive, so a scroll position, a selected row or a typed
## value survives the switch and nothing is rebuilt (no scene reload).
##
## The 3D world lives in [method world_root], a [SubViewport] inside a [SubViewportContainer]. In the
## Engineering style the container is the area left between the docks; in the Sandbox style it fills the window
## and the panels float over it. Placement is one function, [method _relayout], which sets every rectangle.
##
## Panel *contents* belong to later tasks (layers T-142, inspector T-143, timeline T-141); here each slot holds
## a placeholder that says what the panel will show. The one feature this task ships is "Export corridor
## envelope...". Preferences live in [UserSettings] (`user://settings.cfg`); [method apply_preset] on its own
## changes nothing on disk, so the capture driver can pose the shell without touching the user's choices.

signal preset_changed(id: StringName)

const PANEL_LAYERS := &"layers"
const PANEL_INSPECTOR := &"inspector"
const PANEL_TIMELINE := &"timeline"

const DOCK_LEFT_W := 280.0
const DOCK_RIGHT_W := 320.0
const DOCK_BOTTOM_H := 150.0
const FLOAT_CARD_W := 300.0
const FLOAT_MARGIN := 12.0
const FLOAT_PILL_H := 56.0
const PILL_RADIUS_PX := 28

const _STATE_OK_COLOR := Color("16A34A")
const _STATE_BAD_COLOR := Color("DC2626")
const _STATE_PENDING_COLOR := Color("D97706")

const DATA_POPOVER_SIZE := Vector2i(480, 300)
const POPOVER_PADDING_PX := 12.0
## The width the popover's text wraps at: the popover less its padding and the scroll bar.
const POPOVER_WRAP_PX := 440
const ATTRIBUTION_JOIN := " · "
## The muted line an entry with no attribution text shows in the popover.
const NO_ATTRIBUTION_TEXT := "No attribution recorded for this file."
const POPOVER_INTRO := "Where the terrain and imagery on screen come from."
const POPOVER_EMPTY := "No terrain or imagery is shown, so there are no sources to list."
const SOURCES_TOOLTIP := "Click to see where the terrain and imagery come from."
## The heading of each entry of the popover, by the kind of source [TerrainField] reports.
const KIND_TITLES := {"terrain": "Terrain", "imagery": "Imagery"}
## The attribution button is as wide as its text plus this padding, within [constant ATTRIBUTION_MIN_PX] and
## [constant ATTRIBUTION_MAX_SHARE] of the status bar, so the status message keeps the rest of the bar.
const ATTRIBUTION_PAD_PX := 8.0
const ATTRIBUTION_MIN_PX := 120.0
const ATTRIBUTION_MAX_SHARE := 0.4
## A press on the attribution button within this many milliseconds of the popover hiding is the press that dismissed
## it (the popover's light-dismiss hides it on the mouse press), so it does not reopen the popover.
const REOPEN_GUARD_MSEC := 200

@onready var _view_container: SubViewportContainer = %ViewContainer
@onready var _sub_viewport: SubViewport = %SubViewport
@onready var _chrome: Control = %Chrome
@onready var _ribbon_slot: Control = %RibbonSlot
@onready var _left_slot: Control = %LeftSlot
@onready var _right_slot: Control = %RightSlot
@onready var _bottom_slot: Control = %BottomSlot
@onready var _status_bar: PanelContainer = %StatusBar
@onready var _floating_layer: Control = %FloatingLayer
@onready var _segments: HBoxContainer = $Chrome/StatusBar/Segments

var _preset: UiPreset
var _opacity := 1.0
var _settings := UserSettings.new()
var _chrome_visible := true
var _relayout_queued := false
var _overlay: Control

var _ribbon: Ribbon
var _panels := {}                  # panel id -> DockPanel
var _camera_manager: CameraManager

var _status_backend: Label
var _status_project: Label
var _status_crs: Label
var _status_alignments: Label
var _status_message: Label
var _status_sources_separator: VSeparator
var _status_sources: Button
var _data_popover: PopupPanel
var _popover_entries: VBoxContainer
var _popover_close: Button
var _popover_hidden_msec := -REOPEN_GUARD_MSEC  # Time.get_ticks_msec() when the popover last hid

var _export_dialog: EnvelopeExportDialog
var _settings_dialog: SettingsDialog


func _ready() -> void:
	_build_status_bar()
	_build_data_popover()
	_ribbon = Ribbon.new()
	_ribbon.name = "Ribbon"
	_ribbon.command_invoked.connect(_on_command)
	_ribbon.minimum_size_changed.connect(_queue_relayout)
	_ribbon_slot.add_child(_ribbon)

	_panels[PANEL_LAYERS] = DockPanel.create(
		PANEL_LAYERS, "Layers",
		"Track, terrain (ČÚZK DMR 5G) and imagery layers will list here, each with a visibility toggle and an opacity slider."
	)
	_panels[PANEL_INSPECTOR] = DockPanel.create(
		PANEL_INSPECTOR, "Inspector",
		"The properties of the selected object, such as an alignment, a station or a vehicle, will show here for review and editing."
	)
	_panels[PANEL_TIMELINE] = DockPanel.create(
		PANEL_TIMELINE, "Timeline",
		"Play, pause and scrub the run here, with stops and speed markers along the time axis."
	)
	_left_slot.add_child(_panels[PANEL_LAYERS])
	_right_slot.add_child(_panels[PANEL_INSPECTOR])
	_bottom_slot.add_child(_panels[PANEL_TIMELINE])

	_export_dialog = EnvelopeExportDialog.new()
	_export_dialog.exported.connect(_on_exported)
	add_child(_export_dialog)
	_settings_dialog = SettingsDialog.new()
	_settings_dialog.preset_selected.connect(_on_settings_preset_selected)
	_settings_dialog.opacity_changed.connect(_on_settings_opacity_changed)
	_settings_dialog.visibility_changed.connect(_on_settings_visibility_changed)
	_settings_dialog.opacity_slider().drag_ended.connect(func(_changed: bool) -> void: _settings.save())
	add_child(_settings_dialog)

	Backend.state_changed.connect(func(_state: Backend.State) -> void: _refresh_status())
	EventBus.project_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_refresh_status)
	EventBus.camera_changed.connect(_on_camera_changed)
	EventBus.data_sources_changed.connect(_on_data_sources_changed)
	EventBus.status_message.connect(show_message)
	resized.connect(_queue_relayout)

	apply_preset(_settings.preset_id(), _settings.panel_opacity())
	set_source_boundaries(_settings.terrain_source_boundaries())
	_on_data_sources_changed()
	_refresh_status()


# --- accessors --------------------------------------------------------------------------------------------

## The node 3D content belongs under: the [SubViewport] that renders into the view area.
func world_root() -> Node:
	return _sub_viewport


func view_container() -> SubViewportContainer:
	return _view_container


## Rectangle of the 3D view in window coordinates (the whole window when it is full-bleed or the chrome is hidden).
func view_rect() -> Rect2:
	return _view_container.get_global_rect()


## The bar the T-103 overlay's information moved into; [method Main.status_overlay] returns it.
func status_bar() -> Control:
	return _status_bar


func ribbon() -> Ribbon:
	return _ribbon


## The one [DockPanel] of `id` (&"layers", &"inspector", &"timeline"), docked or floating alike.
func panel(id: StringName) -> DockPanel:
	return _panels.get(id)


func preset() -> UiPreset:
	return _preset


func panel_opacity() -> float:
	return _opacity


func settings() -> UserSettings:
	return _settings


func export_dialog() -> EnvelopeExportDialog:
	return _export_dialog


func settings_dialog() -> SettingsDialog:
	return _settings_dialog


func is_chrome_visible() -> bool:
	return _chrome_visible


## Uses `settings` for the preference reads and writes, and applies what they say.
func use_settings(settings: UserSettings) -> void:
	_settings = settings
	apply_preset(_settings.preset_id(), _settings.panel_opacity())
	set_source_boundaries(_settings.terrain_source_boundaries())


## Shows or hides the terrain's source boundaries (T-135 Part B) in the terrain scene and on the ribbon. Writes no
## settings: [method _on_command] stores the user's choice, the capture driver poses a shot with this.
func set_source_boundaries(on: bool) -> void:
	Session.set_source_boundaries(on)
	if _ribbon != null:
		_ribbon.set_checked(Ribbon.CMD_SOURCE_BOUNDARIES, on)


func source_boundaries() -> bool:
	return Session.source_boundaries()


## Lets the camera buttons drive `manager` and show its mode.
func bind_camera_manager(manager: CameraManager) -> void:
	_camera_manager = manager
	_ribbon.set_camera_mode(manager.mode())


# --- presets ----------------------------------------------------------------------------------------------

## Switches the interface style: sets the theme, moves the panels and the ribbon between the dock slots and the
## floating layer, restyles them and lays everything out again. Nothing is rebuilt. Writes no settings.
## `opacity` below 0 keeps the current one.
func apply_preset(id: StringName, opacity: float = -1.0) -> void:
	var next := UiPreset.builtin(id)
	if opacity >= 0.0:
		_opacity = UiPreset.clamp_opacity(opacity)
	elif _preset == null:
		_opacity = next.panel_opacity
	_preset = next
	theme = _preset.theme
	# The attribution button copies the status label's theme tokens, so it is restyled with each theme.
	if _status_sources != null:
		_apply_status_label_metrics(_status_sources, _status_message)

	var floating := _preset.panel_mode == UiPreset.PanelMode.FLOATING
	_place(_ribbon, _ribbon_slot, floating)
	_place(_panels[PANEL_LAYERS], _left_slot, floating)
	_place(_panels[PANEL_INSPECTOR], _right_slot, floating)
	_place(_panels[PANEL_TIMELINE], _bottom_slot, floating)
	_ribbon_slot.visible = not floating
	_left_slot.visible = not floating
	_right_slot.visible = not floating
	_bottom_slot.visible = not floating

	_ribbon.set_compact(floating)
	_ribbon.set_icon_size(_preset.icon_size_px)
	(_panels[PANEL_TIMELINE] as DockPanel).set_pill(floating)
	for panel_id: StringName in _panels:
		var dock_panel: DockPanel = _panels[panel_id]
		var radius := PILL_RADIUS_PX if panel_id == PANEL_TIMELINE else _preset.corner_radius_px
		dock_panel.apply_style(floating, _opacity, radius)
	DockPanel.fit_panel_style(_ribbon, floating, _opacity, _preset.corner_radius_px)
	DockPanel.fit_panel_style(_status_bar, floating, _opacity, 0)
	_view_container.mouse_filter = Control.MOUSE_FILTER_STOP

	_relayout()
	preset_changed.emit(_preset.id)


## Changes only the floating-panel opacity (live, from the settings slider). Writes no settings.
func set_panel_opacity(value: float) -> void:
	_opacity = UiPreset.clamp_opacity(value)
	var floating := _preset.panel_mode == UiPreset.PanelMode.FLOATING
	for panel_id: StringName in _panels:
		var dock_panel: DockPanel = _panels[panel_id]
		dock_panel.apply_style(floating, _opacity, PILL_RADIUS_PX if panel_id == PANEL_TIMELINE else _preset.corner_radius_px)
	DockPanel.fit_panel_style(_ribbon, floating, _opacity, _preset.corner_radius_px)
	DockPanel.fit_panel_style(_status_bar, floating, _opacity, 0)


## Shows or hides every piece of chrome. Hidden, the 3D view fills the window: what the capture driver uses
## for "ui": "hidden" shots, and what a future full-screen mode would use.
func set_chrome_visible(chrome_visible: bool) -> void:
	_chrome_visible = chrome_visible
	_chrome.visible = chrome_visible
	_floating_layer.visible = chrome_visible
	_relayout()


func status_message_text() -> String:
	return _status_message.text


## The status bar's message label, which takes the room the attribution button leaves.
func status_message_label() -> Label:
	return _status_message


func show_message(text: String) -> void:
	_status_message.text = text
	_status_message.tooltip_text = text


# --- dialogs ----------------------------------------------------------------------------------------------

func open_envelope_export() -> void:
	_export_dialog.open(Session.alignments())


func open_settings() -> void:
	var shown_opacity := UiPreset.builtin(UiPreset.SANDBOX).panel_opacity
	if _preset.panel_mode == UiPreset.PanelMode.FLOATING:
		shown_opacity = _opacity
	elif _settings.has_panel_opacity():
		shown_opacity = _settings.panel_opacity()
	_settings_dialog.show_values(_preset.id, shown_opacity)
	_settings_dialog.popup_centered()


## Dialogs by the name the capture scenarios use. `offset` moves the opened window (capture use: to put it over
## the ribbon or a panel).
func show_dialog(dialog_name: StringName, offset: Vector2i = Vector2i.ZERO) -> bool:
	var window: Window
	match dialog_name:
		&"envelope_export":
			open_envelope_export()
			window = _export_dialog
		&"settings":
			open_settings()
			window = _settings_dialog
		&"data_sources":
			show_data_sources(offset)
			return true
		_:
			return false
	window.position += offset
	return true


func close_dialogs() -> void:
	_export_dialog.hide()
	_settings_dialog.hide()
	close_data_sources()


## The Data sources popover (T-135 Part B): one entry per source in view with its full attribution and notices.
func data_sources_popover() -> PopupPanel:
	return _data_popover


## Opens the popover above the right end of the status bar, moved by `offset` (capture use, as for the dialogs).
func show_data_sources(offset: Vector2i = Vector2i.ZERO) -> void:
	var anchor := _status_bar.get_global_rect()
	var box := DATA_POPOVER_SIZE
	var at := Vector2i(int(anchor.end.x) - box.x - int(FLOAT_MARGIN), int(anchor.position.y) - box.y - 8) + offset
	# Deferred: a popover closed in the same frame (see close_dialogs) would otherwise be hidden again.
	_open_popover.call_deferred(Rect2i(at, box))


func _open_popover(rect: Rect2i) -> void:
	_data_popover.popup(rect)
	_popover_close.grab_focus()


func close_data_sources() -> void:
	if _data_popover != null:
		_data_popover.hide()


## The attribution the status bar shows, or "" when no source is in view.
func attribution_text() -> String:
	return _status_sources.text if _status_sources.visible else ""


## The status bar's attribution button, which opens the popover (a flat button, reachable with Tab).
func attribution_button() -> Button:
	return _status_sources


## What the status bar shows for `entries` (dictionaries of `source_id`, `attribution` and `notices`, as
## `Session.data_sources()` holds them): each distinct real attribution text once, joined with " · ". When sources
## are in view but none has a text, "N data sources" (or "1 data source"). Empty when there are no entries.
static func attribution_summary(entries: Array) -> String:
	if entries.is_empty():
		return ""
	var lines := PackedStringArray()
	for entry: Variant in entries:
		var text := String((entry as Dictionary).get("attribution", "")).strip_edges()
		if not text.is_empty() and not lines.has(text):
			lines.append(text)
	if lines.is_empty():
		return "%d data source%s" % [entries.size(), "" if entries.size() == 1 else "s"]
	return ATTRIBUTION_JOIN.join(lines)


## The popover's heading for one entry: "Kind · name", or the kind alone when the source has no name.
static func source_heading(entry: Dictionary) -> String:
	var kind: String = KIND_TITLES.get(String(entry.get("kind", "")), "Data")
	var name := String(entry.get("name", "")).strip_edges()
	return kind if name.is_empty() else "%s · %s" % [kind, name]


## Window rectangles (in window coordinates) of everything that is chrome: the visible ribbon, panels, status
## bar and open dialogs, the latter including their title bar. The capture driver masks them out of the frame
## statistics so that only the 3D render decides whether a shot is empty.
func chrome_rects() -> Array[Rect2]:
	var rects: Array[Rect2] = []
	if _chrome_visible:
		rects.append(_status_bar.get_global_rect())
		rects.append(_ribbon.get_global_rect())
		for panel_id: StringName in _panels:
			rects.append((_panels[panel_id] as Control).get_global_rect())
	if _overlay != null:
		rects.append(_overlay.get_global_rect())
	for window: Window in [_export_dialog, _settings_dialog]:
		if window.visible:
			rects.append(Rect2(Vector2(window.position) - Vector2(1.0, 36.0), Vector2(window.size) + Vector2(2.0, 37.0)))
	if _data_popover != null and _data_popover.visible:
		rects.append(Rect2(Vector2(_data_popover.position), Vector2(_data_popover.size)))
	return rects


## Capture-only: shows `text` in a themed box at the centre of the 3D view (the scenarios' `overlay_text`), to
## prove that glyphs render in each style's default font. An empty text removes it.
func show_capture_text(text: String) -> void:
	if _overlay != null:
		_overlay.queue_free()
		_overlay = null
	if text.is_empty():
		return
	var box := PanelContainer.new()
	box.mouse_filter = Control.MOUSE_FILTER_IGNORE
	var label := Label.new()
	label.text = text
	label.add_theme_font_size_override(&"font_size", 28)
	box.add_child(label)
	add_child(box)
	_overlay = box
	await get_tree().process_frame
	box.position = view_rect().get_center() - box.size * 0.5


# --- internals --------------------------------------------------------------------------------------------

func _place(node: Control, slot: Control, floating: bool) -> void:
	var target: Control = _floating_layer if floating else slot
	if node.get_parent() != target:
		node.reparent(target, false)
	if not floating:
		node.set_anchors_and_offsets_preset(Control.PRESET_FULL_RECT)


func _queue_relayout() -> void:
	if _relayout_queued:
		return
	_relayout_queued = true
	_relayout.call_deferred()


func _relayout() -> void:
	_relayout_queued = false
	if not is_node_ready() or _preset == null:
		return
	var width := size.x
	var height := size.y
	if not _chrome_visible:
		_set_rect(_view_container, Rect2(0.0, 0.0, width, height))
		return

	var status_h := _status_bar.get_combined_minimum_size().y
	_set_rect(_status_bar, Rect2(0.0, height - status_h, width, status_h))
	var ribbon_min := _ribbon.get_combined_minimum_size()

	var inner := Rect2()
	var ribbon_bottom := 0.0
	if _preset.panel_mode == UiPreset.PanelMode.DOCKED:
		var ribbon_h := ribbon_min.y
		var middle_h := height - ribbon_h - status_h
		var centre_w := width - DOCK_LEFT_W - DOCK_RIGHT_W
		_set_rect(_ribbon_slot, Rect2(0.0, 0.0, width, ribbon_h))
		_set_rect(_left_slot, Rect2(0.0, ribbon_h, DOCK_LEFT_W, middle_h))
		_set_rect(_right_slot, Rect2(width - DOCK_RIGHT_W, ribbon_h, DOCK_RIGHT_W, middle_h))
		_set_rect(_bottom_slot, Rect2(DOCK_LEFT_W, height - status_h - DOCK_BOTTOM_H, centre_w, DOCK_BOTTOM_H))
		inner = Rect2(DOCK_LEFT_W, ribbon_h, centre_w, middle_h - DOCK_BOTTOM_H)
		ribbon_bottom = ribbon_h
	else:
		var bar_w := minf(ribbon_min.x, width - 2.0 * FLOAT_MARGIN)
		_set_rect(_ribbon, Rect2((width - bar_w) * 0.5, FLOAT_MARGIN, bar_w, ribbon_min.y))
		var top := FLOAT_MARGIN * 2.0 + ribbon_min.y
		var bottom := height - status_h - FLOAT_MARGIN
		_set_rect(_panels[PANEL_LAYERS], Rect2(FLOAT_MARGIN, top, FLOAT_CARD_W, bottom - top))
		_set_rect(_panels[PANEL_INSPECTOR], Rect2(width - FLOAT_MARGIN - FLOAT_CARD_W, top, FLOAT_CARD_W, bottom - top))
		var pill_x := FLOAT_MARGIN * 2.0 + FLOAT_CARD_W
		_set_rect(_panels[PANEL_TIMELINE], Rect2(pill_x, bottom - FLOAT_PILL_H, width - 2.0 * pill_x, FLOAT_PILL_H))
		inner = Rect2(0.0, ribbon_bottom, width, height - ribbon_bottom - status_h)
	# Data-driven: the preset says whether the view fills the window; otherwise it takes what the docks leave.
	_set_rect(_view_container, Rect2(0.0, 0.0, width, height) if _preset.viewport_full_bleed else inner)


func _set_rect(control: Control, rect: Rect2) -> void:
	control.set_anchors_preset(Control.PRESET_TOP_LEFT)
	control.position = rect.position
	control.size = rect.size


func _on_command(id: StringName) -> void:
	match id:
		Ribbon.CMD_EXPORT_ENVELOPE:
			open_envelope_export()
		Ribbon.CMD_SETTINGS:
			open_settings()
		Ribbon.CMD_SOURCE_BOUNDARIES:
			var on := not _settings.terrain_source_boundaries()
			_settings.set_terrain_source_boundaries(on)
			_settings.save()
			set_source_boundaries(on)
		_:
			if Ribbon.CAMERA_COMMANDS.has(id) and _camera_manager != null:
				_camera_manager.set_mode(Ribbon.CAMERA_COMMANDS[id])


## Keeps the attribution button and the popover in step with the sources in view.
func _on_data_sources_changed() -> void:
	if _status_sources == null:
		return
	var entries := Session.data_sources()
	var summary := attribution_summary(entries)
	var shown := not entries.is_empty()
	_status_sources_separator.visible = shown
	_status_sources.visible = shown
	_status_sources.text = summary
	_status_sources.tooltip_text = SOURCES_TOOLTIP
	_fit_attribution_button()
	_refresh_data_popover()


## The attribution button opens the popover, or closes it when it is open. A flat button, so Tab reaches it and
## Enter presses it. A press just after the popover hid (its light-dismiss) does not reopen it.
func _on_sources_pressed() -> void:
	if _data_popover.visible:
		close_data_sources()
		return
	if Time.get_ticks_msec() - _popover_hidden_msec < REOPEN_GUARD_MSEC:
		return
	show_data_sources()


func _on_popover_hidden() -> void:
	_popover_hidden_msec = Time.get_ticks_msec()


func _build_data_popover() -> void:
	_data_popover = PopupPanel.new()
	_data_popover.name = "DataSources"
	_data_popover.popup_hide.connect(_on_popover_hidden)
	add_child(_data_popover)

	# A fixed width before the first layout: autowrapping labels in a zero-width column report a huge height, and the
	# popover would open far too tall.
	var column := VBoxContainer.new()
	column.add_theme_constant_override(&"separation", 10)
	column.custom_minimum_size = Vector2(DATA_POPOVER_SIZE.x - 2 * int(POPOVER_PADDING_PX), 0)
	_data_popover.add_child(column)

	var title := Label.new()
	title.text = "Data sources"
	column.add_child(title)

	var hint := Label.new()
	hint.theme_type_variation = &"HintLabel"
	hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	hint.custom_minimum_size = Vector2(POPOVER_WRAP_PX, 0)
	hint.text = POPOVER_INTRO
	column.add_child(hint)

	var scroll := ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.custom_minimum_size = Vector2(DATA_POPOVER_SIZE.x - 2 * POPOVER_PADDING_PX, 160)
	column.add_child(scroll)
	_popover_entries = VBoxContainer.new()
	_popover_entries.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_popover_entries.add_theme_constant_override(&"separation", 8)
	_popover_entries.custom_minimum_size = Vector2(POPOVER_WRAP_PX, 0)
	scroll.add_child(_popover_entries)

	_popover_close = Button.new()
	_popover_close.name = "Close"
	_popover_close.text = "Close"
	_popover_close.size_flags_horizontal = Control.SIZE_SHRINK_END
	_popover_close.pressed.connect(close_data_sources)
	column.add_child(_popover_close)


## A label that wraps at [constant POPOVER_WRAP_PX]: the width is fixed, so the popover's first layout is already right.
func _wrapped_label(text: String, variation: StringName) -> Label:
	var label := Label.new()
	if variation != &"":
		label.theme_type_variation = variation
	label.text = text
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.custom_minimum_size = Vector2(POPOVER_WRAP_PX, 0)
	return label


## Rebuilds the popover's entries from `Session.data_sources()`: the full attribution of each source, then its notices.
func _refresh_data_popover() -> void:
	if _popover_entries == null:
		return
	for child in _popover_entries.get_children():
		_popover_entries.remove_child(child)
		child.queue_free()
	var entries := Session.data_sources()
	if entries.is_empty():
		_popover_entries.add_child(_wrapped_label(POPOVER_EMPTY, &"HintLabel"))
		return
	for entry: Variant in entries:
		var source := entry as Dictionary
		if _popover_entries.get_child_count() > 0:
			_popover_entries.add_child(HSeparator.new())
		var heading := _wrapped_label(source_heading(source), &"HintLabel")
		_popover_entries.add_child(heading)
		var text := String(source.get("attribution", "")).strip_edges()
		if text.is_empty():
			_popover_entries.add_child(_wrapped_label(NO_ATTRIBUTION_TEXT, &"HintLabel"))
		else:
			_popover_entries.add_child(_wrapped_label(text, &""))
		var notices: PackedStringArray = source.get("notices", PackedStringArray())
		for notice in notices:
			_popover_entries.add_child(_wrapped_label(notice, &"HintLabel"))


func _on_camera_changed(mode: int) -> void:
	if _ribbon != null:
		_ribbon.set_camera_mode(mode)


func _on_exported(_result: Dictionary, summary: String) -> void:
	show_message(summary)


func _on_settings_preset_selected(id: StringName) -> void:
	_settings.set_preset_id(id)
	_settings.save()
	apply_preset(id, _settings.panel_opacity())


func _on_settings_opacity_changed(value: float) -> void:
	_settings.set_panel_opacity(value)
	set_panel_opacity(value)


func _on_settings_visibility_changed() -> void:
	if not _settings_dialog.visible:
		_settings.save()


func _build_status_bar() -> void:
	_status_backend = _status_label(false)
	_segments.add_child(_status_backend)
	_segments.add_child(VSeparator.new())
	_status_project = _status_label(false)
	_segments.add_child(_status_project)
	_segments.add_child(VSeparator.new())
	_status_crs = _status_label(false)
	_segments.add_child(_status_crs)
	_segments.add_child(VSeparator.new())
	_status_alignments = _status_label(false)
	_segments.add_child(_status_alignments)
	_status_message = _status_label(true)
	_status_message.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_segments.add_child(_status_message)

	# The attribution of the sources in view, on the right; a click opens the Data sources popover (T-135 B). The
	# message takes the room the attribution leaves, and the attribution is only as wide as its text (see
	# _fit_attribution_button), so a status or export message is not cut to a fraction of the bar.
	_status_sources_separator = VSeparator.new()
	_status_sources_separator.visible = false
	_segments.add_child(_status_sources_separator)
	_status_sources = Button.new()
	_status_sources.flat = true
	_status_sources.focus_mode = Control.FOCUS_ALL
	_status_sources.alignment = HORIZONTAL_ALIGNMENT_RIGHT
	_status_sources.clip_text = true
	_status_sources.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	_status_sources.size_flags_horizontal = Control.SIZE_SHRINK_END
	_status_sources.custom_minimum_size = Vector2(0, 0)
	_status_sources.visible = false
	_status_sources.pressed.connect(_on_sources_pressed)
	_segments.add_child(_status_sources)
	_status_bar.resized.connect(_fit_attribution_button)


func _status_label(expand: bool) -> Label:
	var label := Label.new()
	label.theme_type_variation = &"StatusLabel"
	label.mouse_filter = Control.MOUSE_FILTER_PASS
	if expand:
		label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
		label.clip_text = true
	return label


## Gives `button` the metrics of the status `label`: its font, font size and colour from the label's theme tokens, no
## content margins and empty flat style boxes for the normal, hover, pressed and disabled states, so the button is no
## taller than the label and the status bar keeps its height when the button appears. The focus style is the theme's own,
## so keyboard focus shows; a focus style does not change the minimum size. On hover the text takes the accent the ribbon's
## active buttons use (`RibbonButton`'s pressed font colour), since the theme gives no separate hover colour to them.
static func _apply_status_label_metrics(button: Button, label: Label) -> void:
	button.add_theme_font_override(&"font", label.get_theme_font(&"font"))
	button.add_theme_font_size_override(&"font_size", label.get_theme_font_size(&"font_size"))
	var colour := label.get_theme_color(&"font_color")
	var accent := label.get_theme_color(&"font_pressed_color", &"RibbonButton")
	button.add_theme_color_override(&"font_color", colour)
	button.add_theme_color_override(&"font_pressed_color", colour)
	button.add_theme_color_override(&"font_focus_color", colour)
	button.add_theme_color_override(&"font_disabled_color", colour)
	button.add_theme_color_override(&"font_hover_color", accent)
	button.add_theme_color_override(&"font_hover_pressed_color", accent)
	for style_name: StringName in [&"normal", &"hover", &"pressed", &"hover_pressed", &"disabled"]:
		button.add_theme_stylebox_override(style_name, _flat_empty_style())


static func _flat_empty_style() -> StyleBoxEmpty:
	var style := StyleBoxEmpty.new()
	style.content_margin_left = 0.0
	style.content_margin_top = 0.0
	style.content_margin_right = 0.0
	style.content_margin_bottom = 0.0
	return style


## Sizes the attribution button to its text plus [constant ATTRIBUTION_PAD_PX], between [constant ATTRIBUTION_MIN_PX]
## and [constant ATTRIBUTION_MAX_SHARE] of the status bar's width. Called when the text changes and when the bar is
## resized.
func _fit_attribution_button() -> void:
	if _status_sources == null:
		return
	var font := _status_sources.get_theme_font(&"font")
	var font_px := _status_sources.get_theme_font_size(&"font_size")
	var text_px := font.get_string_size(_status_sources.text, HORIZONTAL_ALIGNMENT_LEFT, -1, font_px).x
	var widest := maxf(ATTRIBUTION_MIN_PX, _status_bar.size.x * ATTRIBUTION_MAX_SHARE)
	_status_sources.custom_minimum_size.x = clampf(text_px + ATTRIBUTION_PAD_PX, ATTRIBUTION_MIN_PX, widest)


func _state_color(state: Backend.State) -> Color:
	match state:
		Backend.State.READY:
			return _STATE_OK_COLOR
		Backend.State.FAILED, Backend.State.DISCONNECTED:
			return _STATE_BAD_COLOR
	return _STATE_PENDING_COLOR


## The information the T-103 overlay showed, in one line of segments.
func _refresh_status() -> void:
	if _status_backend == null:
		return
	var state := Backend.state()
	var state_name: String = Backend.State.keys()[state].to_lower()
	_status_backend.text = "Backend: %s" % state_name
	_status_backend.add_theme_color_override(&"font_color", _state_color(state))
	_status_backend.tooltip_text = "Server %s, protocol %d" % [Backend.server_version(), Backend.protocol_version()]

	var project_id := Session.project_id()
	_status_project.text = "Project: %s" % (project_id if not project_id.is_empty() else "none")
	var crs := Session.crs()
	_status_crs.text = "CRS: %s" % (crs if not crs.is_empty() else "none")

	var alignments := Session.alignments()
	if alignments.is_empty():
		_status_alignments.text = "Alignments: none"
		_status_alignments.tooltip_text = ""
		return
	var total_m := 0.0
	var names := PackedStringArray()
	for alignment: Dictionary in alignments:
		total_m += float(alignment.get("length", 0.0))
		names.append("%s [%s] %.1f m" % [alignment.get("name", "?"), alignment.get("mode", "?"), alignment.get("length", 0.0)])
	if alignments.size() == 1:
		var only: Dictionary = alignments[0]
		_status_alignments.text = "Alignment: %s, %.2f km" % [only.get("name", "?"), total_m / 1000.0]
	else:
		_status_alignments.text = "Alignments: %d, %.2f km" % [alignments.size(), total_m / 1000.0]
	_status_alignments.tooltip_text = "\n".join(names)
