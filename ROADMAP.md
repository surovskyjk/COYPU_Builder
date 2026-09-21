# COYPU Builder — Roadmap

Living document. The architect session maintains it; worker sessions do not edit it. Every task listed here
has (or will have) a self-contained specification in [`docs/tasks/`](docs/tasks/README.md).

**Status legend** — `done` · `in progress` · `ready` (spec written, not started) · `planned` (no spec yet)

Last reviewed: 2026-09-21 against `2f14f50` (master green, three consecutive runs) plus the uncommitted T-122 change set. M2 is three of five done; T-123 next.

---

## Phase 0 — Bootstrap · `done`

Delivered the float64 domain kernel, the multi-dialect LandXML reader, the `.coypu` archive reader, the
msgspec/blob IPC on both sides, and two golden vector files.

| Baseline | Verified |
|---|---|
| `backend` pytest | 46 passed |
| Godot 4.7.2 headless `run_ipc_tests.gd` | 27 assertions passed (spawns a real backend over a real WebSocket) |
| Golden vectors | `shared/golden/origin_mapping.json`, `shared/golden/frame_eval.json` |
| Privacy | Fixture is COYPU Feeder / OpenStreetMap (ODbL) provenance; `.gitignore` vendor patterns absolute |

### Debt carried into Phase 1

| # | Debt | Retired by |
|---|---|---|
| D1 | ~~`docs/data-contracts/coordinate-conventions.md` cites `client/core/Origin.gd`, which does not exist~~ | **retired by T-103** |
| D2 | ~~ADR 0003 promised `tools/gen_protocol_docs.py`, `docs/protocol/ipc.md` and a CI freshness gate — all missing~~ | **retired by T-101** |
| D3 | ~~`run.get` raises `E_NOT_FOUND`; `domain/kinematics`, `topology`, `sections`, `analysis`, `io/gis`, `io/mesh`, `io/project` are 0-line packages; `domain/model` is `modes.py` alone~~ | **retired by T-110, T-111 and T-114.** `domain/sections`, `io/gis`, `io/mesh` and `io/project` stay empty by design until M3, M2 and M4 respectively |
| D4 | ~~gdUnit4 is not vendored; `CLAUDE.md` documents a command that cannot run~~ | **retired by T-102** |
| D5 | ~~`tools/run_dev.ps1` passes `--backend-url` / `--backend-token`; nothing reads them~~ | **retired by T-103** |
| D6 | ~~ADR 0006 requires a non-heavy-rail fixture before Phase 2 authoring ships~~ | **retired by T-110** |

---

## Phase 1 — Playback Visualizer + Spatial Context MVP

**Exit definition.** Open the Kralupy LandXML plus its COYPU kinematics run, see the corridor as real track on
real terrain under an orthophoto, and play or scrub a correctly-articulated trainset along it at 60 fps with
orbit, wayside and cab cameras. Read-only: no authoring, no editing, no IFC.

**Scope decisions (approved 2026-09-17).**

1. **Persistence** — a *partial* `.coypub` lands in M4: `project`, `base_point`, `layer`, `run` tables only.
   `entity`, `history`, `wbs` and `ifc_map` wait for Phase 2 to settle the entity set.
2. **GIS** — the tiling and mesh pipeline is built against **local GeoTIFF / ortho files first**, behind a
   provider interface; ČÚZK WMS/WCS and generic WMTS follow as a separate task. Live fetching must never be
   on the CI path.
3. **Track fidelity** — two swept rails + `MultiMesh` sleepers + one default ballast prism. The parametric
   cross-section system is Phase 2; `domain/sections/` stays empty until authoring defines its requirements.
4. **Tram fixture** — a synthetic tram loop with one junction is seeded in **M1**, not at the Phase 2
   boundary, so that mode-specific assumptions never become cheap to introduce.

### P1.M0 — Foundations & harness · `done`

*Goal:* the client becomes an application, and the two ADR promises that are cheapest to keep now get kept.

| Task | Title | Status | Depends on | Spec |
|---|---|---|---|---|
| T-101 | Protocol method registry, doc generator, CI gate | `done` (2026-09-17) | — | [task_101](docs/tasks/task_101_protocol_registry_and_docs.md) |
| T-102 | Vendor gdUnit4, port the client suite, CI switch | `done` (2026-09-17) | — | [task_102](docs/tasks/task_102_gdunit4_harness.md) |
| T-103 | Client app shell: main scene, autoloads, connection lifecycle | `done` (2026-09-18) | T-101, T-102 | [task_103](docs/tasks/task_103_client_app_shell.md) |

**Exit criteria.** `docs/protocol/ipc.md` is generated and CI fails when stale · gdUnit4 runs the client suite
in CI with no loss of assertion coverage · `godot --path client` boots a scene that spawns the backend,
completes `session.hello`, heartbeats, and survives a backend kill by reconnecting · `Origin` is validated
against `shared/golden/origin_mapping.json` · D1, D2, D4, D5 retired.

### P1.M1 — Run domain · `done`

*Goal:* kinematics becomes a first-class domain concept, and the client can mirror baked tables.

| Task | Title | Status | Depends on | Spec |
|---|---|---|---|---|
| T-110 | ADR 0006 entity model, topology graph, tram fixture | `done` (2026-09-18) | — | [task_110](docs/tasks/task_110_domain_model_and_topology.md) |
| T-111 | `KinematicsRun` normalisation and `RunTable` baking | `done` (2026-09-18) | — | [task_111](docs/tasks/task_111_kinematics_domain.md) |
| T-112 | Trainset chain reference + `trainset_chain.json` golden | `done` (2026-09-19) | T-110, T-113 | [task_112](docs/tasks/task_112_trainset_chain.md) |
| T-113 | Vehicle catalogue schema, loader, COYPU vehicle import | `done` (2026-09-18) | — | [task_113](docs/tasks/task_113_vehicle_catalogue.md) |
| T-114 | Protocol surface for runs, catalogue and `.coypu` import | `done` (2026-09-19) | T-101, T-111, T-113 | [task_114](docs/tasks/task_114_protocol_run_surface.md) |
| T-115 | Client domain mirror: alignment table, run table, registries | `done` (2026-09-19) | T-102, T-103, T-114 | [task_115](docs/tasks/task_115_client_domain_mirror.md) |
| T-116 | Cross-platform process supervision and socket reuse | `done` (2026-09-20, merged as `3519daf`) | T-103, T-115 | [task_116](docs/tasks/task_116_cross_platform_supervision.md) |

**Exit criteria.** A COYPU kinematics run imports from both CSV dialects and from `.coypu`, normalises to one
`KinematicsRun`, and bakes to a time-uniform `RunTable` · `run.get` returns real blobs · the client can
interpolate a frame table and a run table with results pinned to `shared/golden/` · a synthetic tram network
with a junction exists and passes the same tests as heavy rail · D3, D6 retired.

### P1.M2 — Track in 3D · `ready`

*Goal:* the first real demo — a train running on rails.

| Task | Title | Status | Depends on | Spec |
|---|---|---|---|---|
| T-120 | Backend track mesh baker: rails + ballast prism, tile-local chunking | `done` (2026-09-20, `e8810f7`) | T-110 | [task_120](docs/tasks/task_120_track_mesh_baker.md) |
| T-121 | Client track scene: chunk instancing, sleeper `MultiMesh`, LOD | `done` (2026-09-21, `2f14f50`) — F18 open | T-115, T-120 | [task_121](docs/tasks/task_121_client_track_scene.md) |
| T-122 | Client vehicle scene: procedural car from the catalogue spec, consist assembly | `done` (2026-09-21, uncommitted) | T-113, T-115 | [task_122](docs/tasks/task_122_client_vehicle_scene.md) |
| T-123 | Client playback: transport, scrub, speed, trainset chain evaluation | `ready` | T-112, T-115, T-122 | [task_123](docs/tasks/task_123_client_playback.md) |
| T-124 | Cameras: manager, orbit, wayside, XR-ready cab rig | `ready` | T-121, T-123 | [task_124](docs/tasks/task_124_cameras.md) |

**Exit criteria.** The Kralupy corridor renders as rails, sleepers and ballast with no vertex shimmer at any
zoom · a three-car consist plays back along it at a locked 60 fps with bogies on the rails and bodies chording
the curves · the client chain matches `shared/golden/trainset_chain.json`.

### P1.M3 — Spatial context · `planned`

*Goal:* the corridor sits on real ground. Parallelisable against M2 — disjoint file sets
(`io/gis` + `scene/terrain` versus `io/mesh` + `scene/track`).

| Task | Title | Status | Depends on |
|---|---|---|---|
| T-130 | DEM ingest: GeoTIFF → tile-local terrain meshes, reprojection, tile grid | `planned` | T-103 |
| T-131 | Imagery ingest: local ortho → tiles; then WMS/WMTS providers behind the same interface | `planned` | T-130 |
| T-132 | Client terrain scene: tile streaming by camera distance, ortho texturing | `planned` | T-121, T-130 |
| T-133 | Ground sampling: `ground_elevation` on the frame table, cut/fill readout, longitudinal profile | `planned` | T-130 |

**Exit criteria.** Terrain and orthophoto stream around the camera without a frame-time spike · the corridor
drapes correctly against the DEM · a cut/fill strip reads out along stations · nothing on the CI path touches
the network.

### P1.M4 — Shell, view modes & persistence · `planned`

*Goal:* Phase 1 becomes a product rather than a demo.

| Task | Title | Status | Depends on |
|---|---|---|---|
| T-140 | UI theme, top bar, floating bottom dock skeleton | `planned` | T-103 |
| T-141 | Timeline bar: transport, scrub, stop markers, station/time/speed readout | `planned` | T-123, T-140 |
| T-142 | Layers panel wired to `layer_state` | `planned` | T-115, T-140 |
| T-143 | Inspector panel with progressive disclosure | `planned` | T-115, T-140 |
| T-144 | View modes: realistic, wireframe, x-ray, diagnostics (curvature / cant / gradient ramps) | `planned` | T-121 |
| T-145 | Partial `.coypub`: schema, repository, `project.save` / `project.open` | `planned` | T-110, T-114 |

**Exit criteria.** The Phase 1 exit definition is met end to end on the Kralupy corridor from a cold start ·
a session survives save and reopen · the diagnostics view mode renders curvature, cant and gradient as colour
ramps, doubling as a visual regression check on the kernel.

> M2–M4 tasks are deliberately left unspecified. Their inputs — the run-domain shapes, the mesh chunking
> parameters, the client mirror's API surface — are not settled until M1 lands, and a spec written against
> guesses would cost more to correct than to write late. They are specified one milestone ahead of execution.

---

## Beyond Phase 1 (direction, not commitment)

- **Phase 2 — Authoring.** Parametric track bed and cross-sections, placement tools with ghost snapping,
  junction and turnout authoring, span-local rebuild deltas (ADR 0007), undo/redo, the full `.coypub` schema.
- **Phase 3 — BIM & analysis.** IFC 4.3 import/export against the mapping layer (ADR 0005), WBS and
  quantities, geotechnics, clearance and gauge analysis, base-point rebasing for very long corridors.
- **Phase 4 — Immersion.** OpenXR cab inspection and walkthrough, presentation rendering, export.

---

## Open findings

| # | Finding | Raised by | Action |
|---|---|---|---|
| F1 | ~~`tools/` is outside the lint path~~ | T-101 review | **closed by T-102 worker.** CI now runs `ruff check . ../tools` and `ruff format --check . ../tools`; verified empirically that `line_length = 110` reaches `tools/` |
| F2 | ~~gdUnit4 writes `client/reports/report_N/` on every run and it was not git-ignored~~ | T-102 review | **closed 2026-09-17** — `client/reports/` added to `.gitignore`, two accumulated report directories removed |
| F3 | gdUnit4's CLI refuses `--headless` without `--ignoreHeadlessMode` (all versions v5.1.1–v6.2.1). Every invocation in `CLAUDE.md`, `client/README.md` and CI now carries the flag | T-102 | Closed; noted here so no later task "cleans up" the flag |
| F4 | gdUnit4 v6.2.1 declares a Godot 4.5 floor, not the 4.4+ the spec asked for — no release satisfies both "declares 4.4+" and "compiles on 4.7.2" (v5.1.1 fails to compile: `FileAccess.get_as_text()` arity). v6.2.1 verified working empirically | T-102 | Accepted deviation. Revisit only if a Godot upgrade breaks the addon |
| F5 | ~~The in-flight disconnect test raced a 0.01 s kill against a ~2 ms `session.ping`, failing 3/3 and stranding the two tests declared after it~~ | T-103 review | **closed 2026-09-18.** Replaced by an `alignment.frame_table` request at `spacing_m = 0.002` (~9M rows, ~8 s of synchronous bake) so no reply can exist at the 0.1 s kill. Re-verified here: 29/29, three consecutive green runs, 0 orphans |
| F13 | ~~The `client` CI job has failed on **every** push since T-103 landed, while both `backend` jobs pass. Linux-only: `_kill_process_tree` uses `OS.kill(pid)` off Windows, which rejects a non-child PID (`The process N does not exist or is not a child of the calling process`), so `uv run`'s Python child survives and holds its port; then `connect_to_url` reuses a `WebSocketPeer` that is not `STATE_CLOSED`, so every reconnect returns `ERR_ALREADY_IN_USE`. Structurally invisible on Windows, where the `taskkill /T` path works~~ | CI review 2026-09-19 | **closed 2026-09-20 by T-116** (run 35531237406: 60/60, 9/9 suites, exit 0, all three jobs green). Note the causal story turned out to be one root cause with two symptoms — once the tree kill works, the socket closes promptly and the `ERR_ALREADY_IN_USE` race stops reproducing, so reverting the websocket fix alone stayed green (run 35530862952). That fix is retained as defence in depth, not because CI can prove it |
| F14 | Nobody looked at a CI result for eleven days. Two pushes went out red | process | Every commit-and-push prompt from now on must end by waiting for the run and reporting its conclusion, not just the push result |
| F15 | `frame_eval.json`'s eight stations all land on exact table rows (uniform-grid integers or geometric key stations `bake_stations` always includes), so the client's *interpolation* between rows is never pinned — only its lookup. Measured deviations were at the float32 noise floor (6.1e-5 m against a 1e-3 budget) because no interpolation error entered the comparison | T-115 | Add off-row stations with backend-computed truth when a later task next regenerates the golden. Not urgent; T-121 and T-123 will exercise interpolation visually |
| F16 | `RunTable` station error against its golden is 7.99e-4 m — **80% of the 1e-3 budget** — from float32 ULP at Kralupy's ~18 km stations. Not algorithmic, but the margin scales with corridor length | T-115 | A corridor much longer than 18 km will breach it. Revisit the budget, or carry station as float64, if M3 brings longer alignments |
| F17 | T-120's full-corridor `alignment.track_mesh` payload is **24.62 MB**, but `IpcWebSocketClient.INBOUND_BUFFER_SIZE` is **16 MiB (16.78 MB)** — one `chunk_index: null` call produces a frame the client cannot receive. My T-120 spec set an abstract ~32 MB flag threshold without checking the limit already in the codebase | T-120 review | **Resolved in spec.** T-121 now must page by `chunk_index` (~337 KB per chunk) and assert no response exceeds 8 MB. `INBOUND_BUFFER_SIZE` stays 16 MiB — paging is the designed path and is what M3's terrain streaming needs anyway |
| F20 | `Car.apply_pose` replaces the body and bogie transforms wholesale each frame, so a build-time `CarBody.position` offset would be silently wiped on the first posed frame — leaving every car centred on the rail head, i.e. uniformly half-buried, which reads as deliberate. T-122 caught this with its own tests and baked `floor_height_m + height_m/2` into the body mesh's vertices instead | T-122 | **Closed by design.** The body node's origin is now the rail head; noted in task_123 and task_124 so neither re-adds a vertical offset |
| F19 | `main.tscn` sets `ambient_light_source = 3` (`AMBIENT_SOURCE_SKY`) while `background_mode = 1` (`BG_COLOR`) and **no `Sky` resource exists**, so the `ambient_light_color` and `ambient_light_energy = 0.6` that were deliberately authored next to it contribute nothing. The scene is lit by the directional light alone, which is why the first corridor screenshot is nearly black | T-121 screenshot review | **Open** — likely a one-token fix to `AMBIENT_SOURCE_COLOR` (2). Folded into T-124, which closes M2 and owns the demo scene's presentation |
| F18 | `TrackCorridor.build` derives the chunk count client-side as `ceil(span / 250.0)`, re-implementing the backend's chunking formula. Correct today and openly flagged by T-121, but if chunking ever stops being fixed-length the client pages too few chunks and **silently renders a truncated corridor** — a failure that looks like the data ending early. Root cause is T-120's API, which offers no cheap "how many chunks?" question | T-121 review | **Open** — follow-up in task_121 adds `metadata_only` to `alignment.track_mesh`. Should land before M3 builds a second paged resource on the same pattern |
| F12 | `_stops_from_coypu` in `server/handlers.py` converts the archive's raw `[station_km, dwell_s, name]` rows into domain `Stop` tuples — format conversion in the wire layer, which `io/` should own. `io/coypu/archive.py` was not in T-114's Deliverables so it stayed in the handler, flagged in a docstring | T-114 | **Open**, low priority. Move to `CoypuProject` as a `stops()` accessor; fold into whichever later task next edits `io/coypu/` |
| F10 | ~~Every `roll` in `shared/golden/trainset_chain.json` is zero, because the Kralupy fixture's cant block is a placeholder. That golden is the **only** reference T-123's client chain is pinned to, so a client that mishandles mean-roll averaging or the Gram-Schmidt correction would still pass it. T-112 covered the behaviour in Python via the tram fixture, which Godot cannot load~~ | T-112 review | **closed 2026-09-19.** A `tram_block` key adds 5 samples, 6 car-poses with non-zero roll and a max lead/trail divergence of 0.0236 rad; the Kralupy block is byte-unchanged. My criterion 3 was wrong twice over — the fixture has no gradient, and a *constant* gradient would not have broken lead/trail symmetry either. The correction is a no-op wherever curvature and cant are constant; only a **change** between the pivots (cant ramp, clothoid, vertical curve) exercises it, which is what the ramp samples do |
| F11 | The T-112 contract's `forward = normalize(P_lead − P_trail) · d` was algebraically wrong: the layout already gives `p_lead − p_trail = d·pivot_distance`, so the extra `· d` squared the sign out and made `forward` equal `+tangent` for both directions, contradicting acceptance criterion 5. Implemented without the `· d` | T-112 | Spec error, correctly caught. Verified independently: the golden's `direction=−1` and `direction=+1` bodies at station 8260 are 180.00° apart. `task_112` and `trainset-chain.md` both corrected |
| F8 | `VehicleDynamics` mixes unit bases: traction-band coefficients are rebased to m/s on import (`b1' = 3.6·b1`, `b2' = 3.6²·b2`), but `davis_a/b/c` are copied through still calibrated for km/h, because no Davis formula is documented anywhere in the data contract. Documented in `vehicle-catalogue.md`, but two unit bases in one dataclass is a trap | T-113 | Any code evaluating Davis must treat `v` as km/h. Settle the formula and the base if Phase 3 ever computes resistance |
| F9 | The T-113 contract typed `max_tractive_force_kn` and `davis_a/b/c` as non-optional `float`, which contradicted its own acceptance criterion 7 (absent sections must leave fields empty, never zeroed). Widened to `float \| None = None` | T-113 | Spec error, correctly flagged. `max_tractive_force_kn` stays `None` for archive imports — only the CSV `Meta` section supplies it, and deriving it from the traction curve's peak would be inventing data |
| F7 | COYPU's kinematics grid overshoots the alignment: `station_m.max()` is `18185.0` against a `station_end` of `18184.971666` (its fixed 1 m simulation grid is not clipped to the exact alignment length). Harmless here, but T-112 and T-123 will pose a consist past the end and must clamp — both contracts already require it | T-111 | Standing data property, not a defect. Verify the clamp actually fires when T-123 plays the final seconds |
| F6 | `topology.validate(network)` cannot check "links whose station range leaves the alignment" — the signature sees only `alignment_id`, never the `Alignment`. The spec asked for a check the contract structurally forbids | T-110 | Spec error, correctly declined. The check belongs to a caller holding both; documented in `docs/data-contracts/entity-model.md`. Revisit when T-145 assembles projects |

---

## Definition of done — applies to every task

1. `cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest` is green.
2. `godot --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests` is green (after T-102).
3. No architecture invariant in `CLAUDE.md` or `docs/adr/` is violated; if one had to change, the ADR is
   updated in the same change and the reason is stated.
4. Cross-language behaviour is pinned to `shared/golden/*.json`, regenerated only via `tools/make_golden.py`.
5. Privacy: no vendor, infrastructure-manager or otherwise proprietary data, filename or reference enters the
   repository. `git check-ignore` is consulted before staging anything new under `tests/fixtures/`.
6. Documentation that describes the changed area is true when the task ends — including this roadmap's
   status column, which the architect session updates, not the worker.
