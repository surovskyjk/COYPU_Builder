# T-116 — Cross-platform process supervision and socket reuse

**Milestone:** P1.M1 · **Depends on:** T-103, T-115 · **Blocks:** trustworthy CI for all of M2

## Context

The `client` CI job has failed on every push since the batch containing T-103, while the `backend` job passes
on both `windows-latest` and `ubuntu-latest`. The failure is **Linux-only** and lives in production client
code, so no amount of local Windows testing can reach it — the local suite is 60/60 green on the same commit
that CI rejects.

Two defects, from the run 35455438591 log:

**1. The process tree is never killed on Linux.**

```
ERROR: The process 2201 does not exist or is not a child of the calling process.
```

`IpcProcessSupervisor._kill_process_tree` calls `OS.execute("taskkill", ["/F", "/T", …])` on Windows and
plain `OS.kill(_pid)` everywhere else. Its comment reasons carefully about Windows needing a tree kill
because `uv run` is a process tree — but the same is true on Linux, and `OS.kill` / `OS.is_process_running`
there reject a PID that is not a direct child of the calling process. The errors repeat from
`process_supervisor.gd:76`, `:77` and `:79`, i.e. from `_process` → `is_running()`, so they fire every frame
once the supervisor is in this state.

**2. Reconnect reuses a socket that is not closed.**

```
ERROR: Condition "ready_state != STATE_CLOSED && ready_state != STATE_CLOSING" is true. Returning: ERR_ALREADY_IN_USE
ERROR: IPC: connect_to_url(ws://127.0.0.1:33913) failed: Already in use
```

`IpcWebSocketClient.connect_to_url` calls `_peer.connect_to_url` on the existing `WebSocketPeer` without
ensuring it has reached `STATE_CLOSED`. `WebSocketPeer` refuses, so **every reconnect attempt fails** and the
client never returns to `READY`. On Windows the tree kill succeeds, the socket closes promptly, and this path
is never taken — which is exactly why it went unseen.

Consequences in CI: `test_in_flight_request_resolves_with_disconnected_when_backend_is_killed` and
`test_killing_the_backend_process_reconnects_and_returns_to_ready` fail, along with 16 runtime errors.

This is not a test problem. The reconnect logic that ADR 0003 requires — "heartbeats every 2 s, restarts on
loss" — does not work on Linux, and a second client or a headless deployment there would be equally broken.

## Preconditions

- T-103 landed the supervisor, the `Backend` state machine and the lifecycle tests.
- T-115 landed the domain mirror; the local suite is 60/60 green on Windows.
- `gh` is authenticated, so CI logs are reachable with
  `gh run view --job <id> --log-failed`.
- The last known-good client CI run is 34101398993 (Phase 0, before the lifecycle tests existed).

Read before starting: `docs/adr/0003-ipc-protocol.md`, `client/ipc/process_supervisor.gd`,
`client/ipc/websocket_client.gd`, `client/core/backend.gd`,
`client/tests/integration/test_connection_lifecycle.gd`, and the failing CI log itself.

## Deliverables

| Path | Action |
|---|---|
| `client/ipc/process_supervisor.gd` | POSIX process-tree kill and a liveness check that does not error on a non-child PID |
| `client/ipc/websocket_client.gd` | make `connect_to_url` safe to call on a peer that is not `STATE_CLOSED` |
| `client/core/backend.gd` | only if the reconnect sequence itself needs to change |
| `client/tests/integration/test_connection_lifecycle.gd` | only if a test encodes a Windows-only assumption |
| `.github/workflows/ci.yml` | see "CI must prove it" below |
| `docs/adr/0003-ipc-protocol.md` | record that supervision is platform-specific and how |

## Contract

### Killing the tree on POSIX

The client spawns `uv run --project <dir> coypu-builder-backend serve`, which is a parent (`uv`) plus at
least one child (the Python interpreter). Killing only the PID Godot handed back leaves the backend running,
holding its port.

Kill the whole group, not the single PID. The conventional approach is to signal the process group —
`OS.execute("pkill", ["-TERM", "-P", str(pid)])` for children, or better, `kill -- -<pgid>` if the child was
spawned into its own group. Verify empirically which one actually reaps `uv run`'s Python child on Linux;
do not assume. Escalate `TERM` → `KILL` if the process survives a short grace period.

`is_running()` must not spam errors for a PID that is not a direct child. Prefer a check that cannot error
(`OS.is_process_running` guarded, or reading `/proc/<pid>` on Linux), and make the failure mode "assume it
died" rather than "log an engine error every frame".

### Reconnecting a socket

`connect_to_url` must be safe to call at any time. Either await `STATE_CLOSED` before reconnecting, or —
simpler and more robust — **construct a fresh `WebSocketPeer`** for each connection attempt and let the old
one be collected. Whichever you choose, `inbound_buffer_size` must still be applied to the peer actually in
use, and `_was_open` bookkeeping must not report a spurious `disconnected` for a peer that was replaced.

### CI must prove it

The whole point is that this class of bug is invisible locally. Add a step to the `client` job that fails the
build if the supervision errors appear at all, even when the assertions happen to pass — for example, tee the
gdUnit4 output and `grep -q` for `is not a child of the calling process` and `ERR_ALREADY_IN_USE`, failing
when either is found. A silent engine error that does not yet break a test is the warning you want.

## Invariants

- **ADR 0003:** the client spawns the backend, reads its port, heartbeats every 2 s and restarts on loss.
  That contract must hold on Linux as well as Windows.
- **ADR 0002:** typed GDScript. Platform branches stay inside `client/ipc/`; nothing above it should know
  which OS it is on.
- The backend is untouched. If you find yourself editing Python, stop and report.
- Do not weaken or delete a failing test to make CI green. The tests are correct; the code is not.

## Acceptance criteria

1. The `client` CI job passes on `ubuntu-latest` — verified by pushing a branch and reading the run, not by
   local reasoning. Report the run id.
2. The CI log contains **zero** occurrences of `is not a child of the calling process` and zero of
   `ERR_ALREADY_IN_USE`.
3. The new grep guard fails the build when the errors are present — prove it once by temporarily reverting
   one fix, observing red, then restoring.
4. The local Windows suite still passes: 60 of 60 test cases, exit 0, no orphaned processes.
5. `test_killing_the_backend_process_reconnects_and_returns_to_ready` genuinely reconnects on Linux, rather
   than passing because the backend never died.
6. After a full CI run, no backend process survives the job.
7. `docs/adr/0003-ipc-protocol.md` states how supervision differs per platform and why.

## Out of scope

- Any backend change.
- Any new client feature; M2 work of any kind.
- Replacing `OS.execute_with_pipe` with a different spawn mechanism, unless you can show the current one
  cannot be made to work — in which case report before doing it.
- Reworking the `Backend` state machine beyond what the reconnect fix requires.

## Verification

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

```bash
cd backend
uv run pytest -q
```

Then, on a branch, push and read the run:

```bash
gh run list --limit 3
gh run view --job <client job id> --log-failed
```

## Report back

State: which POSIX kill mechanism actually reaps `uv run`'s child and how you established it; whether you
replaced the peer or awaited closure, and why; the passing Linux CI run id; confirmation that the grep guard
fails when a fix is reverted; and the local Windows counts.
