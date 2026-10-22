# Terrain providers, the provider cache and source precedence

What T-133 adds on top of the import framework (`import-framework.md`), the ČÚZK client (`cuzk-atom.md`) and the
terrain tiles (`terrain-tiles.md`): terrain that is imported **by query** instead of from a file, the cache
those downloads live in, and the rule that decides which source answers where two overlap. ADR 0009 decisions 6
and 7 are the rules behind it. The code is `io/providers/` (`__init__.py` for the shared query helpers,
`cuzk_terrain.py`, `copernicus.py`, `cache.py`), the provider half of `io/sources/runner.py`,
`domain/terrain_surface.py` (precedence) and `io/mesh/terrain_tiles.py` (the source map).

## Providers

| `provider_id` | Supplies | Reader | Object key | Licence | Attribution |
|---|---|---|---|---|---|
| `cuzk-dmr5g` | ČÚZK DMR 5G ground points (LAZ, S-JTSK) | `las` | sheet code | CC BY 4.0 | `© ČÚZK` |
| `cuzk-dmr4g` | ČÚZK DMR 4G 5 m grid (GeoTIFF, S-JTSK) | `geotiff` | sheet code | CC BY 4.0 | `© ČÚZK` |
| `copernicus-glo30` | Copernicus DEM GLO-30, a 30 m global **surface** model | `geotiff` | tile name | see below | see below |

Providers are registered by `register_builtin_providers()` (idempotent; the server calls it at start-up) and
sit behind the `SourceProvider` seam of T-130 (`provider_id`, `kinds`, `licence`, `attribution`, `inspect(query)`,
`fetch(query, cache_dir) -> list[Path]`). A provider may also define `title` (the default source name),
`format_id` (the reader to use) and `normalise_query(query)` (the query with every default written out); the
three built-ins define all of them. Third-party providers (entry points) need none of these.

## Provider imports on the wire

`import.inspect` and `import.run` take, as the third alternative to `path` and `paths`:

- `provider`: a provider id;
- `query`: the provider's request, a JSON object (below).

Exactly one of `path`, `paths` and `provider` must be given, and `query` only with `provider`; anything else is
`E_BAD_PARAMS`. The result shapes are unchanged. Both need a project with a CRS (`E_NO_PROJECT`,
`E_CRS_REQUIRED`), because the query's default clip is a corridor around the project's alignments and a
bounding-box clip is in the project CRS. `import.inspect` still mutates nothing; both run their network and disk
work off the event loop (ADR 0003 addendum), and `import.run` is a write barrier like every import.

| Condition | Error |
|---|---|
| unknown provider, bad query (unknown field, wrong clip shape, `max_total_mb` <= 0), the selection is over `max_total_mb`, no sheet or tile covers the clip, a refused URL | `E_BAD_PARAMS` |
| the service or the network cannot be reached or misbehaves (after three attempts) | `E_BAD_PARAMS`, with the reason |
| a corridor clip in a project without alignments / with an unknown `alignment_id` | `E_EMPTY` / `E_NOT_FOUND` |
| a source that declares no CRS and no `horizontal_crs` in the settings | `E_CRS_REQUIRED` |
| missing `gis` extra, a provider that raised something unexpected | `E_INTERNAL` |

### The query

```json
{"clip": {"type": "corridor", "buffer_m": 250}, "max_total_mb": 500}
```

- `clip` has the wire shape of `ImportSettings.clip` (`corridor` with `buffer_m`, optional `alignment_ids`,
  `station_from`, `station_to`; or `bbox` with `min_e`, `min_n`, `max_e`, `max_n` in the project CRS). Left out, it
  is the corridor around every alignment: **250 m** for the ČÚZK providers, **2000 m** for Copernicus (the
  generic landscape reaches well beyond the detailed corridor).
- `max_total_mb` (ČÚZK only, default 500, decimal MB) is the download guard of T-139: over it, nothing is
  downloaded.
- Unknown fields are refused.

The pipeline writes the defaults out before anything happens (`normalise_query`), so the recorded request
reproduces the import.

### The pipeline

`runner.run_provider_import` does, in a worker thread:

1. normalise the query; if `settings.clip` is None it becomes the query's clip, so the readers keep what the query
   asked for (the stored settings carry it);
2. `provider.fetch(query, cache_dir)` into the provider cache, returning local files (which must lie inside it);
3. the ordinary reader and `run_import` of T-130/T-131: CRS resolution from the **confirmed settings** (a ČÚZK LAS
   declares no CRS, so `horizontal_crs` must be given; `inspect` proposes it), clip, read, content hashes,
   reconciliation;
4. the `SourceDataset` records `ProviderOrigin(provider_id, canonical_query, sha256)` (canonical JSON: sorted keys,
   no spaces; `sha256` the hash of every fetched file, the ZIPs for ČÚZK, the window GeoTIFFs for Copernicus), and
   the provider's `licence` and `attribution`;
5. the fetched files are pinned in the cache for the life of the source.

A re-import (`reimport_of`) fetches again (the cache revalidates, below), reconciles on the object keys and keeps
the GUIDs: re-importing after one sheet's ETag changed reports `changed: 1`, `unchanged: n - 1`.

Built-in providers read the project through `runner.provider_context()` (a `ContextVar` set around `inspect` and
`fetch`: project CRS and alignments), which is how a `fetch(query, cache_dir)` with no project argument can still
resolve a corridor clip.

`ProviderOriginDTO` gained `sha256` (default empty), `SourceDatasetDTO` gained `resolution_m`.

## `cuzk-dmr5g` and `cuzk-dmr4g`

Selection is T-139's: `load_sheet_index` (conditional, cached), `select_sheets` over the clip polygon,
`resolve_files`, `download` into the provider cache with the `max_total_mb` guard. Every URL passes
`cuzk_atom`'s https-only host allow-list and every feed is parsed with `defusedxml`; nothing here widens either.
A ČÚZK failure that the user can fix (the size limit) is a `ValueError`, any other one an `OSError`.

`inspect` reads feeds only (no data): `counts = {sheets, bytes}`, `extent` = the sheets' outlines in EPSG:5514,
warnings (count and size, over-limit, DMR 5G is all class 8, heights are Bpv), and the suggested settings:
`horizontal_crs: "EPSG:5514"`, `vertical_crs: "EPSG:5705"`, `point_classes: null` (**never a class filter**: F31,
all DMR 5G points are class 8), and the query's clip.

The object key is the member's stem, which for ČÚZK ZIPs is the sheet code (`KRAV82`). A sheet that touches the
clip's envelope but holds no point inside it contributes no object, with a warning (`KRAV93: no points inside the
clip region` for the Kralupy corridor).

## `copernicus-glo30`

Public cloud-optimised GeoTIFFs, one per 1 degree by 1 degree tile, on
`https://copernicus-dem-30m.s3.amazonaws.com/<tile>/<tile>.tif` with `<tile>` like
`Copernicus_DSM_COG_10_N50_00_E014_00_DEM` (the south-west corner). About 30 MB each, byte ranges supported.

- It is a **digital surface model**: it includes tree canopy and buildings, so it lies above bare ground where
  those stand. It is only the generic landscape.
- Heights refer to **EGM2008 (EPSG:3855)**. Phase 1 does not transform heights (ADR 0009), so `inspect` warns
  about both, and the suggested settings record `vertical_crs: "EPSG:3855"`.
- `horizontal_crs: "EPSG:4326"`; T-131's GeoTIFF reader warps each window to the project CRS (bilinear, the
  source's own resolution, about 30 m).

### Licence and attribution

The licence is the Copernicus WorldDEM-30 licence, the document titled "Licence for Copernicus DEM instance
COP-DEM-GLO-30-F Global 30m Full, Free & Open" / "Licence for the use of the Copernicus WorldDEM-30", published at
`https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/DEM/resources/license/License-COPDEM-30.pdf`
(read 2026-10-15; its terms are also referred to from the dataset's registry entry
`https://registry.opendata.aws/copernicus-dem/`). It grants worldwide, non-exclusive, free rights of
reproduction, distribution, communication to the general public, and adaptation, modification and combination,
without limit in time.

`licence` is recorded as `Copernicus WorldDEM-30 licence (COP-DEM-GLO-30-F, Global 30m Full, Free & Open)`.

Article 6 obliges the user, when communicating to the general public or distributing the data, to give the notice
(a):

> © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved.

and, where the data **have been adapted or modified**, notice (b). A clipped and warped window is adapted, so
`attribution` is notice (b), exactly:

> produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved

Article 6(c) also obliges whoever distributes or communicates the data to the general public to include, in the
licence or legal notice of that distribution: "The organisations in charge of the Copernicus programme by law or by
delegation do not incur any liability for any use of the Copernicus WorldDEM-30". Article 6(d): do not give the
impression that the user's activities are officially endorsed by the Provider, the Licensor or the Copernicus
programme. The client's attribution display (T-135) and any export (Phase 3) must carry these.

### The windowed read

`fetch` works out which tiles the clip touches (the clip's longitude/latitude bounds; a box that only touches a tile
edge does not use it; at most 25 tiles), then for each tile:

1. the window of that tile covering the clip, padded by two pixels for the bilinear warp, is read from the remote COG
   through HTTP **range requests** and written as a small local GeoTIFF (deflate) in the cache;
2. the file is `windows/<min_lon>_<min_lat>_<max_lon>_<max_lat>/<tile>.tif` (the folder is the window's bounds, the
   file name the tile name, so the reader's object key, the file stem, is the tile name);
3. a window that is already in the cache is reused without a request, so a second import of the same clip makes
   none. Windows are not revalidated (the DEM does not change); delete the cache folder to refresh.

The read goes through a file object of this module (`_RangeFile`, fetching aligned 128 KiB blocks, handed to
rasterio with `opener=`), **not** through GDAL's `/vsicurl/`: GDAL follows redirects without asking, and every URL
and redirect hop must pass the allow-list. An S3 403 or 404 means no tile (open water has none): the tile is skipped,
and when none exists the import is refused. A server that answers a range request with anything but 206 is an error
(the tile is never downloaded whole). A transient failure (5xx, timeout, short body) is retried twice with a 1 s then 2 s backoff.

**Bounds on every fetch.** A range response is refused before its body is read when its `Content-Range` does not answer the
requested range (start, end, total up to 256 MiB), when its `Content-Length` is over the requested length, or when the
range is over 16 MiB; the body is streamed in chunks and abandoned at the first byte past the requested length. A fetch
may send at most 5000 requests (redirect hops included) and receive at most 1 GB; either limit raises an `OSError` that
is not retried. Every request asks for `Accept-Encoding: identity`.

**Corrupted windows.** A cached window is reused only when `ProviderCache.verify` finds its size and SHA-256 as indexed;
otherwise it is unlinked and read again. Partial windows (`~*.part`) older than an hour are deleted when a fetch starts.

### Allowed hosts

`ALLOWED_SCHEMES = ("https",)` and `ALLOWED_HOSTS = ("copernicus-dem-30m.s3.amazonaws.com",)` (the host itself and
its subdomains, on the dot boundary), the provider host allow-list of ADR 0009 decision 7. The same rules as T-139:
https only, no userinfo, no whitespace, backslash or non-ASCII; redirects are followed by hand (at most 5) and each
`Location` is checked before it is requested; a refused URL is a `ValueError` and nothing is sent to it. Tests widen
the two tuples (and `BASE_URL`) to reach a local server and nothing else changes them.

### Known limits

- The licence notice of Article 6(c) is `LIABILITY_NOTICE` in `copernicus.py`; the client's attribution display (T-135) shows it.
- A clip across a tile boundary yields one grid object per tile; each warped grid ends at its tile edge, so a strip
  of at most one 30 m cell can be left without data along the boundary. The Kralupy corridor lies inside one tile.
- The DEM's tree and building heights are metres above ground (see the seam statistics below).

## The provider cache

`%LOCALAPPDATA%\COYPU Builder\cache\providers\<provider_id>\` on Windows,
`~/.cache/coypu-builder/providers/<provider_id>/` elsewhere (`cache.providers_cache_root()`, the one seam tests
replace). **Never inside the repository** (`ProviderCache` refuses it) and never in the user's data folder.

```text
providers/
  cuzk-dmr5g/
    index.json                    the cache's index (below)
    cuzk_manifest.json            T-139's download manifest
    DMR5G.index.json              the sheet index, with the feed's validators
    DMR5G/<code>.zip              the sheets
    DMR5G/feeds/<code>.json       each sheet's resolved file link
  cuzk-dmr4g/                     the same with DMR4G-TIFF/
  copernicus-glo30/
    index.json
    windows/<bounds>/<tile>.tif
```

`index.json`:

```json
{"version": 1,
 "files": [{"file": "DMR5G/KRAV82.zip", "size": 2321734, "sha256": "...", "url": "https://...",
            "etag": "\"...\"", "last_modified": "...", "fetched_at": "2026-10-15T10:00:00+00:00",
            "last_used": 1781000000.0}]}
```

- **Revalidation:** ČÚZK files are not fetched again while the manifest's ETag (or `Last-Modified`) and length still
  match the server's (one `HEAD` per sheet); a changed ETag downloads that sheet again. A second identical import
  downloads nothing.
- **Bound:** default 5 GB in total (`DEFAULT_CAP_BYTES`). After every fetch the cache deletes files, least recently
  used first, until it is within the cap. It never deletes the files of the fetch in progress, and never the files
  of a **stored source**: `ProviderCache.pin(owner, files)` is called with the source's id by every provider import,
  and re-importing replaces the pin. Pins live in the process, not in the index: a project reopened from disk (T-145)
  pins its sources' files again, and a file nobody pins is simply fetched again if it was evicted. When pinned files
  alone exceed the cap the cache stays over it.
- **Safe to delete by hand:** a file that is gone leaves the index silently; a damaged index is ignored.

## Precedence and the source map

`TerrainStack` orders the terrain sources by **declared resolution, finest first**: DMR 5G, then DMR 4G, then
Copernicus. Within a resolution band a **user's own file beats a provider** (ADR 0009 decision 6), and after that the
**newest import wins**.

- **Declared resolution.** Measured when the source is imported (`payload_resolution_m`, `source_resolution_m`) and
  recorded as `resolution_m` of the source, shown by `source.get`: for a point cloud the median distance from a
  point to its nearest neighbour (from up to 20 000 evenly spread query points against the whole cloud), for a grid
  the geometric mean cell size, for a TIN the median edge length; the finest of a source's objects. Kralupy:
  DMR 5G 1.32 m, GLO-30 30.30 m.
- **Bands.** Two surveys of one density measure a few per cent apart, which must not decide precedence, so sources
  are compared by `resolution_band` = `round(2 * log2(resolution))`, bands a factor of sqrt(2) wide. A resolution that
  is unknown is the coarsest band.
- `stack_from_sources(sources)` builds the ordered stack from `StackSource(source_id, surface, resolution_m,
  recency, from_provider=False)`; the sort key is `(resolution_band, from_provider, -recency)`. `from_provider` is set
  for a source whose origin is a `ProviderOrigin`. `TerrainStack(surfaces, source_ids=())` keeps the old constructor.
- `TerrainStack.heights_and_source(e, n) -> (heights, source_index)`: the first surface's value and its index in
  `surfaces` (`NO_SOURCE` = -1 where none has data). `heights` is `heights_and_source(...)[0]`.

### Tiles

`terrain.tile` returns a second blob next to `heights`:

| Blob | dtype | Shape | Meaning |
|---|---|---|---|
| `source_index` | `<u1` | (129, 129) | laid out like `heights`; the index into `source_ids` of the source that answered at the sample (0 = finest); 255 where no source has data (exactly where `heights` is NaN) |

`TerrainTileResult.source_ids` and `TerrainTilesResult.source_ids` carry the ordered source ids of that revision
(only sources that are in the stack: one that could not be triangulated is skipped with a warning). A tile whose
surface is not a stack (the domain tests) is source 0 where it has data.

The dtype is written `<u1` on the wire: numpy writes `|u1` for one-byte types, and `codec.pack_blobs` rewrites it to
`<u1` so that every blob spec reads the same. The
The Godot envelope decoder has a `<u1` case that returns the bytes as a `PackedByteArray` of indices.

### Seams

Where two sources meet, `terrain.tiles` reports the height step in its `warnings`. For each source and each source
below it, the boundary of the upper one's coverage (its clip polygon cut to its data, inset by twice its resolution
so it has data there) is sampled every 10 m (at most 4000 points), and where both surfaces have data:

```text
seam between 'ČÚZK DMR 5G' and 'Copernicus DEM GLO-30' (heights are not blended): |dH| mean 0.99 m, 95th percentile
4.13 m over 3494 points along the boundary of 'ČÚZK DMR 5G'
```

Nothing is blended in Phase 1.

## Measured on Kralupy (live, not part of CI)

Corridor 250 m for DMR 5G and 2000 m for Copernicus, by provider (`pytest -m network tests/test_terrain_providers.py`):

| | |
|---|---|
| DMR 5G sheets / objects | 15 selected, 14 objects (KRAV93 holds no point in the clip) |
| DMR 5G download | 34.04 MB; first run 7.1 s including the download; re-import 3.8 to 4.5 s with no download; 845 232 points |
| GLO-30 | 1 tile; 5 range requests, 5.37 MB transferred of a 29.76 MB file (18 %); 1.1 to 1.4 s; window 570 kB, 143 166 cells; re-import from the cache 0.2 s |
| Tiles L0 / L1 / L2 / L3 / L4 | 362 / 103 / 34 / 11 / 6 (the Copernicus coverage reaches 2 km beyond the corridor) |
| Seam DMR 5G / GLO-30 | mean 0.99 m, 95th percentile 4.13 m (3494 points) |
| GLO-30 minus DMR 5G, 10 446 points inside the corridor | mean +0.99 m, median +0.11 m, lower-half mean -0.31 m, 10th to 50th percentile band -0.20 m, 5th percentile -0.59 m, 95th percentile +6.31 m |

The lower half of the differences (open ground: trees and buildings only raise a surface model) is within about
0.3 m of zero, so the EGM2008-to-Bpv datum offset is small here; the mean and the upper percentiles are canopy.
