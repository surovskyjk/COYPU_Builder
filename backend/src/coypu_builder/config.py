"""Per-user configuration: `config.toml` (ADR 0010, decision 4).

The file is only ever read. A missing file gives the defaults; a malformed one gives the defaults plus a
warning, never an exception.

    [plugins]
    allow = ["example-reader"]        # entry-point names that may be imported
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

APP_DIR_WINDOWS = "COYPU Builder"
APP_DIR_POSIX = "coypu-builder"
CONFIG_FILE = "config.toml"


@dataclass(frozen=True)
class UserConfig:
    plugins_allow: tuple[str, ...] = ()


def _is_windows() -> bool:
    return sys.platform == "win32"


def config_path() -> Path:
    """Windows: `%APPDATA%\\COYPU Builder\\config.toml`; elsewhere
    `$XDG_CONFIG_HOME/coypu-builder/config.toml`, defaulting to `~/.config/...`."""
    if _is_windows():
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / APP_DIR_WINDOWS / CONFIG_FILE
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return root / APP_DIR_POSIX / CONFIG_FILE


def load_user_config(path: Path | None = None) -> tuple[UserConfig, tuple[str, ...]]:
    """(config, warnings). `path=None` reads `config_path()`."""
    target = Path(path) if path is not None else config_path()
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return UserConfig(), ()
    except OSError as exc:
        return UserConfig(), (f"cannot read {target}: {exc}; using the defaults",)
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return UserConfig(), (f"{target} is not valid TOML ({exc}); using the defaults",)

    warnings: list[str] = []
    allow: tuple[str, ...] = ()
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        warnings.append(f"{target}: [plugins] must be a table; ignoring it")
    elif "allow" in plugins:
        names = plugins["allow"]
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            allow = tuple(names)
        else:
            warnings.append(f"{target}: plugins.allow must be a list of strings; ignoring it")
    return UserConfig(plugins_allow=allow), tuple(warnings)
