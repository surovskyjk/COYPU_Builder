"""Discovery records: how an external client finds a running backend and its session token (ADR 0010).

One small JSON file per running backend, in a per-user runtime directory only that user can read:
Windows `%LOCALAPPDATA%\\COYPU Builder\\runtime\\backend-<pid>.json`; Linux
`$XDG_RUNTIME_DIR/coypu-builder/backend-<pid>.json`, falling back to `~/.cache/coypu-builder/runtime/`.
The record holds the session token, so it is user-only: directory `0700` and file `0600` on POSIX; on Windows
the directory carries a protected (non-inherited) ACL that grants only SYSTEM, Administrators and the owner
(OWNER RIGHTS, i.e. the current user), and the file inherits it. A pre-existing Windows directory that
inherits or names anyone else is repaired down to one explicit entry for the user, or refused with
`PermissionError`, in which case `serve` skips the record and still serves. The record is written atomically
and never holds project data.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

_PREFIX = "backend-"
_SUFFIX = ".json"


@dataclass(frozen=True)
class DiscoveryRecord:
    port: int
    pid: int
    protocol: str
    backend_version: str
    token: str = field(repr=False)  # a secret: never in a repr, log line or traceback
    started_at: str  # ISO 8601 UTC


def _base_dir() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "COYPU Builder" / "runtime"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "coypu-builder"
    return Path.home() / ".cache" / "coypu-builder" / "runtime"


_acl_verified: set[Path] = set()


def _icacls(*args: str) -> str:
    try:
        done = subprocess.run(["icacls", *args], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PermissionError(f"cannot inspect the ACL of the runtime directory ({exc})") from exc
    if done.returncode != 0:
        raise PermissionError("cannot inspect or change the ACL of the runtime directory")
    return done.stdout


_ALLOWED_PRINCIPALS = {"system", "administrators", "owner rights", "creator owner"}


def _acl_entries(directory: Path) -> tuple[list[str], bool]:
    """The principals named in the directory's ACL, as `icacls` prints them (with their domain), and whether
    any entry is inherited."""
    listing = _icacls(str(directory))
    names: list[str] = []
    for line in listing.splitlines():
        if ":(" in line:
            names.append(line.replace(str(directory), "", 1).split(":(")[0].strip())
    return names, ":(I)" in listing


def _acl_is_user_only(directory: Path, user: str | None) -> bool:
    """Protected (nothing inherited), and naming nobody but the current user, SYSTEM, Administrators, OWNER
    RIGHTS and CREATOR OWNER."""
    names, inherits = _acl_entries(directory)
    allowed = _ALLOWED_PRINCIPALS | ({user.lower()} if user else set())
    return not inherits and bool(names) and all(n.split(chr(92))[-1].lower() in allowed for n in names)


def _ensure_protected_windows(directory: Path) -> None:
    """A directory made by `mkdir(mode=0o700)` (Python 3.13) already has a protected ACL naming only SYSTEM,
    Administrators and OWNER RIGHTS. One that existed before may inherit from a broader parent or carry an
    explicit grant to someone else (a record holds the session token). Such a directory is repaired: the
    current user gets an explicit grant first so nothing can lock it out, inheritance is removed, and every
    other principal is removed, leaving one explicit entry for the user. If it still fails the check, or the
    ACL cannot be read, `PermissionError` is raised and the caller skips the discovery record."""
    if directory in _acl_verified:
        return
    user = os.environ.get("USERNAME")
    if not _acl_is_user_only(directory, user):
        if not user:
            raise PermissionError("the runtime directory is not user-only and the current user is unknown")
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}{chr(92)}{user}" if domain else user
        _icacls(str(directory), "/grant", f"{principal}:(OI)(CI)F")
        _icacls(str(directory), "/inheritance:r")
        for name in _acl_entries(directory)[0]:
            if name.split(chr(92))[-1].lower() != user.lower():
                _icacls(str(directory), "/remove", name)
        if not _acl_is_user_only(directory, user):
            raise PermissionError(f"the runtime directory {directory} is not user-only; remove it")
    _acl_verified.add(directory)


def runtime_dir() -> Path:
    """The per-user runtime directory, created if missing and readable by this user only."""
    directory = _base_dir()
    # `mode=0o700` is honoured on both platforms: POSIX permission bits, and since Python 3.13 on Windows a
    # protected ACL that grants only SYSTEM, Administrators and OWNER RIGHTS.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform == "win32":
        _ensure_protected_windows(directory)
    else:
        directory.chmod(0o700)
    return directory


def _record_path(pid: int) -> Path:
    return runtime_dir() / f"{_PREFIX}{pid}{_SUFFIX}"


def write_record(record: DiscoveryRecord) -> Path:
    """Atomically publish `record`: a temp file in the same directory is written, flushed and renamed over
    the final name, so a reader never sees a partial file."""
    target = _record_path(record.pid)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{_PREFIX}{record.pid}-", suffix=".tmp", dir=target.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(asdict(record), handle)
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            tmp.chmod(0o600)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return target


def remove_record(pid: int) -> None:
    with contextlib.suppress(FileNotFoundError):
        _record_path(pid).unlink()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    # `os.kill(pid, 0)` terminates the process on Windows, so ask the kernel for the exit code instead.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: the process exists
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _load(path: Path) -> DiscoveryRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return DiscoveryRecord(
            port=int(data["port"]),
            pid=int(data["pid"]),
            protocol=str(data["protocol"]),
            backend_version=str(data["backend_version"]),
            token=str(data["token"]),
            started_at=str(data["started_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def live_records() -> list[DiscoveryRecord]:
    """Records of running backends, newest first. A record whose process is dead is removed; an unreadable
    one is skipped."""
    records: list[DiscoveryRecord] = []
    for path in runtime_dir().glob(f"{_PREFIX}*{_SUFFIX}"):
        record = _load(path)
        if record is None:
            continue
        if not pid_alive(record.pid):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        records.append(record)
    records.sort(key=lambda r: r.started_at, reverse=True)
    return records
