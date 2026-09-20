# T-120 — Backend track mesh baker: rails, ballast prism, tile-local chunking

**Milestone:** P1.M2 · **Depends on:** T-110, T-114 · **Blocks:** T-121

## Context

Everything so far is numbers. This is the task that turns the Kralupy corridor into geometry a renderer can
draw, and it is the first real test of ADR 0004's tile-local promise: vertices relative to a tile centre, the
tile node placed in world space, so a standard float32 Godot build cannot shimmer regardless of corridor
length.

ADR 0001 puts mesh generation on the backend — the client never computes geometry the backend owns. Sleepers
are the deliberate exception and stay client-side (T-121) because placing instances by interpolating the
frame table is evaluation, not geometry construction, and ADR 0007 already puts that in the client.

Roadmap scope decision 3 fixes the fidelity: **two swept rails plus one default ballast prism**. No
parametric cross-sections, no cess, no ditches, no switch geometry. `domain/sections/` stays empty until
Phase 2 authoring defines what it needs.

## Preconditions

- `domain/lrs.py: frames()` gives `origin`, `tangent`, `left`, `up`, `roll` per station, with the track-plane
  centre already accounting for `RotationPivot.LOW_RAIL`.
- `domain/sampling.py: bake_stations()` produces the chord-error-refined station set.
- `domain/crs.py` provides `points_to_godot`, `BasePoint`.
- `domain/model/vehicle.py: VehicleSpec.gauge_mm`; `CantProfile.gauge_mm` carries the track gauge.
- `io/mesh/` is an empty package. `shared/catalogue/cross_sections/` is an empty placeholder — leave it.
- T-114's `METHODS` registry is the only place a new wire method may be declared.

Read before starting: `docs/adr/0004-precision-and-local-origin.md`, `docs/adr/0001-client-backend-split.md`,
`docs/data-contracts/coordinate-conventions.md`, `domain/lrs.py`, `domain/sampling.py`,
`server/handlers.py: handle_alignment_frame_table` (the blob-returning pattern).

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/mesh/__init__.py` | re-export |
| `backend/src/coypu_builder/io/mesh/profiles.py` | new — rail and ballast cross-section profiles |
| `backend/src/coypu_builder/io/mesh/sweep.py` | new — profile × frames → indexed triangle mesh |
| `backend/src/coypu_builder/io/mesh/track.py` | new — `bake_track_mesh`, chunking, tile origins |
| `backend/src/coypu_builder/protocol/messages.py` | `alignment.track_mesh` params/result + `METHODS` entry |
| `backend/src/coypu_builder/server/handlers.py` | the handler |
| `docs/protocol/ipc.md` | regenerate |
| `backend/tests/test_track_mesh.py` | new |
| `docs/data-contracts/track-mesh.md` | new — the format T-121 consumes |

## Contract

### Profiles

A profile is a 2-D polyline in the track-plane cross-section, `(y, z)` in metres, `y` left-positive and `z`
along the plane normal, exactly the LRS offsets of `domain/lrs.py`.

```python
@dataclass(frozen=True)
class Profile:
    name: str
    points: np.ndarray        # (k, 2) float64, (y, z), ordered so the outward normal is consistent
    closed: bool              # closed rings (rail, ballast) vs open strips

def rail_profile(gauge_mm: float, rail_height_m: float = 0.172) -> tuple[Profile, Profile]: ...
def ballast_profile(gauge_mm: float, shoulder_m: float = 0.4, depth_m: float = 0.5,
                    side_slope: float = 1.5) -> Profile: ...
```

The rail is a **simplified silhouette** — head, web, foot — of roughly 8 to 12 points. It is not a UIC 60
section and must not claim to be; say so in `track-mesh.md`. The two rails sit at `y = ±gauge/2` plus half a
rail-head width, derived from the gauge so the tram fixture's 1000 mm works unchanged.

The ballast is a trapezoidal prism below the track plane: top at the sleeper underside, widening downward by
`side_slope`. One default shape, no interval variation.

### Sweeping

```python
@dataclass(frozen=True)
class MeshChunk:
    chunk_index: int
    station_start: float
    station_end: float
    tile_origin: np.ndarray   # (3,) (E, N, H) — the chunk's local origin in the project CRS
    vertices: np.ndarray      # (n, 3) float32, RELATIVE TO tile_origin, in Godot axes
    normals: np.ndarray       # (n, 3) float32
    uvs: np.ndarray           # (n, 2) float32
    indices: np.ndarray       # (m,) int32, triangles, counter-clockwise when seen from outside
    surface: str              # "rail_left" | "rail_right" | "ballast"

def sweep_profile(profile: Profile, frames: TrackFrames) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...
def bake_track_mesh(alignment, *, chunk_length_m: float = 250.0, spacing_m: float = 1.0,
                    max_chord_error_m: float = 0.002, gauge_mm: float | None = None,
                    pivot: RotationPivot | None = None) -> tuple[MeshChunk, ...]: ...
```

**Tile-locality is the point of this task.** Each chunk's `tile_origin` is the domain-space position of its
mid-station (rounded to a whole metre for stability), and every vertex is stored relative to it, mapped to
Godot axes. A chunk 250 m long therefore has vertex magnitudes under ~150 m, where float32 resolves to well
under a millimetre — the requirement ADR 0004 exists to satisfy. **Never emit a vertex in absolute project
coordinates.**

Chunks are cut at station boundaries and **share their boundary ring**: chunk `i`'s last cross-section and
chunk `i+1`'s first are the same stations, so neighbouring chunks meet with no visible seam. Chunk length is
a parameter because the right value is a vertex-budget question T-121 will answer; 250 m is a starting point,
not a finding.

Vertex positions come from `frames()` — the same call the frame table uses — so the rails sit exactly where
a bogie posed by T-112's chain will sit. Do not re-derive the track plane.

UVs: `u` across the profile by accumulated perimeter distance, `v` along the alignment by **absolute
station in metres**, so a repeating texture never stretches and never resets at a chunk boundary.

### Wire method

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

Blobs, in emission order, **concatenated across all chunks in `chunks` order** — one set per chunk, named
`vertices_<i>_<surface>`, `normals_<i>_<surface>`, `uvs_<i>_<surface>`, `indices_<i>_<surface>`. State the
naming scheme explicitly in the `BlobSpec` descriptions; T-121 depends on it.

Measure the whole-corridor payload and report it. If all-chunks-at-once for Kralupy exceeds roughly 32 MB,
say so rather than inventing a streaming protocol — `chunk_index` already gives the client a way to page.

## Invariants

- **ADR 0004:** vertices are tile-local. A test must assert the maximum vertex magnitude per chunk is bounded
  by roughly `chunk_length` — that assertion is the whole safeguard.
- **ADR 0001:** the backend owns this geometry. Do not emit anything that asks the client to construct
  surfaces.
- **ADR 0006:** mode-agnostic. The bake must work on the tram fixture's 1000 mm-gauge canted alignment with
  no branch on `Mode`. Test it.
- **`domain/` stays pure** — this is `io/mesh/`, which may not import msgspec either.
- `lrs.frames` stays the single place cant roll is applied.

## Acceptance criteria

1. Kralupy bakes into chunks with no gaps: for every adjacent pair, chunk `i`'s last ring and chunk `i+1`'s
   first ring agree in world space to 1e-9 m after adding their respective tile origins.
2. Max vertex magnitude within any chunk is under `chunk_length_m`, asserted.
3. Rail head centres sit at `±(gauge + rail_head_width)/2` from the track-plane centre, measured from the
   emitted vertices, at a straight, in a curve with cant, and on the tram fixture's 1000 mm gauge.
4. Every triangle is wound consistently; no degenerate triangles (zero area within 1e-12).
5. Vertex and index counts match the declared `TrackMeshChunkInfo` for every chunk and every surface.
6. `u` is monotone across the profile and `v` equals absolute station, verified at a chunk boundary.
7. The tram fixture bakes without error, including through its junction node's alignment.
8. `METHODS` and `DISPATCH` still agree; `gen_protocol_docs.py --check` exits 0 with the docs committed.
9. Wall time and total payload for the full Kralupy corridor are measured and reported.

## Out of scope

- Sleepers — T-121 places them as a client `MultiMesh` from the frame table.
- Materials, textures, colour. This task emits geometry and UVs only.
- LOD, decimation, or multiple detail levels — T-121 decides what it needs.
- Turnout, crossing or any junction geometry. `Junction` carries semantics only until Phase 2.
- Cess, ditches, drainage, catenary, signals, platforms.
- Caching bakes to disk.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

## Report back

State: the chunk length and spacing you settled on and why; measured max vertex magnitude per chunk; total
vertex count, payload size and bake wall time for Kralupy; the rail profile's point count and what it is a
silhouette of; and whether the tram fixture needed anything the heavy-rail path did not.
