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
(T-135) therefore drapes one image over each heightfield tile with no resampling of its own.

Facts checked on 2026-10-14:
- ČÚZK publishes the orthophoto as a WMTS **in EPSG:5514**:
  `https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS?service=WMTS&request=GetCapabilities`,
  TileMatrixSet `default028mm`.
- A Web Mercator variant (`ORTOFOTO_WM`) and a WMS exist as well.
- Licence: ČÚZK open data, CC BY 4.0, with attribution "© ČÚZK".

## Preconditions

- T-130:
  - the import framework and provider interface;
  - `SourceKind.IMAGERY`;
  - the host allow-list rule (ADR 0009, decision 7).
- T-132: the tile grid (`tile_edge`, `lattice`, levels 0–4), revisions, off-loop handlers and write barriers.
- T-133: source precedence by resolution and the provider cache.
- T-139: ČÚZK ATOM `ORTOFOTO` sheets (JPEG plus world files, with editions such as `WRTO24.2025`).

Read before starting:
- `docs/adr/0009-context-data-import.md`;
- `docs/adr/0003-ipc-protocol.md`, including the addendum;
- `docs/data-contracts/terrain-tiles.md`, `cuzk-atom.md` and `terrain-providers.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/imagery.py` | new — imagery payload types (numpy only) |
| `backend/src/coypu_builder/io/imagery/__init__.py` | new — registers readers and providers |
| `backend/src/coypu_builder/io/imagery/raster_image.py` | new — local GeoTIFF/JPEG/PNG with world file or GeoTIFF georeferencing |
| `backend/src/coypu_builder/io/imagery/cuzk_wmts.py` | new — `cuzk-ortho-wmts` streaming provider |
| `backend/src/coypu_builder/io/imagery/cuzk_bulk.py` | new — `cuzk-ortho-sheets` provider over T-139 |
| `backend/src/coypu_builder/io/imagery/relief.py` | new — shaded relief from a `TerrainStack` |
| `backend/src/coypu_builder/io/mesh/imagery_tiles.py` | new — compose imagery into the terrain tile grid |
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | `imagery.tiles` and `imagery.tile` |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/imagery-tiles.md` | new |
| `backend/tests/test_imagery.py` | new — local test servers only |

## Contract

### Imagery sources

- **Local files** (format `raster-image`):
  - georeferenced GeoTIFF, or JPEG/PNG with a world file (`.jgw`, `.pgw`, `.tfw`) and an explicit
    `horizontal_crs`;
  - read windowed to the clip.
  - **A world file carries no CRS**, so these files follow the same refusal rule as LAS (ADR 0009).
- **`cuzk-ortho-sheets`:** T-139 `ORTOFOTO` sheets for the clip.
  - Opt-in, with the size guard: the user raises `max_total_mb` deliberately.
  - Cached in the provider cache (T-133).
  - The object key is the sheet code, and the edition is recorded.
- **`cuzk-ortho-wmts`:** a streaming source.
  - Its import records only the service, layer, TileMatrixSet and clip — **no download at import time**.
  - Tiles are fetched lazily when `imagery.tile` needs them.
  - Allowed hosts: https on `ags.cuzk.gov.cz` (within `*.cuzk.gov.cz`), checked for every request and every
    redirect.
  - Read the capabilities with `defusedxml`.
  - Fetch at most 4 at a time, with retries and backoff as in T-139.
  - Fetched WMTS tiles go into the provider cache, keyed by TileMatrix, row and column, and revalidated with
    `ETag` / `If-Modified-Since` after 30 days.

### Imagery tiles

- `imagery.tiles {level}` returns metadata like `terrain.tiles`, plus each tile's contributing imagery
  sources. It uses the same revision, and the revision changes when either the terrain or the imagery sources
  change.
- `imagery.tile {level, ix, iy, revision}` returns blob `image`: JPEG bytes (quality 85, `<u1`, `(n,)`) of an
  RGB image, `512 × 512` pixels, covering exactly the tile's square.
  - Pixel (0, 0) is the north-west corner.
  - Pixel centres sit at `(c + 0.5) · edge / 512`.
- **Composition:**
  - imagery sources are layered by resolution, finest first, like terrain;
  - pixels with no imagery are filled with shaded relief from the `TerrainStack`;
  - pixels with no terrain either are transparent-equivalent: the encoded colour `(0, 0, 0)` and an `alpha`
    blob (`<u1`, `(512, 512)`).
- **Attribution:** the tile metadata lists the attribution strings of every contributing source — imagery
  and terrain — for T-135's on-screen attribution.
- **Shaded relief** (`relief.py`):
  - hillshade from the analysis surface (sun azimuth 315°, altitude 45°);
  - multiplied with a hypsometric colour ramp scaled to the project's height range;
  - deterministic, and a function of `TerrainStack` only.
- **Size:** each tile response stays under 1 MB.
- **Performance:** composition runs off the event loop (ADR 0003 addendum). Concurrent requests for the same
  tile share one composition. A WMTS fetch failure degrades to relief for that area with a warning, never an
  error.

## Invariants

- No network on the CI path: WMTS and ATOM are tested against local servers with the allow-list seam, and
  live tests are opt-in with `-m network`.
- The orthophoto is never downloaded in bulk without an explicit size override.
- Nothing is written into the repository or the user's data folder. Everything goes into the provider cache.
- No secrets in any URL, log line or file. The ČÚZK services need no key.

## Acceptance criteria

1. **Local image:** a synthetic georeferenced image composes into the tile grid with correct orientation and
   placement, checked at four asymmetric markers. A JPEG with a world file and no CRS is refused until
   `horizontal_crs` is given.
2. **WMTS streaming (local server mimicking the capabilities and tiles):**
   - an `imagery.tile` fetches only the WMTS tiles that cover it;
   - a second request hits the cache;
   - after the expiry, a revalidation that answers 304 refetches nothing;
   - a foreign host, plain http and a redirect off the allow-list are refused;
   - a 503 from the server is retried, then degrades to relief with a warning.
3. **Bulk sheets:** without a size override, `cuzk-ortho-sheets` refuses over the limit and reports the total.
   With an override against the test server, the sheets compose correctly.
4. **Relief and precedence:**
   - where no imagery exists, the tile shows relief;
   - where terrain is absent, alpha is 0;
   - a finer imagery source overrides a coarser one;
   - the attribution list matches the contributing sources.
5. **Revision:** an import of terrain or imagery changes the revision, and a stale `imagery.tile` returns
   `E_NOT_FOUND`. Pings keep answering while tiles compose.
6. **Live (`-m network`, Verification only):**
   - stream Kralupy L0 imagery over the KRAV82 area from the WMTS;
   - report the tile count, bytes fetched, cold and warm times, and one composed tile's JPEG size;
   - do **not** bulk-download sheets; list their total only.
7. **No regressions:** both suites are green on Windows and on Linux CI, with no previously passing test
   removed, skipped or weakened, and `gen_protocol_docs.py --check` exits 0.

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
uv run pytest -q -m network tests/test_imagery.py
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- every live figure from criterion 6;
- the WMTS TileMatrix chosen for each of our levels, and why;
- the cache layout and the revalidation behaviour;
- one composed tile, described: where imagery, relief and transparency appear;
- anything about the ČÚZK services that differed from this spec.
