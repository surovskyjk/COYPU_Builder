# ADR 0002 — Typed GDScript for the client

Status: accepted (2026-09-06)

## Decision

The Godot client is written in typed GDScript. Heavy math lives in the backend or in Godot packed-array
operations (`PackedFloat32Array`, `ArrayMesh`, `MultiMesh`). C# is not adopted (extra .NET toolchain, no
benefit for a thin view client). GDExtension C++ is reserved as an escape hatch for mesh/LOD hot paths and is
not built in Phase 1.

## Consequences

- No compile step, tightest editor integration, gdUnit4 for tests, `--headless` in CI.
- Performance-sensitive loops must be written against packed arrays; profile before reaching for GDExtension.
