extends Node3D
## Deliberately temporary bootstrap scene (T-103): a free-look camera and a status overlay showing
## backend/session state. M2 replaces the camera (T-124); M4 replaces the overlay (T-140). Do not extend
## this file's scope — see "Out of scope" in docs/tasks/task_103_client_app_shell.md.

const _LOOK_SPEED := 0.005
const _MOVE_SPEED := 12.0
const _MOVE_SPEED_FAST := 40.0

@onready var _status_label: Label = %StatusLabel
@onready var _camera: Camera3D = %FreeLookCamera

var _project_path := ""
var _look_active := false
var _yaw := 0.0
var _pitch := 0.0


func _ready() -> void:
	var args := CliArgs.from_cmdline()
	_project_path = args.get("project", "")

	var euler := _camera.rotation
	_yaw = euler.y
	_pitch = euler.x

	Backend.state_changed.connect(_on_backend_state_changed)
	EventBus.project_changed.connect(_refresh_status)
	EventBus.alignments_changed.connect(_refresh_status)

	Backend.start()
	_refresh_status()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST:
		Backend.shutdown()
		get_tree().quit()


func _on_backend_state_changed(state: Backend.State) -> void:
	_refresh_status()
	if state == Backend.State.READY:
		_bootstrap_session()


## Only feature work this bootstrap scene is allowed: when `--project` names a `.xml` file, open a new
## project and import it so the overlay has real alignment summaries to show; otherwise just mirror
## whatever project the backend already has open (there may be none yet).
func _bootstrap_session() -> void:
	if _project_path.to_lower().ends_with(".xml"):
		await Backend.request("project.new", {})
		var imported := await Backend.request("import.landxml", {"path": _project_path})
		if imported.type != "res":
			push_error("main: import.landxml failed: %s" % str(imported.error))
	var got := await Backend.request("project.get", {})
	if got.type == "res":
		Session.set_project_info(got.result)
	_refresh_status()


func _refresh_status() -> void:
	var lines := PackedStringArray()
	lines.append("backend: %s" % Backend.State.keys()[Backend.state()])
	lines.append("server: %s (protocol %d)" % [Backend.server_version(), Backend.protocol_version()])

	var project_id := Session.project_id()
	lines.append("project: %s" % (project_id if not project_id.is_empty() else "(none)"))
	var crs := Session.crs()
	lines.append("crs: %s" % (crs if not crs.is_empty() else "(none)"))

	var alignments := Session.alignments()
	lines.append("alignments: %d" % alignments.size())
	for alignment: Dictionary in alignments:
		lines.append(
			" - %s [%s] %.1f m" % [
				alignment.get("name", "?"), alignment.get("mode", "?"), alignment.get("length", 0.0)
			]
		)

	_status_label.text = "\n".join(lines)


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_RIGHT:
		_look_active = event.pressed
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED if _look_active else Input.MOUSE_MODE_VISIBLE
	elif event is InputEventMouseMotion and _look_active:
		_yaw -= event.relative.x * _LOOK_SPEED
		_pitch = clampf(_pitch - event.relative.y * _LOOK_SPEED, -1.5, 1.5)
		_camera.rotation = Vector3(_pitch, _yaw, 0.0)


func _process(delta: float) -> void:
	if not _look_active:
		return
	var input_dir := Vector3.ZERO
	if Input.is_key_pressed(KEY_W):
		input_dir.z -= 1.0
	if Input.is_key_pressed(KEY_S):
		input_dir.z += 1.0
	if Input.is_key_pressed(KEY_A):
		input_dir.x -= 1.0
	if Input.is_key_pressed(KEY_D):
		input_dir.x += 1.0
	if Input.is_key_pressed(KEY_Q):
		input_dir.y -= 1.0
	if Input.is_key_pressed(KEY_E):
		input_dir.y += 1.0
	if input_dir == Vector3.ZERO:
		return
	var speed := _MOVE_SPEED_FAST if Input.is_key_pressed(KEY_SHIFT) else _MOVE_SPEED
	_camera.translate(input_dir.normalized() * speed * delta)
