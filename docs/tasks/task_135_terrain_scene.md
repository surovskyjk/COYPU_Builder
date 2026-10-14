# T-135 — Client terrain scene: tile streaming, imagery draping, source boundaries

**Milestone:** P1.M3 · **Depends on:** T-132, T-133, T-134, T-140, T-125 · **Blocks:** T-137's 3D
readout, T-144

## Context

The backend now serves terrain as paged heightfield tiles at five levels of detail (T-132), with a
per-sample source map (T-133). It also serves matching imagery tiles: orthophoto where it exists, shaded
relief elsewhere (T-134).

This task puts them on screen. Around the camera, the client streams the right level of detail, builds
meshes from the heightfields, drapes the imagery, and keeps a locked frame rate while it does so. The
user can also see where detailed ČÚZK terrain ends and generic Copernicus terrain begins, which is part of
the M3 exit criterion.

ADR 0007's addendum makes the client responsible for triangulating heightfield tiles: connectivity over
heights the backend owns. Every ground *query* still goes to the backend.

## Preconditions

- T-132:
  - `terrain.tiles {level}` gives metadata: `level, ix, iy, edge_m, tile_origin` in Godot axes, float64,
    `valid_fraction`, plus `revision`;
  - `terrain.tile {level, ix, iy, revision}` returns blob `heights` (`<f4`, `(129, 129)`, row 0 south,
    relative to `tile_origin.y`, NaN = no data);
  - a stale revision returns `E_NOT_FOUND`.
- `docs/data-contracts/terrain-tiles.md` gives the vertex mapping (x = c·edge/128, z = −r·edge/128,
  y = heights[r, c]) and the float32 caveat: shared edges agree within about 1e-4 m on the client, not
  bit for bit.
- T-133: blob `source_index` (`<u1`, `(129, 129)`, 255 = none) and the ordered source ids per revision.
- T-134:
  - `imagery.tiles` / `imagery.tile`, same grid and revision;
  - blob `image` (JPEG, 512 × 512, pixel (0, 0) at the north-west), blob `alpha`;
  - per-tile attribution strings.
- T-140:
  - `UiShell.world_root()` (the SubViewport scene), `status_bar()`, `ribbon()`, `show_message()`;
  - the ribbon's View group;
  - `LayerState` through `Session.layers()`.
- T-125: the capture harness, scenarios, the perf block and `scene_fraction`.
- T-121's `TrackCorridor` is the pattern for paged loading (F18), tile-local nodes and LOD.
- F27: `client/domain_mirror/layer_state.gd:4` and `client/README.md` still name T-132 as a subscriber.

Read before starting:
- `docs/adr/0004-precision-and-local-origin.md`;
- `docs/adr/0007-evaluation-locality.md`, including the addendum;
- `docs/data-contracts/terrain-tiles.md`, `terrain-providers.md` and `imagery-tiles.md`;
- `client/scene/track/track_corridor.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/scene/terrain/terrain_field.gd` | new — LOD selection, paging, cache, per-frame budget |
| `client/scene/terrain/terrain_tile.gd` | new — one tile node: mesh, skirts, material |
| `client/scene/terrain/heightfield_mesh.gd` | new — pure heightfield to `ArrayMesh` builder, unit-testable |
| `client/scene/terrain/terrain_material.gd` | new — imagery texture, alpha, source-boundary overlay |
| `client/core/session.gd` | fetch helpers for terrain and imagery tiles, revision-aware caches |
| `client/scene/main.gd` | build the `TerrainField` under `world_root()`; feed it the active camera |
| `client/ui/top_bar/ribbon.gd` | View group: a "Source boundaries" toggle |
| `client/ui/shell/ui_shell.gd` | attribution text on the right of the status bar |
| `client/domain_mirror/layer_state.gd`, `client/README.md` | fix the F27 comments; document the terrain scene |
| `client/tools/capture/scenario.gd`, `capture_driver.gd`, `scenarios/terrain_kralupy.json` | an `imports` key and a terrain scenario |
| `client/tests/unit/test_heightfield_mesh.gd`, `test_terrain_lod.gd` | new |
| `client/tests/integration/test_terrain_field.gd` | new — live backend, synthetic terrain |

## Contract

### Level-of-detail selection

- **Quadtree:** `TerrainField` refines over the backend's tile grid, from L4 down to L0.
- **Split rule:** a tile is split into its four children while the camera's distance to the tile's
  bounding box is below `split_factor × edge_m`. The default factor is 1.5.
- **Coverage:** only tiles present in that level's `terrain.tiles` list are shown. Holes in the grid are
  never filled by inventing tiles.
- **Function:** `select_tiles(camera_position: Vector3, lists: Dictionary) -> Array[Vector3i]` is pure and
  deterministic. Each element is (level, ix, iy).

### Paging and budget

- **Fetch order:** tiles are fetched one request at a time, nearest first, through `Session`. Each request
  checks the revision.
- **Stale revision:** on `E_NOT_FOUND`, the field re-lists every level and rebuilds, keeping the old tiles
  visible until their replacements are ready, so the user never sees a blank flash.
- **Building:** meshes and textures are built **off the main thread** with `WorkerThreadPool`. At most two
  finished tiles are attached to the scene per frame.
- **Cache:** an LRU of 256 built tiles; tiles out of view and beyond the cache limit are freed.
- **E_BUSY:** the field backs off (exponential, up to 2 s) and retries. It never spins.

### Heightfield mesh (`heightfield_mesh.gd`, pure)

- **Vertices:** 129 × 129, using the contract mapping. The node's position is `tile_origin`, which is
  never folded into the vertices (ADR 0004).
- **Holes:** a quad with any NaN corner is omitted. NaN vertices are never emitted.
- **Normals:** from central differences of the heights, one-sided at edges and next to NaN.
- **UVs:** `(c / 128, r / 128)` on vertices, matching the imagery pixel layout.
- **Skirts:** each tile edge gets a strip hanging `max(0.5, edge_m / 64)` metres straight down from its edge
  vertices, so cracks between neighbouring levels never show sky.

### Material

- **Albedo:** `imagery.tile`'s JPEG (`Image.load_jpg_from_buffer`).
- **Alpha:** from the `alpha` blob. Pixels with no terrain are discarded, not blended.
- **Source boundaries:** when the toggle is on, a shader overlay tints each source with a distinct, subtle
  colour, using `source_index` as a second texture, and draws a 2-pixel line where the source changes. The
  toggle state is a per-user setting in `UserSettings`.
- **Placeholder:** until a tile's imagery arrives, the mesh uses a flat neutral material.

### Layers and attribution

- `Session.layers()` gains `"terrain"` (Terrain) and `"imagery"` (Imagery).
  - Hiding imagery shows the neutral material.
  - Hiding terrain hides the whole field.
- The status bar shows, on the right, the de-duplicated attribution strings of every source contributing
  to the tiles currently visible, for example "© ČÚZK · © Copernicus …".

### Capture

- **`imports` key:** the scenario's `imports` is a list of `import.run` calls issued after the project loads
  and before `scene_ready`. Paths may contain `${COYPU_DATA}`, which `tools/capture.ps1` expands from the
  environment variable of that name.
- **Skipping:** a scenario whose variables are unset is skipped with a clear message (exit 0). It must not
  fail, so the repository never needs a real data path.
- **`terrain_kralupy.json`:**
  - imports the 15 DMR 5G sheets from `${COYPU_DATA}/Terrain_Samples/cuzk/DMR5G` with a 250 m corridor clip,
    and the Copernicus GLO-30 provider over 2000 m. Copernicus needs the network, and its absence is a skip
    with a message;
  - shots: an orbit overview of the KRAV82 area, a low orbit along the cutting near km 13 (the F32
    profile), a cab view at km 16.7 at the Vltava bridge gap, and the source-boundary overlay on;
  - perf: cab at 4× through km 12–17, with streaming active.

## Invariants

- No geometry computed for analysis on the client; heights come only from tiles.
- No RPC from `_process`. Paging runs in coroutines; the frame loop only selects tiles and attaches finished
  ones.
- Nothing in the repository contains a local absolute path. Only `${COYPU_DATA}` appears, and it is expanded
  locally.

## Acceptance criteria

1. **LOD selection unit tests:**
   - a camera close to one corner refines only nearby tiles;
   - moving away coarsens them;
   - tiles absent from the lists are never selected;
   - the result is deterministic.
2. **Mesh unit tests:**
   - vertex positions match the contract mapping exactly;
   - NaN quads are omitted with no NaN in any array;
   - normals of a plane are exact;
   - skirts are present on all four edges at the right depth;
   - UVs follow the imagery layout, checked with an asymmetric marker.
3. **Live backend with a synthetic terrain** (T-130 fixture plus a synthetic image source):
   - the field shows the expected tiles at a fixed camera, and their heights match `terrain.tile`;
   - a stale revision after an import is handled without a blank frame;
   - `E_BUSY` is retried with backoff.
4. **Capture of `terrain_kralupy`** (with `COYPU_DATA` set; Verification only):
   - every shot has terrain and imagery;
   - the cutting near km 13 is visible as ground above the rail;
   - the bridge gap at km 16.7 shows as a hole or water area under the track;
   - the boundary overlay distinguishes DMR 5G from Copernicus;
   - attribution text is present;
   - in the perf block, p95 stays under 16.7 ms, and no frame exceeds 50 ms while streaming.
5. **Regression:** `kralupy_m2` and `ui_shell` still pass. Both suites are green on Windows and on Linux CI,
   with no previously passing test removed, skipped or weakened.

## Out of scope

- The import dialog — T-136. Cut/fill and the profile view — T-137.
- Sky, fog and render quality — T-144. Engineering's dark 3D background against the light chrome is noted
  for T-144.
- Water surfaces and bridges as objects — M5 (T-153) and Phase 2.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario kralupy_m2
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario ui_shell
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario terrain_kralupy   # with COYPU_DATA set to your data folder
```

```bash
cd backend
uv run pytest -q
```

## Report back

State:
- the LOD thresholds, and the tile counts per level at each shot;
- the streaming times, cold and warm;
- the perf figures, including the worst frame while streaming;
- each `terrain_kralupy` shot, described;
- whether any cracks, seams or swimming were visible, and how the skirts behaved.
