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

## Preconditions

- T-130/T-131:
  - `import.inspect {path|paths|provider+query, format_id?}` returns format, kind, `declared_crs`, extent,
    counts (for example `sample_class_<N>` for LAS), `suggested_settings` (an `ImportSettingsDTO`) and
    warnings, with a resolution note for rasters;
  - `import.run {…, kind, settings, name?, reimport_of?}` returns `source_id`, `effective_crs`,
    `transformation`, `transformation_accuracy_m`, the added / changed / unchanged / removed counts and
    warnings;
  - `source.list`, `source.get`, `plugins.list`;
  - `E_CRS_REQUIRED`, `E_BAD_PARAMS`, `E_BUSY` and `E_INTERNAL`.
- T-133/T-134 providers:
  - `cuzk-dmr5g`, `cuzk-dmr4g` and `copernicus-glo30`;
  - `cuzk-ortho-wmts` (streaming) and `cuzk-ortho-sheets` (bulk, size guard);
  - their queries, default clips, licences and attributions.
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
  `imagery-tiles.md`;
- `client/ui/dialogs/envelope_export_dialog.gd`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/protocol/messages.py`, `server/handlers.py` | `crs.search` (experimental, read-only) |
| `backend/tests/test_crs_search.py` | new |
| `docs/protocol/ipc.md` | regenerate |
| `client/ui/dialogs/import_dialog.gd` | new — the three-step dialog |
| `client/ui/widgets/crs_picker.gd` | new — searchable CRS field over `crs.search` |
| `client/ui/top_bar/ribbon.gd` | Import group: "Import terrain or imagery…" |
| `client/ui/shell/ui_shell.gd` | open and own the dialog, like the export dialog |
| `client/tools/capture/scenarios/ui_shell.json` | add import-dialog shots |
| `client/tests/unit/test_import_dialog.gd` | new — fake requester |
| `client/tests/integration/test_import_dialog_live.gd` | new — live backend, synthetic data |
| `client/README.md` | document the dialog |

## Contract

### `crs.search` (backend)

- **Params and result:** `crs.search {query: str, limit: int = 20}` returns a list of
  `{code: "EPSG:5514", name, kind: "projected|geographic|vertical|compound", area: str, deprecated: bool}`.
- **Source:** pyproj's database, matched on the code and on words in the name.
- **Order:** codes used in Czech practice rank first for an empty or short query (5514, 5513, 4326, 32633,
  5705, 3855), then by relevance.
- **Read-only:** `mutates: false`, and it runs on the loop if a measurement keeps it under 100 ms; otherwise
  it runs off-loop (ADR 0003).

### Dialog flow

1. **Source**, one of two kinds:
   - **Files:** a native multi-select file dialog for `.laz`, `.las`, `.zip`, `.tif`, `.tiff`, `.xml`,
     `.jpg` and `.png` with world files. Paths are absolute.
   - **Online source:** a list of the providers, each with its licence and a short description. Bulk
     orthophoto shows its size warning.

   The kind (terrain or imagery) follows the choice. For files it is taken from the inspect report, and can
   be overridden only where the reader allows both.
2. **Inspect:** `import.inspect` runs with a busy indicator and elapsed time, and its report is shown:
   - format and file count;
   - **declared CRS, or a prominent "No coordinate system declared" notice;**
   - extent, the counts (a class histogram for LAS, as a small table), the resolution note, the licence;
   - every warning, verbatim.
3. **Settings**, pre-filled from `suggested_settings`, every field editable:
   - horizontal CRS (`crs_picker`), required before Import is enabled when none is declared;
   - vertical CRS (`crs_picker` limited to vertical CRSs; it is labelled "recorded only — heights are not
     transformed");
   - coordinate order (Auto / East-North / North-East, with a one-line explanation of what Auto means for
     the format);
   - linear unit;
   - clip: corridor with buffer and optional station range, bounding box, or none. The default is the
     provider's default clip, or the corridor at 250 m;
   - point classes, as checkboxes from the histogram. All are kept by default; F31 means never default to
     class 2;
   - display resolution and nodata (rasters only);
   - name.

   A **"Re-import of"** selector lists existing sources of the same kind, and selecting one sends
   `reimport_of`.
4. **Import:**
   - `import.run`, with a busy indicator and elapsed time. The dialog stays responsive; long imports are
     fine.
   - `E_BUSY` retries automatically with backoff, showing "Backend busy, retrying…".
   - Other errors appear as one plain sentence with the next step. `E_CRS_REQUIRED` focuses the CRS field.
5. **Result:**
   - the added / changed / unchanged / removed counts;
   - the effective CRS and transformation, with its accuracy;
   - warnings, and the licence and attribution;
   - a "Close" button. The Layers panel (T-142) and the terrain scene (T-135) pick the new source up by
     revision.

### Rules

- No path from the Data folder is stored in the repository or a test.
- The dialog never blocks the UI thread. Every request is awaited in a coroutine.
- Settings the user changed are sent exactly as entered. The dialog never "helpfully" rewrites a CRS the
  user chose.

## Invariants

- The client computes no geometry and never parses data files itself; it only shows backend reports.
- Every user-visible string follows the plain-language style of T-140's dialogs. Czech diacritics render.

## Acceptance criteria

1. **`crs.search`:** "5514" finds EPSG:5514 first, "Krovak" lists the Křovák CRSs, "Bpv" or "Baltic" finds
   EPSG:5705, the vertical filter works, and the limit is respected.
2. **Unit tests (fake requester):**
   - a no-CRS report keeps Import disabled until a CRS is chosen;
   - the suggested settings pre-fill every field;
   - edits are sent verbatim;
   - `E_BUSY` retries; `E_CRS_REQUIRED` focuses the CRS field;
   - "Re-import of" sends `reimport_of`.
3. **Live (synthetic data from T-130/T-131 fixtures, written into a temporary folder outside the
   repository):**
   - files → inspect → set EPSG:5514 → import produces a source;
   - a second run as a re-import reports unchanged objects with their GUIDs kept;
   - an online-source import against the test-server providers works the same way.
4. **Capture:** `ui_shell` gains shots of the dialog at the inspect report (no-CRS notice visible) and at
   the result, in Engineering and in Sandbox. Text is legible and the diacritics render.
5. **No regressions:** both suites are green on Windows and on Linux CI, with no previously passing test
   removed, skipped or weakened, and `gen_protocol_docs.py --check` exits 0.

## Out of scope

- Layers panel content — T-142. Inspector content for sources — T-143. Saving sources with a project —
  T-145.
- Cancelling a running import — not planned for Phase 1. Imports run off-loop and finish; the dialog can be
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
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario ui_shell
```

## Report back

State:
- the dialog's steps, with a description of each capture shot;
- how `crs.search` ranks results;
- how `E_BUSY` and long imports feel in practice;
- any setting from ADR 0009 the dialog could not expose, and why.
