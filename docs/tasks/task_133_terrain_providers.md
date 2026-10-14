# T-133 — Terrain providers: ČÚZK and Copernicus, with source precedence

**Milestone:** P1.M3 · **Depends on:** T-130, T-131, T-132, T-139 · **Blocks:** T-135, T-136, T-137

## Context

The user chose the terrain sources on 2026-10-13 (ROADMAP, "Source decisions"):

- **Detailed terrain** comes from ČÚZK: DMR 5G point sheets, or the DMR 4G 5 m grid, fetched through T-139's
  ATOM client.
- **The generic landscape** comes from the **Copernicus DEM GLO-30**, a free global 30 m elevation model. It
  fills everywhere detailed data is absent, including beyond the corridor, so the surroundings are visible.

Today every import starts from a local file. This task makes these sources importable by query, through the
`SourceProvider` interface T-130 defined. It also replaces T-132's interim "newest source first" with a real
precedence policy, and gives display tiles a per-sample source map so the client can show where each
source applies.

Facts checked on 2026-10-14:

- **ČÚZK:** see the ROADMAP's "ČÚZK download service" paragraph and T-139.
- **Copernicus GLO-30:** public cloud-optimised GeoTIFFs, 1°×1°, named for example
  `https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N50_00_E014_00_DEM/Copernicus_DSM_COG_10_N50_00_E014_00_DEM.tif`,
  about 30 MB each, with byte-range support.
  - It is a **surface model (DSM)**: it includes vegetation and buildings.
  - Its heights refer to EGM2008 (EPSG:3855).
  - Read the licence and attribution from the official Copernicus DEM licence before shipping, and record
    them exactly.

## Preconditions

- T-130:
  - `SourceProvider` (`provider_id`, `kinds`, `licence`, `attribution`, `inspect(query)`,
    `fetch(query, cache_dir) -> list[Path]`);
  - `ProviderOrigin(provider_id, query)`;
  - `register_provider`;
  - reconcile and the plugin allow-list.
- T-131: the LAS and GeoTIFF readers, multi-file sources, the empty-file notes.
- T-132:
  - `TerrainStack`, the tile bake, `terrain.tiles` / `terrain.tile` with `revision`;
  - the off-loop handlers and write barriers (ADR 0003 addendum).
- T-139: `io/gis/cuzk_atom.py` (`load_sheet_index`, `select_sheets`, `resolve_files`, `download`, the
  manifest), with the https host allow-list and `defusedxml`.

Read before starting:
- `docs/adr/0009-context-data-import.md` (decisions 3, 6 and 7);
- `docs/adr/0003-ipc-protocol.md`, including the addendum;
- `docs/data-contracts/cuzk-atom.md`, `import-framework.md` and `terrain-tiles.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/providers/__init__.py` | new — registers the built-in providers |
| `backend/src/coypu_builder/io/providers/cuzk_terrain.py` | new — `cuzk-dmr5g` and `cuzk-dmr4g`, over T-139 |
| `backend/src/coypu_builder/io/providers/copernicus.py` | new — `copernicus-glo30` |
| `backend/src/coypu_builder/io/providers/cache.py` | new — per-user provider cache directory and policy |
| `backend/src/coypu_builder/domain/terrain_surface.py` | precedence ordering, and the source index in `TerrainStack` |
| `backend/src/coypu_builder/io/mesh/terrain_tiles.py` | per-sample source index blob |
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | provider imports; tile source map |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/terrain-providers.md` | new |
| `backend/tests/test_terrain_providers.py` | new — local test servers only, no network unless `-m network` |

## Contract

### Provider imports

- `import.inspect` and `import.run` accept, as an alternative to `path` / `paths`:
  - `provider: str`;
  - `query: dict`.

  Exactly one of `path`, `paths` and `provider` is allowed. The result shape is unchanged.
- **Provider pipeline:**
  - `fetch` downloads into the provider cache and returns local files;
  - T-131's readers read them;
  - the `SourceDataset` records `ProviderOrigin(provider_id, canonical-JSON query)`, the file hashes, and the
    provider's licence and attribution.
- **Re-import:** re-importing with `reimport_of` refetches, reconciles by object key, keeps GUIDs, and
  reports what changed.

### `cuzk-dmr5g` and `cuzk-dmr4g`

- **Query:** `{"clip": <ImportSettings clip, default the corridor at 250 m>, "max_total_mb": 500}`.
- **Pipeline:**
  - sheets are selected with T-139's `select_sheets` over the clip polygon;
  - files are downloaded with T-139's `download` into the provider cache, never into the user's Data folder;
  - the reader is LAS for DMR 5G and GeoTIFF for DMR 4G.
- **Suggested settings:**
  - `horizontal_crs: "EPSG:5514"`;
  - `vertical_crs: "EPSG:5705"`;
  - `point_classes: None` (F31: DMR 5G points are class 8; never suggest a class filter).
- **Objects:** the object key is the sheet code.
- **Licence:** "CC BY 4.0", with attribution "© ČÚZK".

### `copernicus-glo30`

- **Query:** `{"clip": <clip, default the corridor at 2000 m>}`. The generic landscape reaches well beyond
  the detailed corridor.
- **Fetch:**
  - work out the 1°×1° tiles the clip touches;
  - for each tile, **read only the window covering the clip** from the remote COG (range requests through
    rasterio's HTTP access, or the downloaded file);
  - write each window as a local GeoTIFF in the cache, keyed by tile name plus window bounds.

  A re-import with the same clip reuses the cache.
- **Allowed hosts:** https only, on `copernicus-dem-30m.s3.amazonaws.com`. This is a provider host allow-list
  as ADR 0009 decision 7 requires; T-139's pattern applies to every redirect hop.
- **Suggested settings:**
  - `horizontal_crs: "EPSG:4326"`, warped to the project CRS by T-131's GeoTIFF reader;
  - `vertical_crs: "EPSG:3855"`.
- **Heights:** they are not transformed to the project's vertical datum in Phase 1 (ADR 0009). The
  inspect report must warn about this, and that the data is a surface model, not bare ground.
- **Objects:** the object key is the tile name.

### Provider cache

- **Location:** `%LOCALAPPDATA%\COYPU Builder\cache\providers\<provider_id>\` on Windows, and
  `~/.cache/coypu-builder/providers/<provider_id>/` elsewhere.
- **Index:** the cache keeps a JSON index of what it holds — files, sizes, hashes, source URL and validators.
- **Rules:** never inside the repository; size-bounded (default 5 GB in total, oldest-first eviction, files of
  a stored source pinned); and safe to delete by hand.

### Precedence and the source map

- **Ordering:** `TerrainStack` orders surfaces by **declared resolution, finest first**: DMR 5G, then
  DMR 4G, then Copernicus. Newest-first breaks ties. A source's resolution comes from its reader (the median
  point spacing for points, the cell size for grids), is recorded in the source's metadata, and is shown in
  `source.get`.
- **Source index:** `TerrainStack.heights_and_source(e, n) -> (heights, source_index)`. The index is an int
  into the stack's ordered source list, or −1 where no source has data.
- **Tile responses:**
  - each tile carries a second blob, `source_index` (`<u1`, `(129, 129)`; 255 means none);
  - each tile's metadata carries the ordered source ids for that revision.

  This is how the client draws the boundary.
- **Seams:** where two sources meet, report the height step along the boundary as statistics in the
  `terrain.tiles` warnings (mean and 95th percentile of the absolute difference, where both have data). Do
  not blend in Phase 1.

## Invariants

- No network on the CI path: providers are tested against local HTTP(S) test servers with the allow-list
  seam, and live tests are opt-in with `-m network`.
- Every network-sourced file passes the provider's host allow-list and the size limits. XML goes through
  `defusedxml`.
- Nothing is written into the repository or into the user's data folder. Provider data lives in the cache.
- Heavy work stays off the event loop. Imports and fetches follow the ADR 0003 addendum.

## Acceptance criteria

1. **ČÚZK provider (local test server with a synthetic feed and LAZ files):** a provider import produces
   one source with one object per sheet. It carries `ProviderOrigin`, the file hashes and "© ČÚZK".
   Re-importing after one sheet's ETag changes reports 1 changed, with the GUIDs kept.
2. **Copernicus provider (local server serving a synthetic COG):**
   - only the window covering the clip is read, which the test asserts through the number of range
     requests or bytes;
   - the warped grid matches the synthetic surface within half a cell;
   - the surface-model and vertical-datum warnings are present;
   - a foreign host and a plain-http URL are refused.
3. **Precedence:**
   - with a synthetic fine source inside a coarse one, the stack answers from the fine one inside its
     coverage and from the coarse one outside;
   - `source_index` in the tiles matches;
   - the seam statistics are reported.
4. **Cache:** a second identical import downloads nothing, and eviction respects the cap without evicting a
   stored source's files.
5. **Live (`-m network`, run in Verification, not in CI):**
   - import Kralupy DMR 5G (corridor at 250 m) and Copernicus GLO-30 (corridor at 2000 m) by provider, and
     list L0 to L4 tiles;
   - report the sheet and tile counts, the download sizes and times, and the seam statistics between DMR 5G
     and Copernicus. Expect metres where Copernicus includes trees;
   - report the mean height difference over open ground where both exist, as the datum-offset estimate.
6. **No regressions:** both suites are green on Windows and on Linux CI, with no previously passing test
   removed, skipped or weakened, and `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Imagery and shaded relief — T-134. Client rendering of terrain and of the source boundary — T-135. The
  client import dialog — T-136. Cut/fill — T-137.
- Vertical datum transformation and seam blending — not planned for Phase 1.
- DMP 1G (the surface model) as terrain — it is used for building heights in T-151, not as ground.

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run pytest -q -m network tests/test_terrain_providers.py
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- the exact Copernicus licence and attribution text, and the URL you took it from;
- every live figure from criterion 5;
- the cache layout and its index format;
- how the range-limited read was verified;
- the seam statistics and what they suggest for Phase 2 blending.
