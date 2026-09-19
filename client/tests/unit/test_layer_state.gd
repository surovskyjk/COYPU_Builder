extends GdUnitTestSuite
## LayerState unit tests (T-115): pure state, no scene tree, no backend — must stay fast enough for
## `-a tests/unit`. Each test builds its own `LayerState.new()`, never the shared `Session.layers()`
## instance, so tests can't leak state into each other.


func test_undefined_layer_defaults_to_visible_and_opaque() -> void:
	var layers := LayerState.new()
	assert_bool(layers.is_visible("track")).is_true()
	assert_float(layers.opacity("track")).is_equal_approx(1.0, 1e-6)


func test_define_sets_visibility_and_opacity() -> void:
	var layers := LayerState.new()
	layers.define("track", "Track", false, 0.5)
	assert_bool(layers.is_visible("track")).is_false()
	assert_float(layers.opacity("track")).is_equal_approx(0.5, 1e-6)


func test_set_visible_updates_state_and_emits_layer_changed() -> void:
	var layers := LayerState.new()
	layers.define("track", "Track")
	var monitored := monitor_signals(layers)

	layers.set_visible("track", false)

	assert_bool(layers.is_visible("track")).is_false()
	await assert_signal(monitored).is_emitted("layer_changed", ["track"])


func test_set_opacity_updates_state_and_emits_layer_changed() -> void:
	var layers := LayerState.new()
	layers.define("track", "Track")
	var monitored := monitor_signals(layers)

	layers.set_opacity("track", 0.25)

	assert_float(layers.opacity("track")).is_equal_approx(0.25, 1e-6)
	await assert_signal(monitored).is_emitted("layer_changed", ["track"])


func test_set_visible_on_an_undefined_layer_is_a_no_op() -> void:
	var layers := LayerState.new()
	layers.set_visible("ghost", false)
	assert_bool(layers.is_visible("ghost")).is_true()


func test_set_opacity_on_an_undefined_layer_is_a_no_op() -> void:
	var layers := LayerState.new()
	layers.set_opacity("ghost", 0.1)
	assert_float(layers.opacity("ghost")).is_equal_approx(1.0, 1e-6)


func test_layers_lists_every_defined_layer_in_definition_order() -> void:
	var layers := LayerState.new()
	layers.define("track", "Track")
	layers.define("terrain", "Terrain", false, 0.8)

	var entries: Array[Dictionary] = layers.layers()
	assert_int(entries.size()).is_equal(2)
	assert_str(entries[0]["layer_id"]).is_equal("track")
	assert_bool(entries[0]["visible"]).is_true()
	assert_str(entries[1]["layer_id"]).is_equal("terrain")
	assert_bool(entries[1]["visible"]).is_false()
	assert_float(entries[1]["opacity"]).is_equal_approx(0.8, 1e-6)


func test_redefining_a_layer_keeps_its_position_in_definition_order() -> void:
	var layers := LayerState.new()
	layers.define("track", "Track")
	layers.define("terrain", "Terrain")
	layers.define("track", "Track (renamed)", false, 0.3)

	var entries: Array[Dictionary] = layers.layers()
	assert_int(entries.size()).is_equal(2)
	assert_str(entries[0]["layer_id"]).is_equal("track")
	assert_str(entries[0]["name"]).is_equal("Track (renamed)")
	assert_bool(entries[0]["visible"]).is_false()
