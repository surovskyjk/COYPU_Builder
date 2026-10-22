"""The Copernicus DEM GLO-30 provider: a free global 30 m surface model, used as the generic landscape.

GLO-30 is published as one cloud-optimised GeoTIFF per 1 degree by 1 degree tile on a public bucket. This
provider works out which tiles the query's clip touches and reads **only the window that covers the clip**
from each, through HTTP range requests (the COG's internal tiling means a corridor costs a few hundred
kilobytes of a 30 MB file). Every window is written as a small local GeoTIFF in the provider cache, keyed
by tile name
and window bounds, and then goes through T-131's GeoTIFF reader, which warps it to the project CRS. A
second import of the same clip is answered from the cache without a request.

The data is a **surface model** (it includes vegetation and buildings) and its heights refer to EGM2008
(EPSG:3855); Phase 1 does not transform heights (ADR 0009), and `inspect` says both.

Network access is https only, to the one host of `ALLOWED_HOSTS`. Every URL and every redirect hop is checked
before it is requested, which is why the range reads go through this module's own file object and not
through GDAL's `/vsicurl/` (which follows redirects without asking). The licence is the Copernicus
WorldDEM-30 licence; its notice (article 6) is `ATTRIBUTION`.
"""

from __future__ import annotations

import io
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import numpy as np

from coypu_builder import __version__
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.gis.envelope import _require
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import (
    check_keys,
    clip_from_query,
    clip_to_query,
)
from coypu_builder.io.sources.registry import InspectReport
from coypu_builder.io.sources.runner import fetch_note, provider_context

PROVIDER_ID = "copernicus-glo30"
BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
ALLOWED_HOSTS: tuple[str, ...] = (
    "copernicus-dem-30m.s3.amazonaws.com",
)  # each host itself and its subdomains

LICENCE = "Copernicus WorldDEM-30 licence (COP-DEM-GLO-30-F, Global 30m Full, Free & Open)"
# Article 6(b) of the licence: the notice for adapted data, which a clipped and warped window is.
ATTRIBUTION = (
    "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
    "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved"
)
HORIZONTAL_CRS = "EPSG:4326"
VERTICAL_CRS = "EPSG:3855"
DEFAULT_BUFFER_M = 2000.0
MAX_TILES = 25
WINDOW_PAD_PIXELS = 2  # beyond the clip, so the bilinear warp has its neighbours
BLOCK_BYTES = 128 * 1024  # remote reads are aligned to, and cached in, blocks of this size
MAX_CACHED_BLOCKS = 64
_TIMEOUT_S = (10.0, 60.0)
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_sleep = time.sleep
_FILE_NAME = "tile.tif"  # what GDAL is told it is opening; the opener serves only this name
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # one range response, which is read into memory
MAX_FETCH_BYTES = 1_000_000_000  # what one fetch may receive, in all
MAX_FETCH_REQUESTS = 5000  # what one fetch may send, redirect hops included
MAX_FILE_BYTES = 256 * 1024 * 1024  # the largest remote file a range response may describe
READ_CHUNK = 65536
CLIP_SEGMENT_M = 100.0  # the clip's edges are cut to this length before they are transformed to lon/lat
STALE_PART_S = 3600.0  # a partial window older than this was left by a fetch that did not finish
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")

# Article 6(c) of the licence: the notice whoever distributes the data must include (terrain-providers.md).
LIABILITY_NOTICE = (
    "The organisations in charge of the Copernicus programme by law or by delegation do not incur any "
    "liability for any use of the Copernicus WorldDEM-30"
)

SURFACE_WARNING = (
    "Copernicus GLO-30 is a digital surface model: it includes tree canopy and buildings, so it lies metres "
    "above bare ground where those stand; it is only a generic landscape, not ground"
)
DATUM_WARNING = (
    "heights refer to EGM2008 (EPSG:3855) and are not transformed to the project's vertical datum "
    "(Phase 1): expect a constant offset from ground heights such as ČÚZK's Bpv"
)


# --- tiles  ----------------------------------------------------------------------------------------------


def tile_name(lat0: int, lon0: int) -> str:
    """`Copernicus_DSM_COG_10_N50_00_E014_00_DEM` for the tile whose south-west corner is (50, 14)."""
    ns = "N" if lat0 >= 0 else "S"
    ew = "E" if lon0 >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat0):02d}_00_{ew}{abs(lon0):03d}_00_DEM"


def tile_url(name: str) -> str:
    return f"{BASE_URL}/{name}/{name}.tif"


def _check_url(url: str) -> str:
    """Only an https URL (see `ALLOWED_SCHEMES`) on an allowed host (see `ALLOWED_HOSTS`), without userinfo.
    The host must be an allowed name itself or end with `.` plus one. Raises `ValueError`."""

    def refuse(reason: str) -> ValueError:
        return ValueError(f"Copernicus DEM: refused URL {url[:80]!r} ({reason})")

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
        raise refuse("not a Copernicus DEM host")
    return url


# --- HTTP  -----------------------------------------------------------------------------------------------


class _Missing(Exception):
    """The tile does not exist (HTTP 403/404: GLO-30 publishes no tile over open water), or its window lies
    outside the tile. `status` is the HTTP status, None for the window case."""

    def __init__(self, what: str, status: int | None = None):
        super().__init__(what)
        self.status = status


class _Http:
    """Range reads over `requests`, with every hop checked and every byte counted."""

    def __init__(self) -> None:
        requests = _require("requests")
        self._requests = requests
        self._session = requests.Session()
        self._session.headers["User-Agent"] = f"COYPU-Builder/{__version__} (Copernicus DEM client)"
        self._session.headers["Accept-Encoding"] = "identity"  # the bytes must be the file's own
        self.requests_made = 0
        self.bytes_received = 0

    def close(self) -> None:
        self._session.close()

    def _once(self, url: str, headers: dict[str, str]):
        requests = self._requests
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            _check_url(current)
            if self.requests_made >= MAX_FETCH_REQUESTS:
                raise OSError(
                    f"Copernicus: request limit of {MAX_FETCH_REQUESTS} requests reached; fetch refused"
                )
            self.requests_made += 1
            try:
                response = self._session.request(
                    "GET", current, headers=headers, stream=True, timeout=_TIMEOUT_S, allow_redirects=False
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                raise _Transient(f"{type(exc).__name__}: {exc}") from exc
            status = response.status_code
            location = response.headers.get("Location")
            if status in _REDIRECT_STATUSES and location:
                response.close()
                current = urljoin(current, location)
                continue
            return response
        raise OSError(f"Copernicus DEM: {url}: more than {_MAX_REDIRECTS} redirects")

    def get_range(self, url: str, start: int, end: int) -> tuple[bytes, int]:
        """Bytes `start..end` (inclusive) of `url` and the size of the whole file; retries 5xx and drops."""
        last: _Transient | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self._get_range(url, start, end)
            except _Transient as exc:
                last = exc
                if attempt < _MAX_ATTEMPTS:
                    _sleep(_BACKOFF_BASE_S * 2 ** (attempt - 1))
        raise OSError(f"Copernicus DEM: {url}: giving up after {_MAX_ATTEMPTS} attempts ({last})")

    def _get_range(self, url: str, start: int, end: int) -> tuple[bytes, int]:
        requested = end - start + 1
        if requested > MAX_RESPONSE_BYTES:
            raise OSError(f"Copernicus DEM: {url}: a range of {requested} bytes is over {MAX_RESPONSE_BYTES}")
        response = self._once(url, {"Range": f"bytes={start}-{end}"})
        try:
            status = response.status_code
            if status in (403, 404):
                raise _Missing(url, status)
            if status >= 500:
                raise _Transient(f"HTTP {status}")
            if status == 416:  # the range starts past the end: the file is smaller than we thought
                raise OSError(f"Copernicus DEM: {url}: range {start}-{end} not satisfiable")
            if status != 206:
                raise OSError(
                    f"Copernicus DEM: {url}: HTTP {status} for a range request (the server must answer 206)"
                )
            header = response.headers.get("Content-Range", "")
            match = _CONTENT_RANGE.match(header)
            if match is None:
                raise OSError(f"Copernicus DEM: {url}: unusable Content-Range {header!r}")
            first, last, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if not (first == start and start <= last <= end and last < total <= MAX_FILE_BYTES):
                raise OSError(
                    f"Copernicus DEM: {url}: Content-Range {header!r} does not answer {start}-{end}"
                )
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > requested):
                raise OSError(
                    f"Copernicus DEM: {url}: Content-Length {length!r} is over the {requested} asked"
                )
            wanted = min(end, total - 1) - start + 1
            body = self._read_body(response, url, requested)
            if len(body) != wanted:
                raise _Transient(f"short body: {len(body)} of {wanted} bytes")
            return bytes(body), total
        finally:
            response.close()

    def _read_body(self, response, url: str, limit: int) -> bytearray:
        """The body in chunks, refusing as soon as it passes `limit` or the fetch's byte budget."""
        body = bytearray()
        try:
            for chunk in response.iter_content(READ_CHUNK):
                if len(body) + len(chunk) > limit:
                    raise OSError(f"Copernicus DEM: {url}: the body is longer than the {limit} bytes asked")
                if self.bytes_received + len(body) + len(chunk) > MAX_FETCH_BYTES:
                    raise OSError(
                        f"Copernicus: byte limit of {MAX_FETCH_BYTES} bytes reached; the fetch is refused"
                    )
                body += chunk
        except self._requests.RequestException as exc:
            raise _Transient(f"{type(exc).__name__}: {exc}") from exc
        self.bytes_received += len(body)
        return body


class _Transient(Exception):
    """A failure worth retrying: a 5xx, a timeout, a dropped connection or a short body."""


class _RangeFile(io.RawIOBase):
    """A read-only file over a remote object, fetched in aligned blocks by range request. GDAL reads a COG
    header, its tile table and then the tiles a window needs; this serves exactly those blocks."""

    def __init__(self, http: _Http, url: str):
        super().__init__()
        self._http, self._url = http, url
        self._blocks: OrderedDict[int, bytes] = OrderedDict()
        self._pos = 0
        self.error: OSError | None = (
            None  # a refusal (limit, bad range): GDAL reports it only as a read failure
        )
        first, self.size = http.get_range(url, 0, BLOCK_BYTES - 1)
        self._blocks[0] = first

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self.size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _fill(self, first: int, last: int) -> dict[int, bytes]:
        """Every block of `first..last` (inclusive), cached or fetched with as few requests as possible.
        Fetched blocks are cached too, but nothing is evicted here: `readinto` trims the cache after it has
        copied these blocks, so one read longer than the cache still finds all of its blocks."""
        found: dict[int, bytes] = {}
        for block in range(first, last + 1):
            if block in self._blocks:
                self._blocks.move_to_end(block)
                found[block] = self._blocks[block]
        missing = [b for b in range(first, last + 1) if b not in found]
        runs: list[list[int]] = []
        for block in missing:
            if runs and runs[-1][-1] == block - 1:
                runs[-1].append(block)
            else:
                runs.append([block])
        for run in runs:
            start = run[0] * BLOCK_BYTES
            end = min((run[-1] + 1) * BLOCK_BYTES, self.size) - 1
            body, total = self._http.get_range(self._url, start, end)
            if total != self.size:
                raise OSError(f"Copernicus DEM: {self._url} changed size while it was being read")
            for number, block in enumerate(run):
                found[block] = self._blocks[block] = body[number * BLOCK_BYTES : (number + 1) * BLOCK_BYTES]
        return found

    def readinto(self, buffer) -> int:
        view = memoryview(buffer).cast("B")
        want = min(len(view), self.size - self._pos)
        if want <= 0:
            return 0
        first, last = self._pos // BLOCK_BYTES, (self._pos + want - 1) // BLOCK_BYTES
        try:
            blocks = self._fill(first, last)
        except OSError as exc:
            self.error = exc
            raise
        done = 0
        while done < want:
            block, offset = divmod(self._pos, BLOCK_BYTES)
            piece = blocks[block][offset : offset + want - done]
            if not piece:
                raise OSError(f"Copernicus DEM: {self._url}: block {block} is shorter than the file says")
            view[done : done + len(piece)] = piece
            done += len(piece)
            self._pos += len(piece)
        while len(self._blocks) > MAX_CACHED_BLOCKS:
            self._blocks.popitem(last=False)
        return done


# --- the provider  ---------------------------------------------------------------------------------------


def _lonlat_bounds(polygon, project_crs) -> tuple[float, float, float, float]:
    import shapely

    xy = shapely.get_coordinates(shapely.segmentize(polygon, CLIP_SEGMENT_M))
    lon, lat = project_crs.to_wgs84(xy[:, 0], xy[:, 1])
    if not (np.isfinite(lon).all() and np.isfinite(lat).all()):
        raise ValueError("the clip cannot be transformed to longitude and latitude")
    return float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())


def _tiles_for(bounds: tuple[float, float, float, float]) -> list[tuple[int, int]]:
    """`(lat0, lon0)` of every 1 degree tile that the box overlaps (a box that only touches one is not)."""
    min_lon, min_lat, max_lon, max_lat = bounds
    eps = 1e-9
    lats = range(math.floor(min_lat + eps), math.ceil(max_lat - eps))
    lons = range(math.floor(min_lon + eps), math.ceil(max_lon - eps))
    return [(lat, lon) for lat in lats for lon in lons] or [(math.floor(min_lat), math.floor(min_lon))]


def _wanted(bounds: tuple[float, float, float, float], lat0: int, lon0: int):
    """The part of the clip's longitude/latitude box that lies in the tile with corner (lat0, lon0)."""
    return (
        max(bounds[0], lon0),
        max(bounds[1], lat0),
        min(bounds[2], lon0 + 1.0),
        min(bounds[3], lat0 + 1.0),
    )


def _window_key(name: str, bounds: tuple[float, float, float, float]) -> str:
    """The cache file of one window: keyed by the window's bounds (a folder) and the tile name (the file, so
    that the reader's object key, the file's stem, is the tile name)."""
    return "windows/{:.6f}_{:.6f}_{:.6f}_{:.6f}/{}.tif".format(*bounds, name)


def _remove_stale_parts(directory: Path) -> None:
    """Delete the `~*.part` files that a fetch which did not finish left behind. A fresh one may belong to a
    fetch running now, so only those older than `STALE_PART_S` go."""
    if not directory.is_dir():
        return
    cutoff = time.time() - STALE_PART_S
    for part in directory.rglob("~*.part"):
        try:
            if part.stat().st_mtime < cutoff:
                part.unlink()
        except OSError:
            continue


class CopernicusGlo30Provider:
    provider_id = PROVIDER_ID
    title = "Copernicus DEM GLO-30"
    kinds = frozenset({SourceKind.TERRAIN})
    licence = LICENCE
    attribution = ATTRIBUTION
    format_id = "geotiff"

    def __init__(self) -> None:
        self.last_stats: dict[str, Any] = {}  # of the latest fetch: requests, bytes, windows, cache hits

    def normalise_query(self, query: Mapping[str, Any]) -> dict[str, Any]:
        check_keys(query, ("clip",))
        return {"clip": clip_to_query(clip_from_query(query, DEFAULT_BUFFER_M))}

    def _plan(self, query: Mapping[str, Any]):
        context = provider_context()
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        polygon = context.clip_polygon(clip)
        bounds = _lonlat_bounds(polygon, context.project_crs)
        tiles = _tiles_for(bounds)
        if len(tiles) > MAX_TILES:
            raise ValueError(
                f"the clip touches {len(tiles)} Copernicus tiles, more than the {MAX_TILES} one import "
                "reads; "
                "use a smaller clip"
            )
        return clip, bounds, tiles

    def inspect(self, query: Mapping[str, Any]) -> InspectReport:
        clip, bounds, tiles = self._plan(query)
        cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir(self.provider_id))
        cached = 0
        for lat0, lon0 in tiles:
            cached += _window_key(tile_name(lat0, lon0), _wanted(bounds, lat0, lon0)) in cache.entries()
        warnings = [
            f"{len(tiles)} tile(s) of 1 degree; only the window covering the clip is read from each "
            f"({cached} already in the cache)",
            SURFACE_WARNING,
            DATUM_WARNING,
        ]
        return InspectReport(
            format_id=self.format_id,
            kind=SourceKind.TERRAIN,
            declared_crs=HORIZONTAL_CRS,
            extent=bounds,
            counts={"tiles": len(tiles)},
            suggested=ImportSettings(horizontal_crs=HORIZONTAL_CRS, vertical_crs=VERTICAL_CRS, clip=clip),
            licence_hint=self.licence,
            warnings=tuple(warnings),
        )

    def fetch(self, query: Mapping[str, Any], cache_dir: Path) -> list[Path]:
        _clip, bounds, tiles = self._plan(query)
        directory = Path(cache_dir)
        _remove_stale_parts(directory)
        fetch_note(SURFACE_WARNING)
        fetch_note(DATUM_WARNING)
        cache = provider_cache.ProviderCache(directory)
        stats: dict[str, Any] = {"tiles": [], "windows_read": 0, "cache_hits": 0, "requests": 0, "bytes": 0}
        self.last_stats = stats
        http: _Http | None = None
        names: list[str] = []
        try:
            for lat0, lon0 in tiles:
                name = tile_name(lat0, lon0)
                want = _wanted(bounds, lat0, lon0)
                key = _window_key(name, want)
                if cache.verify(key):
                    stats["cache_hits"] += 1
                    names.append(key)
                    continue
                cache.path(key).unlink(missing_ok=True)  # missing, or changed since it was recorded
                if http is None:
                    http = _Http()
                try:
                    self._read_window(http, name, want, cache, key)
                except _Missing as exc:
                    stats["missing"] = [*stats.get("missing", []), name]
                    detail = f"HTTP {exc.status}" if exc.status is not None else "outside the raster"
                    fetch_note(
                        f"{name}: no such tile ({detail}); open water has none, or the service refused it; "
                        "skipped"
                    )
                    continue
                stats["windows_read"] += 1
                names.append(key)
                stats["tiles"].append(name)
        finally:
            if http is not None:
                stats["requests"], stats["bytes"] = http.requests_made, http.bytes_received
                http.close()
        if not names:
            raise ValueError("no Copernicus DEM tile covers the clip region (open water has none)")
        cache.touch(names)
        cache.evict(protect=names)
        return [cache.path(n) for n in names]

    def _read_window(
        self,
        http: _Http,
        name: str,
        want: tuple[float, float, float, float],
        cache: provider_cache.ProviderCache,
        key: str,
    ) -> None:
        rasterio = _require("rasterio")
        from rasterio.windows import Window

        from coypu_builder.io.terrain.raster import clip_window

        url = _check_url(tile_url(name))
        remote = _RangeFile(http, url)

        def opener(path: str, mode: str = "rb", **_kwargs: Any):
            if path != _FILE_NAME or "r" not in mode:
                raise FileNotFoundError(path)  # GDAL looks for sidecar files; there are none
            return remote

        try:
            with rasterio.open(_FILE_NAME, opener=opener) as ds:
                if ds.crs is None or ds.crs.to_epsg() != 4326:
                    raise ValueError(f"{name} is not in EPSG:4326 ({ds.crs})")
                t = ds.transform
                if t.b != 0.0 or t.d != 0.0 or not (t.a > 0.0 and t.e < 0.0):
                    raise ValueError(f"{name} is not a north-up raster")
                pad_e, pad_n = WINDOW_PAD_PIXELS * t.a, WINDOW_PAD_PIXELS * -t.e
                found = clip_window(
                    t,
                    (ds.width, ds.height),
                    (want[0] - pad_e, want[1] - pad_n, want[2] + pad_e, want[3] + pad_n),
                )
                if found is None:
                    raise _Missing(name)
                col0, row0, width, height = found
                window = Window(col0, row0, width, height)
                heights = ds.read(1, window=window)
                profile = {
                    "driver": "GTiff",
                    "width": width,
                    "height": height,
                    "count": 1,
                    "dtype": heights.dtype.name,
                    "crs": ds.crs,
                    "transform": ds.window_transform(window),
                    "nodata": ds.nodata,
                    "compress": "deflate",
                }
        except rasterio.errors.RasterioError as exc:
            if remote.error is not None:
                raise remote.error from exc
            raise OSError(f"Copernicus DEM: cannot read {name}: {exc}") from exc
        target = cache.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f"~{uuid4().hex[:12]}.part")
        try:
            with rasterio.open(part, "w", **profile) as out:
                out.write(heights, 1)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        cache.record(key, url=url)
