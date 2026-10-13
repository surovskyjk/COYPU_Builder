extends GdUnitTestSuite
## Unit tests for the pure parts of `IpcProcessSupervisor` (T-126): the spawn arguments and the startup
## line. No process is started. The token must never be among the spawn arguments (ADR 0010).

const _LINE := '{"port": 51234, "token": "tok-abc_123", "pid": 4242, "protocol": "2"}'


func test_spawn_arguments_add_only_the_port() -> void:
	var base := PackedStringArray(["run", "--project", "C:/b", "coypu-builder-backend", "serve"])
	var full := IpcProcessSupervisor.build_args(base)
	assert_array(full).is_equal(
		["run", "--project", "C:/b", "coypu-builder-backend", "serve", "--port", "0"]
	)


func test_spawn_arguments_carry_no_token_option_and_do_not_mutate_the_input() -> void:
	var base := PackedStringArray(["serve"])
	var full := IpcProcessSupervisor.build_args(base)
	assert_int(base.size()).is_equal(1)
	for arg in full:
		assert_bool(arg.contains("token")).is_false()


func test_startup_line_yields_port_and_token() -> void:
	var parsed := IpcProcessSupervisor.parse_startup_line(_LINE)
	assert_int(parsed["port"]).is_equal(51234)
	assert_str(parsed["token"]).is_equal("tok-abc_123")
	assert_int(parsed["pid"]).is_equal(4242)


func test_startup_line_without_a_token_yields_an_empty_one() -> void:
	var parsed := IpcProcessSupervisor.parse_startup_line('{"port": 7, "pid": 1, "protocol": "2"}')
	assert_int(parsed["port"]).is_equal(7)
	assert_str(parsed["token"]).is_empty()
	assert_int(parsed["pid"]).is_equal(1)


func test_lines_that_are_not_startup_lines_are_ignored() -> void:
	assert_that(IpcProcessSupervisor.parse_startup_line("")).is_equal({})
	assert_that(IpcProcessSupervisor.parse_startup_line("not json")).is_equal({})
	assert_that(IpcProcessSupervisor.parse_startup_line("[1, 2]")).is_equal({})
	assert_that(IpcProcessSupervisor.parse_startup_line('{"status": "ok"}')).is_equal({})
