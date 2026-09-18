extends GdUnitTestSuite
## Unit tests for `CliArgs.parse` (T-103). No backend, no network.


func test_parses_all_three_known_flags() -> void:
	var result := CliArgs.parse(
		PackedStringArray(
			["--backend-url", "ws://127.0.0.1:8765", "--backend-token", "dev", "--project", "C:/p.xml"]
		)
	)
	assert_str(result.get("backend_url", "")).is_equal("ws://127.0.0.1:8765")
	assert_str(result.get("backend_token", "")).is_equal("dev")
	assert_str(result.get("project", "")).is_equal("C:/p.xml")


func test_flags_can_appear_in_any_order() -> void:
	var result := CliArgs.parse(
		PackedStringArray(["--project", "p.xml", "--backend-url", "ws://x"])
	)
	assert_str(result.get("project", "")).is_equal("p.xml")
	assert_str(result.get("backend_url", "")).is_equal("ws://x")
	assert_bool(result.has("backend_token")).is_false()


func test_empty_args_yield_empty_dictionary() -> void:
	var result := CliArgs.parse(PackedStringArray())
	assert_that(result).is_equal({})


## Ignored arguments go through `push_warning`, which `project.godot`'s `report/godot/push_error=true`
## turns into a test failure unless it is explicitly expected and consumed via `assert_error(...)
## .is_push_warning(...)` — plain `CliArgs.parse(...)` calls would otherwise fail these tests on the very
## behaviour they are checking.
##
## The result is threaded out of the lambda through a single-element Array ("box"): a lambda captures
## outer locals by value, so reassigning a captured variable directly would not be visible afterwards —
## mutating a captured container's contents is.
func test_unknown_argument_is_ignored() -> void:
	var result_box := [{}]
	await assert_error(
		func() -> void:
			result_box[0] = CliArgs.parse(PackedStringArray(["--editor", "--backend-url", "ws://x"]))
	).is_push_warning("CliArgs: unknown argument '--editor'")

	var result: Dictionary = result_box[0]
	assert_bool(result.has("--editor")).is_false()
	assert_int(result.size()).is_equal(1)
	assert_str(result.get("backend_url", "")).is_equal("ws://x")


func test_flag_without_a_value_is_ignored() -> void:
	var result_box := [{}]
	await assert_error(
		func() -> void: result_box[0] = CliArgs.parse(PackedStringArray(["--backend-token"]))
	).is_push_warning("CliArgs: '--backend-token' given without a value")

	var result: Dictionary = result_box[0]
	assert_bool(result.has("backend_token")).is_false()


func test_flag_immediately_followed_by_another_flag_is_ignored() -> void:
	var result_box := [{}]
	await assert_error(
		func() -> void:
			result_box[0] = CliArgs.parse(
				PackedStringArray(["--backend-token", "--backend-url", "ws://x"])
			)
	).is_push_warning("CliArgs: '--backend-token' given without a value")

	var result: Dictionary = result_box[0]
	assert_bool(result.has("backend_token")).is_false()
	assert_str(result.get("backend_url", "")).is_equal("ws://x")
