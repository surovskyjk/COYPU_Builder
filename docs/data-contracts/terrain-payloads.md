# Terrain payloads and readers

What the three built-in terrain readers (T-131) hand to the analysis-surface and tile builders (T-132). The
code is `domain/terrain.py` (payload types, pure numpy) and `io/terrain/` (`las.py`, `raster.py`,
`landxml_surface.py`, `archive.py`). They plug into the reader registry of the import framework
(`import-framework.md`); `io.terrain.register_builtin_readers()` registers them, idempotently, when the server
module loads. `laspy` (with the `lazrs` LAZ backend), `rasterio` and `defusedxml` come from the `gis` extra
and are imported inside the reader that needs them.

## Payloads

All arrays are float64 in the project CRS `(E, N, H)`; nothing is narrowed to float32 before T-132 builds
display tiles. Equality on the dataclasses is identity (`eq=False`); compare the arrays.

| Type | Fields | Notes |
|---|---|---|
| `TerrainPoints` | `points (n, 3)`, `classes (n,) uint8 \| None` | clipped point cloud; LAS always fills `classes` |
| `TerrainGrid` | `origin_e`, `origin_n`, `cell_e`, `cell_n`, `heights (rows, cols)` | row 0 is the northernmost row; `origin_*` is the outer corner of cell `[0, 0]`; NaN = nodata or outside the clip. `bounds` and `centres()` are provided |
| `TerrainTin` | `points (n, 3)`, `triangles (m, 3) int32` | counter-clockwise in plan view; every point is used by a triangle |

## Readers

| `format_id` | Reads | Object per | Key |
|---|---|---|---|
| `las` | `.las`, `.laz`, or a `.zip` holding exactly one | file | the file's stem |
| `geotiff` | `.tif`, `.tiff` (COG included), or a `.zip` holding exactly one | file | the file's stem |
| `landxml-surface` | `.xml`, `.landxml` with a `<Surface>`, or a `.zip` holding exactly one | `<Surface>` | `<file stem>/<surface name>`, or `<file stem>/<0-based index>` for an unnamed surface, so two files may each have a surface `EG` |

Common rules:

- `content_hash` is the SHA-256 of the file as read (the extracted member for a ZIP, so it equals the bare
  file's hash; every surface of one LandXML file shares the file's hash). The SHA-256 recorded in the
  `SourceDataset` origin is that of the file given, i.e. the ZIP's.
- A ZIP member is extracted under a fixed name into a private temporary directory. Only members with a
  supported suffix count: none, or more than one, is a `ValueError`. An encrypted or damaged archive is a
  `ValueError`.
- ZIPs are untrusted. A member is refused before anything is written if it declares more than 8 GiB, or
  expands more than 100:1 beyond a 16 MiB allowance (a ZIP bomb); the declared size is enforced while
  streaming. Sniffing reads a bounded head of the member (4 MiB for LandXML, 4 bytes otherwise) and never
  extracts. During one `inspect_source` or `run_import` call a member is extracted at most once (inspect,
  hash and read share it) and the files are deleted when the call ends; `run_import` releases a file's
  extraction after reading it, and the scope drops the oldest extractions beyond 4 GiB.
- An object that is empty after the class filter, nodata handling and clip (a sheet entirely outside the
  corridor) is not yielded, so it is not in the source, and the import gets a warning naming the file and the
  cause (for example `KRAV82: no points with classes (2,); this file's classes are [8]`). If every file is
  empty the import also warns that it produced no objects. Re-importing with a wider clip adds an emptied
  object with a fresh GUID. Readers raise these warnings with `runner.note()`.
- The "declares no coordinate reference system" warning comes from the import framework (once, from
  `inspect`), not from the readers, so an import with `horizontal_crs` set does not repeat it.
- Native coordinate order: `las` and `geotiff` declare `"EN"`, `landxml-surface` `"NE"`
  (`coordinate_order = "auto"`; an explicit `EN`/`NE` wins). The Křovák sign rule is the one of
  `io/landxml/dialects.py`.
- Reader errors that are the input's fault are `ValueError`, so the handlers answer `E_BAD_PARAMS`.

### LAS / LAZ

- Inspect reads the header, plus at most 5 000 000 points for a class histogram: the whole file when it is
  smaller (so the counts are exact), else 50 blocks spread evenly over it, so a file sorted by class still shows its classes (a class
  rarer than the sample can be missed). Counts: `points` (the file), `point_format_<id>` (1 per file, so it
  sums across files), `sample_points` and `sample_class_<code>` (counts in the sample, not in the file). Extent is the header's `mins`/`maxs`. The CRS comes from a WKT or GeoKeys VLR; a file with none is
  `declared_crs: null` with a warning, and imports only with `horizontal_crs` set.
- Coordinates are `integer * scale + offset`, evaluated by `laspy` in float64 and fed to the CRS transform as
  float64. Křovák values near -1 025 000 m would lose about a decimetre in float32.
- Read in chunks of 1 000 000 points; the order of operations per chunk is: drop non-finite points, keep
  `settings.point_classes`, drop `settings.nodata` heights, transform, clip. Memory is the kept points.

### GeoTIFF / COG

- Inspect: `declared_crs`, `extent` (the bounds), `counts = {width, height, bands}`, `suggested.nodata` from
  the file (when finite), a `resolution: cell size A x B (source units)` note in the warnings (counts hold
  integers only; the note names the files it concerns when it is not common to all of a source's files), and
  warnings for a missing geotransform, rotation, or more than one band (band 1 is read). Across several
  files `width`, `height` and `bands` are sums: read the cell size from the per-file notes, not from them.
- Only north-up, unrotated rasters are read. A raster with positive Křovák coordinates (X south, Y west) is
  refused; `coordinate_order = "NE"` is refused because a raster's own transform is east-north.
- Source CRS = project CRS: the window covering the clip's bounding box is read (offsets within 1e-9 of a
  cell edge snap to it) and returned cell for cell; cells whose centre is outside the clip polygon are NaN.
- Another CRS: a grid in the project CRS is built over the footprint intersected with the clip, with cells of
  the source's own size measured in the project CRS (or `display_resolution_m`), with its origin snapped to a
  multiple of the cell size so neighbouring sheets align. Each cell centre is mapped back into the source with
  the same pyproj operation as the point readers (not GDAL's), and sampled bilinearly. A cell is NaN when it
  lies off the raster or when a source cell it has non-zero weight on is nodata.
- Nodata comes from the file's nodata value and mask, plus `settings.nodata`; non-finite values are NaN.
  `linear_unit_m` scales the cell size, origin and heights.
- More than 100 000 000 cells per object is refused: set a clip.

### LandXML surface

- Points are `<P id>` "north east elevation" resolved through `resolve_coordinates` like every LandXML token;
  faces are `<F>` point ids. Ids need not be consecutive or numeric. Every `<P>` must hold exactly three
  numbers and every `<F>` exactly three ids; a mix such as 2 and 4 values is refused, not re-paired.
- Faces with `i="1"` or `i="true"` are dropped, clockwise faces are reversed, and points no kept face uses
  are dropped. A face naming an unknown point id is a `ValueError`.
- Clip: a triangle is kept if any vertex is inside the polygon; unused points are then dropped and the
  indices renumbered.
- Inspect counts `surfaces`, `points`, `faces` (all `<F>`) and, when present, `invisible_faces`. The CRS is
  `<CoordinateSystem>` (`epsgCode`, else the first integer of an `EPSG:` in `desc` or `name`, so
  `EPSG:5514 + EPSG:5705` is 5514); a code pyproj does not know is no declaration, with a warning, and
  `horizontal_crs` then rescues the file. Sniffing looks for a LandXML root and a `<Surface>` in the first
  4 MiB; a file that lists its surfaces later needs `format_id`; the suggested `linear_unit_m` comes
  from `<Units>`. A surface without faces is warned about and skipped on import.
- The file is streamed with `defusedxml` (DTDs are refused) and `P`/`F` elements are released as they are
  read.

## Several files, one source

`import.inspect` and `import.run` take `paths` (exactly one of `path` and `paths`). All files must resolve
to the same reader, otherwise `E_BAD_PARAMS`. The inspect report sums the counts, unites the extents,
merges warnings (a warning not common to all files names the files it concerns), and when the files declare
different CRSs reports `declared_crs: null` with a warning, so the import needs an explicit `horizontal_crs`
rather than applying one file's CRS to the rest. The `SourceDataset` origin lists every path and hash in the
order given; keys are per file, so re-import (`reimport_of`) reconciles across the files. A source's default
name is the folder the files share, else the first file's stem.

`run_import` inspects every file itself before reading (the reader's `inspect` runs once per file in
`import.inspect` and again in `import.run`). The extra pass is cheap for headers and rasters but parses the
whole file for LandXML; it is left as is for now.

## Measured on the real ČÚZK sheets (Kralupy, not part of CI)

The DMR 5G sheets are LAS 1.4, point format 6, with no VLR at all (LASzip's own record is consumed by the
reader), so no CRS is declared. Every point of the sampled sheets is class 8. Fifteen sheets hold 6 338 002
points; a 250 m corridor around the alignment keeps 845 232 of them in 14 objects, the fifteenth lying
outside the corridor.
