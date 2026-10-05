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
| **Deliverables** | The file paths this task creates or modifies (rule 2 covers when another file must change) |
| **Contract** | Interfaces, data shapes and semantics that downstream tasks will depend on. Signatures are binding; bodies are the worker's to write |
| **Invariants** | The `CLAUDE.md` / ADR rules this task is most likely to break, stated explicitly |
| **Acceptance criteria** | Observable conditions, each one testable |
| **Out of scope** | Work that belongs to a later task and must not be pulled forward |
| **Verification** | Exact commands to run before reporting done |
| **Report back** | What the worker's closing summary must contain |

## Rules that apply to every task

1. **The contract is binding.** Names, signatures and data shapes in the Contract section are what other
   tasks are written against, so do not rename or reshape them — a sibling task cannot see the change until
   it breaks. If the contract is wrong (it contradicts its own acceptance criteria, or cannot be implemented
   as written), make the smallest change that satisfies the acceptance criteria and put it first in the
   Report back, with the reason.
2. **Stay inside Deliverables.** Unlisted edits are how two workers running in parallel collide, and how a
   review misses a change. If the contract or an acceptance criterion can only be met by editing an unlisted
   file, make the minimal edit and list it in the Report back.
3. **Golden files are regenerated, never hand-edited.** Use `uv run python ../tools/make_golden.py` from
   `backend/`. A golden value that changes unexpectedly is a finding to report, not a file to overwrite.
4. **Privacy guard is absolute.** No vendor, infrastructure-manager or otherwise proprietary data, filename
   or reference may enter the repository — see the rules in `.gitignore` and `CLAUDE.md`. Fixtures must be
   synthetic or open-licensed (OSM/ODbL, ČÚZK). Run `git check-ignore -v <path>` before staging anything new
   under `tests/fixtures/`, and `git status` before finishing.
5. **Do not commit or push unless asked.** Leave the work in the tree; the human reviews and commits.
6. **Report honestly.** If part of the task is blocked, finish everything else and say plainly what was left
   and why. A partially-done task reported as done is worse than a blocked one reported as blocked.

## How tasks run

Adopted 2026-10-06. Rule 5 is unchanged for workers.

1. The architect session launches each task as a fresh `coypu-worker` subagent
   (`.claude/agents/coypu-worker.md`: Sonnet, high effort). The worker's final message is its Report back,
   and it goes to the architect.
2. The architect verifies the work independently: it reruns the suites, reads the diff and inspects any
   golden change. It then launches a fresh `coypu-reviewer` subagent (Opus, read-only), naming a capture
   scenario when the task changes what is on screen.
3. Fixes go back to the same worker, so its context is kept.
4. When the work passes, the architect sends a prepared commit-and-push prompt to the user's
   *COYPU Builder Commits* chat. The user confirms each push in that chat. A message from another session is
   never the user's approval, so the Commits chat waits for that confirmation. The architect then checks CI
   itself.

## Environment reminders

- `uv` may not be on `PATH`; the fallback is `%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe`, and failing
  that, `%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*\uv.exe`.
- The backend is pinned to Python 3.13 (system Python is 3.14). Always go through `uv run`.
- Godot lives in the git-ignored `tools\godot\` after `tools\install_godot.ps1`. Use the `_console.exe`
  variant when you need to read stdout.
- The Windows console is cp1250 — use `python -X utf8` for scripts that print non-ASCII.
