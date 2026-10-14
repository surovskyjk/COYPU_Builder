class_name DockPanel
extends PanelContainer
## One panel of the shell (T-140): a title bar and a body. The same instance lives in a dock slot (docked)
## or in the floating layer (floating); [UiShell] moves it with [method Node.reparent], so its children, scroll
## position, selected row and typed values are never rebuilt. Later tasks fill [method body] (layers T-142,
## inspector T-143, timeline T-141) and need no knowledge of where the panel currently sits.
##
## Floating panels draw the theme's panel box with the preset's corner radius and a translucent
## background ([method apply_style]); docked panels use the theme's box as it is.

var _id: StringName = &""
var _title_label: Label
var _header: PanelContainer
var _scroll: ScrollContainer
var _body: VBoxContainer
var _placeholder: Label
var _floating := false
var _opacity := 1.0
var _radius_px := 0
var _margin: MarginContainer
var _placeholder_text := ""
var _pill := false


## `id` names the slot (&"layers", &"inspector", &"timeline"); `placeholder` is the one line the body shows
## until the task that owns the panel puts real content in it.
static func create(panel_id: StringName, panel_title: String, placeholder: String) -> DockPanel:
	var panel := DockPanel.new()
	panel._id = panel_id
	panel.name = String(panel_id).capitalize().replace(" ", "") + "Panel"
	panel.theme_type_variation = &"DockPanel"
	panel.mouse_filter = Control.MOUSE_FILTER_STOP
	panel.clip_contents = true
	panel._build(panel_title, placeholder)
	return panel


func panel_id() -> StringName:
	return _id


func title() -> String:
	return _title_label.text


## The vertical container the owning task adds its content to.
func body() -> VBoxContainer:
	return _body


## The scroll container around the body (its position is part of the state that survives a style switch).
func scroll() -> ScrollContainer:
	return _scroll


func placeholder_label() -> Label:
	return _placeholder


func is_floating() -> bool:
	return _floating


func opacity() -> float:
	return _opacity


## Floating panels get a rounded, translucent card; docked ones the plain theme box. `radius_px` is the preset's
## corner radius. The text and icons stay fully opaque: only the card's own fill is translucent.
func apply_style(floating: bool, opacity: float, radius_px: int) -> void:
	_floating = floating
	_opacity = UiPreset.clamp_opacity(opacity)
	_radius_px = radius_px
	fit_panel_style(self, floating, _opacity, radius_px)
	_fit_header()


## How much of the theme's drop shadow a floating card keeps at `opacity`: none at the lowest opacity, all of it
## when fully opaque. The shadow is drawn under the whole card, so at full strength it would darken the scene
## seen through a translucent card and make the opacity slider look as if it did nothing.
static func shadow_scale(opacity: float) -> float:
	var t := (UiPreset.clamp_opacity(opacity) - UiPreset.OPACITY_MIN) / (UiPreset.OPACITY_MAX - UiPreset.OPACITY_MIN)
	return t * t


## Pill form (the floating timeline): no header, the title leads the single line of the body, tight margins.
## State is untouched; only visibility and margins change.
func set_pill(pill: bool) -> void:
	_pill = pill
	_header.visible = not pill
	var side := 24 if pill else 12
	var vertical := 17 if pill else 12
	_margin.add_theme_constant_override("margin_left", side)
	_margin.add_theme_constant_override("margin_right", side)
	_margin.add_theme_constant_override("margin_top", vertical)
	_margin.add_theme_constant_override("margin_bottom", 0 if pill else 12)
	_placeholder.autowrap_mode = TextServer.AUTOWRAP_OFF if pill else TextServer.AUTOWRAP_WORD_SMART
	_placeholder.text = "%s   %s" % [_title_label.text, _placeholder_text] if pill else _placeholder_text
	_placeholder.clip_text = pill


func is_pill() -> bool:
	return _pill


## Gives `control` its theme's "panel" box, adapted to the style: floating adds the translucent fill and the
## rounded corners, docked keeps the theme's box. Always starts from the theme item (the control's own override
## from an earlier call is removed first), so it can be called again on a style or opacity change. Works for any
## [PanelContainer] the shell owns (the dock panels, the ribbon, the status bar).
static func fit_panel_style(control: Control, floating: bool, opacity: float, radius_px: int) -> void:
	control.remove_theme_stylebox_override(&"panel")
	var base := control.get_theme_stylebox(&"panel") as StyleBoxFlat
	if base == null:
		return
	var box := base.duplicate() as StyleBoxFlat
	if floating:
		var fill := box.bg_color
		fill.a *= UiPreset.clamp_opacity(opacity)
		box.bg_color = fill
		box.set_corner_radius_all(maxi(radius_px, 0))
		box.shadow_color.a *= shadow_scale(opacity)
	control.add_theme_stylebox_override(&"panel", box)


## The header's fill is square by default; floating, it takes the card's top corners so it does not poke out.
func _fit_header() -> void:
	_header.remove_theme_stylebox_override(&"panel")
	if not _floating:
		return
	var base := _header.get_theme_stylebox(&"panel") as StyleBoxFlat
	if base == null:
		return
	var box := base.duplicate() as StyleBoxFlat
	box.corner_radius_top_left = maxi(_radius_px - 1, 0)
	box.corner_radius_top_right = maxi(_radius_px - 1, 0)
	_header.add_theme_stylebox_override(&"panel", box)


func _build(panel_title: String, placeholder: String) -> void:
	var column := VBoxContainer.new()
	column.name = "Column"
	column.add_theme_constant_override(&"separation", 0)
	add_child(column)

	_header = PanelContainer.new()
	_header.name = "Header"
	_header.theme_type_variation = &"DockHeader"
	_header.mouse_filter = Control.MOUSE_FILTER_PASS
	column.add_child(_header)

	_title_label = Label.new()
	_title_label.name = "Title"
	_title_label.text = panel_title
	_title_label.theme_type_variation = &"PanelTitle"
	_header.add_child(_title_label)

	_scroll = ScrollContainer.new()
	_scroll.name = "Scroll"
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	column.add_child(_scroll)

	_margin = MarginContainer.new()
	_margin.name = "Margin"
	_margin.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for side in ["left", "top", "right", "bottom"]:
		_margin.add_theme_constant_override("margin_" + side, 12)
	_scroll.add_child(_margin)

	_body = VBoxContainer.new()
	_body.name = "Body"
	_body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_margin.add_child(_body)

	_placeholder_text = placeholder
	_placeholder = Label.new()
	_placeholder.name = "Placeholder"
	_placeholder.text = placeholder
	_placeholder.theme_type_variation = &"MutedLabel"
	_placeholder.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_placeholder.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_body.add_child(_placeholder)
