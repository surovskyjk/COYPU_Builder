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

## How this task runs

Three parts, each its own worker order, run in this order:

| Part | What | Can start when |
|---|---|---|
| **A — terrain field** | LOD, paging, meshes, neutral material, the Terrain layer | T-133 is committed |
| **B — imagery and presentation** | draped imagery, source-boundary overlay, attribution | Part A and T-134 Part A are committed |
| **C — capture** | the `imports` key, `${COYPU_DATA}` expansion, the `terrain_kralupy` scenario | Part B and T-134 Part B are committed |

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
- T-133:
  - blob `source_index` (`<u1`, `(129, 129)`, 255 = none);
  - the ordered `source_ids` per revision;
  - `client/ipc/envelope.gd` decodes `<u1` to a `PackedByteArray`.
- T-134:
  - `imagery.tiles` / `imagery.tile`, same grid;
  - an imagery `revision` of its own, which changes when the terrain or the imagery sources change, and
    the `terrain_revision` it was composed against;
  - blob `image` (JPEG, 512 × 512, pixel (0, 0) at the north-west);
  - blob `alpha` (`<u1`, 512 × 512), sent only when `has_alpha` — without it every pixel is opaque;
  - `attributions` (source id → text), `notices` (source id → licence notices) and each imagery tile's
    `source_ids`.
- T-140:
  - `UiShell.world_root()` (the SubViewport scene), `status_bar()`, `ribbon()`, `show_message()`;
  - the ribbon's View group;
  - `LayerState` through `Session.layers()`.
- T-125: the capture harness, scenarios, the perf block and `scene_fraction`.
- F27: `client/domain_mirror/layer_state.gd:4` and `client/README.md` still name T-132 as a subscriber.

Read before starting:
- `docs/adr/0004-precision-and-local-origin.md`;
- `docs/adr/0007-evaluation-locality.md`, including the addendum;
- `docs/data-contracts/terrain-tiles.md`, `terrain-providers.md` and, for Part B, `imagery-tiles.md`.

**Code to copy the patterns from:**

| You write | Copy the pattern of |
|---|---|
| `TerrainField` (paging, tile-local nodes, LOD, layer visibility) | `client/scene/track/track_corridor.gd` |
| `Session.fetch_terrain_tiles` / `fetch_terrain_tile` | `client/core/session.gd: fetch_track_mesh_chunks`, `fetch_track_mesh` |
| `E_BUSY` handling | `client/ui/dialogs/envelope_export_dialog.gd` (around line 159) |
| live-backend integration tests | `client/tests/integration/test_track_corridor.gd` |
| capture keys and validation (Part C) | `client/tools/capture/scenario.gd` and `tests/unit/test_capture_scenario.gd` |

## Part A — terrain field

### Deliverables (Part A)

| Path | Action |
|---|---|
| `client/scene/terrain/terrain_lod.gd` | new — `class_name TerrainLod`, pure static `select_tiles` |
| `client/scene/terrain/heightfield_mesh.gd` | new — `class_name HeightfieldMesh`, pure static `build` |
| `client/scene/terrain/terrain_tile.gd` | new — `class_name TerrainTile`, one tile node |
| `client/scene/terrain/terrain_field.gd` | new — `class_name TerrainField`, paging, worker builds, cache, budget |
| `client/core/session.gd` | `fetch_terrain_tiles`, `fetch_terrain_tile`; the `"terrain"` layer |
| `client/scene/main.gd` | build the `TerrainField` under `world_root()`; feed it the active camera |
| `client/domain_mirror/layer_state.gd`, `client/README.md` | fix the F27 comments; document the terrain scene |
| `client/tests/unit/test_terrain_lod.gd`, `test_heightfield_mesh.gd` | new |
| `client/tests/integration/test_terrain_field.gd` | new — live backend, synthetic terrain |

### Contract (Part A)

**Level-of-detail selection** — `TerrainLod.select_tiles(camera: Vector3, lists: Dictionary, split_factor: float = 1.5) -> Array[Vector3i]`:
- `lists` maps level (int, 0–4) to a `Dictionary` whose keys are `Vector2i(ix, iy)` and whose values are
  each tile's `{edge_m, tile_origin: Vector3}`, as `terrain.tiles` lists them.
- The quadtree starts from every L4 tile and refines toward L0.
- A tile is replaced by its children while the distance from `camera` to the tile's axis-aligned bounding
  box, in the x–z plane, is below `split_factor × edge_m`.
  - The children of `(L, ix, iy)` are `(L−1, 2ix + dx, 2iy + dy)` for `dx, dy ∈ {0, 1}`.
  - Only children present in `lists[L−1]` are kept. If none is present, the parent stays.
- Only tiles present in the lists are ever returned. Holes in the grid are never filled by inventing tiles.
- Each element is `Vector3i(level, ix, iy)`. The result is sorted by distance to the camera, ties broken by
  `(level, ix, iy)`, so it is deterministic.
- The tile's bounding box is x from `tile_origin.x` to `+edge_m`, and z from `tile_origin.z − edge_m` to
  `tile_origin.z`. Grid row 0 is south and Godot z points south (`z = −(N − N0)`), so north is −z.

**Heightfield mesh** — `HeightfieldMesh.build(heights: PackedFloat32Array, edge_m: float, skirt_m: float) -> Dictionary`, pure:
- **Returns** `{vertices: PackedVector3Array, normals: PackedVector3Array, uvs: PackedVector2Array,
  indices: PackedInt32Array}`, ready for `ArrayMesh.add_surface_from_arrays`.
- **Vertices:** 129 × 129, with `x = c·edge/128`, `z = −r·edge/128`, `y = heights[r·129 + c]`. The node's
  position is `tile_origin`, which is never folded into the vertices (ADR 0004).
- **Holes:** a quad with any NaN corner is omitted, and no vertex with a NaN is emitted. A NaN sample's
  vertex is kept at `y = 0` but referenced by no triangle.
- **Normals:** from central differences of the heights, one-sided at the tile edges and next to a NaN.
- **UVs:** `(c / 128, 1 − r / 128)` on vertices. Imagery row 0 is north and heights row 0 is south, so `v`
  flips.
- **Skirts:** each tile edge gets a strip hanging `skirt_m` straight down from its edge vertices, so cracks
  between neighbouring levels never show sky. Only edge vertices that are not NaN get a skirt.
  `TerrainTile` passes `skirt_m = max(0.5, edge_m / 64)`.
- **Winding:** counter-clockwise seen from above (+y), so Godot's default back-face culling keeps the top
  side.

**Paging and budget** (`TerrainField`):
- **Listing:** on start, and whenever the project's terrain may have changed (`EventBus` source import, or
  any `E_NOT_FOUND` for a tile), call `Session.fetch_terrain_tiles(level)` for levels 4 down to 0.
- **Every frame**, `_process` calls `select_tiles` with the active camera's position. It only selects,
  queues and attaches; it never sends a request.
- **Fetching:** one paging coroutine fetches the queued tiles one request at a time, nearest first, with
  the listing's revision.
- **Building:** meshes are built off the main thread with `WorkerThreadPool.add_task`. At most
  `ATTACH_PER_FRAME = 2` finished tiles are attached to the scene per frame.
- **Stale revision:** on `E_NOT_FOUND`, re-list every level and rebuild. Keep the old tiles visible until
  their replacements are attached, so the user never sees a blank frame.
- **Cache:** an LRU of `MAX_TILES = 256` built tiles. Tiles out of view and beyond the limit are freed with
  `queue_free`.
- **`E_BUSY`:** back off exponentially from 0.1 s, doubling up to 2 s, then retry. Never spin.
- **Material:** for Part A, one shared neutral `StandardMaterial3D` (grey-green, roughness 1).
- **Layer:** `Session.layers()` gains `"terrain"` (Terrain). Hiding it hides the whole field.
- **Freeing:** the field frees every worker task's result it does not use. Nothing leaks when the scene
  closes; check that `WorkerThreadPool.wait_for_task_completion` is called for every task in flight.

### Acceptance criteria (Part A)

1. **LOD** (`test_terrain_lod.gd`):
   - `test_camera_near_a_corner_refines_only_nearby_tiles`;
   - `test_moving_away_coarsens`;
   - `test_tiles_absent_from_the_lists_are_never_selected`;
   - `test_parent_stays_when_no_child_is_listed`;
   - `test_selection_is_deterministic_and_sorted`.
2. **Mesh** (`test_heightfield_mesh.gd`):
   - `test_vertex_positions_follow_the_contract_mapping`, exact equality;
   - `test_nan_quads_are_omitted_and_no_array_holds_nan` — check every float, with `is_nan`, not
     `is_equal_approx`;
   - `test_plane_normals_are_exact`;
   - `test_skirts_on_four_edges_at_the_right_depth`;
   - `test_uvs_put_the_north_west_corner_at_v_zero`, with an asymmetric marker;
   - `test_winding_faces_up`.
3. **Live backend with a synthetic terrain** (`test_terrain_field.gd`), using the T-130 synthetic grid fixture:
   - `test_field_shows_the_expected_tiles_at_a_fixed_camera`, whose heights match `terrain.tile`;
   - `test_stale_revision_after_an_import_keeps_tiles_visible`, which checks at every frame that the
     attached tile count never drops to 0;
   - `test_busy_is_retried_with_backoff`. Inject `E_BUSY` through a test seam on `Session`; do not load the
     real backend.
4. **No regressions:**
   - `kralupy_m2` and `ui_shell` captures still pass;
   - both suites are green, with executed = declared test counts;
   - no previously passing test is removed, skipped or weakened.

## Part B — imagery and presentation

### Deliverables (Part B)

| Path | Action |
|---|---|
| `client/scene/terrain/terrain_material.gd` | new — `class_name TerrainMaterial`, shader material: imagery, alpha, overlay |
| `client/scene/terrain/terrain_tile.gd`, `terrain_field.gd` | fetch and apply imagery per tile |
| `client/core/session.gd` | `fetch_imagery_tiles`, `fetch_imagery_tile`; the `"imagery"` layer |
| `client/ui/top_bar/ribbon.gd` | View group: a "Source boundaries" toggle |
| `client/core/user_settings.gd` | `terrain_source_boundaries: bool`, default false |
| `client/ui/shell/ui_shell.gd` | attribution label on the right of the status bar, and the Data sources popover |
| `client/tests/unit/test_terrain_material.gd` | new |
| `client/tests/integration/test_terrain_field.gd` | imagery cases |

### Contract (Part B)

- **Albedo:** `imagery.tile`'s JPEG, decoded with `Image.load_jpg_from_buffer` in the worker task, never on
  the main thread.
- **Alpha:** from the `alpha` blob when `has_alpha`, otherwise opaque. Pixels with alpha 0 are discarded in
  the shader (`discard`), not blended.
- **Source boundaries:**
  - `source_index` becomes a second texture: an `R8` `Image` of 129 × 129, sampled with nearest filtering;
  - when the toggle is on, the shader tints each source index with a distinct subtle colour (10 %
    strength) and draws a 2-pixel line where the index changes;
  - the toggle state is the `UserSettings` key above.
- **Placeholder:** until a tile's imagery arrives, the tile keeps Part A's neutral material.
- **Layer:** `Session.layers()` gains `"imagery"` (Imagery). Hiding it shows the neutral material.
- **Revisions:** imagery is listed with `imagery.tiles` per level, and fetched with its own revision. An
  `E_NOT_FOUND` re-lists imagery only, unless `terrain_revision` changed too.
- **Attribution:**
  - the status bar shows, on the right, the de-duplicated attribution texts of every source contributing to
    the tiles currently attached;
  - imagery sources come from each imagery tile's `source_ids`, terrain sources from the unique values of
    each tile's `source_index`;
  - the texts are joined with " · " and elided to the space available, with the full list in the tooltip;
  - clicking it opens a **Data sources** popover that lists each source's full attribution and its
    `notices` (the Copernicus liability notice, for one), with a Close button and Escape to close.

### Acceptance criteria (Part B)

1. **Material** (`test_terrain_material.gd`):
   - `test_imagery_marker_lands_on_the_right_vertex` — an asymmetric marker in a synthetic JPEG maps to the
     expected UV after the `v` flip;
   - `test_alpha_texture_only_when_has_alpha`;
   - `test_source_index_texture_is_nearest_filtered_r8`.
2. **Live** (`test_terrain_field.gd`), with a synthetic image source:
   - `test_imagery_replaces_the_placeholder`;
   - `test_hiding_imagery_shows_the_neutral_material`;
   - `test_attribution_lists_exactly_the_sources_in_view`;
   - `test_an_imagery_only_import_does_not_rebuild_meshes`.
3. **Settings:** `test_source_boundary_toggle_persists`, through `UserSettings`.
4. **UI:** the reviewer judges a `ui_shell` capture with terrain, imagery and the popover open.
5. **No regressions:** as Part A.

## Part C — capture

### Deliverables (Part C)

| Path | Action |
|---|---|
| `client/tools/capture/scenario.gd`, `capture_driver.gd` | the `imports` key; skipping when a variable is unset |
| `tools/capture.ps1` | expand `${COYPU_DATA}` from the environment; pass the expanded scenario to Godot |
| `client/tools/capture/scenarios/terrain_kralupy.json` | new |
| `client/tests/unit/test_capture_scenario.gd` | `imports` validation cases |

### Contract (Part C)

- **`imports` key:** the scenario's `imports` is a list of `import.run` parameter dictionaries. They are
  issued after the project loads and before `scene_ready`, and each must succeed.
- **Expansion:** paths may contain `${COYPU_DATA}`. `tools/capture.ps1` expands it from the environment
  variable of that name, into a temporary copy of the scenario outside the repository.
- **Skipping:** a scenario that names an unset variable is skipped with the message
  `skipped: COYPU_DATA is not set` and exit 0. It must not fail, so the repository never needs a real data
  path. A provider import that fails for lack of network is also a skip, with its message.
- **`terrain_kralupy.json`:**
  - imports the 15 DMR 5G sheets from `${COYPU_DATA}/Terrain_Samples/cuzk/DMR5G` with a 250 m corridor clip,
    the Copernicus GLO-30 provider over 2000 m, and the ČÚZK orthophoto WMTS over the same corridor;
  - shots:
    - an orbit overview of the KRAV82 area;
    - a low orbit along the cutting near km 13 (the F32 profile);
    - a cab view at km 16.7, at the Vltava bridge gap;
    - the source-boundary overlay on;
    - the shell visible with the Data sources popover open;
  - perf: cab at 4× through km 12–17, with streaming active.

### Acceptance criteria (Part C)

1. **Validation:**
   - `test_imports_must_be_a_list_of_dictionaries`;
   - `test_an_unset_variable_skips_the_scenario`.
2. **Capture of `terrain_kralupy`** (with `COYPU_DATA` set; Verification only):
   - every shot has terrain and imagery;
   - the cutting near km 13 is visible as ground above the rail;
   - the bridge gap at km 16.7 shows as a hole or water area under the track;
   - the boundary overlay distinguishes DMR 5G from Copernicus;
   - attribution text is present;
   - in the perf block, p95 stays under 16.7 ms, and no frame exceeds 50 ms while streaming.
3. **No regressions:** as Part A.

## Invariants

- No geometry computed for analysis on the client; heights come only from tiles.
- No RPC from `_process`. Paging runs in coroutines; the frame loop only selects tiles and attaches finished
  ones.
- No scene-tree access from a `WorkerThreadPool` task. It builds arrays, meshes and images only, and the
  main thread attaches them.
- Nothing in the repository contains a local absolute path. Only `${COYPU_DATA}` appears, and it is expanded
  locally.

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
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario terrain_kralupy   # Part C, with COYPU_DATA set
```

```bash
cd backend
uv run pytest -q
```

## Report back

**Part A:**
- the LOD thresholds, and the tile counts per level at the integration test's camera;
- the build time per tile on a worker thread;
- every contract change, with its reason.

**Part B:**
- the shader's uniforms and how the overlay colours are chosen;
- the attribution text shown for the synthetic scene;
- every contract change, with its reason.

**Part C:**
- the streaming times, cold and warm;
- the perf figures, including the worst frame while streaming;
- each `terrain_kralupy` shot, described;
- whether any cracks, seams or swimming were visible, and how the skirts behaved.
