class_name IpcDiscovery
extends RefCounted
## Finds a running backend through its discovery record (ADR 0010; format in `docs/protocol/attach.md`):
## one `backend-<pid>.json` per backend in a per-user runtime directory. Used by `Backend` in attach mode
## so a session token never has to travel on a command line. The records hold the token: nothing here
## prints, logs or returns more of a record than the caller needs.
##
## The token must never leave this machine's loopback interface, so a caller never connects to a URL it was
## handed: [method parse_attach_url] accepts one exact shape and the caller builds the URL it connects to
## from the matching record's port.

const _PREFIX := "backend-"
const _SUFFIX := ".json"
const _REQUIRED_KEYS: Array[String] = ["port", "pid", "protocol", "backend_version", "token", "started_at"]
const _ATTACH_URL_PREFIX := "ws://127.0.0.1:"


## The runtime directory the backend writes to (the same rule as `server/discovery.py`), or "" if the
## environment does not say where the user's profile is.
static func runtime_dir() -> String:
	if OS.get_name() == "Windows":
		var local_app_data := OS.get_environment("LOCALAPPDATA")
		if local_app_data.is_empty():
			return ""
		return local_app_data.path_join("COYPU Builder").path_join("runtime")
	var xdg := OS.get_environment("XDG_RUNTIME_DIR")
	if not xdg.is_empty():
		return xdg.path_join("coypu-builder")
	var home := OS.get_environment("HOME")
	if home.is_empty():
		return ""
	return home.path_join(".cache").path_join("coypu-builder").path_join("runtime")


## Validates a user-supplied `--backend-url`. Exactly `ws://127.0.0.1:<port>` (optionally followed by one
## `/`) is accepted: the backend binds 127.0.0.1 only, and anything else (another host, `localhost`, an IPv6
## literal, userinfo such as `...:8765@host:443`, a missing port, a path or a query) is refused so the token
## cannot be steered elsewhere. Returns `{"port": int}`, or an empty [Dictionary] when refused.
static func parse_attach_url(url: String) -> Dictionary:
	if not url.begins_with(_ATTACH_URL_PREFIX):
		return {}
	var rest := url.substr(_ATTACH_URL_PREFIX.length())
	var digits := ""
	while digits.length() < rest.length() and "0123456789".contains(rest[digits.length()]):
		digits += rest[digits.length()]
	var tail := rest.substr(digits.length())
	if digits.is_empty() or digits.length() > 5 or (tail != "" and tail != "/"):
		return {}
	var port := int(digits)
	if port < 1 or port > 65535:
		return {}
	return {"port": port}


## The record to attach to: the one whose port is [param port] when that is >= 0, else the newest. Only that
## one candidate is checked for liveness (a process listing costs ~170 ms on Windows, and this runs on the
## main thread once per attach attempt, which retries no faster than once a second); a dead candidate's
## record is deleted and an empty [Dictionary] returned, so the next attempt moves on to the next record.
## Returns the record (keys: port, pid, protocol, backend_version, token, started_at) or an empty one.
static func find(port: int = -1, dir: String = "") -> Dictionary:
	var directory := dir if not dir.is_empty() else runtime_dir()
	var candidate := select(records_in(directory), port)
	if candidate.is_empty():
		return {}
	if not is_pid_alive(int(candidate["pid"])):
		delete_record(directory, int(candidate["pid"]))
		return {}
	return candidate


## Parsed, valid records found in [param dir] (not sorted, liveness not checked). A file whose name does not
## match the pid inside it is ignored.
static func records_in(dir: String) -> Array[Dictionary]:
	var records: Array[Dictionary] = []
	if dir.is_empty():
		return records
	var listing := DirAccess.open(dir)
	if listing == null:
		return records
	for file_name in listing.get_files():
		if not file_name.begins_with(_PREFIX) or not file_name.ends_with(_SUFFIX):
			continue
		var record := parse_record(FileAccess.get_file_as_string(dir.path_join(file_name)))
		if record.is_empty() or file_name != _file_name(int(record["pid"])):
			continue
		records.append(record)
	return records


## Removes the record of the backend with process id [param pid] from [param dir] (no error if absent).
static func delete_record(dir: String, pid: int) -> void:
	if dir.is_empty() or pid <= 0:
		return
	var path := dir.path_join(_file_name(pid))
	if FileAccess.file_exists(path):
		DirAccess.remove_absolute(path)


static func _file_name(pid: int) -> String:
	return "%s%d%s" % [_PREFIX, pid, _SUFFIX]


## Validates one record's JSON text; an empty [Dictionary] for anything that is not a complete record.
static func parse_record(text: String) -> Dictionary:
	var json := JSON.new()
	if json.parse(text) != OK:
		return {}
	var parsed = json.data
	if typeof(parsed) != TYPE_DICTIONARY:
		return {}
	for key in _REQUIRED_KEYS:
		if not parsed.has(key):
			return {}
	var port := int(parsed["port"])
	if String(parsed["token"]).is_empty() or port < 1 or port > 65535:
		return {}
	return {
		"port": port,
		"pid": int(parsed["pid"]),
		"protocol": String(parsed["protocol"]),
		"backend_version": String(parsed["backend_version"]),
		"token": String(parsed["token"]),
		"started_at": String(parsed["started_at"]),
	}


## Picks from [param records]: the one on [param port] when that is >= 0 (newest if several), else the
## newest. `started_at` is ISO 8601 UTC from one writer, so string order is time order.
static func select(records: Array[Dictionary], port: int = -1) -> Dictionary:
	var best := {}
	for record in records:
		if port >= 0 and int(record["port"]) != port:
			continue
		if best.is_empty() or String(record["started_at"]) > String(best["started_at"]):
			best = record
	return best


## A plain existence check that works for any process, not only our children. (`OS.is_process_running`
## only knows children, and logs an engine error for anything else.) When the check itself cannot run the
## answer is "alive": a record is only ever deleted on positive evidence that its process is gone.
static func is_pid_alive(pid: int) -> bool:
	if pid <= 0:
		return false
	if OS.get_name() == "Windows":
		var output: Array = []
		var code := OS.execute("tasklist", ["/NH", "/FO", "CSV", "/FI", "PID eq %d" % pid], output)
		if code != OK:
			return true
		for chunk in output:
			if String(chunk).contains("\"%d\"" % pid):
				return true
		return false
	if DirAccess.dir_exists_absolute("/proc"):
		return DirAccess.dir_exists_absolute("/proc/%d" % pid)
	return OS.execute("kill", ["-0", str(pid)]) == 0
