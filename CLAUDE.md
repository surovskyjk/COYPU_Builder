# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Commands

```powershell
# backend (run from backend/)
uv sync                                    # Python 3.13 venv + dev deps (uv is a winget package; if the
                                           # current shell lacks it: %LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe)
uv run pytest                              # all tests; golden fixtures live in tests/fixtures
uv run pytest tests/test_lrs_golden.py -k dense
uv run ruff check . ; uv run ruff format .
uv run coypu-builder-backend inspect tests/fixtures/kralupy/kralupy_neratovice_092.xml
uv run python ../tools/make_golden.py      # regenerate shared/golden/*.json (both test suites consume them)

# client (Godot 4.7.x, standard build)
..\tools\install_godot.ps1                 # one-time download into tools/godot (git-ignored)
tools\godot\Godot_v4.7.2-stable_win64.exe --path client --editor
tools\godot\Godot_v4.7.2-stable_win64.exe --path client --headless -s addons/gdUnit4/bin/GdUnitCmdTool.gd  # tests (once addon is vendored)
tools\run_dev.ps1                          # backend + client together
```

- System Python is 3.14; the backend is pinned to 3.13 (wheel coverage for IfcOpenShell/rasterio). Always go through `uv run`.
- The Windows console is cp1250 — use `python -X utf8` for scripts printing non-ASCII.

## Architecture invariants (see docs/adr)

- **Godot client = view + interaction only.** It never computes geometry the backend owns; it interpolates baked tables (`alignment.frame_table`, `run.get`) per frame and sends commands. No Qt, no other UI toolkit.
- **Backend `domain/` is pure**: numpy/scipy/pyproj/networkx only, no I/O, no protocol types. `io/` converts formats ⇄ domain; `server/` is the only layer that knows the wire format. Heavy optional deps (rasterio, OWSLib, ifcopenshell) are imported lazily inside their `io/` modules.
- **float64 domain, float32 wire.** Every position sent to the client is relative to the Project Base Point (`domain/crs.py: to_local`) and mapped to Godot axes `x = E−E0, y = H−H0, z = −(N−N0)`; meshes are additionally tile-local.
- **Stations are absolute metres** everywhere in the domain (LandXML `staStart` basis). COYPU's km appear only inside `io/coypu`.
- **LandXML point tokens** follow `io/landxml/dialects.py`: Křovák CRS → positive tokens are (X south, Y west) ⇒ `E=−t2, N=−t1`; otherwise spec order (N, E) unless overridden. Never special-case a fixture.
- **Cant**: |mm|, `roll = −sign(κ)·asin(D/base)`, base 1500 mm default; `RotationPivot.LOW_RAIL` moves the track-plane centre, not the profile. Keep `lrs.frames` the single place this is applied.
- **Multi-modal**: `Mode` tags semantics; the LRS `(s, y, z)` model is shared by heavy rail, trams, trolleybus and service roads. Do not add rail-only assumptions to `domain/lrs.py` or `domain/model`.
- Prove geometry changes with the golden tests (`tests/test_lrs_golden.py`) — stations bit-identical to 1e-6, key points to 1e-9, dense polyline within the documented tolerance.

## Conventions

- Python: ruff (line length 110), type hints, frozen dataclasses for domain values, msgspec Structs for protocol messages. No comments that restate code.
- GDScript: typed, tabs, one autoload per concern (`Backend`, `Session`, `Origin`, `EventBus`), scenes under `client/scene`, no logic in `.tscn`.
- Cross-language behaviour (frame interpolation, trainset chain, origin mapping) is validated against `shared/golden/*.json` on both sides.
- ADRs in `docs/adr/` (numbered); update the relevant ADR when an invariant changes.

## Reference implementations (read-only, outside this repo)

- `D:\COYPU_Feeder\Claude Code\src\geometry\candidates.py` — PI/clothoid model; `src\landxml\builder.py` — Feeder LandXML dialect.
- `D:\MT\thesis-railway\readfile.py` (`ParseLandXML`, `discretizeSpiral`), `vehicle_engine.py`, `batch_export.py`, `gui.py:4106`, `project_file.py` — COYPU parser, kinematics semantics, CSV/archive formats.
