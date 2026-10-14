extends GdUnitTestSuite
## UserSettings (T-140): defaults, clamping, the file round trip and a damaged file. Files go to `user://`
## under test-only names and are removed afterwards; the real `user://settings.cfg` is never touched.

var _paths: Array[String] = []


func after_test() -> void:
	for path in _paths:
		DirAccess.remove_absolute(ProjectSettings.globalize_path(path))
	_paths.clear()


func _temp_path() -> String:
	var path := "user://t140_settings_%d_%d.cfg" % [Time.get_ticks_usec(), _paths.size()]
	_paths.append(path)
	return path


func test_defaults() -> void:
	var settings := UserSettings.new()
	assert_str(String(settings.preset_id())).is_equal("engineering")
	assert_float(settings.panel_opacity()).is_equal_approx(1.0, 1e-6)
	assert_bool(settings.has_panel_opacity()).is_false()


func test_default_opacity_comes_from_the_current_preset() -> void:
	var settings := UserSettings.new()
	settings.set_preset_id(&"sandbox")
	assert_float(settings.panel_opacity()).is_equal_approx(0.72, 1e-6)


func test_opacity_is_clamped_to_the_allowed_range() -> void:
	var settings := UserSettings.new()
	settings.set_panel_opacity(0.1)
	assert_float(settings.panel_opacity()).is_equal_approx(0.4, 1e-6)
	settings.set_panel_opacity(3.0)
	assert_float(settings.panel_opacity()).is_equal_approx(1.0, 1e-6)
	settings.set_panel_opacity(0.65)
	assert_float(settings.panel_opacity()).is_equal_approx(0.65, 1e-6)


func test_unknown_preset_id_is_ignored() -> void:
	var settings := UserSettings.new()
	settings.set_preset_id(&"sandbox")
	settings.set_preset_id(&"retro")
	assert_str(String(settings.preset_id())).is_equal("sandbox")


func test_choices_survive_a_save_and_reload() -> void:
	var path := _temp_path()
	var settings := UserSettings.new(path)
	settings.set_preset_id(&"sandbox")
	settings.set_panel_opacity(0.55)
	assert_bool(settings.save()).is_true()

	var reloaded := UserSettings.new(path)
	assert_str(String(reloaded.preset_id())).is_equal("sandbox")
	assert_float(reloaded.panel_opacity()).is_equal_approx(0.55, 1e-6)
	assert_bool(reloaded.has_panel_opacity()).is_true()


func test_memory_only_settings_write_nothing() -> void:
	var settings := UserSettings.new()
	settings.set_preset_id(&"sandbox")
	assert_bool(settings.save()).is_false()


func test_missing_file_gives_the_defaults() -> void:
	var settings := UserSettings.new(_temp_path())
	assert_str(String(settings.preset_id())).is_equal("engineering")
	assert_bool(settings.has_panel_opacity()).is_false()


func test_corrupt_file_gives_the_defaults() -> void:
	var path := _temp_path()
	var file := FileAccess.open(path, FileAccess.WRITE)
	file.store_string("[interface\npreset = {{{ not a config\n")
	file.close()
	var settings := UserSettings.new(path)
	assert_str(String(settings.preset_id())).is_equal("engineering")
	assert_bool(settings.has_panel_opacity()).is_false()


func test_wrong_value_types_are_ignored() -> void:
	var path := _temp_path()
	var config := ConfigFile.new()
	config.set_value("interface", "preset", 5)
	config.set_value("interface", "panel_opacity", "lots")
	config.save(path)
	var settings := UserSettings.new(path)
	assert_str(String(settings.preset_id())).is_equal("engineering")
	assert_bool(settings.has_panel_opacity()).is_false()


func test_stored_out_of_range_opacity_is_clamped_on_load() -> void:
	var path := _temp_path()
	var config := ConfigFile.new()
	config.set_value("interface", "preset", "sandbox")
	config.set_value("interface", "panel_opacity", 0.01)
	config.save(path)
	assert_float(UserSettings.new(path).panel_opacity()).is_equal_approx(0.4, 1e-6)


func _write_raw(path: String, text: String) -> void:
	var file := FileAccess.open(path, FileAccess.WRITE)
	file.store_string(text)
	file.close()


func test_file_with_lf_line_endings_loads() -> void:
	var path := _temp_path()
	_write_raw(path, "[interface]\n\npreset=\"sandbox\"\npanel_opacity=0.5\n")
	var settings := UserSettings.new(path)
	assert_str(String(settings.preset_id())).is_equal("sandbox")
	assert_float(settings.panel_opacity()).is_equal_approx(0.5, 1e-6)


func test_file_with_crlf_line_endings_loads() -> void:
	var path := _temp_path()
	_write_raw(path, "[interface]\r\n\r\npreset=\"sandbox\"\r\npanel_opacity=0.5\r\n")
	var settings := UserSettings.new(path)
	assert_str(String(settings.preset_id())).is_equal("sandbox")
	assert_float(settings.panel_opacity()).is_equal_approx(0.5, 1e-6)
