"""Guard on the committed Claude Code configuration (T-126).

`.claude/agents/**/*.md` is committed so the worker/reviewer workflow is versioned, and Claude Code executes
that configuration for anyone who opens the repository: an agent definition may declare `hooks` (shell
commands) and `mcpServers`, and a committed `settings.json` may run commands or switch servers on. None of
that may be committed, and machine-local state must stay untracked.

Known limitation: frontmatter keys are matched line by line, so the rare YAML spellings that do not put a
key at the start of a line (merge keys such as `<<: *anchor`, the explicit `? hooks` key form, flow-style
`{hooks: ...}` on one line) are not recognised. Claude Code's agent files do not use them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_AGENT_KEYS = {"hooks", "mcpservers"}
# Keys of a committed settings.json that run commands or enable servers for anyone opening the repository.
_FORBIDDEN_SETTINGS_KEYS = {
    "hooks",
    "mcpservers",
    "apikeyhelper",
    "enableallprojectmcpservers",
    "statusline",
}
_ALLOWED_PERMISSION_MODES = {"default", "plan"}
_FORBIDDEN_TRACKED_PREFIXES = (".claude/worktrees/", ".claude/agent-memory-local/")
_FORBIDDEN_TRACKED_FILES = (".claude/settings.local.json",)
_FORBIDDEN_TRACKED_NAMES = (".mcp.json",)
# A YAML mapping key at any indentation, bare or quoted.
_KEY = re.compile(r"""^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z][A-Za-z0-9_-]*))\s*:\s*(.*)$""")


def _frontmatter(text: str) -> list[str]:
    lines = text.lstrip("﻿").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() != "---":
        return []
    for end in range(1, len(lines)):
        if lines[end].strip() in {"---", "..."}:
            return lines[1:end]
    return lines[1:]


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return [name for name in out.split("\0") if name]


def claude_config_violations(root: Path, tracked: list[str]) -> list[str]:
    """Every way the Claude Code configuration under `root` breaks the rules; empty when it is clean."""
    problems: list[str] = []

    agents = root / ".claude" / "agents"
    for path in sorted(agents.rglob("*.md")):
        label = path.relative_to(agents).as_posix()
        for line in _frontmatter(path.read_text(encoding="utf-8-sig")):
            match = _KEY.match(line)
            if match is None:
                continue
            key = match.group(1) or match.group(2) or match.group(3)
            value = match.group(4).strip().strip("'\"")
            if key.lower() in _FORBIDDEN_AGENT_KEYS:
                problems.append(f"{label}: frontmatter declares '{key}'")
            if key.lower() == "permissionmode" and value not in _ALLOWED_PERMISSION_MODES:
                problems.append(f"{label}: permissionMode '{value}' is not default or plan")

    settings = root / ".claude" / "settings.json"
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except ValueError:
            problems.append("settings.json is not valid JSON")
        else:
            if isinstance(data, dict):
                for key in data:
                    if key.lower() in _FORBIDDEN_SETTINGS_KEYS:
                        problems.append(f"settings.json declares '{key}'")

    for name in tracked:
        normalised = name.replace("\\", "/")
        if (
            normalised in _FORBIDDEN_TRACKED_FILES
            or normalised.startswith(_FORBIDDEN_TRACKED_PREFIXES)
            or normalised.rsplit("/", 1)[-1] in _FORBIDDEN_TRACKED_NAMES
        ):
            problems.append(f"'{normalised}' is tracked by git")
    return problems


def test_committed_claude_configuration_is_clean():
    assert claude_config_violations(REPO_ROOT, tracked_files()) == []


def _copy_agents(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / ".claude" / "agents", tmp_path / ".claude" / "agents")
    return tmp_path


def _worker(root: Path) -> Path:
    return root / ".claude" / "agents" / "coypu-worker.md"


def _inject(root: Path, extra: str, *, encoding: str = "utf-8") -> Path:
    """Add `extra` to the worker's frontmatter, right after its `color:` line."""
    agent = _worker(root)
    text = agent.read_text(encoding="utf-8")
    assert "color: blue\n" in text
    agent.write_text(text.replace("color: blue\n", "color: blue\n" + extra, 1), encoding=encoding)
    return agent


def test_a_clean_temporary_copy_of_the_agents_passes(tmp_path):
    assert claude_config_violations(_copy_agents(tmp_path), []) == []


def test_agent_declaring_hooks_fails(tmp_path):
    root = _copy_agents(tmp_path)
    _inject(root, "hooks:\n  PreToolUse: []\n")
    problems = claude_config_violations(root, [])
    assert any("coypu-worker.md" in p and "hooks" in p for p in problems), problems


def test_agent_declaring_mcp_servers_fails(tmp_path):
    root = _copy_agents(tmp_path)
    _inject(root, "mcpServers:\n  x: {}\n")
    assert any("mcpServers" in p for p in claude_config_violations(root, []))


@pytest.mark.parametrize("declaration", ['"hooks":', "'hooks':", '"mcpServers":', "  hooks:", "HOOKS:"])
def test_quoted_or_indented_forbidden_keys_are_caught(tmp_path, declaration):
    root = _copy_agents(tmp_path)
    _inject(root, f"{declaration}\n  x: []\n")
    assert claude_config_violations(root, []), declaration


@pytest.mark.parametrize(
    ("mode", "bad"),
    [("default", False), ("plan", False), ("acceptEdits", True), ("bypassPermissions", True)],
)
def test_agent_permission_mode(tmp_path, mode, bad):
    root = _copy_agents(tmp_path)
    _inject(root, f"permissionMode: {mode}\n")
    problems = claude_config_violations(root, [])
    assert bool(problems) is bad, problems


def test_a_quoted_permission_mode_key_is_caught(tmp_path):
    root = _copy_agents(tmp_path)
    _inject(root, '"permissionMode": bypassPermissions\n')
    assert any("permissionMode" in p for p in claude_config_violations(root, []))


def test_a_byte_order_mark_before_the_frontmatter_does_not_hide_it(tmp_path):
    root = _copy_agents(tmp_path)
    agent = _inject(root, "hooks:\n  Stop: []\n", encoding="utf-8-sig")
    assert agent.read_bytes().startswith(b"\xef\xbb\xbf")
    assert any("hooks" in p for p in claude_config_violations(root, []))


def test_agents_in_subdirectories_are_inspected(tmp_path):
    root = _copy_agents(tmp_path)
    nested = root / ".claude" / "agents" / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "x.md").write_text("---\nname: x\nhooks:\n  Stop: []\n---\nbody\n", encoding="utf-8")
    problems = claude_config_violations(root, [])
    assert any("sub/deeper/x.md" in p and "hooks" in p for p in problems), problems


def test_frontmatter_after_leading_blank_lines_is_inspected(tmp_path):
    root = _copy_agents(tmp_path)
    (root / ".claude" / "agents" / "y.md").write_text("\n\n---\nhooks:\n  Stop: []\n---\n", encoding="utf-8")
    assert any("y.md" in p for p in claude_config_violations(root, []))


@pytest.mark.parametrize(
    "key", ["hooks", "mcpServers", "apiKeyHelper", "enableAllProjectMcpServers", "statusLine"]
)
def test_command_running_keys_in_a_committed_settings_file_fail(tmp_path, key):
    root = _copy_agents(tmp_path)
    settings = root / ".claude" / "settings.json"
    settings.write_text(json.dumps({key: {}}), encoding="utf-8")
    assert any(f"settings.json declares '{key}'" in p for p in claude_config_violations(root, [])), key
    settings.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
    assert claude_config_violations(root, []) == []


def test_a_settings_file_with_a_bom_is_still_inspected(tmp_path):
    root = _copy_agents(tmp_path)
    (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8-sig")
    assert any("hooks" in p for p in claude_config_violations(root, []))


@pytest.mark.parametrize("name", [".mcp.json", "sub/.mcp.json"])
def test_a_committed_mcp_json_fails(tmp_path, name):
    root = _copy_agents(tmp_path)
    assert any(name in p for p in claude_config_violations(root, [name]))


@pytest.mark.parametrize(
    "name",
    [
        ".claude/settings.local.json",
        ".claude/worktrees/a/file.txt",
        ".claude/agent-memory-local/coypu-worker/MEMORY.md",
    ],
)
def test_machine_local_claude_state_must_not_be_tracked(tmp_path, name):
    root = _copy_agents(tmp_path)
    assert any(name in p for p in claude_config_violations(root, [name]))
    assert claude_config_violations(root, [".claude/agents/coypu-worker.md"]) == []
