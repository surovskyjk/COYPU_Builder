extends GdUnitTestSuite
## Unit tests for `IpcDiscovery` (T-126): record validation, selection and the directory scan. The records
## written here use made-up tokens that exist only inside this suite.

const _DIR := "user://t126_discovery"
const _DEAD_PID := 2147483000


func _record(port: int, pid: int, started_at: String, token: String = "tok-for-tests") -> Dictionary:
	return {
		"port": port,
		"pid": pid,
		"protocol": "2",
		"backend_version": "0.1.0",
		"token": token,
		"started_at": started_at,
	}


func _write(file_name: String, record: Dictionary) -> void:
	var file := FileAccess.open(_DIR.path_join(file_name), FileAccess.WRITE)
	file.store_string(JSON.stringify(record))
	file.close()


func before_test() -> void:
	DirAccess.make_dir_recursive_absolute(_DIR)
	_clear()


func after_test() -> void:
	_clear()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(_DIR))


func _clear() -> void:
	var dir := DirAccess.open(_DIR)
	if dir == null:
		return
	for file_name in dir.get_files():
		dir.remove(file_name)


func test_a_complete_record_parses() -> void:
	var parsed := IpcDiscovery.parse_record(JSON.stringify(_record(5000, 12, "2026-10-08T10:00:00+00:00")))
	assert_int(parsed["port"]).is_equal(5000)
	assert_int(parsed["pid"]).is_equal(12)
	assert_str(parsed["token"]).is_equal("tok-for-tests")


func test_incomplete_or_malformed_records_are_rejected() -> void:
	assert_that(IpcDiscovery.parse_record("")).is_equal({})
	assert_that(IpcDiscovery.parse_record("{broken")).is_equal({})
	assert_that(IpcDiscovery.parse_record("[]")).is_equal({})
	var missing_token := _record(5000, 12, "t")
	missing_token.erase("token")
	assert_that(IpcDiscovery.parse_record(JSON.stringify(missing_token))).is_equal({})
	assert_that(IpcDiscovery.parse_record(JSON.stringify(_record(5000, 12, "t", "")))).is_equal({})
	assert_that(IpcDiscovery.parse_record(JSON.stringify(_record(0, 12, "t")))).is_equal({})


func test_select_prefers_the_matching_port_then_the_newest() -> void:
	var records: Array[Dictionary] = [
		_record(1, 10, "2026-10-08T09:00:00+00:00"),
		_record(2, 11, "2026-10-08T11:00:00+00:00"),
		_record(3, 12, "2026-10-08T10:00:00+00:00"),
	]
	assert_int(IpcDiscovery.select(records, -1)["port"]).is_equal(2)
	assert_int(IpcDiscovery.select(records, 3)["port"]).is_equal(3)
	assert_that(IpcDiscovery.select(records, 99)).is_equal({})
	assert_that(IpcDiscovery.select([] as Array[Dictionary], -1)).is_equal({})


func test_records_in_ignores_unrelated_files_and_a_name_that_does_not_match_the_pid() -> void:
	var me := OS.get_process_id()
	_write("backend-%d.json" % me, _record(4000, me, "2026-10-08T10:00:00+00:00"))
	_write("backend-%d.json" % _DEAD_PID, _record(4001, _DEAD_PID, "2026-10-08T12:00:00+00:00"))
	_write("notes.json", _record(4002, me, "2026-10-08T13:00:00+00:00"))
	_write("backend-7.json", _record(4003, me, "2026-10-08T14:00:00+00:00"))
	var ports: Array[int] = []
	for record in IpcDiscovery.records_in(_DIR):
		ports.append(int(record["port"]))
	ports.sort()
	assert_array(ports).is_equal([4000, 4001])


func test_find_returns_the_live_candidate() -> void:
	var me := OS.get_process_id()
	_write("backend-%d.json" % me, _record(4000, me, "2026-10-08T10:00:00+00:00"))
	assert_int(IpcDiscovery.find(4000, _DIR)["port"]).is_equal(4000)
	assert_int(IpcDiscovery.find(-1, _DIR)["port"]).is_equal(4000)
	assert_that(IpcDiscovery.find(4999, _DIR)).is_equal({})


func test_find_deletes_the_record_of_a_dead_candidate_and_returns_nothing() -> void:
	var me := OS.get_process_id()
	_write("backend-%d.json" % me, _record(4000, me, "2026-10-08T10:00:00+00:00"))
	_write("backend-%d.json" % _DEAD_PID, _record(4001, _DEAD_PID, "2026-10-08T12:00:00+00:00"))
	var dead_path := _DIR.path_join("backend-%d.json" % _DEAD_PID)
	# The newest record is the dead one: it is the only candidate checked, and it is removed.
	assert_that(IpcDiscovery.find(-1, _DIR)).is_equal({})
	assert_bool(FileAccess.file_exists(dead_path)).is_false()
	# The next attempt moves on to the live one, which is untouched.
	assert_int(IpcDiscovery.find(-1, _DIR)["port"]).is_equal(4000)
	assert_bool(FileAccess.file_exists(_DIR.path_join("backend-%d.json" % me))).is_true()


func test_delete_record_removes_only_the_named_pid() -> void:
	var me := OS.get_process_id()
	_write("backend-%d.json" % me, _record(4000, me, "t"))
	_write("backend-%d.json" % _DEAD_PID, _record(4001, _DEAD_PID, "t"))
	IpcDiscovery.delete_record(_DIR, _DEAD_PID)
	IpcDiscovery.delete_record(_DIR, _DEAD_PID)
	IpcDiscovery.delete_record(_DIR, -1)
	IpcDiscovery.delete_record("", 5)
	assert_bool(FileAccess.file_exists(_DIR.path_join("backend-%d.json" % _DEAD_PID))).is_false()
	assert_bool(FileAccess.file_exists(_DIR.path_join("backend-%d.json" % me))).is_true()


func test_attach_url_accepts_only_loopback_with_a_port() -> void:
	assert_int(IpcDiscovery.parse_attach_url("ws://127.0.0.1:8765")["port"]).is_equal(8765)
	assert_int(IpcDiscovery.parse_attach_url("ws://127.0.0.1:8765/")["port"]).is_equal(8765)
	assert_int(IpcDiscovery.parse_attach_url("ws://127.0.0.1:1")["port"]).is_equal(1)
	assert_int(IpcDiscovery.parse_attach_url("ws://127.0.0.1:65535")["port"]).is_equal(65535)


func test_attach_url_refuses_userinfo_a_missing_port_other_hosts_paths_and_queries() -> void:
	var refused: Array[String] = [
		"",
		"ws://127.0.0.1:8765@attacker.example:443",
		"ws://127.0.0.1:8765@attacker.example",
		"ws://user@127.0.0.1:8765",
		"ws://127.0.0.1",
		"ws://127.0.0.1/",
		"ws://127.0.0.1:",
		"ws://localhost:8765",
		"ws://[::1]:8765",
		"ws://192.0.2.1:8765",
		"ws://127.0.0.1.attacker.example:8765",
		"ws://127.0.0.1:8765/path",
		"ws://127.0.0.1:8765//",
		"ws://127.0.0.1:8765/?x=1",
		"ws://127.0.0.1:8765?x=1",
		"ws://127.0.0.1:8765#f",
		"ws://127.0.0.1:8765:9",
		"ws://127.0.0.1:87 65",
		"ws://127.0.0.1:-1",
		"ws://127.0.0.1:0",
		"ws://127.0.0.1:65536",
		"ws://127.0.0.1:123456",
		"wss://127.0.0.1:8765",
		"http://127.0.0.1:8765",
		"WS://127.0.0.1:8765",
		"127.0.0.1:8765",
	]
	for url in refused:
		assert_that(IpcDiscovery.parse_attach_url(url)).is_equal({})


func test_a_missing_directory_yields_no_records() -> void:
	assert_int(IpcDiscovery.records_in("user://t126_no_such_dir").size()).is_equal(0)
	assert_int(IpcDiscovery.records_in("").size()).is_equal(0)


func test_pid_liveness() -> void:
	assert_bool(IpcDiscovery.is_pid_alive(OS.get_process_id())).is_true()
	assert_bool(IpcDiscovery.is_pid_alive(_DEAD_PID)).is_false()
	assert_bool(IpcDiscovery.is_pid_alive(0)).is_false()
