# T-102 — Vendor gdUnit4, port the client suite, switch CI

**Milestone:** P1.M0 · **Retires debt:** D4 · **Depends on:** nothing · **Blocks:** T-103, T-115

## Context

ADR 0008 chose gdUnit4 for the client and `CLAUDE.md` documents the command to run it, but the addon was
never vendored — the command in the documentation cannot execute. The client suite is instead a hand-rolled
`SceneTree` runner, `client/tests/run_ipc_tests.gd`, which works (27 assertions, green) but hand-rolls
signal awaiting, timeouts, boxed-lambda workarounds and its own pass/fail bookkeeping.

The client is about to grow by an order of magnitude across M1–M4. The test harness has to be settled before
that happens, not after, and every later client task assumes gdUnit4 plus a golden-vector loader exists.

## Preconditions

- `client/addons/` contains only `.gitkeep`; no addon is enabled in `client/project.godot`.
- `client/tests/run_ipc_tests.gd` holds two groups of assertions: pure codec unit tests, and a live
  round-trip that spawns the real backend through `IpcProcessSupervisor`.
- `.github/workflows/ci.yml`'s `client` job runs `godot --headless --editor --path client --quit` to build
  `global_script_class_cache.cfg`, then runs the suite via `--script`.
- Godot 4.7.2 is at `tools\godot\Godot_v4.7.2-stable_win64.exe` after `tools\install_godot.ps1`.

Read before starting: `docs/adr/0002-client-language.md`, `docs/adr/0008-tooling.md`,
`client/tests/run_ipc_tests.gd`, `.github/workflows/ci.yml`, `client/README.md`.

## Deliverables

| Path | Action |
|---|---|
| `client/addons/gdUnit4/**` | new — vendored addon, committed, at a pinned release |
| `client/project.godot` | enable the plugin; add the test-related settings gdUnit4 requires |
| `client/tests/unit/test_ipc_envelope.gd` | new — the codec assertions, ported |
| `client/tests/integration/test_backend_roundtrip.gd` | new — the live backend assertions, ported |
| `client/tests/helpers/backend_fixture.gd` | new — spawn/teardown of a real backend for integration tests |
| `client/tests/helpers/golden_loader.gd` | new — loads `shared/golden/*.json` from the client |
| `client/tests/helpers/uv_locator.gd` | new — the `uv` discovery logic, lifted out of the old runner |
| `client/tests/run_ipc_tests.gd` | delete (and its `.uid`) once parity is proven |
| `.github/workflows/ci.yml` | run gdUnit4; keep the editor-import step |
| `client/README.md` | document the addon version and how to run unit-only vs full suites |
| `CLAUDE.md` | correct the client test command so it is true |

## Contract

### Addon pinning

Vendor the newest gdUnit4 release that declares support for Godot 4.4+ and runs cleanly on 4.7.2. Record the
exact version tag in `client/README.md`. Vendor the addon source into the repository — do not add a submodule,
and do not rely on the Godot asset library at build time; CI must work from a plain checkout.

Verify `.gitignore` does not swallow any vendored path (`client/.godot/` is ignored; `client/addons/` must
not be) with `git check-ignore -v` on a few addon files before committing.

### Suite layout

```
client/tests/
  unit/          no backend, no network, no timers longer than a frame — must run in < 5 s total
  integration/   may spawn the real backend through backend_fixture.gd
  helpers/       shared test utilities, not themselves test suites
```

The split is load-bearing: local iteration runs `-a tests/unit`, CI runs `-a tests`.

### `golden_loader.gd`

```gdscript
class_name GoldenLoader
extends RefCounted

## Loads shared/golden/<name>.json as a Dictionary. Fails the calling test with a clear message when the
## file is missing, rather than returning an empty dictionary that produces a confusing assertion failure.
static func load_golden(name: String) -> Dictionary
```

Resolve the path from `res://` via `ProjectSettings.globalize_path("res://").path_join("../shared/golden")`.
This helper is how T-103, T-115 and every later cross-language task consume goldens, so its name and
signature are binding.

### `backend_fixture.gd`

Encapsulates: locate `uv`, start `IpcProcessSupervisor` with
`["run", "--project", <backend dir>, "coypu-builder-backend", "serve"]`, await the port line, connect an
`IpcWebSocketClient`, complete `session.hello`, and tear the whole thing down — including killing the process
tree — in a way that cannot leak a backend process when a test fails or times out.

```gdscript
class_name BackendFixture
extends RefCounted

const TOKEN := "gdunit-tests"

static func start() -> BackendFixture      # null when uv cannot be located
func client() -> IpcWebSocketClient
func request(method: String, params: Dictionary) -> IpcEnvelope   # coroutine, times out to a synthetic err
func stop() -> void
```

### Assertion parity

The ported suites must cover every assertion the old runner made — enumerate them from
`run_ipc_tests.gd` and check them off. Concretely: envelope encode/decode round-trip, `<f4` 1-D and
`(n, 3)` decoding, `<i4` decoding, blob offsets, backend spawn and port reporting, WebSocket connect,
`session.hello`, `project.new`, `import.landxml` returning exactly one alignment,
`alignment.frame_table` blob sizes against `row_count`, and `E_NOT_FOUND` for an unknown alignment id.

### CI

Keep the editor-import step and its explanatory comment — without it, headless scripts that reference
cross-file `class_name` types fail to parse. Then invoke gdUnit4's command tool and let its exit code fail
the job. Confirm the exit code is actually non-zero on failure by breaking one assertion locally once.

## Invariants

- ADR 0002: typed GDScript, tabs, no C#. The vendored addon is third-party and exempt from house style; your
  own test code is not.
- ADR 0008: gdUnit4 is the client test framework; `--headless` in CI.
- No production client code (`client/ipc/*.gd`) changes in this task. If a test can only be written by
  modifying production code, that is a finding to report, not a change to make.

## Acceptance criteria

1. `godot --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode` runs both suites and
   exits 0.
2. The same command with a deliberately broken assertion exits non-zero.
3. `-a tests/unit` runs with no backend process spawned and completes in under 5 seconds.
4. Assertion count is greater than or equal to the old runner's 27, with every listed assertion present.
5. No orphaned `coypu-builder-backend`, `uv` or `python` process remains after a full run, including after a
   forced failure. Check with `Get-Process` after the run.
6. `client/tests/run_ipc_tests.gd` and its `.uid` are deleted.
7. `CLAUDE.md`'s client test command, run verbatim, works.

## Out of scope

- Any new client feature, autoload or scene — that is T-103.
- Tests for code that does not exist yet.
- Coverage reporting, mutation testing, CI matrix expansion.
- Replacing `IpcProcessSupervisor`'s own logic; the fixture wraps it, it does not reimplement it.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

```bash
cd backend
uv run pytest -q
```

## Report back

State: the pinned gdUnit4 version and where it came from; the final assertion count against the old 27 with
any gaps named; whether the editor-import CI step was still required; and any friction between gdUnit4 and
Godot 4.7.2 that a later task should know about.
