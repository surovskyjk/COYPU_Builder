# Imagery display tiles

What T-134 (Part A) adds on top of the terrain tiles of `terrain-tiles.md`: an orthophoto, or shaded relief where
there is none, composed onto the terrain tile grid for display. ADR 0009 decisions 5, 6 and 9 are the rules behind
it: display tiles are never used for analysis, sources layer by precedence, and every contributing source's
attribution is visible. The code is `domain/imagery.py` (rasters, layers and composition, numpy only),
`domain/relief.py` (hillshade and the colour ramp, numpy only), `io/imagery/` (the file reader and the layer
factories), `io/mesh/imagery_tiles.py` (the tile composition and the JPEG encoding) and the `imagery.tiles` /
`imagery.tile` handlers in `server/handlers.py`.

Part B (the ČÚZK WMTS and bulk orthophoto providers) is not part of this document yet; it will add its providers
here when it lands.

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

## Layers and composition

Each imagery object is an `ImageryLayer` (`RasterLayer` for `ImageryRaster`). Its `render(bounds, size)` returns
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
| `imagery.tile` | `level`, `ix`, `iy`, `revision` | `level`, `ix`, `iy`, `edge_m`, `revision`, `width` and `height` (512), `has_alpha`, `source_ids`; blobs `image` and, when `has_alpha`, `alpha` |

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
  (`IMAGERY_TILE_CACHE_SIZE`).
- **Size.** A response stays under 1 MB. A listing holds at most
  `min(5000, 900 000 // (70 + 40 * imagery layers))` tiles, and a level above that is `E_BAD_PARAMS`. A single
  `imagery.tile` is tens of kilobytes at level 0, plus the 262 144-byte alpha blob when present.

## Not in Part A

- The ČÚZK WMTS and bulk orthophoto providers (Part B).
- Client draping and imagery choice (T-135, T-136).
- Global imagery outside Czechia (deferred, ROADMAP "Source decisions").
