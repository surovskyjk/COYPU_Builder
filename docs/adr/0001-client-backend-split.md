# ADR 0001 — Godot 4 client with a decoupled Python backend service

Status: accepted (2026-09-06)

## Context

COYPU Builder needs game-like interaction (ghost snapping, fluid cameras, future OpenXR) and CAD/BIM depth
(IfcOpenShell, GDAL, geotechnics, float64 linear referencing). The sibling apps are PySide6, but Builder is a
greenfield project and must not inherit their monolithic layout.

## Decision

- **Godot 4.7.x (standard float32 build)** is the only client: UI, 2D/3D viewports (`SubViewport`,
  orthographic `Camera3D`, `CanvasLayer`), cameras, tools, XR.
- **Python 3.13 backend service** owns the document, the float64 domain kernel, GIS, IFC and analysis. It runs
  as a sidecar process spawned and supervised by the client (dev mode may attach to an external instance).
- The per-frame path never crosses the process boundary (ADR 0007).

## Consequences

- Two toolchains (Godot + uv/Python), one wire protocol (ADR 0003) that must stay versioned and documented.
- Headless use (CLI, CI, batch) comes for free from the backend.
- A second client (VR-only walkthrough, web) can reuse the backend unchanged.
