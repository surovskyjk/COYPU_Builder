class_name Ribbon
extends PanelContainer
## The command strip of the shell (T-140): a ribbon in the Engineering style, one floating top bar in the
## Sandbox style. Both are this one node built from one command list ([method default_commands]); only
## [method set_compact] differs (icon beside a short label, no group captions).
##
## A group appears only once a task has put a command in it: Project (open/save, T-145) and Import (T-136)
## have none yet, so they are absent rather than shown with disabled buttons. Adding a command is adding one
## dictionary to the list.
##
## Every command carries a tooltip and a keyboard shortcut taken from an input action in `project.godot`
## (rebindable there); the tooltip prints the key. The ribbon owns no behaviour: pressing a button emits
## [signal command_invoked] and [UiShell] routes it. The camera buttons are a toggle group that follows
## [method set_camera_mode] (the shell calls it from [signal EventBus.camera_changed], so a key press shows
## up here too).

signal command_invoked(id: StringName)

const ICON_DIR := "res://assets/icons/"

const GROUP_ORDER: Array[StringName] = [&"project", &"import", &"corridor", &"view", &"app"]
const GROUP_TITLES := {
	&"project": "Project", &"import": "Import", &"corridor": "Corridor", &"view": "View", &"app": "Application",
}

const CMD_EXPORT_ENVELOPE := &"corridor.export_envelope"
const CMD_CAMERA_ORBIT := &"view.camera_orbit"
const CMD_CAMERA_WAYSIDE := &"view.camera_wayside"
const CMD_CAMERA_CAB := &"view.camera_cab"
const CMD_SETTINGS := &"app.settings"

## camera command id -> CameraManager.Mode value
const CAMERA_COMMANDS := {
	CMD_CAMERA_ORBIT: CameraManager.Mode.ORBIT,
	CMD_CAMERA_WAYSIDE: CameraManager.Mode.WAYSIDE,
	CMD_CAMERA_CAB: CameraManager.Mode.CAB,
}

var _commands: Array[Dictionary] = []
var _buttons := {}                  # command id -> Button
var _captions: Array[Label] = []
var _separators: Array[Control] = []
var _camera_group := ButtonGroup.new()
var _row: HBoxContainer
var _compact := false
var _icon_px := 18


## The commands this task ships. `action` is the input action that gives the shortcut; `toggle` marks a
## state button. Later tasks append their own (Open/Save in `project`, terrain and imagery in `import`, ...).
static func default_commands() -> Array[Dictionary]:
	return [
		{
			"id": CMD_EXPORT_ENVELOPE, "group": &"corridor", "label": "Export corridor envelope…",
			"short": "Envelope", "icon": "export_envelope", "action": "shell_export_envelope",
			"tip": "Write the corridor buffer as GeoJSON or Shapefile, to find terrain and imagery sheets.",
		},
		{
			"id": CMD_CAMERA_ORBIT, "group": &"view", "label": "Orbit", "short": "Orbit", "icon": "camera_orbit",
			"action": "camera_mode_orbit", "toggle": true,
			"tip": "Orbit camera: drag to rotate, wheel to zoom, middle-drag to pan.",
		},
		{
			"id": CMD_CAMERA_WAYSIDE, "group": &"view", "label": "Wayside", "short": "Wayside",
			"icon": "camera_wayside", "action": "camera_mode_wayside", "toggle": true,
			"tip": "Wayside camera: a fixed observer beside the track that watches the train pass.",
		},
		{
			"id": CMD_CAMERA_CAB, "group": &"view", "label": "Cab", "short": "Cab", "icon": "camera_cab",
			"action": "camera_mode_cab", "toggle": true,
			"tip": "Cab camera: the driver's view from the lead car.",
		},
		{
			"id": CMD_SETTINGS, "group": &"app", "label": "Settings…", "short": "Settings", "icon": "settings",
			"action": "shell_open_settings",
			"tip": "Interface style and panel translucency.",
		},
	]


static func load_icon(icon_name: String) -> Texture2D:
	var path := ICON_DIR + icon_name + ".svg"
	if not ResourceLoader.exists(path):
		return null
	return load(path) as Texture2D


## "Ctrl+E" style text of the first key bound to `action`, or "" when it has none.
static func shortcut_text(action: String) -> String:
	if not InputMap.has_action(action):
		return ""
	for event in InputMap.action_get_events(action):
		if event is InputEventKey:
			var key := event as InputEventKey
			return key.as_text_keycode() if key.keycode != KEY_NONE else key.as_text_physical_keycode()
	return ""


func _init() -> void:
	theme_type_variation = &"RibbonBar"
	mouse_filter = Control.MOUSE_FILTER_STOP


func _ready() -> void:
	if _commands.is_empty():
		build(default_commands())


## (Re)builds the groups from `commands`. Groups with no command are not created.
func build(commands: Array[Dictionary]) -> void:
	for child in get_children():
		child.queue_free()
	_buttons.clear()
	_captions.clear()
	_separators.clear()
	_commands = commands

	_row = HBoxContainer.new()
	_row.name = "Groups"
	_row.add_theme_constant_override(&"separation", 6)
	add_child(_row)

	var first := true
	for group in GROUP_ORDER:
		var members: Array[Dictionary] = []
		for command in commands:
			if command["group"] == group:
				members.append(command)
		if members.is_empty():
			continue
		if not first:
			var divider := VSeparator.new()
			_row.add_child(divider)
			_separators.append(divider)
		first = false
		_row.add_child(_build_group(group, members))
	_apply_compact()


func button(id: StringName) -> Button:
	return _buttons.get(id)


func command_ids() -> Array[StringName]:
	var ids: Array[StringName] = []
	for command in _commands:
		ids.append(command["id"])
	return ids


## The `Mode` value of the camera button that is pressed, or -1.
func pressed_camera_mode() -> int:
	for id: StringName in CAMERA_COMMANDS:
		if _buttons.has(id) and (_buttons[id] as Button).button_pressed:
			return CAMERA_COMMANDS[id]
	return -1


## Shows `mode` (a [enum CameraManager.Mode] value) as the pressed camera button without emitting a command.
func set_camera_mode(mode: int) -> void:
	for id: StringName in CAMERA_COMMANDS:
		if _buttons.has(id):
			(_buttons[id] as Button).set_pressed_no_signal(CAMERA_COMMANDS[id] == mode)


## Compact: one row of icon and short label, no group captions (the Sandbox top bar).
func set_compact(compact: bool) -> void:
	_compact = compact
	_apply_compact()


func set_icon_size(px: int) -> void:
	_icon_px = px
	for id: StringName in _buttons:
		(_buttons[id] as Button).add_theme_constant_override(&"icon_max_width", px)


func is_compact() -> bool:
	return _compact


func _build_group(group: StringName, members: Array[Dictionary]) -> Control:
	var column := VBoxContainer.new()
	column.name = String(group).capitalize() + "Group"
	column.add_theme_constant_override(&"separation", 0)

	var row := HBoxContainer.new()
	row.name = "Commands"
	row.add_theme_constant_override(&"separation", 2)
	row.size_flags_vertical = Control.SIZE_EXPAND_FILL
	column.add_child(row)

	for command in members:
		var command_button := _build_button(command)
		row.add_child(command_button)
		_buttons[command["id"]] = command_button

	var caption := Label.new()
	caption.name = "Caption"
	caption.text = GROUP_TITLES.get(group, String(group).capitalize())
	caption.theme_type_variation = &"GroupCaption"
	caption.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	caption.mouse_filter = Control.MOUSE_FILTER_IGNORE
	column.add_child(caption)
	_captions.append(caption)
	return column


func _build_button(command: Dictionary) -> Button:
	var command_button := Button.new()
	command_button.name = String(command["id"]).replace(".", "_")
	command_button.theme_type_variation = &"RibbonButton"
	command_button.icon = load_icon(command.get("icon", ""))
	command_button.focus_mode = Control.FOCUS_NONE
	command_button.add_theme_constant_override(&"icon_max_width", _icon_px)
	command_button.set_meta(&"command", command["id"])

	var action: String = command.get("action", "")
	var key_text := shortcut_text(action)
	var tip: String = command.get("tip", "")
	var heading: String = command["label"]
	command_button.tooltip_text = "%s%s\n%s" % [heading, "  (%s)" % key_text if not key_text.is_empty() else "", tip]
	if InputMap.has_action(action):
		var shortcut := Shortcut.new()
		shortcut.events = InputMap.action_get_events(action)
		command_button.shortcut = shortcut
		command_button.shortcut_in_tooltip = false

	if command.get("toggle", false):
		command_button.toggle_mode = true
		command_button.button_group = _camera_group
		command_button.toggled.connect(_on_toggled.bind(command["id"]))
	else:
		command_button.pressed.connect(_on_pressed.bind(command["id"]))
	return command_button


func _on_pressed(id: StringName) -> void:
	command_invoked.emit(id)


func _on_toggled(is_pressed: bool, id: StringName) -> void:
	if is_pressed:
		command_invoked.emit(id)


func _apply_compact() -> void:
	for caption in _captions:
		caption.visible = not _compact
	for divider in _separators:
		divider.visible = true
	for command in _commands:
		var command_button: Button = _buttons.get(command["id"])
		if command_button == null:
			continue
		if _compact:
			command_button.text = command["short"]
			command_button.icon_alignment = HORIZONTAL_ALIGNMENT_LEFT
			command_button.vertical_icon_alignment = VERTICAL_ALIGNMENT_CENTER
		else:
			command_button.text = command["label"]
			command_button.icon_alignment = HORIZONTAL_ALIGNMENT_CENTER
			command_button.vertical_icon_alignment = VERTICAL_ALIGNMENT_TOP
