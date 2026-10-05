# Corridor envelope

The corridor envelope is a buffer polygon around the plan centreline of one or more alignments. It has two
jobs: it is the area of interest a user loads into a data portal (for example the ČÚZK Geoprohlížeč) to find
terrain and imagery sheets, and it is the default clip region ("corridor buffer", ADR 0009 decision 2) of
every later context-data import. Both use the one definition below; the code lives in
`domain/corridor.py` (centreline) and `io/gis/envelope.py` (buffer, simplify, reproject, write).

## Geometry

1. **Centreline.** `plan_centreline(alignment, station_from, station_to, max_chord_error_m=0.05)` returns the
   `(E, N)` samples of the alignment in the project CRS, ordered by station. Stations are absolute metres
   (the `frames()` basis); `None` means the alignment's own end; both range ends are sampled exactly; a range
   outside the alignment, or with `from >= to`, raises `ValueError`. A chord between neighbouring samples
   deviates from the true centreline by at most `max_chord_error_m`.
2. **Buffer.** Each centreline is buffered in the project CRS (metric, so `buffer_m` is a true half-width)
   with round joins and the chosen cap: `round` (default, extends `buffer_m` past each end) or `flat` (cut
   perpendicular at each end). The buffers of several alignments are united. A station range is only valid
   with exactly one alignment.
3. **Simplify.** Douglas-Peucker with `simplify_m` (default 0.5 m) and topology preserved, so the result stays
   valid and holes survive. A buffer around a closed loop that is narrower than the loop is an annulus: the
   hole is written, never dropped. A buffer wider than the tightest radius still gives one valid polygon.
4. **Densify, reproject.** Edges longer than `densify_m` (default 50 m) are subdivided, then the vertices are
   transformed through `domain/crs.py`. `area_m2` is measured in the project CRS before reprojection.

The result is a Polygon, or a MultiPolygon where alignments are further apart than twice the buffer.

## CRS, axis order and winding per format

| | GeoJSON (`.geojson`, `.json`) | Shapefile (`.shp`) |
|---|---|---|
| Default CRS | EPSG:4326 (RFC 7946) | the project CRS |
| Axis order | `[longitude, latitude]` (`always_xy`) | easting, northing (x, y) |
| `crs` member / `.prj` | none for 4326; for any other EPSG the legacy `"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::<code>"}}` member, and the tool warns that the file is outside RFC 7946 | ESRI WKT (`WKT1_ESRI`) of the output CRS |
| Decimals | 8 (geographic) or 3 (projected) | 3 in a projected CRS, 8 in a geographic one |
| Exterior ring | counter-clockwise | **clockwise** |
| Hole rings | clockwise | **counter-clockwise** |
| Files | the one `.geojson` | `.shp .shx .dbf .prj .cpg` (`.cpg` holds `UTF-8`) |

Rings are oriented after reprojection and rounding, on the coordinates actually written. `pyshp`'s
`Writer.poly()` stores rings exactly as given; it does not reorient them.

## Attributes

One feature (GeoJSON) or one record (DBF), identical in both formats; the names fit the 10-character DBF limit.

| Field | Type | Meaning |
|---|---|---|
| `name` | text | alignment names joined with `"; "` |
| `buffer_m` | number | half-width in metres |
| `sta_from`, `sta_to` | number or null | absolute stations of the range; null when the whole alignment is used |
| `crs_src` | text | the project CRS, `EPSG:<code>` |
| `created` | text | ISO 8601 UTC timestamp |
| `tool` | text | `coypu-builder <version>` |

## Limits and file safety

- `buffer_m` is at most 5,000 m.
- The output CRS must be a horizontal 2D CRS (projected or geographic); vertical, geocentric, compound,
  engineering and 3D geographic CRSs are refused before anything is written.
- Files are written under temporary names next to the target and renamed on success, so a failed write leaves
  no partial files. An existing output file (for a Shapefile, any of its parts) is not replaced unless
  `overwrite` is set (`--force` on the CLI).
- `alignment.envelope` additionally requires an absolute `path` outside the repository checkout.
- The DBF `name` field is cut to 254 UTF-8 bytes on a character boundary; GeoJSON keeps the full name.

## Entry points

- CLI: `coypu-builder-backend envelope <file.xml> -o <out.geojson|out.shp> [--buffer 250] [--alignment NAME]...
  [--from S] [--to S] [--cap round|flat] [--epsg N] [--crs PROJECT_CRS] [--force]`
- Protocol: `alignment.envelope` (`docs/protocol/ipc.md`).
- Both need the backend's `gis` extra (`uv sync --extra gis`); without it they fail with a message naming that
  command.
