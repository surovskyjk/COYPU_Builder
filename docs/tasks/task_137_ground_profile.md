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

Two parts, each its own worker order:

| Part | What | Can start when |
|---|---|---|
| **A — backend** | `domain/earthworks.py`, `alignment.ground_profile`, the `profile_idealised` flag | T-136 Part A is committed |
| **B — client** | the Profile tab, the cursor, the readout, the captures; assess M3's exit criteria | Part A and T-135 Part C are committed |

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
