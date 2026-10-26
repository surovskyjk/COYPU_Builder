extends GdUnitTestSuite
## Session's terrain calls (T-135): which failures reach EventBus.backend_error. No backend: a seam answers.

var _reported: Array = []


func before_test() -> void:
	_reported = []
	EventBus.backend_error.connect(_on_backend_error)


func after_test() -> void:
	EventBus.backend_error.disconnect(_on_backend_error)
	Session.set_terrain_request_override(Callable())


func _on_backend_error(code: String, _message: String) -> void:
	_reported.append(code)


func _failing(code: String) -> Callable:
	return func(method: String, _params: Dictionary) -> IpcEnvelope:
		var envelope := IpcEnvelope.new()
		envelope.type = "err"
		envelope.method = method
		envelope.error = {"code": code, "message": "test"}
		envelope.blobs = {}
		return envelope


func test_expected_failures_are_not_reported_as_backend_errors() -> void:
	for code in ["E_BUSY", "E_NOT_FOUND", "E_NO_PROJECT", "E_NO_SESSION"]:
		Session.set_terrain_request_override(_failing(code))
		var listed := await Session.fetch_terrain_tiles(0)
		var fetched := await Session.fetch_terrain_tile(0, 0, 0, 1)
		assert_str(String(listed.error.get("code", ""))).is_equal(code)
		assert_str(String(fetched.error.get("code", ""))).is_equal(code)
	assert_array(_reported).append_failure_message("reported: %s" % str(_reported)).is_empty()


func test_other_failures_are_reported_once_per_call() -> void:
	Session.set_terrain_request_override(_failing("E_INTERNAL"))
	await Session.fetch_terrain_tiles(0)
	await Session.fetch_terrain_tile(0, 0, 0, 1)
	assert_array(_reported).is_equal(["E_INTERNAL", "E_INTERNAL"])
