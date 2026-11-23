# T-147 — Vectorised `curvature_side` (F46)

**Milestone:** P1.M3 (a small backend fix in the run order, after T-137 A) · **Depends on:** T-137 A
committed · **Blocks:** nothing, but T-135 B and every later capture of a cant-bearing alignment run faster

## Context

`domain/lrs.py: frames()` calls `curvature_side(alignment, s)` whenever any station has cant above 0. Cant
ramps that run onto a straight need to know which side is "inside", and the nearest curve tells them.

For every station on a straight, `curvature_side` walks outwards in 5 m steps up to 250 m: first `s + d`,
then `s − d`. It stops at the first candidate whose curvature is at least `CURVATURE_EPS`. Each candidate is
a scalar `h.curvature(cand)` call from a Python loop, up to 100 of them per station.

The T-137 A review measured `frames()` on the Kralupy alignment with a cant ramp added:

- 2.4 s for 1,820 stations;
- 22 s for 18,186 stations.

Kralupy itself has no cant, so no test or golden sees this. COYPU-optimised alignments carry COYPU's cant
(T-129), and every `frames()` caller pays the cost:

- `alignment.frame_table` (`domain/sampling.py`);
- the track mesh baker (`io/mesh/track.py`);
- the trainset chain (`domain/kinematics/trainset.py`);
- `to_xyz`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/domain/lrs.py` | `curvature_side` vectorised; same signature, same results |
| `backend/tests/test_lrs_curvature_side.py` | new — the reference comparison, edge cases and the timing check |

Nothing else changes. `frames()`, its callers, the protocol and the goldens stay as they are.

## Contract

- **Same signature:** `curvature_side(alignment, s, search_m=250.0, step_m=5.0) -> np.ndarray`.
- **Same result, element for element,** as the current loop, for every input. That includes:
  - the order of candidates: `+step`, `−step`, `+2·step`, `−2·step` and so on, so on a tie the forward
    candidate wins;
  - candidates outside `[h.stations[0], h.stations[-1]]` are skipped, not clamped;
  - 0 when no curved candidate lies within `search_m`;
  - stations that are already curved keep `sign(κ)`.
- **Method:**
  1. Build the candidate matrix for the flat stations only: one row per flat station, one column per
     candidate in the order above.
  2. Evaluate `h.curvature` on it in one vectorised call per chunk. It takes a 1-D array, so ravel the
     chunk and reshape the result. Chunk the rows so that no chunk holds more than about 200,000
     candidates. The review measured 2 million as twice as slow and 5 times the memory on Kralupy (61 MB
     against 12 MB), because the smaller chunk stays in cache.
  3. Mask the out-of-range candidates, take the first curved column of each row with `argmax` on the
     boolean mask, and read its sign. A row with no curved column gives 0.
- **Pure numpy,** like the rest of `domain/`.

## Acceptance criteria

1. **`test_matches_the_reference_loop`.** Keep the current loop verbatim in the test file as
   `_reference_curvature_side`. Compare it with the new function, exactly with `array_equal`, on:
   - the Kralupy alignment at 1 m, with a cant profile added;
   - a synthetic alignment with a straight between a left-hand and a right-hand curve, with stations on
     the straight's exact midpoint (the tie) and at every 0.5 m across it;
   - stations within 250 m of either end of the alignment;
   - a straight longer than 500 m, whose middle stations have no curve in range and so give 0;
   - non-default `search_m` and `step_m`.
2. **`test_frames_unchanged_with_cant`.** On the same cant-bearing alignments, `frames()` output equals
   what the reference loop gives: roll, `left`, `up` and, with `LOW_RAIL`, `origin`. Use `array_equal`.
3. **`test_curvature_side_is_fast`.** `frames()` for 18,000 stations of the cant-bearing Kralupy alignment
   takes under 1 s. Take the best of 3 runs, so one slow scheduling slice cannot fail it (the F45 lesson).
   Report the measured time.
4. **Goldens unchanged.** `tools/make_golden.py` regenerates `shared/golden/*.json` byte-identical:
   `git status` shows no change under `shared/`. Do not commit a regenerated golden.
5. **No regressions:** `ruff check`, `ruff format --check`, `pytest -q` and `gen_protocol_docs.py --check`
   are green. The client is untouched.

## Out of scope

- Any change to cant semantics or to `frames()`'s outputs.
- Caching frames between calls.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
uv run python ../tools/make_golden.py && git status --short ../shared
```

## Report back

- the vectorised method and its chunk size;
- the timings before and after, for 1,820 and 18,186 stations;
- confirmation that the goldens regenerate byte-identical.
