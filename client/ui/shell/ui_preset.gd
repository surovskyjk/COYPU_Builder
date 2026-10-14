class_name UiPreset
extends Resource
## An interface style (T-140): which theme, whether panels are docked or floating, and the numbers that
## differ between the two. Presets are data; [UiShell] is the only consumer, and switching between them
## reparents the same [DockPanel] instances rather than rebuilding anything.
##
## Two built-in presets exist: [code]engineering[/code] (the default: light, ribbon, docked panels) and
## [code]sandbox[/code] (the 3D view fills the window and the same panels float over it as translucent cards).

enum PanelMode { DOCKED, FLOATING }

const ENGINEERING := &"engineering"
const SANDBOX := &"sandbox"
const DEFAULT_ID := ENGINEERING

const OPACITY_MIN := 0.4
const OPACITY_MAX := 1.0

const _THEME_PATHS := {
	ENGINEERING: "res://ui/theme/engineering_theme.tres",
	SANDBOX: "res://ui/theme/sandbox_theme.tres",
}

@export var id: StringName
@export var display_name := ""
@export var theme: Theme
@export var panel_mode: PanelMode = PanelMode.DOCKED
## True: the 3D view fills the window under floating panels.
@export var viewport_full_bleed := false
## 0.4 .. 1.0; used when [member panel_mode] is FLOATING.
@export_range(0.4, 1.0) var panel_opacity := 1.0
@export var corner_radius_px := 4
@export var icon_size_px := 18


## A fresh instance of the built-in preset `id`; an unknown id gives the default one.
static func builtin(preset_id: StringName) -> UiPreset:
	var preset := UiPreset.new()
	if preset_id == SANDBOX:
		preset.id = SANDBOX
		preset.display_name = "Sandbox"
		preset.panel_mode = PanelMode.FLOATING
		preset.viewport_full_bleed = true
		preset.panel_opacity = 0.72
		preset.corner_radius_px = 12
		preset.icon_size_px = 20
	else:
		preset.id = ENGINEERING
		preset.display_name = "Engineering"
		preset.panel_mode = PanelMode.DOCKED
		preset.viewport_full_bleed = false
		preset.panel_opacity = 1.0
		preset.corner_radius_px = 4
		preset.icon_size_px = 18
	preset.theme = load(_THEME_PATHS[preset.id]) as Theme
	return preset


static func ids() -> Array[StringName]:
	return [ENGINEERING, SANDBOX]


static func is_known(preset_id: StringName) -> bool:
	return _THEME_PATHS.has(preset_id)


static func clamp_opacity(value: float) -> float:
	if is_nan(value):
		return OPACITY_MAX
	return clampf(value, OPACITY_MIN, OPACITY_MAX)
