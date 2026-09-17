# T-103 — Client app shell: main scene, autoloads, connection lifecycle

**Milestone:** P1.M0 · **Retires debt:** D1, D5 · **Depends on:** T-101, T-102 · **Blocks:** T-115 and every
later client task

## Context

The client is IPC plumbing without an application. There is no `main.tscn`, no autoload, and no
`client/core/origin.gd` — even though `docs/data-contracts/coordinate-conventions.md` cites that file as the
implementation of the domain → Godot axis mapping. `tools/run_dev.ps1` passes `--backend-url` and
`--backend-token` to a client that reads neither.

This task turns the repository into something that runs: `godot --path client` boots a scene, spawns or
attaches to a backend, completes the handshake, heartbeats, recovers from a backend crash, and shows the user
what state it is in. Everything in M1–M4 is built on the four autoloads created here, so their APIs are
contracts.

## Preconditions

- T-101 has landed: `session.ping` exists on the backend.
- T-102 has landed: gdUnit4 runs the client suite, `GoldenLoader` and `BackendFixture` exist.
- `client/ipc/{envelope,websocket_client,process_supervisor}.gd` work and are tested.
- `client/core/` contains only `.gitkeep`. `client/project.godot` has no `run/main_scene` and no autoloads.
- `shared/golden/origin_mapping.json` exists: a base point, four `(E, N, H) → godot` point pairs, and three
  frames with their expected Godot basis.

Read before starting: `docs/adr/0001-client-backend-split.md`, `docs/adr/0003-ipc-protocol.md`,
`docs/adr/0004-precision-and-local-origin.md`, `docs/data-contracts/coordinate-conventions.md`,
`client/README.md`, `tools/run_dev.ps1`, `backend/src/coypu_builder/domain/crs.py`.

## Deliverables

| Path | Action |
|---|---|
| `client/core/origin.gd` | new — autoload `Origin` |
| `client/core/event_bus.gd` | new — autoload `EventBus` |
| `client/core/backend.gd` | new — autoload `Backend` |
| `client/core/session.gd` | new — autoload `Session` |
| `client/core/cli_args.gd` | new — parses `--backend-url` / `--backend-token` / `--project` |
| `client/scene/main.tscn` + `client/scene/main.gd` | new — temporary bootstrap scene |
| `client/project.godot` | `run/main_scene`, the four autoloads |
| `client/tests/unit/test_origin.gd` | new — golden-pinned mapping tests |
| `client/tests/unit/test_cli_args.gd` | new |
| `client/tests/integration/test_connection_lifecycle.gd` | new — handshake, heartbeat, crash recovery |
| `docs/data-contracts/coordinate-conventions.md` | correct the implementation reference to the real path |
| `client/README.md` | describe the autoloads and the two launch modes |

## Contract

### `Origin` — what it is *not*

The backend already maps every position it sends into Godot axes relative to the base point
(`handlers.py` calls `points_to_godot` / `basis_to_godot`). `Origin` therefore does **not** transform incoming
geometry — doing so would double-apply the mapping and would violate ADR 0001. Its jobs are:

1. hold the project's base point for display,
2. map **back** from a Godot position to `(E, N, H)` for inspector and status readouts and, later, picking,
3. be the one place the client knows the axis convention, pinned to the backend by golden vectors.

```gdscript
extends Node   # autoload name: Origin

signal base_point_changed(easting: float, northing: float, height: float)

func set_base_point(easting: float, northing: float, height: float) -> void
func has_base_point() -> bool
func base_point() -> Vector3            # (E0, N0, H0) as float64-ish; see the precision note below

## (E, N, H) -> Godot. Present for readouts and tests, NOT for transforming backend geometry.
func to_godot(easting: float, northing: float, height: float) -> Vector3
## Godot -> (E, N, H). Returns a Vector3 carrying (E, N, H) in that order.
func from_godot(local: Vector3) -> Vector3
## Domain frame columns -> Godot Basis, columns (right, up, back); -Z is the tangent.
func basis_from_frame(tangent: Vector3, left: Vector3, up: Vector3) -> Basis
```

Precision note: GDScript `float` is 64-bit, so `to_godot` / `from_godot` are exact against the golden file
even though the *wire* is float32. Compare against `origin_mapping.json` at `1e-9`, not at float32 tolerance.

### `EventBus`

One autoload of typed signals, no logic, no state. Declare at least:

```gdscript
extends Node   # autoload name: EventBus

signal backend_state_changed(state: int)          # Backend.State
signal backend_error(code: String, message: String)
signal project_changed()                          # Session.project_info replaced
signal alignments_changed()
```

Later tasks add signals here; they do not add cross-autoload direct calls.

### `Backend`

Owns one `IpcProcessSupervisor` and one `IpcWebSocketClient`, and is the only object in the client that talks
to them.

```gdscript
extends Node   # autoload name: Backend

enum State { DISCONNECTED, SPAWNING, CONNECTING, HANDSHAKING, READY, RECONNECTING, FAILED }

signal state_changed(state: State)
signal event_received(envelope: IpcEnvelope)       # server-push 'evt' envelopes

const HEARTBEAT_INTERVAL_SEC := 2.0                # ADR 0003
const HEARTBEAT_MISS_LIMIT := 3
const REQUEST_TIMEOUT_SEC := 30.0

func state() -> State
func server_version() -> String
func protocol_version() -> int
func start() -> void        # spawn mode, or attach mode when --backend-url was given
func shutdown() -> void

## Coroutine. Awaits the matching 'res'/'err' envelope. On timeout or a dead connection it returns a
## locally synthesised 'err' envelope with code "E_TIMEOUT" / "E_DISCONNECTED" so that callers have exactly
## one failure shape to handle and never await forever.
func request(method: String, params: Dictionary = {}) -> IpcEnvelope
```

Lifecycle: `start()` → spawn (or attach) → connect → `session.hello` with the token → `READY`. Heartbeat
`session.ping` every 2 s once `READY`; three consecutive misses, a socket close, or a supervisor
`backend_crashed` signal all mean the connection is lost. Reconnect with backoff 1 s, 2 s, 4 s, 8 s, 10 s,
then `FAILED`. Every in-flight `request()` awaiting a reply when the connection drops must be resolved with
an `E_DISCONNECTED` envelope — never left hanging.

Attach mode (`--backend-url`) skips the supervisor entirely and never spawns or kills a process; a lost
connection there still retries, because the developer may be restarting the backend under a debugger.

### `Session`

The client's read-only mirror of backend document state. This task populates only what `project.get` returns.

```gdscript
extends Node   # autoload name: Session

func project_id() -> String
func crs() -> String
func alignments() -> Array[Dictionary]   # AlignmentSummary dictionaries, as they arrive on the wire
func set_project_info(result: Dictionary) -> void   # also updates Origin's base point, emits EventBus signals
```

T-115 extends this with runs, layers and the entity registry; do not anticipate that here.

### `CliArgs`

Parses `OS.get_cmdline_user_args()` (the arguments after `--`, which is how `run_dev.ps1` passes them):
`--backend-url <url>`, `--backend-token <token>`, `--project <path>`. Unknown arguments are ignored with a
warning. Pure and unit-testable: the parse function takes a `PackedStringArray` and returns a Dictionary, and
only a thin wrapper reads the real command line.

### `main.tscn`

Deliberately temporary and marked as such in a comment in `main.gd`: `WorldEnvironment`, a `Camera3D` with
simple free-look, a ground grid or `GridMap`-free reference plane, and a `CanvasLayer` label showing backend
state, server version, protocol version, project id and CRS. M2 replaces the camera (T-124), M4 replaces the
overlay (T-140). Do not invest in visual design here.

When a `--project` path ending in `.xml` is supplied, the scene may call `project.new` + `import.landxml` and
show the resulting alignment summaries in the label. That is the full extent of feature work permitted.

## Invariants

- **ADR 0001 / ADR 0007:** the client computes no geometry. `Origin` maps readouts, nothing more. No RPC is
  issued from `_process`; the heartbeat runs on a `Timer`.
- **ADR 0004:** the axis mapping is `x = E−E0`, `y = H−H0`, `z = −(N−N0)`; Basis columns are (right, up,
  back) so `-Z` is the tangent. `origin.gd` is the only client file that encodes this.
- **ADR 0002:** typed GDScript, tabs, one autoload per concern, no logic in `.tscn`.
- `client/ipc/*.gd` should need no changes. If it does, keep the change minimal and report it.

## Acceptance criteria

1. `test_origin.gd` reproduces every point and every frame in `shared/golden/origin_mapping.json` to `1e-9`,
   and `from_godot(to_godot(p)) == p` round-trips to `1e-9`.
2. `godot --path client` (windowed) boots, spawns a backend, reaches `READY`, and the overlay shows a real
   server version and protocol version.
3. `tools\run_dev.ps1` launches a backend and a client that **attaches** to it via `--backend-url` without
   spawning a second backend — verify only one `python`/`coypu-builder-backend` process exists.
4. Killing the backend process while the client is running moves it to `RECONNECTING` and back to `READY`
   after it respawns (spawn mode). This is asserted in `test_connection_lifecycle.gd`.
5. A `request()` in flight when the backend is killed resolves with `E_DISCONNECTED` rather than hanging.
6. `request()` to a method that does not exist resolves with `E_UNKNOWN_METHOD`, not a timeout.
7. Heartbeats are visible on the backend as `session.ping` calls roughly every 2 s and stop on shutdown.
8. Closing the client leaves no orphaned backend process.
9. `docs/data-contracts/coordinate-conventions.md` names the file that now exists.

## Out of scope

- Any UI theme, dock, panel or toolbar (M4).
- Track, terrain or vehicle rendering (M2, M3).
- The alignment/run table mirror and interpolation (T-115) — `Session` holds summaries only.
- Reading `frame_eval.json`; that golden belongs to T-115.
- Saving anything to disk.

## Verification

```bash
cd backend
uv run pytest -q
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
tools\run_dev.ps1
```

## Report back

State: the final autoload APIs if they deviated from this contract and why; how in-flight requests are
resolved on disconnect; measured reconnect behaviour after a forced backend kill; whether anything in
`client/ipc/*.gd` had to change; and any place where the ADRs and the code disagreed.
