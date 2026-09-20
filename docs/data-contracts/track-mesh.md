# Track mesh (T-120)

Two swept rails plus one default ballast prism for an `Alignment`, chunked and tile-local (ADR 0004). This
is the format T-121 consumes to build the client-side track scene; sleepers are not part of it (T-121 places
them itself, as a `MultiMesh` driven by the frame table — ADR 0007 puts per-instance placement in the
client). Read `docs/data-contracts/coordinate-conventions.md` first for the station/frame/Godot-axis
conventions this document builds on.

## Why this exists

`lrs.frames()` already gives a dense, oriented track-plane centre per station (`origin`, `tangent`, `left`,
`up`, `roll`) — the same table `alignment.frame_table` streams for bogie posing. This module turns that into
*surfaces*: a 2-D cross-section swept along the frame table, triangulated, with every vertex stored relative
to its own chunk's tile origin so a standard float32 Godot build never shimmers, regardless of corridor
length (ADR 0004). It never re-derives the track plane — every vertex is `origin + y·left + z·up` for some
`(y, z)` from a `Profile`, exactly `lrs.to_xyz`'s definition.

## Profiles: `io/mesh/profiles.py`

```python
@dataclass(frozen=True)
class Profile:
    name: str
    points: np.ndarray   # (k, 2) float64, (y, z), metres
    closed: bool
```

`y` is the LRS lateral offset (left-positive), `z` is the LRS normal offset along `up`. **`z = 0` is the
track plane itself** — the plane through both rail heads that `frames().origin` sits on. `closed=True` means
the polyline wraps (point `k-1` connects back to point `0`); both rail and ballast profiles are closed rings,
swept into a thin shell (a "tube"), not a solid.

### Rail — **not a UIC60 section**

`rail_profile(gauge_mm, rail_height_m=0.172)` returns two `Profile`s (`rail_left`, `rail_right`): a
10-point head/web/foot silhouette (`_rail_silhouette`), head top at `z=0`, foot bottom at `z=-rail_height_m`.
The silhouette is symmetric about its own local `y=0` before translation, so each rail's head-top-corner
pair (profile points 0, 1) averages to exactly its rail's `y`-centre — the value the acceptance test measures
directly from emitted vertices, not from the construction formula.

Each rail is translated to `y = ±(gauge + RAIL_HEAD_WIDTH_M) / 2` (`RAIL_HEAD_WIDTH_M = 0.072 m`, the
silhouette's own head width), so the two rail heads are `gauge` apart at their inner edges. `gauge` comes
from `CantProfile.gauge_mm` — **not** `superelevation_base_mm` (the rail-head-*centre* distance cant uses
for its torque arm, 1500 mm by convention even at 1435 mm gauge). Using `gauge_mm` directly is what makes the
tram fixture's 1000 mm gauge (and its own, different, `superelevation_base_mm = 1100 mm`) work with no
branch: the rail placement only ever depends on the one number every `Mode` already carries.

The dimensions (head/web/foot half-widths, head/foot depths) are fixed module constants, chosen to *look*
like a rail in a viewport at reasonable zoom — they are not sourced from any rail catalogue and must never be
read as one.

### Ballast

`ballast_profile(gauge_mm, shoulder_m=0.4, depth_m=0.5, side_slope=1.5)` returns one closed 4-point trapezoid:
top half-width `gauge/2 + shoulder_m`, bottom half-width widened by `side_slope` (horizontal run per metre of
depth) over `depth_m`. **The top is pinned to `z = -DEFAULT_RAIL_HEIGHT_M`** (the rail base level), *not*
`z = 0`. This is a deliberate simplification: sleeper thickness is not modelled anywhere in this task (T-121
places sleeper meshes independently, from the frame table, with no coordination back to this baker), so
"sleeper underside" has no independent datum to pin to. Placing the ballast top at the rail base keeps the
two surfaces from overlapping (a literal `z=0` top would put the ballast prism *through* the rail heads) —
it does not claim to model an actual sleeper's thickness or footprint. One shape, no interval variation, no
cess or ditches (out of scope, per the roadmap's fidelity decision).

## Sweeping: `io/mesh/sweep.py`

```python
def sweep_profile(profile: Profile, frames: TrackFrames) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ...  # -> (vertices, normals, uvs), float64, absolute domain (E, N, H)

def tube_indices(n_stations: int, k: int) -> np.ndarray:
    ...  # -> flat (m,) int32 triangle indices
```

`sweep_profile` is row-major with the profile point index varying fastest: row `i*k + j` is `frames` row `i`,
profile point `j`. `vertices[i*k+j] = frames.origin[i] + y_j·frames.left[i] + z_j·frames.up[i]` — this is the
one place vertex positions are computed, and it calls `lrs.frames()` for the geometry, never re-implementing
curvature/cant/gradient math.

**Normals** are the profile's own outward 2-D normal (per-vertex, averaged from its two adjacent edges, sign
picked automatically from the profile's shoelace winding — a `Profile` author never has to pre-orient point
order) rotated into `(left, up)`, with **no tangential component**. This is an accepted simplification: a
true swept-surface normal picks up a small tangential term from curvature/torsion between stations, but at
the station spacing this baker uses (`bake_stations`' chord-error refinement, ≤2 mm by default) the faces are
already near-planar, so the omitted term is negligible for shading and not worth the extra derivative math.

**UVs**: `u` (`uvs[:, 0]`) is accumulated perimeter distance around the profile starting at point 0 —
monotone by construction, independent of `frames` entirely. `v` (`uvs[:, 1]`) is the row's **absolute
station** in metres — the same quantity for every profile point in a ring, and (because chunk boundaries
share their station value exactly, see below) identical across a chunk seam, so a repeating texture never
stretches or resets. `v` is stored float32 on the wire; at Kralupy's ~18000 m stations that resolves to
about 1 mm — fine for a texture coordinate, not a claim of positional precision (the same trade-off
`alignment.frame_table`'s `station` blob documents).

`tube_indices` triangulates each `(ring_i, ring_i+1)` quad as `(a, b, c)` + `(b, d, c)` for
`a=(i,j) b=(i,j+1) c=(i+1,j) d=(i+1,j+1)` (indices mod `k` on the profile axis). This winding is
counter-clockwise as seen from outside for *any* simple (non-self-intersecting) closed profile, derived once
algebraically from the right-handed `(tangent, left, up)` frame and reused for every profile — no per-surface
special-casing.

## Chunking and tile origins: `io/mesh/track.py`

```python
def bake_track_mesh(
    alignment, *, chunk_length_m: float = 250.0, spacing_m: float = 1.0, max_chord_error_m: float = 0.002,
    gauge_mm: float | None = None, pivot: RotationPivot | None = None,
) -> tuple[MeshChunk, ...]: ...
```

`gauge_mm` defaults to `alignment.cant.gauge_mm`; `pivot` defaults to `alignment.cant.pivot` — same defaulting
convention as `lrs.frames`. Stations are `bake_stations(alignment, spacing_m, max_chord_error_m)` (the exact
chord-error-refined set the frame table uses) **unioned with the exact chunk-boundary stations**
(`station_start + i · chunk_length_m`, clamped, last one snapped to `station_end`) — the union guarantees
every cut lands on a station that is actually sampled, not interpolated.

**Chunk boundaries are forced bit-identical.** After splitting the unioned station set at each boundary, the
first and last station of every chunk's slice is overwritten with the boundary value itself (`chunk_stations[0]
= s_lo`, `chunk_stations[-1] = s_hi`), so chunk `i`'s last frame row and chunk `i+1`'s first frame row are
computed from the *exact same float64 station*, not two near-duplicates left over from the union/dedupe step.
Because `lrs.frames()` and `sweep_profile()` are stateless per row (no cross-row coupling, verified by
inspection of `curvature_side`'s neighbour search — it searches the *alignment*, not neighbouring rows of the
same batch), this guarantees the two chunks' shared ring is not just close but **identical** in absolute
domain space, before any float32 cast. `backend/tests/test_track_mesh.py` asserts the boundary stations
compare exactly equal (`==`, not `approx`) for this reason, and separately checks the actual wire-shaped
(float32, tile-local) vertices reconstruct to the same world position within float32's own resolution — the
two checks target different layers of the pipeline on purpose.

**Tile origin**: for each chunk, `tile_origin` is `frames(alignment, [mid_station]).origin`, rounded to the
nearest whole metre (`np.round`), where `mid_station = (station_start + station_end) / 2`. Every vertex in
that chunk is `sweep_profile`'s absolute-domain vertex minus `tile_origin`, mapped to Godot axes
(`domain.crs.vector_to_godot`) and cast to float32. A 250 m chunk therefore has vertex magnitudes under
~150 m, where float32 resolves to a fraction of a millimetre — comfortably inside ADR 0004's budget.

```python
@dataclass(frozen=True)
class MeshChunk:
    chunk_index: int
    station_start: float
    station_end: float
    tile_origin: np.ndarray   # (3,) float64 (E, N, H), project CRS — NOT Godot axes
    vertices: np.ndarray      # (n, 3) float32, Godot axes, relative to tile_origin
    normals: np.ndarray       # (n, 3) float32, Godot axes, unit
    uvs: np.ndarray           # (n, 2) float32
    indices: np.ndarray       # (m,) int32, CCW from outside
    surface: str              # "rail_left" | "rail_right" | "ballast"
```

One `MeshChunk` per `(chunk_index, surface)` pair — three per chunk. `MeshChunk.tile_origin` stays in the
project CRS (domain axes); the wire result maps it to Godot axes, relative to the project base point, exactly
like every other position that crosses the boundary (`domain.crs.points_to_godot`).

## Wire method: `alignment.track_mesh`

```python
class AlignmentTrackMeshParams(msgspec.Struct, frozen=True):
    alignment_id: str
    chunk_length_m: float = 250.0
    spacing_m: float = 1.0
    chunk_index: int | None = None     # None = all chunks

class TrackMeshChunkInfo(msgspec.Struct, frozen=True):
    chunk_index: int
    surface: str
    station_start: float
    station_end: float
    tile_origin: tuple[float, float, float]   # Godot axes, relative to the project base point
    vertex_count: int
    index_count: int

class AlignmentTrackMeshResult(msgspec.Struct, frozen=True):
    alignment_id: str
    chunks: tuple[TrackMeshChunkInfo, ...]
```

`chunks` carries one `TrackMeshChunkInfo` per `(chunk_index, surface)` pair actually returned. `gauge_mm`,
`pivot` and `max_chord_error_m` are not exposed on the wire — the handler always uses the alignment's own
defaults; only the client-facing knobs (`chunk_length_m`, `spacing_m`, and `chunk_index` for paging) are
params.

### Blob naming

Four blobs per `(chunk_index, surface)` pair, named `vertices_<i>_<surface>`, `normals_<i>_<surface>`,
`uvs_<i>_<surface>`, `indices_<i>_<surface>` — e.g. `vertices_3_ballast`, `indices_0_rail_left`. `<i>` is
that entry's `chunk_index` (an integer, not zero-padded); `<surface>` is one of `rail_left` | `rail_right` |
`ballast`. This is a **dynamic** blob set: the number of blobs is `4 × len(chunks)`, not fixed by the method
— T-121 must build the blob names itself from the `chunks` list in the result, not assume a static set the
way `alignment.frame_table`'s callers can.

`chunk_index` in `AlignmentTrackMeshParams` filters the response to one chunk's three surfaces (still
`4 × 3 = 12` blobs, not the whole corridor) — this is how a client pages a long alignment without a separate
streaming protocol, per the roadmap's scope decision. It reduces response *size*; `bake_track_mesh` still
computes the full corridor internally (baking is not cached across calls — out of scope for this task).

## What T-121 needs to know

- Build one `MeshInstance3D` (or `ArrayMesh` surface) per `(chunk_index, surface)`, positioned at
  `tile_origin` (already Godot axes, base-point-relative — set it directly as the node's `position`), with
  vertices/normals/uvs/indices from the matching blob quadruplet.
- Do not attempt to weld chunk-boundary rings client-side — they already coincide exactly in domain space
  (see "Chunk boundaries are forced bit-identical" above); any residual gap after Godot's own float32 node
  placement is the sub-millimetre error ADR 0004 already accepts, not a bug to work around.
- Sleepers are not here. Place them separately from the frame table (`alignment.frame_table`), the same table
  this baker's `frames()` calls draw from, so sleeper spacing and rail geometry agree by construction.
- No materials, colour or LOD are emitted — geometry and UVs only (out of scope for T-120).
- A multi-chunk response is far larger than any other method's — the `websockets` client default frame
  limit is 1 MiB, which an all-chunks-at-once request for anything but a short alignment will exceed. Either
  raise the client's max message size for this method, or page with `chunk_index` as intended; the wire
  protocol does not invent a streaming alternative (see "Blob naming" above).
