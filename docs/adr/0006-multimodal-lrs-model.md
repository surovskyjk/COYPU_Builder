# ADR 0006 — One multi-modal LRS model

Status: accepted (2026-09-06)

## Decision

- Core entities: `Network(mode)`, `Node`, `Link(alignment)`, `Junction(kind)`, `Alignment(H, V, cant)`,
  `Corridor(alignment, cross-section intervals)`, `Asset(catalogue item, LRS anchor)`, `Trainset`,
  `KinematicsRun`, `Layer`, `WbsNode`.
- Placement is always an LRS anchor `(alignment, s, y, z, heading offset)`; `domain/lrs.py` is mode-agnostic.
- `Mode` (heavy_rail, light_rail_tram, trolleybus, road_service) tags semantics only: cant/superelevation
  interpretation, gauge, catalogue templates, IFC spatial classes (`IfcRailway` vs `IfcRoad`).
- Topology is a graph; junction nodes carry turnout / crossing / intersection semantics and generated geometry.

## Consequences

Fixtures must include at least one non-heavy-rail network (tram loop with a junction) before Phase 2 authoring
ships; no rail-only assumption may live in `domain/lrs.py` or `domain/model`.
