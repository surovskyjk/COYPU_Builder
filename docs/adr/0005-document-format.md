# ADR 0005 — `.coypub` SQLite document with an IFC 4.3 mapping layer

Status: accepted (2026-09-06)

## Decision

- The native project is one SQLite file (`.coypub`, WAL mode): `project` (metadata, CRS, base point,
  schema_version), `entity` (GUID-keyed, kind, parent, WBS, mode, JSON props), `blob` (typed numpy buffers),
  `alignment_geometry`, `lrs_anchor`, `layer`, `run`, `wbs`, `ifc_map`, `history` (undo journal).
- Plain `sqlite3` with a repository layer; migrations are explicit scripts keyed by `schema_version`.
- IFC 4.3 is a first-class import/export **mapping** (every entity declares its IFC class and property sets;
  GUIDs map to IFC GlobalIds via `ifcopenshell.guid.compress`), not the in-memory model.

## Alternatives rejected

- Native IFC (Bonsai-style): lossless by construction but slow for interactive edits and cannot hold GIS
  layers, playback runs or catalogue state.
- ZIP + JSON like COYPU's `.coypu`: whole-file rewrites; the Kralupy sample is already 9 MB of JSON.
