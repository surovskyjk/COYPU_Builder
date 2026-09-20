class_name IpcProcessSupervisor
extends Node
## Spawns and supervises the backend subprocess (ADR 0003): launches it via [method
## OS.execute_with_pipe], reads the `{"port": N}` line it prints on startup, and reports if the
## process exits unexpectedly so the caller can decide whether to restart it.

signal backend_ready(url: String)
signal backend_failed(reason: String)
signal backend_crashed

@export var host: String = "127.0.0.1"
@export var port_wait_timeout_sec: float = 10.0

## How long to wait after SIGTERM before escalating to SIGKILL on POSIX (see [method _kill_process_tree]).
const _POSIX_KILL_GRACE_MSEC := 200

var _pid: int = -1
var _stdio: FileAccess
var _waiting_for_port := false
var _wait_deadline_msec: int = 0
var _confirmed_running := false


## `executable` + `args` are the process to launch (e.g. `uv`, ["run", "--project", "<backend dir>",
## "coypu-builder-backend", "serve"]); `--port 0 --token <token>` is appended automatically.
func start(executable: String, args: PackedStringArray, token: String) -> void:
	stop()
	var full_args := args.duplicate()
	full_args.append_array(["--port", "0", "--token", token])
	var spawned := OS.execute_with_pipe(executable, full_args)
	if spawned.is_empty():
		backend_failed.emit("failed to spawn '%s'" % executable)
		return
	_stdio = spawned["stdio"]
	_pid = spawned["pid"]
	_waiting_for_port = true
	_confirmed_running = false
	_wait_deadline_msec = Time.get_ticks_msec() + int(port_wait_timeout_sec * 1000)


func stop() -> void:
	if is_running():
		_kill_process_tree(_pid)
	_pid = -1
	_stdio = null
	_waiting_for_port = false
	_confirmed_running = false


## `uv run coypu-builder-backend ...` is itself a process tree (uv → the console-script shim → the
## Python interpreter) on Windows, since Windows has no fork/exec image replacement; [method OS.kill]
## only terminates the one PID we were handed, orphaning its children, so ask the OS to kill the tree.
## POSIX has the same problem — `uv run` forks the interpreter as a child of itself rather than exec'ing
## over it — but no `taskkill /T` equivalent, and Godot never places the spawned process in its own
## process group, so `kill -- -<pgid>` would hit this process too. Instead: find uv's direct children
## before signalling anything (once uv exits they are reparented to init and `pgrep -P <uv_pid>` no
## longer finds them), TERM the whole set, give it a grace period, then KILL whatever is still alive.
func _kill_process_tree(pid: int) -> void:
	if OS.get_name() == "Windows":
		OS.execute("taskkill", ["/F", "/T", "/PID", str(pid)])
		return
	var pids: Array[int] = [pid]
	pids.append_array(_posix_child_pids(pid))
	for p: int in pids:
		OS.execute("kill", ["-TERM", str(p)])
	OS.delay_msec(_POSIX_KILL_GRACE_MSEC)
	for p: int in pids:
		if _is_posix_pid_alive(p):
			OS.execute("kill", ["-KILL", str(p)])


## Direct children of `pid` (one level — the shape ADR 0003 documents for `uv run` on POSIX), via
## `pgrep -P`. Best-effort: an empty result just means there was nothing to find (e.g. `pgrep` missing,
## or `uv` exec'd over itself instead of forking), not an error.
func _posix_child_pids(pid: int) -> Array[int]:
	var output: Array = []
	OS.execute("pgrep", ["-P", str(pid)], output)
	var result: Array[int] = []
	for chunk in output:
		for line: String in String(chunk).split("\n"):
			line = line.strip_edges()
			if line.is_valid_int():
				result.append(int(line))
	return result


func is_running() -> bool:
	if _pid == -1:
		return false
	if OS.get_name() == "Windows":
		return OS.is_process_running(_pid)
	return _is_posix_pid_alive(_pid)


## [method OS.is_process_running] / [method OS.kill] log an engine-level "does not exist or is not a
## child of the calling process" ERROR for a PID that is not (or is no longer) a direct child of Godot —
## which happens routinely here once a POSIX kill above has reaped it, or for a grandchild we discovered
## via [method _posix_child_pids]. Checking for `/proc/<pid>` directly never errors that way. Note this
## is a plain existence check, not a `stat`-field read: [FileAccess] sizes its buffer from the file's
## reported length, which procfs pseudo-files report as 0, so `get_as_text()` on `/proc/<pid>/stat` reads
## back empty every time regardless of the real content — that cannot be used here.
func _is_posix_pid_alive(pid: int) -> bool:
	if DirAccess.dir_exists_absolute("/proc"):
		return DirAccess.dir_exists_absolute("/proc/%d" % pid)
	return _is_posix_pid_alive_via_signal(pid)


## Fallback for POSIX systems without `/proc` (e.g. macOS, not a current CI target). `kill -0` only
## checks whether the PID exists and is signalable — unlike Godot's own process tracking, it does not
## require the PID to be a direct child, so it cannot produce the "not a child" error either.
func _is_posix_pid_alive_via_signal(pid: int) -> bool:
	return OS.execute("kill", ["-0", str(pid)]) == 0


## Test-only (T-103 `test_connection_lifecycle.gd`): the spawned process id, or -1 if none is running.
func pid() -> int:
	return _pid if is_running() else -1


## Test-only (T-103 `test_connection_lifecycle.gd`): kills the process the same way an external crash
## would, without clearing bookkeeping first — unlike [method stop], so the next [method _process] poll
## still finds it dead and emits [signal backend_crashed] exactly like a real crash.
func debug_kill() -> void:
	if is_running():
		_kill_process_tree(_pid)


func _process(_delta: float) -> void:
	if _waiting_for_port:
		_poll_for_port()
	elif _confirmed_running and not is_running():
		_confirmed_running = false
		backend_crashed.emit()


func _poll_for_port() -> void:
	if not is_running():
		_waiting_for_port = false
		backend_failed.emit("backend process exited before printing its port")
		return
	if Time.get_ticks_msec() > _wait_deadline_msec:
		_waiting_for_port = false
		stop()
		backend_failed.emit("timed out waiting for the backend to report its port")
		return
	var line := _stdio.get_line()
	if line.is_empty():
		return
	var parsed = JSON.parse_string(line)
	if typeof(parsed) != TYPE_DICTIONARY or not parsed.has("port"):
		return
	_waiting_for_port = false
	_confirmed_running = true
	backend_ready.emit("ws://%s:%d" % [host, int(parsed["port"])])
