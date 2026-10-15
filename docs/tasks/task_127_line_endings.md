# T-127 — Line-ending policy

**Milestone:** Phase 1 workflow tooling · **Depends on:** T-140 committed, and a clean working tree ·
**Blocks:** nothing (T-133 onward run after it)

## Context

The repository has no `.gitattributes`, and this Windows machine uses `core.autocrlf=true`. The index is
LF, but working copies are a mix: 33 tracked files check out as CRLF and one as mixed, among them
`shared/golden/*.json` and several Python and GDScript sources. The rest are LF (ROADMAP F36).

This caused a real defect in T-140. A GDScript string literal containing a newline became `\r\n` on a
Windows checkout, so saved settings never parsed. Linux CI cannot see this class of bug. The fix is a single
explicit policy: **LF everywhere** in the repository and on every OS, except files that must be CRLF for
Windows tools.

## Preconditions

- T-140 committed and pushed, and a clean working tree. **Start only if `git status --porcelain` prints
  nothing,** and stop and report otherwise.
- `git ls-files --eol` shows the current state.
- `client/addons/gdUnit4/runtest.cmd` is a Windows batch file, so it needs CRLF.

## Deliverables

| Path | Action |
|---|---|
| `.gitattributes` | new |
| the working tree | renormalised; the index must not change except for files that really were CRLF in the index (expected: none) |
| `CLAUDE.md` | one line under Conventions: "LF line endings everywhere (`.gitattributes`); never rely on platform newlines in string literals" |
| `backend/tests/test_repo_hygiene.py` | a test that every tracked text file is LF in the index, and that `.gitattributes` exists with the policy |

## Contract

`.gitattributes`:

```gitattributes
* text=auto eol=lf
*.cmd text eol=crlf
*.bat text eol=crlf
*.png binary
*.jpg binary
*.jpeg binary
*.webp binary
*.ttf binary
*.otf binary
*.zip binary
*.laz binary
*.las binary
*.tif binary
*.tiff binary
```

Extend the binary list with any binary type found among the tracked files. `git ls-files --eol` lists them
as `-text`.

**Procedure:**
1. Write the file.
2. Run `git add --renormalize .`.
3. Inspect `git status` and `git diff --cached --stat`. Expected: `.gitattributes` and the two deliverable
   edits only. Any other staged change means some file was CRLF in the index: list it and explain it.
4. Unstage everything, without committing.
5. Refresh the working copies so they become LF: `git rm --cached -r -q . && git reset --hard` **only on a
   clean tree** — or the equivalent per-file checkout. Confirm `git ls-files --eol` shows `w/lf` for every
   text file, and `w/crlf` only for `*.cmd` / `*.bat`.

## Acceptance criteria

1. `git ls-files --eol`: every text file is `i/lf w/lf`, except `*.cmd` / `*.bat`, which are
   `i/lf w/crlf`: git keeps the index LF and writes CRLF to the working tree, corrected 2026-10-15. No `w/mixed`.
2. The goldens are byte-identical in the index; `tools/make_golden.py` is not run.
3. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened.
4. `gen_protocol_docs.py --check` exits 0, and ruff is clean.
5. `tools\capture.ps1 -Scenario kralupy_m2` still runs; PowerShell scripts work with LF.
6. The new hygiene test fails if a CRLF text file is staged (proven with a temporary-index mutant).

## Out of scope

- Re-formatting or changing any file's content beyond line endings.

## Verification

```bash
git ls-files --eol
cd backend
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run python ../tools/gen_protocol_docs.py --check
```

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
powershell -ExecutionPolicy Bypass -File tools\capture.ps1 -Scenario kralupy_m2
```

## Report back

State:
- the `git ls-files --eol` summary before and after;
- any file whose index content changed, and why;
- the binary patterns you added;
- confirmation that every suite passed on the LF working tree.
