# ADR 0004 — float64 domain, Project Base Point and tile-local rendering

Status: accepted (2026-09-06)

## Context

National grids place coordinates at 10^5–10^6 m. float32 has ~7 significant digits, so raw values shimmer
in a standard Godot build. Building Godot with `precision=double` was rejected (custom templates, larger
binaries, tooling friction).

## Decision

- The domain stores everything as float64 `(E, N, H)` in the project CRS (`domain/crs.py: ProjectCRS`).
- Every project has a **Project Base Point** `(E0, N0, H0)` (stored, re-choosable). All positions sent to the
  client are `(E−E0, N−N0, H−H0)` mapped to Godot axes `x = E−E0, y = H−H0, z = −(N−N0)` (right-handed,
  −Z north). Frames become a Godot `Basis` with columns (right, up, back) so `-Z` is the tangent.
- Meshes (terrain, track chunks, building chunks) are additionally **tile-local**: vertices relative to the tile
  centre, the tile `Node3D` positioned in world space. Vertex shimmer is therefore impossible regardless of
  corridor length; only node placement carries float32 error (< 1 cm at 10^5 m).
- Rebasing (re-baking against a new base point) is supported for very long corridors (Phase 3).
- The base point maps 1:1 to IFC `IfcMapConversion` (Eastings/Northings/OrthogonalHeight).

## Consequences

`to_local`/`vector_to_godot`/`basis_to_godot` in `domain/crs.py` and `Origin.gd` in the client are the only
places that know this mapping; both are validated against `shared/golden/origin_mapping.json`.
