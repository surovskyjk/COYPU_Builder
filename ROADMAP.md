# COYPU Builder — Roadmap

Living document. The architect session maintains it; worker sessions do not edit it. Every task listed here
has (or will have) a self-contained specification in [`docs/tasks/`](docs/tasks/README.md).

**Status legend** — `done` · `in progress` · `ready` (spec written, not started) · `planned` (no spec yet)

Last reviewed: 2026-10-06 against `3ba9870` (CI run 37384504655 green on all three jobs; F18 landed as `8c5cd79`). **M2 is complete.** F25 is answered and Phase 1 is re-scoped (ADR 0009): M3 becomes terrain and imagery import with explicit settings and stable GUIDs, and a new M5 adds buildings, vegetation and water bodies. ADR 0010 plans the API for mods and AI: its seams and security now (T-126), the MCP server in M4 (T-146), mods in Phase 2.

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
real terrain under an orthophoto, among buildings, vegetation and water bodies, and play or scrub a
correctly-articulated trainset along it at 60 fps with orbit, wayside and cab cameras. Every imported source
records its settings, licence and provenance, and every imported object keeps one GUID through re-import and
save. Read-only: no authoring, no editing, no IFC.

**Scope decisions (approved 2026-09-17; 1, 2 and 5–8 amended or added 2026-10-06, ADR 0009).**

1. **Persistence** — a *partial* `.coypub` lands in M4: `project`, `base_point`, `layer`, `run` and
   `source_dataset` tables. M5 brings ADR 0005's `entity` and `blob` tables forward for context features only.
   `history`, `wbs`, `ifc_map` and the track entity set wait for Phase 2.
2. **GIS** — every source arrives through one provider interface: **local files first**, then generic
   low-resolution open-data providers (a fast "generic landscape" anywhere) and national services such as
   ČÚZK. Live fetching must never be on the CI path; CI uses synthetic sources generated in code.
3. **Track fidelity** — two swept rails + `MultiMesh` sleepers + one default ballast prism. The parametric
   cross-section system is Phase 2; `domain/sections/` stays empty until authoring defines its requirements.
4. **Tram fixture** — a synthetic tram loop with one junction is seeded in **M1**, not at the Phase 2
   boundary, so that mode-specific assumptions never become cheap to introduce.
5. **Import settings and identity** — every import is *inspect, confirm settings, import*. Settings cover
   horizontal CRS (EPSG), vertical datum, axis order and sign, units, clip region, point-class filter and
   display resolution, and are stored with the result. A source with no CRS is refused rather than guessed.
   Every imported object gets a GUID at first import; re-import matches on the source's own key (OSM id,
   RÚIAN code, file + feature index) and keeps it. Every source gets a `SourceDataset` record with licence
   and attribution.
6. **Terrain representation** — the backend's analysis surface is a TIN over the original points (or the
   native grid for rasters), clipped to the corridor buffer; the client displays heightfield tiles with LOD
   derived from it. Display tiles are never used for ground queries.
7. **Terrain formats in M3** — LAS/LAZ point clouds, GeoTIFF/COG and LandXML TIN surfaces. ASCII XYZ, ESRI
   ASCII grid and DXF are deferred: each is one more reader behind the same interface, specified when a real
   file needs it.
8. **Context models** — buildings, vegetation and water bodies are a Phase 1 milestone (M5) after M4, so they
   arrive with a layers panel to toggle them and a document to save them in. Each class imports from a file
   or from an open-data source.

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

### P1.M2 — Track in 3D · `done`

*Goal:* the first real demo — a train running on rails.

| Task | Title | Status | Depends on | Spec |
|---|---|---|---|---|
| T-120 | Backend track mesh baker: rails + ballast prism, tile-local chunking | `done` (2026-09-20, `e8810f7`) | T-110 | [task_120](docs/tasks/task_120_track_mesh_baker.md) |
| T-121 | Client track scene: chunk instancing, sleeper `MultiMesh`, LOD | `done` (2026-09-21, `2f14f50`); F18 follow-up done 2026-10-06 | T-115, T-120 | [task_121](docs/tasks/task_121_client_track_scene.md) |
| T-122 | Client vehicle scene: procedural car from the catalogue spec, consist assembly | `done` (2026-09-21, `5a2ead7`) | T-113, T-115 | [task_122](docs/tasks/task_122_client_vehicle_scene.md) |
| T-123 | Client playback: transport, scrub, speed, trainset chain evaluation | `done` (2026-09-21, `0899c35`) | T-112, T-115, T-122 | [task_123](docs/tasks/task_123_client_playback.md) |
| T-124 | Cameras: manager, orbit, wayside, XR-ready cab rig | `done` (2026-09-21, `403b879`) | T-121, T-123 | [task_124](docs/tasks/task_124_cameras.md) |

**Exit criteria — met, with one amended (2026-09-21).** The Kralupy corridor renders as rails, sleepers and
ballast with no vertex shimmer at any zoom · **a consist** plays back along it at a locked 60 fps in all three
camera modes, with bogies on the rails and bodies chording the curves · the client chain matches
`shared/golden/trainset_chain.json`.

The original wording said *three-car* consist. That was my assumption, not something the data produces: the
Kralupy archive's vehicle is `DMU BR 650 (CD 840)`, a **single-car** RS1 railcar, so `import.coypu` correctly
yields `car_count() == 1`. Multi-car geometry is proven at catalogue level (T-122, T-123's goldens) but has
never run end to end through a live import — see F23.

### P1 workflow tooling

| Task | Title | Status | Depends on | Spec |
|---|---|---|---|---|
| T-125 | Visual capture harness: scripted screenshots and frame-time statistics for the reviewer agent | `ready` | T-124 | [task_125](docs/tasks/task_125_visual_capture.md) |
| T-126 | API foundations and IPC hardening (ADR 0010 decisions 1–3): stability tiers, discovery record, a strong token that is always required, `Origin` rejection | `ready` | T-116, T-138 | [task_126](docs/tasks/task_126_api_foundations.md) |

How work runs since 2026-10-06 is described in [`docs/tasks/README.md`](docs/tasks/README.md#how-tasks-run):
- a fresh `coypu-worker` subagent per task;
- the architect's independent verification;
- a fresh `coypu-reviewer` subagent;
- commits through the user's Commits chat;
- no push without the user's go-ahead.

### P1.M3 — Terrain & imagery · `planned` (specs next)

*Goal:* the corridor sits on real ground, imported cleanly from files or open data, with every source
traceable (ADR 0009).

| Task | Title | Status | Depends on |
|---|---|---|---|
| T-138 | Corridor envelope export: a buffer polygon around the alignments as GeoJSON or Shapefile, for selecting download areas (e.g. in the ČÚZK Geoprohlížeč); also the shared corridor-clip geometry — [task_138](docs/tasks/task_138_corridor_envelope.md) | `done` (2026-10-06; Kralupy at 250 m: 9.2885 km², 922 / 356 vertices; review hardened file writes per ADR 0010) | — |
| T-139 | ČÚZK ATOM client: sheet index from the service feeds, envelope → SM5 sheet selection, cached and resumable download (DMR 5G, DMR 4G, DMP 1G, orthophoto), as a CLI first — [task_139](docs/tasks/task_139_cuzk_atom_download.md) | `ready` | T-138 |
| T-130 | Import framework: `import.inspect` / `import.run`, `ImportSettings`, `SourceDataset` provenance, GUID assignment, synthetic terrain fixtures | `planned` | T-101, T-110 |
| T-131 | Terrain readers: LAS/LAZ, GeoTIFF/COG, LandXML TIN surface; multi-sheet merge, clip to corridor, reprojection | `planned` | T-130, T-138 |
| T-132 | Terrain surface and tiling: TIN/grid analysis surface, heightfield display tiles with LOD, paged `terrain.*` methods | `planned` | T-131, F18 follow-up |
| T-133 | Terrain providers behind the import framework: ČÚZK DMR 5G / DMR 4G through T-139's client, and a low-resolution global DEM; disk cache, corridor bounding, precedence | `planned` | T-132, T-139 |
| T-134 | Imagery: local ortho files → tiles; ČÚZK orthophoto through T-139's client; generic WMS/WMTS providers behind the same interface | `planned` | T-130, T-132, T-139 |
| T-135 | Client terrain scene: tile streaming by camera distance, ortho texturing, source seams, attribution overlay | `planned` | T-121, T-132 |
| T-136 | Client import dialog: inspect preview, settings form (EPSG search, vertical datum, clip, resolution), progress, report | `planned` | T-130, T-140 |
| T-137 | Ground sampling: ground elevation on the frame table, cut/fill strip, longitudinal profile | `planned` | T-132 |

T-140 (UI theme and dock, M4) depends only on T-103 and is pulled forward into the M3 window, because T-136
needs it.

**Run order:** F18 follow-up → T-138 → T-139 → T-125 → T-126 → T-130 → T-131 → T-132 → T-140, then T-133 to
T-137. T-138 and T-139 run first because the user needs the remaining DMR 5G sheets: T-138 for a manual
selection in the Geoprohlížeč, T-139 to download them automatically.

**ČÚZK download service (checked 2026-10-06).** ČÚZK publishes INSPIRE ATOM download services, free of charge,
organised by SM5 map sheet.

- **Feeds:** each dataset has one service feed, for example `https://atom.cuzk.gov.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml`,
  about 24 MB with 16,301 sheet entries. Each entry carries:
  - the sheet code and name, e.g. `KRAV82`, "Kralupy nad Vltavou 8-2";
  - an `updated` timestamp;
  - the sheet outline as a WGS84 `georss:polygon`.

  So the feed itself is the sheet index, and selecting sheets by envelope needs no other data.
- **Files:** each sheet's dataset feed links one ZIP, e.g.
  `https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/KRAV82.zip`. The server sends `Content-Length`, `ETag`
  and `Last-Modified`, and accepts byte ranges, so downloads can be cached, validated and resumed. `ETag` and
  `updated` also tell a re-import whether a sheet changed.
- **Datasets:**
  - DMR 5G — LAZ, about 2.3 MB per sheet;
  - DMR 4G — GeoTIFF, 5 m grid;
  - DMP 1G — LAZ, a surface model that includes buildings and vegetation;
  - orthophoto (`ORTOFOTO`) — JPEG with world files, about 58 MB per sheet, so roughly 0.9 GB for the Kralupy
    corridor. Orthophoto downloads must therefore be opt-in, show progress and resume.
- **Licence:** every feed entry states "žádné podmínky neplatí" (no conditions apply). ČÚZK publishes its spatial
  data as open data under CC BY 4.0 (since 1 July 2023), so the source is attributed as "© ČÚZK" (ADR 0009,
  decision 9).
- **Rules for T-139:**
  - fetch feeds with conditional GETs;
  - download sequentially, or at most two at a time;
  - CI uses a few-entry feed excerpt as a fixture and never touches the network. T-130's readers
and providers register through entry points (ADR 0010, decision 4).

**Real data for manual verification** lives outside the repository in `D:\COYPU_Builder\Data`. The first
sample is ČÚZK DMR 5G sheet KRAV82 (`Terrain_Samples\KRAV82.zip`), which covers stations 12.7–17.5 km (see
F25). The full corridor needs 12–16 such sheets.

**Generic-source candidates**, each licence-checked in its spec before it ships: Copernicus DEM (GLO-30 /
GLO-90) for terrain; ČÚZK services for Czech terrain and orthophoto; a global imagery source is still an open
licence question. OpenStreetMap and ČÚZK ZABAGED / RÚIAN serve M5.

**Exit criteria.** KRAV82 and a synthetic multi-sheet set import through inspect → settings → import, with
the missing CRS supplied explicitly and recorded · the generic terrain fills everywhere detailed data is
absent, with the boundary visible · the corridor drapes correctly, and the rail-to-ground profile over
stations 12.7–17.5 km is plausible on manual review · terrain and orthophoto stream around the camera without
a frame-time spike · re-importing a source keeps every GUID and reports no changes · a cut/fill strip reads out
along stations · nothing on the CI path touches the network or a real data file.

### P1.M4 — Shell, view modes & persistence · `planned`

*Goal:* Phase 1 becomes a product rather than a demo.

| Task | Title | Status | Depends on |
|---|---|---|---|
| T-140 | UI theme, top bar, floating bottom dock skeleton, and an *Export corridor envelope* action over T-138 (pulled forward into the M3 window) | `planned` | T-103, T-138 |
| T-141 | Timeline bar: transport, scrub, stop markers, station/time/speed readout | `planned` | T-123, T-140 |
| T-142 | Layers panel wired to `layer_state`, including one layer per imported source | `planned` | T-115, T-140 |
| T-143 | Inspector panel with progressive disclosure, including a source's settings, licence and provenance | `planned` | T-115, T-140 |
| T-144 | View modes: realistic, wireframe, x-ray, diagnostics (curvature / cant / gradient ramps) | `planned` | T-121 |
| T-145 | Partial `.coypub`: schema, repository, `project.save` / `project.open`, `source_dataset` table | `planned` | T-110, T-114, T-130 |
| T-146 | MCP server `coypu-builder-mcp` (ADR 0010, decision 6): attaches through the discovery record, `stable` methods as tools, read-only by default, a screenshot tool | `planned` | T-125, T-126, T-145 |

**Exit criteria.** Track, run, terrain and imagery work end to end on the Kralupy corridor from a cold start ·
a session, including its imported sources and their settings, survives save and reopen · the diagnostics view
mode renders curvature, cant and gradient as colour ramps, doubling as a visual regression check on the
kernel.

### P1.M5 — Context models · `planned`

*Goal:* the corridor sits among its surroundings. Buildings, vegetation and water bodies import from a file or
from an open-data source, through M3's import framework, and every object keeps its GUID through re-import
and save (ADR 0009).

| Task | Title | Status | Depends on |
|---|---|---|---|
| T-150 | Vector import core: GeoPackage, GeoJSON, Shapefile and OSM readers into GUID-tagged context features, draped on the terrain | `planned` | T-130, T-132 |
| T-151 | Buildings: footprints → LOD1 extrusion (height from attributes, levels, the ČÚZK surface model DMP 1G minus the terrain DMR 5G, or a default); CityGML / CityJSON LOD1–2 files | `planned` | T-150, T-139 |
| T-152 | Vegetation: woodland polygons → instanced scatter, single trees from points, low-poly proxies by class | `planned` | T-150 |
| T-153 | Water bodies: polygons → water surfaces at a terrain-derived level; streams and rivers from lines | `planned` | T-150 |
| T-154 | Open-data providers for context: OpenStreetMap, ČÚZK ZABAGED / RÚIAN; cached, bounded, licence-tagged | `planned` | T-150 |
| T-155 | Client context scene: one layer per class, instancing, distance culling, attribution | `planned` | T-142, T-151, T-152, T-153 |
| T-156 | Re-import and lifecycle: match on source keys, keep GUIDs, added/changed/removed report; context features in `.coypub` | `planned` | T-145, T-150 |

**Exit criteria.** Around the Kralupy corridor, buildings, vegetation and water bodies import both from a
file and from an open-data source, and render at 60 fps with per-class layer toggles · every object's GUID
survives save, reopen and re-import of an updated source · attribution for every contributing source is on
screen · nothing on the CI path touches the network · the Phase 1 exit definition is met end to end from a
cold start.

> Tasks are specified one milestone ahead of execution, because a spec written against guessed inputs costs
> more to correct than to write late. M3 is specified next. M4 and M5 wait until M3's import framework (T-130)
> and terrain surface API (T-132) have landed.

---

## Beyond Phase 1 (direction, not commitment)

- **Phase 2 — Authoring.** Parametric track bed and cross-sections, placement tools with ghost snapping,
  junction and turnout authoring, span-local rebuild deltas (ADR 0007), undo/redo, the full `.coypub` schema.
  Mods (ADR 0010, decisions 4–5): backend method plugins under `x.<plugin>.*`, client mods as resource packs
  through a `ModApi`, a sample mod and an SDK page.
- **Phase 3 — BIM & analysis.** IFC 4.3 import/export against the mapping layer (ADR 0005), WBS and
  quantities, geotechnics, clearance and gauge analysis, base-point rebasing for very long corridors. Context
  objects export with their M5 GUIDs as `GlobalId`s; IFC files become one more context source.
- **Deferred import formats.** ASCII XYZ and ESRI ASCII grid terrain, DXF points and 3D faces, textured
  CityGML LOD3, raw point-cloud display. Each is a reader behind the M3/M5 interfaces.
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
| F26 | Prompt audit 2026-09-23 (`/claude-api prompt-audit`, judged against Claude Sonnet 5, the worker model). Applied: `CLAUDE.md` uv fallback now names the Packages path that exists, and its lint line matches CI (`ruff check . ../tools`, `ruff format --check . ../tools`); the contract and Deliverables rules in `CLAUDE.md` and `docs/tasks/README.md` now say *make the smallest change the acceptance criteria require and report it first*, matching six deviations the reviews endorsed (T-101, T-110 ×2, T-112, T-113, T-124) instead of the old *stop* / *touch only*; the F18 follow-up drops its stale absolute test counts and history asides and gains a Verification block with the backend lint gate. **Template notes for M3/M4 specs:** reserve bold for instructions workers have actually got wrong; state current rules without correction history or F-number archaeology inside Contracts; make every Out-of-scope entry name the task that owns the work; never put absolute test counts in acceptance criteria — use "no previously passing test removed, skipped or weakened" | architect | **Applied.** Memory corrected too (stale Phase 0 status, and a vendor-fixture clause that no longer matched the tree). The commit-script recommendation was superseded on 2026-10-06: the architect now sends commit prompts straight into the user's Commits chat (see `docs/tasks/README.md`, "How tasks run") |
| F28 | IPC security is adequate for a local tool, but not for an API that mods and AI clients will attach to. (1) `client/core/backend.gd:117` builds the token from `Time.get_ticks_usec()` and `randi()`, which is not cryptographically random. (2) `server/handlers.py:135` skips the check when the expected token is empty, and `tools/run_dev.ps1` defaults it to `"dev"`. (3) `client/ipc/process_supervisor.gd:29` passes `--token` on the command line, where other local processes can read it. (4) `server/app.py` does not reject browser `Origin` headers. (5) The comparison is not constant-time. The listener binds to `127.0.0.1` and the token is never logged — both correct | architect, 2026-10-06 | **Open** — T-126 owns all five, per ADR 0010 decisions 2–3. Must land before T-146 exposes anything to an external client |
| F27 | The 2026-10-06 renumbering moved the client terrain scene from T-132 to T-135. Two worker-written comments still name T-132 as a `LayerState` subscriber: `client/domain_mirror/layer_state.gd:4` (which also calls it "vehicles") and `client/README.md:78` | architect, 2026-10-06 | **Open**, trivial. T-135's Deliverables will include both lines |
| F25 | ~~**M3 is blocked on data.** No DEM or imagery file exists in or near the repo, and `.tif`/`.tiff` are git-ignored, so a fixture cannot be committed~~ | architect, 2026-09-21 | **closed 2026-10-06.** Real data lives outside the repo in `D:\COYPU_Builder\Data`; CI uses synthetic sources generated in code. First sample: ČÚZK DMR 5G sheet KRAV82, LAS 1.4 with LASzip, 2.5 × 2.0 km, 433,396 points (about one per 11.5 m², roughly 3.4 m spacing), Z 167.9–235.4 m. It covers corridor stations 12,720–17,545 m, where the rail lies at 170.6–180.3 m, so the heights are consistent. **The file declares no CRS** — its only VLR is LASzip's — and EPSG:5514 is inferred from the value range alone. That is why ADR 0009 refuses an import without an explicit CRS. The scope answers also re-scoped M3 and added M5 |
| F23 | The live `.coypu` demo path produces a **one-car** consist, because the Kralupy archive's vehicle (`DMU BR 650 (CD 840)`) is a single RS1 railcar in the catalogue. Inter-car geometry — coupling gaps, car-to-car spacing, the body chord across a coupled pair — has therefore never been exercised end to end against a live backend, only in catalogue-level tests and the golden | T-124 review | **Open.** `trainset.create(spec_key, units=3)` already exists; fold a multi-unit consist into T-141 or T-143 where consist selection naturally lives |
| F24 | The synthetic tram network is a Python test fixture with **no export path the client can reach** — `grep` finds no tram usage anywhere under `client/`. Several acceptance criteria across T-121, T-123 and T-124 asked for verification on the tram alignment; each was satisfied with a synthetic unit test instead, correctly flagged each time. The client has never loaded a 1000 mm-gauge canted alignment | T-124 review (accumulated since T-110) | **Open.** Export `tram_loop.py` to a LandXML file the client can `import.landxml`. Blocks nothing in Phase 1; blocks tram authoring in Phase 2 |
| F21 | `shared/golden/trainset_chain.json` carries each pivot's station and position but **not its orientation**, so the client's bogie *basis* is not pinned cross-language. T-123 filled the gap by computing orientations once from `domain.lrs.frames()` and embedding them as literal fixtures in its own test — correct values, but self-generated, so a wrong bogie basis would still pass | T-123 review | **Open.** Add pivot `godot_quaternion_xyzw` to both golden blocks next time `make_golden.py` is touched. Until then the bogie basis is unpinned across the language boundary |
| F22 | The 1e-5 quaternion-component budget in T-123's criterion 1 is tight for a float32 client reproducing a float64 reference through normalize → Gram-Schmidt → basis → quaternion. 26 of 27 car-samples fit; the clamped end-of-run car 0 measured 1.9e-5. Inspecting the golden, that car's chord is compressed to 6.5 m by clamping (against 17.5 m for cars 1 and 2) but is nowhere near degenerate, so this is ordinary float32 accumulation rather than an ill-conditioned sample — the budget was simply optimistic | T-123 review | Accept the scoped 3e-5 tolerance. Use **3e-5** for quaternion components in future client-vs-golden criteria rather than treating this as an anomaly |
| F20 | `Car.apply_pose` replaces the body and bogie transforms wholesale each frame, so a build-time `CarBody.position` offset would be silently wiped on the first posed frame — leaving every car centred on the rail head, i.e. uniformly half-buried, which reads as deliberate. T-122 caught this with its own tests and baked `floor_height_m + height_m/2` into the body mesh's vertices instead | T-122 | **Closed by design.** The body node's origin is now the rail head; noted in task_123 and task_124 so neither re-adds a vertical offset |
| F19 | ~~`main.tscn` sets `ambient_light_source = 3` (`AMBIENT_SOURCE_SKY`) while `background_mode = 1` (`BG_COLOR`) and **no `Sky` resource exists**, so the `ambient_light_color` and `ambient_light_energy = 0.6` that were deliberately authored next to it contribute nothing~~ | T-121 screenshot review | **closed by T-124** (in its uncommitted change set): `ambient_light_source = 2`. T-124 also removed the 4 km placeholder ground plane and the free-look camera; M3's terrain replaces the former |
| F18 | ~~`TrackCorridor.build` derives the chunk count client-side as `ceil(span / 250.0)`, re-implementing the backend's chunking formula. Correct today and openly flagged by T-121, but if chunking ever stops being fixed-length the client pages too few chunks and **silently renders a truncated corridor** — a failure that looks like the data ending early. Root cause is T-120's API, which offers no cheap "how many chunks?" question~~ | T-121 review | **closed 2026-10-06** by the task_121 follow-up. `alignment.track_mesh` gained `metadata_only` (Kralupy list: 35.7 KB, 0.143 s), and `TrackCorridor.build` pages the backend's list; no chunk-count arithmetic is left in the client. The independent review's three findings (stale README, weak truncation test, a false test comment) were fixed in a second round; 144 pytest, 138/138 gdUnit4 |
| F12 | `_stops_from_coypu` in `server/handlers.py` converts the archive's raw `[station_km, dwell_s, name]` rows into domain `Stop` tuples — format conversion in the wire layer, which `io/` should own. `io/coypu/archive.py` was not in T-114's Deliverables so it stayed in the handler, flagged in a docstring | T-114 | **Open**, low priority. Move to `CoypuProject` as a `stops()` accessor; fold into whichever later task next edits `io/coypu/` |
| F10 | ~~Every `roll` in `shared/golden/trainset_chain.json` is zero, because the Kralupy fixture's cant block is a placeholder. That golden is the **only** reference T-123's client chain is pinned to, so a client that mishandles mean-roll averaging or the Gram-Schmidt correction would still pass it. T-112 covered the behaviour in Python via the tram fixture, which Godot cannot load~~ | T-112 review | **closed 2026-09-19.** A `tram_block` key adds 5 samples, 6 car-poses with non-zero roll and a max lead/trail divergence of 0.0236 rad; the Kralupy block is byte-unchanged. My criterion 3 was wrong twice over — the fixture has no gradient, and a *constant* gradient would not have broken lead/trail symmetry either. The correction is a no-op wherever curvature and cant are constant; only a **change** between the pivots (cant ramp, clothoid, vertical curve) exercises it, which is what the ramp samples do |
| F11 | The T-112 contract's `forward = normalize(P_lead − P_trail) · d` was algebraically wrong: the layout already gives `p_lead − p_trail = d·pivot_distance`, so the extra `· d` squared the sign out and made `forward` equal `+tangent` for both directions, contradicting acceptance criterion 5. Implemented without the `· d` | T-112 | Spec error, correctly caught. Verified independently: the golden's `direction=−1` and `direction=+1` bodies at station 8260 are 180.00° apart. `task_112` and `trainset-chain.md` both corrected |
| F8 | `VehicleDynamics` mixes unit bases: traction-band coefficients are rebased to m/s on import (`b1' = 3.6·b1`, `b2' = 3.6²·b2`), but `davis_a/b/c` are copied through still calibrated for km/h, because no Davis formula is documented anywhere in the data contract. Documented in `vehicle-catalogue.md`, but two unit bases in one dataclass is a trap | T-113 | Any code evaluating Davis must treat `v` as km/h. Settle the formula and the base if Phase 3 ever computes resistance |
| F9 | The T-113 contract typed `max_tractive_force_kn` and `davis_a/b/c` as non-optional `float`, which contradicted its own acceptance criterion 7 (absent sections must leave fields empty, never zeroed). Widened to `float \| None = None` | T-113 | Spec error, correctly flagged. `max_tractive_force_kn` stays `None` for archive imports — only the CSV `Meta` section supplies it, and deriving it from the traction curve's peak would be inventing data |
| F7 | COYPU's kinematics grid overshoots the alignment: `station_m.max()` is `18185.0` against a `station_end` of `18184.971666` (its fixed 1 m simulation grid is not clipped to the exact alignment length). Harmless here, but T-112 and T-123 will pose a consist past the end and must clamp — both contracts already require it | T-111 | Standing data property, not a defect. Verify the clamp actually fires when T-123 plays the final seconds |
| F6 | `topology.validate(network)` cannot check "links whose station range leaves the alignment" — the signature sees only `alignment_id`, never the `Alignment`. The spec asked for a check the contract structurally forbids | T-110 | Spec error, correctly declined. The check belongs to a caller holding both; documented in `docs/data-contracts/entity-model.md`. Revisit when T-145 assembles projects |

---

## Definition of done — applies to every task

1. `cd backend && uv run ruff check . ../tools && uv run ruff format --check . ../tools && uv run pytest` is green.
2. `godot --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode` is green,
   and the latest CI run on `master` is green on all three jobs.
3. No architecture invariant in `CLAUDE.md` or `docs/adr/` is violated; if one had to change, the ADR is
   updated in the same change and the reason is stated.
4. Cross-language behaviour is pinned to `shared/golden/*.json`, regenerated only via `tools/make_golden.py`.
5. Privacy: no vendor, infrastructure-manager or otherwise proprietary data, filename or reference enters the
   repository. `git check-ignore` is consulted before staging anything new under `tests/fixtures/`.
6. Documentation that describes the changed area is true when the task ends — including this roadmap's
   status column, which the architect session updates, not the worker.
