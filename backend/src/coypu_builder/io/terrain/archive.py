"""Open a terrain source file, or the one supported file inside a ZIP, for a reader.

ČÚZK delivers each sheet as a ZIP holding a single file. A reader asks for `open_source(path, suffixes)` and
gets a real file on disk: the bare file itself, or the ZIP's only supported member extracted into a private
temporary directory. Members are always written under a fixed name, never under the name stored in the
archive, so a hostile archive cannot choose where its content lands.

Archives are untrusted, so a member is refused before anything is written if it declares more than
`MAX_MEMBER_BYTES`, or compresses better than `MAX_RATIO`:1 beyond a small allowance (a ZIP bomb), and the
declared size is enforced while streaming. Content sniffing reads a bounded head of the member (`peek`) and
never extracts.

`extraction_scope()` makes a member extracted at most once while it is open (an import inspects, then
reads); the extracted files are deleted when the scope ends, and the oldest are dropped early if the scope
holds more than `MAX_SCOPE_BYTES`.
"""

from __future__ import annotations

import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

_ZIP_MAGIC = b"PK\x03\x04"
_CHUNK = 1 << 20
MAX_MEMBER_BYTES = 8 << 30  # refuse a member that declares more than this
MAX_RATIO = 100  # declared / compressed size beyond which a member is refused ...
RATIO_ALLOWANCE_BYTES = 16 << 20  # ... once it also exceeds this much more than the ratio allows
MAX_SCOPE_BYTES = 4 << 30  # extracted bytes one scope keeps on disk before dropping the oldest


@dataclass(frozen=True)
class SourceFile:
    """A readable file on disk, and the name it has in the source (the member's name inside a ZIP)."""

    path: Path
    name: str

    @property
    def stem(self) -> str:
        return Path(self.name).stem


def is_zip(path: Path) -> bool:
    try:
        with Path(path).open("rb") as handle:
            return handle.read(4) == _ZIP_MAGIC
    except OSError:
        return False


def _supported_member(archive: zipfile.ZipFile, path: Path, suffixes: tuple[str, ...]) -> zipfile.ZipInfo:
    wanted = {s.lower() for s in suffixes}
    members = [
        info
        for info in archive.infolist()
        if not info.is_dir()
        and Path(info.filename).suffix.lower() in wanted
        and not info.filename.startswith("__MACOSX/")
        and not Path(info.filename).name.startswith("._")
    ]
    listing = ", ".join(sorted(wanted))
    if not members:
        raise ValueError(f"'{path.name}' holds no {listing} file")
    if len(members) > 1:
        raise ValueError(
            f"'{path.name}' holds {len(members)} {listing} files ({', '.join(m.filename for m in members)}); "
            "a ZIP must hold exactly one, so import the files as separate paths"
        )
    member = members[0]
    if member.flag_bits & 0x1:
        raise ValueError(f"'{member.filename}' in '{path.name}' is encrypted")
    if member.file_size > MAX_MEMBER_BYTES:
        raise ValueError(
            f"'{member.filename}' in '{path.name}' declares {member.file_size} bytes "
            f"(limit {MAX_MEMBER_BYTES}); refused"
        )
    if member.file_size > MAX_RATIO * max(member.compress_size, 1) + RATIO_ALLOWANCE_BYTES:
        raise ValueError(
            f"'{member.filename}' in '{path.name}' expands {member.file_size} bytes from "
            f"{member.compress_size} (more than {MAX_RATIO}:1); refused as a possible ZIP bomb"
        )
    return member


@contextmanager
def _open_archive(path: Path) -> Iterator[zipfile.ZipFile]:
    try:
        with zipfile.ZipFile(path) as archive:
            yield archive
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise ValueError(f"'{path.name}' is not a readable ZIP archive: {exc}") from exc


def peek(path: Path, suffixes: tuple[str, ...], size: int) -> tuple[str, bytes]:
    """(file name, first `size` bytes) of the bare file, or of the ZIP's only supported member. Only that
    much of a member is decompressed."""
    path = Path(path)
    if not is_zip(path):
        with path.open("rb") as handle:
            return path.name, handle.read(size)
    with _open_archive(path) as archive:
        member = _supported_member(archive, path, suffixes)
        try:
            with archive.open(member) as handle:
                return member.filename, handle.read(size)
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"'{member.filename}' in '{path.name}' is damaged: {exc}") from exc


def _extract(archive: zipfile.ZipFile, member: zipfile.ZipInfo, target: Path, path: Path) -> int:
    """Stream `member` to `target`, failing as soon as more bytes arrive than it declared."""
    written = 0
    try:
        with archive.open(member) as source, target.open("wb") as sink:
            while chunk := source.read(_CHUNK):
                written += len(chunk)
                if written > member.file_size:
                    raise ValueError(
                        f"'{member.filename}' in '{path.name}' holds more than the {member.file_size} bytes "
                        "it declares; refused"
                    )
                sink.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise ValueError(f"'{member.filename}' in '{path.name}' is damaged: {exc}") from exc
    return written


@dataclass
class _Entry:
    stamp: tuple[int, int]
    folder: Path
    file: SourceFile
    size: int


@dataclass
class _Scope:
    root: Path
    entries: dict[Path, _Entry] = field(default_factory=dict)
    counter: int = 0

    def total(self) -> int:
        return sum(e.size for e in self.entries.values())

    def drop(self, key: Path) -> None:
        entry = self.entries.pop(key, None)
        if entry is not None:
            _remove(entry.folder)


_SCOPE: ContextVar[_Scope | None] = ContextVar("terrain_extraction_scope", default=None)


def _remove(folder: Path) -> None:
    for child in folder.glob("*"):
        child.unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


def _stamp(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_size


@contextmanager
def extraction_scope() -> Iterator[None]:
    """Within the scope each ZIP member is extracted at most once; everything is deleted on exit. Scopes nest
    (the outermost owns the files)."""
    if _SCOPE.get() is not None:
        yield
        return
    with tempfile.TemporaryDirectory(prefix="coypu-terrain-") as root:
        token = _SCOPE.set(_Scope(Path(root)))
        try:
            yield
        finally:
            _SCOPE.reset(token)


def release(path: Path) -> None:
    """Delete the extraction of the ZIP at `path` held by the current scope, if any."""
    scope = _SCOPE.get()
    if scope is not None:
        scope.drop(Path(path).resolve())


def _scoped(scope: _Scope, path: Path, suffixes: tuple[str, ...]) -> SourceFile:
    key = Path(path).resolve()
    stamp = _stamp(path)
    entry = scope.entries.get(key)
    if entry is not None and entry.stamp == stamp:
        return entry.file
    scope.drop(key)
    with _open_archive(path) as archive:
        member = _supported_member(archive, path, suffixes)
        scope.counter += 1
        folder = scope.root / f"m{scope.counter}"
        folder.mkdir()
        target = folder / f"member{Path(member.filename).suffix.lower()}"
        try:
            size = _extract(archive, member, target, path)
        except BaseException:
            _remove(folder)
            raise
    file = SourceFile(target, Path(member.filename).name)
    scope.entries[key] = _Entry(stamp, folder, file, size)
    while scope.total() > MAX_SCOPE_BYTES and len(scope.entries) > 1:
        scope.drop(next(iter(scope.entries)))
    return file


@contextmanager
def open_source(path: Path, suffixes: tuple[str, ...]) -> Iterator[SourceFile]:
    """Yield the file to read: `path` itself, or the only supported member of the ZIP at `path`, extracted
    to a temporary file (kept until the end of the enclosing `extraction_scope`, else deleted on exit).
    Raises `ValueError` for a ZIP that is damaged, encrypted, too large, too well compressed, or that does
    not hold exactly one supported file."""
    path = Path(path)
    if not is_zip(path):
        yield SourceFile(path, path.name)
        return
    scope = _SCOPE.get()
    if scope is not None:
        yield _scoped(scope, path, suffixes)
        return
    with extraction_scope():
        scope = _SCOPE.get()
        assert scope is not None
        yield _scoped(scope, path, suffixes)
