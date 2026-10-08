"""ČÚZK INSPIRE ATOM download client: the service feed as a sheet index, selection by corridor envelope,
and resumable, validated downloads with a manifest (see docs/data-contracts/cuzk-atom.md).

Every service feed lists one entry per SM5 map sheet with its code, name, `updated` stamp and outline
(`georss:polygon`, `latitude longitude` pairs). Each entry links a per-sheet dataset feed, which links the one
ZIP on the file server. All network access of the ČÚZK integration lives here; `requests`, `shapely` and
`defusedxml` come from the optional `gis` extra and are imported inside the functions that need them.
Feeds arrive from the network, so every one is parsed with `defusedxml`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import numpy as np

from coypu_builder import __version__
from coypu_builder.io.gis.envelope import _require

if TYPE_CHECKING:
    from coypu_builder.domain.crs import ProjectCRS

MANIFEST_NAME = "cuzk_manifest.json"
MAX_CONCURRENCY = 4
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_TIMEOUT_S = (10.0, 120.0)
_CHUNK = 1 << 16
_INDEX_CACHE_VERSION = 1
_ATOM = "{http://www.w3.org/2005/Atom}"
_GEORSS = "{http://www.georss.org/georss}"
_DLS = "{http://inspire.ec.europa.eu/schemas/inspire_dls/1.0}"
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)\Z")
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_sleep = time.sleep

# Every URL named by a feed, and every redirect hop, must pass `_check_url`: feeds carry no checksum, so TLS
# to a ČÚZK host is the only proof that downloaded bytes are ČÚZK's. Tests widen these to reach a local one.
ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
ALLOWED_HOSTS: tuple[str, ...] = ("cuzk.gov.cz", "cuzk.cz")  # each host itself and its subdomains


class CuzkDataset(StrEnum):
    DMR5G = "DMR5G"  # LAZ, ground points, S-JTSK
    DMR4G = "DMR4G-TIFF"  # GeoTIFF, 5 m grid, S-JTSK
    DMP1G = "DMP1G"  # LAZ, surface incl. buildings and vegetation, S-JTSK
    ORTOFOTO = "ORTOFOTO"  # JPEG + world files

    @classmethod
    def parse(cls, text: str) -> CuzkDataset:
        """Accepts a member name (`DMR4G`) or value (`DMR4G-TIFF`), case-insensitively."""
        key = text.strip().upper()
        for member in cls:
            if key in (member.name, member.value.upper()):
                return member
        raise ValueError(f"unknown ČÚZK dataset '{text}'; choose one of {', '.join(m.name for m in cls)}")


SERVICE_FEEDS: dict[CuzkDataset, str] = {
    CuzkDataset.DMR5G: "https://atom.cuzk.gov.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml",
    CuzkDataset.DMR4G: "https://atom.cuzk.gov.cz/DMR4G-SJTSK-TIFF/DMR4G-SJTSK-TIFF.xml",
    CuzkDataset.DMP1G: "https://atom.cuzk.gov.cz/DMP1G-SJTSK/DMP1G-SJTSK.xml",
    CuzkDataset.ORTOFOTO: "https://atom.cuzk.gov.cz/ORTOFOTO/ORTOFOTO.xml",
}


class CuzkError(RuntimeError):
    """Base class of every error this module raises on purpose."""


class CuzkFeedError(CuzkError):
    """A feed is malformed or unsafe."""


class CuzkHTTPError(CuzkError):
    """The server answered with a status that must not be retried (4xx)."""

    def __init__(self, url: str, status: int):
        super().__init__(f"HTTP {status} for {url}")
        self.url = url
        self.status = status


class CuzkDownloadError(CuzkError):
    """A download failed or did not match its expected size."""


class CuzkSizeLimitError(CuzkError):
    """The selection is larger than `max_total_mb`."""


class _Retryable(Exception):
    """A transient failure (5xx, timeout, dropped connection, short body); retried with backoff."""


@dataclass(frozen=True)
class SheetEntry:
    dataset: CuzkDataset
    code: str  # e.g. "KRAV82"
    name: str  # e.g. "Kralupy nad Vltavou 8-2", from the entry title
    edition: str | None  # orthophoto only, e.g. "WRTO24.2025"; None for terrain
    dataset_id: str  # inspire_dls:spatial_dataset_identifier_code
    updated: datetime
    outline_lonlat: tuple[tuple[float, float], ...]  # closed ring, (longitude, latitude)
    dataset_feed_url: str

    @property
    def stem(self) -> str:
        return f"{self.code}.{self.edition}" if self.edition else self.code


@dataclass(frozen=True)
class SheetFile:
    sheet: SheetEntry
    url: str
    length: int  # bytes, from the dataset feed's link
    media_type: str


@dataclass(frozen=True)
class DownloadRecord:  # one manifest entry
    dataset: str
    code: str
    edition: str | None
    url: str
    file: str  # relative to the output directory
    length: int
    sha256: str
    etag: str | None
    last_modified: str | None
    feed_updated: str  # ISO 8601
    downloaded_at: str  # ISO 8601 UTC


def default_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "COYPU Builder" / "cache" / "cuzk-atom"
    return Path.home() / ".cache" / "coypu-builder" / "cuzk-atom"


def user_agent() -> str:
    return f"COYPU-Builder/{__version__} (CUZK ATOM client)"


# --- HTTP ---------------------------------------------------------------------------------------------------


def _session():
    requests = _require("requests")
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    return session


def _request(session, method: str, url: str, *, headers=None, stream: bool = False, ok=(200,)):
    """One request with redirects followed by hand, each hop checked before it is sent. Statuses in `ok`
    are returned; 5xx and transport failures raise `_Retryable`; any other status is final."""
    requests = _require("requests")
    transient = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        _check_url(current, "request URL")
        try:
            response = session.request(
                method, current, headers=headers, stream=stream, timeout=_TIMEOUT_S, allow_redirects=False
            )
        except transient as exc:
            raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
        status = response.status_code
        if status in ok:
            return response
        location = response.headers.get("Location")
        response.close()
        if status in _REDIRECT_STATUSES and location:
            current = urljoin(current, location)
            continue
        if status >= 500:
            raise _Retryable(f"HTTP {status}")
        raise CuzkHTTPError(current, status)
    raise CuzkError(f"{url}: more than {_MAX_REDIRECTS} redirects")


def _retrying(call: Callable[[], Any], what: str) -> Any:
    last: _Retryable | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return call()
        except _Retryable as exc:
            last = exc
            if attempt < _MAX_ATTEMPTS:
                _sleep(_BACKOFF_BASE_S * 2 ** (attempt - 1))
    raise CuzkError(f"{what}: giving up after {_MAX_ATTEMPTS} attempts ({last})")


def _get(session, url: str, headers=None, ok=(200,)):
    return _retrying(lambda: _request(session, "GET", url, headers=headers, ok=ok), url)


def _check_url(url: str, what: str) -> str:
    """Accept only an https URL (see `ALLOWED_SCHEMES`) on a ČÚZK host (see `ALLOWED_HOSTS`), without
    userinfo. The host must be an allowed name itself or end with `.` plus one, so `cuzk.gov.cz.evil.com`
    and `cuzk.gov.cz@evil.com` fail."""

    def refuse(reason: str) -> CuzkFeedError:
        return CuzkFeedError(f"{what}: refused URL {url[:80]!r} ({reason})")

    if not url.isascii() or any(ch.isspace() or ch == "\\" for ch in url):
        raise refuse("unexpected characters")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise refuse("unparsable") from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise refuse(f"scheme must be {' or '.join(ALLOWED_SCHEMES)}")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise refuse("userinfo is not allowed")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
        raise refuse("not a ČÚZK host")
    return url


def _atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"~{uuid4().hex[:12]}.tmp")
    try:
        if isinstance(data, str):
            tmp.write_text(data, encoding="utf-8")
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- feed parsing -------------------------------------------------------------------------------------------


def _text(entry, tag: str) -> str | None:
    child = entry.find(tag)
    return child.text.strip() if child is not None and child.text and child.text.strip() else None


def _split_identifier(identifier: str) -> tuple[str, str | None]:
    """`CZ-00025712-CUZK_ORTOFOTO_WRTO24.2025.KRAV82` -> (`KRAV82`, `WRTO24.2025`)."""
    token = identifier.rsplit("_", 1)[-1]
    edition, dot, code = token.rpartition(".")
    return (code, edition) if dot else (token, None)


def _parse_polygon(text: str, who: str) -> tuple[tuple[float, float], ...]:
    """`georss:polygon` is `lat lon lat lon ...`; the result is (longitude, latitude), closed."""
    try:
        values = [float(v) for v in text.split()]
    except ValueError as exc:
        raise CuzkFeedError(f"{who}: unreadable georss:polygon") from exc
    if len(values) % 2 or len(values) < 6:
        raise CuzkFeedError(f"{who}: georss:polygon needs at least three coordinate pairs")
    ring = [(lon, lat) for lat, lon in zip(values[0::2], values[1::2], strict=True)]
    if any(not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) for lon, lat in ring):
        raise CuzkFeedError(f"{who}: georss:polygon is out of range for latitude/longitude")
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return tuple(ring)


def _parse_entry(entry, dataset: CuzkDataset) -> SheetEntry:
    identifier = _text(entry, _DLS + "spatial_dataset_identifier_code")
    who = identifier or _text(entry, _ATOM + "id") or "<entry>"
    title = _text(entry, _ATOM + "title")
    updated = _text(entry, _ATOM + "updated")
    polygon = _text(entry, _GEORSS + "polygon")
    if not (identifier and title and updated and polygon):
        raise CuzkFeedError(f"{who}: feed entry lacks identifier, title, updated or polygon")
    code, edition = _split_identifier(identifier)
    for part in (code, edition):
        if part is not None and not _SAFE_NAME.match(part):
            raise CuzkFeedError(f"{who}: unsafe sheet code or edition {part!r}")
    try:
        stamp = datetime.fromisoformat(updated)
    except ValueError as exc:
        raise CuzkFeedError(f"{who}: unreadable updated stamp {updated!r}") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    feed_url = None
    for link in entry.findall(_ATOM + "link"):
        if link.get("rel") == "alternate" and link.get("href"):
            feed_url = link.get("href")
            break
    if feed_url is None:
        raise CuzkFeedError(f"{who}: entry has no dataset feed link")
    return SheetEntry(
        dataset=dataset,
        code=code,
        name=title.rpartition("mapový list:")[2].strip() or title,
        edition=edition,
        dataset_id=identifier,
        updated=stamp,
        outline_lonlat=_parse_polygon(polygon, who),
        dataset_feed_url=_check_url(feed_url, f"{who}: dataset feed link"),
    )


def _iter_entries(content: bytes, what: str):
    """Yield every Atom `entry` of a feed, parsed safely: DTDs, entity expansion and external references
    are refused."""
    defused = _require("defusedxml")
    from defusedxml import ElementTree as safe_et

    try:
        for _event, element in safe_et.iterparse(BytesIO(content), events=("end",), forbid_dtd=True):
            if element.tag == _ATOM + "entry":
                yield element
                element.clear()
    except defused.DefusedXmlException as exc:
        raise CuzkFeedError(f"{what}: refused unsafe XML ({exc})") from exc
    except safe_et.ParseError as exc:
        raise CuzkFeedError(f"{what}: malformed XML ({exc})") from exc


def _parse_service_feed(content: bytes, dataset: CuzkDataset) -> list[SheetEntry]:
    entries = [_parse_entry(e, dataset) for e in _iter_entries(content, f"{dataset.value} service feed")]
    if not entries:
        raise CuzkFeedError(f"{dataset.value} service feed contains no entries")
    return entries


def _parse_dataset_feed(content: bytes, sheet: SheetEntry) -> tuple[str, int, str]:
    for entry in _iter_entries(content, f"dataset feed {sheet.dataset_id}"):
        for link in entry.findall(_ATOM + "link"):
            href, length = link.get("href"), link.get("length")
            if link.get("rel") == "alternate" and href and length:
                try:
                    size = int(length)
                except ValueError as exc:
                    raise CuzkFeedError(f"{sheet.dataset_id}: unreadable length {length!r}") from exc
                if size <= 0:
                    raise CuzkFeedError(f"{sheet.dataset_id}: non-positive length {size}")
                return _check_url(href, f"{sheet.dataset_id}: file link"), size, link.get("type") or ""
    raise CuzkFeedError(f"dataset feed {sheet.dataset_id} links no file with a length")


# --- sheet index --------------------------------------------------------------------------------------------


def _entry_to_json(e: SheetEntry) -> dict[str, Any]:
    return {
        "dataset": e.dataset.value,
        "code": e.code,
        "name": e.name,
        "edition": e.edition,
        "dataset_id": e.dataset_id,
        "updated": e.updated.isoformat(),
        "outline": [list(p) for p in e.outline_lonlat],
        "feed": e.dataset_feed_url,
    }


def _entry_from_json(d: Mapping[str, Any]) -> SheetEntry:
    return SheetEntry(
        dataset=CuzkDataset(d["dataset"]),
        code=d["code"],
        name=d["name"],
        edition=d["edition"],
        dataset_id=d["dataset_id"],
        updated=datetime.fromisoformat(d["updated"]),
        outline_lonlat=tuple((float(lon), float(lat)) for lon, lat in d["outline"]),
        dataset_feed_url=d["feed"],
    )


def _read_index_cache(path: Path, feed_url: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != _INDEX_CACHE_VERSION or data.get("feed_url") != feed_url:
            return None
        data["parsed"] = [_entry_from_json(d) for d in data["entries"]]
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_sheet_index(dataset: CuzkDataset, cache_dir: Path, *, refresh: bool = False) -> list[SheetEntry]:
    """The sheet index of `dataset`. The service feed is fetched with `If-None-Match` / `If-Modified-Since`
    against the validators stored with the parsed index, so an unchanged feed costs one 304 and no parsing.
    `refresh=True` ignores the cache and fetches the whole feed."""
    cache_dir = Path(cache_dir)
    url = SERVICE_FEEDS[dataset]
    cache_path = cache_dir / f"{dataset.value}.index.json"
    cached = None if refresh else _read_index_cache(cache_path, url)
    headers = {}
    if cached is not None:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    session = _session()
    try:
        response = _get(session, url, headers=headers, ok=(200, 304))
        try:
            if response.status_code == 304:
                if cached is None:
                    raise CuzkFeedError(f"{url}: 304 Not Modified without a cached index")
                return cached["parsed"]
            entries = _parse_service_feed(response.content, dataset)
            etag, last_modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        finally:
            response.close()
    finally:
        session.close()

    payload = {
        "version": _INDEX_CACHE_VERSION,
        "feed_url": url,
        "etag": etag,
        "last_modified": last_modified,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "entries": [_entry_to_json(e) for e in entries],
    }
    _atomic_write(cache_path, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return entries


def select_sheets(index: Sequence[SheetEntry], envelope, project_crs: ProjectCRS) -> list[SheetEntry]:
    """The sheets whose outline overlaps `envelope` (a shapely geometry in `project_crs`), in index order.
    Outlines are stored (longitude, latitude) and transformed with x = longitude first; sheets that merely
    touch the envelope's boundary are not selected."""
    shapely = _require("shapely")
    entries = list(index)
    if not entries or envelope.is_empty:
        return []
    counts = [len(e.outline_lonlat) for e in entries]
    flat = np.array([p for e in entries for p in e.outline_lonlat], dtype=np.float64)
    xs, ys = project_crs.from_crs("EPSG:4326", flat[:, 0], flat[:, 1])
    ends = np.cumsum(counts)
    starts = ends - np.asarray(counts)
    min_x, min_y, max_x, max_y = envelope.bounds
    selected = []
    for entry, a, b in zip(entries, starts, ends, strict=True):
        x, y = xs[a:b], ys[a:b]
        if not (np.isfinite(x).all() and np.isfinite(y).all()):
            continue
        if x.max() < min_x or x.min() > max_x or y.max() < min_y or y.min() > max_y:
            continue
        outline = shapely.make_valid(shapely.Polygon(np.column_stack([x, y])))
        if outline.intersects(envelope) and not outline.touches(envelope):
            selected.append(entry)
    return selected


# --- file resolution ----------------------------------------------------------------------------------------


def resolve_files(sheets: Sequence[SheetEntry], cache_dir: Path) -> list[SheetFile]:
    """Fetch each sheet's dataset feed (conditionally, through `cache_dir`) and read the file link."""
    cache_dir = Path(cache_dir)
    session = _session()
    try:
        return [_resolve_one(session, sheet, cache_dir) for sheet in sheets]
    finally:
        session.close()


def _resolve_one(session, sheet: SheetEntry, cache_dir: Path) -> SheetFile:
    path = cache_dir / sheet.dataset.value / "feeds" / f"{sheet.stem}.json"
    cached: dict[str, Any] | None = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("url") == sheet.dataset_feed_url and data.get("length"):
            cached = data
    except (OSError, ValueError):
        pass
    headers = {}
    if cached is not None:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    response = _get(session, sheet.dataset_feed_url, headers=headers, ok=(200, 304))
    try:
        if response.status_code == 304 and cached is not None:
            file_url, length, media_type = cached["file_url"], int(cached["length"]), cached["media_type"]
        else:
            file_url, length, media_type = _parse_dataset_feed(response.content, sheet)
            _atomic_write(
                path,
                json.dumps(
                    {
                        "url": sheet.dataset_feed_url,
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                        "file_url": file_url,
                        "length": length,
                        "media_type": media_type,
                    }
                ),
            )
    finally:
        response.close()
    return SheetFile(sheet=sheet, url=file_url, length=length, media_type=media_type)


# --- download -----------------------------------------------------------------------------------------------


def _repository_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def ensure_outside_repository(path: Path) -> None:
    """Downloads are ZIPs that no `.gitignore` rule covers, so they must never land in the checkout."""
    root = _repository_root()
    if root is not None and Path(path).resolve().is_relative_to(root):
        raise CuzkDownloadError(f"refusing to download into the repository ({root}): {path}")


def read_manifest(out_dir: Path) -> list[DownloadRecord]:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.exists():
        return []
    try:
        return [DownloadRecord(**item) for item in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError) as exc:
        raise CuzkDownloadError(f"cannot read {path}: {exc}") from exc


@dataclass(frozen=True)
class _Probe:
    etag: str | None
    last_modified: str | None
    length: int | None


def _probe(session, url: str) -> _Probe:
    def call() -> _Probe:
        response = _request(session, "HEAD", url, ok=(200, 405, 501))
        try:
            if response.status_code != 200:  # the server does not answer HEAD: read the headers of a GET
                response.close()
                response = _request(session, "GET", url, stream=True)
            length = response.headers.get("Content-Length")
            return _Probe(
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
                int(length) if length and length.isdigit() else None,
            )
        finally:
            response.close()

    return _retrying(call, url)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


class _Manifest:
    def __init__(self, out_dir: Path):
        self.path = out_dir / MANIFEST_NAME
        self._lock = threading.Lock()
        self._records = {r.file: r for r in read_manifest(out_dir)}

    def get(self, file: str) -> DownloadRecord | None:
        with self._lock:
            return self._records.get(file)

    def put(self, record: DownloadRecord) -> None:
        with self._lock:
            self._records[record.file] = record
            payload = json.dumps(
                [asdict(r) for _file, r in sorted(self._records.items())], ensure_ascii=False, indent=2
            )
            _atomic_write(self.path, payload + "\n")


def _validator_path(part: Path) -> Path:
    return part.with_name(part.name + ".validator")


def _discard_part(part: Path) -> None:
    part.unlink(missing_ok=True)
    _validator_path(part).unlink(missing_ok=True)


def _prepare_part(part: Path, probe: _Probe, expected: int) -> int:
    """Bytes of `part` that may be resumed. A partial file is only continued when the validators recorded
    beside it match the server's current ones, so bytes of two versions are never joined."""
    current = {"etag": probe.etag, "last_modified": probe.last_modified}
    have = part.stat().st_size if part.exists() else 0
    if have:
        try:
            stored = json.loads(_validator_path(part).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if stored != current or not any(current.values()) or have >= expected:
            _discard_part(part)
            have = 0
    if not have:
        _atomic_write(_validator_path(part), json.dumps(current))
        part.write_bytes(b"")
    return have


def _transfer(session, f: SheetFile, part: Path, probe: _Probe, progress, name: str):
    """One attempt to fill `part`, resuming from what is already there. Returns the response headers."""
    requests = _require("requests")
    expected = f.length
    have = _prepare_part(part, probe, expected)
    headers: dict[str, str] = {}
    if have:
        headers["Range"] = f"bytes={have}-"
        if probe.etag:
            headers["If-Range"] = probe.etag
    response = _request(session, "GET", f.url, headers=headers, stream=True, ok=(200, 206))
    try:
        if response.status_code == 206:
            match = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
            if not have or not match or int(match.group(1)) != have:
                _discard_part(part)
                raise _Retryable("server answered a range request with an unusable 206")
            total = int(match.group(3)) if match.group(3) != "*" else expected
            mode = "ab"
        else:
            have, mode = 0, "wb"
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else expected
        if total != expected:
            _discard_part(part)
            raise CuzkDownloadError(
                f"{name}: server reports {total} bytes but the feed says {expected}; refusing the file"
            )
        done = have
        try:
            with part.open(mode) as handle:
                for chunk in response.iter_content(_CHUNK):
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(name, done, expected)
        except requests.RequestException as exc:
            raise _Retryable(f"{type(exc).__name__}: {exc}") from exc
        size = part.stat().st_size
        if size < expected:
            raise _Retryable(f"short transfer: {size} of {expected} bytes")
        if size != expected:
            _discard_part(part)
            raise CuzkDownloadError(f"{name}: received {size} bytes, expected {expected}")
        return response.headers
    finally:
        response.close()


def _download_one(f: SheetFile, out_dir: Path, manifest: _Manifest, progress) -> DownloadRecord:
    sheet = f.sheet
    file = f"{sheet.dataset.value}/{sheet.stem}.zip"
    final = out_dir / sheet.dataset.value / f"{sheet.stem}.zip"
    part = final.with_name(final.name + ".part")
    name = file
    session = _session()
    try:
        probe = _probe(session, f.url)
        if probe.length is not None and probe.length != f.length:
            raise CuzkDownloadError(
                f"{name}: server reports {probe.length} bytes but the feed says {f.length}; refusing the file"
            )
        existing = manifest.get(file)
        if (
            existing is not None
            and existing.url == f.url
            and existing.length == f.length
            and final.is_file()
            and final.stat().st_size == f.length
            and (
                existing.etag == probe.etag
                if probe.etag is not None
                else probe.last_modified is not None and existing.last_modified == probe.last_modified
            )
        ):
            if progress is not None:
                progress(name, f.length, f.length)
            return existing

        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            headers = _retrying(lambda: _transfer(session, f, part, probe, progress, name), name)
        except BaseException:
            if part.exists() and part.stat().st_size == 0:
                _discard_part(part)
            raise
        digest = _sha256(part)
        os.replace(part, final)
        _validator_path(part).unlink(missing_ok=True)
    finally:
        session.close()

    record = DownloadRecord(
        dataset=sheet.dataset.value,
        code=sheet.code,
        edition=sheet.edition,
        url=f.url,
        file=file,
        length=f.length,
        sha256=digest,
        etag=headers.get("ETag") or probe.etag,
        last_modified=headers.get("Last-Modified") or probe.last_modified,
        feed_updated=sheet.updated.isoformat(),
        downloaded_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    manifest.put(record)
    return record


def download(
    files: Sequence[SheetFile],
    out_dir: Path,
    *,
    max_total_mb: float = 500.0,
    concurrency: int = 2,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[DownloadRecord]:
    """Download `files` into `out_dir/<dataset>/`, record them in `out_dir/cuzk_manifest.json` and return one
    record per file, in order. A file whose manifest entry still matches the server's ETag and length is not
    fetched again. `progress(name, bytes_done, bytes_total)` is called from worker threads.

    Raises `CuzkSizeLimitError` before any request when the selection exceeds `max_total_mb` (decimal MB)."""
    out_dir = Path(out_dir)
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    ensure_outside_repository(out_dir)
    if not files:
        return []
    total = sum(f.length for f in files)
    if total > max_total_mb * 1_000_000:
        raise CuzkSizeLimitError(
            f"{len(files)} file(s) total {total / 1e6:.2f} MB, over the limit of {max_total_mb:g} MB; "
            "raise the limit explicitly (--max-total-mb) to download them"
        )
    names = [f"{f.sheet.dataset.value}/{f.sheet.stem}.zip" for f in files]
    if len(set(names)) != len(names):
        raise ValueError("the selection lists the same sheet twice")

    manifest = _Manifest(out_dir)
    workers = min(concurrency, MAX_CONCURRENCY, len(files))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cuzk-download") as pool:
        futures = [pool.submit(_download_one, f, out_dir, manifest, progress) for f in files]
        failures: list[Exception] = []
        for future in futures:
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - collected, reported below
                if not future.cancelled():
                    failures.append(exc)
                for pending in futures:
                    pending.cancel()
    if failures:
        first = failures[0]
        if len(failures) == 1:
            raise first
        raise CuzkDownloadError(f"{len(failures)} downloads failed; first: {first}") from first
    return [future.result() for future in futures]
