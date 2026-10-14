class_name SettingsDialog
extends AcceptDialog
## "Settings..." (T-140): the interface style and the translucency of floating panels. Changes apply as they
## are made (the shell listens to the two signals) and are stored by the shell through [UserSettings]; the
## window only has a Close button.

signal preset_selected(id: StringName)
signal opacity_changed(value: float)

var _style: OptionButton
var _style_note: Label
var _opacity: HSlider
var _opacity_value: Label
var _opacity_note: Label


func _init() -> void:
	title = "Settings"
	ok_button_text = "Close"
	min_size = Vector2i(520, 0)
	exclusive = true
	_build()


## Shows the current choices without emitting anything.
func show_values(preset_id: StringName, opacity: float) -> void:
	for index in _style.item_count:
		if _style.get_item_metadata(index) == preset_id:
			_style.select(index)
	_opacity.set_value_no_signal(UiPreset.clamp_opacity(opacity))
	_refresh_labels()


func style_choice() -> OptionButton:
	return _style


func opacity_slider() -> HSlider:
	return _opacity


func _build() -> void:
	var column := VBoxContainer.new()
	column.add_theme_constant_override(&"separation", 12)
	add_child(column)

	var grid := GridContainer.new()
	grid.columns = 2
	grid.add_theme_constant_override(&"h_separation", 16)
	grid.add_theme_constant_override(&"v_separation", 10)
	column.add_child(grid)

	var style_label := Label.new()
	style_label.text = "Interface style"
	grid.add_child(style_label)
	_style = OptionButton.new()
	_style.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for preset_id in UiPreset.ids():
		_style.add_item(UiPreset.builtin(preset_id).display_name)
		_style.set_item_metadata(_style.item_count - 1, preset_id)
	_style.item_selected.connect(_on_style_selected)
	grid.add_child(_style)

	var spacer := Control.new()
	grid.add_child(spacer)
	_style_note = Label.new()
	_style_note.theme_type_variation = &"HintLabel"
	_style_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_style_note.custom_minimum_size = Vector2(300, 0)
	_style_note.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	grid.add_child(_style_note)

	var opacity_label := Label.new()
	opacity_label.text = "Panel translucency"
	grid.add_child(opacity_label)
	var slider_row := HBoxContainer.new()
	slider_row.add_theme_constant_override(&"separation", 10)
	_opacity = HSlider.new()
	_opacity.min_value = UiPreset.OPACITY_MIN
	_opacity.max_value = UiPreset.OPACITY_MAX
	_opacity.step = 0.01
	_opacity.value = UiPreset.OPACITY_MAX
	_opacity.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_opacity.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	_opacity.custom_minimum_size = Vector2(200, 24)
	_opacity.value_changed.connect(_on_opacity_changed)
	slider_row.add_child(_opacity)
	_opacity_value = Label.new()
	_opacity_value.custom_minimum_size = Vector2(48, 0)
	_opacity_value.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	slider_row.add_child(_opacity_value)
	grid.add_child(slider_row)

	var spacer_two := Control.new()
	grid.add_child(spacer_two)
	_opacity_note = Label.new()
	_opacity_note.theme_type_variation = &"HintLabel"
	_opacity_note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_opacity_note.custom_minimum_size = Vector2(300, 0)
	_opacity_note.text = "How much of the 3D view shows through the floating panels of the Sandbox style (40 % to 100 % opaque)."
	grid.add_child(_opacity_note)


func _on_style_selected(index: int) -> void:
	_refresh_labels()
	preset_selected.emit(_style.get_item_metadata(index))


func _on_opacity_changed(value: float) -> void:
	_refresh_labels()
	opacity_changed.emit(value)


func _refresh_labels() -> void:
	_opacity_value.text = "%d %%" % roundi(_opacity.value * 100.0)
	var chosen: StringName = _style.get_item_metadata(maxi(_style.selected, 0))
	if chosen == UiPreset.SANDBOX:
		_style_note.text = "The 3D view fills the window and the panels float over it as translucent cards."
	else:
		_style_note.text = "A light theme with a ribbon and docked panels around the 3D view."
