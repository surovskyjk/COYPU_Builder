# Terrain analysis surface and display tiles

What T-132 builds from the terrain payloads of `terrain-payloads.md`, and what the client receives. The code
is `domain/terrain_surface.py` (analysis surfaces, pure numpy/scipy), `io/mesh/terrain_tiles.py` (the tile
grid and bake) and the `terrain.tiles` / `terrain.tile` handlers. ADR 0009, decision 5, is the rule behind
the split: **every ground query uses the analysis surface; display tiles are derived from it and are never
used for analysis.** Read `coordinate-conventions.md` and ADR 0004 for the axes and the tile-origin rule.

## Analysis surface

`TerrainSurface` is `heights(e, n) -> float64 (same shape as the broadcast inputs)` plus
`bounds() -> (min_e, min_n, max_e, max_n)`. NaN means "no data here"; it is never an error.

| Type | Built from | Interpolation |
|---|---|---|
| `TinSurface` | `TerrainPoints` (triangulated by `tin_from_points`) or `TerrainTin` (triangles as delivered) | linear, barycentric |
| `GridSurface` | `TerrainGrid` | bilinear between cell centres |
| `TerrainStack` | a tuple of surfaces, highest precedence first | the first non-NaN value |

`surface_for_source(payloads)` builds the surface of one source:

- **All point objects of a source are merged before triangulating**, so the seams between sheets are
  continuous: a 2 x 2 sheet split gives the same heights as the unsplit source (tested to 1e-9 m along the
  seams). Classes are not filtered (all DMR 5G points are class 8).
- A TIN keeps its own triangles.
- Grids with the same cell size and a common cell lattice (origins differing by whole cells) become **one**
  `GridSurface`, earlier sheets winning where they overlap, provided the mosaic holds at most 4 times the
  cells of its sheets and 25 million cells. Any other set of grids stays separate and is stacked, never
  resampled.
- A source of one kind returns that surface; a mixed one a `TerrainStack` in order of first appearance.

### The triangulation

`tin_from_points(points, max_edge_m=None, inside=None)`:

1. non-finite rows are dropped and coincident plan positions are merged to their mean height;
2. the points are sorted by `(E, N)`, so the result does not depend on the order the sheets arrived in;
3. Qhull (`scipy.spatial.Delaunay`) triangulates the points **after subtracting their centroid**. At
   Křovák magnitudes (about 1e6) the uncentred coordinates give a different, still valid, triangulation in
   near-degenerate cases;
4. two rules remove the triangles that would bridge ground without data. The Delaunay triangulation fills
   the convex hull, but a corridor clip is concave (S-bends, the area between two arms):
   - **the clip rule (primary).** `inside(e, n) -> bool array` is the source's import clip region, passed
     as a callable so the domain module stays free of GIS types. A triangle whose **centroid** is not inside
     it is dropped. This cuts concave bends exactly, independent of point density. The handler passes
     the clip polygon that `import.run` resolved (corridor envelope or box), boundary-inclusive. A source
     imported without a clip has no clip rule;
   - **the edge rule (secondary).** A triangle with an edge longer than `max_edge_m` is dropped. The default
     is `DEFAULT_MAX_EDGE_M = 30.0` m: an absolute limit for a genuine gap in the data (a river, a building)
     that the clip does not know about. It replaces the first design, 5 times the median nearest-neighbour
     distance, which cut ordinary flat ground (see the table at the end);
5. surviving triangles are made counter-clockwise and points no triangle uses are removed. The clip rule is
   applied first: `TinSurface.dropped_by_clip` counts its triangles and `dropped_triangles` all dropped
   triangles, i.e. the edge rule counts only triangles inside the clip.

The centroid test is not an exact point-in-clip test. A stray triangle across the open end of a fold can keep
a few points within about three spacings of the clip; elsewhere a kept triangle's vertices are inside the
clip, so a point farther than its longest edge from the clip is always NaN. A TIN as delivered
(`TerrainTin`) and grids are used as they are: neither rule applies.

### Point location

`Delaunay.find_simplex` only applies to a Delaunay triangulation, and a source TIN is not one. `TinSurface`
therefore uses its own vectorised locator: triangle bounding boxes are binned on a uniform grid, the entries
sorted by cell id, and a query visits its cell's candidates one rank at a time. The Python loop is over the
maximum number of candidates in a cell, never over query points.

Binning every box into every cell it covers would blow up for the long thin triangles on the hull of a
delivered TIN. A triangle whose box covers more than 16 cells at the base cell size (1.5 times the median
bounding-box extent) is therefore binned into a grid with cells twice as large, and so on, so **every
triangle has at most 16 entries**; the grids are searched finest first. The locator is built on the first
query (about 0.8 s for 1.5 million triangles) and costs 5 to 10 ms per 16 641-point tile. A query exactly on
a shared edge is accepted by either triangle (barycentric slack 1e-9), which agree to rounding.

### Grid

`GridSurface` reads a grid's heights as values at the cell centres and interpolates bilinearly. Inside
`bounds()` but outside the outermost centres (the outer half cell) the nearest centre row or column is used,
so every cell of the grid is covered; outside `bounds()` the answer is NaN. The result is NaN when any of the
four surrounding cells is NaN, including a neighbour whose weight is zero.

### Stack

`TerrainStack.heights` takes the first surface's value where it has one and asks the next only about the
points still missing. T-135 and T-137 query the stack; T-133 will decide the order. Until then the handler
puts the **most recently imported source first**.

## Display tiles

### Grid

- Square tiles aligned in the project CRS. At `level` L the edge is `512 * 2**L` m and the south-west corner
  is `(ix * edge, iy * edge)`. `ix` and `iy` are signed (Křovák coordinates are negative). Levels 0 to 4:
  512 m to 8 km.
- Every tile holds **129 x 129** heights on its own lattice, spacing `edge / 128` (4 m at L0, 64 m at L4).
  Spacing and corners are powers of two times integers, so lattice coordinates are exact in float64 and a
  corner shared by two tiles has the same coordinates in both.
- A height is `TerrainStack.heights()` at the lattice point, with no filtering: coarser levels are point
  samples of the same surface.
- The tiles that exist are those whose square intersects the coverage: for each terrain source its import
  clip polygon intersected with the bounding box of its data, or that bounding box when there was no clip. A tile in the
  coverage with no data still exists (`valid_fraction` 0, origin height 0).

### Encoding

A tile carries:

| Field | Meaning |
|---|---|
| `tile_origin` | the SW corner at the tile's minimum valid height, in Godot axes relative to the base point: `(E - E0, Hmin - H0, -(N - N0))`, float64 |
| `heights` | `<f4` `(129, 129)`; row 0 is the south edge, column 0 the west edge; metres relative to the origin height; NaN = no data |
| `valid_fraction` | share of the 129 x 129 samples that are not NaN |

**Position the tile node at `tile_origin`; the origin is never folded into the values** (ADR 0004). Vertex
`(row r, column c)` is at `x = c * edge / 128`, `z = -r * edge / 128` (north is `-z`) and `y = heights[r, c]`
in the node's frame.

**Quantisation.** Before encoding, every height is rounded to a multiple of 2^-12 m (0.244 mm), at most
0.122 mm from the surface. The origin height is the minimum of the rounded heights, so every stored value is
a non-negative multiple of 2^-12 below 2^24 of them while the tile's relief is under 4096 m. float32 holds
such a value exactly, and `origin_h + value` decodes to the rounded height with no further error. That makes
the heights along an edge shared by two tiles **bit-identical when `origin_h + value` is decoded in
float64**, whatever their origins. A tile with relief above 4096 m still works but loses that exactness.

**On the client this does not carry over.** Godot node positions and vertex data are float32: a vertex ends
up at `H0`-relative `origin_y + value` in float32, so two tiles agree along a shared edge only to about
1e-5 m (float32 resolution at the heights involved), unless the project base height H0 is itself a multiple
of 2^-12 m. T-135 must not assert bit-identity on the client; use a tolerance of about 1e-4 m.

### Client meshing

The client connects the heights into triangles and may derive normals from them; that is connectivity over
backend-owned heights, not geometry of its own (ADR 0007). Holes (NaN) stay open. Skirts against cracks
between LOD levels belong to T-135.

## Protocol (`experimental`)

| Method | Params | Result |
|---|---|---|
| `terrain.tiles` | `level` | `level`, `revision`, `tiles`: per tile `level`, `ix`, `iy`, `edge_m`, `tile_origin`, `valid_fraction`; `warnings` |
| `terrain.tile` | `level`, `ix`, `iy`, `revision` | the same metadata for that tile plus `revision`, and the blob `heights` |

The pattern is F18's: list a level, then fetch the listed tiles one at a time.

- `revision` is an integer from a process-wide counter. It changes whenever the project's terrain sources
  change (an import or re-import) and never repeats across projects.
- `terrain.tile` with a `revision` other than the current one, or for a tile that is not in the current list
  of its level, is `E_NOT_FOUND`: re-list instead of mixing old and new tiles.
- A project with no terrain returns an empty `tiles` list, not an error; fetching any tile is then
  `E_NOT_FOUND`.
- `level` outside 0 to 4 is `E_BAD_PARAMS`, as is a level with more than 5000 tiles (about 140 to 170 bytes
  each as JSON, which keeps the answer under 1 MB): ask for a coarser level.
- A `terrain.tile` response is 66 869 bytes (66 564 of them the blob). A `terrain.tiles` entry costs about
  140 bytes.
- `warnings` names a source that was skipped because it could not be triangulated.
- Listing a level bakes each of its tiles once, because `tile_origin` and `valid_fraction` come from the
  baked heights: 78 L0 tiles take 0.76 s on the Kralupy corridor, and a level near the 5000-tile cap would
  take about a minute. The metadata is then kept for the revision, so later listings are immediate. The bake
  runs in a worker thread (see below).
- A `terrain.tile` request whose `revision` is not the current one is refused at once, without building the
  new stack.

### Off the event loop

Both methods are coroutine handlers (ADR 0003 addendum): building the stack takes 8 to 9 s for Kralupy and
baking a listing can take seconds, while the client treats three missed 2 s pings as a lost connection.
`server/app.py` runs them as tasks, so the connection's read loop never waits for them
(`docs/protocol/ipc.md` describes the connection rules, including `E_BUSY`).

**Ordering.** Neither method changes state, so each waits only for the latest state-changing request sent
before it on the connection (an import, `project.new`, `session.hello`), then takes its snapshot, and no
later request can change what it sees. A `terrain.tiles` pipelined after an `import.run` lists the new
source; one sent before it lists the old state, under the old revision, however long its build takes
(the fetch with that revision is then `E_NOT_FOUND` and the client lists again). A request that is refused
for want of a session or project is refused in arrival order, before the later `session.hello` or
`project.new` takes effect.

- The stack is built by `asyncio.to_thread`. Qhull releases the GIL, so a worker thread suffices: measured on
  800 000 random points a thread running `Delaunay` kept the main thread ticking at 172 ticks per second
  (6.2 s in all), apart from **one stall of 572 ms** at the end, where scipy converts the result with the GIL
  held. Real Kralupy data shows the same: the loop stalls once for about 0.6 s in a 10 s build, and every
  ping is answered, against a budget of 6 s.
- Builds are serialised per project (`ProjectState.terrain_build_lock`): a superseded build cannot be stopped,
  but the next one waits for it, so only one Qhull's memory is live. A request bound to a superseded
  revision still builds it, so it can answer for the state it was ordered after.
- The sources are snapshotted on the event loop before the thread starts, so an import cannot change them
  mid-build. Each point surface's locator is built at the end of the build (`warm_up`), so baking threads do
  not each build one.
- Concurrent requests for one revision share one build (`TerrainCache.build`, awaited behind
  `asyncio.shield`, so a client that disconnects does not cancel it). A build for a superseded revision fills
  only its own, no longer current, `TerrainCache`, so it is never taken for the current one.
- Tile bakes run in worker threads too, one thread hop per tile, so a listing bakes in parallel. Concurrent
  requests for the same `(revision, level, ix, iy)` share one bake (`TerrainCache.baking`).
- A connection runs at most 4 blocking jobs at once (`Session.gate`); a source's clip polygon is prepared
  with `shapely.prepare` on the loop when it is stored, never in a worker.

### Caches

Per project, in `server/session.py` (`TerrainCache`), replaced whenever the terrain sources change:

- the `TerrainStack`, the coverage and the per-level tile lists, built on first use, plus the build and tile
  bakes in flight;
- the metadata of every tile baked so far;
- baked tiles in a bounded LRU keyed by `(revision, level, ix, iy)`, 512 tiles.

`ProjectState.invalidate_terrain()` is called by `import.run`; `ProjectState.terrain_cache()` also compares a
fingerprint of the terrain sources, so a source set any other way still gets a new cache and revision.

## Measured on the Kralupy DMR 5G sheets (not part of CI)

15 sheets, 250 m corridor clip, via T-131: 845 232 points in 14 objects. Surface built with the clip
polygon and the 30 m default.

| | |
|---|---|
| `surface_for_source` | 7.9 to 8.8 s over runs (Qhull about 7 s, sort 0.2 s) |
| first `terrain.tiles` L0 over the wire (build + bake 78 tiles) | 9.8 s, with 86 pings answered meanwhile, slowest 4 ms; a second listing 2 ms |
| first query (locator build) | 0.8 s |
| process working set | 791 MB peak during the triangulation, 179 MB afterwards (255 MB with the locator) |
| triangles | 1 690 364 Delaunay; 1 687 599 kept; 2 765 dropped = 1 011 by the clip rule + 1 754 by the 30 m edge rule |
| points used | all 845 232 |
| tiles L0 / L1 / L2 / L3 / L4 | 78 / 34 / 16 / 9 / 6 |
| bake per tile | L0 10.3 ms mean, 23 ms max (all 78 in 0.76 s); L4 4.0 ms |
| NaN share inside the clip | 2.41 % of 89 300 random points; median 14.7 m (max 72.7 m) from the nearest data point |
| points outside the clip but in the hull | 269 736 sampled; none received a height |

The 2.41 % is the edge rule alone: the 30 m rule without a clip gives the identical figure, and the clip rule
alone leaves 0.20 % NaN (a sliver along the clip boundary). The remaining holes are real: a 150 m stretch
of the line at about km 16.66 to 16.81 has no ground points (nearest point up to 54 m from the centreline).

The first design, `max_edge_m` = 5 times the median nearest-neighbour distance (1.41 m, limit 7.05 m), for
comparison. DMR 5G key points are dense on breaks and sparse on flat ground, and 7.05 m sits at the 90th
percentile of all Delaunay edges. Share of the area inside the clip left NaN, by edge limit alone (separate random samples, so the 30 m row
differs slightly from the figure above):

| limit | triangles dropped | NaN inside the clip |
|---|---|---|
| 7.05 m (the first default) | 8.73 % | 25.45 % |
| 10 m | 1.02 % | 6.14 % |
| 15 m | 0.40 % | 3.64 % |
| 20 m | 0.26 % | 2.93 % |
| 30 m (the default) | 0.16 % | 2.41 % |
| 50 m | 0.11 % | 1.83 % |
| 100 m | 0.07 % | 1.22 % |
