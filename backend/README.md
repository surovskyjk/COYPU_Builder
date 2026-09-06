# COYPU Builder backend

Python 3.13 service behind the Godot client: float64 railway domain kernel (alignments, LRS, cant,
kinematics), GIS ingestion, IFC 4.3 mapping and the `.coypub` SQLite document, exposed to the client over a
local WebSocket IPC (see `docs/protocol/`).

```powershell
uv sync                                   # create .venv with Python 3.13 and all dev deps
uv run pytest                             # golden + unit tests (fixtures in tests/fixtures)
uv run ruff check .                       # lint
uv run coypu-builder-backend inspect tests\fixtures\kralupy\kralupy_neratovice_092.xml
uv sync --extra gis --extra ifc           # optional: rasterio/OWSLib and IfcOpenShell
```

Package layout: `coypu_builder.domain` (pure math, no I/O) · `coypu_builder.io` (LandXML, COYPU, GIS, IFC,
project) · `coypu_builder.protocol` / `server` (IPC) · `coypu_builder.cli`.
