# COYPU Builder

Visual modeler, parametric railway authoring tool and open-BIM platform — the third application of the
open-source **COYPU** railway engineering suite:

1. **[COYPU Feeder](https://github.com/surovskyjk/COYPU-Feeder)** — OpenStreetMap → LandXML 1.2 alignments
2. **[COYPU](https://github.com/surovskyjk/COYPU)** — cant design, permissible speed profiles, train run simulation (CSV kinematics)
3. **COYPU Builder** (this repository) — 3D modeling, kinematic playback, parametric track bed, GIS context, IFC 4.3

Builder bridges the fluid interaction of sandbox simulation games with the precision of infrastructure CAD/BIM.

## Architecture in one paragraph

A **Godot 4.7** client owns the UI, the 2D/3D viewports, cameras, snapping and (later) OpenXR. A **Python 3.13
backend service** owns the document (`.coypub`, SQLite), the float64 domain kernel (linear referencing,
clothoids, cant, kinematics), GIS (GDAL/rasterio, WMS/WMTS) and IFC 4.3 (IfcOpenShell). They talk over a
localhost WebSocket carrying JSON headers plus binary blobs. Everything the client renders is expressed relative
to a **Project Base Point** and per-tile local origins, so a standard float32 Godot build stays jitter-free on
national grids (S-JTSK, UTM, …). Per-frame work (vehicle poses, cameras) is evaluated in Godot from
backend-baked tables — the backend never sits in the frame loop. Decisions are recorded in `docs/adr/`.

## Repository layout

```
backend/   Python service (uv project)          client/   Godot 4 project
shared/    golden vectors + catalogues           docs/     ADRs, protocol, data contracts
tools/     install_godot.ps1, run_dev.ps1, make_golden.py
```

## Getting started (Windows)

```powershell
winget install --id astral-sh.uv -e          # once
cd backend; uv sync; uv run pytest           # backend + golden tests
..\tools\install_godot.ps1                   # downloads Godot 4.7.2 into tools\godot
..\tools\run_dev.ps1                         # backend + client (once the IPC server lands)
```

## Status

Phase 0 (bootstrap): domain kernel, multi-dialect LandXML reader, `.coypu` reader and golden tests against the
Kralupy–Neratovice fixture are in place. Phase 1 (playback visualizer + spatial context) is specified and
under way — see [`ROADMAP.md`](ROADMAP.md) for milestones and [`docs/adr/`](docs/adr/) for the decisions
behind them.

## License

[MIT](LICENSE) — © 2026 Jakub Surovský and contributors.
