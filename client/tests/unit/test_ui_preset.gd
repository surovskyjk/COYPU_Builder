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
		Ribbon.CMD_EXPORT_ENVELOPE, Ribbon.CMD_CAMERA_ORBIT, Ribbon.CMD_CAMERA_WAYSIDE, Ribbon.CMD_CAMERA_CAB,
		Ribbon.CMD_SETTINGS,
	])
	for id in ids:
		var command_button := ribbon.button(id)
		assert_bool(command_button.disabled).is_false()
		assert_str(command_button.tooltip_text).is_not_empty()
		assert_object(command_button.shortcut).is_not_null()
		assert_object(command_button.icon).is_not_null()
	assert_object(ribbon.find_child("ProjectGroup", true, false)).is_null()
	assert_object(ribbon.find_child("ImportGroup", true, false)).is_null()


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
