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
func _kill_process_tree(pid: int) -> void:
	if OS.get_name() == "Windows":
		OS.execute("taskkill", ["/F", "/T", "/PID", str(pid)])
	else:
		OS.kill(pid)


func is_running() -> bool:
	return _pid != -1 and OS.is_process_running(_pid)


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
