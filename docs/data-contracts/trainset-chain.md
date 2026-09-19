# Trainset chain (T-112)

Poses every car and bogie of a `Trainset` along an `Alignment` for one lead station. This is the algorithm
T-123 re-implements client-side (ADR 0007: per-frame evaluation happens in Godot, from baked tables — this
module is never on the runtime path). The backend reference lives at
`backend/src/coypu_builder/domain/kinematics/trainset.py`; the pinned numeric reference is
`shared/golden/trainset_chain.json`. Read `docs/data-contracts/coordinate-conventions.md` first for the
station/frame/Godot-axis conventions this document builds on.

## Inputs

- **Alignment**: horizontal + vertical + cant, one absolute station axis (metres), `[station_start,
  station_end]`.
- **Trainset**: `cars` — a tuple of car specs, **front to back** — each with `length_m`,
  `bogie_pivot_distance_m` (pivot-to-pivot distance, may be `0`), and `coupling_gap_m` (one value, reused
  between every adjacent pair).
- **station_lead**: the station of the **front face of car 0**.
- **direction** `d ∈ {+1, −1}`: which way the whole (rigid) consist faces. `+1` = car 0's front face is the
  nose, facing increasing station. `−1` = car 0's front face is the nose, facing decreasing station.
- **pivot**: the `RotationPivot` convention forwarded to `lrs.frames` (defaults to the alignment's own cant
  pivot).

## 1. Station layout

Walk the consist from car 0 backwards (toward the tail), using only station arithmetic — arc length along
the alignment, never chord distance:

```
car 0 front face      f_0 = station_lead

car i rear face        r_i      = f_i − d · length_i
car i lead-bogie pivot p_i,lead  = f_i − d · (length_i − pivot_distance_i) / 2
car i trail-bogie pivot p_i,trail = p_i,lead − d · pivot_distance_i
car i+1 front face      f_{i+1}  = r_i − d · coupling_gap
```

`p_i,lead` is always the pivot inset from car *i*'s own front face (by `(length_i − pivot_distance_i)/2`);
`p_i,trail` is the one inset from its rear face. This holds for both signs of `d` — which physical end of
the alignment ends up "front" flips with `d`, but the lead/trail *labels*, relative to each car's own front
face, do not.

**Clamping.** Any of `f_i`, `r_i`, `p_i,lead`, `p_i,trail` may fall outside `[station_start, station_end]`
(the lead station is close to either end, or the consist is longer than the modelled alignment). Clip every
one of them into `[station_start, station_end]` before using it for anything else (frame lookup *or*
reporting) — this is what stops cars from silently overlapping past the alignment's end. Set one
`clamped` flag per `(station_lead, direction)` result to `true` iff **any** of those four values, for **any**
car, needed clipping. `clamped` communicates that the consist is partly off the modelled alignment; it does
not, by itself, make the pose look correct — pivots beyond the end pile up on the endpoint, by construction.

## 2. Bogie pose

For every (clamped) pivot station, look up one track frame (`lrs.frames`, or the client's per-frame
interpolation of the baked `FrameTable` — same contract, same values to table resolution). Evaluate **all**
pivot stations for a batch in one call; do not call the frame lookup once per bogie.

- `position` = frame `origin` (the track-plane centre — already includes the rotation-pivot convention,
  e.g. `LOW_RAIL`'s inset for cant).
- `orientation` = the frame basis itself, i.e. `(tangent, left, up)`. Bogies follow the track exactly,
  including cant roll and gradient pitch — there is no independent bogie-yaw or suspension model.
- `roll`, `pitch` = the frame's own values, passed through unchanged.

## 3. Car body pose

The body is rigid and spans its two bogie pivots. It **chords** between them — it does *not* follow the
tangent at its own midpoint. That is what makes a car visibly cut across a curve the way a real vehicle
does, instead of bending to follow the rail.

Let `P_lead`, `P_trail` be the two bogies' `position`s (from step 2) and `up_lead`, `up_trail`,
`roll_lead`, `roll_trail` their `up`/`roll`:

```
origin  = (P_lead + P_trail) / 2
forward = normalize(P_lead − P_trail)
roll    = (roll_lead + roll_trail) / 2
up_raw  = normalize(up_lead + up_trail)
up      = normalize(up_raw − (up_raw · forward) · forward)     # Gram-Schmidt against forward
left    = cross(up, forward)
```

Apply these **in this exact order** — mean roll is not used to construct `up`/`left` geometrically (it is
carried through only as a reported scalar); `up_raw` is normalized *before* the Gram-Schmidt step, not
after; `left` is derived from `up` and `forward`, never independently averaged. T-123's implementation is
pinned to this order because floating-point results depend on it, not just the closed-form math.

### On `forward`'s sign — read this before implementing

`forward` must already be signed by travel direction: for `direction = −1`, every body's `forward` points
toward *decreasing* station (the whole consist is physically turned around), even though — from step 1 —
the consist's *cars* still lay out toward *increasing* station as the index grows. Do **not** multiply
`normalize(P_lead − P_trail)` by `d` again. The sign dependency on `d` is already fully carried by which
pivot ends up "lead" vs "trail" (`p_lead − p_trail = d · pivot_distance`, from step 1) — re-multiplying by
`d` squares that sign back out and makes `forward` direction-independent, which is wrong (verified both
algebraically and against a synthetic straight: with the extra multiplication, `forward` comes out equal to
the frame tangent for **both** `direction = +1` and `direction = −1`). This was caught while building the
golden and is called out explicitly because a first reading of "`forward = normalize(P_lead − P_trail) · d`"
suggests the extra factor is needed; it is not, given how `P_lead`/`P_trail` are already assigned in step 1.

## 4. Degenerate case: zero pivot distance

A car with `bogie_pivot_distance_m == 0` (or one whose two pivots both land on the same clamped alignment
endpoint) has `P_lead == P_trail` — no chord, so step 3's `normalize(P_lead − P_trail)` is `0/0`. Detect
this by chord length (guard at a small epsilon, e.g. `1e-9 m`, not just by testing `pivot_distance_i == 0`,
so a clamp-induced collapse is also caught) and fall back to the single frame at that (shared) pivot
station — which, since `p_lead == p_trail` reduces to `f_i − d·length_i/2`, *is* the car's centre station:

```
forward = d · tangent        # frame tangent at the shared pivot station, signed by direction
roll    = roll               # that frame's own roll
up      = up                 # that frame's own up
left    = cross(up, forward)
```

Here the explicit `· d` **is** needed — unlike the two-pivot case, there is no chord to carry the sign, so it
must come from `direction` directly. (This is also the correct limit of the two-pivot formula as
`pivot_distance → 0⁺`.)

## 5. Worked example (sign convention sanity check)

Single car, `length_m = 10`, `bogie_pivot_distance_m = 6`, `station_lead = 100`:

| `direction` | `front face` | `rear face` | `lead pivot` | `trail pivot` | `forward` points toward |
|---|---|---|---|---|---|
| `+1` | 100 | 90 | 98 | 92 | increasing station (= tangent) |
| `−1` | 100 | 110 | 102 | 108 | decreasing station (= −tangent) |

For `direction = −1` with two cars (`coupling_gap_m = g`), car 1's front face is
`f_1 = r_0 − d·g = (f_0 + length_0) + g`, i.e. **larger** than `f_0` — the consist extends toward increasing
station even though every car's own nose still faces decreasing station.

## API (backend reference; T-123 mirrors this shape, not these names)

```python
def pose_trainset(alignment, trainset, station_lead: float, direction: int = 1, pivot=None) -> TrainsetPose: ...
def pose_trainset_many(alignment, trainset, station_lead: np.ndarray, direction: int = 1, pivot=None) -> tuple[TrainsetPose, ...]: ...
```

`TrainsetPose.cars[i]` carries `station_front`/`station_rear` (clamped, step 1), `lead`/`trail` (`BogiePose`,
step 2) and the body fields (step 3). All positions are float64 `(E, N, H)` in the project CRS — the Godot
mapping (`points_to_godot`/`basis_to_godot`/`quaternion_from_matrix`) is applied only when writing the
golden or the wire protocol, never inside this algorithm.

## Golden: `shared/golden/trainset_chain.json`

```
{
  "source": "kralupy_neratovice_092.xml", "crs": "EPSG:5514", "base_point": {...},
  "trainset": { "spec_key", "name", "mode", "gauge_mm", "coupling_gap_m", "cars": [...] },
  "samples": [
    { "station_lead": <float>, "direction": ±1, "clamped": <bool>,
      "cars": [
        { "index": 0,
          "lead_pivot":  {"station": <float>, "godot_position": [x,y,z]},
          "trail_pivot": {"station": <float>, "godot_position": [x,y,z]},
          "body": {"godot_position": [x,y,z], "godot_quaternion_xyzw": [x,y,z,w], "roll": <float>} },
        ...
      ]
    }, ...
  ]
}
```

`trainset` is the consist actually used (a 3× `dmu_br650_cd840` set, 3 identical cars, so the client needs
no catalogue lookup to check the golden). The body quaternion is built from `(forward, left, up)` exactly as
`basis_to_godot` builds one from `(tangent, left, up)` elsewhere — a car body's `forward` *is* its effective
tangent for orientation purposes. Per-bogie orientation is not included in the golden: it is exactly the
alignment's own frame basis at `lead_pivot.station`/`trail_pivot.station`, already covered by
`shared/golden/frame_eval.json`.

### The nine samples, and why each was chosen

| station_lead | direction | regime |
|---|---|---|
| 100.0 | +1 | start of the route |
| 8260.0 | +1 | long straight (6615–9905 m), clear of any transition |
| 16850.0 | +1 | sharpest curve on the file, R ≈ 300 m |
| 16790.0 | +1 | clothoid ramping into that same sharpest curve |
| 3850.0 | +1 | consist straddles the arc→clothoid→line boundary near 3801.56 m |
| 16034.09 | +1 | nominal cant-ramp station — **see limitation below** |
| 2860.255557 | +1 | vertical curve PVI, horizontally flat (isolates the pitch/gradient effect) |
| 18199.971666 | +1 | 15 m past the alignment end — exercises clamping (`clamped: true`) |
| 8260.0 | −1 | the long straight again, reversed — pins the `direction` sign from §3 |

**No real cant ramp in this block.** The Kralupy LandXML's cant block is a zero placeholder
(`backend/tests/test_lrs_golden.py::test_cant_block_is_zero_placeholder_with_feeder_units` asserts
`max(cant_mm) == 0`), so every sample above has `roll == 0`. The 16034.09 m sample sits inside a clothoid
purely to represent where a cant ramp *would* be exercised in a file that had one; it proves nothing about
mean-roll averaging or the Gram-Schmidt correction. **`tram_block` (below) is what actually pins that
behaviour** — do not treat this Kralupy block as covering roll handling at all.

## Golden: `tram_block` (Follow-up F10)

A second, clearly-separated group inside the *same* `trainset_chain.json`, added as one more top-level key
alongside `source`/`crs`/`base_point`/`trainset`/`samples` above — purely additive, so every byte of the
Kralupy block is unaffected. **This is the block T-123's roll handling is actually measured against**: mean
`roll` averaging (`(roll_lead + roll_trail) / 2`) and the Gram-Schmidt correction of `up` are no-ops on every
Kralupy sample (zero cant throughout) and would pass a broken implementation silently. `tram_block` uses the
synthetic tram loop (`tests/fixtures/synthetic/tram_loop.py:build_tram_loop`, imported directly rather than
re-implemented — see the note in that function's docstring), which has two real, hand-authored cant ramps.

```
"tram_block": {
  "source": "tests/fixtures/synthetic/tram_loop.py:build_tram_loop",
  "crs": null,                              // arbitrary local metres, not a real projected CRS
  "base_point": {...},                      // easting/northing 0, height = tram_loop.py's ELEVATION_M
  "trainset": { ... a 2-car `tram_generic` consist (modules A + B), inline as above ... },
  "samples": [
    { "alignment": "street" | "loop", "station_lead": <float>, "direction": 1, "clamped": <bool>,
      "cars": [
        { "index": 0,
          "lead_pivot":  {"station": <float>, "roll": <float>, "godot_position": [x,y,z]},
          "trail_pivot": {"station": <float>, "roll": <float>, "godot_position": [x,y,z]},
          "body": {"godot_position": [x,y,z], "godot_quaternion_xyzw": [x,y,z,w], "roll": <float>} },
        ...
      ]
    }, ...
  ]
}
```

Two differences from the Kralupy block's schema, both because this block spans two separate `Alignment`s
(street + the terminal loop) rather than one: each sample names its `alignment`, and each pivot carries its
own `roll` directly (not just `station`/`godot_position`) so a non-zero-roll claim can be checked from the
JSON alone, without recomputing frames.

### The five samples, their pivot rolls, and what each pins

| alignment | station_lead | car | lead.roll | trail.roll | body.roll | what it pins |
|---|---|---|---|---|---|---|
| street | 20.0 | both | 0.0 | 0.0 | 0.0 | **control** — zero cant, zero curvature |
| street | 65.0 | 0 | −0.013637 | −0.000000 | −0.006818 | **mid cant-ramp (ascending)** — pivots straddle the 60–70 m ramp; pins mean-roll averaging (13.6 mrad apart, ≫ 1e-4) |
| street | 80.0 | 0 | −0.036372 | −0.036372 | −0.036372 | **full cant inside the arc** — both pivots equal; closes F10's "every roll is zero" finding directly |
| street | 95.0 | 0 | −0.022729 | −0.036372 | −0.029550 | **mid cant-ramp (descending)** — mirrors the 65 m sample (13.6 mrad apart), redundancy for the "≥4 non-zero samples" criterion |
| loop | 25.0 | 0 | −0.018751 | −0.000000 | −0.009376 | a second cant ramp at the loop's tighter 30 m radius (vs. the street's 120 m) |

Every sample carries two cars; only the car that lands inside the interesting regime is shown above. Car 1
sits further back in the consist and, in every ramp sample, is either still short of the ramp (roll `0.0`,
at 65.0 and 25.0) or already fully past it and sitting at the local plateau roll (`−0.036372`, at 95.0) — see
the golden itself for every car's full numbers.

### Why there is no literal "cant + gradient" sample, and what stands in for it

F10's contract asked for "a station where cant and a non-zero gradient coexist, so the Gram-Schmidt
correction against `forward` is not a no-op." `tram_loop.py`'s vertical alignment is `VerticalAlignment.constant`
on **both** the street and the loop — exactly flat, gradient ≡ 0 everywhere in this fixture. No such sample
exists to pick, and adding one would mean hand-editing `tram_loop.py`, which is out of scope here and would
be exactly the kind of drift-prone duplication F10 explicitly rules out.

Investigating *why* the correction matters revealed the actual mechanism, which is more precise than "cant
and gradient both present": on a span of **constant** curvature and **constant** cant (a plain circular arc,
same roll at both pivots), the lead/trail geometry is symmetric enough that `up_raw` comes out *exactly*
perpendicular to `forward` already — the Gram-Schmidt projection removed is `< 1e-16` (float noise), a
mathematical no-op, independent of how large the constant cant is. This is verified in the golden itself:
the 80.0 m sample (full, constant cant) has `lead.roll == trail.roll` to the printed precision. What actually
breaks that symmetry, and makes the correction non-trivial, is a **changing** roll or curvature between the
two pivots — i.e. exactly the ramp samples (65.0, 95.0, 25.0), where the projected-out component is
genuinely non-zero (≈0.05–0.06° between the raw and corrected `up`, measured directly, not float noise). A
constant non-zero gradient alone would likely have been just as symmetric and just as much of a no-op; a
*changing* gradient (inside a vertical curve) would not — but that combination does not exist in this
fixture either. `docs/tasks/task_112_trainset_chain.md`'s Follow-up F10 should be read with this correction:
the ramp samples are the proof, not a stand-in for a still-missing case.
