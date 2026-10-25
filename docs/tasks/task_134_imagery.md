# T-134 — Imagery: ortho tiles from files, ČÚZK streaming and bulk sheets, shaded relief

**Milestone:** P1.M3 · **Depends on:** T-130, T-132, T-133, T-139 · **Blocks:** T-135

## Context

The terrain needs a skin. The user decided on 2026-10-13 (ROADMAP, "Source decisions"):

- **ČÚZK orthophoto, both delivery modes:**
  - **streamed** for browsing — only the tiles in view, fetched from ČÚZK's tile service into a disk cache;
  - **bulk** whole sheets on request for offline work or export, through T-139 (about 58 MB per sheet,
    848 MB for the Kralupy corridor).
- **Where no orthophoto exists, shaded relief:** colour by height plus hillshade, computed from the terrain
  itself. A global imagery source is deferred.

Imagery tiles share T-132's terrain tile grid: same levels, same `ix, iy`, same revision rules. The client
(T-135) therefore drapes one image over each heightfield tile with no resampling of its own. Level 0 is the
finest: a 512 m tile drawn as 512 × 512 pixels is 1 m per pixel, so imagery finer than 1 m is never shown
and is averaged down when read.

Facts checked on 2026-10-14:
- ČÚZK publishes the orthophoto as a WMTS **in EPSG:5514**:
  `https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS?service=WMTS&request=GetCapabilities`,
  TileMatrixSet `default028mm`.
- A Web Mercator variant (`ORTOFOTO_WM`) and a WMS exist as well.
- Licence: ČÚZK open data, CC BY 4.0, with attribution "© ČÚZK".

## How this task runs

The task has two parts, each its own worker order. **Part B starts only after Part A is committed.**

- **Part A — imagery tile pipeline:** local image files, shaded relief, composition onto the tile grid, and
  the `imagery.tiles` / `imagery.tile` methods. Everything Part B plugs into.
- **Part B — ČÚZK orthophoto providers:** the WMTS streaming source and the bulk-sheet provider, as
  imagery layers that Part A's composition already accepts.

## Preconditions

- T-130:
  - the import framework and provider interface (`io/sources/registry.py`: `SourceReader`, `SourceProvider`);
  - `SourceKind.IMAGERY`;
  - the host allow-list rule (ADR 0009, decision 7).
- T-132: the tile grid (`io/mesh/terrain_tiles.py`: `tile_edge`, `tile_bounds`, levels 0–4), revisions,
  off-loop handlers and write barriers.
- T-133: source precedence by resolution, and the provider cache (`io/providers/cache.py`).
- T-139: ČÚZK ATOM `ORTOFOTO` sheets (JPEG plus world files, with editions such as `WRTO24.2025`).

Read before starting:
- `docs/adr/0009-context-data-import.md`;
- `docs/adr/0003-ipc-protocol.md`, including the addendum;
- `docs/data-contracts/terrain-tiles.md`, `cuzk-atom.md` and `terrain-providers.md`.

**Code to copy the patterns from** — read these before writing anything:

| You write | Copy the pattern of |
|---|---|
| `ImageryRaster` | `domain/terrain.py: TerrainGrid` (field layout, `__post_init__` checks) |
| `raster_image.py` reader | `io/terrain/raster.py: GeoTiffReader` (sniff, inspect, windowed read, `_read_resampled`) |
| `ImageryCache`, `ProjectState.imagery_cache()` | `server/session.py: TerrainCache`, `ProjectState.terrain_cache()` |
| `_imagery_snapshot`, `_ensure_imagery`, `_compose` | `server/handlers.py: _terrain_snapshot`, `_ensure_terrain`, `_bake` / `_bake_into` |
| `handle_imagery_tiles`, `handle_imagery_tile` | `server/handlers.py: handle_terrain_tiles`, `handle_terrain_tile` |
| the WMTS client (Part B) | `io/providers/copernicus.py: _check_url`, `_Http` (allow-list, redirects, retries) |
| the bulk provider (Part B) | `io/providers/cuzk_terrain.py: CuzkTerrainProvider` |
| tests with a local HTTP server | `tests/test_terrain_providers.py` |

## Part A — imagery tile pipeline

### Deliverables (Part A)

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/imagery.py` | new — `ImageryRaster`, `ImageryLayer`, `RasterLayer`, `compose_layers` (numpy only) |
| `backend/src/coypu_builder/domain/relief.py` | new — `hillshade`, `hypsometric`, `relief` (numpy only) |
| `backend/src/coypu_builder/io/imagery/__init__.py` | new — `register_imagery_readers`, `layer_for_payload`, `register_layer_factory` |
| `backend/src/coypu_builder/io/imagery/raster_image.py` | new — `RasterImageReader` (format `raster-image`) |
| `backend/src/coypu_builder/io/mesh/imagery_tiles.py` | new — `ImageryTile`, `compose_tile`, `encode_jpeg`, `height_range` |
| `backend/src/coypu_builder/io/sources/registry.py` | `reader_for(path, format_id=None, kind=None)` |
| `backend/src/coypu_builder/server/session.py` | `ImageryCache`, `ProjectState.imagery_cache()` |
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | `imagery.tiles`, `imagery.tile`; `kind` on `import.inspect` |
| `backend/src/coypu_builder/io/providers/copernicus.py` | `notices = (LIABILITY_NOTICE,)` on the provider class |
| `docs/protocol/ipc.md` | regenerate with `tools/gen_protocol_docs.py` |
| `docs/data-contracts/imagery-tiles.md` | new |
| `backend/tests/test_imagery.py` | new |

### Contract (Part A)

**`domain/imagery.py`** (numpy only):

```python
@dataclass(frozen=True, eq=False)
class ImageryRaster:
    """Row 0 is the northernmost row; cell [r, c] covers
    E in [origin_e + c*cell_e, origin_e + (c+1)*cell_e], N in [origin_n - (r+1)*cell_n, origin_n - r*cell_n]."""
    origin_e: float      # project CRS, the outer corner of cell [0, 0]
    origin_n: float
    cell_e: float        # > 0
    cell_n: float        # > 0
    rgb: np.ndarray      # (rows, cols, 3) uint8
    valid: np.ndarray    # (rows, cols) bool; False = no data, transparent, or outside the clip
    native_resolution_m: float  # the source's own cell size before any averaging; used for precedence

    def bounds(self) -> Bounds: ...   # (min_e, min_n, max_e, max_n)

class ImageryLayer(Protocol):
    source_id: str
    resolution_m: float
    def bounds(self) -> Bounds: ...
    def render(self, bounds: Bounds, size: int) -> tuple[np.ndarray, np.ndarray]:
        """(rgb (size, size, 3) uint8, valid (size, size) bool) over the square `bounds`. Row 0 is north.
        Pixel [r, c] has its centre at E = min_e + (c + 0.5) * edge / size, N = max_n - (r + 0.5) * edge / size."""

class RasterLayer:  # ImageryLayer over one ImageryRaster
    def __init__(self, source_id: str, raster: ImageryRaster) -> None: ...

def compose_layers(layers: Sequence[ImageryLayer], bounds: Bounds, size: int
                   ) -> tuple[np.ndarray, np.ndarray, tuple[str, ...]]:
    """Layers ordered finest `resolution_m` first (stable for ties: the caller's order). For each pixel, the
    first layer with a valid pixel wins. Returns (rgb, valid, source ids that won at least one pixel, in
    layer order). Layers whose bounds do not meet `bounds` are not rendered."""
```

`RasterLayer.render` resampling, exactly:
- `k = clamp(ceil(pixel_m / min(cell_e, cell_n)), 1, 8)` where `pixel_m = edge / size`.
- Sample each pixel at `k × k` sub-points, at offsets `(i + 0.5) / k` of the pixel. Take the nearest raster
  cell at each sub-point.
- A pixel's colour is the mean of its valid sub-samples, rounded half up. It is valid if at least half of
  its sub-samples are valid.
- Accumulate one `(size, size)` pass per sub-point offset, so memory stays at `O(size²)` and never reaches
  `O(size² k²)`.

**`domain/relief.py`** (numpy only):

```python
SUN_AZIMUTH_DEG = 315.0   # clockwise from north
SUN_ALTITUDE_DEG = 45.0
RAMP = ((0.00, (70, 120, 70)), (0.35, (165, 185, 110)), (0.70, (190, 160, 115)), (1.00, (240, 238, 232)))

def hillshade(heights: np.ndarray, cell_m: float) -> np.ndarray:
    """(rows-2, cols-2) float in [0, 1] for the interior of a (rows, cols) height grid, row 0 north.
    Central differences: dz/de = (h[r, c+1] - h[r, c-1]) / (2 cell), dz/dn = (h[r-1, c] - h[r+1, c]) / (2 cell).
    Normal (−dz/de, −dz/dn, 1) normalised; light L = (sin az · cos alt, cos az · cos alt, sin alt) in (E, N, up).
    Result max(0, n·L); NaN where any of the five heights is NaN."""

def hypsometric(heights: np.ndarray, low: float, high: float) -> np.ndarray:
    """(…, 3) float colour: t = clip((h − low) / (high − low), 0, 1), linear between the RAMP stops.
    high == low gives t = 0.5 everywhere."""

def relief(heights: np.ndarray, cell_m: float, low: float, high: float) -> tuple[np.ndarray, np.ndarray]:
    """(rgb uint8, valid bool) for the interior: rgb = round(hypsometric · (0.25 + 0.75 · hillshade)),
    valid = finite hillshade. Deterministic."""
```

**`io/mesh/imagery_tiles.py`:**

```python
IMAGE_SIZE = 512
JPEG_QUALITY = 85

@dataclass(frozen=True)
class ImageryTile:
    level: int; ix: int; iy: int; edge_m: float
    jpeg: bytes                  # RGB, IMAGE_SIZE², quality 85, baseline, no EXIF
    alpha: np.ndarray | None     # (IMAGE_SIZE, IMAGE_SIZE) uint8 0/255, or None when every pixel is 255
    source_ids: tuple[str, ...]  # imagery sources that won a pixel, then "relief" if relief filled any

def height_range(stack: TerrainStack) -> tuple[float, float] | None:
    """The 2nd and 98th percentile of finite heights sampled on a 256 × 256 lattice over stack.bounds();
    None when there are none. Computed once per imagery revision."""

def compose_tile(layers, stack, heights_range, level, ix, iy) -> ImageryTile:
    """Imagery layers first (`compose_layers`); the remaining pixels get relief from `stack` sampled at the
    pixel centres plus a one-pixel border (a (514, 514) lattice); pixels with neither are (0, 0, 0) and
    alpha 0."""

def encode_jpeg(rgb: np.ndarray, quality: int = JPEG_QUALITY) -> bytes: ...  # Pillow, imported lazily
```

**Local images, `io/imagery/raster_image.py`** — format `raster-image`, `kinds = {IMAGERY}`, suffixes
`.tif .tiff .jpg .jpeg .png .zip`:
- `sniff` accepts the JPEG (`FF D8 FF`), PNG (`89 50 4E 47`) and TIFF magics.
- Georeferencing comes from GeoTIFF tags or a world file next to the image (`.jgw` / `.jpgw`, `.pgw` /
  `.pngw`, `.tfw` / `.tifw`, or `.wld`). GDAL reads these sidecars itself.
- **A world file carries no CRS.** `inspect` then reports `declared_crs = None`, and the framework's
  existing refusal applies until `horizontal_crs` is given (ADR 0009). Do not add a second refusal path.
- **Bands:**
  - 1 band: grey, copied to R, G and B;
  - 3 bands: RGB;
  - 4 bands: RGBA, where alpha 0 means not valid.

  Any other count is refused in `inspect`, with the count in the message.
- **Read:**
  - only the window that covers the clip;
  - decimated with average resampling (`ds.read(window=…, out_shape=…, resampling=Resampling.average)`)
    to `display_resolution_m`, by default `tile_edge(0) / IMAGE_SIZE` (1 m);
  - never finer than the source.
- **Reprojection:** a source in another CRS is resampled onto a grid in the project CRS by nearest
  neighbour, as `_read_resampled` does for heights.
- **Size limit:** at most `MAX_PIXELS = 100_000_000` per object, after decimation. Above that the reader
  refuses and asks for a coarser `display_resolution_m`.
- **Result:** yields `ImportedObject(stem, sha256, ImageryRaster)`. `native_resolution_m` is the source's
  cell size in metres.

**Reader choice by kind:**
- `reader_for(path, format_id=None, kind=None)`: with `kind` given and no `format_id`, only readers whose
  `kinds` contain it are tried.
- `import.run` passes its `kind`.
- `import.inspect` gains `kind: str | None = None` and passes it on.
- So a GeoTIFF sent with `kind = "imagery"` is read as an image, and without a kind it is read as terrain,
  as before.

**`io/imagery/__init__.py`:**
- `register_imagery_readers()` registers `RasterImageReader`. Call it wherever the terrain readers are
  registered.
- `register_layer_factory(payload_type, factory)` and `layer_for_payload(source_id, payload) -> ImageryLayer`.
  Part A registers `ImageryRaster → RasterLayer`. Part B adds its own payload types here.

**Revision and cache** (`server/session.py`):
- `ImageryCache` holds:
  - `revision`, from the same process-wide `_REVISIONS` counter;
  - `fingerprint = (terrain cache revision, imagery-source fingerprint)`;
  - `layers`, `heights_range`, `attributions`;
  - `composing` (futures in flight);
  - an LRU of `ImageryTile` capped at `IMAGERY_TILE_CACHE_SIZE = 64`.
- `ProjectState.imagery_cache()` replaces the cache when the fingerprint changes. So the imagery revision
  changes when either the terrain or the imagery sources change, and only then.
- The terrain stack is not rebuilt for an imagery-only change.

**Methods**, both `mutates = False`, both read-only, stability tier as `terrain.tile`:
- **`imagery.tiles {level}` returns:**
  - `level`, `revision`, `terrain_revision`;
  - `tiles`: a tuple of `{level, ix, iy, edge_m, source_ids}`. The keys are the terrain tile keys of that
    level, and `source_ids` lists the imagery layers whose bounds meet the tile; nothing is composed;
  - `attributions`: a mapping from source id to attribution string for every imagery and terrain source.
    T-135 shows those of the sources in view: imagery from each tile's `source_ids`, terrain from the
    terrain tiles' `source_index`;
  - `notices`: a mapping from source id to a tuple of licence notices that must be shown alongside the
    attribution. A provider declares them as a class attribute `notices: tuple[str, ...]`, default `()`.
    Look it up from the source's `ProviderOrigin.provider_id`; a file source has none. Give the Copernicus
    provider `notices = (LIABILITY_NOTICE,)`; that constant exists since T-133's fixes;
  - `warnings`.
- **`imagery.tile {level, ix, iy, revision}` returns:**
  - `level, ix, iy, edge_m, revision, width = 512, height = 512, has_alpha, source_ids`;
  - blob `image`: the JPEG bytes, `<u1`, `(n,)`;
  - when `has_alpha` is true, blob `alpha`: `<u1`, `(512, 512)`.
- A stale revision gets `E_NOT_FOUND`, and so does a key that is not a terrain tile.
- Composition runs in a worker thread (`asyncio.to_thread`) under `session.gate`.
- Concurrent requests for one tile and revision share one future, exactly as `_bake` does.
- The response stays under 1 MB.

### Acceptance criteria (Part A)

Each criterion names the test that must prove it in `backend/tests/test_imagery.py`.

1. **Placement.** `test_local_image_lands_at_four_asymmetric_markers`:
   - a synthetic GeoTIFF in EPSG:5514 has four differently coloured 8 × 8 m markers near its NW, NE and SE
     corners and at an off-centre point;
   - after `import.run` (`kind = imagery`), the composed L0 tile has each marker's colour at the expected
     pixel, ±1 pixel.
2. **World-file refusal.** `test_jpeg_with_world_file_needs_a_crs`: a JPEG plus `.jgw` with no CRS is
   refused, and the same import succeeds once `horizontal_crs` is given.
3. **Resampling.** `test_raster_layer_render_averages_fine_cells`:
   - a 0.25 m checkerboard of black and white cells renders to mid-grey (127 or 128) at 1 m pixels;
   - the same raster at 2 m pixels with `k` forced to 1 shows only black or white.
4. **Relief.** `test_relief_lights_the_north_west_slope`:
   - a plane falling toward the north-west is brighter than one falling toward the south-east;
   - a flat plane gets hillshade exactly `sin(45°)`;
   - a NaN height makes its neighbours invalid.
5. **Composition.** `test_composition_layers_relief_and_alpha`:
   - pixels inside the image show the image;
   - terrain-only pixels show relief and have alpha 255;
   - pixels with neither have alpha 0 and colour (0, 0, 0);
   - a finer image placed over a coarser one wins where they overlap;
   - `source_ids` lists exactly the winners, then `"relief"`.
6. **Attribution and revision.** `test_imagery_revision_and_attribution`:
   - `imagery.tiles.attributions` maps every imagery and terrain source id to its attribution, and nothing
     else;
   - `notices` holds the Copernicus liability notice for a Copernicus source, and an empty tuple for a file
     source;
   - an imagery import changes `revision` but not `terrain_revision`;
   - a terrain import changes both;
   - a stale `imagery.tile` gets `E_NOT_FOUND`.
7. **Off-loop.** `test_ping_answers_while_tiles_compose`: while 8 `imagery.tile` requests compose, a `ping`
   answers within 100 ms. Two concurrent requests for one tile compose it once; count the calls.
8. **Wire.** `test_imagery_tile_blobs`:
   - `image` decodes with Pillow to 512 × 512 RGB;
   - `alpha` is present exactly when `has_alpha`;
   - the whole response is under 1 MB.
9. **Kind routing.** `test_reader_for_kind`: a GeoTIFF is read by `raster-image` with `kind = imagery` and
   by `geotiff` without a kind.
10. **No regressions:**
    - both suites are green;
    - no previously passing test is removed, skipped or weakened;
    - `gen_protocol_docs.py --check` exits 0.

## Part B — ČÚZK orthophoto providers

### Deliverables (Part B)

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/imagery/cuzk_wmts.py` | new — `cuzk-ortho-wmts` provider, `WmtsSource` payload, `WmtsLayer` |
| `backend/src/coypu_builder/io/imagery/cuzk_bulk.py` | new — `cuzk-ortho-sheets` provider over T-139 |
| `backend/src/coypu_builder/io/imagery/__init__.py` | register both providers and the `WmtsSource → WmtsLayer` factory |
| `docs/data-contracts/imagery-tiles.md` | add the two providers, the TileMatrix choice and the cache layout |
| `backend/tests/test_imagery_providers.py` | new — local test servers only |

### Contract (Part B)

- **`cuzk-ortho-sheets`:** T-139 `ORTOFOTO` sheets for the clip, fetched into the provider cache and then
  read with Part A's `RasterImageReader`.
  - Opt-in, with the size guard: `max_total_mb` defaults to 200. Above the limit it refuses and reports the
    total, so the user raises the limit deliberately.
  - The object key is the sheet code, and the edition is recorded.
  - `horizontal_crs` is EPSG:5514. The licence is CC BY 4.0 with the attribution "© ČÚZK".
- **`cuzk-ortho-wmts`:** a streaming source.
  - Its import records only a `WmtsSource(service_url, layer, tile_matrix_set, matrices, clip)` payload,
    where `matrices` comes from the capabilities. **Nothing is downloaded at import time** beyond the
    capabilities document.
  - **Capabilities:** read with `defusedxml`. Take each TileMatrix's identifier, scale denominator,
    top-left corner, tile size and matrix size.
    - Pixel size is `scale_denominator × 0.00028` m.
    - In EPSG:5514 the WMTS corner is given as (X, Y) Křovák. Apply the coordinate-token rule from
      `CLAUDE.md`, and test the corner against a known tile of the live service in the network test.
  - **`WmtsLayer.render(bounds, size)`:**
    - choose the coarsest TileMatrix whose pixel size is at most `edge / size`; if none qualifies, the
      finest;
    - fetch the WMTS tiles that cover `bounds`;
    - mosaic them into an `ImageryRaster`;
    - render it with `RasterLayer`'s resampling.
  - **Allow-list:**
    - https only, on `ags.cuzk.gov.cz` (within `*.cuzk.gov.cz`);
    - no userinfo in any URL;
    - checked for every request and every redirect, at most 5 redirects;
    - copy `copernicus.py: _check_url`.
  - **Concurrency:** at most 4 fetches at a time across all threads (`threading.BoundedSemaphore(4)`), with
    the retries and backoff of T-139.
  - **Cache:**
    - tiles go into the provider cache under `cuzk-ortho-wmts/<TileMatrix>/<row>/<col>.jpg`;
    - each is revalidated with `If-None-Match` / `If-Modified-Since` once it is 30 days old;
    - a 304 refreshes the timestamp and fetches no body.
  - **Failure:** a fetch that still fails after its retries leaves those pixels not valid, so relief shows
    through, and adds a warning. It is never an error.
  - **Size:** a single tile body is capped at 2 MB; a larger `Content-Length` or body is refused.
- **Provider attributes:** both providers carry `title`, `description`, `licence`, `attribution` and
  `notices`, as the terrain providers do. T-136's `import.formats` lists them.

### Acceptance criteria (Part B)

1. **Streaming.** `test_wmts_tile_fetches_only_what_it_needs`, against a local server that mimics the
   capabilities and serves tiles:
   - one `imagery.tile` fetches only the WMTS tiles that cover it;
   - a second request hits the cache;
   - after the expiry, a 304 refetches nothing.
2. **Allow-list.** `test_wmts_refuses_foreign_hosts`: a foreign host, plain http, userinfo and a redirect off
   the allow-list are each refused.
3. **Degrading.** `test_wmts_503_degrades_to_relief`: a 503 is retried, then the pixels show relief and a
   warning names the failure.
4. **Corner and orientation.** `test_wmts_matrix_choice_and_corner`: for each level 0–4, the chosen
   TileMatrix is the one the rule above gives; a synthetic tile with an asymmetric marker lands at the
   right pixel.
5. **Bulk sheets.** `test_bulk_sheets_size_guard`:
   - without a size override, `cuzk-ortho-sheets` refuses over the limit and reports the total;
   - with an override, against the test server, the sheets compose correctly.
6. **Live** (`-m network`, Verification only, not in CI):
   - stream Kralupy L0 imagery over the KRAV82 area from the WMTS;
   - report the tile count, bytes fetched, cold and warm times, and one composed tile's JPEG size;
   - do **not** bulk-download sheets; list their total only.
7. **No regressions:** as Part A.

## Invariants

- No network on the CI path: WMTS and ATOM are tested against local servers through the allow-list seam,
  and live tests are opt-in with `-m network`.
- The orthophoto is never downloaded in bulk without an explicit size override.
- Nothing is written into the repository or the user's data folder. Everything goes into the provider cache.
- No secrets in any URL, log line or file. The ČÚZK services need no key.
- `domain/imagery.py` and `domain/relief.py` import numpy only. Pillow and rasterio are imported lazily
  inside `io/`.

## Out of scope

- Draping and texturing in the client — T-135. Choosing imagery in the client — T-136.
- Global imagery outside Czechia — deferred (ROADMAP, "Source decisions").
- Web Mercator WMTS and WMS variants — not needed while the EPSG:5514 service exists.

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run pytest -q -m network tests/test_imagery_providers.py   # Part B only
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

**Part A:**
- one composed tile from the placement test, described: where image, relief and transparency appear;
- the composition time for one L0 tile, and the size of its JPEG and alpha blobs;
- every contract change, with its reason.

**Part B:**
- every live figure from criterion B6;
- the WMTS TileMatrix chosen for each of our levels, and why;
- the cache layout and the revalidation behaviour;
- anything about the ČÚZK services that differed from this spec.
