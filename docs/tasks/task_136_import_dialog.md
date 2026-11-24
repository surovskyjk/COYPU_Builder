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
- `code` is `"EPSG:5514"`-style, and **each code appears at most once**. PROJ returns one row per area of
  use, so keep one row per code, the one with the widest area.
- `kind` in a result is one of `projected`, `geographic`, `vertical` or `compound`.
- **The `kind` parameter** is one of those four, or `horizontal`, or `None`. `""` counts as `None`.
  - `horizontal` means projected plus 2D geographic: the CRSs `import.run` accepts as `horizontal_crs`. It
    leaves out 3D geographic CRSs (EPSG:4979, 4937 and the like), vertical and compound CRSs.
  - Any other value is `E_BAD_PARAMS`.
- **Source:** `pyproj.database.query_crs_info(auth_name="EPSG", allow_deprecated=True)`, read once and cached
  in the module.
- **Matching** is case-insensitive and ignores diacritics (NFKD fold), so "krovak" finds "Křovák".
  - Split the query into words.
  - Drop words that are only punctuation ("—", "/", "+"), and the word "epsg". Strip an "epsg:" prefix from
    any word.
  - A CRS matches when every remaining word matches **either** its code (as a prefix: "551" matches 5514,
    not 4551) **or** its name or an alias (as a substring).
  - A name also matches with spaces and hyphens removed from both sides, so "sjtsk" finds "S-JTSK" and
    "wgs84" finds "WGS 84".
  - The string the picker shows for a chosen CRS, `EPSG:5514 — S-JTSK / Krovak East North`, must find that
    CRS first.
- **Order:**
  - deprecated CRSs last;
  - then, for an empty query or one of at most 2 characters, the Czech-practice codes first, in this order:
    `5514, 5513, 4326, 4258, 25833, 25834, 32633, 32634, 5705, 3855`;
  - then the match groups: an exact code; a code that starts with the query; an exact name; a name that
    starts with the query; the rest;
  - within each group, the Czech-practice codes first, in the order above, for every query length;
  - then alphabetically by name, then by code number.
- **Limit:** at most `limit`, clamped to 1–200.
- `mutates: false`, on the loop. Warm calls take 2–9 ms. The first build of the catalogue takes about 0.1 s
  and mostly holds the GIL, so `serve()` builds it once before the socket opens, and no client ever waits
  for it.

**`import.formats {}`** returns
`{readers: [{format_id, kinds, suffixes}], providers: [{provider_id, title, description, kinds, licence, attribution, notices, default_query}]}`:
- every registered reader and provider, sorted by id. Call `plugin_report()` first, as the inspect and run
  handlers do;
- `default_query` is what `normalise_query({})` gives. A provider that has no `normalise_query`, raises
  from it, or returns something that is not JSON gives `{}`. One plugin must never break the whole list;
- `notices` defaults to `[]`, and a plain-string `notices` is one notice, not a list of characters (as
  `_notices_for` does);
- reader suffixes are lower case with a leading dot. `register_reader` refuses any other suffix, so the
  plugin is recorded as failed;
- `mutates: false`, on the loop.

**Provider descriptions** (one line in the dialog's list, plain language):

| Provider | `description` |
|---|---|
| `cuzk-dmr5g` | Bare-ground height points from the Czech national laser scan, downloaded one map sheet at a time. Czech Republic only. |
| `cuzk-dmr4g` | A 5 m grid of bare-ground heights, downloaded one map sheet at a time. Czech Republic only. |
| `copernicus-glo30` | Free worldwide heights on a grid of about 30 m. They follow treetops and rooftops, not bare ground, so use them only where better ground data is missing. |

### Acceptance criteria (Part A)

1. `test_code_query_finds_it_first`: "2003" gives EPSG:2003 (Grenada 1953 / British West Indies Grid) first,
   ahead of EPSG:20034 (CGVD2013a(2002) height), which matches by prefix and whose name sorts first. "5514"
   cannot test the order, because it matches only one CRS.
2. `test_name_query_ignores_diacritics`: "krovak" and "Křovák" both list EPSG:5514 and EPSG:5513.
3. `test_vertical_filter`: `kind = "vertical"` with "Baltic" or "Bpv" finds EPSG:5705, and every result is
   vertical.
4. `test_short_query_puts_czech_codes_first`, and `test_limit_is_respected_and_clamped`.
5. `test_import_formats_lists_every_reader_and_provider`: it matches the registry and carries the Copernicus
   liability notice. The `las` reader's suffixes are exactly `[".las", ".laz", ".zip"]`.
6. From the review:
   - `test_codes_are_unique`, and "utm zone 33" lists EPSG:25833 once;
   - `test_the_shown_value_finds_its_crs`: `EPSG:5514 — S-JTSK / Krovak East North`, "epsg 5514",
     "5514 krovak", "sjtsk" and "wgs84" each find their CRS first;
   - `test_code_matches_by_prefix`: "551" lists 5514 before 4551, or leaves 4551 out;
   - `test_czech_codes_lead_their_group`: "krovak" and "S-JTSK" give EPSG:5514 within the first two;
   - `test_short_query_boundary`: two characters put the Czech-practice codes first, three do not;
   - `test_horizontal_kind`: "wgs 84" with `kind = "horizontal"` lists 4326 and not 4979, and `kind = ""`
     is no filter;
   - `test_a_broken_plugin_provider_does_not_break_the_list`;
   - `test_reader_suffixes_must_be_lower_case_with_a_dot`.
7. **No regressions:** as T-134. `gen_protocol_docs.py --check` exits 0.

## Part B — the dialog for files

### Deliverables (Part B)

| Path | Action |
|---|---|
| `client/ui/widgets/crs_picker.gd` | new — `class_name CrsPicker`, a searchable CRS field over `crs.search` |
| `client/ui/dialogs/import_dialog.gd` | new — `class_name ImportDialog`, the five-step dialog |
| `client/ui/widgets/number_field.gd`, `client/tests/unit/test_number_field.gd` | new (review round 2) — `class_name NumberField`, exact numeric entry |
| `client/core/backend.gd` | `request()` gains an optional `timeout_sec` (review decision 5) |
| `client/ui/top_bar/ribbon.gd` | Import group: "Import terrain or imagery…"; its doc comment no longer says Import has no command |
| `client/project.godot` | input action `shell_import_sources`, Ctrl+I (free today) |
| `client/assets/icons/import_sources.svg` | new — original line artwork on the 24 px grid with a 2 px stroke, like its neighbours; the editor run writes its `.import` file, which is committed with it |
| `client/assets/icons/LICENSE` | add `import_sources` to the list of original icons |
| `client/ui/shell/ui_shell.gd` | open and own the dialog, like the export dialog; `show_dialog(&"import")` and `close_dialogs()` cover it |
| `client/core/event_bus.gd` | new signal `sources_changed(kind: String)` |
| `client/scene/terrain/terrain_field.gd` | connect `sources_changed` to `relist()` |
| `client/tests/unit/test_crs_picker.gd`, `test_import_dialog.gd` | new — fake requester |
| `client/tests/unit/test_ui_preset.gd` | the expected command list gains the import command |
| `client/tests/integration/test_terrain_field.gd` | `test_sources_changed_relists` |
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
   - horizontal CRS (`CrsPicker` with `kind = horizontal`). It is required: when none is declared, the
     primary button stays disabled until one is chosen;
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

### Clarifications (Part B, 2026-11-23, before the worker)

Checked against the code after T-134 B. Where the contract above leaves a choice open, these settle it.

**Buttons.**
- The dialog's OK button is the primary, and it is the Next button. It reads:
  - *Next* on steps 1 and 2;
  - *Import* on step 3;
  - *Close* on step 5.

  On step 4 it is disabled while the import runs.
- *Back* is an extra button (`add_button`). It is disabled on steps 1, 4 and 5.
- *Cancel* closes the dialog on every step.
- Set `dialog_hide_on_ok = false`, so that OK moves to the next step instead of hiding the window. Only *Close* on
  step 5 hides it.
- Pressing Next on step 1 runs `import.inspect` and shows step 2 with the busy indicator. Next on step 2 is
  enabled once a report has arrived. Back keeps what was entered, but choosing other files on step 1 and
  inspecting them again resets Settings to the new `suggested_settings`.
- Each disabled primary button has a reason, shown in its tooltip and in the hint line. For example:
  - "Choose one or more files first";
  - "The inspection failed — go back and choose other files";
  - "Choose a coordinate system for this data";
  - "Enter a box whose minimum is below its maximum".

**What is sent.**
- `import.inspect {paths, kind?}`: always `paths`, also for one file. No `kind`, so every reader is tried.
- `import.run {paths, kind, settings, format_id, name?}`: `format_id` is the one the report gave, so the run
  uses the same reader as the inspection. An empty name sends no `name`.
- `settings` is a whole `ImportSettingsDTO` dictionary. A field the user did not touch keeps its
  `suggested_settings` value, including `null`.

**The CRS picker and declared values.**
- `set_code(code)` holds any CRS string: `EPSG:<n>`, WKT or PROJ. `code()` returns it unchanged.
- For `EPSG:<n>` the picker shows the code at once, then fills in the name from
  `crs.search {query: code, limit: 1, kind}` when the first result has that code.
- For any other string it shows "As declared in the file".

  A WKT that `suggested_settings` carries is therefore sent back exactly as it came. Readers suggest the
  declared CRS as `EPSG:<n>`, or as WKT when it has no EPSG code.
- Choosing a result from the list sets its `code`. Free text sets `""` (as the contract says).

**Rasters.**
- The report is a raster's when its `counts` hold `width` and `height`. GeoTIFF terrain and orthophoto images
  report these; LAS and LandXML surfaces do not. Base this on the report, never on a list of format ids.
- Only for rasters, the dialog shows display resolution and nodata.
- These, and the corridor's station range, are optional numbers. Each is a CheckBox with a SpinBox, as in
  `EnvelopeExportDialog`'s station range. Unchecked sends `null`. Checked by default only when the suggested
  value is not `null`.

**Point classes.**
- One checkbox per `sample_class_<N>` count, labelled with its code and its sampled share.
- While every box is checked, `point_classes` keeps its suggested value (`null` means every class, including
  any the sample missed).
- Once one is unchecked, it is the sorted list of the checked codes.
- Unchecking all of them disables Import, with the reason "Keep at least one point class".

**Clip.**
- Default: the corridor at 250 m, with `alignment_ids` empty (every alignment), when `Session.alignments()` is
  not empty. Otherwise none.
- The station range is offered only when the project has exactly one alignment.
- The bounding box is in the project's coordinate system, and the dialog says so beside it.
  - It is pre-filled from the report's `extent` only when the settings' horizontal CRS equals
    `Session.crs()`. Otherwise it starts empty.
  - Import stays disabled until the minimum is below the maximum on both axes.
- The dialog is opened with `open(alignments: Array[Dictionary])`, like the export dialog.

**`E_BUSY`.**
- `import.inspect` and `import.run` both retry `E_BUSY`.
- The delays come from `static func busy_backoff_sec(attempt: int) -> float`: 0.5, 1, 2, then 4 s.
- At most 8 retries, then one sentence: "The backend stayed busy — try again in a moment."
- The wait goes through a seam, `var wait: Callable = func(sec: float) -> void: await
  get_tree().create_timer(sec).timeout`, so the test records the delays instead of sleeping them.
- `E_BUSY` is answered before the backend does any work, so retrying `import.run` is safe.

**Closing while an import runs.**
- Out of scope says the dialog may be closed during an import. The coroutine keeps going.
- If the window is hidden when the reply arrives, the outcome is still delivered:
  - a success emits `sources_changed(kind)` and `EventBus.status_message` with one sentence, for example
    "Imported 1 terrain source "kralupy_dmr" (12 objects added)";
  - a failure emits `EventBus.status_message` with the plain error sentence.
- Opening the dialog again while that import runs shows step 4 with the busy indicator.

**Errors.** Reuse the shape of `EnvelopeExportDialog.plain_error`: one sentence for each code.
- `E_CRS_REQUIRED`: "This file declares no coordinate system — choose one". It returns to Settings.
- `E_BAD_PARAMS`: the backend's message, which is already plain.
- `E_NO_PROJECT`: "Open or create a project first".
- `E_NOT_FOUND`: "A file could not be found — choose it again".
- `E_EMPTY`: "Nothing of this data lies inside the clip — widen the clip or choose none". It returns to
  Settings.
- `E_INTERNAL`: "Something went wrong in the backend — the log has the details".

### Review decisions (Part B, 2026-11-23)

The Sonnet review passed Part B with changes: 352/352 gdUnit tests, and every settings combination
decodes into the backend's own `ImportRunParams`. The GPU renders found what headless tests cannot see.
These decisions are binding for the fix round.

1. **The dialog fits on the screen (review finding 1, high).** At the default 1600×900 window, the Inspect and
   Settings pages pushed Back, Next and Cancel below the screen edge. At 1280×720, Import could not be clicked.
   The cause: the window was centred once at about 296 px high and then grew downward.
   - On every step change while visible, the dialog calls `reset_size()` and re-centres. Defer it by one
     frame, so that the new page's size is known.
   - The window's height never exceeds 90 % of the visible screen.
   - The Inspect report sits in a `ScrollContainer` with a capped height, like the Settings page.
2. **The CRS picker (finding 2).**
   - The dropdown no longer widens the dialog: `fit_to_longest_item = false`, `clip_text = true`.
   - The field and the dropdown share the row 3 : 2.
   - `set_code` clears the list back to its idle placeholder, so a code from an earlier file is never shown
     beside a new one.
   - A chosen result shows its code and name in the field.
   - **Enter** in the field, while it holds no chosen code and results are listed, chooses the first result.
     With a chosen code, it presses the primary button. **Down** opens the dropdown.
3. **Numbers are never rounded, snapped or clamped on the way through (finding 3; the "sent exactly as
   entered" rule).**
   - **No data** is a CheckBox with a `LineEdit`, not a SpinBox.
     - The text shows the value with `str()`, so −3.4028234663852886e38 is shown in full precision.
     - Text that is not a valid float disables Import, with the reason "Enter a number for no data".
     - Unticking and re-ticking restores the suggested value, unless the user edited it.
   - **The other SpinBoxes** (linear unit, buffer, resolution, station range, box):
     - `step = 0`, with `custom_arrow_step` for the arrows;
     - `allow_greater` and `allow_lesser` wherever a bound is not physical. A unit, a resolution and a buffer
       must still be above 0.
     - A value typed or suggested is the value sent. Check the display at `step = 0` with a probe, and report
       how a long value shows.
4. **"Import as" on step 1 (finding 4).** In the real registry no reader takes both kinds, so the Settings
   "Kind" control was always disabled. A `.tif` orthophoto was inspected as terrain, with no way to import it
   as imagery.
   - Step 1 gains an "Import as" choice: *Detect from the files* (the default, which sends no `kind`),
     *Terrain (heights)* or *Imagery (orthophoto)*.
   - The chosen kind is sent as `kind` to `import.inspect`.
   - Settings shows the kind as text, with "to import these files as the other kind, go back to step 1"
     where the reader takes only one kind. It stays a choice where a reader takes both.
5. **Long requests (finding 5).**
   - `client/core/backend.gd: request()` gains an optional third parameter, `timeout_sec := REQUEST_TIMEOUT_SEC`.
     That is a minimal edit to an unlisted file, made because the contract needs it. Calls that already
     exist do not change.
   - The dialog's default requester uses 1800 s for `import.run` and 300 s for `import.inspect`. The backend
     answers pings meanwhile (ADR 0003 addendum), so a long wait is not a dead connection.
   - `E_TIMEOUT` on `import.run` reads "The import is taking longer than expected — it may still finish; check
     the data sources before importing again". It still emits `sources_changed`, so a finished import shows.
6. **Stale errors (finding 6).** Inspecting clears the Settings error and its label.
7. **Point classes (finding 7).**
   - Ticks unchanged from how they were filled: send the suggested value verbatim.
   - Otherwise, every box ticked: send `null` (every class).
   - Otherwise: the sorted list of the ticked codes.
8. **Back keeps the report (finding 8).** Next on step 1 with the same files and a report re-inspects
   nothing. It goes straight to step 2, with the settings kept. Choosing files or changing "Import as" clears
   the report.
9. **Smaller fixes:**
   - **The extent** has no unit. Values of 1,000 or more show 1 decimal; smaller ones (degrees) show 6.
   - **The box pre-fill** is re-evaluated when the horizontal CRS changes, as long as the user has not edited
     the box.
   - **No project:**
     - Step 1 shows "Open or create a project first — imported data belongs to a project" while
       `Session.project_id()` is empty, and Next is disabled with that reason.
     - Re-evaluate it when the dialog opens and on `EventBus.project_changed`.
   - **Format labels:** add `wmts-descriptor` ("ČÚZK orthophoto service (.wmts)") and `cuzk-ortho-sheet`
     ("ČÚZK orthophoto sheet").
   - **The Result page** opens with a headline, `Imported "<name>"`.
   - **Point classes** show the standard ASPRS names from the LAS 1.4 specification beside their codes, for
     example "2 Ground", "6 Building", "9 Water". An unknown code shows only its number. These are display
     labels; the client parses no data.
   - **`E_DISCONNECTED`** reads "The backend is not running — wait for it to reconnect, then try again". It
     no longer says "nothing was imported".
   - **Shadowed `name` locals** in both scripts are renamed.
10. **Tests that can fail (finding 10).** Every assertion compares with literal numbers, never with a widget's
   own value.
   - `test_edits_are_sent_verbatim` covers:
     - linear unit 0.3048006096;
     - a box of literals;
     - `vertical_crs`;
     - a station range of 12000.25 to 15500.5 on an alignment that starts at 12345.6789;
     - display resolution 0.125;
     - nodata −3.4028234663852886e38 left untouched, then unticked and re-ticked.
   - The Enter half of `test_escape_closes_and_enter_presses_the_primary_button` focuses a `LineEdit`, not
     the button.
   - The prefill test feeds JSON-shaped floats (`[2.0, 9.0]`) and asserts that ints are sent.

### Review decisions, round 2 (Part B, 2026-11-23)

After the first fix round: 371/371 gdUnit, and the dialog fits and is centred at 1280×720 and 1600×900. The GPU
shots showed two problems that `step = 0` brings with it:
- the station "To" field showed "99999998 m" for 18184.9717. A SpinBox at `step = 0` prints 16 decimals
  ("18184.971699999998"), and the narrow field clipped the head;
- the no-data value showed as a 39-digit decimal.

The values sent were right in both cases. These decisions are binding:

11. **`NumberField`** (new: `client/ui/widgets/number_field.gd`, `class_name NumberField extends LineEdit`;
    tests in `client/tests/unit/test_number_field.gd`). Every numeric setting in the dialog uses it: linear
    unit, buffer, station range, display resolution, no data and the four box values. It replaces the
    SpinBoxes and the no-data `LineEdit`.
    - `set_value(v: float)` stores `v` and shows `exact_text(v)`. `value() -> float` returns the stored float.
      An untouched field gives back exactly what it was given.
    - User text is parsed on every edit. Valid text sets the value and emits
      `value_edited(value: float)`. Invalid or out-of-bounds text makes `is_valid()` false.
    - Bounds: `minimum` and `maximum` (default −INF and INF), plus `minimum_exclusive: bool` for a unit, a
      resolution and a buffer, which must be above 0. Nothing is ever clamped or snapped. A value outside
      the bounds is invalid, not changed.
    - Up and Down step the value by `arrow_step` while the field has focus. The result is shown with
      `exact_text`.
    - `static func exact_text(v: float) -> String` gives the shortest text that `to_float()` reads back as
      exactly `v`:
      - `12345.6789` stays as written, and `0.3048006096` stays as written;
      - `250.0` shows as `250`;
      - magnitudes of 1e15 or more, and non-zero magnitudes below 1e-4, use scientific notation. For example
        −3.4028234663852886e38 shows as `-3.4028234663852886e+38` or shorter, if a shorter text reads back
        exactly.

      GDScript has no `%g`. A loop over the decimal count that checks the read-back is enough.
    - A unit (`m`) is a `Label` beside the field, not part of its text.
    - Every invalid field disables Import with a reason that names it, for example "Enter a number for the
      buffer" or "The linear unit must be above 0". The no-data reason stays "Enter a number for no data".
12. **Enter.** Enter in a `NumberField` only confirms the number. It never presses Import, because a user who
    types a value and presses Enter means the value, not the import. Enter in the name field, and in the CRS
    field once a code is chosen, presses the primary button. Enter on a focused button presses it, as Godot
    does by default.
13. **No project:** the reason shows once, in the hint line. The red line on the page goes.
14. **Seams for the untested bits:**
    - `static func request_timeout_sec(method: String) -> float`: 1800 for `import.run`, 300 for
      `import.inspect`, `Backend.REQUEST_TIMEOUT_SEC` otherwise. The default requester uses it.
    - A test presses Down in the CRS field and asserts that the results popup is shown.

### Review decisions, round 3 (Part B, 2026-11-24)

The re-review (Sonnet) passed the two fix rounds with changes. Of its first ten findings, eight are fixed and
two are mostly fixed. The dialog fits and is centred in every state, in both presets, at 1280×720 and
1600×900. `exact_text` is correct and fast over 21,055 doubles: p99 0.37 ms, with no format errors and no
overflow. These small items remain:

15. **The CRS dropdown must not look chosen.** `_show_results` selected the placeholder before adding the
    entries, so result 1 showed as selected while `code()` was "". Select the placeholder after the entries
    are added. A test asserts `results_choice().selected == 0` once results are listed.
16. **Enter never picks from an out-of-date list.** Every keystroke puts the list back to its idle
    placeholder until the answer for the new text arrives. Enter with no current list does nothing.
    A test types "k", lets its answer arrive, types "krovak", presses Enter before the debounce runs out, and
    asserts that `code()` is still "".
17. **Warnings are never below the fold.**
    - The report lists the warnings, with the no-CRS notice, above the class table.
    - The scroll caps of the report, Settings and Result pages follow the screen: the 90 % rule minus the
      dialog's own chrome, not a fixed 300 or 360 px.
    - The fit test keeps passing at 1280×720.
18. **Arrow steps are decimal.** Up and Down round the sum to the larger decimal count of the current text
    and of `arrow_step`, so 0.3048 + 0.01 gives 0.3148, then 0.3248, and 0.7 + 0.1 gives 0.8. A field that
    is not `editable` ignores Up and Down.
19. **Smaller items:**
    - The buffer has a client-side maximum of 5000 m, which matches `domain/sources.py`. Its reason reads
      "The buffer must be at most 5000 m".
    - The dropdown's idle placeholder is short enough not to be clipped mid-word: "Search results". Set
      `text_overrun_behavior` to an ellipsis on the OptionButton.
    - ASPRS codes 8 and 12 are reserved in LAS 1.4, so they show only their numbers.
    - `_import_running` stays true until `_finish_import`, including its `source.get`, has finished. Hiding
      and re-opening during that wait then shows step 4.
20. **Tests for the survivors the re-review named:**
    - `exact_text` gains these cases:
      - 1e15 and both its neighbours, and 999999999999999.9;
      - 1e-4 and both its neighbours;
      - −0.0;
      - the smallest subnormal and the largest double;
      - −1947302.4602381494, a value that Godot cannot read back. It must give the correctly rounded
        17-digit text.
    - The "no longer than the shortest" assertion must also bind for the scientific cases.
    - A dialog-level test shows that Enter in the CRS field with a chosen code presses the primary button.
    - The fit test also checks the 36 px title bar.

### Review decisions, round 4 (Part B, 2026-11-24)

Round 3 reached 390/390. The architect's look at its GPU shots found two leftovers:

21. **Pages sized to their content.** Each of the report, Settings and Result scrolls is as tall as its content,
    up to the screen-derived cap of decision 17. They are no longer always as tall as the cap: at 1600×900
    the Result page was a mostly empty, nearly full-screen window. Measure the content after layout, in the
    deferred fit; take a second deferred pass if wrapped labels have not settled after the first. Then
    reset the size and re-centre.
22. **Step 5 shows only *Close*.** *Back* and *Cancel* are hidden on the Result page. A user should not see
    two buttons that do the same thing.

Tests:
- `test_short_pages_are_sized_to_their_content`: at 1600×900, the Result page with the standard result is
  less than half the screen tall, and the LAS report at 1280×720 still fits;
- `test_the_result_page_shows_only_close`.

The CRS dropdown's popup list can still run past the screen with a very long name. That is low, because
real EPSG names are shorter, so it goes to Part C's capture review.

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

Added with the clarifications:
- `test_crs_picker.gd`:
  - `test_a_declared_wkt_is_kept_verbatim`;
  - `test_set_code_fills_the_name`;
  - `test_a_late_name_lookup_does_not_replace_a_chosen_code`.
- `test_import_dialog.gd`:
  - `test_rasters_show_resolution_and_nodata_others_do_not`;
  - `test_all_classes_checked_keeps_the_suggestion_and_unchecking_sends_the_list`;
  - `test_corridor_is_the_default_only_with_an_alignment`;
  - `test_inspect_and_run_send_paths_and_the_reported_format`;
  - `test_busy_gives_up_after_eight_retries_with_one_sentence`;
  - `test_closing_during_import_reports_in_the_status_bar`;
  - `test_every_disabled_primary_has_a_reason`.
- `test_ui_preset.gd`: `test_import_command_opens_the_dialog`.

Added with the review decisions:
- `test_crs_picker.gd`:
  - `test_enter_chooses_the_first_result`;
  - `test_set_code_clears_an_earlier_list`;
  - `test_long_names_do_not_widen_the_picker`.
- `test_import_dialog.gd`:
  - `test_every_step_fits_a_1280_by_720_screen`: after each step change, the dialog's size and position lie
    inside a 1280×720 visible rect, with the large LAS report and the GeoTIFF settings;
  - `test_nodata_keeps_a_float32_value_through_a_toggle`;
  - `test_import_as_sends_the_kind_to_inspect`;
  - `test_a_timed_out_import_says_it_may_still_finish`;
  - `test_inspect_clears_an_old_settings_error`;
  - `test_point_classes_rule`, which covers the three cases of decision 7;
  - `test_back_and_next_on_the_same_files_keeps_the_settings`;
  - `test_no_project_disables_next_with_a_reason`;
  - `test_result_page_shows_every_field`;
  - `test_report_page_shows_every_field`;
  - `test_bbox_prefill_follows_the_horizontal_crs`;
  - `test_not_found_returns_to_source_and_empty_returns_to_settings`;
  - `test_a_failure_while_hidden_reports_in_the_status_bar`;
  - `test_reopening_while_an_import_runs_shows_step_four`;
  - `test_inspect_retries_busy`.

Added with round 2:
- `test_number_field.gd`:
  - `test_exact_text_reads_back_exactly`, for 0.3048006096, 12345.6789, 18184.971666, 250.0, −746900.0,
    1e-7, 0.0 and −3.4028234663852886e38. Each text reads back equal, and each is no longer than the
    shortest such text;
  - `test_an_untouched_field_returns_the_value_it_was_given`;
  - `test_invalid_and_out_of_bounds_text_is_not_a_value`;
  - `test_up_and_down_step_by_the_arrow_step`.
- `test_import_dialog.gd`:
  - `test_enter_in_a_number_field_does_not_start_the_import`;
  - `test_request_timeouts_per_method`;
  - `test_every_invalid_number_disables_import_with_its_name`.
- `test_crs_picker.gd`: `test_down_opens_the_results`.

Added with round 3:
- `test_crs_picker.gd`:
  - `test_listed_results_leave_the_placeholder_selected`;
  - `test_enter_ignores_an_out_of_date_list`.
- `test_number_field.gd`:
  - `test_exact_text_boundaries_and_extremes`;
  - `test_arrow_steps_are_decimal`;
  - `test_a_read_only_field_ignores_the_arrows`.
- `test_import_dialog.gd`:
  - `test_warnings_come_before_the_class_table`;
  - `test_enter_in_the_crs_field_with_a_code_presses_the_primary_button`;
  - `test_buffer_above_5000_disables_import_with_a_reason`;
  - `test_hiding_during_the_source_lookup_reopens_at_step_four`.

**GPU check:** the reviewer opens the dialog on the GPU in both presets and judges steps 1 to 5 from real
windows. A scratch script outside the repository drives it with a fake requester. Headless tests cannot show
layout, clipping or focus rings (F44).
- The worker adds no capture shots (that is Part C).
- `ui_shell.show_dialog(&"import")` must make the dialog reachable for that check.

No regressions: both suites are green, with executed = declared test counts.

## Part C — online sources and polish

### Deliverables (Part C)

| Path | Action |
|---|---|
| `client/ui/dialogs/import_dialog.gd` | the online-source step, and "Re-import of" |
| `client/tests/unit/test_import_dialog.gd` | the cases below |
| `client/tests/integration/test_import_dialog_live.gd` | new — live backend, synthetic data, the test-server providers |
| `client/tools/capture/scenarios/ui_shell.json` | import-dialog shots |
| `client/ipc/envelope.gd`, `client/tests/unit/test_ipc_envelope.gd` | F51: encode headers at full precision (`JSON.stringify(header, "", true, true)`); a unit test shows that the encoded header parses back (`JSON.parse_string`) to the same float64 for −3.4028234663852886e38 and 0.3048006096 |
| `backend/src/coypu_builder/io/terrain/raster.py`, `io/imagery/raster_image.py`, a backend test | F51, decode side: Godot reads the suggested float32 nodata as the neighbouring double and sends it back 1 ulp off. Cast a confirmed `nodata` to the raster's dtype before masking, and record the cast value. A test shows that a nodata 1 ulp above FLT_MAX still masks the file's FLT_MAX cells and is recorded as FLT_MAX |

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
