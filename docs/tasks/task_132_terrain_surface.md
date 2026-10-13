# T-132 — Terrain surface and display tiles

**Milestone:** P1.M3 · **Depends on:** T-131, the F18 follow-up (paged-resource pattern) · **Blocks:** T-133,
T-135, T-137

## Context

T-131's readers produce terrain payloads in the project CRS: `TerrainPoints`, `TerrainGrid` and
`TerrainTin`. ADR 0009, decision 5, splits terrain into two representations:

- an **analysis surface** for every ground query — sampling, cut/fill, draping. It is a TIN over the
  original points for point sources, or the native grid for rasters, and is never resampled;
- **display tiles** for the client: regular heightfields with a level-of-detail (LOD) pyramid, derived from
  the analysis surface and never used for analysis.

This task builds both, plus paged protocol methods that follow the F18 pattern: ask for the tile list
first, then fetch tiles one at a time.

## Preconditions

- T-131:
  - `domain/terrain.py` (`TerrainPoints`, `TerrainGrid`, `TerrainTin`);
  - sources stored in `ProjectState.sources`, with one payload per object.
- F18: `alignment.track_mesh` with `metadata_only`; the client pages by index from the backend's list.
- ADR 0004: float64 domain, float32 wire, tile-local vertices, Godot axes `x = E − E0`, `y = H − H0`,
  `z = −(N − N0)`.
- `scipy.spatial.Delaunay` is available (scipy is a core dependency).

Read before starting:
- `docs/adr/0004-precision-and-local-origin.md`;
- `docs/adr/0009-context-data-import.md`;
- `docs/data-contracts/terrain-payloads.md` (T-131);
- `docs/data-contracts/track-mesh.md` (the paging pattern);
- `docs/data-contracts/coordinate-conventions.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/terrain_surface.py` | new — analysis surfaces and the source stack; pure numpy/scipy |
| `backend/src/coypu_builder/io/mesh/terrain_tiles.py` | new — the tile grid and the heightfield bake |
| `backend/src/coypu_builder/protocol/messages.py` | `terrain.tiles` and `terrain.tile`, both `experimental` |
| `backend/src/coypu_builder/server/handlers.py` | handlers; per-project caches for surfaces and tiles |
| `backend/src/coypu_builder/server/session.py` | cache fields, invalidated whenever sources change |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/terrain-tiles.md` | new — the tile grid, LOD levels, heightfield encoding |
| `backend/tests/test_terrain_surface.py` | new |
| `backend/tests/test_terrain_tiles.py` | new |

## Contract

### Analysis surface — `domain/terrain_surface.py`

```python
class TerrainSurface(Protocol):
    def heights(self, e: np.ndarray, n: np.ndarray) -> np.ndarray: ...   # float64; NaN where not covered
    def bounds(self) -> tuple[float, float, float, float]: ...           # min_e, min_n, max_e, max_n

@dataclass(frozen=True)
class TinSurface:                 # from TerrainPoints (triangulated) or TerrainTin (triangles as given)
    points: np.ndarray            # (n, 3) float64
    triangles: np.ndarray         # (m, 3) int32
    def heights(self, e, n) -> np.ndarray: ...     # linear (barycentric) interpolation

@dataclass(frozen=True)
class GridSurface:                # from TerrainGrid
    grid: TerrainGrid
    def heights(self, e, n) -> np.ndarray: ...     # bilinear; NaN if any of the 4 cells is NaN

def tin_from_points(points: np.ndarray, max_edge_m: float | None = None) -> TinSurface: ...
def surface_for_source(payloads: Sequence[TerrainPayload], max_edge_m: float | None = None) -> TerrainSurface: ...

@dataclass(frozen=True)
class TerrainStack:
    surfaces: tuple[TerrainSurface, ...]           # highest precedence first
    def heights(self, e, n) -> np.ndarray: ...     # the first non-NaN value, in order
```

- `surface_for_source`:
  - **merges every point object of a source before triangulating**, so sheet seams stay continuous;
  - takes a TIN's triangles as given;
  - mosaics several grids into one `GridSurface` when they share CRS and cell size, and otherwise stacks
    them.
- **No triangle may bridge a gap.** A Delaunay triangulation fills the convex hull, but the corridor clip is
  concave: S-bends, and the area between two corridor arms. So `tin_from_points` drops every triangle with an
  edge longer than `max_edge_m`. The default is 5 × the median nearest-neighbour distance, computed with
  `scipy.spatial.cKDTree`. Outside the remaining triangles, `heights` returns NaN.
- Point location: use `Delaunay.find_simplex` on the kept triangulation (or an equivalent vectorised
  locator). Queries must be vectorised; 1e6 points must not run a Python loop.
- `TerrainStack` is the single query API for T-135 and T-137. T-133 decides the order and records each
  source's boundary.

### Display tiles — `io/mesh/terrain_tiles.py`

- **Grid:**
  - Square tiles aligned in the project CRS.
  - A tile at `level` L has edge `512 · 2^L` m and its south-west corner at
    `(ix · edge, iy · edge)`.
  - Levels 0 to 4 give 512 m to 8 km tiles.
- **Samples:**
  - Every tile is 129 × 129 heights on its own lattice, with corners shared with the neighbours; spacing is
    `edge / 128`, so 4 m at L0 and 64 m at L4.
  - Each height is `TerrainStack.heights()` at the lattice point.
- **Which tiles exist:** those whose square intersects the union of the sources' coverage, i.e. their clip
  polygons, or their data extent if there was no clip.
- **Encoding:** a tile carries:
  - `tile_origin`: the SW corner at the tile's minimum valid height, mapped to Godot axes, float64;
  - `heights`: float32 `(129, 129)`, row 0 at the south edge, values relative to `tile_origin`'s height;
  - NaN where there is no data.

  **The tile origin goes on the node's position and is never folded into the values** (ADR 0004, as for
  track chunks).
- **Client meshing:** the client turns a heightfield into triangles. This is connectivity over backend-owned
  heights, so the client computes no geometry of its own (ADR 0007). Normals may be derived client-side from
  the same heights. Holes (NaN) are left open; skirts against cracks between LOD levels are T-135's job.

### Protocol (`experimental`)

| Method | Params | Result |
|---|---|---|
| `terrain.tiles` | `level` | metadata only: for each tile `level`, `ix`, `iy`, `edge_m`, `tile_origin` (Godot axes), `valid_fraction`; plus `revision` |
| `terrain.tile` | `level`, `ix`, `iy`, `revision` | the same metadata, plus blob `heights` (`<f4`, `(129, 129)`) |

- `revision` changes whenever sources change.
- A `terrain.tile` request carrying an outdated `revision` returns `E_NOT_FOUND`, so the client re-lists
  instead of mixing old and new tiles.
- A project with no terrain source returns an empty list, not an error.
- A single response is never larger than 1 MB; one L0 tile is about 66 KB.

### Caches

- The `TerrainStack` is built lazily on first use and dropped when sources change.
- Baked tiles are cached by `(revision, level, ix, iy)` with a bounded LRU (default 512 tiles).

## Invariants

- `domain/terrain_surface.py` imports numpy and scipy only.
- float64 everywhere in the domain; float32 appears only in tile blobs, relative to a tile origin.
- Display tiles are never used for a ground query. `TerrainStack` is.

## Acceptance criteria

1. **TIN accuracy (synthetic):**
   - irregular samples at about 3.4 m mean spacing (the DMR 5G density) over T-130's synthetic surface
     reproduce `surface()` at random interior points within the linear-interpolation bound;
   - state the bound you derive from the surface's curvature, then assert it.
2. **No bridging:** with a concave corridor clip, every query point outside the clip polygon but inside the
   points' convex hull returns NaN, and every point inside, farther than 2 × spacing from the polygon's
   edge, returns a value.
3. **Seams:** the 2 × 2 sheet split, imported as one multi-file source, gives heights identical to the
   unsplit source along the sheet boundaries, to within 1e-9 m.
4. **Grid:** `GridSurface` reproduces bilinear interpolation exactly at known points, and NaN propagates
   from any nodata corner.
5. **Stack:** where two surfaces overlap, the first wins; where only the second has data, it is used.
6. **Tiles:**
   - the tile list covers the coverage polygon at each level, and no listed tile lies wholly outside it;
   - heights equal `TerrainStack.heights()` at the lattice points within 1 mm after float32 decoding
     relative to the origin;
   - shared edges between neighbouring tiles are bit-identical;
   - each `terrain.tile` response stays under 1 MB.
7. **Paging and revision:** the client-side pattern works over a live server — list, then fetch each tile.
   An import between list and fetch makes a stale fetch return `E_NOT_FOUND`.
8. **Performance (real data, Verification only):** build the surface from the 15 Kralupy DMR 5G sheets
   (250 m corridor clip, via T-131), then report:
   - point count and triangulation time;
   - peak memory;
   - kept-triangle count and how many were dropped by the edge rule;
   - tile counts per level and the mean bake time per tile.

   The target is under 15 s for the surface and under 50 ms per L0 tile on this machine.

   Also sample the analysis surface **directly under the Kralupy centreline every 100 m** over
   stations 12.7–17.5 km. Report terrain height minus rail height per station, to separate cuttings from
   profile error (ROADMAP F32).
9. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened; `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Client rendering, tile streaming and skirts — T-135.
- Source precedence policy, provider boundaries and the global DEM — T-133.
- Orthophoto texturing — T-134 and T-135.
- Ground elevation on the frame table, and cut/fill — T-137.

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

Run the real-data performance check of criterion 8 from a scratch script outside the repository.

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- the TIN error bound and the measured maximum error;
- every real-data figure from criterion 8;
- the tile counts per level for Kralupy;
- one L0 tile's response size;
- how the edge-length default behaved at the corridor's concave bends.
