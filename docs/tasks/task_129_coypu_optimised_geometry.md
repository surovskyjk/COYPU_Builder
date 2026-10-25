# T-129 — Optimised COYPU alignments: use the optimised geometry and the run simulated on it

**Milestone:** P1 (M2 follow-up) · **Depends on:** T-128 · **Blocks:** —

## Context

COYPU can optimise an alignment: it slews the horizontal geometry, and the chainage changes with it (F41). A
`.coypu` archive of an optimised project holds:

- `assets/*.xml`: the **original** LandXML, captured at import;
- `alignmentsData.landXml`: COYPU's parsed geometry, with the **optimised** values promoted onto the
  active keys. COYPU's own LandXML export (`landxml_exporter.py`) writes the optimised alignment from these
  per-type element arrays:
  - `line*`: `StartX/Y`, `EndX/Y`, `StationStart`;
  - `spiral*`: `StartX/Y`, `PIX/Y`, `EndX/Y`, `StationStart`, `Length`, `RadiusStart`, `RadiusEnd`, `Rot`,
    `Type`;
  - `curve*`: `StartX/Y`, `EndX/Y`, `CenterX/Y`, `StationStart`, `Rot`, `Radius`;
  - plus `stationHorizontal` and `geometryType`, two entries per element in chain order;
- `calculationCache.landXmlDerived`: the chainage map `chainageMapBaselineKm` / `chainageMapActiveKm`
  (baseline to active), and the calculated cant;
- the stored run, always simulated after the last optimisation, so it is on the active chainage (T-128
  survey, F4).

T-128 mitigated this by mapping the run back onto the original geometry. **The user decided on 2026-10-25:
"If the alignment was optimized, please use the optimized geometry and travel time for the builder."**
So for an optimised archive, Builder:
- rebuilds the alignment from the optimised element arrays;
- keeps the run on its own (active) chainage;
- re-stations everything else that was entered on the original chainage, the profile, the cant and the
  stops, onto the active chainage, as COYPU does.

COYPU's exporter invents placeholder vertical-curve radii (`buildVerticalCurvePlan`), so Builder does **not**
take the profile from COYPU's parsed `stationVertical` / `elevation`. It keeps the original file's true PVIs
and curves, re-stationed.

The non-optimised Kralupy fixture holds the same element arrays (17 lines, 32 spirals, 16 curves). So it
gives a strong test: rebuilding its alignment from the arrays must reproduce the alignment parsed from its
embedded LandXML.

## Preconditions

- T-128:
  - `io/coypu/archive.py`: `CoypuProject.landxml` holds the parsed geometry and `landxml_derived` holds
    the calculated results;
  - `chainage_map`, `_applicable_chainage` and `_active_to_baseline_m`;
  - the warnings.
- `io/landxml/reader.py`: `LandXmlAlignment`, `RawElement` (`points` are raw coordinate tokens `(t1, t2)`),
  `RawProfileItem`, `RawCant`, and `read_landxml`. `io/landxml/dialects.py` resolves tokens to E and N.
- `server/handlers.py: _read_coypu_bundle`, `_import_alignments`.
- The COYPU source is a read-only reference outside this repository; the architect gives its location in the
  order. The rules of T-128 apply: copy key names and semantics only, never data, and never write its path
  into the repository.

## Survey first (answer with `file:line` in the COYPU source)

1. **Token order.** For each element array (`lineStartX`, `lineStartY` and the rest), which token of the
   original LandXML `<Start>` / `<End>` / `<Center>` / `<PI>` text does X hold, and which does Y? Check this
   against the Kralupy fixture's embedded file and its arrays: compare values, not names.
2. **`denseAlignment`.** What is the layout of each entry (station in km? X? Y? anything else)? Which
   coordinate convention does it use?
3. **Cant.** Which cant did COYPU's simulation on an optimised alignment use: the imported cant re-stationed
   by `projectChainageArrays`, or the calculated `cantPossible`? Is the cant that Builder should show the same
   one?
4. **Spiral parameters.** Does the optimiser change `spiralType`, `spiralRot` or `curveRot`? Are
   `spiralConst` or `curveType` ever needed to rebuild an element?

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/coypu/geometry.py` | new — `optimised_alignment(...)` |
| `backend/src/coypu_builder/io/coypu/archive.py` | expose what `geometry.py` needs; the run is not mapped when the optimised geometry is used |
| `backend/src/coypu_builder/server/handlers.py` | `_read_coypu_bundle` uses the optimised alignment when there is one |
| `docs/data-contracts/coypu-kinematics.md` | the rule, the gates and the survey answers |
| `backend/tests/test_coypu_optimised.py` | new |

## Contract

```python
def optimised_alignment(project: CoypuProject, original: LandXmlAlignment) -> tuple[LandXmlAlignment | None, tuple[str, ...]]:
    """The optimised alignment rebuilt from the archive's element arrays, with the original file's profile and
    cant re-stationed onto it, plus warnings. Returns (None, warnings) when the archive is not optimised, or
    when a gate below fails, and the warnings then name the gate."""
```

**When it applies:** `project.chainage_map` is present and is not the identity (T-128's rule), **and** the
element arrays are present.

**Building `elements`:**
- element `i` has type `geometryType[2i]` (`Line`, `Spiral` or `Curve`). Its station range is
  `stationHorizontal[2i]` to `stationHorizontal[2i + 1]`, in km;
- a per-type counter indexes the per-type arrays, as `landxml_exporter.buildElementTable` does;
- each `RawElement` gets:
  - `kind`;
  - `sta_start = stationHorizontal[2i] × 1000`;
  - `length`: `spiralLength` for a spiral. A line or a curve gets no length, so the chord or the swept arc
    applies, as in the original file. Decided after the first implementation: the station span put a
    curve 0.36 mm off on Kralupy;
  - `points`, from the arrays, in the token order the survey (question 1) found, so they mean the same as
    in the original file;
  - `attrs`: `radius`, `rot`, `radiusStart`, `radiusEnd` and `spiType`, as strings, exactly as
    `read_landxml` would produce them from a file.

**The rest of the `LandXmlAlignment`:**
- `name`, `epsg`, `application_name`, `application_version` and `dialect` come from `original`;
- `sta_start = stationHorizontal[0] × 1000`;
- `length` is the station span of the whole alignment.

**Profile and cant**, from `original`:
- every station is mapped baseline to active with `np.interp(station_km, baseline_km, active_km)`;
- beyond the map's ends a station keeps the shift of the nearest end, the mirror of T-128's
  `_active_to_baseline_m`;
- PVI elevations and vertical-curve lengths and radii are unchanged;
- **the cant is COYPU's calculated cant.** The survey found the simulation on an optimised alignment used
  `cantPossible`, and the export prefers it too. Take it from
  `landxml_derived["stationCantPossible"]` / `["cantPossible"]`, which are already on the active
  chainage. Gauge and rotation point come from the original file. When it is absent, use the original
  cant, re-stationed.

**Gates.** Any failure means: return `None`, so T-128's map-back path is used, with the warning
`"optimised geometry not used: <gate>; the run was mapped back to the original geometry"`. The gates are:
1. every per-type array exists, and its length equals the number of elements of that type;
2. the end point of element `i` equals the start point of element `i + 1` within 1 mm, and the stations
   chain within 1 mm;
3. after conversion, every `denseAlignment` point, as `(station km, latitude, longitude)` in WGS 84,
   lies within **20 mm** of the rebuilt axis, measured as the distance to the axis, and the largest
   deviation is reported. A distance is used rather than a station match, because COYPU writes every
   sample of a curve with the curve's start station (F43). The limit is 20 mm, not 5 mm, because the
   pyproj round trip alone gives 1.8 mm on Kralupy, while a wrongly built element is metres off;
4. the archive embeds exactly one alignment. COYPU merges several LandXML sources into one parsed
   geometry, so with several embedded alignments the arrays cannot be matched to one of them.

**When it is used:**
- `_read_coypu_bundle` imports the optimised alignment in place of the embedded one;
- the run keeps its stored stations, with **no** mapping back;
- stop records and `trainStops` are mapped baseline to active, with the same `np.interp`. If
  `calculationCache.dataStorage.simulatedTrainStops` exists, the mapped stops must match it within 1 mm,
  and otherwise there is a warning;
- the warning reads:
  `"COYPU optimised this alignment: Builder uses the optimised geometry from the archive ({n} elements; largest deviation from COYPU's dense axis {d:.1f} mm), with the original file's profile re-stationed onto it and {cant}"`.
  `{cant}` is `"COYPU's calculated cant"`, or `"the original cant re-stationed onto it"` when no
  calculated cant is stored;
- T-128's map-back warning does not appear.

**Archives that are not optimised** are bit-identical to T-128's result.

## Acceptance criteria

All tests are in `backend/tests/test_coypu_optimised.py`. Synthetic archives come from T-128's
`make_v2_archive` (import it, or move it into a shared test helper module).

1. **`test_kralupy_elements_rebuild_the_embedded_alignment`.** On the non-optimised Kralupy fixture,
   `optimised_alignment`'s element builder (called directly, gates included) gives a `LandXmlAlignment`.
   Converted, it equals the one from the embedded file:
   - key-point stations within 1e-6 m;
   - positions within 1e-6 m at 1 m sampling over the whole alignment;
   - the same dialect.
2. **`test_an_optimised_archive_uses_the_optimised_geometry`.** A synthetic optimised archive, built from
   Kralupy:
   - translate every element coordinate and every `denseAlignment` point by 0.5 m in one axis;
   - add 2.0 m to every station of `stationHorizontal` and of the element station starts;
   - set the chainage map to baseline + 0.002 km.

   Then:
   - the imported alignment is the translated one;
   - the run's stations are the stored ones, unmapped;
   - the stops are mapped by +2.0 m;
   - the profile stations are mapped by +2.0 m;
   - the warning reports the deviation.
3. **Gates.** Each gate fails once and falls back with its warning, and the run is then mapped back as in
   T-128:
   - `test_missing_arrays_fall_back`;
   - `test_a_continuity_gap_falls_back`;
   - `test_a_dense_axis_mismatch_falls_back`.
4. **`test_non_optimised_archives_are_unchanged`.** The Kralupy fixture and T-128's synthetic version 2
   archive give results bit-identical to T-128's.
5. **`test_import_coypu_uses_the_optimised_alignment_over_the_wire`.** The alignment summary's
   `station_start` and `length` are the optimised ones, and the warning is present.
6. **No regressions:**
   - both suites are green;
   - the goldens are unchanged;
   - no previously passing test is removed, skipped or weakened;
   - `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Importing the original alignment as a second, reference alignment to show the slew. That is a later idea
  for the roadmap.
- Writing LandXML.
- Any client change.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run pytest -q tests/test_coypu_optimised.py tests/test_coypu_v2.py -rs
uv run pytest -q tests/test_lrs_golden.py
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

- The four survey answers, with `file:line`.
- The largest deviation from COYPU's dense axis on the Kralupy fixture, where it is identical by
  construction, and on the synthetic optimised archive.
- Every contract change, with its reason.
