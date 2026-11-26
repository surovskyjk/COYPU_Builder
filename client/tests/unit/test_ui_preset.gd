extends GdUnitTestSuite
## UiPreset values and the UiShell style switch (T-140): the two built-in presets, and that switching between
## them moves the *same* DockPanel/Ribbon instances between the dock slots and the floating layer without
## rebuilding anything, so panel state survives. No backend is started; the shell is the real scene.

const _SHELL_SCENE := "res://ui/shell/ui_shell.tscn"


func _make_shell() -> UiShell:
	var host := Control.new()
	host.set_anchors_preset(Control.PRESET_TOP_LEFT)
	host.size = Vector2(1600.0, 900.0)
	add_child(host)
	auto_free(host)
	var shell := (load(_SHELL_SCENE) as PackedScene).instantiate() as UiShell
	host.add_child(shell)
	await get_tree().process_frame
	await get_tree().process_frame
	return shell


func test_engineering_preset_values() -> void:
	var preset := UiPreset.builtin(UiPreset.ENGINEERING)
	assert_str(String(preset.id)).is_equal("engineering")
	assert_int(preset.panel_mode).is_equal(UiPreset.PanelMode.DOCKED)
	assert_bool(preset.viewport_full_bleed).is_false()
	assert_float(preset.panel_opacity).is_equal_approx(1.0, 1e-6)
	assert_int(preset.icon_size_px).is_equal(18)
	assert_int(preset.corner_radius_px).is_less(8)
	assert_object(preset.theme).is_not_null()


func test_sandbox_preset_values() -> void:
	var preset := UiPreset.builtin(UiPreset.SANDBOX)
	assert_str(String(preset.id)).is_equal("sandbox")
	assert_int(preset.panel_mode).is_equal(UiPreset.PanelMode.FLOATING)
	assert_bool(preset.viewport_full_bleed).is_true()
	assert_float(preset.panel_opacity).is_equal_approx(0.72, 1e-6)
	assert_int(preset.corner_radius_px).is_equal(12)
	assert_int(preset.icon_size_px).is_equal(20)
	assert_object(preset.theme).is_not_null()
	assert_bool(preset.theme == UiPreset.builtin(UiPreset.ENGINEERING).theme).is_false()


func test_unknown_preset_gives_the_default() -> void:
	assert_str(String(UiPreset.builtin(&"nonsense").id)).is_equal("engineering")


func test_opacity_is_clamped() -> void:
	assert_float(UiPreset.clamp_opacity(0.0)).is_equal_approx(0.4, 1e-6)
	assert_float(UiPreset.clamp_opacity(7.0)).is_equal_approx(1.0, 1e-6)
	assert_float(UiPreset.clamp_opacity(0.55)).is_equal_approx(0.55, 1e-6)


func test_cold_start_is_engineering_with_docked_panels() -> void:
	var shell := await _make_shell()
	assert_str(String(shell.preset().id)).is_equal("engineering")
	assert_object(shell.panel(UiShell.PANEL_LAYERS).get_parent()).is_same(shell.get_node("%LeftSlot"))
	assert_object(shell.panel(UiShell.PANEL_INSPECTOR).get_parent()).is_same(shell.get_node("%RightSlot"))
	assert_object(shell.panel(UiShell.PANEL_TIMELINE).get_parent()).is_same(shell.get_node("%BottomSlot"))
	assert_object(shell.ribbon().get_parent()).is_same(shell.get_node("%RibbonSlot"))
	var view := shell.view_rect()
	assert_float(view.position.x).is_equal_approx(UiShell.DOCK_LEFT_W, 0.5)
	assert_float(view.size.x).is_equal_approx(1600.0 - UiShell.DOCK_LEFT_W - UiShell.DOCK_RIGHT_W, 0.5)


func test_switching_styles_reparents_the_same_instances_and_keeps_state() -> void:
	var shell := await _make_shell()
	var layers := shell.panel(UiShell.PANEL_LAYERS)
	var inspector := shell.panel(UiShell.PANEL_INSPECTOR)
	var timeline := shell.panel(UiShell.PANEL_TIMELINE)
	var ribbon := shell.ribbon()
	var ids := [layers.get_instance_id(), inspector.get_instance_id(), timeline.get_instance_id(), ribbon.get_instance_id()]

	var rows := ItemList.new()
	rows.custom_minimum_size = Vector2(0.0, 1200.0)
	for i in 30:
		rows.add_item("Row %d" % i)
	layers.body().add_child(rows)
	var field := LineEdit.new()
	inspector.body().add_child(field)
	field.text = "12.5"
	await get_tree().process_frame
	await get_tree().process_frame
	layers.scroll().scroll_vertical = 120
	rows.select(7)

	shell.apply_preset(UiPreset.SANDBOX)
	await get_tree().process_frame
	await get_tree().process_frame

	var floating_layer := shell.get_node("%FloatingLayer")
	assert_object(layers.get_parent()).is_same(floating_layer)
	assert_object(inspector.get_parent()).is_same(floating_layer)
	assert_object(timeline.get_parent()).is_same(floating_layer)
	assert_object(ribbon.get_parent()).is_same(floating_layer)
	assert_bool(layers.is_floating()).is_true()
	assert_bool(ribbon.is_compact()).is_true()
	assert_int(layers.get_instance_id()).is_equal(ids[0])
	assert_int(inspector.get_instance_id()).is_equal(ids[1])
	assert_int(timeline.get_instance_id()).is_equal(ids[2])
	assert_int(ribbon.get_instance_id()).is_equal(ids[3])
	assert_int(layers.scroll().scroll_vertical).is_equal(120)
	assert_array(rows.get_selected_items()).contains_exactly([7])
	assert_str(field.text).is_equal("12.5")
	assert_float(shell.view_rect().size.x).is_equal_approx(1600.0, 0.5)
	assert_float(shell.view_rect().size.y).is_equal_approx(900.0, 0.5)

	shell.apply_preset(UiPreset.ENGINEERING)
	await get_tree().process_frame
	await get_tree().process_frame

	assert_object(layers.get_parent()).is_same(shell.get_node("%LeftSlot"))
	assert_object(inspector.get_parent()).is_same(shell.get_node("%RightSlot"))
	assert_object(timeline.get_parent()).is_same(shell.get_node("%BottomSlot"))
	assert_object(ribbon.get_parent()).is_same(shell.get_node("%RibbonSlot"))
	assert_bool(layers.is_floating()).is_false()
	assert_int(layers.get_instance_id()).is_equal(ids[0])
	assert_int(layers.scroll().scroll_vertical).is_equal(120)
	assert_array(rows.get_selected_items()).contains_exactly([7])
	assert_str(field.text).is_equal("12.5")


func test_floating_panel_opacity_follows_the_setting() -> void:
	var shell := await _make_shell()
	shell.apply_preset(UiPreset.SANDBOX, 0.72)
	var panel := shell.panel(UiShell.PANEL_LAYERS)
	var full := (panel.get_theme_stylebox(&"panel") as StyleBoxFlat).bg_color.a
	shell.set_panel_opacity(0.4)
	var faint := (panel.get_theme_stylebox(&"panel") as StyleBoxFlat).bg_color.a
	assert_float(full).is_equal_approx(0.72, 0.01)
	assert_float(faint).is_equal_approx(0.4, 0.01)
	shell.set_panel_opacity(0.0)
	assert_float(shell.panel_opacity()).is_equal_approx(0.4, 1e-6)


func test_ribbon_holds_only_commands_that_exist() -> void:
	var shell := await _make_shell()
	var ribbon := shell.ribbon()
	var ids := ribbon.command_ids()
	assert_array(ids).contains_exactly_in_any_order([
		Ribbon.CMD_IMPORT_SOURCES, Ribbon.CMD_EXPORT_ENVELOPE, Ribbon.CMD_CAMERA_ORBIT, Ribbon.CMD_CAMERA_WAYSIDE,
		Ribbon.CMD_CAMERA_CAB, Ribbon.CMD_SOURCE_BOUNDARIES, Ribbon.CMD_SETTINGS,
	])
	for id in ids:
		var command_button := ribbon.button(id)
		assert_bool(command_button.disabled).is_false()
		assert_str(command_button.tooltip_text).is_not_empty()
		assert_object(command_button.shortcut).is_not_null()
		assert_object(command_button.icon).is_not_null()
	assert_object(ribbon.find_child("ProjectGroup", true, false)).is_null()
	assert_object(ribbon.find_child("ImportGroup", true, false)).is_not_null()


## T-136 Part B: the Import command is in the Import group, and pressing it opens the import dialog.
func test_import_command_opens_the_dialog() -> void:
	var shell := await _make_shell()
	var ribbon := shell.ribbon()
	var button := ribbon.button(Ribbon.CMD_IMPORT_SOURCES)
	assert_object(button).is_not_null()
	assert_object(button.get_parent().get_parent()).is_same(ribbon.find_child("ImportGroup", true, false))
	assert_str(button.tooltip_text).contains("Import terrain or imagery…")
	assert_bool(shell.import_dialog().visible).is_false()

	button.pressed.emit()
	await get_tree().process_frame
	assert_bool(shell.import_dialog().visible).append_failure_message("the command did not open the dialog").is_true()
	assert_int(shell.import_dialog().step()).is_equal(ImportDialog.STEP_SOURCE)

	shell.close_dialogs()
	assert_bool(shell.import_dialog().visible).is_false()
	assert_bool(shell.show_dialog(&"import")).is_true()
	assert_bool(shell.import_dialog().visible).is_true()
	shell.close_dialogs()


func test_camera_buttons_follow_camera_changes() -> void:
	var shell := await _make_shell()
	var manager := CameraManager.new()
	shell.world_root().add_child(manager)
	shell.bind_camera_manager(manager)
	var ribbon := shell.ribbon()
	assert_int(ribbon.pressed_camera_mode()).is_equal(CameraManager.Mode.ORBIT)

	manager.set_mode(CameraManager.Mode.CAB)
	assert_int(ribbon.pressed_camera_mode()).is_equal(CameraManager.Mode.CAB)

	ribbon.button(Ribbon.CMD_CAMERA_WAYSIDE).button_pressed = true
	assert_int(manager.mode()).is_equal(CameraManager.Mode.WAYSIDE)
	assert_int(ribbon.pressed_camera_mode()).is_equal(CameraManager.Mode.WAYSIDE)


func _send_wheel(position: Vector2, button: MouseButton) -> void:
	for pressed in [true, false]:
		var event := InputEventMouseButton.new()
		event.button_index = button
		event.pressed = pressed
		event.position = position
		event.global_position = position
		Input.parse_input_event(event)
	await get_tree().process_frame
	await get_tree().process_frame


func test_mouse_input_reaches_the_camera_in_the_view_and_not_through_a_panel() -> void:
	var shell := await _make_shell()
	var manager := CameraManager.new()
	shell.world_root().add_child(manager)
	shell.bind_camera_manager(manager)
	var orbit := manager.camera_for(CameraManager.Mode.ORBIT).get_parent() as OrbitCamera

	var before := orbit.distance()
	await _send_wheel(shell.view_rect().get_center(), MOUSE_BUTTON_WHEEL_DOWN)
	assert_float(orbit.distance()).is_greater(before)

	var distance := orbit.distance()
	await _send_wheel(Vector2(100.0, 400.0), MOUSE_BUTTON_WHEEL_DOWN)
	assert_float(orbit.distance()).is_equal_approx(distance, 1e-6)

	shell.apply_preset(UiPreset.SANDBOX)
	await get_tree().process_frame
	await _send_wheel(Vector2(800.0, 300.0), MOUSE_BUTTON_WHEEL_DOWN)
	assert_float(orbit.distance()).is_greater(distance)
	var floating_distance := orbit.distance()
	await _send_wheel(Vector2(100.0, 400.0), MOUSE_BUTTON_WHEEL_DOWN)
	assert_float(orbit.distance()).is_equal_approx(floating_distance, 1e-6)


func _send_key(keycode: Key, unicode: int) -> void:
	for pressed in [true, false]:
		var event := InputEventKey.new()
		event.keycode = keycode
		event.physical_keycode = keycode
		event.unicode = unicode
		event.pressed = pressed
		Input.parse_input_event(event)
	await get_tree().process_frame
	await get_tree().process_frame


func test_typing_digits_in_a_field_does_not_switch_the_camera_but_the_shortcut_does() -> void:
	var shell := await _make_shell()
	var manager := CameraManager.new()
	shell.world_root().add_child(manager)
	shell.bind_camera_manager(manager)
	var field := LineEdit.new()
	shell.panel(UiShell.PANEL_INSPECTOR).body().add_child(field)
	await get_tree().process_frame
	field.grab_focus()

	await _send_key(KEY_2, 50)
	assert_str(field.text).is_equal("2")
	assert_int(manager.mode()).is_equal(CameraManager.Mode.ORBIT)

	field.release_focus()
	await _send_key(KEY_3, 51)
	assert_int(manager.mode()).is_equal(CameraManager.Mode.CAB)
	assert_int(shell.ribbon().pressed_camera_mode()).is_equal(CameraManager.Mode.CAB)

	shell.set_chrome_visible(false)
	await _send_key(KEY_2, 50)
	assert_int(manager.mode()).is_equal(CameraManager.Mode.WAYSIDE)


func test_settings_dialog_choices_are_stored_and_restored_on_the_next_start() -> void:
	var path := "user://t140_shell_settings_%d.cfg" % Time.get_ticks_usec()
	var shell := await _make_shell()
	shell.use_settings(UserSettings.new(path))
	var dialog := shell.settings_dialog()
	shell.open_settings()
	await get_tree().process_frame

	dialog.style_choice().select(1)
	dialog.style_choice().item_selected.emit(1)
	dialog.opacity_slider().value = 0.55
	dialog.hide()
	await get_tree().process_frame
	assert_str(String(shell.preset().id)).is_equal("sandbox")
	assert_float(shell.panel_opacity()).is_equal_approx(0.55, 1e-6)
	assert_bool(shell.panel(UiShell.PANEL_LAYERS).is_floating()).is_true()

	var restart := await _make_shell()
	restart.use_settings(UserSettings.new(path))
	assert_str(String(restart.preset().id)).is_equal("sandbox")
	assert_float(restart.panel_opacity()).is_equal_approx(0.55, 1e-6)
	assert_bool(restart.panel(UiShell.PANEL_LAYERS).is_floating()).is_true()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(path))


func _seen_through(scene_luma: float, box: StyleBoxFlat) -> float:
	var after_shadow := scene_luma * (1.0 - box.shadow_color.a)
	var fill_luma := 0.2126 * box.bg_color.r + 0.7152 * box.bg_color.g + 0.0722 * box.bg_color.b
	return after_shadow * (1.0 - box.bg_color.a) + fill_luma * box.bg_color.a


func test_a_lower_opacity_shows_more_of_the_scene_because_the_shadow_fades_too() -> void:
	var shell := await _make_shell()
	shell.apply_preset(UiPreset.SANDBOX, 0.72)
	var panel := shell.panel(UiShell.PANEL_LAYERS)
	var at_072 := (panel.get_theme_stylebox(&"panel") as StyleBoxFlat).duplicate() as StyleBoxFlat
	shell.set_panel_opacity(0.4)
	var at_040 := (panel.get_theme_stylebox(&"panel") as StyleBoxFlat).duplicate() as StyleBoxFlat
	shell.set_panel_opacity(1.0)
	var at_100 := (panel.get_theme_stylebox(&"panel") as StyleBoxFlat).duplicate() as StyleBoxFlat

	assert_float(at_040.shadow_color.a).is_less(at_072.shadow_color.a)
	assert_float(at_072.shadow_color.a).is_less(at_100.shadow_color.a)
	assert_float(at_040.shadow_color.a).is_equal_approx(0.0, 1e-6)
	assert_float(DockPanel.shadow_scale(1.0)).is_equal_approx(1.0, 1e-6)
	# A bright scene behind the card: the lower the opacity, the brighter it shows through.
	var bright_040 := _seen_through(0.8, at_040)
	var bright_072 := _seen_through(0.8, at_072)
	var bright_100 := _seen_through(0.8, at_100)
	assert_float(bright_040).is_greater(bright_072 + 0.05)
	assert_float(bright_072).is_greater(bright_100)


func test_sandbox_timeline_is_a_real_pill_and_the_view_is_full_bleed() -> void:
	var shell := await _make_shell()
	var timeline := shell.panel(UiShell.PANEL_TIMELINE)
	shell.apply_preset(UiPreset.SANDBOX)
	await get_tree().process_frame
	assert_bool(timeline.is_pill()).is_true()
	assert_float(timeline.size.y).is_equal_approx(UiShell.FLOAT_PILL_H, 0.5)
	assert_float(UiShell.FLOAT_PILL_H).is_between(48.0, 64.0)
	assert_int(UiShell.PILL_RADIUS_PX).is_equal(int(UiShell.FLOAT_PILL_H * 0.5))
	assert_bool(timeline.get_node("Column/Header").visible).is_false()
	assert_bool(UiPreset.builtin(UiPreset.SANDBOX).viewport_full_bleed).is_true()
	assert_float(shell.view_rect().size.x).is_equal_approx(1600.0, 0.5)

	shell.apply_preset(UiPreset.ENGINEERING)
	await get_tree().process_frame
	assert_bool(timeline.is_pill()).is_false()
	assert_bool(timeline.get_node("Column/Header").visible).is_true()
	assert_float(timeline.size.y).is_equal_approx(UiShell.DOCK_BOTTOM_H, 0.5)


func test_export_result_reaches_the_status_bar() -> void:
	var shell := await _make_shell()
	var summary := "Envelope written: a.geojson (C:/o) | 12.50 km² | 321 vertices"
	shell.export_dialog().exported.emit({"files": ["C:/o/a.geojson"]}, summary)
	assert_str(shell.status_message_text()).is_equal(summary)


func test_chrome_rects_cover_the_visible_chrome_and_nothing_when_hidden() -> void:
	var shell := await _make_shell()
	assert_int(shell.chrome_rects().size()).is_greater_equal(5)
	shell.set_chrome_visible(false)
	assert_int(shell.chrome_rects().size()).is_equal(0)


## Part B: the popover closes with Escape and with its Close button, and Close has the focus when it opens.
func test_popover_closes_on_escape_and_close() -> void:
	var shell := await _make_shell()
	shell.show_data_sources()
	await get_tree().process_frame
	var popover := shell.data_sources_popover()
	assert_bool(popover.visible).is_true()
	var close := popover.find_child("Close", true, false) as Button
	assert_object(close).is_not_null()
	assert_bool(close.has_focus()).append_failure_message("Close does not have the initial focus").is_true()

	# A real Escape key through the viewport, as the user sends it: the popover's own ui_cancel handling must close it.
	var escape := InputEventKey.new()
	escape.keycode = KEY_ESCAPE
	escape.physical_keycode = KEY_ESCAPE
	escape.pressed = true
	assert_bool(escape.is_action_pressed("ui_cancel")).append_failure_message("Escape is not ui_cancel").is_true()
	shell.get_viewport().push_input(escape)
	await get_tree().process_frame
	assert_bool(popover.visible).append_failure_message("Escape did not close the popover").is_false()

	shell.show_data_sources()
	await get_tree().process_frame
	close.pressed.emit()
	assert_bool(popover.visible).append_failure_message("Close did not close the popover").is_false()


## T-135 Part B, review round: the press that dismisses the popover (its light-dismiss hides it) is followed at once by the
## button's pressed on the release, which must not reopen it. A press later than the guard does open it.
func test_pressing_the_attribution_just_after_the_popover_hid_does_not_reopen_it() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "A", "notices": PackedStringArray()}]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	shell.show_data_sources()
	await get_tree().process_frame
	var popover := shell.data_sources_popover()
	assert_bool(popover.visible).is_true()

	popover.hide()
	shell.attribution_button().pressed.emit()
	await get_tree().process_frame
	await get_tree().process_frame
	assert_bool(popover.visible).append_failure_message("the press after the dismissal reopened the popover").is_false()

	await get_tree().create_timer(0.25).timeout
	shell.attribution_button().pressed.emit()
	await get_tree().process_frame
	await get_tree().process_frame
	assert_bool(popover.visible).append_failure_message("a press after the guard did not open the popover").is_true()
	shell.close_data_sources()
	Session.set_data_sources([] as Array[Dictionary])


## T-135 Part B, review round: the status message takes the room the attribution leaves, so with a long message and three
## sources the message is wider than the attribution button.
func test_status_message_is_wider_than_the_attribution_with_three_sources() -> void:
	var shell := await _make_shell()
	shell.show_message("Envelope written: a long file name in a long folder, 12.50 km² | 321 vertices | Kralupy corridor")
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "A", "notices": PackedStringArray()},
		{"source_id": "b", "kind": "imagery", "name": "I", "attribution": "B", "notices": PackedStringArray()},
		{"source_id": "c", "kind": "imagery", "name": "J", "attribution": "C", "notices": PackedStringArray()},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	await get_tree().process_frame
	var button := shell.attribution_button()
	assert_bool(button.visible).is_true()
	var message := shell.status_message_label()
	assert_float(message.size.x).append_failure_message(
		"the message is %f px wide, the attribution button %f px" % [message.size.x, button.size.x]
	).is_greater(button.size.x)
	Session.set_data_sources([] as Array[Dictionary])


## T-135 Part B, review round: the attribution button has the status label's metrics, so the status bar keeps its height
## when the button appears.
func test_status_bar_keeps_its_height_with_the_attribution_button() -> void:
	var shell := await _make_shell()
	Session.set_data_sources([] as Array[Dictionary])
	await get_tree().process_frame
	var without := shell.status_bar().get_combined_minimum_size().y
	var entries: Array[Dictionary] = [{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "Survey office", "notices": PackedStringArray()}]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_bool(shell.attribution_button().visible).is_true()
	assert_float(shell.status_bar().get_combined_minimum_size().y).append_failure_message(
		"the status bar is %f px with the button and %f px without" % [shell.status_bar().get_combined_minimum_size().y, without]
	).is_equal(without)
	assert_float(shell.status_bar().size.y).is_equal(without)
	Session.set_data_sources([] as Array[Dictionary])


## Part B: the attribution is a flat button that Tab reaches and Enter presses, which opens the popover.
func test_status_attribution_is_keyboard_reachable() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [{
		"source_id": "a", "kind": "imagery", "name": "Ortho", "attribution": "Survey office", "notices": PackedStringArray(),
	}]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	var button := shell.attribution_button()
	assert_bool(button.visible).is_true()
	assert_bool(button.flat).is_true()
	assert_int(button.focus_mode).is_equal(Control.FOCUS_ALL)
	assert_str(button.tooltip_text).is_equal("Click to see where the terrain and imagery come from.")
	button.grab_focus()
	await get_tree().process_frame
	assert_bool(button.has_focus()).append_failure_message("the attribution button cannot take the focus").is_true()
	var enter := InputEventKey.new()
	enter.keycode = KEY_ENTER
	enter.physical_keycode = KEY_ENTER
	enter.pressed = true
	shell.get_viewport().push_input(enter)
	var released := enter.duplicate() as InputEventKey
	released.pressed = false
	shell.get_viewport().push_input(released)
	await get_tree().process_frame
	await get_tree().process_frame
	assert_bool(shell.data_sources_popover().visible).append_failure_message("Enter did not open the popover").is_true()
	shell.close_data_sources()


## Part B: the ribbon's Source boundaries toggle reaches the terrain and the user's settings file.
func test_ribbon_source_boundaries_toggle_persists() -> void:
	var path := "user://t135b_shell_%d.cfg" % Time.get_ticks_usec()
	var shell := await _make_shell()
	shell.use_settings(UserSettings.new(path))
	var button := shell.ribbon().button(Ribbon.CMD_SOURCE_BOUNDARIES)
	assert_object(button).is_not_null()
	button.set_pressed(true)
	assert_bool(Session.source_boundaries()).is_true()
	assert_bool(UserSettings.new(path).terrain_source_boundaries()).is_true()
	button.set_pressed(false)
	assert_bool(Session.source_boundaries()).is_false()
	assert_bool(UserSettings.new(path).terrain_source_boundaries()).is_false()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(path))
	Session.set_source_boundaries(false)


## Part B: the status bar names only real texts, and the popover shows each entry's heading, attribution and notices.
func test_popover_shows_attribution_notices_and_headings() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "imagery", "name": "Ortho", "attribution": "Survey office",
			"notices": PackedStringArray(["Keep this notice."])},
		{"source_id": "b", "kind": "terrain", "name": "Tin", "attribution": "", "notices": PackedStringArray()},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	assert_str(shell.attribution_text()).is_equal("Survey office")
	var texts := []
	for child in shell.data_sources_popover().find_children("*", "Label", true, false):
		texts.append((child as Label).text)
	assert_array(texts).contains(["Imagery · Ortho", "Survey office", "Keep this notice.", "Terrain · Tin"])
	assert_array(texts).contains(["No attribution recorded for this file."])
	Session.set_data_sources([] as Array[Dictionary])


func test_status_summary_counts_sources_when_none_has_a_text() -> void:
	var none := [{"source_id": "a", "kind": "terrain", "attribution": ""}, {"source_id": "b", "kind": "imagery", "attribution": " "}]
	assert_str(UiShell.attribution_summary(none)).is_equal("2 data sources")
	assert_str(UiShell.attribution_summary([{"source_id": "a", "kind": "terrain", "attribution": ""}])).is_equal("1 data source")
	assert_str(UiShell.attribution_summary([])).is_empty()
	var repeated := [{"attribution": "X"}, {"attribution": "X"}, {"attribution": "Y"}]
	assert_str(UiShell.attribution_summary(repeated)).is_equal("X · Y")


## T-135 Part B, review round: the attribution button is a keyboard stop that shows focus in both presets: its effective
## focus style is the theme's, not an empty one.
func test_attribution_button_shows_keyboard_focus() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "A", "notices": PackedStringArray()}]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	for preset_id: StringName in [UiPreset.ENGINEERING, UiPreset.SANDBOX]:
		shell.apply_preset(preset_id)
		await get_tree().process_frame
		var focus := shell.attribution_button().get_theme_stylebox(&"focus")
		assert_bool(focus != null).append_failure_message("no focus style in %s" % preset_id).is_true()
		assert_bool(focus is StyleBoxEmpty).append_failure_message("the focus style is empty in %s" % preset_id).is_false()
	Session.set_data_sources([] as Array[Dictionary])


## T-135 Part B, review round: the popover separates each entry from the next, with the theme's separator, and not before the first.
func test_popover_separates_each_entry_from_the_next() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "A", "notices": PackedStringArray()},
		{"source_id": "b", "kind": "imagery", "name": "I", "attribution": "B", "notices": PackedStringArray(["N"])},
		{"source_id": "c", "kind": "imagery", "name": "J", "attribution": "C", "notices": PackedStringArray()},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	var separators := shell.data_sources_popover().find_children("*", "HSeparator", true, false)
	assert_int(separators.size()).is_equal(2)
	Session.set_data_sources([] as Array[Dictionary])


## T-135 Part C review: the Data sources popover is as tall as its entries, and no taller than the cap: 80 % of the window.
func test_popover_grows_with_its_entries_and_stops_at_the_cap() -> void:
	var shell := await _make_shell()
	shell.popover_window_height = 500.0
	var one: Array[Dictionary] = [{"source_id": "a", "kind": "terrain", "name": "T", "attribution": "A", "notices": PackedStringArray()}]
	Session.set_data_sources(one)
	await get_tree().process_frame
	shell.show_data_sources()
	await get_tree().process_frame
	await get_tree().process_frame
	var single := shell.data_sources_popover().size.y
	shell.close_data_sources()
	var many: Array[Dictionary] = []
	for i in 30:
		many.append({"source_id": "s%d" % i, "kind": "imagery", "name": "Source %d" % i,
			"attribution": "Attribution text %d, long enough to wrap onto a second line in the popover." % i,
			"notices": PackedStringArray()})
	Session.set_data_sources(many)
	await get_tree().process_frame
	shell.show_data_sources()
	await get_tree().process_frame
	await get_tree().process_frame
	var crowded := shell.data_sources_popover().size.y
	var cap := int(500.0 * UiShell.POPOVER_MAX_HEIGHT_FRACTION)
	assert_int(int(single)).append_failure_message("one entry: %d px" % single).is_less(int(crowded))
	assert_int(int(crowded)).append_failure_message("thirty entries: %d px, cap %d px" % [crowded, cap]).is_less_equal(cap)
	# The headless window is 16 px smaller than the requested rect (measured: 400 px box, 384 px window), so it can fall
	# up to 16 px short of the cap. On a real display the window is exact.
	assert_int(int(crowded)).append_failure_message("thirty entries stop short of the cap: %d px, cap %d px" % [crowded, cap]).is_greater_equal(cap - 16)
	shell.close_data_sources()
	Session.set_data_sources([] as Array[Dictionary])


## T-136 Part C3: the swatch of a terrain entry is the colour the boundary overlay draws that source in. Its colour is
## read from the palette and from [method TerrainMaterial.overlay_colour] alike, exactly. Imagery and unnamed sources
## have no swatch.
func test_swatch_colour_matches_the_overlay() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "terrain", "name": "Fine", "attribution": "A", "notices": PackedStringArray(),
			"licence": "", "overlay_index": 0},
		{"source_id": "b", "kind": "terrain", "name": "Coarse", "attribution": "B", "notices": PackedStringArray(),
			"licence": "", "overlay_index": 3},
		{"source_id": "c", "kind": "terrain", "name": "Unnamed", "attribution": "C", "notices": PackedStringArray(),
			"licence": "", "overlay_index": -1},
		{"source_id": "d", "kind": "imagery", "name": "Ortho", "attribution": "D", "notices": PackedStringArray(),
			"licence": "", "overlay_index": -1},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	var swatches := shell.data_sources_popover().find_children("*", "ColorRect", true, false)
	assert_int(swatches.size()).append_failure_message("expected one swatch per terrain entry with an index").is_equal(2)
	if swatches.size() != 2:
		Session.set_data_sources([] as Array[Dictionary])
		return
	var palette := TerrainMaterial.palette_texture().get_image()
	for pair: Array in [[0, swatches[0]], [3, swatches[1]]]:
		var index: int = pair[0]
		var colour := (pair[1] as ColorRect).color
		assert_int(colour.to_rgba32()).append_failure_message(
			"the swatch of index %d is not overlay_colour(%d)" % [index, index]
		).is_equal(TerrainMaterial.overlay_colour(index).to_rgba32())
		assert_int(colour.to_rgba32()).append_failure_message(
			"the swatch of index %d is not the palette's pixel" % index
		).is_equal(palette.get_pixel(index, 0).to_rgba32())
	Session.set_data_sources([] as Array[Dictionary])


## T-136 Part C3: a recorded licence of a file source shows as "Licensed under <licence>" once, and never for the
## placeholder or when one of the entry's notices already states it (a provider's notice begins with its licence).
func test_licence_line_is_shown_once_and_never_for_the_placeholder() -> void:
	assert_str(UiShell.licence_line({"licence": "CC BY 4.0", "notices": PackedStringArray()})).is_equal(
		"Licensed under CC BY 4.0"
	)
	assert_str(UiShell.licence_line({"licence": " CC BY 4.0 ", "notices": PackedStringArray()})).is_equal(
		"Licensed under CC BY 4.0"
	)
	assert_str(UiShell.licence_line({"licence": "unknown", "notices": PackedStringArray()})).is_empty()
	assert_str(UiShell.licence_line({"notices": PackedStringArray()})).is_empty()
	assert_str(UiShell.licence_line({
		"licence": "CC BY 4.0", "notices": PackedStringArray(["Licensed under CC BY 4.0, see the survey office."]),
	})).is_empty()


## The swatches are the same in the sandbox preset as in the engineering one, with the popover opened under it.
func test_swatches_show_under_the_sandbox_preset() -> void:
	var shell := await _make_shell()
	shell.apply_preset(UiPreset.SANDBOX, 0.72)
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "terrain", "name": "Fine", "attribution": "A", "notices": PackedStringArray(),
			"licence": "", "overlay_index": 0},
		{"source_id": "b", "kind": "terrain", "name": "Coarse", "attribution": "B", "notices": PackedStringArray(),
			"licence": "", "overlay_index": 1},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	shell.show_data_sources()
	await get_tree().process_frame
	await get_tree().process_frame
	var swatches := shell.data_sources_popover().find_children("*", "ColorRect", true, false)
	assert_int(swatches.size()).append_failure_message("the sandbox popover has %d swatches, not 2" % swatches.size()).is_equal(2)
	for node in swatches:
		var swatch := node as ColorRect
		assert_vector(swatch.custom_minimum_size).append_failure_message("the swatch is not 12 x 12 px").is_equal(Vector2(12.0, 12.0))
		assert_str(swatch.tooltip_text).is_equal(UiShell.SWATCH_TOOLTIP)
		assert_bool(swatch.is_visible_in_tree()).append_failure_message("the swatch is not shown").is_true()
	shell.close_data_sources()
	Session.set_data_sources([] as Array[Dictionary])


## T-136 Part C3, review: with imagery warnings and no entry, the status bar still opens the popover under the title
## "Imagery warnings", and the popover lists the warnings without the empty sentence.
func test_warnings_only_popover_is_reachable() -> void:
	var shell := await _make_shell()
	Session.set_data_sources([] as Array[Dictionary])
	Session.set_imagery_warnings(PackedStringArray(["A warning that names no source"]))
	await get_tree().process_frame
	var button := shell.attribution_button()
	assert_bool(button.visible).append_failure_message("the status bar hides the warnings-only popover").is_true()
	assert_str(button.text).is_equal(UiShell.IMAGERY_WARNINGS_TITLE)
	var texts := []
	for child in shell.data_sources_popover().find_children("*", "Label", true, false):
		texts.append((child as Label).text)
	assert_array(texts).contains(["Imagery warnings", "A warning that names no source"])
	assert_bool(texts.has(UiShell.POPOVER_EMPTY)).append_failure_message("the empty sentence shows with warnings").is_false()
	Session.set_imagery_warnings(PackedStringArray())


## The popover shows the licence line under the attribution of a file source, in the same entry as its notices.
func test_popover_shows_the_licence_of_a_file_source() -> void:
	var shell := await _make_shell()
	var entries: Array[Dictionary] = [
		{"source_id": "a", "kind": "terrain", "name": "DMR 5G", "attribution": "© ČÚZK",
			"notices": PackedStringArray(), "licence": "CC BY 4.0", "overlay_index": 0},
	]
	Session.set_data_sources(entries)
	await get_tree().process_frame
	var texts := []
	for child in shell.data_sources_popover().find_children("*", "Label", true, false):
		texts.append((child as Label).text)
	assert_array(texts).contains(["Licensed under CC BY 4.0"])
	Session.set_data_sources([] as Array[Dictionary])
