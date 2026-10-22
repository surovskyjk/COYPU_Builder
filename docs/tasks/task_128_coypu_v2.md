# T-128 — `.coypu` archive format 2: compatibility and the new line and stop data

**Milestone:** P1 (M2 follow-up) · **Depends on:** T-133 committed (shared `server/handlers.py`) · **Blocks:** —

## Context

COYPU 0.4 (2026-10-20) saves projects as **format version 2** and still loads version 1. Builder reads
`.coypu` archives in `backend/src/coypu_builder/io/coypu/archive.py` (M2, T-112 to T-114). That reader:
- checks no version at all;
- reads only `project.json` and `assets/*.xml`.

Every key it reads today is still written by version 2, so a v2 archive should import as before. This task
**proves** that with tests, closes the version check, and reads what version 2 adds that Builder can use:
- the line number, name and category;
- stop records with stop ids and stopping-pattern membership (`Os`, `Sp`, `Ex`);
- the run direction;
- the optional `karto/dataset.json` entry.

**Version 2 adds:**
- **`projectMetadata`:** `lineNumber`, `lineName`, `lineCategory` (`""`, `"regional"` or `"national"`).
- **`stopsData`:**
  - `stopRecords`: a list of `{stopId, stopName, stationingKm, dwellTimeS, municipalityName, population,
    stopInOs, stopInSp, stopInEx, stopInSpIsManual, stopInExIsManual}`, numpy-encoded like other values;
  - `activeStopPattern` (default `"Os"`);
  - `spPopulationThreshold`, `isSpThresholdAuto`;
  - the legacy `trainStops` is still written.
- **Vehicle settings:** each vehicle may carry `directionLabels {forward, reverse}`. `runReversed` exists at
  the settings level and per vehicle; it predates version 2.
- **Optional entry `karto/dataset.json`**, with `schemaVersion: 1`. It holds:
  - a header (`application`, `units`);
  - `line`, `crs` (source and `EPSG:4326`), `centerline` and `chainageMap`;
  - `keyPoints`, `stops` (with coordinates), `stopPatterns`;
  - `speedProfiles`, `vehicles`, `travelTimes`.

  COYPU writes this entry only when it could build it, and never reads it back.

## Preconditions

- Read `docs/data-contracts/coypu-kinematics.md` and `io/coypu/archive.py`, `io/coypu/vehicles.py`, and
  `server/handlers.py: _read_coypu_bundle`, `handle_import_coypu`, `_stops_from_coypu`.
- The COYPU source is a read-only reference outside this repository; the architect gives its location in your
  order. **Read it, never edit it, and copy nothing from it into this repository except key names and
  semantics.**
  - In particular, no design-standard lists, no vendor or operator names, no references to other imported
    file types, and no sample data.
  - Fixtures are synthesised by the tests.

## Survey first

Before writing code, answer these from the COYPU source (`project_file.py`, `stops_model.py`,
`simulation_runner.py`, `karto_export.py`) and put the answers in your report:

1. Which stops were the stored kinematics (`calculationCache.dataStorage`, `kinematics*_{i}`) simulated with:
   the records of `activeStopPattern`, or `trainStops`? Does `ensureStopSettings` keep `trainStops` in step
   with the active pattern?
2. Is the run direction of the stored kinematics `settingsData.runReversed`, and is the `station` array then
   descending?
3. Are the per-direction results of "every simulation runs both directions" persisted in the archive, and
   under which keys? Report only. A later task may import the second direction as a second run.
4. When an alignment is optimised (`chainageMapBaselineKm` / `chainageMapActiveKm`), is the embedded
   `assets/*.xml` the baseline or the optimised geometry? Is the stored kinematics' station on that file's
   chainage or on the active one? Report only. If they can disagree, say exactly how, with the code lines.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/coypu/archive.py` | version check, entry size guards, the new fields |
| `backend/src/coypu_builder/domain/kinematics/run.py` | `Stop` gains `stop_id` and `patterns` |
| `backend/src/coypu_builder/server/handlers.py` | stops from records; `format_version` and `line` in the result |
| `backend/src/coypu_builder/protocol/messages.py` | `StopDTO`, `ImportCoypuResult`, new `CoypuLineDTO` |
| `docs/protocol/ipc.md` | regenerate |
| `docs/data-contracts/coypu-kinematics.md` | a section "Archive format versions" with the subset Builder reads and the survey answers |
| `backend/tests/test_coypu_v2.py` | new |

## Contract

**Version check** in `read_coypu`:
- `formatVersion` must be an `int` that is not a `bool`, and at least 1. Otherwise raise
  `ValueError("not a COYPU project file")`. `handle_import_coypu` maps it to `E_BAD_PARAMS`.
- 1 and 2 are read silently (`SUPPORTED_FORMAT_VERSIONS = (1, 2)`).
- A higher version is read as far as possible, with the warning
  `"written by a newer COYPU (format N); only the parts this version knows are read"`.

**Entry size guards.** Check `ZipInfo.file_size` before reading anything:
- `project.json` at most 256 MB;
- `karto/dataset.json` at most 64 MB;
- all `assets/*.xml` together at most 256 MB.

Beyond a limit, refuse with `ValueError` naming the entry and its size. Read only those three kinds of entry.

**New fields on `CoypuProject`:**

```python
@dataclass(frozen=True)
class CoypuLine:
    number: str = ""
    name: str = ""
    category: str = ""            # "", "regional" or "national"; anything else is kept and warned about

@dataclass(frozen=True)
class CoypuStopRecord:
    stop_id: str
    name: str
    station_km: float
    dwell_s: float
    municipality: str
    population: int | None
    patterns: tuple[str, ...]     # subset of ("Os", "Sp", "Ex"), in that order

# on CoypuProject (all with defaults, so version 1 archives fill them empty):
line: CoypuLine
stop_records: tuple[CoypuStopRecord, ...]
active_stop_pattern: str          # "Os" when absent
run_reversed: bool                # settingsData.runReversed, False when absent
direction_labels: tuple[dict[str, str], ...]   # per vehicle {"forward": ..., "reverse": ...}, {} when absent
karto: dict | None                # parsed karto/dataset.json when present with schemaVersion 1, else None
warnings: tuple[str, ...]
```

`karto/dataset.json`:
- parse it with `json.loads`, and reject `NaN` (`parse_constant` raises);
- a malformed entry or a `schemaVersion` other than 1 gives `karto = None` and a warning, never an error;
- nothing on the wire uses `karto` in this task. It is a seam for later.

**Stops:**
- `Stop` gains `stop_id: str = ""` and `patterns: tuple[str, ...] = ()`. Both have defaults, so every
  existing construction still works.
- **When `stop_records` is not empty:**
  - `project.stops` holds every record, with its patterns;
  - each run's `stops` are the records of the active pattern, in station order — if the survey (question 1)
    says the kinematics used them; otherwise use what the survey found, and say so.
- **Otherwise** both come from `trainStops`, exactly as today.
- Stations convert km to m as `_stops_from_coypu` does now.

**Wire** (additive only; regenerate `ipc.md`):
- `StopDTO` gains `stop_id: str = ""` and `patterns: tuple[str, ...] = ()`.
- `ImportCoypuResult` gains:
  - `format_version: int`;
  - `line: CoypuLineDTO | None`, which is `None` when all three fields are empty;
  - `CoypuLineDTO {number, name, category}`.
- The archive's warnings are appended to `warnings`.

## Acceptance criteria

Every test is in `backend/tests/test_coypu_v2.py`. A helper there, `make_v2_archive(tmp_path, **changes)`,
copies the Kralupy fixture `backend/tests/fixtures/kralupy/kralupy_neratovice_092.coypu`. It then rewrites
`project.json` with version 2 keys and can add a `karto/dataset.json`. Synthesise the values, never copy
them from a real file.

1. **Version 1 is unchanged.** `test_v1_import_is_unchanged`: importing the Kralupy fixture gives the same
   runs, stations, times and stops as before this task, compared array for array, with every new field
   empty.
2. **Version 2 imports.** `test_v2_import_reads_line_stops_and_patterns`:
   - a synthesised v2 archive imports with the same runs;
   - `line` is filled;
   - the stops carry ids and patterns;
   - the run's stops are the active pattern's.
3. **Pattern switch.** `test_active_pattern_selects_the_run_stops`: with `activeStopPattern = "Ex"`, the run
   gets only the `Ex` stops.
4. **Version checks.** `test_bad_format_versions_are_refused`: a missing version, `0`, `true` and `"2"` are
   each refused with "not a COYPU project file". `test_newer_format_warns`: version 3 imports with the
   warning.
5. **Size guards.** `test_oversized_entries_are_refused`: monkeypatch the limits small, and each of the
   three entry kinds is refused with its name in the message.
6. **Karto.** `test_karto_dataset_is_parsed_or_warned`:
   - schema 1 is parsed;
   - schema 2, invalid JSON and a `NaN` each give `karto is None` plus a warning.
7. **Wire.** `test_import_coypu_result_carries_version_and_line`, over the live server as the existing
   `import.coypu` tests do.
8. **Live** (`COYPU_DATA` set, otherwise skipped with a reason).
   `test_real_coypu_samples_import`: every `${COYPU_DATA}/Coypu_Samples/*.coypu` imports without an error.
   Report each file's version, run count, stop count and warnings.
9. **No regressions:**
   - both suites are green;
   - no previously passing test is removed, skipped or weakened;
   - `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Importing the second direction's run, speed profiles or travel times — a later task, once the survey
  answers are in.
- Any client change: stops already reach the client through `StopDTO`, and the new fields are ignored until
  a UI task uses them.
- Writing `.coypu` files.

## Verification

```bash
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run pytest -q tests/test_coypu_v2.py -rs        # shows the live test's skip reason or its figures
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

- The four survey answers, with `file:line` references into the COYPU source.
- The live figures from criterion 8, or the skip reason.
- Every contract change, with its reason.
