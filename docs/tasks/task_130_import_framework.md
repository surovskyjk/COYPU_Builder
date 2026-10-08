# T-130 — Import framework: settings, source records, GUIDs and pluggable readers

**Milestone:** P1.M3 · **Depends on:** T-126 (stability tiers), T-138 (corridor envelope) · **Blocks:** T-131,
T-132, T-133, T-134, T-136, T-145

## Context

ADR 0009 decides how every context source — terrain, imagery, and later buildings, vegetation and water —
enters a project:

- **inspect, confirm settings, import;**
- explicit coordinate settings, and a source with no CRS is refused rather than guessed;
- a `SourceDataset` record with licence, attribution and provenance;
- one GUID per imported object, kept on re-import by matching the object's own source key.

ADR 0010 (decision 4, confirmed by the user on 2026-10-08) adds that readers and providers are pluggable
through Python entry points, but a plugin is loaded only if the user lists it in an allow-list.

This task builds that framework and nothing format-specific. Real readers (LAS/LAZ, GeoTIFF, LandXML TIN) are
T-131, the analysis surface and display tiles are T-132, and providers are T-133 and T-134. To exercise the
framework end to end without them, this task adds a synthetic terrain generator, which every later M3 task
reuses, and a test-only reader.

## Preconditions

- T-126: `MethodSpec.stability` is required; new methods are `experimental`.
- T-138: `io/gis/envelope.py: corridor_envelope(alignments, EnvelopeOptions)`.
- `domain/model/ids.py: EntityId, new_id()` (uuid4 hex); `domain/crs.py: ProjectCRS` (`from_crs`, `to_crs`,
  `is_krovak`).
- `io/landxml/dialects.py: resolve_coordinates(tokens, crs, coordinate_order)`, which holds the Křovák sign
  convention (positive X south / Y west tokens ⇒ `E = −Y`, `N = −X`). Reuse it; do not restate the rule.
- `server/session.py: ProjectState`; `E_CRS_REQUIRED`, `E_BAD_PARAMS`, `E_NOT_FOUND`, `E_NO_PROJECT` exist.

Read before starting: `docs/adr/0009-context-data-import.md`, `docs/adr/0010-extensibility-api-plugins-mcp.md`
(decisions 2 to 4), `docs/data-contracts/entity-model.md` (identity), `docs/data-contracts/corridor-envelope.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/sources.py` | new — settings, clip regions, source records, `reconcile`; pure |
| `backend/src/coypu_builder/io/sources/__init__.py` | new |
| `backend/src/coypu_builder/io/sources/registry.py` | new — reader and provider protocols, registry, plugin discovery |
| `backend/src/coypu_builder/io/sources/runner.py` | new — inspect and import orchestration, CRS resolution, clip geometry |
| `backend/src/coypu_builder/config.py` | new — per-user configuration (`config.toml`) |
| `backend/src/coypu_builder/server/session.py` | `ProjectState.sources` |
| `backend/src/coypu_builder/protocol/messages.py` | DTOs and five methods (below), all `experimental` |
| `backend/src/coypu_builder/server/handlers.py` | handlers |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/import-framework.md` | new — settings, records, identity rules, plugin configuration |
| `backend/tests/fixtures/synthetic/terrain.py` | new — analytic synthetic terrain and a test-only file format |
| `backend/tests/test_import_framework.py` | new |

## Contract

### Domain — `domain/sources.py` (numpy and domain modules only)

```python
class SourceKind(StrEnum):
    TERRAIN = "terrain"; IMAGERY = "imagery"; BUILDINGS = "buildings"; VEGETATION = "vegetation"; WATER = "water"

@dataclass(frozen=True)
class CorridorClip:
    buffer_m: float
    alignment_ids: tuple[EntityId, ...] = ()    # empty = every alignment in the project
    station_from: float | None = None           # only with exactly one alignment
    station_to: float | None = None

@dataclass(frozen=True)
class BBoxClip:
    min_e: float; min_n: float; max_e: float; max_n: float   # project CRS

ClipRegion = CorridorClip | BBoxClip

@dataclass(frozen=True)
class ImportSettings:
    horizontal_crs: str | None = None     # EPSG code, WKT or PROJ; None = the source's declared CRS
    vertical_crs: str | None = None       # e.g. "EPSG:5705"; recorded only — heights are not transformed in Phase 1
    coordinate_order: str = "auto"        # "auto" | "EN" | "NE", as io/landxml/dialects.py
    linear_unit_m: float = 1.0            # source unit in metres
    clip: ClipRegion | None = None
    point_classes: tuple[int, ...] | None = None   # point-cloud classification codes to keep; None = all
    display_resolution_m: float | None = None
    nodata: float | None = None

@dataclass(frozen=True)
class FileOrigin:
    paths: tuple[str, ...]                # absolute
    sha256: tuple[str, ...]

@dataclass(frozen=True)
class ProviderOrigin:
    provider_id: str
    query: str                            # canonical JSON of the request

@dataclass(frozen=True)
class ObjectRecord:
    key: str                              # the object's own source key
    id: EntityId
    content_hash: str

@dataclass(frozen=True)
class SourceDataset:
    id: EntityId
    kind: SourceKind
    name: str
    format_id: str
    origin: FileOrigin | ProviderOrigin
    settings: ImportSettings              # exactly as confirmed
    declared_crs: str | None              # what the source itself said
    effective_crs: str                    # what was used
    transformation: str                   # pyproj's description, or "none" for an identical CRS
    transformation_accuracy_m: float | None
    licence: str
    attribution: str
    imported_at: str                      # ISO 8601 UTC
    tool_version: str
    objects: tuple[ObjectRecord, ...]

@dataclass(frozen=True)
class Reconciliation:
    added: tuple[str, ...]; changed: tuple[str, ...]; unchanged: tuple[str, ...]; removed: tuple[str, ...]
    objects: tuple[ObjectRecord, ...]     # the new list: matched keys keep their GUID

def reconcile(previous: Sequence[ObjectRecord], current: Mapping[str, str],
              make_id: Callable[[], EntityId] = new_id) -> Reconciliation: ...
    # current: key -> content hash. A matched key keeps its GUID whether or not its hash changed.
```

**GUIDs are never derived from keys.** Two separate imports of the same file produce two sources with
disjoint GUIDs; only `reimport_of` reconciles.

### Readers, providers and plugins — `io/sources/registry.py`

```python
@dataclass(frozen=True)
class InspectReport:
    format_id: str
    kind: SourceKind
    declared_crs: str | None
    extent: tuple[float, float, float, float] | None   # in the declared CRS, or raw coordinates if none
    counts: Mapping[str, int]                          # e.g. {"points": 433396}
    suggested: ImportSettings
    licence_hint: str | None
    warnings: tuple[str, ...]

@dataclass(frozen=True)
class ImportedObject:
    key: str
    content_hash: str
    payload: object                       # reader-specific; T-131 defines terrain payloads

class SourceReader(Protocol):
    format_id: str
    kinds: frozenset[SourceKind]
    suffixes: tuple[str, ...]
    def sniff(self, path: Path) -> bool: ...
    def inspect(self, path: Path) -> InspectReport: ...
    def read(self, path: Path, settings: ImportSettings, transform: CrsTransform,
             clip_polygon: object | None) -> Iterable[ImportedObject]: ...

class SourceProvider(Protocol):
    provider_id: str
    kinds: frozenset[SourceKind]
    licence: str
    attribution: str
    def inspect(self, query: Mapping[str, Any]) -> InspectReport: ...
    def fetch(self, query: Mapping[str, Any], cache_dir: Path) -> list[Path]: ...   # files then go through readers

def register_reader(reader: SourceReader) -> None: ...
def register_provider(provider: SourceProvider) -> None: ...
def reader_for(path: Path, format_id: str | None = None) -> SourceReader: ...   # ValueError if none fits

@dataclass(frozen=True)
class PluginInfo:
    name: str                  # entry-point name
    group: str                 # "coypu_builder.readers" | "coypu_builder.providers"
    distribution: str
    version: str
    state: str                 # "loaded" | "available" (not allow-listed) | "failed"
    error: str | None

def load_plugins(config: UserConfig) -> list[PluginInfo]: ...
```

- A discovered plugin that is not allow-listed is **never imported**: no `EntryPoint.load()` call.
- A plugin that raises on load is recorded as `failed` with its error, and loading continues.
- Built-in readers register directly, not through entry points.

### Per-user configuration — `config.py`

```python
@dataclass(frozen=True)
class UserConfig:
    plugins_allow: tuple[str, ...] = ()

def config_path() -> Path: ...    # Windows %APPDATA%\COYPU Builder\config.toml;
                                  # elsewhere $XDG_CONFIG_HOME/coypu-builder/config.toml (~/.config/...)
def load_user_config(path: Path | None = None) -> tuple[UserConfig, tuple[str, ...]]: ...   # config, warnings
```

```toml
[plugins]
allow = ["example-reader"]        # entry-point names
```

A missing file gives the defaults. A malformed file gives the defaults plus a warning, never an exception.
Read it with the standard library's `tomllib`.

### Orchestration — `io/sources/runner.py`

- `resolve_transform(settings, declared_crs, project_crs) -> CrsTransform`:
  - uses `settings.horizontal_crs`, or failing that `declared_crs`; with neither it raises a `CrsRequired`
    error, which the handler maps to `E_CRS_REQUIRED`;
  - applies `coordinate_order` through `dialects.resolve_coordinates`, so the Křovák sign convention has one
    home;
  - applies `linear_unit_m`;
  - records pyproj's operation description and accuracy;
  - warns when a declared CRS is overridden.
- Clip: `CorridorClip` resolves through T-138's `corridor_envelope`, `BBoxClip` to a box, both in the project
  CRS. Readers receive the polygon.
- Import:
  - hash every input file (SHA-256);
  - run the reader;
  - `reconcile` against `reimport_of`'s objects when given, or against nothing;
  - build the `SourceDataset` and store it with its payloads in `ProjectState.sources`.

### Protocol (all `experimental`)

| Method | Params | Result |
|---|---|---|
| `import.inspect` | `path` (absolute), `format_id?` | the `InspectReport` fields, `suggested_settings` as an `ImportSettingsDTO` |
| `import.run` | `path`, `kind`, `settings: ImportSettingsDTO`, `name?`, `format_id?`, `reimport_of?` | `source_id`, `name`, `effective_crs`, `transformation`, `transformation_accuracy_m`, counts `added` / `changed` / `unchanged` / `removed`, `warnings` |
| `source.list` | — | one summary per source: `id`, `kind`, `name`, `format_id`, `object_count`, `imported_at`, `attribution` |
| `source.get` | `source_id` | the full `SourceDataset` as a DTO, objects included |
| `plugins.list` | — | every `PluginInfo` |

`ImportSettingsDTO` mirrors `ImportSettings`. Its `clip` is a msgspec tagged union of `CorridorClipDTO` and
`BBoxClipDTO`, or null. Paths must be absolute. A `reimport_of` must name a source of the same kind and
format.

### Synthetic terrain — `tests/fixtures/synthetic/terrain.py`

This is an analytic surface that every M3 test can check exactly:
- a gentle plane plus one Gaussian hill and one linear ridge;
- placed over Kralupy stations 12.7–17.5 km (the KRAV82 area);
- level with the rail there to within a few metres, so T-137's cut/fill has known non-trivial answers.

Provide:
- `surface(e, n)`;
- `sample_irregular(bbox, mean_spacing_m, seed)`, a deterministic jittered grid;
- `sample_grid(bbox, cell_m)`;
- `split_sheets(bbox, nx, ny)`, for T-131's multi-sheet merge;
- `write_synthetic(path, points, declared_crs | None, krovak_positive=False)` and its reader.

The test-only `SyntheticXyzReader` lives in the tests and registers through `register_reader`; it is not
shipped. Its payload is the clipped `(n, 3)` float64 points in the project CRS. Its object key is the file name
plus sheet index. Its licence is "synthetic (CC0)".

## Invariants

- `domain/sources.py` imports no I/O, protocol or optional heavy dependency.
- Reprojection goes through `domain/crs.py`, and the coordinate-order rule through `io/landxml/dialects.py`.
- The CI path touches no network and no real data file.
- Nothing outside a test's `tmp_path` is written; the user config is only read.

## Acceptance criteria

1. **CRS:**
   - inspecting a synthetic file with no CRS reports `declared_crs: null` with a warning;
   - importing it without `horizontal_crs` returns `E_CRS_REQUIRED`;
   - with `"EPSG:5514"` it succeeds and records `effective_crs` and `transformation: "none"`;
   - a file declared in EPSG:32633 lands within 0.01 m of its EPSG:5514 twin, with pyproj's description
     recorded;
   - overriding a declared CRS produces a recorded warning.
2. **Křovák signs:** a file written with positive Křovák tokens and `coordinate_order: "auto"` lands within
   1e-6 m of the same points written as negative East-North.
3. **Identity:**
   - a first import gives uuid4 GUIDs;
   - re-importing an identical source reports everything `unchanged` with the same GUIDs;
   - re-importing with one object changed, one removed and one added reports `1 / 1 / 1`, keeps the GUIDs of
     the unchanged and changed objects, and gives a new GUID to the added one;
   - two independent imports of one file have disjoint GUIDs.
4. **Provenance:** the record carries each file's SHA-256, the settings verbatim, the tool version, an ISO
   timestamp, licence and attribution; `source.get` returns all of it.
5. **Plugins (fake entry points):**
   - a non-allow-listed plugin is reported `available` and its module is never imported (prove it with a
     sentinel);
   - an allow-listed plugin registers its reader, and an import through it works;
   - a plugin that raises is reported `failed` with its error, and the backend keeps serving;
   - a missing config file gives the defaults, and a malformed one gives the defaults plus a warning.
6. **Clip:** a `CorridorClip` and a `BBoxClip` each keep exactly the synthetic points inside their polygon,
   checked against an independent point-in-polygon count.
7. **Live server:**
   - all five methods work over a live server;
   - `ipc.md` shows them as `experimental`;
   - `gen_protocol_docs.py --check` exits 0;
   - bad parameters return `E_BAD_PARAMS`, an unknown `reimport_of` returns `E_NOT_FOUND`, and the
     connection survives every error.
8. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened.

## Out of scope

- LAS/LAZ, GeoTIFF and LandXML-surface readers — T-131.
- The analysis surface, display tiles and `terrain.*` methods — T-132.
- The ČÚZK and global-DEM providers — T-133; imagery — T-134.
- Saving sources in `.coypub` — T-145; the client import dialog — T-136.
- Method plugins and client mods — Phase 2 (ADR 0010).

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- the synthetic surface's formula and its height range over the KRAV82 area, with the rail height there;
- the pyproj description recorded for the EPSG:32633 case;
- the three plugin states as `plugins.list` returned them;
- the reconciliation counts from criterion 3.
