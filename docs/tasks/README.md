# Implementation task specifications

Each `task_*.md` in this directory is a **self-contained work order** for one worker session. A worker is
expected to open the repository cold, read only its own task file plus the documents that file names, and
finish without further architectural input.

Tasks are authored by the architect session and tracked in [`ROADMAP.md`](../../ROADMAP.md). Workers do not
edit the roadmap, do not edit other task files, and do not invent scope.

## Structure of a task file

| Section | Meaning |
|---|---|
| **Context** | Why this exists and which roadmap milestone it serves |
| **Preconditions** | What already exists in the repository, and which tasks must have landed first |
| **Deliverables** | Exact file paths to create or modify — nothing outside this list |
| **Contract** | Interfaces, data shapes and semantics that downstream tasks will depend on. Signatures are binding; bodies are the worker's to write |
| **Invariants** | The `CLAUDE.md` / ADR rules this task is most likely to break, stated explicitly |
| **Acceptance criteria** | Observable conditions, each one testable |
| **Out of scope** | Work that belongs to a later task and must not be pulled forward |
| **Verification** | Exact commands to run before reporting done |
| **Report back** | What the worker's closing summary must contain |

## Rules that apply to every task

1. **The contract is binding.** Names, signatures and data shapes in the Contract section are how parallel
   tasks stay compatible. If one is genuinely wrong, stop and say so in the closing summary rather than
   silently improving it — a unilateral rename breaks a sibling task written against it.
2. **Stay inside Deliverables.** Touching a file the task does not list is how two parallel workers collide.
3. **Golden files are regenerated, never hand-edited.** Use `uv run python ../tools/make_golden.py` from
   `backend/`. A golden value that changes unexpectedly is a finding to report, not a file to overwrite.
4. **Privacy guard is absolute.** No vendor, infrastructure-manager or otherwise proprietary data, filename
   or reference may enter the repository — see the rules in `.gitignore` and `CLAUDE.md`. Fixtures must be
   synthetic or open-licensed (OSM/ODbL, ČÚZK). Run `git check-ignore -v <path>` before staging anything new
   under `tests/fixtures/`, and `git status` before finishing.
5. **Do not commit or push unless asked.** Leave the work in the tree; the human reviews and commits.
6. **Report honestly.** If part of the task is blocked, finish everything else and say plainly what was left
   and why. A partially-done task reported as done is worse than a blocked one reported as blocked.

## Environment reminders

- `uv` may not be on `PATH`; the fallback is `%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe`, and failing
  that, `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe`.
- The backend is pinned to Python 3.13 (system Python is 3.14). Always go through `uv run`.
- Godot lives in the git-ignored `tools\godot\` after `tools\install_godot.ps1`. Use the `_console.exe`
  variant when you need to read stdout.
- The Windows console is cp1250 — use `python -X utf8` for scripts that print non-ASCII.
