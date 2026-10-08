# T-126 — API foundations and IPC hardening

**Milestone:** Phase 1 workflow tooling · **Depends on:** T-116 (and T-138, whose new method it classifies)
· **Blocks:** T-146 (MCP server) and any other external client

## Context

ADR 0010 makes the IPC protocol the one public API: the Godot client, future mods, scripts and the MCP server
all call the same methods. Before anything other than our own client attaches, three things must be true:

- each method declares how stable it is;
- an external client can find a running backend;
- the connection is properly secured.

F28 lists five security gaps that are acceptable for a private local tool but not for an API:

1. the token is not cryptographically random;
2. an empty token disables the check;
3. the token travels on a command line;
4. browser `Origin` headers are accepted;
5. the comparison is not constant-time.

## Preconditions

- `protocol/messages.py`: `MethodSpec` and the `METHODS` tuple; `tools/gen_protocol_docs.py` generates
  `docs/protocol/ipc.md` and CI checks it is current.
- `server/app.py: serve(host="127.0.0.1", port=0, token="")`; `server/handlers.py` checks the token in
  `session.hello` and skips the check when the expected token is empty.
- `__main__.py serve` prints `{"port": N}` on stdout; `client/ipc/process_supervisor.gd` spawns
  `uv run coypu-builder-backend serve --port 0 --token <token>` through `OS.execute_with_pipe` and reads that
  line.
- `client/core/backend.gd` generates the token from `Time.get_ticks_usec()` and `randi()`, and has an attach
  mode driven by `--backend-url` and `--backend-token`, which `tools/run_dev.ps1` uses with the token `"dev"`.

Read before starting: `docs/adr/0010-extensibility-api-plugins-mcp.md` (decisions 1–3),
`docs/adr/0003-ipc-protocol.md`, `docs/tasks/task_116_cross_platform_supervision.md` (process-tree
behaviour that must not regress).

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/protocol/messages.py` | `Stability` enum; required `stability` on `MethodSpec`; classify every method |
| `tools/gen_protocol_docs.py`, `docs/protocol/ipc.md` | show each method's tier; regenerate |
| `backend/src/coypu_builder/server/discovery.py` | new — discovery records |
| `backend/src/coypu_builder/server/app.py` | `Origin` rejection; no-token mode only through the test flag |
| `backend/src/coypu_builder/server/handlers.py` | constant-time token check, no empty-token bypass |
| `backend/src/coypu_builder/__main__.py` | `serve` generates its own token; new startup line; discovery record |
| `backend/src/coypu_builder/__init__.py` | bump `PROTOCOL_VERSION` per its existing scheme |
| `backend/tests/test_ipc_security.py` | new |
| `backend/tests/` (existing server tests) | adapt to the generated token |
| `client/ipc/process_supervisor.gd` | no token argument; read port and token from the startup line |
| `client/core/backend.gd` | stop generating tokens; attach mode reads the discovery record |
| `client/core/cli_args.gd` | drop `--backend-token`; add `--attach` |
| `tools/run_dev.ps1` | no token parameter or argument |
| `client/tests/` (affected unit and integration tests) | adapt; add supervisor argument tests |
| `docs/protocol/attach.md` | new — how an external client finds and authenticates to a backend |
| `backend/tests/test_repo_hygiene.py` | new — guard on committed Claude Code configuration |
| `.gitignore` | add `.claude/worktrees/`, `.claude/agent-memory-local/`, `CLAUDE.local.md` |

## Contract

### Stability tiers

```python
class Stability(StrEnum):
    INTERNAL = "internal"          # the Godot client only; may change any time
    EXPERIMENTAL = "experimental"  # usable externally; may change with a release note
    STABLE = "stable"              # changes only with a PROTOCOL_VERSION bump and a deprecation period

class MethodSpec(msgspec.Struct, frozen=True):
    name: str
    summary: str
    stability: Stability           # required, no default
    ...                            # existing fields unchanged
```

Classify `session.hello` and `session.ping` as `stable` and every other method as `experimental`, including
T-138's `alignment.envelope`. Promotion to `stable` is T-146's decision.

### Token and startup line

- `serve` creates the token itself with `secrets.token_urlsafe(32)`, and has no `--token` option.
- It prints exactly one JSON line on stdout and flushes:
  `{"port": N, "token": "<token>", "pid": P, "protocol": "<PROTOCOL_VERSION>"}`.
- **When stdout is a terminal, the line omits `token`** and names the discovery-record path instead, so a
  manual run never shows the secret on screen.
- `session.hello` compares tokens with `hmac.compare_digest`. A wrong or missing token returns
  `E_UNAUTHORIZED`.
- Only the Python test API may start a server without a token (`serve(..., token=None, allow_no_token=True)`);
  the CLI cannot.
- **The token never appears in a command line, a log line or an error message** — including the
  supervisor's spawn arguments and any `push_error`/`print` in the client.

### Discovery record

```python
# server/discovery.py
@dataclass(frozen=True)
class DiscoveryRecord:
    port: int
    pid: int
    protocol: str
    backend_version: str
    token: str
    started_at: str                    # ISO 8601 UTC

def runtime_dir() -> Path: ...         # created if missing, user-only
def write_record(record: DiscoveryRecord) -> Path: ...    # atomic: temp file + rename
def remove_record(pid: int) -> None: ...
def live_records() -> list[DiscoveryRecord]: ...          # skips and removes records whose pid is dead
```

- Location: Windows `%LOCALAPPDATA%\COYPU Builder\runtime\backend-<pid>.json`. Linux
  `$XDG_RUNTIME_DIR/coypu-builder/backend-<pid>.json`, falling back to `~/.cache/coypu-builder/runtime/`;
  the directory is mode `0700` and the file `0600`.
- `serve` writes the record once it is listening, and removes it on clean shutdown (including the signals
  T-116 sends).
- The record never contains a project path or any project data.

### Listener

- `serve` binds to loopback only (unchanged).
- A WebSocket handshake carrying any `Origin` header is refused (HTTP 403) before any message is read. A
  handshake without one is accepted. Use the library's origin check rather than hand-parsing headers.

### Client

- `ProcessSupervisor` spawns `serve --port 0` with no token, reads port and token from the startup line, and
  keeps the token in memory only.
- `--attach` (optionally with `--backend-url`) makes `Backend` read the discovery record — the newest live one,
  or the one whose port matches the URL — instead of taking a token on the command line.
- `tools/run_dev.ps1` starts the backend, then the client with `--attach`.

### Committed Claude Code configuration

`.claude/agents/*.md` is committed so that the worker/reviewer workflow is versioned. Claude Code executes
that configuration for anyone who opens the repository, and an agent definition may declare `hooks` (shell
commands) and `mcpServers`. `test_repo_hygiene.py` fails if:

- the frontmatter of any `.claude/agents/*.md` declares `hooks`, `mcpServers`, or a `permissionMode` other
  than `default` or `plan`;
- a committed `.claude/settings.json` declares `hooks` or `mcpServers`;
- `git ls-files` lists `.claude/settings.local.json` or anything under `.claude/worktrees/` or
  `.claude/agent-memory-local/`.

## Invariants

- No regression in T-116's process-tree supervision on Windows or Linux: no orphan after a kill or a
  reconnect.
- `docs/protocol/ipc.md` stays generated, never hand-edited.
- Nothing secret is written into the repository, CI logs or test output.

## Acceptance criteria

1. Every `MethodSpec` declares a tier (a missing tier is a type error); `ipc.md` shows the tiers;
   `gen_protocol_docs.py --check` exits 0.
2. A wrong or missing token gets `E_UNAUTHORIZED`; the CLI cannot start a server without a token; tokens are
   compared in constant time.
3. A handshake with `Origin: https://example.com` is rejected before any message; one without `Origin`
   succeeds.
4. The discovery record is written atomically, is user-only (mode asserted on Linux), matches the running
   server, disappears on clean shutdown, and a record whose pid is dead is skipped by `live_records()`.
5. With stdout attached to a terminal, the startup line carries no token.
6. No token on any command line: a unit test of the supervisor's spawn arguments, plus a check that `--token`
   and `--backend-token` appear nowhere in `client/` or `tools/`.
7. No token in logs: integration tests capture backend stderr and client output, and assert the session token
   never appears in either.
8. `tools/run_dev.ps1` works end to end through `--attach`; a bare `godot --path client` launch still spawns
   and connects to its own backend.
9. `test_repo_hygiene.py` passes on the current tree and fails, in a test using a temporary copy, for an agent
   file that declares `hooks`.
10. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
    weakened, and no orphaned backend process after the client suite.

## Out of scope

- Session scopes (read, edit, import, file-system write) — T-146.
- Plugin entry points for readers and providers — T-130; method plugins and client mods — Phase 2.
- The MCP server — T-146.
- TLS — not planned; the listener is loopback-only.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\run_dev.ps1
```

After the client suite, confirm that no `coypu-builder-backend` process remains.

## Report back

State: the tier given to each method; where the discovery record landed on Windows and how its permissions
were set; how the Origin check was implemented and tested; how you proved the token is absent from command
lines and logs; and the new `PROTOCOL_VERSION`.
