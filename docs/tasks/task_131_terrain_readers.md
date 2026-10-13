# T-131 — Terrain readers: LAS/LAZ, GeoTIFF/COG and LandXML TIN surfaces

**Milestone:** P1.M3 · **Depends on:** T-130 · **Blocks:** T-132, T-133

## Context

T-130 built the import framework:
- `import.inspect` / `import.run`;
- `ImportSettings`;
- `SourceDataset` records with GUIDs;
- a reader registry.

This task adds the three terrain formats the user chose for M3 (ROADMAP scope decision 7):
- **LAS/LAZ point clouds** — the ČÚZK DMR 5G and DMP 1G sheets;
- **GeoTIFF/COG rasters** — ČÚZK DMR 4G, and later the Copernicus DEM;
- **LandXML TIN surfaces** — the `<Surface>` point and face lists that Civil 3D, OpenRoads and similar tools
  export.

ČÚZK delivers each sheet as a ZIP holding one file. The Kralupy corridor needs 15 DMR 5G sheets, so a single
import must accept several files and merge them into one source, one object per sheet.

The readers produce terrain *payloads*: the clipped, reprojected data in the project CRS. Building the
analysis surface and the display tiles from those payloads is T-132.

## Preconditions

- T-130:
  - `SourceReader` protocol and `register_reader`;
  - `InspectReport` and `ImportedObject`;
  - `CrsTransform` from `resolve_transform`, which applies `coordinate_order` through
    `io/landxml/dialects.py`;
  - the clip polygon in the project CRS;
  - `SourceDataset` and `reconcile`;
  - `tests/fixtures/synthetic/terrain.py`: `surface`, `sample_irregular`, `sample_grid`, `split_sheets`.
- The `gis` extra has `rasterio` and `shapely` (both installed in CI); `laspy` is **not** installed yet.
- Real data for manual checks, outside the repository:
  - `D:\COYPU_Builder\Data\Terrain_Samples\cuzk\DMR5G\` — 15 sheets, with `cuzk_manifest.json`;
  - `D:\COYPU_Builder\Data\Terrain_Samples\KRAV82.zip`.
- The DMR 5G LAS header declares **no CRS**: its only VLR is LASzip's (ROADMAP F25). Values are S-JTSK /
  Krovak East North (EPSG:5514), and heights are Baltic (Bpv).

Read before starting:
- `docs/adr/0009-context-data-import.md` (decisions 2, 5 and 7);
- `docs/data-contracts/import-framework.md`;
- `docs/data-contracts/coordinate-conventions.md`;
- `docs/data-contracts/landxml-dialects.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/terrain.py` | new — terrain payload types, pure numpy |
| `backend/src/coypu_builder/io/terrain/__init__.py` | new — registers the three built-in readers |
| `backend/src/coypu_builder/io/terrain/las.py` | new — LAS/LAZ |
| `backend/src/coypu_builder/io/terrain/raster.py` | new — GeoTIFF/COG |
| `backend/src/coypu_builder/io/terrain/landxml_surface.py` | new — LandXML `<Surface>` TIN |
| `backend/src/coypu_builder/io/terrain/archive.py` | new — open a single-file ZIP as its member |
| `backend/src/coypu_builder/protocol/messages.py` | additive: `paths` on `import.inspect` and `import.run` |
| `backend/src/coypu_builder/io/sources/runner.py` | multi-file sources: one object per file |
| `backend/src/coypu_builder/server/handlers.py` | pass `paths` through |
| `docs/protocol/ipc.md` | regenerate |
| `backend/pyproject.toml`, `backend/uv.lock` | add `laspy` with a LAZ backend (`lazrs`) to the `gis` extra |
| `docs/data-contracts/terrain-payloads.md` | new |
| `backend/tests/test_terrain_readers.py` | new — every fixture written at test time into `tmp_path` |

## Contract

### Payloads — `domain/terrain.py` (numpy only)

```python
@dataclass(frozen=True)
class TerrainPoints:
    points: np.ndarray            # (n, 3) float64 — E, N, H in the project CRS, after clip
    classes: np.ndarray | None    # (n,) uint8 point-cloud classification, if the source has one

@dataclass(frozen=True)
class TerrainGrid:
    origin_e: float               # project CRS; the outer corner of cell [0, 0]
    origin_n: float
    cell_e: float                 # > 0
    cell_n: float                 # > 0; row 0 is the northernmost row
    heights: np.ndarray           # (rows, cols) float64; NaN = nodata or outside the clip

@dataclass(frozen=True)
class TerrainTin:
    points: np.ndarray            # (n, 3) float64, project CRS
    triangles: np.ndarray         # (m, 3) int32, counter-clockwise in plan view

TerrainPayload = TerrainPoints | TerrainGrid | TerrainTin
```

### Readers

All three register with T-130's registry, declare `kinds = {TERRAIN}`, and accept a `.zip` that holds exactly
one supported file. The SHA-256 recorded is the ZIP's. Each file becomes one `ImportedObject`:
- its key is the file's stem (`KRAV82`);
- its `content_hash` is the SHA-256 of the file as read;
- its payload is one of the types above.

| Reader | `format_id` | Inspect reports | Read produces |
|---|---|---|---|
| `las.py` | `las` | the declared CRS from a WKT or GeoKeys VLR, or `None` with a warning; point count; header extent; point format; and, from a capped sample, a class histogram | `TerrainPoints`, filtered by `settings.point_classes`, reprojected, clipped |
| `raster.py` | `geotiff` | the declared CRS, bounds, resolution, size, nodata, band count | `TerrainGrid`; a source in another CRS is warped to the project CRS at its own resolution, or at `display_resolution_m` when set, with bilinear resampling |
| `landxml_surface.py` | `landxml-surface` | the number of surfaces, points and faces; the CRS from `<CoordinateSystem>` | `TerrainTin`, with point tokens resolved by the dialect rules |

- **Native coordinate order:** LAS and GeoTIFF readers pass their tokens as (x, y) = (E, N) and declare the
  native order `"EN"` to T-130's `resolve_transform` (CLAUDE.md, "Coordinate tokens"). The LandXML-surface
  reader keeps `"NE"`. Positive Křovák values are negated by the dialect's sign rule whatever the order.
- **LAS:**
  - Apply the header's scale and offset in float64.
  - **Never round-trip coordinates through float32.** Křovák values sit near −1,025,000, so float32 would
    lose decimetres.
  - Use `laspy` with its LAZ backend, imported lazily inside the module.
- **Raster:**
  - Read only the window that covers the clip polygon, then set cells outside the polygon to NaN.
  - The source's nodata becomes NaN.
  - Heights stay float64.
- **LandXML surface:**
  - LandXML `<P>` tokens are "northing easting elevation" in the specification's order. Resolve them through
    `dialects.resolve_coordinates`, so a Křovák file with positive tokens lands correctly. Do not restate
    the rule.
  - Faces may be listed clockwise; normalise them to counter-clockwise.
  - Faces marked invisible (`i="1"`) are dropped.
  - A file with several `<Surface>` elements gives one object per surface: the key is the surface name, or
    its index if it has none.
- **Clip:**
  - Points are kept if they lie inside the polygon (vectorised, e.g. `shapely.contains_xy`).
  - TIN triangles are kept if any vertex is inside, and unused points are then dropped.
- **Multi-file:**
  - `import.inspect` and `import.run` accept `paths: list[str]` as an alternative to `path`.
  - All files must resolve to the same reader. Mixed formats return `E_BAD_PARAMS`.
  - The inspect report sums the counts, unions the extents, and warns if the declared CRSs differ between
    files.

## Invariants

- `domain/terrain.py` is pure numpy; `laspy` and `rasterio` are imported lazily inside `io/terrain`.
- float64 throughout the domain; nothing is converted to float32 before T-132 builds display tiles.
- Fixtures are generated at test time from T-130's synthetic surface; no binary terrain file is committed
  (`.las`, `.laz` and `.tif` are git-ignored anyway).
- CI never reads `D:\COYPU_Builder\Data`. The real-data checks are part of Verification only.

## Acceptance criteria

1. **LAS/LAZ (synthetic, written with `laspy` in `tmp_path`):**
   - points read back within 1e-6 m of the synthetic surface samples, with Křovák-scale offsets in the
     header;
   - a file without a CRS VLR inspects as `declared_crs: null` and imports only with `horizontal_crs` set;
   - a file with a WKT VLR reports it;
   - `point_classes = (2,)` keeps exactly the class-2 points;
   - LAS and LAZ give identical payloads.
2. **GeoTIFF (synthetic, written with `rasterio`):**
   - a grid in EPSG:5514 reads back cell for cell, with nodata cells as NaN;
   - the same surface written in EPSG:32633 and warped to the project CRS matches `surface()` within 0.05 m
     at cell centres away from the edges;
   - a windowed read of a clip touches only the needed window (assert the window size).
3. **LandXML TIN (synthetic, generated as text):**
   - every point resolves correctly, including a Křovák positive-token variant, which lands within 1e-6 m of
     the negative-EN variant;
   - faces are counter-clockwise, invisible faces are dropped, and a two-surface file gives two objects.
4. **ZIP and multi-file:**
   - a `.zip` holding one `.laz` reads like the bare file, with its SHA-256 taken from the ZIP;
   - a ZIP holding two files is refused;
   - a 2 × 2 synthetic sheet split imported as four `paths` gives one source with four objects whose union
     equals the unsplit sample;
   - mixed formats return `E_BAD_PARAMS`;
   - a re-import with one sheet changed reports `1 changed, 3 unchanged` with the same GUIDs (T-130's
     reconciliation, now across files).
5. **Clip:** a corridor clip over the synthetic sheets keeps exactly the points that an independent
   point-in-polygon check keeps.
6. **Real data (Verification only, not CI):**
   - inspect `KRAV82.zip` and report its declared CRS (expected none), point count and class histogram;
   - import the 15 Kralupy DMR 5G sheets with `horizontal_crs: "EPSG:5514"`, `vertical_crs: "EPSG:5705"` and a
     250 m corridor clip, and report the kept point count, the wall-clock time and the peak memory;
   - report the median terrain height within 20 m of the track centreline over stations 12.7–17.5 km, next to
     the rail height there, as a datum sanity check.
7. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened; `gen_protocol_docs.py --check` exits 0.

## Out of scope

- The TIN analysis surface, height queries, display tiles and `terrain.*` methods — T-132.
- ČÚZK and global-DEM providers, and the precedence between sources — T-133.
- ASCII XYZ, ESRI ASCII grid and DXF — deferred formats (ROADMAP, "Beyond Phase 1").
- Vertical datum transformation — recorded only in Phase 1 (ADR 0009).

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

Then run the real-data checks of criterion 6 from a scratch script outside the repository, through the same
functions the handlers call.

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- the `laspy` and `lazrs` versions;
- every real-data figure from criterion 6;
- how float64 precision was preserved through LAS scale and offset;
- the warped-raster error from criterion 2;
- anything in the DMR 5G files that differed from this spec's description, such as classes or VLRs.
