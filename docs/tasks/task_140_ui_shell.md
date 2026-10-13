# T-140 — UI shell: ribbon, docks, two style presets, envelope export

**Milestone:** P1.M4, pulled forward into the M3 window · **Depends on:** T-103, T-124, T-125, T-126,
T-138 · **Blocks:** T-136, T-141, T-142, T-143, T-144

## Context

The client still shows T-103's bootstrap status overlay. This task gives it a product shell. The user chose
the style on 2026-10-13 from three mock-ups:

- **Default, "Engineering"** (mock-up C): a light theme, a ribbon toolbar along the top, and docked panels —
  layers on the left, the inspector on the right, the timeline at the bottom — around the 3D view. It
  should feel familiar to anyone who uses Civil 3D, OpenRoads or QGIS.
- **Optional preset, "Sandbox"** (mock-up B), chosen in Settings: the 3D view fills the window, and the same
  panels float over it as rounded translucent cards, with the timeline as a floating pill. The translucency
  is adjustable.

Engineering is the core; Sandbox is a preset over the same components. **Every panel is implemented once
and can be placed docked or floating.** There must be no second copy of any panel for the other style.

The shell provides the frame and the empty slots. The panels' contents belong to later tasks: layers
(T-142), the inspector (T-143), the timeline (T-141), the import dialog (T-136) and view modes (T-144). The
one end-to-end feature this task ships is **Export corridor envelope…**, over T-138's `alignment.envelope`.

## Preconditions

- `client/scene/main.gd` / `main.tscn`:
  - T-103's bootstrap scene, extended by T-121 to T-125;
  - accessors `camera_manager()`, `timeline()`, `playback()`, `corridor()`, `status_overlay()`, and
    `scene_ready`;
  - temporary keyboard playback bindings (Space, Home, End, `[`, `]`).
- `client/ui/{top_bar,bottom_dock,layers_panel,inspector_panel,timeline_bar,dialogs,widgets,theme}/` are
  empty placeholders (`.gitkeep`).
- `CameraManager.set_mode(Mode)`, and `EventBus.camera_changed`.
- T-138: `alignment.envelope {path, buffer_m, alignment_ids?, station_from?, station_to?, epsg?, cap,
  overwrite}`. The path must be absolute and outside the repository; an existing file is refused unless
  `overwrite` is true (ADR 0010).
- T-125: `tools/capture.ps1`, scenarios and the capture driver. The driver hides `status_overlay()` during
  shots.

Read before starting:
- `docs/adr/0002-client-language.md`;
- `docs/adr/0010-extensibility-api-plugins-mcp.md` (the file-write rule);
- `docs/data-contracts/corridor-envelope.md`;
- `client/README.md`;
- `client/scene/main.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/ui/shell/ui_shell.gd`, `ui_shell.tscn` | new — the shell: ribbon, dock slots, floating layer, status bar, viewport area |
| `client/ui/shell/dock_panel.gd` | new — one panel that can live docked or floating |
| `client/ui/shell/ui_preset.gd` | new — preset resource type and the two built-in presets |
| `client/ui/theme/engineering_theme.tres`, `sandbox_theme.tres` | new — Godot `Theme` resources |
| `client/ui/top_bar/ribbon.gd` | new — ribbon groups and commands |
| `client/ui/dialogs/settings_dialog.gd` | new — interface style and translucency |
| `client/ui/dialogs/envelope_export_dialog.gd` | new — export corridor envelope |
| `client/core/user_settings.gd` | new — per-user client preferences (`user://settings.cfg`) |
| `client/scene/main.gd`, `main.tscn` | host the shell; the bootstrap overlay's content moves into the status bar |
| `client/project.godot` | register any new autoload or input actions |
| `client/tools/capture/scenario.gd`, `capture_driver.gd`, `scenarios/ui_shell.json` | per-shot `ui` (`"hidden"` / `"visible"`) and `preset` keys; a new scenario |
| `client/assets/icons/` and `client/assets/fonts/`, each with a `LICENSE` file | only if needed (see Contract) |
| `client/tests/unit/test_ui_preset.gd`, `test_user_settings.gd`, `test_envelope_export.gd` | new |
| `client/README.md` | describe the shell, the presets and the settings file |

## Contract

### Presets

```gdscript
class_name UiPreset extends Resource
enum PanelMode { DOCKED, FLOATING }
@export var id: StringName            # &"engineering" | &"sandbox"
@export var theme: Theme
@export var panel_mode: PanelMode
@export var viewport_full_bleed: bool # true: the 3D view fills the window under floating panels
@export var panel_opacity: float      # 0.4 .. 1.0; used when FLOATING
@export var corner_radius_px: int
@export var icon_size_px: int
static func builtin(id: StringName) -> UiPreset
```

- **`engineering`** (the default):
  - DOCKED, not full-bleed, opacity 1.0;
  - light theme, small radius, 18 px icons;
  - ribbon with icon and label.
- **`sandbox`**:
  - FLOATING, full-bleed, opacity 0.72 by default;
  - dark translucent cards, 12 px radius, 20 px icons;
  - ribbon reduced to one floating top bar of icon and short label;
  - the timeline is a floating pill.
- Switching presets at runtime reparents the same `DockPanel` instances between the dock slots and the
  floating layer. **There is no scene reload, and no panel is rebuilt.** Panel state survives the switch:
  scroll position, a selected row, a typed value.

### User settings

```gdscript
class_name UserSettings extends RefCounted   # or an autoload; one concern
func preset_id() -> StringName               # default &"engineering"
func set_preset_id(id: StringName) -> void
func panel_opacity() -> float                # default from the preset
func set_panel_opacity(x: float) -> void
func save() -> void                          # ConfigFile at user://settings.cfg
```

- Settings are per-user client preferences, never project data.
- A missing or corrupt file gives the defaults and never an error dialog.
- Opacity is clamped to 0.4 .. 1.0.

### Ribbon (Engineering) and top bar (Sandbox)

Both are built from one command list.

| Group | Commands in this task | Slots for later tasks |
|---|---|---|
| Project | — | Open, Save (T-145) |
| Import | — | Import terrain or imagery (T-136) |
| Corridor | **Export corridor envelope…** | — |
| View | Orbit / Wayside / Cab (`CameraManager.set_mode`), shown as a selected state | View modes (T-144) |
| App | **Settings…** | — |

- **Later commands do not appear yet.** Avoid disabled buttons: a slot is added by the task that implements
  it.
- Every command has a keyboard shortcut and a tooltip.
- The camera buttons follow `EventBus.camera_changed`.

### Docks and status bar

- **Slots:** left (layers, T-142), right (inspector, T-143), bottom (timeline, T-141).
- **Placeholder panels:** each slot holds a `DockPanel` with its title and a one-line placeholder. The
  placeholder must not say "coming soon"; it states what the panel will show.
- **Status bar:** shows the backend state, the project, the CRS and the alignment summary that the T-103
  overlay showed.
- **Overlay removed:** the T-103 overlay is removed. `status_overlay()` is kept for the capture driver and
  returns the status bar.

### Export corridor envelope…

1. A dialog asks for:
   - the buffer half-width (default 250 m, 1–5000 m);
   - the format (GeoJSON or Shapefile);
   - an optional station range;
   - the output CRS (default per T-138: 4326 for GeoJSON, the project CRS for Shapefile);
   - a save path, through Godot's native `FileDialog`.
2. **If the target exists, the dialog asks before sending `overwrite: true`.** It never sends it silently.
3. It sends `alignment.envelope` and shows the result in the status bar: the files written, the area in
   km² and the vertex count.
4. Backend errors are shown as one plain sentence with the next step. A path inside the repository says it
   must be outside the project folder.

### Fonts and icons

- **Czech diacritics must render** in every label: Č, Ú, Ž, ř, ů, and so on. Verify with the capture.
- If Godot's default font lacks any of them, bundle Noto Sans (OFL) in `client/assets/fonts/`.
- Icons: use an MIT or OFL icon set (for example Tabler Icons as SVG), only the icons used, with the
  licence file beside them.
- No other third-party assets.

### Capture

- A shot gains `ui: "hidden" | "visible"` (default `"hidden"`, which keeps T-125's behaviour) and
  `preset: "engineering" | "sandbox"`.
- The new `ui_shell.json` scenario captures:
  - the Engineering shell with the corridor and train;
  - the Sandbox preset over the same view, at default opacity;
  - Sandbox at 0.4 opacity;
  - the envelope export dialog.
- `kralupy_m2` must still pass unchanged.

## Invariants

- No logic in `.tscn` files; themes and presets are resources.
- No RPC from `_process`. The envelope request runs from the dialog's coroutine.
- Nothing is written into the repository. The capture writes to `captures/`, and user settings to `user://`.

## Acceptance criteria

1. **Default layout:** a cold start shows the Engineering shell, with the ribbon, three dock slots, the
   status bar and the 3D view. The corridor and train render as before.
2. **Switching styles:**
   - switching to Sandbox in Settings reparents the same panel instances to the floating layer, with no
     reload. A test asserts the instance ids before and after, and that a panel's state survives;
   - switching back restores the docks;
   - the choice and the opacity survive a restart.
3. **Opacity:** the slider changes panel opacity live within 0.4–1.0, and the 3D view stays interactive
   under the floating panels except where a panel is.
4. **Camera buttons:** they switch the camera and reflect the current mode, including a change made by
   keyboard.
5. **Envelope export:**
   - over a live backend, it writes a GeoJSON to a temporary folder outside the repository and the status
     bar shows the result;
   - an existing file triggers the overwrite question and is replaced only after confirmation;
   - an in-repository path shows the plain-language refusal.
6. **Capture:** `ui_shell` captures four shots in which the panels, labels and Czech diacritics are legible.
   Engineering and Sandbox are clearly distinct, and `kralupy_m2` still passes.
7. **Frame time:** in Sandbox at 0.4 opacity, the perf block shows no frame-time regression greater than
   0.5 ms at p95 against the hidden-UI run.
8. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened.

## Out of scope

- Layers panel content — T-142. Inspector content — T-143. Timeline UI — T-141; the temporary keyboard
  bindings stay until then.
- The import dialog — T-136. View modes and render quality — T-144. Save and open — T-145. MCP — T-146.
- A dark Engineering variant, user-defined presets and dock dragging or resizing — not planned for Phase 1.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario ui_shell
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario kralupy_m2
```

```bash
cd backend
uv run pytest -q
```

Open every PNG and confirm it shows what its name claims.

## Report back

State:
- the panel reparenting approach and how state survives it;
- where user settings live;
- which font and icon set you used, with their licences;
- the `ui_shell` shots, described;
- the perf comparison;
- anything about the two presets that should be tuned before T-141 to T-143 fill the panels.
