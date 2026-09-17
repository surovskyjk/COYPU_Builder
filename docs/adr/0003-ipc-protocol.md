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
- Lifecycle: client spawns `coypu-builder-backend serve --port 0 --token <random>` via `OS.execute_with_pipe`,
  reads `{"port": N}` from stdout, sends `session.hello`, heartbeats every 2 s, restarts on loss. `--backend-url`
  attaches to an externally started backend for debugging.
- `protocol/messages.py` (msgspec Structs) is the single source of truth; `tools/gen_protocol_docs.py` renders
  `docs/protocol/ipc.md` and CI fails when it is stale.

## Alternatives rejected

gRPC/ZeroMQ (no first-class Godot support), shared memory (needs GDExtension), Godot `var_to_bytes`
(no Python codec), MessagePack/FlatBuffers (extra dependencies on the Godot side for no gain at local latency).

## Status of implementation

T-101 built the generator side: `protocol/messages.py` now exports `ErrorCode`, `BlobSpec`, `MethodSpec` and
the `METHODS` registry as the actual SSOT (not just a stated intent), `tools/gen_protocol_docs.py` renders
`docs/protocol/ipc.md` from it, and CI runs `--check` in the `backend` job so a stale doc fails the build.
