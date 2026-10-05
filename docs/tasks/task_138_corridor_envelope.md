# T-138 — Corridor envelope export: GeoJSON and Shapefile

**Milestone:** P1.M3 · **Depends on:** nothing beyond the existing LandXML import · **Blocks:** T-131 (its
corridor clip reuses this geometry), T-140 (the *Export corridor envelope* action)

## Context

To download terrain and imagery for a corridor, the user selects an area of interest in a data portal, for
example the ČÚZK Geoprohlížeč, which can load a polygon from a file. This task produces that polygon: a buffer
of chosen half-width around the plan centreline of the imported alignments, written as GeoJSON or as an ESRI
Shapefile. The Kralupy corridor alone needs 12–16 DMR 5G sheets, so picking them by hand is the chore this
removes.

The same polygon is the *corridor buffer* that ADR 0009 names as the default clip region for every later
import (T-131 onwards), so its definition lives in one place from the start.

## Preconditions

- `domain/lrs.py: frames()`, `domain/sampling.py: bake_stations(alignment, spacing_m, max_chord_error_m)`,
  `domain/crs.py: ProjectCRS`, and `Alignment.station_start` / `station_end` (absolute stations).
- The LandXML reader and the `inspect` subcommand (`__main__.py` + `cli/inspect.py`), which is the pattern
  for a new subcommand.
- `io/gis/` is an empty package. The `gis` extra in `backend/pyproject.toml` declares `shapely`, but neither
  the local venv nor CI installs the extra today (`uv sync` without `--extra gis`).
- Synthetic fixtures: `tests/fixtures/synthetic/tram_loop.py` (a full circular loop, `LOOP_RADIUS_M = 30`).

Read before starting: `docs/adr/0009-context-data-import.md` (decisions 2 and 8),
`docs/data-contracts/coordinate-conventions.md`, `docs/protocol/ipc.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/corridor.py` | new — `plan_centreline()`, pure numpy |
| `backend/src/coypu_builder/domain/crs.py` | add a public transform helper if none fits (the existing `_transformer` is private) |
| `backend/src/coypu_builder/io/gis/envelope.py` | new — buffer, simplify, reproject, write |
| `backend/src/coypu_builder/cli/envelope.py` | new — the `envelope` subcommand |
| `backend/src/coypu_builder/__main__.py` | register the subcommand |
| `backend/src/coypu_builder/protocol/messages.py` | `AlignmentEnvelopeParams`, `AlignmentEnvelopeResult`, registry entry |
| `backend/src/coypu_builder/server/handlers.py` | `alignment.envelope` handler |
| `docs/protocol/ipc.md` | regenerate with `tools/gen_protocol_docs.py` |
| `backend/pyproject.toml`, `backend/uv.lock` | add `pyshp` to the `gis` extra |
| `.github/workflows/ci.yml` | install the extra: `uv sync --extra gis` wherever the jobs run `uv sync` |
| `CLAUDE.md` | `uv sync --extra gis` in Commands, plus one `envelope` example line |
| `.gitignore` | ignore exported Shapefile parts: `*.shp`, `*.shx`, `*.dbf`, `*.cpg`, `*.prj` |
| `docs/data-contracts/corridor-envelope.md` | new — geometry definition, CRS rules, file layout, attributes |
| `backend/tests/test_corridor_envelope.py` | new |

## Contract

### Domain — plan centreline

```python
# domain/corridor.py — numpy only
def plan_centreline(
    alignment: Alignment,
    station_from: float | None = None,   # absolute stations; None = the alignment's own end
    station_to: float | None = None,
    max_chord_error_m: float = 0.05,
) -> np.ndarray: ...                     # (n, 2) float64 (E, N) in the project CRS, ordered by station
```

Samples come from `bake_stations(..., max_chord_error_m=...)` restricted to the range, with both range ends
included exactly. A range outside `[station_start, station_end]` or with `from >= to` raises `ValueError`.

### I/O — envelope geometry and files

```python
# io/gis/envelope.py — shapely and pyshp are imported inside functions, never at module level
@dataclass(frozen=True)
class EnvelopeOptions:
    buffer_m: float                      # half-width, > 0
    station_from: float | None = None    # only valid with exactly one alignment
    station_to: float | None = None
    cap: Literal["round", "flat"] = "round"
    simplify_m: float = 0.5              # max deviation simplification may add
    densify_m: float = 50.0              # max segment length before reprojection

@dataclass(frozen=True)
class EnvelopeResult:
    files: tuple[Path, ...]              # every file written; a Shapefile is .shp .shx .dbf .prj .cpg
    epsg: int                            # output CRS
    area_m2: float                       # measured in the project CRS, before reprojection
    vertex_count: int
    bounds: tuple[float, float, float, float]   # min_x, min_y, max_x, max_y in the output CRS

def corridor_envelope(alignments: Sequence[Alignment], options: EnvelopeOptions): ...
    # -> shapely Polygon or MultiPolygon in the project CRS: the union of each alignment's buffer
def write_envelope(geometry, path: Path, project_crs: ProjectCRS, epsg: int | None,
                   attributes: Mapping[str, str | float | None]) -> EnvelopeResult: ...
    # format from the suffix: .geojson / .json -> GeoJSON, .shp -> Shapefile
```

Geometry: buffer the plan centreline with round joins, the chosen cap, then simplify with topology
preserved. A buffer wider than the tightest radius must still yield one valid polygon. A buffer around a
closed loop narrower than the loop is an annulus with a hole, and holes are written, not dropped.

**CRS and axis order — get these right per format:**

- GeoJSON defaults to EPSG:4326 as RFC 7946 requires: coordinates are `[longitude, latitude]` (use
  `always_xy=True`), no `crs` member, 8 decimal places. If another EPSG is requested, write the legacy
  `"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::<code>"}}` member and print a
  warning that the file is outside RFC 7946.
- Shapefile defaults to the project CRS; `.prj` holds ESRI WKT (`CRS.to_wkt(WKTVersion.WKT1_ESRI)`), `.cpg`
  holds `UTF-8`, and coordinates have 3 decimal places in a projected CRS.
- **The two formats wind rings in opposite directions.** GeoJSON: exterior counter-clockwise, holes
  clockwise. Shapefile: exterior clockwise, holes counter-clockwise. Verify on the written file, not on the
  shapely object.
- Densify to `densify_m` before reprojecting, so long straight edges follow the target projection.

Attributes, identical in both formats (field names fit the 10-character DBF limit): `name` (alignment names
joined with `"; "`), `buffer_m`, `sta_from`, `sta_to` (empty when the whole alignment), `crs_src` (project
EPSG), `created` (ISO 8601 UTC), `tool` (`coypu-builder <version>`).

### Protocol

```python
class AlignmentEnvelopeParams(msgspec.Struct, frozen=True):
    path: str                                  # output file; the suffix decides the format
    buffer_m: float = 250.0
    alignment_ids: list[str] | None = None     # None = every alignment in the project
    station_from: float | None = None
    station_to: float | None = None
    epsg: int | None = None                    # None = 4326 for GeoJSON, the project CRS for Shapefile
    cap: str = "round"

class AlignmentEnvelopeResult(msgspec.Struct, frozen=True):
    files: list[str]
    epsg: int
    area_m2: float
    vertex_count: int
    bounds: list[float]
```

Method `alignment.envelope`. Invalid parameters return `E_BAD_PARAMS`. A missing `gis` extra returns an error
whose message names `uv sync --extra gis`; it must never crash the server.

### CLI

```text
coypu-builder-backend envelope <file.xml> -o <out.geojson|out.shp> [--buffer 250] [--alignment NAME]...
                              [--from S] [--to S] [--cap round|flat] [--epsg N] [--crs PROJECT_CRS]
```

It prints the alignments used, the station range, the area in km², the vertex count, the output CRS and every
file written. It exits non-zero with a one-line message on bad input or a missing extra.

## Invariants

- `domain/corridor.py` imports numpy and domain modules only — no shapely, no I/O.
- Stations are absolute metres, the same basis as `frames()`.
- Reprojection goes through `domain/crs.py`, not through ad-hoc `pyproj` calls scattered in `io/`.
- Nothing is written inside the repository except by tests into `tmp_path`.

## Acceptance criteria

1. **Straight line, flat caps:** the polygon is the `2b × L` rectangle; area within 1e-6 relative.
2. **Circular arc with `R > b`, flat caps:** area equals `2·b·L` within 0.1 %, because a parallel strip's
   area is exact while `b < R`. Every centreline sample lies inside, at least `b − simplify_m − 0.05` m from
   the boundary.
3. **Tram loop** (`R = 30 m`): with `b = 10 m` the result is valid with exactly one hole, the loop's inside;
   with `b = 50 m` it is valid with no hole; in both cases `area < 2·b·L` plus the cap area.
4. **Kralupy, `b = 250 m`, round caps:** valid; area within 0.5 % of `2·b·L + π·b²` (`b` is below the
   minimum radius of about 300 m, so this is exact up to discretisation); vertex count reported and below
   2,000.
5. **Station sub-range** 12,720–17,545 m on Kralupy (the stretch DMR 5G sheet KRAV82 covers), flat caps: the
   centreline at 12,730 m and 17,535 m lies inside; at 12,700 m and 17,565 m it lies outside.
6. **GeoJSON:** a FeatureCollection with one Feature; longitude within [12, 19] and latitude within
   [48.5, 51.1] for Kralupy; closed rings; RFC 7946 winding checked on the written file; transforming the
   vertices back to EPSG:5514 lands within 0.01 m of the project-CRS geometry.
7. **Shapefile:** read back with `pyshp`: one polygon record with the contract's fields; ESRI winding checked
   on the written file, holes included (criterion 3's case); `.prj` parses with `pyproj` to the requested
   CRS; `.cpg` reads `UTF-8`.
8. `plan_centreline` deviates from `frames()` at mid-sample stations by at most `max_chord_error_m`.
9. `alignment.envelope` works over a live server and returns the contract's shape;
   `gen_protocol_docs.py --check` exits 0.
10. With the `shapely` import forced to fail, the CLI exits non-zero naming `uv sync --extra gis`, and the
    handler returns an error response instead of crashing.
11. CI installs the extra. Both suites are green on Windows and on Linux CI, with no previously passing test
    removed, skipped or weakened.

## Out of scope

- The client's *Export corridor envelope* action and file dialog — T-140.
- Clipping imported terrain to the envelope — T-131 calls `corridor_envelope`.
- KML, GML and DXF output — the deferred formats in the roadmap.
- Listing and downloading the ČÚZK map sheets the envelope touches — T-139, which reads the sheet outlines
  from ČÚZK's download-service feeds.

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
uv run coypu-builder-backend envelope tests/fixtures/kralupy/kralupy_neratovice_092.xml --buffer 250 -o D:/COYPU_Builder/Data/Envelopes/kralupy_250m.geojson
uv run coypu-builder-backend envelope tests/fixtures/kralupy/kralupy_neratovice_092.xml --buffer 250 -o D:/COYPU_Builder/Data/Envelopes/kralupy_250m.shp
```

The last two commands write outside the repository on purpose; create `D:\COYPU_Builder\Data\Envelopes\` if it
does not exist.

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State: the Kralupy area and vertex count at 250 m with round caps, and both output paths; how ring winding
was verified on each written file; the `pyshp` version and whether it reorients rings by itself; and the
result of criterion 3's two buffer widths.
