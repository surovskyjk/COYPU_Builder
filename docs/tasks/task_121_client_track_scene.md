# T-121 — Client track scene: chunk instancing, sleeper `MultiMesh`, LOD

**Milestone:** P1.M2 · **Depends on:** T-115, T-120 · **Blocks:** T-124

## Context

**This is the task where COYPU Builder first becomes something you can look at.** After it, the Kralupy
corridor is visible as rails, sleepers and ballast, correctly placed on the Křovák grid, and the whole
float64-domain / float32-wire / tile-local apparatus either works or visibly does not.

The rails and ballast arrive from T-120 as tile-local vertex blobs. Sleepers are the one piece of geometry
the client owns: placing thousands of identical boxes by interpolating the frame table is evaluation, which
ADR 0007 puts in the client, and a `MultiMesh` does it in one draw call.

## Preconditions

- T-120 has landed: `alignment.track_mesh` returns `TrackMeshChunkInfo` per chunk plus blobs named
  `vertices_<i>_<surface>`, `normals_<i>_<surface>`, `uvs_<i>_<surface>`, `indices_<i>_<surface>`.
- **Hard constraint (F17): you must fetch chunk by chunk.** T-120 measured the full Kralupy corridor at
  **24.62 MB** across 73 chunk groups (219 surfaces, 440,448 vertices). `IpcWebSocketClient.INBOUND_BUFFER_SIZE`
  is **16 MiB = 16.78 MB**, so a single `chunk_index: null` call produces one frame the client cannot
  receive. Call `alignment.track_mesh` once per `chunk_index` — roughly 337 KB each, comfortably inside the
  buffer. Do **not** raise `INBOUND_BUFFER_SIZE` to paper over this: paging is the designed path, it is what
  LOD and M3's terrain streaming will need anyway, and a 25 MB frame would block the socket poll regardless.
- T-115 has landed: `AlignmentTable` with `sample()`, `index_of()`, `position_at()`, `stations()`,
  `positions()`; `Session.fetch_alignment_table()`; `LayerState`; `EventBus`.
- T-103's `main.tscn` has a temporary free-look camera and a status overlay. **Both are still temporary** —
  T-124 replaces the camera. Do not invest in either.
- `client/scene/track/` and `client/view_modes/materials/` are empty placeholders.

Read before starting: `docs/data-contracts/track-mesh.md`, `docs/adr/0004-precision-and-local-origin.md`,
`docs/adr/0007-evaluation-locality.md`, `client/domain_mirror/alignment_table.gd`, `client/core/origin.gd`.

## Deliverables

| Path | Action |
|---|---|
| `client/scene/track/track_corridor.gd` | new — owns all chunks for one alignment |
| `client/scene/track/track_chunk.gd` | new — one `MeshInstance3D` built from blobs |
| `client/scene/track/sleeper_field.gd` | new — `MultiMeshInstance3D` placed from the frame table |
| `client/scene/track/track_materials.gd` | new — the three placeholder materials |
| `client/core/session.gd` | add `fetch_track_mesh()` and its cache |
| `client/core/event_bus.gd` | add `track_mesh_ready(alignment_id)` |
| `client/scene/main.gd` | build the corridor once an alignment is imported |
| `client/tests/unit/test_sleeper_placement.gd` | new |
| `client/tests/integration/test_track_corridor.gd` | new — real backend, real bake, headless |
| `client/README.md` | describe `scene/track/` |

## Contract

### Chunk instancing

```gdscript
class_name TrackChunk
extends Node3D

## Builds an ArrayMesh from one chunk's decoded blobs and positions this node at its tile origin.
static func build(info: Dictionary, vertices: PackedVector3Array, normals: PackedVector3Array,
                  uvs: PackedVector2Array, indices: PackedInt32Array, material: Material) -> TrackChunk

func station_start() -> float
func station_end() -> float
func surface() -> String
```

**The tile origin goes on the node's `position`; the vertices go into the mesh unchanged.** That is the whole
of ADR 0004 on this side — adding the origin into the vertices would reintroduce exactly the float32 shimmer
the design exists to prevent. Do not pass vertices through `Origin`; they arrive already base-point-relative
and in Godot axes.

`ArrayMesh` is built with `ARRAY_VERTEX`, `ARRAY_NORMAL`, `ARRAY_TEX_UV`, `ARRAY_INDEX` via
`surface_from_arrays`. Do not recompute normals — T-120 supplies them.

### Sleeper field

```gdscript
class_name SleeperField
extends MultiMeshInstance3D

const DEFAULT_SPACING_M := 0.6      # sleeper pitch
const SLEEPER_SIZE := Vector3(2.6, 0.16, 0.26)   # length across track, height, width along track

## Places one instance per sleeper station across [from_station, to_station], each transform taken from
## AlignmentTable.sample() so sleepers inherit cant roll and gradient pitch exactly as the rails do.
func populate(table: AlignmentTable, from_station: float, to_station: float,
              spacing_m: float = DEFAULT_SPACING_M, tile_origin: Vector3 = Vector3.ZERO) -> void
```

Sleeper transforms are built from the sampled `position` and `rotation`, offset downward along the frame's
own up axis so the sleeper top meets the rail foot. Like chunks, a sleeper field is **tile-local**: subtract
the field's `tile_origin` from each instance position and put the origin on the node, or float32 instance
transforms will shimmer at 10^5 m just as vertices would.

One `SleeperField` per chunk keeps instance counts and tile origins aligned with the rails.

### Corridor and LOD

```gdscript
class_name TrackCorridor
extends Node3D

## Coroutine. Fetches the chunk list, then each chunk separately by chunk_index (see F17 — a single
## all-chunks call exceeds the client's inbound buffer), instantiating as it goes.
func build(alignment_id: String, table: AlignmentTable) -> void
func chunk_count() -> int
func set_visible_layer(visible: bool) -> void                        # driven by LayerState
func update_lod(camera_position: Vector3) -> void
```

LOD is **distance-based visibility, not decimation**: beyond a near threshold hide the sleeper field (the
expensive instancing) while keeping rails and ballast; beyond a far threshold hide the chunk entirely. Two
thresholds, both constants, both reported with the frame-time numbers that justify them. Do not build a
mesh-decimation pipeline — that is not what Phase 1 needs and T-120 emits a single detail level.

`update_lod` is called from `_process`, which is legal — it is local arithmetic, not an RPC. **No RPC may be
issued from `_process`** (ADR 0007).

### Materials

Three placeholder `StandardMaterial3D`s in `track_materials.gd` — rail, sleeper, ballast — distinguishable by
albedo and roughness, no textures, no shaders. T-144's view modes replace them. Name them so the swap is
obvious.

## Invariants

- **ADR 0001 / 0007:** the client renders baked geometry and interpolates baked tables. It constructs no
  alignment geometry; the sleeper placement reads `AlignmentTable`, it does not evaluate a clothoid.
- **ADR 0004:** tile origins on nodes, never folded into vertices or instance transforms.
- **ADR 0002:** typed GDScript, tabs. Hot paths use packed arrays; `update_lod` must not allocate per frame.
- **ADR 0006:** nothing here may assume 1435 mm. Sleeper length comes from the alignment's gauge where the
  data provides it; where it does not, the constant is a documented placeholder.

## Acceptance criteria

1. `godot --path client` with the Kralupy fixture shows a continuous corridor of rails, sleepers and ballast
   along the full 18.2 km, with no gap or visible seam at any chunk boundary.
2. **No vertex shimmer.** Fly the camera to the far end of the corridor and confirm geometry is stable under
   motion — this is the acceptance test ADR 0004 exists for. Capture a screenshot at the far end.
3. Sleeper transforms match `AlignmentTable.sample()` at their stations: a unit test asserts position within
   1e-3 m and basis within 1e-5 of a directly sampled frame, including in a canted curve.
4. Sleepers roll with cant and pitch with gradient — assert non-zero roll on the tram fixture's canted span.
5. Frame time with the whole corridor loaded stays under 16.6 ms on your machine with LOD active; report the
   measured figure both with and without LOD, and the sleeper instance count.
6. `LayerState.set_visible("track", false)` hides the corridor; toggling back restores it.
7. The headless integration test builds a corridor against a real backend and asserts chunk count, total
   instance count and that every chunk node's position is non-zero and distinct.
8. No single `alignment.track_mesh` response exceeds 8 MB — assert it, so a future chunk-length change
   cannot silently reintroduce F17. `INBOUND_BUFFER_SIZE` is unchanged at 16 MiB.
9. Full gdUnit4 suite green on Windows **and** on Linux CI, with the T-116 grep guard still clean.

## Out of scope

- Terrain and imagery — M3. The corridor floats over an empty grid for now, and that is expected.
- Vehicles and playback — T-122 and T-123.
- Cameras beyond the temporary free-look — T-124.
- View modes, wireframe, x-ray, diagnostics ramps — T-144.
- Any UI panel or dock — M4.
- Editing, picking or selection.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
tools\run_dev.ps1
```

```bash
cd backend
uv run pytest -q
```

## Report back

State: measured frame time with and without LOD, the two LOD thresholds and the sleeper instance count;
confirmation that the far end of the corridor is shimmer-free, with a screenshot; chunk count and total
vertex count actually instantiated; and whether T-120's chunk length turned out to be the right vertex
budget or wants changing.

---

## Follow-up F18 — ask the backend how many chunks there are

*Added 2026-09-21 after the T-121 review. Small, spans both sides, and should land before M3 builds a second
paged resource on the same pattern.*

`TrackCorridor.build` currently derives the chunk count client-side:

```gdscript
const CHUNK_LENGTH_M := 250.0
chunk_count_expected = maxi(1, int(ceil(span / CHUNK_LENGTH_M)))
```

This re-implements the backend's chunking formula in the client. It is correct today because both sides use
fixed-length chunks and the client passes `chunk_length_m` along — and T-121 flagged it openly in a docstring
rather than hiding it, which is why it is a follow-up and not a defect.

It is still the wrong shape. The moment chunking becomes anything but `ceil(span / L)` — a vertex budget, a
key-station-aligned split, a per-surface difference — the client pages too few chunks and **silently renders
a truncated corridor**. That failure looks like the data ending early, not like a bug, which is the worst
kind to ship.

The real gap is in T-120's API, which I designed: `chunk_index: int | None` gives "one chunk with blobs" or
"all chunks with all blobs" (24.62 MB), and no way to ask the cheap question "how many chunks, and where?"

### Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/protocol/messages.py` | add a metadata-only mode to `AlignmentTrackMeshParams` |
| `backend/src/coypu_builder/server/handlers.py` | honour it — return `chunks`, emit no blobs |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/track-mesh.md` | document the two-step fetch as the intended pattern |
| `backend/tests/test_track_mesh.py` | assert the metadata call returns every chunk and zero blobs |
| `client/scene/track/track_corridor.gd` | drop `CHUNK_LENGTH_M`-based counting; use the returned list |
| `client/core/session.gd` | cache the chunk list alongside the chunk pages |
| `client/tests/integration/test_track_corridor.gd` | assert the count comes from the backend |

### Contract

Add `metadata_only: bool = False` to `AlignmentTrackMeshParams`. When true the handler bakes (or reads its
cache), fills `chunks` with every `TrackMeshChunkInfo`, and emits **no blobs** — a response of a few KB.
Keep `chunk_index: None` meaning "all chunks with blobs"; do not change existing behaviour.

If baking the whole corridor just to count chunks proves slow, say so with a measurement rather than adding
a cache — T-120 measured the full bake at 0.147 s, so this is very likely a non-issue.

Client side: `build()` calls once with `metadata_only: true`, then iterates the returned `chunks` by their
own `chunk_index`. `CHUNK_LENGTH_M` stays only as the value **passed to** the backend, never as the basis
for a count. The client must not contain a second expression of how chunking works.

### Acceptance

1. The metadata-only response carries every chunk's info and zero blobs; its size is under 64 KB, asserted.
2. `TrackCorridor` contains no arithmetic deriving a chunk count. The count comes from the response length.
3. The corridor still renders complete: 73 chunks, 440,448 vertices, 30,333 sleepers, unchanged.
4. A test proves truncation is now impossible: stub or vary the backend's chunking so the naive
   `ceil(span / 250)` would give the wrong answer, and confirm the client still fetches every chunk.
5. `gen_protocol_docs.py --check` exits 0 with the regenerated docs committed.
6. Both suites green on Windows and on Linux CI: 143 pytest, 70 gdUnit4 cases.

### Report back

State: the metadata response size; confirmation that no chunk-count arithmetic remains in the client; and
how you proved truncation cannot recur.
