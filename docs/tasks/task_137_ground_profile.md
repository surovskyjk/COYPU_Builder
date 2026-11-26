# T-137 — Ground sampling: longitudinal profile, cut/fill, and the profile panel

**Milestone:** P1.M3 (closes M3) · **Depends on:** T-132, T-133, T-140; T-135 for the 3D cursor ·
**Blocks:** T-144 (the cut/fill diagnostics ramp)

## Context

M3's exit criteria include "the corridor drapes correctly against the DEM" and "a cut/fill strip reads out
along stations". This task samples the terrain analysis surface (T-132's `TerrainStack`, never display
tiles) along an alignment and computes:

- the ground line under the centreline, beside the rail profile;
- cut and fill per station, from a simple, explicit formation template;
- volumes between stations.

The client shows them in a **Profile** panel next to the timeline, with a cursor that follows playback.

Two Kralupy facts shape the acceptance (ROADMAP F32, closed by the user's answer):

- The fixture's vertical profile is **idealised.** COYPU Feeder had no DMR 5G when it was made. So the
  numbers show large nominal cut (terrain up to +17 m over the rail near km 13) by design. That is test
  data, not a design error, and the panel must not present it as one.
- **Stations 16.66–16.81 km are the Vltava bridge.** DMR 5G has no ground points on water, so these stations
  are "no ground" and must not count as cut or fill.

## How this task runs

Three parts, each its own worker order:

| Part | What | Can start when |
|---|---|---|
| **A — backend** | `domain/earthworks.py`, `alignment.ground_profile`, the `profile_idealised` flag | T-136 Part A is committed |
| **B — client** | the Profile tab, the cursor, the readout, the captures; assess M3's exit criteria | Part A and T-135 Part C are committed |
| **C — surface models** | a source can be a surface model; earthworks skip it (F57); the readout says so | Part B is committed |

## Preconditions

- T-132:
  - `TerrainStack.heights(e, n)` (float64, NaN where not covered);
  - with T-133, `heights_and_source(e, n)` and the ordered source ids;
  - the off-loop handler pattern and write barriers (ADR 0003 addendum).
- `domain/lrs.py: frames(alignment, s)`: `origin`, `tangent`, `left`, `up`, `elevation` and `cant_mm` per
  station, in float64.
- T-140: `UiShell.panel(...)`, the bottom dock slot (Timeline placeholder), `DockPanel`, themes, and the
  timeline state through `main.timeline()` / `playback()`.
- T-135 (optional at build time): `TerrainField` for drawing the station cursor in 3D.

Read before starting:
- `docs/data-contracts/terrain-tiles.md`;
- `docs/data-contracts/coordinate-conventions.md`;
- `docs/adr/0003-ipc-protocol.md` (addendum);
- `domain/lrs.py`.

## Deliverables

| Path | Part | Action |
|---|---|---|
| `backend/src/coypu_builder/domain/earthworks.py` | A | new — profile sampling, the formation template, areas, volumes (pure numpy) |
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | A | `alignment.ground_profile` (experimental, read-only, off-loop) |
| `docs/protocol/ipc.md` | A | regenerate |
| `docs/data-contracts/earthworks.md` | A | new — the template, conventions and sign rules |
| `backend/tests/test_earthworks.py` | A | new — analytic cases |
| `client/ui/profile_panel/profile_panel.gd` | B | new — 2D profile view with a cursor and readout |
| `client/ui/shell/ui_shell.gd` | B | bottom slot becomes tabs: Timeline, Profile (`TabContainer` theme items in both themes) |
| `client/ui/theme/engineering_theme.tres`, `sandbox_theme.tres` | B | `TabContainer` styling |
| `client/tools/capture/scenarios/terrain_kralupy.json` | B | add a Profile-panel shot |
| `client/tests/unit/test_profile_panel.gd`, `client/tests/integration/test_ground_profile_live.gd` | B | new |
| `client/core/session.gd` | B | `fetch_ground_profile(alignment_id, spacing_m)`, cached per alignment and terrain revision |
| `backend/src/coypu_builder/server/session.py` | A | `AlignmentEntry.profile_idealised: bool = False` |

## Contract

### Template (Phase 1; parametric sections are Phase 2)

```python
@dataclass(frozen=True)
class FormationTemplate:
    formation_width_m: float = 6.0          # at formation level, centred on the track axis
    formation_depth_m: float = 0.8          # rail top to formation level
    cut_slope: float = 1.5                  # horizontal : vertical
    fill_slope: float = 1.5
    sample_spacing_m: float = 0.5           # across-track sampling for areas
    max_half_width_m: float = 60.0          # stop searching for a daylight point beyond this offset
```

- **Formation level** at station s is `elevation(s) − formation_depth_m`, the profile at the rail-top
  pivot. Cant is ignored for earthworks in Phase 1: the formation is level across. This is documented.
- **Cut area** is the area between the ground and the cut template above the formation.
- **Fill area** is the area between the fill template and the ground below the formation.
- **Daylight:** each side finds its daylight point where the slope meets the ground. A side that finds
  none within `max_half_width_m` is flagged `open_side` and stops at that offset.

### Computation (`domain/earthworks.py`, Part A)

```python
def section_offsets(template: FormationTemplate) -> np.ndarray:
    """Across-track offsets from -max_half_width_m to +max_half_width_m at sample_spacing_m, including 0 and
    +-formation_width_m / 2 exactly."""

def section_areas(offsets: np.ndarray, ground: np.ndarray, formation_h: float,
                  template: FormationTemplate) -> tuple[float, float, int]:
    """(cut_area, fill_area, flags) of one cross-section; `ground` holds heights at `offsets`, NaN = none."""

def ground_profile(alignment: Alignment, heights: Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]],
                   stations: np.ndarray, template: FormationTemplate) -> GroundProfile:
    """`heights(e, n)` is `TerrainStack.heights_and_source`. Returns a frozen `GroundProfile` with the
    per-station arrays of the wire blobs, the volumes and the no-ground spans."""

def end_area_volume(stations: np.ndarray, areas: np.ndarray, valid: np.ndarray) -> float: ...
def no_ground_spans(stations: np.ndarray, no_ground: np.ndarray) -> tuple[tuple[float, float], ...]: ...
```

- **The section line:**
  - the centre is the horizontal alignment point at s, and the rail height is the vertical profile's
    elevation at s. Read them from `alignment.horizontal` and `alignment.vertical` directly, not through
    `frames()`. `frames()` gives the same values with `pivot=RotationPivot.CENTERLINE`, and a test asserts
    the equality. But on any alignment with cant, `frames()` runs a per-station Python search (F46), which
    costs seconds on a 1 m profile, and earthworks ignore cant anyway;
  - the across direction is horizontal, `(-sin heading, cos heading)`, with positive offsets to the left;
  - cant is ignored.
- **Template per side:**
  - between `±W/2` the template is at formation level;
  - beyond `W/2`, if the ground at the formation edge is at or above formation level, the side is in cut:
    the template rises at `1 / cut_slope`. Otherwise it is in fill, and the template falls at
    `1 / fill_slope`;
  - the daylight point is where the side's template meets the ground, found by linear interpolation between
    samples. A side with none within `max_half_width_m` sets flag 2 and ends there;
  - the search for the daylight point starts after the formation edge sample. Ground exactly at formation
    level at the edge, rising beyond it, is a cut side that daylights further out, not a zero-width one.
- **Areas:**
  - integrate `max(0, ground − template)` (cut) and `max(0, template − ground)` (fill) piecewise linearly
    between samples, from daylight to daylight;
  - split every interval at a sign change of `ground − template` and at the formation edges, so a planar
    ground gives the exact area, to 1e-9 relative;
  - an interval with a NaN end is skipped and sets flag 4.
- **No ground:** NaN ground at offset 0 sets flag 1. Both areas are then 0, and `ground_h` is NaN.
- **Volumes:** average end area over consecutive stations that both lack flag 1, never across a flag-1
  station.
- **Spans:** maximal runs of consecutive flag-1 stations, as `(first station, last station)`.

### `alignment.ground_profile`

```python
class GroundProfileParams(msgspec.Struct, frozen=True):
    alignment_id: str
    spacing_m: float = 10.0                 # 1 m .. 100 m
    station_from: float | None = None
    station_to: float | None = None
    template: FormationTemplateDTO | None = None   # None = the defaults above
```

**Stations fall on round chainages:** `station_from`, then every multiple of `spacing_m` strictly between,
then `station_to`. Compute them as integer multiples, `k · spacing_m`, so there is no drift. A multiple
within 1e-6 m of either end is not repeated. A 10 m profile reads 12 700, 12 710, … whatever the alignment's
start station; only the last row may be closer than `spacing_m` to its neighbour.

**Work budget:** before computing anything, the request is refused with `E_BAD_PARAMS` when
`rows × samples per section > 12 000 000` (about 20 s of terrain queries) or `rows > 100 000`. The message
says what to change: a coarser `spacing_m`, a shorter station range, or a coarser `sample_spacing_m`. The
Kralupy line at 1 m with the default template is 4.4 M points, so it fits.

The result carries scalars and blobs, one row per station:

- **Blobs:** `station` `<f8`, `rail_h` `<f8`, `ground_h` `<f8` (NaN = no ground), `source` `<u1`
  (255 = none), `cut_area` `<f8`, `fill_area` `<f8`, `flags` `<u1`. Flag bits: 1 = no ground under the
  centreline, 2 = open side, 4 = partial ground across the section.
  - Flag 1 comes alone.
  - Flag 2 means "no daylight point in the ground that exists". It also appears where the ground ends
    before `max_half_width_m`, for example at a clip edge, together with flag 4. It does not mean steep
    terrain.
- **Echoes:**
  - `terrain_revision`, the revision of the terrain the profile was computed on, so the client caches it
    under the right key;
  - `template`, the effective `FormationTemplateDTO`, defaults included, so the client can draw the
    formation level `rail_h − formation_depth_m`.
- **Totals:** `cut_volume_m3` and `fill_volume_m3`, by average end area between consecutive valid stations,
  never across a no-ground station.
- **Spans:** `no_ground_spans`, a list of `[from, to]`: the first and the last flag-1 station of each run,
  so a single flag-1 station is `[s, s]`. The ground is unknown from the valid station before a span to the
  valid station after it, and volumes skip both of those intervals. The panel hatches from the previous
  valid station to the next one. For Kralupy at 10 m this is about `[16 660, 16 800]`; at 1 m the gap runs
  from about 16 659 to 16 802. That is the bridge.
- **Sources:** the ordered `source_ids`, their `source_names` in the same order (the dataset names, for the
  readout), and the sources' attributions.

The request runs off the event loop (ADR 0003), and is `mutates: false`.

### Profile panel (client)

- **Placement:** the bottom slot hosts a `TabContainer` with **Timeline** (the existing placeholder) and
  **Profile**, in both presets. In Sandbox the pill grows to fit the profile.
- **Drawing:** the panel draws:
  - the rail profile (line);
  - the ground line, with gaps where there's no ground;
  - cut shaded in one colour and fill in another, with a legend;
  - no-ground spans hatched and labelled "no ground data (e.g. water)".
- **Cursor:** it follows `PlaybackController.lead_station()`, and the readout shows: station, rail height,
  ground height, difference, cut and fill areas, and the source name.
- **Axes:** vertical exaggeration is selectable (1×, 5×, 10×, default 10×). The axes are labelled in metres
  and km.
- **Idealised-profile note:** a subtle note, "Vertical profile is idealised in this dataset", shows only for
  alignments whose metadata marks the profile as idealised. **Never key on a fixture name** (CLAUDE.md:
  never special-case a fixture). Part A adds `profile_idealised: bool = False` to the `import.landxml` and
  `import.coypu` params. It is stored on
  `AlignmentEntry` and returned in `AlignmentSummary`. The Kralupy capture scenario sets it in its import
  call.

### Clarifications (Part B, 2026-11-26, before the worker)

The contract above was written before T-135 and T-136 built the shell, the terrain field and the capture
driver as they stand. Where these items differ from the contract, they win.

1. **Deliverables, complete.**
   - New:
     - `client/ui/profile_panel/profile_panel.gd`: the panel, drawn in `_draw`, plus a pure static helper for
       the drawing maths so unit tests need no rendering;
     - `client/tests/unit/test_profile_panel.gd`;
     - `client/tests/integration/test_ground_profile_live.gd`;
     - `.uid` files for each.
   - Changed:
     - `client/ui/shell/ui_shell.gd`: the tabs and the slot height;
     - `client/core/session.gd`: `fetch_ground_profile` and its cache;
     - `client/scene/main.gd`: hand the playback to the panel, and pass `profile_idealised`;
     - `client/core/cli_args.gd`: the `--profile-idealised` flag;
     - `tools/capture.ps1`, `client/tools/capture/scenario.gd`, `client/tools/capture/capture_driver.gd`:
       the top-level `profile_idealised` key and the `bottom_tab` shot key;
     - `client/tests/unit/test_capture_scenario.gd` and `client/tests/unit/test_cli_args.gd`;
     - `client/tools/capture/scenarios/terrain_kralupy.json` and `terrain_synthetic.json`: the shots in item 8;
     - both theme `.tres` files, if `TabContainer` needs styling;
     - `client/README.md`: the Profile tab, in a short paragraph.
   - No backend change: Part A is done (`d80c852`). If the backend seems wrong, report it; do not change it.
2. **The tabs.**
   - The bottom `DockPanel` keeps its id `&"timeline"`, so `panel(&"timeline")`, the capture driver and the
     tests keep working. Its body hosts a `TabContainer` with **Timeline** (the existing placeholder text) and
     **Profile**.
   - The tab bar replaces the panel's title: no "Timeline" heading above the tabs. If `DockPanel` cannot hide
     its header, add the smallest method for it (`set_header_visible(false)`) and list it.
   - The selected tab is per session, not saved.
   - Add `UiShell.profile_panel() -> ProfilePanel` and `UiShell.select_bottom_tab(name: StringName)` for
     `&"timeline"` and `&"profile"`; the capture driver and the tests use them.
3. **The height.** Today the docked bottom slot is `DOCK_BOTTOM_H` = 150 px, and the Sandbox pill is
   `FLOAT_PILL_H` = 56 px. Neither leaves room for a profile.
   - With **Profile** selected, the bottom slot is `PROFILE_H` = 260 px in Engineering, and the pill grows to
     260 px in Sandbox. With **Timeline** selected, both keep today's heights.
   - The 3D view gives up the space, as it does for the other docks.
   - At 1600 × 900, the drawing area inside the Profile tab is at least 150 px tall in both presets. A test
     measures it.
4. **What the panel fetches.**
   - `Session.fetch_ground_profile(alignment_id: String, spacing_m: float = 10.0) -> Dictionary` is a
     coroutine. It returns the result fields plus the decoded blobs (`station`, `rail_h`, `ground_h` and the
     areas as `PackedFloat64Array`; `source` and `flags` as `PackedByteArray`), or `{}` after emitting
     `EventBus.backend_error` on failure.
   - It caches by `(alignment_id, spacing_m)`. The cache is cleared on `EventBus.project_changed`,
     `EventBus.alignments_changed` and `EventBus.sources_changed("terrain")`. The client does not track the
     terrain revision, so these events stand in for it; the echoed `terrain_revision` is kept in the result
     for the readout's tooltip.
   - The panel fetches the whole alignment once at 10 m, which is about 1,800 rows for Kralupy, and draws a
     window of it. It fetches only while the Profile tab is visible, and again when the cache is cleared.
     While a fetch runs, the panel shows "Loading the ground profile…". On `{}` it shows "The ground profile
     could not be computed" with the error's sentence, and a Retry button.
5. **Which alignment.**
   - The panel shows the alignment of the bound playback: `ProfilePanel.bind_playback(controller:
     PlaybackController)`. `main.gd` calls it from `_wire_playback`, and the panel takes the id from the
     controller's alignment table.
   - Without a playback, it shows the first entry of `Session.alignments()` with the cursor at its start
     station and no cursor line.
   - With no alignment, it shows "Import an alignment to see its ground profile".
   - With no terrain, so that every row has flag 1, the panel draws the rail and shows "No terrain covers
     this alignment — import terrain to see the ground". The hatching is not drawn for the whole line.
6. **The window and axes.**
   - **Width.** A selector offers "1 km", "5 km" and "Whole line", default 5 km. The window is centred on the
     cursor and clamped to the alignment's ends.
   - **Vertical exaggeration.** 1×, 5× or 10×, default 10× (the contract). The vertical range fits the rail,
     the ground and the formation level within the window, plus 10 % of margin.
   - **Axes.** The horizontal axis is in km, with 3 decimals at 1 km and fewer at wider windows. The vertical
     axis is in metres. Each axis has about 5 to 8 ticks, at round values.
   - **Drawing.** Draw only the rows inside the window, plus one either side. The panel redraws when the
     cursor moves by at least one pixel, or when the size, the window, the exaggeration or the data change.
     It does not redraw every frame.
7. **The cursor, the readout and the colours.**
   - **Cursor.** `controller.lead_station()` is read in `_process`, only while the tab is visible. The
     readout is interpolated linearly between the two rows around the cursor. A NaN ground or a flag-1 row
     reads "no ground data".
   - **Readout.** It shows the station ("km 13.000"), the rail height, the ground height, ground − rail (with
     its sign, "+17.36 m"), the cut area, the fill area and the source name. It sits in a fixed column at
     the right of the panel and does not cover the drawing.
   - **Colours.** Cut and fill take colours from the theme: two new theme colours, or existing ones. The
     legend reads "Cut", "Fill" and "No ground data (e.g. water)".
   - **Idealised note.** The note shows for an alignment with `profile_idealised` true in its
     `Session.alignments()` entry.
   - **3D.** No 3D station cursor in this task; the train is the 3D cursor.
8. **The capture.**
   - **Marking the profile idealised.** The project is opened by `main.gd` from `--project`, not by an import
     the scenario makes, so:
     - a top-level scenario key `"profile_idealised": true` makes `capture.ps1` pass `--profile-idealised`;
     - `cli_args.gd` parses the flag;
     - `main.gd` passes `{"profile_idealised": true}` to `Session.import_coypu`, which gains an optional
       params argument.
   - **The `bottom_tab` shot key** takes `"timeline"` or `"profile"`, and the driver calls
     `select_bottom_tab`.
   - **`terrain_kralupy.json`** gets `"profile_idealised": true` and two shots with `"ui": "visible"`,
     `"preset": "engineering"` and `"bottom_tab": "profile"`:
     - `kralupy_profile_km13`, at the `time_s` of `kralupy_cutting_km13`: large cut and the note;
     - `kralupy_profile_bridge`, at the `time_s` of `kralupy_cab_bridge`: the hatched span.
   - **`terrain_synthetic.json`** gets `synthetic_profile_engineering` and `synthetic_profile_sandbox` at
     `time_s` 873. They need no `COYPU_DATA` and are the shots a reviewer can always run.
   - **Running them.** The Kralupy run needs `COYPU_DATA`. Set it inline for that one command, from
     `CLAUDE.local.md`, and never write or print its value.
   - **Judging.** View every shot on the GPU with the Read tool and describe it.
   - **Performance.** The perf block runs before the shots with the Timeline tab selected, so it is not
     affected. Report its numbers against the previous Kralupy run in `captures/`, if one exists.
9. **The live test.** `test_ground_profile_live.gd` starts a real backend.
   - It opens the Kralupy `.coypu` fixture with `profile_idealised`, imports
     `client/tests/fixtures/terrain/synthetic_tin.xml` (EPSG:5514) and fetches the profile at 10 m.
   - Then it checks:
     - `station` is increasing;
     - some rows have ground and some rows have flag 1 (the synthetic surface covers only part of the line);
     - `cut_volume_m3 + fill_volume_m3 > 0`;
     - the panel, given the result, draws without errors;
     - the alignment entry has `profile_idealised == true`.
10. **Unit tests.** Use a fake profile with no backend, and include at least:
    - `test_tabs_switch_and_profile_grows_the_bottom_slot` (both presets);
    - `test_window_is_centred_on_the_cursor_and_clamped`;
    - `test_readout_interpolates_between_rows`;
    - `test_no_ground_rows_read_no_ground_and_are_not_drawn_as_ground`;
    - `test_cut_and_fill_are_shaded_where_their_areas_are_positive`;
    - `test_exaggeration_scales_the_vertical_range`;
    - `test_idealised_note_follows_the_alignment_flag`;
    - `test_profile_cache_clears_on_terrain_import`;
    - `test_no_alignment_and_no_terrain_messages`;
    - `test_cli_parses_profile_idealised` and the new scenario keys in `test_capture_scenario.gd`.
11. **M3 assessment.** The report assesses each M3 exit criterion in `ROADMAP.md` as met, partly met or not
    met, and gives the evidence (a test, a shot or a figure) for each.

### Review decisions (Part B, 2026-11-26)

The first build failed review on one blocker. A fresh worker fixes the items below. Each fix needs a test that
fails without it.

1. **Blocker: the panel stops hearing events after a preset switch.**
   - `UiShell._place` moves the bottom dock with `reparent`. That runs `_exit_tree` and `_enter_tree`, but
     not `_ready`. So the `EventBus` connections made in `_ready` and dropped in `_exit_tree` are lost for
     good after a switch, or from start-up when Sandbox is the saved preset.
   - Connect in `_enter_tree` and disconnect in `_exit_tree`, each guarded by `is_connected`.
   - Test, in a shell:
     - with the Profile tab visible, switch to Sandbox and emit `sources_changed("terrain")`: the panel
       fetches again;
     - switch back to Engineering and emit it again: the panel fetches again;
     - after a switch, `project_changed` with a new project changes `alignment_id()`.
2. **A stale result is accepted.**
   - Bump the panel's generation when the alignment id changes and in `_on_inputs_changed`, including the
     no-alignment branch.
   - On completion, accept a result only when its alignment id is still the panel's.
   - Test with a request override that delays its reply:
     - the project closes while the fetch runs, and the panel ends in the no-alignment state;
     - the alignment changes while the fetch runs, and the old data is not drawn under the new id.
3. **Narrow windows.** The controls row and the labels must never draw into the readout column.
   - The controls and the legend sit in an `HFlowContainer` no wider than the plot, so they wrap to a
     second row when the plot is narrow.
   - The legend's third item reads "No ground data", with the tooltip "No ground data, e.g. water under a
     bridge".
   - A span label goes inside a span only when `rect.size.x >= text_width + 12` and it ends before
     `plot.end.x`. Otherwise it goes beside the span, where it fits, or is skipped.
   - A label that would overlap the previous one is skipped.
   - The label is drawn on a plate of the panel's background at 85 % alpha.
   - Test at 1280 × 720 and 1600 × 900, in both presets:
     - no control, legend item or label rect intersects the readout column;
     - the drawing area is at least 150 px tall at 1600 × 900 (the existing test) and at least 110 px at
       1280 × 720.
4. **The cost of a redraw.**
   - Draw at most about one row per pixel column of the plot: take every k-th row in the window, with k
     from the window's row count and the plot width, and always keep the rows that start or end a cut,
     fill or no-ground run.
   - Build the shading as one polygon per run of cut, or of fill, instead of one per segment.
   - The scenario's `perf` block accepts `ui` and `bottom_tab`.
   - Report one local `terrain_synthetic` run whose perf block has `"ui": "visible"`,
     `"bottom_tab": "profile"` and the Whole-line window. Do not commit that change.
   - Test that the number of drawn rows never exceeds the plot width in pixels plus the kept run ends.
5. **Degenerate polygons.**
   - No polygon handed to `draw_colored_polygon` may have a repeated consecutive vertex or an area below
     1e-6 (in its drawing units).
   - Test on a sine-shaped profile whose formation crossings fall within 0.1 m of a row: every polygon
     the helper returns gives a non-empty `Geometry2D.triangulate_polygon`.
6. **Following playback.** Add a test with a small `PlaybackController` subclass that overrides
   `lead_station()` and `alignment_id()`, bound with `bind_playback`:
   - the panel takes the controller's alignment;
   - after a frame, the readout follows a moved station;
   - a move of less than one pixel causes no redraw.
7. **Vertical scale.**
   - The exaggeration selector offers "Fit", "1×", "5×" and "10×". The default stays 10×: a stable
     ratio keeps the gradients comparable while the window slides during playback.
   - "Fit" fits the rail, the ground and the formation level in the window, plus 10 % margin, with a
     span of at least 2 m.
   - A label beside the selector shows the effective factor, "≈ 38×", when "Fit" is chosen, or when the
     fitted range overrides the chosen ratio by more than 5 %.
   - The selectors have the tooltips "Window width" and "Vertical exaggeration".
8. **Tick labels.**
   - Each axis takes the decimals its tick step needs: the vertical axis shows "240" at a 20 m step and
     "180.5" at a 0.5 m step.
   - The km axis shows at least one decimal, and 3 at the 1 km window, as before.
   - The "m" unit label does not touch the top tick label.
9. **Small items.**
   - The readout reads "Ground − rail: −0.40 m", with U+2212 for minus and "+" for plus, in the readout
     and in `summary()`.
   - Engineering's no-ground text colour reaches 4.5:1 against the panel background.
   - Readout values that are too long end in an ellipsis and show the full text as a tooltip.
   - `row_range` takes exactly one row either side of the window, and its test asserts that.
   - Replace the colour test's assertion that cannot fail.
   - Move the misplaced T-135 C comment in `test_capture_scenario.gd`.
   - `test_ui_preset.gd` also asserts that the Layers header stays visible.
10. **Accepted as they are.**
    - Two requests for one key while one is loading (the first result is discarded).
    - A late failure that overwrites `ground_profile_error()`.
    - The tab focus ring.
    - The pill under a larger font.
    - A hover readout and click-to-seek go to T-141, the timeline bar.

11. **After the fix round (architect, 2026-11-26).**
    - **Accepted:**
      - the readout keeps "Ground − rail −0.40 m" without a colon, which the existing test pins;
      - "Fit" is the fourth choice;
      - labels that do not fit are left out rather than moved.
    - **One more fix.** The vertical axis showed two labels, 150 and 200, at a 50 m step in a 160 px plot.
      That is too sparse to read heights from.
      - Choose the step from 1, 2, 2.5 and 5 × 10ⁿ: the finest step whose labels are at least 1.2 font heights
        apart.
      - The test asserts at least 4 metre labels in a plot 150 px tall at the 5 km window and 10×.

12. **Re-review (2026-11-26): pass with findings.** Fixed with Part C's client half, by the same worker:
    - **Slivers at real station magnitudes.** At stations of about 13,000 m, float32 pixel polygons under about
      0.006 px² are still refused by the engine. Raise `AREA_MIN_PIXELS` to 1e-2 px², or skip a polygon whose
      `Geometry2D.triangulate_polygon` is empty. Add a test with stations from 13,000 m and crossings at
      fractions from 1e-6 to 1 − 1e-6 of a segment.
    - **Undecimated shading.** `band_runs` takes every row in the window, so the whole-line redraw is about
      10 ms. Run it over the sampled rows. Compute `_vertical_extent` once per redraw; today
      `_refresh_factor_label` computes it a second time.
    - **Legend swatches.** They are filled before the theme applies, so they keep the fallback colours: the
      no-ground swatch is dark grey on the dark Sandbox pill. Refresh them on `NOTIFICATION_THEME_CHANGED`.
      Test: after a switch to Sandbox, each swatch equals its theme colour.
    - **Stale controls height.** After a narrow → wide resize, the plot stays 27 px shorter, because
      `_layout` reads the flow container's minimum size before its deferred sort. Connect
      `_controls.minimum_size_changed` to a deferred `_layout`, and test the resize.
    - **Dead helpers.** Delete `band_polygons`, `valid_runs`, `nice_ticks` and `tick_step`. Retarget their
      tests to `band_runs`, `sampled_runs`, `ticks_at` and `spaced_step`. The shading test covers the fill
      side too, and checks one polygon's area against a hand integral.
    - **Accepted:**
      - the cursor line over a plated label;
      - the length of the panel file;
      - the `background` theme token;
      - two requests per project change.

## Part C — surface models are not ground (F57, 2026-11-26)

Part B's Kralupy capture imports Copernicus GLO-30 with a 2 km clip, as M3's "generic terrain fills
everywhere detailed data is absent" requires. GLO-30 is a surface model (T-133): it follows treetops,
rooftops and, over the Vltava, the water surface. `alignment.ground_profile` samples the whole stack, so
where DMR 5G has no points, GLO-30 is taken as ground. At the bridge this reads ground 167.50 m, fill
3.1 m² and no hatch. In a forest it would read the canopy as ground and report metres of false cut.

**Decision (architect, 2026-11-26):** a surface model is display terrain only. Earthworks use bare-ground
sources only. Where only a surface model covers the centreline, the station has no ground for earthworks,
and the result says why. The 3D terrain, the tiles, the source map and the overlay are unchanged.

### Contract

- **The setting.** `ImportSettings.surface_model: bool = False`: the source is a surface model (it includes
  vegetation and buildings), not bare ground.
  - `ImportSettingsDTO` gains the same field with the same default, and a test checks that the defaults
    are equal.
  - It is recorded with the dataset's settings and round-trips like every other setting.
- **Providers.** A provider may declare `surface_model = True` as a class attribute; read it with `getattr`
  and a default of `False`, as `runner.py` already does for `title` and `format_id`.
  - `CopernicusGlo30Provider` declares it.
  - The runner records `surface_model = True` for such a provider's import whatever the request sent. The
    flag describes the data, not a choice.
  - Its `inspect` report suggests `surface_model=True`.
- **`source.list`.** Each terrain entry gains `surface_model: bool`.
- **The stack.**
  - `StackSource` gains `surface_model: bool = False`, carried from the dataset's settings through
    `_terrain_snapshot` and `_build_terrain`.
  - `TerrainStack` gains `surface_model: tuple[bool, ...]`, aligned with `surfaces`.
  - `TerrainStack.heights_and_source(e, n, *, bare_ground: bool = False)`. With `bare_ground=True`, surfaces
    marked as surface models are skipped. The returned index still indexes `surfaces`, so it still indexes
    `source_ids`.
  - The default call is unchanged, and so are precedence, tiles and the source map.
- **`alignment.ground_profile`.**
  - It samples with `bare_ground=True`.
  - For every flag-1 station it queries the full stack at the centreline. Where a surface model covers it,
    the row's flags are `1 | 8` (9), and `source` is that surface model's index.
  - `ground_h` stays NaN and both areas stay 0. Volumes and `no_ground_spans` treat the row exactly as
    flag 1.
  - Flag 8 never comes without flag 1.
  - `domain/earthworks.py` does not change. The handler passes a bare-ground callable and sets flag 8
    afterwards.
- **Docs.**
  - `docs/data-contracts/earthworks.md`: bit 8 in the flags table, "Flag 1 comes alone, or with 8", and a
    short "Surface models" paragraph.
  - `docs/data-contracts/import-framework.md`: the setting.
  - `docs/protocol/ipc.md`: regenerated.
- **Client (`profile_panel.gd`).**
  - For a flag-9 row, the readout reads "Ground: surface model only" and the source's name, for example
    "Copernicus GLO-30".
  - The hatching and the legend are unchanged.
  - A line covered only by a surface model must not read "No terrain covers this alignment": that check
    tests flag 1 without flag 8.
  - The import dialog needs no change in this part; a checkbox for files goes to the roadmap.

### Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/sources.py` | `ImportSettings.surface_model` |
| `backend/src/coypu_builder/domain/terrain_surface.py` | `StackSource.surface_model`, `TerrainStack.surface_model`, `bare_ground` |
| `backend/src/coypu_builder/io/providers/copernicus.py` | `surface_model = True`; the suggested setting |
| `backend/src/coypu_builder/io/sources/runner.py` | the provider's flag wins |
| `backend/src/coypu_builder/protocol/messages.py` | `ImportSettingsDTO.surface_model`, `source.list`'s `surface_model` |
| `backend/src/coypu_builder/server/handlers.py` | snapshot, build, `ground_profile` with flag 8 |
| `docs/data-contracts/earthworks.md`, `import-framework.md`, `docs/protocol/ipc.md` | as above |
| `backend/tests/test_earthworks.py`, `test_terrain_surface.py` (or the existing stack tests), provider tests | the tests below |
| `client/ui/profile_panel/profile_panel.gd`, `client/tests/unit/test_profile_panel.gd` | the readout |

### Acceptance criteria

1. `test_bare_ground_skips_surface_models`: a stack holding a bare-ground TIN over `x < 50` and a surface
   model 5 m higher everywhere.
   - With `bare_ground=True`: NaN and −1 where only the surface model covers, and the TIN's heights and
     index elsewhere.
   - The default call returns the surface model's heights in the gap, as before.
2. `test_ground_profile_flags_surface_model_only_stations` (over the wire, synthetic files, one imported
   with `surface_model: true`).
   - Rows in the gap carry flags 9, NaN ground, zero areas and the surface model's index in `source`.
   - The totals and `no_ground_spans` equal those of the same project without the surface model.
   - `terrain.tile` heights in the gap still come from the surface model.
3. `test_copernicus_import_is_always_a_surface_model`, with the existing provider test server.
   - An import requesting `surface_model: false` is recorded as true.
   - `source.list` says so.
   - `inspect` suggests it.
4. `test_import_settings_dto_defaults_match_the_domain` covers the new field. A file import defaults to
   false, and `true` round-trips.
5. Client: `test_surface_model_only_rows_read_surface_model_only`.
6. Live, run locally with `COYPU_DATA` and reported, not on CI: in `terrain_kralupy`, which still imports
   GLO-30, `kralupy_profile_bridge` shows the hatched span. The readout at km 16.700 reads "surface model
   only", Copernicus GLO-30. Ground − rail at km 13.0, 14.0 and 16.0 is unchanged: +17.36, +11.11 and
   +1.00 m.
7. No regressions: both suites are green, with no previously passing test removed, skipped or weakened.
   Goldens are unchanged, and `gen_protocol_docs.py --check` exits 0.

### Review decisions (Part C, 2026-11-26)

The review passed Part C with findings, and the same worker fixes them in the main tree. Each fix needs a test
that fails without it.

1. **The wire doc.** The `flags` BlobSpec in `protocol/messages.py` still says flag 1 comes "alone" and has
   no bit 8. The `source` BlobSpec says "the source that gave the centreline ground", which is not true on
   flag 9.
   - Fix both: "1 = no bare ground under the centreline, alone or with 8"; "8 = a surface model covers the
     centreline, always with 1"; and "on flag 9, the surface model at the centreline".
   - Say "bare ground only" in the method's summary.
   - Regenerate `docs/protocol/ipc.md`.
2. **"Flag 8 never comes without flag 1" is untested.** Add a case where the surface model outranks the bare
   source: a finer DSM over a coarser DTM, unit-level with a stub alignment, or a second wire project. Assert:
   - `(flags & 8) == 0` wherever `ground_h` is finite;
   - `flags & 8` implies `flags & 1`;
   - rows with bare ground carry the bare source's index.
3. **Strict snapshots.** Add the eighth element (`False`) to the hand-built snapshot tuples in
   `test_terrain_providers.py`, and make `_build_terrain`'s unpacking strict: no `*flags` tolerance.
4. **The vertical-extent counter.** It must count inside `_vertical_extent` itself, so a direct second call
   from `_draw` fails `test_the_vertical_range_is_computed_once_per_redraw`.
5. **Razor-thin polygons.** Drop a polygon whose mean thickness, its area divided by its x-extent, is under
   1e-3 px.
   - Test: a profile whose ground stays within a few millimetres of the formation across hundreds of
     pixels.
   - The test passes only when every polygon kept triangulates.
6. **A line covered only by a surface model.**
   - When every row has flag 1 and at least one has flag 8, show the status message "Only a surface model
     covers this alignment (<its name>) — import bare-ground terrain for cut and fill".
   - When every row has flag 1 and none has flag 8, keep "No terrain covers this alignment — import terrain
     to see the ground".
   - In both cases the whole-line hatching stays suppressed. Partial spans stay hatched.
   - Test both messages and a mixed 1/9 line.
7. **The readout on a row with no ground.** On a flag-1 or flag-9 row, the cut and fill read "—", not
   "0.0 m²".
8. **Accepted, or to the roadmap.**
   - The span label still reads "no ground data (e.g. water)" over a surface-model span. That goes to the
     F57 residue.
   - A file re-import resets `surface_model` to the request's value. The future dialog checkbox must prefill
     it from `source.get`. That also goes to the F57 residue.
   - The ADR 0009 addendum is written by the architect.

## Invariants

- Earthworks read only the analysis surface, never display tiles.
- `domain/earthworks.py` is pure numpy and imports no shapely, I/O or protocol modules.
- Stations are absolute metres throughout.

## Acceptance criteria

1. **Analytic, synthetic** (Part A, `test_earthworks.py`):
   - `test_flat_at_formation_is_zero`;
   - `test_flat_two_metres_above_is_18_m2_cut`;
   - `test_flat_two_metres_below_is_18_m2_fill`;
   - `test_sloping_plane_gives_the_exact_mixed_section`;
   - `test_end_area_volume_is_exact_for_linear_areas`.

   They check:
   - flat ground at formation level gives zero cut and zero fill;
   - flat ground 2 m above formation gives the exact trapezoidal cut area `(W + 1.5·2)·2 = 18 m²` for
     W = 6 m: the bottom is W wide, the top W + 2·1.5·2. Ground 2 m below gives the same fill area, 18 m²;
   - a sloping ground plane gives the exact mixed cut and fill;
   - volumes by average end area are exact for linear area change.
2. **No ground** (`test_gap_gives_nan_flag_1_and_a_span_and_volumes_do_not_bridge_it`): a synthetic gap
   gives NaN ground, flag 1, zero area, and a span in `no_ground_spans`. Volumes do not bridge it.
3. **Sources and partial data:**
   - `test_source_matches_the_stack`;
   - `test_one_sided_ground_sets_flag_4`;
   - `test_ground_profile_over_the_wire`, for the method, the off-loop rule and `profile_idealised`.

   They check: `source` matches the stack; a section with ground on only one side is
   flagged 4 and its area is computed only where ground exists.
4. **From the review** (Part A):
   - `test_sloping_plane_through_ground_profile`, with ground `F − 0.2 + 0.1·(offset to the left)` on a
     straight alignment heading east, one heading north and one at 45°. Every station gives cut 1/17 and
     fill 25/17. This test fails if the section is sampled along the track instead of across it;
   - `test_asymmetric_slopes`: with `cut_slope = 2` and `fill_slope = 1`, flat ground 2 m above formation
     gives 20 m² of cut, and 2 m below gives 16 m² of fill;
   - `test_edge_tie_daylights_beyond_the_edge`: ground exactly at formation level at the edge, rising 1:1
     beyond it, gives the same cut as ground 1e-9 m higher;
   - `test_stations_fall_on_round_chainages`: from 3.7 to 47.2 at 10 m gives 3.7, 10, 20, 30, 40, 47.2;
     from 10 to 40 gives 10, 20, 30, 40, with no near-duplicate rows;
   - `test_work_budget_is_refused_before_computing`: both limits, with a heights callable that fails if
     it is ever called;
   - `test_section_line_equals_frames_centreline`;
   - over the wire: `attributions[source_id]` equals the dataset's attribution, `source_names` holds the
     dataset name, `terrain_revision` equals the terrain cache's, the echoed `template` equals the domain
     defaults when none is sent, and `profile_idealised` round-trips through `import.landxml` and
     `import.coypu`;
   - `FormationTemplateDTO`'s defaults equal `FormationTemplate`'s, by a test.
5. **Live (Kralupy with the 15 DMR 5G sheets via `${COYPU_DATA}` or the provider; Verification only):**
   - report the profile from 12.7 to 17.5 km at 10 m;
   - the ground-minus-rail figures at km 13.0, 14.0 and 16.0 agree with F32, where T-132 measured about
     +17 m at km 13.0, falling smoothly to about 0–2 m by km 16 (within 1 m). They also equal
     `TerrainStack.heights` sampled directly at the same centreline points, to 1e-6 m;
   - the bridge span is reported as no ground;
   - report the total cut and fill volumes, noting they are nominal because the profile is idealised.
6. **Client:**
   - the Profile tab draws all elements with the cursor following playback (unit tests with a fake
     profile);
   - a capture shot of the Profile tab at km 13 shows the large nominal cut and the idealised-profile
     note, and one at km 16.7 shows the hatched bridge span;
   - performance is unchanged.
7. **No regressions:** both suites are green on Windows and on Linux CI, with no previously passing test
   removed, skipped or weakened, and `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Parametric cross-sections, drainage and sub-ballast layers — Phase 2.
- A colour ramp of cut and fill on the 3D corridor — T-144's diagnostics view mode, which reads this
  method.
- Mass haul diagrams and quantities export — Phase 3 (WBS and quantities).

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario terrain_kralupy   # with COYPU_DATA set to your data folder
```

## Report back

State:
- the analytic results, with the formulas used;
- the Kralupy profile figures, totals and bridge span from criterion 5;
- the capture shots of the Profile tab, described;
- whether M3's exit criteria are now met end to end. This task closes M3: assess each criterion.
