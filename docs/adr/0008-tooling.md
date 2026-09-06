# ADR 0008 — Tooling

Status: accepted (2026-09-06)

## Decision

- Backend: `uv` project (`pyproject.toml`, `uv.lock`), Python 3.13 (not the system 3.14 — wheel coverage for
  IfcOpenShell, rasterio, GDAL), pytest, ruff, msgspec. Optional extras `gis`, `ifc`. PyInstaller spec for
  releases (frozen `coypu-builder-backend.exe` shipped next to the Godot export).
- Client: Godot 4.7.x stable, standard build, installed by `tools/install_godot.ps1` into the git-ignored
  `tools/godot/`; gdUnit4 for tests; `--headless` in CI.
- Golden data and catalogues under `shared/` are consumed by both sides.
- CI: GitHub Actions — backend (uv sync, ruff, pytest) and client (Godot headless tests) jobs; protocol docs
  freshness check.
