# ADR 0003 — Localhost WebSocket IPC with JSON header + binary blobs

Status: accepted (2026-09-06)

## Decision

- Transport: one localhost WebSocket connection (`WebSocketPeer` in Godot, `websockets` in Python).
  Request/response with ids plus server-push events.
- Envelope (binary frame): `[u32 LE header_len][UTF-8 JSON header][blob 0][blob 1]…`. The header carries
  `v, id, type (req|res|evt|err), method, params|result|error` and a `blobs` list of
  `{name, dtype, shape, offset, length}` (little-endian, C-order). Godot decodes with
  `PackedByteArray.slice(...).to_float32_array()` etc.; Python with `np.frombuffer`.
- Bulk static assets (terrain height tiles, ortho images) are written to a session cache directory and referenced
  by path in the header.
- Lifecycle: client spawns `coypu-builder-backend serve --port 0` via `OS.execute_with_pipe`, reads
  `{"port": N, "token": "...", "pid": P, "protocol": "..."}` from stdout (the backend creates its own token,
  ADR 0010), sends `session.hello` with it, heartbeats every 2 s, restarts on loss. `--attach` (optionally with
  `--backend-url`) attaches to an externally started backend through its discovery record
  (`docs/protocol/attach.md`).
- `protocol/messages.py` (msgspec Structs) is the single source of truth; `tools/gen_protocol_docs.py` renders
  `docs/protocol/ipc.md` and CI fails when it is stale.

## Alternatives rejected

gRPC/ZeroMQ (no first-class Godot support), shared memory (needs GDExtension), Godot `var_to_bytes`
(no Python codec), MessagePack/FlatBuffers (extra dependencies on the Godot side for no gain at local latency).

## Status of implementation

T-101 built the generator side: `protocol/messages.py` now exports `ErrorCode`, `BlobSpec`, `MethodSpec` and
the `METHODS` registry as the actual SSOT (not just a stated intent), `tools/gen_protocol_docs.py` renders
`docs/protocol/ipc.md` from it, and CI runs `--check` in the `backend` job so a stale doc fails the build.

T-116 found that process supervision is platform-specific and was only implemented for Windows, which the
`backend` job's Windows-only-passing / `client` job's Linux-only-failing split hid from local testing
entirely. `client/ipc/process_supervisor.gd` now branches:

- **Windows**: `taskkill /F /T` on the tracked PID, as before — it kills the whole `uv` → shim → interpreter
  tree in one shot, and `OS.is_process_running` is safe to poll for liveness.
- **POSIX**: there is no tree-kill equivalent, and Godot never places the spawned process in its own process
  group, so `kill -- -<pgid>` would hit Godot itself. Instead, `uv`'s direct children are found with
  `pgrep -P` *before* signalling anything (once `uv` exits they're reparented to init and no longer found
  that way), `SIGTERM` is sent to `uv` and each child, and whatever is still alive after a 200 ms grace
  period gets `SIGKILL`. Liveness is checked with a plain `/proc/<pid>` existence test rather than
  `OS.is_process_running`/`OS.kill`, because both of those raise an engine-level "does not exist or is not
  a child of the calling process" error for a PID that isn't (or is no longer) a direct child of Godot —
  routine here once a kill above has reaped it. A `kill -0` fallback covers POSIX systems without `/proc`
  (e.g. macOS, not a current CI target).

`client/ipc/websocket_client.gd`'s `connect_to_url` now constructs a fresh `WebSocketPeer` per call instead
of reusing one: `WebSocketPeer.connect_to_url` refuses to run again until the existing peer reaches
`STATE_CLOSED`, and the restart-on-loss path above calls it as soon as a respawned backend is ready, with no
guarantee the old peer has finished closing. The Windows tree-kill closes the old socket promptly enough that
this race was never observed there; on Linux it failed every reconnect with `ERR_ALREADY_IN_USE`.

## Addendum (2026-10-14): long calls never block the event loop, and requests stay sequential

**Off the event loop.** Any handler that can spend more than about 100 ms of CPU time runs off the asyncio
event loop: in a worker thread when the heavy library releases the GIL, which Qhull, numpy, laspy and
rasterio do, otherwise in a worker process. A test must show `session.ping` still answering during the call.

The client counts a heartbeat miss only when a ping has no reply within its 30 s request timeout, so a blocked
loop does not disconnect it at once. But a blocked loop delays every reply and the client's view of the
connection, so the 100 ms rule stands.

The first case was the TIN build in T-132 (ROADMAP F34): about 8.8 s for the Kralupy corridor, with one
remaining 0.6 s GIL stall at the end of Qhull.

**Sharing and limits.** Concurrent requests for the same result share one computation. A result computed for
a superseded state is discarded rather than cached. Each connection runs at most 4 off-loop jobs at once and
holds at most 64 pending requests; beyond that it answers `E_BUSY`.

**Sequential consistency per connection.** Within one connection, every request observes all state changes
made by requests sent before it on that connection, and none made by requests sent after it.
`session.ping` is always answered immediately.

Every method declares `mutates`. A mutating request is a barrier: it waits for the previous barrier and then
becomes the new one. Read-only requests wait for the latest barrier that arrived before them, and may run
concurrently with each other. The rule keeps pipelining safe for scripts and for the MCP server (T-146),
which do not wait for each reply before sending the next request.
