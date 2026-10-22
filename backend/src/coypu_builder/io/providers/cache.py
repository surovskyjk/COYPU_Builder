"""The per-user provider cache: where online sources put what they download (T-133, ADR 0009 decision 7).

One directory per provider, `%LOCALAPPDATA%\\COYPU Builder\\cache\\providers\\<provider_id>\\` on Windows and
`~/.cache/coypu-builder/providers/<provider_id>/` elsewhere. Never inside the repository, and never in the
user's data folder: a project keeps the `SourceDataset` record, not the bytes (ADR 0009 decision 8).

Each directory holds an `index.json` listing the files the cache is responsible for, with size, SHA-256,
source URL, validators and the time they were last used. The cache is bounded (default 5 GB in total): when
it is over the cap, the files used longest ago are deleted first, except the files of a source that is
still stored (pinned, see `ProviderCache.pin`) and the files of the fetch in progress. A file that is
missing from the disk simply drops out of the index, and the whole directory is safe to delete by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

DEFAULT_CAP_BYTES = 5 * 10**9
INDEX_NAME = "index.json"
INDEX_VERSION = 1

_LOCK = threading.RLock()  # index read-modify-write, and the pin table
_PINS: dict[Path, dict[str, frozenset[str]]] = {}  # cache directory -> owner -> pinned files (this process)


def providers_cache_root() -> Path:
    """The folder that holds one sub-folder per provider."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "COYPU Builder" / "cache" / "providers"
    return Path.home() / ".cache" / "coypu-builder" / "providers"


def provider_cache_dir(provider_id: str) -> Path:
    if not provider_id or any(ch in provider_id for ch in '/\\:*?"<>| ') or provider_id.startswith("."):
        raise ValueError(f"'{provider_id}' is not a usable provider id for a cache folder")
    return providers_cache_root() / provider_id


def _repository_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def ensure_outside_repository(path: Path) -> None:
    root = _repository_root()
    if root is not None and Path(path).resolve().is_relative_to(root):
        raise ValueError(f"refusing to keep provider downloads inside the repository ({root}): {path}")


def _safe_relative(file: str) -> str:
    pure = PurePosixPath(file.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or ":" in file:
        raise ValueError(f"'{file}' is not a path inside the cache")
    return pure.as_posix()


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class CacheEntry:
    file: str  # relative to the provider's directory, `/` separators
    size: int
    sha256: str
    url: str
    etag: str | None
    last_modified: str | None
    fetched_at: str  # ISO 8601 UTC
    last_used: float  # seconds since the epoch


_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "file": (str,),
    "size": (int,),
    "sha256": (str,),
    "url": (str,),
    "etag": (str, type(None)),
    "last_modified": (str, type(None)),
    "fetched_at": (str,),
    "last_used": (int, float),
}


def _entry_from_item(item: object) -> CacheEntry | None:
    """An index item as an entry, or None when this cache did not write it: other fields or types, or a file
    name outside the cache (`_safe_relative`). Such items are dropped when the index is read."""
    if not isinstance(item, dict) or set(item) != set(_FIELD_TYPES):
        return None
    for name, kinds in _FIELD_TYPES.items():
        value = item[name]
        if isinstance(value, bool) or not isinstance(value, kinds):
            return None
    try:
        _safe_relative(item["file"])
    except ValueError:
        return None
    return CacheEntry(**item)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class ProviderCache:
    """The cache directory of one provider: its index, the pins and the eviction. Cheap to construct; all
    state is in the directory (and the process-wide pin table), so any number of instances may exist."""

    def __init__(self, directory: Path, cap_bytes: int | None = None):
        cap_bytes = DEFAULT_CAP_BYTES if cap_bytes is None else cap_bytes
        if cap_bytes < 0:
            raise ValueError(f"cap_bytes must be >= 0, got {cap_bytes}")
        ensure_outside_repository(directory)
        self.directory = Path(directory)
        self.cap_bytes = cap_bytes

    @property
    def index_path(self) -> Path:
        return self.directory / INDEX_NAME

    def path(self, file: str) -> Path:
        return self.directory / _safe_relative(file)

    def relative(self, path: Path) -> str:
        """`path` as a cache-relative name; `ValueError` for a file outside this cache."""
        try:
            return Path(path).resolve().relative_to(self.directory.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"'{path}' is not inside the cache {self.directory}") from exc

    # -- index -------------------------------------------------------------------------------------------

    def _load(self) -> dict[str, CacheEntry]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # a damaged index is rebuilt as files are used again; the files stay on disk
        if (
            not isinstance(data, dict)
            or data.get("version") != INDEX_VERSION
            or not isinstance(data.get("files"), list)
        ):
            return {}
        entries: dict[str, CacheEntry] = {}
        for item in data["files"]:
            entry = _entry_from_item(item)
            if entry is not None:
                entries[entry.file] = entry
        return entries

    def _save(self, entries: dict[str, CacheEntry]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "files": [asdict(e) for _file, e in sorted(entries.items())],
        }
        tmp = self.directory / f"~{uuid4().hex[:12]}.tmp"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, self.index_path)
        finally:
            tmp.unlink(missing_ok=True)

    def entries(self) -> dict[str, CacheEntry]:
        """The indexed files that are still on disk, by relative name."""
        with _LOCK:
            return {f: e for f, e in self._load().items() if self.path(f).is_file()}

    def get(self, file: str) -> CacheEntry | None:
        return self.entries().get(_safe_relative(file))

    def verify(self, file: str) -> bool:
        """True only if `file` is indexed, on the disk at the indexed size, with the indexed SHA-256.
        A file that fails is not trusted: the caller fetches it again."""
        entry = self.get(file)
        if entry is None:
            return False
        path = self.path(entry.file)
        try:
            return path.stat().st_size == entry.size and _sha256(path) == entry.sha256
        except OSError:
            return False  # the file vanished between the index and the read

    def record(
        self,
        file: str,
        *,
        url: str,
        sha256: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CacheEntry:
        """Enter (or refresh) the file `file` of this cache in the index. Its size comes from the disk and its
        SHA-256 is computed unless the caller already has it."""
        name = _safe_relative(file)
        path = self.path(name)
        entry = CacheEntry(
            file=name,
            size=path.stat().st_size,
            sha256=sha256 or _sha256(path),
            url=url,
            etag=etag,
            last_modified=last_modified,
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            last_used=_now(),
        )
        with _LOCK:
            entries = self._load()
            entries[name] = entry
            self._save(entries)
        return entry

    def touch(self, files: list[str]) -> None:
        """Mark `files` as used just now, so eviction takes older ones first."""
        names = {_safe_relative(f) for f in files}
        with _LOCK:
            entries = self._load()
            stamp = _now()
            changed = False
            for name in names & entries.keys():
                entry = entries[name]
                entries[name] = CacheEntry(**{**asdict(entry), "last_used": stamp})
                changed = True
            if changed:
                self._save(entries)

    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries().values())

    # -- pins and eviction ---------------------------------------------------------------------------------

    def _key(self) -> Path:
        return self.directory.resolve()

    def pin(self, owner: str, files: list[str]) -> None:
        """Keep `files` safe from eviction for as long as `owner` (a stored source's id) holds them. Pins live
        in this process: a saved project pins its sources again when it is opened."""
        names = frozenset(_safe_relative(f) for f in files)
        with _LOCK:
            _PINS.setdefault(self._key(), {})[owner] = names

    def unpin(self, owner: str) -> None:
        with _LOCK:
            owners = _PINS.get(self._key())
            if owners is not None:
                owners.pop(owner, None)
                if not owners:
                    _PINS.pop(self._key(), None)

    def pinned(self) -> frozenset[str]:
        with _LOCK:
            owners = _PINS.get(self._key(), {})
            return frozenset().union(*owners.values()) if owners else frozenset()

    def evict(self, protect: list[str] | tuple[str, ...] = ()) -> list[str]:
        """Delete files, least recently used first, until the cache is within `cap_bytes`. Pinned files and
        `protect` (the files of the fetch in progress) are never deleted, so a cache can stay over its cap
        when they alone exceed it. Returns the relative names deleted."""
        keep = self.pinned() | {_safe_relative(f) for f in protect}
        with _LOCK:
            entries = {f: e for f, e in self._load().items() if self.path(f).is_file()}
            total = sum(e.size for e in entries.values())
            evicted: list[str] = []
            for name, entry in sorted(entries.items(), key=lambda item: (item[1].last_used, item[0])):
                if total <= self.cap_bytes:
                    break
                if name in keep:
                    continue
                try:
                    self.path(name).unlink()
                except OSError:
                    continue
                total -= entry.size
                evicted.append(name)
                del entries[name]
            self._save(entries)
        return evicted
