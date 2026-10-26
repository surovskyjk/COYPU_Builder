# T-136 — Import dialog: inspect, confirm settings, import

**Milestone:** P1.M3 · **Depends on:** T-130, T-131, T-133, T-134, T-140 · **Blocks:** T-145 (sources saved
with a project)

## Context

ADR 0009 makes every import a three-step conversation: **inspect, confirm settings, import.** A file that
declares no coordinate system is refused rather than guessed, and every object gets a GUID that survives
re-import. The backend has implemented all of this (T-130, T-131, T-133, T-134).

This task gives the user the front end: an **Import terrain or imagery…** command in the ribbon's Import
group, which opens a dialog. In it the user can:

- pick files or an online source;
- see what the inspection found;
- correct the settings;
- import, with progress;
- read the result.

The user's own request when M3 was scoped (2026-10-06) was "selecting EPSG while importing … among other
useful settings important for the clean import". This dialog is where that becomes real.

## How this task runs

Three parts, each its own worker order:

| Part | What | Can start when |
|---|---|---|
| **A — backend** | `crs.search` and `import.formats` | T-128 is committed |
| **B — dialog for files** | the CRS picker and the five-step dialog for local files | Part A and T-135 Part A are committed |
| **C — online sources and polish** | providers, re-import, the live test, captures in both presets | Part B and T-134 Part B are committed |

## Preconditions

- T-130/T-131:
  - `import.inspect {path|paths|provider+query, format_id?, kind?}` returns:
    - format, kind, `declared_crs`, extent;
    - counts, for example `sample_class_<N>` for LAS;
    - `suggested_settings` (an `ImportSettingsDTO`);
    - warnings, with a resolution note for rasters;
  - `import.run {…, kind, settings, name?, reimport_of?}` returns:
    - `source_id`, `effective_crs`, `transformation`, `transformation_accuracy_m`;
    - the added / changed / unchanged / removed counts;
    - warnings;
  - `source.list`, `source.get`, `plugins.list`;
  - `E_CRS_REQUIRED`, `E_BAD_PARAMS`, `E_BUSY` and `E_INTERNAL`.
- T-133/T-134 providers:
  - `cuzk-dmr5g`, `cuzk-dmr4g` and `copernicus-glo30`;
  - `cuzk-ortho-wmts` (streaming) and `cuzk-ortho-sheets` (bulk, size guard);
  - their queries, default clips, licences, attributions and `notices`.
- T-140:
  - `UiShell`, `Ribbon` (an Import group slot, where commands appear only when they work), dialog patterns
    (`EnvelopeExportDialog`), `show_message()`, `UserSettings`;
  - the Engineering and Sandbox themes;
  - the ADR 0010 file rules in the export dialog.
- ADR 0003 addendum: imports run off the event loop. The client awaits each reply, sees `E_BUSY` as "try
  again in a moment", and gets per-connection ordering.

Read before starting:
- `docs/adr/0009-context-data-import.md`;
- `docs/data-contracts/import-framework.md`, `terrain-payloads.md`, `terrain-providers.md` and
  `imagery-tiles.md`.

**Code to copy the patterns from:**

| You write | Copy the pattern of |
|---|---|
| `crs.search`, `import.formats` handlers and messages | `server/handlers.py: handle_plugins_list`; `protocol/messages.py: PluginsListResult`, the `METHODS` entries |
| `ImportDialog` (the `requester` seam, busy state, error sentence, theme) | `client/ui/dialogs/envelope_export_dialog.gd` |
| the dialog's unit tests with a fake requester | `client/tests/unit/test_envelope_export.gd` |
| the live dialog test | `client/tests/integration/test_envelope_export_live.gd` |
| capture shots of a dialog | `client/tools/capture/scenarios/ui_shell.json` (its `dialog` key) |

## Part A — backend: `crs.search` and `import.formats`

### Deliverables (Part A)

| Path | Action |
|---|---|
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | `crs.search` and `import.formats` (both experimental, read-only) |
| `backend/src/coypu_builder/io/providers/*.py`, `io/imagery/*.py` | `title` and `description` class attributes on every provider, where missing |
| `backend/tests/test_crs_search.py`, `backend/tests/test_import_formats.py` | new |
| `docs/protocol/ipc.md` | regenerate |

### Contract (Part A)

**`crs.search {query: str, limit: int = 20, kind: str | None = None}`** returns
`{results: [{code, name, kind, area, deprecated}]}`:
- `code` is `"EPSG:5514"`-style. `kind` is one of `projected`, `geographic`, `vertical` or `compound`, and
  the `kind` parameter filters by it.
- **Source:** `pyproj.database.query_crs_info(auth_name="EPSG")`, read once and cached in the module. Match
  on the code, and on every word of the query against the name, case-insensitively and ignoring diacritics
  (NFKD fold), so "krovak" finds "Křovák".
- **Order:**
  - for an empty query, or one of at most 2 characters, the Czech-practice codes come first, in this order:
    `5514, 5513, 4326, 4258, 32633, 32634, 5705, 3855`;
  - otherwise an exact code match first, then names that start with the query, then the rest;
  - deprecated CRSs last;
  - each group alphabetically.
- **Limit:** at most `limit`, clamped to 1–200.
- `mutates: false`. Time it: if the first call stays under 100 ms after the cache is warm, run it on the loop.
  Otherwise use `_offload`, and say which in the report.

**`import.formats {}`** returns
`{readers: [{format_id, kinds, suffixes}], providers: [{provider_id, title, description, kinds, licence, attribution, notices, default_query}]}`:
- every registered reader and provider, sorted by id;
- `default_query` is what `normalise_query({})` gives, or `{}` where it needs a project;
- `notices` defaults to `[]`;
- `mutates: false`, on the loop.

### Acceptance criteria (Part A)

1. `test_code_query_finds_it_first`: "5514" gives EPSG:5514 first.
2. `test_name_query_ignores_diacritics`: "krovak" and "Křovák" both list EPSG:5514 and EPSG:5513.
3. `test_vertical_filter`: `kind = "vertical"` with "Baltic" or "Bpv" finds EPSG:5705, and every result is
   vertical.
4. `test_short_query_puts_czech_codes_first`, and `test_limit_is_respected_and_clamped`.
5. `test_import_formats_lists_every_reader_and_provider`: it matches the registry and carries the Copernicus
   liability notice.
6. **No regressions:** as T-134. `gen_protocol_docs.py --check` exits 0.

## Part B — the dialog for files

### Deliverables (Part B)

| Path | Action |
|---|---|
| `client/ui/widgets/crs_picker.gd` | new — `class_name CrsPicker`, a searchable CRS field over `crs.search` |
| `client/ui/dialogs/import_dialog.gd` | new — `class_name ImportDialog`, the five-step dialog |
| `client/ui/top_bar/ribbon.gd` | Import group: "Import terrain or imagery…" |
| `client/ui/shell/ui_shell.gd` | open and own the dialog, like the export dialog |
| `client/core/event_bus.gd` | new signal `sources_changed(kind: String)` |
| `client/scene/terrain/terrain_field.gd` | connect `sources_changed` to `relist()` |
| `client/tests/unit/test_crs_picker.gd`, `test_import_dialog.gd` | new — fake requester |
| `client/README.md` | document the dialog |

### Contract (Part B)

**`CrsPicker`** (`HBoxContainer`):
- A `LineEdit` and a dropdown list of results. Typing debounces 250 ms, then calls
  `crs.search {query, limit: 20, kind}` through its `requester`. Only the latest answer is shown; earlier
  replies that arrive late are dropped.
- **API:** `set_kind_filter(kind: String)`, `set_code(code: String)`, `code() -> String` (`""` when none is
  chosen), and signal `code_changed(code: String)`.
- **Shown value:** the code and name, for example `EPSG:5514 — S-JTSK / Krovak East North`. Free text that
  is not a chosen result leaves `code()` empty.

**`ImportDialog`** (`ConfirmationDialog`), with a `requester: Callable` seam as in `EnvelopeExportDialog`:

**Layout:**
- a step strip at the top: `1 Source · 2 Inspect · 3 Settings · 4 Import · 5 Result`, with the current step
  emphasised;
- Back and Next buttons;
- the primary button reads *Import* on step 3 and *Close* on step 5;
- Escape closes, and Enter presses the enabled primary button;
- a disabled primary button shows its reason as a tooltip and as a one-line hint under the buttons.

**Steps:**
1. **Source.**
   - *Files…* opens a native multi-select file dialog. Its filters are built from `import.formats` reader
     suffixes, never hard-coded. Paths are absolute.
   - *Online source* is shown disabled with "Coming in the next step of this feature" until Part C.
2. **Inspect.** `import.inspect` runs with a busy indicator and the elapsed time. The report shows:
   - format and file count;
   - **the declared CRS, or a prominent notice "No coordinate system declared — choose one in Settings"**;
   - the extent, the counts (for LAS, a small class table), the resolution note and the licence;
   - every warning, verbatim.
3. **Settings**, pre-filled from `suggested_settings`, every field editable:
   - horizontal CRS (`CrsPicker`). It is required: when none is declared, the primary button stays disabled
     until one is chosen;
   - vertical CRS (`CrsPicker` with `kind = vertical`), labelled "recorded only — heights are not
     transformed";
   - coordinate order: Auto / East-North / North-East. Next to it, one line on what Auto means for this
     format: LandXML is North, East; LAS and GeoTIFF are East, North;
   - linear unit, in metres per source unit;
   - clip:
     - corridor, with a buffer and an optional station range;
     - bounding box;
     - none;

     the default is the corridor at 250 m when the project has an alignment, otherwise none;
   - point classes, as checkboxes from the histogram. All are kept by default; F31 means never default to
     class 2 alone;
   - display resolution and nodata, for rasters only;
   - kind, terrain or imagery: from the report, and editable only where the reader's `kinds` hold both;
   - name.
4. **Import.**
   - `import.run`, with a busy indicator and the elapsed time. The dialog stays responsive.
   - `E_BUSY` retries automatically with backoff (0.5 s, doubling to 4 s), showing "Backend busy,
     retrying…".
   - Other errors show one plain sentence with the next step. `E_CRS_REQUIRED` returns to Settings and
     focuses the CRS field.
5. **Result:**
   - the added / changed / unchanged / removed counts;
   - the effective CRS and the transformation, with its accuracy in metres;
   - the warnings, the licence and the attribution;
   - *Close*.

   After a successful `import.run` the dialog emits `EventBus.sources_changed(kind)`. T-135 Part A left this
   signal out, because `event_bus.gd` was not in its Deliverables. `TerrainField` connects it to its
   `relist()`, so the new source appears at once and not only at the next `E_NOT_FOUND`.

**Rules:**
- Settings the user changed are sent exactly as entered. The dialog never rewrites a CRS the user chose.
- Every request is awaited in a coroutine; the UI thread never blocks.
- No path from the data folder appears in the repository or a test. Tests write synthetic files into a
  temporary folder outside the repository.
- Every string uses the plain-language style of T-140's dialogs, the theme tokens and no hard-coded colours.

### Acceptance criteria (Part B)

`test_crs_picker.gd`:
- `test_typing_debounces_and_shows_only_the_latest_answer`;
- `test_free_text_is_not_a_code`;
- `test_kind_filter_is_sent`.

`test_import_dialog.gd`, with a fake requester:
- `test_no_crs_report_keeps_import_disabled_until_a_crs_is_chosen`;
- `test_suggested_settings_prefill_every_field`;
- `test_edits_are_sent_verbatim` — compare the whole `settings` dictionary sent;
- `test_busy_retries_with_backoff`;
- `test_crs_required_returns_to_settings_and_focuses_the_crs_field`;
- `test_file_filters_come_from_import_formats`;
- `test_escape_closes_and_enter_presses_the_primary_button`;
- `test_a_successful_import_emits_sources_changed`, plus, in `test_terrain_field.gd`,
  `test_sources_changed_relists`.

No regressions: both suites are green, with executed = declared test counts.

## Part C — online sources and polish

### Deliverables (Part C)

| Path | Action |
|---|---|
| `client/ui/dialogs/import_dialog.gd` | the online-source step, and "Re-import of" |
| `client/tests/unit/test_import_dialog.gd` | the cases below |
| `client/tests/integration/test_import_dialog_live.gd` | new — live backend, synthetic data, the test-server providers |
| `client/tools/capture/scenarios/ui_shell.json` | import-dialog shots |

### Contract (Part C)

- **Online source:**
  - a list from `import.formats.providers`. Each row shows the title, the description, the licence, and its
    notices on expansion;
  - choosing one sets the kind and the provider's `default_query`. The clip is edited in Settings;
  - the bulk orthophoto (`cuzk-ortho-sheets`) shows its size warning. Its `max_total_mb` field is visible
    in Settings, with the total that inspect reported.
- **"Re-import of":** a selector in Settings that lists the existing sources (`source.list`) of the same
  kind and format, plus "— none —". Choosing one sends `reimport_of`.
- **Capture:** `ui_shell` gains shots of the dialog in Engineering and in Sandbox:
  - at the inspect report with the no-CRS notice;
  - at Settings;
  - at the result.

### Acceptance criteria (Part C)

1. **Unit:**
   - `test_online_source_sets_kind_and_default_query`;
   - `test_bulk_orthophoto_shows_its_size_warning`;
   - `test_reimport_of_sends_the_source_id`.
2. **Live** (`test_import_dialog_live.gd`, with synthetic data from the T-130/T-131 fixtures written into a
   temporary folder outside the repository):
   - files → inspect → set EPSG:5514 → import produces a source;
   - a second run as a re-import reports unchanged objects, with their GUIDs kept;
   - an online-source import against the test-server providers works the same way.
3. **Capture:** the six shots exist. The text is legible and the Czech diacritics render. The reviewer
   judges the flow as a first-time user would meet it.
4. **No regressions:** both suites are green on Windows and on Linux CI, with no previously passing test
   removed, skipped or weakened.

## Invariants

- The client computes no geometry and never parses data files itself; it only shows backend reports.
- Every user-visible string follows the plain-language style of T-140's dialogs. Czech diacritics render.

## Out of scope

- Layers panel content — T-142. Inspector content for sources — T-143. Saving sources with a project —
  T-145.
- Cancelling a running import — not planned for Phase 1. Imports run off-loop and finish. The dialog can be
  closed while one runs, and the result appears in the status bar.
- Vector context imports — M5.

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
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario ui_shell   # Part C
```

## Report back

- **Part A:** how `crs.search` ranks results, its timing, and whether it runs on or off the loop.
- **Part B:** each step as the user meets it, and every contract change.
- **Part C:**
  - each capture shot, described;
  - how `E_BUSY` and long imports feel in practice;
  - any setting from ADR 0009 the dialog could not expose, and why.
