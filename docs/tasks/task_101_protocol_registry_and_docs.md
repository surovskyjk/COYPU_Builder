# T-101 — Protocol method registry, documentation generator, CI freshness gate

**Milestone:** P1.M0 · **Retires debt:** D2 · **Depends on:** nothing · **Blocks:** T-103, T-114

## Context

ADR 0003 states that `protocol/messages.py` is the single source of truth for the wire format, that
`tools/gen_protocol_docs.py` renders `docs/protocol/ipc.md`, and that CI fails when that file is stale. None
of the generator side was built. Worse, the truth is currently split three ways with nothing binding it:
method names live in `DISPATCH` (`server/handlers.py`), params/result shapes live in `messages.py`, the blob
contract for `alignment.frame_table` lives in a code comment, and error codes are bare string literals
scattered through handler bodies.

This task makes the method table **data**, generates the documentation from it, and gates it in CI — so that
T-114, which adds six methods, cannot introduce drift.

## Preconditions

- `backend/src/coypu_builder/protocol/messages.py` defines `Envelope`, `BlobRef`, `ErrorInfo` and the
  per-method params/result Structs.
- `backend/src/coypu_builder/server/handlers.py` defines `DISPATCH` with six methods and raises
  `ProtocolError(code, message)` with ad-hoc string codes.
- `docs/protocol/` contains only `.gitkeep`.

Read before starting: `docs/adr/0003-ipc-protocol.md`, `backend/src/coypu_builder/protocol/messages.py`,
`backend/src/coypu_builder/server/{codec,handlers,app}.py`, `client/ipc/envelope.gd`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/protocol/messages.py` | add `ErrorCode`, `BlobSpec`, `MethodSpec`, `METHODS`; add `SessionPingParams` / `SessionPingResult` |
| `backend/src/coypu_builder/server/handlers.py` | use `ErrorCode` instead of string literals; add `handle_session_ping`; register it in `DISPATCH` |
| `tools/gen_protocol_docs.py` | new — renderer with a `--check` mode |
| `docs/protocol/ipc.md` | new — generated output, committed |
| `backend/tests/test_protocol_contract.py` | new — registry/dispatch agreement, docs freshness, ping round-trip |
| `.github/workflows/ci.yml` | add the freshness step to the `backend` job |
| `docs/adr/0003-ipc-protocol.md` | add a short "Status of implementation" line noting the registry is the SSOT |

## Contract

### Error codes

```python
class ErrorCode(StrEnum):
    BAD_FRAME = "E_BAD_FRAME"
    BAD_PARAMS = "E_BAD_PARAMS"
    UNKNOWN_METHOD = "E_UNKNOWN_METHOD"
    UNAUTHORIZED = "E_UNAUTHORIZED"
    NO_SESSION = "E_NO_SESSION"
    NO_PROJECT = "E_NO_PROJECT"
    NOT_FOUND = "E_NOT_FOUND"
    EMPTY = "E_EMPTY"
    CRS_REQUIRED = "E_CRS_REQUIRED"
    INTERNAL = "E_INTERNAL"
```

Every existing raise site adopts the enum. The wire values must not change — the Godot suite asserts on
`E_NOT_FOUND` today and must keep passing untouched.

### Registry

```python
class BlobSpec(msgspec.Struct, frozen=True):
    name: str
    dtype: str            # numpy dtype string as it appears on the wire, e.g. "<f4", "<i4"
    shape: str            # documentation shape, e.g. "(n,)" or "(n, 3)" or "(n, 4)"
    description: str

class MethodSpec(msgspec.Struct, frozen=True):
    name: str
    summary: str          # one line, imperative
    params: type | None
    result: type | None
    blobs: tuple[BlobSpec, ...] = ()
    errors: tuple[ErrorCode, ...] = ()

METHODS: tuple[MethodSpec, ...] = (...)   # order is the documentation order and must be stable
```

`METHODS` covers all seven methods after this task: `session.hello`, `session.ping`, `project.new`,
`project.get`, `import.landxml`, `alignment.frame_table`, `run.get`. The `alignment.frame_table` entry
carries the ten blobs that `handle_alignment_frame_table` actually emits, in emission order — that ordering
is part of the contract because the tail is concatenated in dict order.

### `session.ping`

Liveness probe for the client heartbeat (ADR 0003, 2 s interval). Requires `session.hello` first.

```python
class SessionPingParams(msgspec.Struct, frozen=True):
    client_time_ms: int = 0

class SessionPingResult(msgspec.Struct, frozen=True):
    client_time_ms: int      # echoed back so the client can measure round-trip latency
    server_time_ms: int      # time.time_ns() // 1_000_000
```

### Generator

`tools/gen_protocol_docs.py`, run from `backend/` as `uv run python ../tools/gen_protocol_docs.py`:

- Default mode writes `docs/protocol/ipc.md`.
- `--check` renders to memory, compares with the file on disk, and exits `1` with a unified diff on mismatch
  and `0` when identical.
- Output is **deterministic**: no timestamps, no absolute paths, no dictionary-iteration-order dependence,
  no version string that changes on every release. Two runs on the same source produce byte-identical files.
- Field types are read from the Structs by introspection (`__struct_fields__` plus
  `typing.get_type_hints`, or `msgspec.inspect.type_info`) — never by hand-maintained tables.

The rendered document contains, in this order: the envelope and framing description (from ADR 0003), the blob
encoding rules, the error-code table, and then one section per method with its summary, params table
(field, type, default), result table, blob table and possible errors.

## Invariants

- ADR 0003: `protocol/messages.py` is the single source of truth. The generator reads it; nothing generates
  *into* it.
- The wire format does not change in this task. `client/ipc/envelope.gd` must keep working unmodified, and
  `client/tests/run_ipc_tests.gd` must keep passing unmodified.
- `domain/` stays pure — none of this touches it.

## Acceptance criteria

1. `METHODS` and `DISPATCH` have identical key sets, asserted by a test that fails loudly when they diverge.
2. Every `MethodSpec.params` / `.result` is a `msgspec.Struct` subclass or `None`, asserted by a test.
3. `uv run python ../tools/gen_protocol_docs.py` then `--check` exits 0; mutating any field in a params
   Struct makes `--check` exit 1 (verify this manually once, then revert).
4. Running the generator twice produces byte-identical output.
5. `session.ping` round-trips over a real WebSocket in `tests/test_server_smoke.py` style, echoes
   `client_time_ms`, and returns `E_NO_SESSION` when called before `session.hello`.
6. No string literal `"E_..."` remains in `server/handlers.py` or `server/app.py`.
7. The existing 46 pytest tests and the 27 Godot assertions still pass, unmodified.

## Out of scope

- Any new method beyond `session.ping` — the run/catalogue surface is T-114.
- Client-side heartbeat logic — that is T-103; this task only provides the method it calls.
- Protocol versioning or negotiation beyond the existing `PROTOCOL_VERSION` constant.
- Generating GDScript bindings from the registry. Tempting; not now.

## Verification

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --script res://tests/run_ipc_tests.gd
```

## Report back

State: the final `METHODS` entries; whether any existing handler behaviour had to change to adopt `ErrorCode`;
the introspection approach chosen for field types and why; anything in ADR 0003 you found to be untrue of the
current code.

---

## Follow-up F1 — bring `tools/` into the lint path

*Added 2026-09-17 after the T-101 review. Small, and it must land before T-111 and T-112 extend
`tools/make_golden.py`.*

CI runs `uv run ruff check .` with `backend/` as the working directory, so nothing under `tools/` has ever
been linted. `tools/gen_protocol_docs.py:142` is 112 characters, over the repo's 110-character limit, and it
passed review only because the checker never saw the file.

**Deliverables**

| Path | Action |
|---|---|
| `tools/gen_protocol_docs.py` | wrap the over-long line |
| `backend/pyproject.toml` | extend the ruff configuration to cover `../tools`, or add a root `ruff.toml` inheriting the same settings (line length 110, `py313`, the existing `select` list) |
| `.github/workflows/ci.yml` | ensure the `backend` job's ruff invocation actually reaches `tools/` |

**Acceptance**

1. `ruff check` from CI's working directory reports the `tools/` files as checked — verify by re-introducing
   an over-long line and seeing it fail.
2. `ruff format --check` covers them too.
3. `tools/make_golden.py` and `tools/gen_protocol_docs.py` both pass with no suppressions.
4. The pytest and Godot suites are unaffected.

One ruff configuration governs both trees; do not create a second settings block that can drift from the
first.
