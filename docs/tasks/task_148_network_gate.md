# T-148 — Streamed imagery outside the CPU gate (F50)

**Milestone:** P1.M3 (a small backend fix in the run order, before T-136 C) · **Depends on:** T-134 B
committed (`8c656a0`) · **Blocks:** T-136 C, which puts the online orthophoto in front of users

## Context

ADR 0003's addendum gives each connection one gate, `session.gate`. It holds 4 slots, and every off-loop job
passes through it: terrain bakes, the relief ramp, imagery compositions, imports and inspections.

`server/handlers.py: _compose_into` holds a slot for the whole of `_compose_blocking`. When the stack holds
the streamed ČÚZK orthophoto (`io/imagery/cuzk_wmts.py: WmtsLayer`), that includes the network fetches. Each
composition has a render deadline of 15 s.

If ČÚZK is slow without failing, four compositions can therefore hold all four slots for up to 15 s each.
The circuit breaker does not help, because nothing fails. Meanwhile, `terrain.tile` bakes and every other
off-loop request wait behind them, so the terrain stops loading in the client while the imagery waits for
the network (F50, found in the T-134 B re-review).

**Decision (architect, 2026-11-23).** A composition whose stack holds a network-bound layer goes through a
second per-connection semaphore, `session.net_gate`, with 4 slots. Every other off-loop job keeps
`session.gate`. A slow service then delays only the streamed imagery.

Two alternatives were rejected:
- **A shorter deadline at fine levels.** It still holds the CPU gate, only for less time. It also degrades
  the imagery whenever the service is merely slow.
- **Separate fetch and compose phases.** Fetch the tiles outside every gate, then compose under
  `session.gate`. This is the right long-term shape, but it changes the `ImageryLayer` protocol. If it is
  ever needed, it goes to the roadmap.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/imagery.py` | `ImageryLayer`'s doc string names the optional attribute `network_bound: bool`; a layer without it is not network-bound |
| `backend/src/coypu_builder/io/imagery/cuzk_wmts.py` | `WmtsLayer.network_bound = True` (a class attribute) |
| `backend/src/coypu_builder/server/session.py` | `MAX_NETWORK = 4`; `Session.net_gate`, made like `gate` |
| `backend/src/coypu_builder/server/handlers.py` | `_gate_for(session, layers)`; `_compose_into` uses it |
| `tools/gen_protocol_docs.py` | the "Long requests" paragraph gains one sentence (below); regenerate `docs/protocol/ipc.md` |
| `docs/protocol/ipc.md` | regenerated, never hand-edited |
| `docs/adr/0003-ipc-protocol.md` | one paragraph under the addendum's **Sharing and limits** |
| `backend/tests/test_imagery_gate.py` | new — the tests below |

Nothing else changes. That covers the wire format, the `E_BUSY` accounting (`MAX_EXECUTING + MAX_PENDING`
coroutine requests in flight per connection), terrain bakes, the relief ramp and imports.

## Contract

- **`server/session.py`:**
  - `MAX_NETWORK = 4`, beside `MAX_EXECUTING`;
  - `net_gate: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_NETWORK),
    repr=False)`, with a one-line comment in the style of `gate`'s.
- **`server/handlers.py`:**
  - `def _gate_for(session: Session, layers) -> asyncio.Semaphore` returns `session.net_gate` when any layer
    in `layers` has `getattr(layer, "network_bound", False)` true. Otherwise, including `layers` being
    `None` or empty, it returns `session.gate`.
  - `_compose_into` takes `async with _gate_for(session, imagery.layers):` in place of
    `async with session.gate:`. Nothing else in it changes.
  - The whole stack decides, not just the layers that overlap the tile. A tile outside the orthophoto's clip
    also uses `net_gate`. That is harmless, because its render makes no request, and it keeps the rule to
    one line.
- **`ImageryLayer`:** no new method, and the `Protocol`'s members stay as they are. The attribute is optional
  and read with `getattr`, so local layers and test fakes need no change.
- **ADR 0003, under "Sharing and limits":** add this paragraph:

  > **Network-bound work (2026-11-23, F50).** An imagery composition whose layer stack holds a streamed
  > layer waits for the network, not the CPU. It passes through a second per-connection gate of 4 slots,
  > `session.net_gate`, so a slow service never holds the slots that terrain bakes and imports need. The
  > `E_BUSY` limit counts both kinds of request together.
- **`tools/gen_protocol_docs.py`:** after "One connection runs at most 4 such requests at a time and holds at
  most 64 more waiting;", the paragraph says that imagery tiles from a streamed service count against their
  own 4 slots instead. Keep it one sentence. Regenerate, and `--check` must pass.

## Acceptance criteria

Test file `backend/tests/test_imagery_gate.py`. Copy the pattern of
`tests/test_imagery.py: test_ping_answers_while_tiles_compose`. Reuse its helpers by importing them from
`tests/test_imagery.py`, without copying them:
- `server_url`, `_open`, `_import`, `_list` and `_send`;
- `write_terrain` and `write_bands`;
- its `TILE_W` and `TILE_N` constants.

The test project imports one synthetic terrain and one synthetic image. Two `monkeypatch` calls then turn
the image into a slow streamed layer:
- `handlers.layer_for_payload` is wrapped, so it returns a small wrapper with `network_bound = True`. The
  wrapper passes `source_id`, `resolution_m`, `bounds()` and `render()` through to the real layer.
- `handlers.compose_tile` is wrapped, so it blocks on a `threading.Event` before calling the original. It
  counts how many compositions are running at once, under a lock.

Check by reading the code that `layer_for_payload` and `compose_tile` are looked up through `handlers` at
call time. Otherwise patch them where they are looked up, and say so in the report.

For criterion 1, the `terrain.tile` request must be for a tile that no request has asked for before, so that
it needs a bake under `session.gate`. A cached answer proves nothing. Check in `handle_terrain_tile` that it
bakes through `session.gate`, and report the line.

1. **`test_slow_streamed_imagery_does_not_hold_the_cpu_gate`.**
   - Send 6 `imagery.tile` requests for distinct tiles and wait until 4 renders are blocked.
   - Then a `terrain.tile` request for a tile that has not been baked is answered within 2 s, while all 4
     renders are still blocked.
   - Release the event: all 6 imagery replies arrive.
2. **`test_streamed_compositions_are_bounded_by_the_network_gate`.** With the same fake and 8 requests, at
   most 4 renders run at once. All 8 replies arrive after the release.
3. **`test_gate_for_picks_the_network_gate_only_for_network_bound_layers`.** A unit test of `_gate_for`:
   - `None` and `()` give `session.gate`;
   - local layers only give `session.gate`;
   - a stack with one network-bound layer among local ones gives `session.net_gate`.
4. **`test_wmts_layer_is_network_bound`.** `WmtsLayer.network_bound is True`. No network access: build the
   layer from a `WmtsSource` the way `test_imagery_providers.py` builds one.
5. **Mutation check.** Revert `_compose_into` to `session.gate` and run the file: criterion 1 must fail. Then
   restore it with the Edit tool. Report the failure line.
6. **No regressions:** `ruff check`, `ruff format --check`, `pytest -q` (886 passed before this task) and
   `gen_protocol_docs.py --check` are all green. The client is untouched.

Follow the F45 rule for every timing assertion: test the property with a generous bound, never a tight
maximum. Use `threading.Event` and polling with a deadline, never `sleep` for fixed times. Release the event
in a `finally`, so a failing test cannot hang the suite.

## Out of scope

- Splitting fetch and compose, as described under Context.
- Per-host limits beyond the existing `TILE_CONCURRENCY` slots in `cuzk_wmts.py`.
- Client changes. T-135 C's retry of incomplete imagery is unaffected.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
uv run pytest tests/test_imagery_gate.py -v
```

## Report back

- the gate rule as implemented, and every file changed;
- the timing that test 1 measured for the terrain reply;
- the mutation check's failure line;
- the pytest totals.
