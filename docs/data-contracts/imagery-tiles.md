# Imagery display tiles

What T-134 (Part A) adds on top of the terrain tiles of `terrain-tiles.md`: an orthophoto, or shaded relief where
there is none, composed onto the terrain tile grid for display. ADR 0009 decisions 5, 6 and 9 are the rules behind
it: display tiles are never used for analysis, sources layer by precedence, and every contributing source's
attribution is visible. The code is `domain/imagery.py` (rasters, layers and composition, numpy only),
`domain/relief.py` (hillshade and the colour ramp, numpy only), `io/imagery/` (the file reader and the layer
factories), `io/mesh/imagery_tiles.py` (the tile composition and the JPEG encoding) and the `imagery.tiles` /
`imagery.tile` handlers in `server/handlers.py`.

The two ČÚZK orthophoto providers (T-134 Part B) are described in their own section below: `cuzk-ortho-wmts`
streams tiles while the view shows them, and `cuzk-ortho-sheets` downloads whole sheets for offline work.

## Grid

The tile grid is the terrain grid of `terrain-tiles.md`: the same level (0 to 4), the same `ix`, `iy` and edge
(`512 * 2**level` m), the same square in the project CRS. An imagery tile is **512 x 512 pixels** over that
square, so level 0 is 1 m per pixel and a coarser level is 2, 4, 8 or 16 m per pixel.

Pixel conventions differ from the height grid, and the difference is deliberate:

| | row 0 is | column 0 is | sample position |
|---|---|---|---|
| imagery pixels | the **north** edge | the west edge | pixel `[r, c]` centred at `E = min_e + (c + 0.5) * edge / 512`, `N = max_n - (r + 0.5) * edge / 512` |
| terrain heights | the **south** edge | the west edge | lattice points, `129 x 129`, spacing `edge / 128` |

The client drapes an imagery tile over its heightfield tile with no resampling, so the two grids must agree on
the square, not on the row order: row 0 of an image is the top of the picture.

## Imagery rasters

`ImageryRaster` is the payload of an imagery object: a regular grid of 8-bit colours (`rgb`, `(rows, cols, 3)`
uint8, row 0 north) with a validity mask (`valid`, bool; false means no data, transparent, or outside the clip),
its outer corner and cell sizes in the project CRS, and `native_resolution_m`, the source's own cell size before
any averaging. That last value orders the layers: the finest source composes first.

### Reading a file (`kind = imagery`, format `raster-image`)

`RasterImageReader` reads GeoTIFF, JPEG and PNG images. Georeferencing comes from the GeoTIFF tags or from a world
file beside the image: `.jgw` / `.jpgw`, `.pgw` / `.pngw`, `.tfw` / `.tifw`, or `.wld`. A world file carries no
CRS, so such an image declares none and `import.run` needs `horizontal_crs`, as for any source with no declared
CRS (ADR 0009: never guessed).

- **Bands:** 1 band is grey (copied to R, G and B), 3 bands are RGB, and any 4-band file is read as RGBA where
  alpha 0 means not valid. GDAL's internal masks are ignored. Any other count is refused at `import.inspect` with
  the count in the message. Samples must be 8-bit (`uint8`); an indexed-colour (palette) image is refused with a
  message asking for conversion to RGB.
- **Nodata:** when the image declares a nodata value, a pixel is invalid only when every band equals it, so a
  pixel where one band happens to match stays valid.
- **ZIP:** a ZIP holds one supported image. The world file stored beside that image in the archive (same stem,
  a world-file suffix, at most 4096 bytes, not encrypted) is copied next to the extracted image, so the sheet is
  georeferenced as the bare file would be.
- **Window and decimation:** only the window that covers the clip is read, averaged (rasterio's average
  resampling) to `display_resolution_m`, which defaults to 1 m and is never finer than the source.
- **Another CRS:** the image is warped onto a grid in the project CRS by nearest neighbour, at the larger of
  `display_resolution_m` and the source's own cell size in the project CRS. The reprojection is GDAL's
  `WarpedVRT` (nearest), pinned to the project's pyproj transform by `test_warped_cells_sit_where_pyproj_puts_them`
  for EPSG:4326 and EPSG:32633 sources.
- **Size:** at most `MAX_PIXELS = 100 000 000` pixels per object, after decimation. Above that the reader
  refuses and asks for a coarser `display_resolution_m` or a clip.
- **Kind routing:** `reader_for(path, format_id, kind)` tries the readers of the given kind first when no format is
  named; a file none of them recognises goes to any other reader, so the kind check can say what is wrong. A
  GeoTIFF is therefore an imagery file for `kind = imagery` and a terrain file otherwise.
- **Content hash:** the object's hash covers the image and its world file, so a changed world file is a changed
  object on re-import.

## ČÚZK orthophoto providers (T-134 Part B)

Both providers supply the ČÚZK orthophoto (EPSG:5514, CC BY 4.0, attributed "© ČÚZK"). The code is
`io/imagery/cuzk_wmts.py` (the streamed provider, its `wmts-descriptor` reader and `WmtsLayer`) and
`io/imagery/cuzk_bulk.py` (the sheets provider); both are registered by `register_imagery_readers()`.

| `provider_id` | Supplies | Reader (format) | Object key | Default `max_total_mb` |
|---|---|---|---|---|
| `cuzk-ortho-wmts` | the ORTOFOTO WMTS, one `WmtsSource` per import | `wmts-descriptor` (`.wmts`) | `ORTOFOTO/default028mm` | not applicable |
| `cuzk-ortho-sheets` | ORTOFOTO sheets (JPEG with world files, in ZIPs) | `cuzk-ortho-sheet` (`CuzkSheetReader`: a sheet read as `raster-image` reads it) | the sheet code, which leads the cached file name `<code>.<edition>.zip`; the ZIP's members are named by edition (`WRTO24.2025.KRAV82.jpg`), so their stem is not the code | 200 |

Both declare `licence` "CC BY 4.0", `attribution` "© ČÚZK" and one `notices` entry, "Licensed under CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). Resampled for display.", and their `title` and `description`
are the sentences of the import dialog. `import.formats` lists them with those attributes.

### Streamed: `cuzk-ortho-wmts`

- **Import path.** `fetch` reads the GetCapabilities document (one request, through the allow-list and
  `defusedxml`) and writes `ORTOFOTO.wmts`, a small LF JSON descriptor in the provider cache: the tile URL template,
  the layer, the TileMatrixSet, the parsed TileMatrices and the clip box, all in EPSG:5514. Nothing else is
  downloaded at import. The reader turns the descriptor into a `WmtsSource` payload and checks its URL against the
  allow-list too, so a descriptor on disk cannot name another host.
- **Corner and orientation.** Each TileMatrix's `TopLeftCorner` is read through `io/landxml/dialects.py` with
  native order "EN" (East first): the service's `MatrixWidth` counts the East tiles, and the layer bounding box
  agrees. Row 0 is the northern row. A TileMatrix pixel is `ScaleDenominator x 0.00028` m.
- **TileMatrix choice.** For a window of edge `E` at `size` pixels, the coarsest TileMatrix whose pixel is at most
  `E / size x 1.001` is used, and the finest when none is. The 0.1 % tolerance is the decision of 2026-11-23: the
  ČÚZK `default028mm` matrices sit 0.0127 % above the powers of two (TileMatrix 11 is 1.00013 m), and a strict
  comparison would take the next finer matrix at every level, four times the tiles. For the five display levels:

  | level | pixel `edge / 512` | TileMatrix | matrix pixel | tile edge |
  |---|---|---|---|---|
  | 0 | 1.0 m | 11 | 1.00013 m | 256.03 m |
  | 1 | 2.0 m | 10 | 2.00025 m | 512.07 m |
  | 2 | 4.0 m | 9 | 4.00051 m | 1 024.13 m |
  | 3 | 8.0 m | 8 | 8.00102 m | 2 048.26 m |
  | 4 | 16.0 m | 7 | 16.00203 m | 4 096.52 m |

  A matrix pixel must lie between 0.01 m and 100 000 m, a tile side is at most 1 024 px, and a TileMatrix that
  breaks either is refused when the capabilities or a descriptor are read.
- **Fetching.** `WmtsLayer.render` fetches only the tiles that cover the window **and the clip**, through a
  process-wide limit of four requests in flight (`TILE_CONCURRENCY`, shared by every render of the process). Each
  tile body is capped at 2 MB, by its `Content-Length` and by the bytes read; an image is checked for its size
  before its pixels are decoded. Retries are three attempts with 1 s and 2 s backoff, for 5xx, 429, timeouts and
  dropped connections; a 403 or 404 is not retried. The connect timeout is 4 s, the read timeout 15 s, and each
  render has a 15 s deadline: tiles not in by then degrade at once.
- **Limits.** A render that needs more than 64 tiles, or a mosaic over 64 Mpx (decimal), fetches nothing: it
  degrades to relief with one warning. The tile counts are computed from the span of the window before any tile is
  listed, so a hostile matrix cannot make the backend build a huge list.
- **Clip.** The window is intersected with the clip box before the tiles are chosen, and a pixel whose centre lies
  outside the clip is never valid. The descriptor's clip is narrowed by the import's clip (its bounding box) when
  the sheet is read.
- **Transparency.** A tile's alpha is part of its validity: an RGBA, LA or PA image, or a P or RGB image with a
  transparency key, has its transparent pixels not valid, so relief shows through there, as `raster-image` treats an
  RGBA source. Pillow's decoder errors (garbage, a truncated image, a decompression bomb) are failed tiles.
- **Failure.** A tile that still fails, or that is not an image of the matrix's size, is not valid: relief shows
  through there, and the layer adds a warning naming the tile and the reason. `imagery.tiles` reports those
  warnings from the next listing on. It is never an error.
  - **Warnings are for people.** The reason is a short phrase: "cannot connect", "timed out", "HTTP 503",
    "unusable tile". The exception text goes to the log only.
  - **Warnings clear.** A tile's warning is dropped when that tile later loads. When the breaker is closed and a
    render completes with nothing missing, every tile warning clears.
- **No tile here is an answer.** A 403 or 404 means the service has no tile there, for example at the edge of
  coverage. Those pixels are not valid, but the composition still counts as complete, so the client does not
  retry it. A small marker beside where the tile would be (`<col>.absent`) remembers the answer for 30 days, and no
  request is made for that tile while it is fresh. The marker goes when the tile loads.
- **Stale copies beat none.** A tile whose stored copy is on disk is shown from that copy when its revalidation has
  not answered by the render's deadline, stale or not. The copies are read before any request is sent.
- **Circuit breaker.** One breaker per host, in the process, with an injectable clock for the tests:
  - only a transient failure that exhausted its retries (5xx, 429, a timeout, a connection error) counts; a 403 or
    404, an oversized body or an image that does not decode does not. A failed revalidation is the exception: its
    failure, or its bad body, counts, and it keeps the stale copy;
  - it trips after 3 exhausted tiles in a row, and a success resets the count;
  - while open, no request is made and no backoff is slept; the workers in flight check it before each attempt
    and between retries. It stays open for 30 s, doubling after each failed probe up to 5 min;
  - after the cool-down one probe is admitted, with a single attempt; its success closes the breaker, its failure
    reopens it with the doubled cool-down;
  - while it is open, the layer's warning is one aggregated line, for example "ČÚZK orthophoto is not reachable (HTTP
    503); shaded relief is shown; retrying in 30 s". It disappears when the breaker closes;
  - a worker that waited for a fetch slot re-checks the breaker and the deadline before it sends anything, and the
    body loop checks the render's deadline;
  - a bad revalidation body counts as a failure, without a success being credited first; the success of an answer
    is credited only once its body has decoded.
- **Disk errors degrade.** A store, load, unlink or cap failure (`OSError`, Windows sharing violations included)
  never raises out of `render`: the decoded tile is used uncached. The cap's running total is seeded by one walk per
  process; the folder is walked again only when that total is over `TILE_CACHE_CAP_BYTES`. A validator sidecar
  that is valid JSON but not an object is ignored, and the tile is fetched without validators.
- **Degraded compositions.** A composition with a tile that could not be loaded is answered with
  `complete: false` on `imagery.tile`, and it is **not** put in the imagery LRU, so the next request composes it
  again and fetches what is missing. Completeness is reported per thread (a `tile_report` around the composition),
  so renders that run in parallel do not mix.
- **Licence notice.** Both providers carry `notices`: "Licensed under CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/). Resampled for display." The attribution "© ČÚZK" is unchanged.
- **Cache layout.** The provider's cache folder (`providers/cuzk-ortho-wmts/`):

  ```text
  cuzk-ortho-wmts/
    ORTOFOTO.wmts                  the descriptor (LF JSON, no timestamp, so an unchanged service keeps its hash)
    <TileMatrix>/<row>/<col>.jpg   each tile as the service answered it (.png for a PNG)
    <TileMatrix>/<row>/<col>.json  its validators: ETag and Last-Modified
  ```

  A tile older than 30 days (`REVALIDATE_AFTER_S`) is revalidated with `If-None-Match` and `If-Modified-Since`. A
  304 refreshes its modification time and fetches no body; a failed revalidation keeps the cached copy. The folder
  is bounded by `TILE_CACHE_CAP_BYTES` (5 GB by default, the provider cache's cap). Its image bytes are a running
  total, seeded by one `os.scandir` walk per process and corrected by each eviction: the tree is walked again only
  when the total is over the cap, and then the tiles fetched or revalidated longest ago go first, never those the
  current render wrote. Tiles are not pinned to a source, so an evicted tile is simply fetched again.

### Bulk: `cuzk-ortho-sheets`

`cuzk-ortho-sheets` is T-139's ORTOFOTO selection (`load_sheet_index`, `select_sheets`, `resolve_files`,
`download`) with the terrain providers' error mapping. Its `inspect` reports the sheet count, the total size and the
extent, and warns when the total is over `max_total_mb`. Its `fetch` refuses before any sheet is requested when the
total is over the limit (default 200 MB; decimal), and records each sheet's edition as a warning of the import
("orthophoto editions: KRAV82 WRTO24.2025"). The cache holds `ORTOFOTO/<code>.<edition>.zip`, the ORTOFOTO index and
T-139's manifest, in `providers/cuzk-ortho-sheets/`. Sheets are EPSG:5514, and their world files carry no CRS, so
`horizontal_crs` is EPSG:5514 in the settings. The ZIP is read by `CuzkSheetReader`, which reads the member as
`raster-image` does and keys the object by the sheet code, taken from the cached file name: the member of a ČÚZK
sheet is named by its edition (`WRTO24.2025.KRAV82.jpg`), so `raster-image`'s own key would be that stem, not
`KRAV82`. The reader is chosen by the provider only; sniffing never picks it.

### Measured live (2026-11-23, not part of CI)

`pytest -m network -k live_kralupy -s`, on a 1 km box at the centre of sheet KRAV82 (nine level-0 tiles), measured
after the TileMatrix tolerance change and the review fixes:

| | |
|---|---|
| Import (capabilities and descriptor only) | 0.23 s |
| WMTS tile requests (TileMatrix 11) | 25, all distinct, stored as 25 files, 684 602 bytes (about 27 KB each) |
| Cold composition of the nine level-0 tiles | 1.73 s |
| Warm composition, the same tiles | 0.421 s, **no request** |
| Composed level-0 JPEG | 5 827 to 61 557 bytes |
| Placement | the level-0 tile at the sheet centre is TileMatrix 11, row 410, column 698: a real image (pixel standard deviation 22.8), no warning |
| ZIP tail, one ranged GET | 65 536 bytes; its members are `WRTO24.2025.KRAV82.jpg` and `.jgw`, and two further `.jgw` files (`TM33N`, `TM34N`); the sheet key is `KRAV82` |
| ORTOFOTO sheets for the box | 1 sheet, 58.50 MB (not downloaded) |

Pending: the 2 km mosaic at 1 024 pixels that the earlier report described (TileMatrix 12) has not been measured
again under the new TileMatrix rule.

## Layers and composition

Each imagery object is an `ImageryLayer` (`RasterLayer` for `ImageryRaster`, `WmtsLayer` for a streamed ČÚZK
source). Its `render(bounds, size)` returns
the colours and validity over a square window:

- the subsampling factor is `k = clamp(ceil(pixel / cell), 1, 8)`, where `pixel = edge / size` and `cell` is the
  smaller of the raster's cell sizes;
- each pixel is sampled at `k x k` sub-points, each taking its nearest raster cell;
- a pixel's colour is the mean of its valid sub-samples, rounded half up, and the pixel is valid when at least
  half of its sub-samples are.

`compose_layers` takes the layers finest `resolution_m` first (ties keep the caller's order). For each pixel the
first layer with a valid pixel wins. A layer whose bounds do not meet the window is not rendered. Imagery
sources are passed newest first, so among equal resolutions the newest import wins.

### Relief

Where no imagery is valid, a pixel shows shaded relief from the **analysis surface** (`TerrainStack`), sampled at
the pixel centres and at a one-pixel border, so the hillshade is defined at the edge:

- `hillshade`: central differences over the 3 x 3 neighbourhood, sun at azimuth 315 degrees clockwise from north
  and altitude 45 degrees; NaN where any of the five heights is not finite;
- `hypsometric`: colour by height, linear between four stops (`RAMP` in `domain/relief.py`), with `t` clipped to
  `[0, 1]` over the **height range** of the imagery revision;
- `relief`: `rgb = round(hypsometric * (0.25 + 0.75 * hillshade))`.

The height range is the 2nd and 98th percentile of the finite heights sampled on a 256 x 256 lattice over the
stack's bounds. It is computed once per imagery revision, so neighbouring tiles agree on the ramp.

A pixel that neither imagery nor relief covers is transparent (alpha 0) and black `(0, 0, 0)`.

## Tile encoding

`imagery.tile` returns the composed tile as:

- `image`: the JPEG of the RGB tile, baseline, quality 85, no EXIF; transparent pixels are black in the JPEG;
- `alpha`: `<u1` `(512, 512)`, 0 or 255, present only when some pixel is transparent (`has_alpha`). Uncompressed,
  so it is 262 144 bytes whenever it is present.

`source_ids` lists the imagery sources that won a pixel, in precedence order, then `"relief"` if relief filled any
pixel. Measured on synthetic data at level 0 (1 m pixels, 2 m terrain), composition takes about 75 ms warm (the
first call in a process is about 0.3 s): a fully imaged tile has a 36 064-byte JPEG and no alpha blob; a tile
half imaged with relief elsewhere, with a transparent strip, has a 21 770-byte JPEG and a 262 144-byte alpha blob.

## Methods

Both are `experimental`, `mutates = false`. A project with no terrain lists no tiles.

| Method | Params | Result |
|---|---|---|
| `imagery.tiles` | `level` | `level`, `revision`, `terrain_revision`, `tiles` (per terrain tile: `level`, `ix`, `iy`, `edge_m`, `source_ids` of the imagery layers whose bounds meet the tile; nothing composed), `attributions`, `notices`, `warnings` |
| `imagery.tile` | `level`, `ix`, `iy`, `revision` | `level`, `ix`, `iy`, `edge_m`, `revision`, `width` and `height` (512), `has_alpha`, `source_ids`, `complete`; blobs `image` and, when `has_alpha`, `alpha` |

- **Revisions.** `revision` comes from the process-wide counter that `terrain.tiles` uses. It changes when the
  terrain **or** the imagery sources change. `terrain_revision` changes only when the terrain sources change. An
  imagery-only import keeps the terrain stack: the stack is not rebuilt.
- **Stale or unknown.** An `imagery.tile` whose `revision` is not the current one, or whose `(ix, iy)` is not a
  terrain tile of that level, is `E_NOT_FOUND`. The client lists again.
- **Attribution.** `attributions` maps every imagery and terrain source to its attribution string, and nothing else.
  The client shows those of the sources in view (ADR 0009 decision 9).
- **Notices.** `notices` maps each of those sources to the licence notices its provider declares, as a `notices`
  class attribute on the provider. A file source has none, so its entry is an empty tuple. The Copernicus provider
  declares the liability notice of its licence (article 6(c), see `terrain-providers.md`).
- **Off the event loop.** Composition and the relief range run in worker threads under the connection's gate.
  Concurrent requests for one tile share one composition (`ImageryCache.composing`), as terrain bakes do.
- **Cache.** Per project, in `ImageryCache` (`server/session.py`): the layers, attributions, notices and warnings
  (taken from the sources when first used for a revision), the relief range, and an LRU of 64 composed tiles
  (`IMAGERY_TILE_CACHE_SIZE`). A composition with a missing streamed tile (`complete: false`) is not put in the LRU.
- **Completeness.** `complete` is false when a streamed layer could not load one of the tiles of the composition; the
  client asks again later (T-135 Part C). It is true for every file source and for every complete composition.
- **Size.** A response stays under 1 MB. A listing holds at most
  `min(5000, 900 000 // (70 + 40 * imagery layers))` tiles, and a level above that is `E_BAD_PARAMS`. A single
  `imagery.tile` is tens of kilobytes at level 0, plus the 262 144-byte alpha blob when present.

## Not yet

- Client draping and imagery choice (T-135, T-136).
- Global imagery outside Czechia (deferred, ROADMAP "Source decisions"), and the ČÚZK Web Mercator and WMS
  variants (not needed while the EPSG:5514 service exists).
