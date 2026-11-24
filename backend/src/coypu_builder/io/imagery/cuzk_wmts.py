"""The ČÚZK orthophoto as a streamed WMTS source, `cuzk-ortho-wmts` (T-134, Part B).

The import reads the service's GetCapabilities document (`fetch`), keeps the layer's TileMatrixSet (each
TileMatrix, the tile URL template and the clip) and writes it as a small `<layer>.wmts` descriptor in the
provider cache. Nothing else is downloaded at import. The reader `wmts-descriptor` turns the descriptor into
a `WmtsSource` payload, and `WmtsLayer` fetches, when a window is rendered, only the tiles that cover it. A
tile that cannot be fetched or decoded is not valid, so relief shows through there, and it adds a warning.

The service is EPSG:5514 and its tile corners are East-North: the TopLeftCorner of the TileMatrixSet and the
layer's bounding box agree on that order. The corner goes through `io/landxml/dialects.py` with native order
"EN", so no sign or order is special-cased here. A TileMatrix pixel is `scale denominator x 0.00028` m (the
OGC 0.28 mm pixel), and row 0 of a matrix is its northern row.

Network access is https only, to `ALLOWED_HOSTS` on `ALLOWED_PORTS`, and every URL and every redirect hop is
checked before it is requested, including the template in the capabilities and the one in a descriptor on
disk. Capabilities are parsed with `defusedxml`. A tile body is capped at `MAX_TILE_BYTES` both by its
`Content-Length` and by the bytes read, and its image is checked for its size before it is decoded. At most
`TILE_CONCURRENCY` requests are in flight across all threads. A per-host circuit breaker stops requests while
the service is down (`_Breaker`), and a render has a deadline (`RENDER_DEADLINE_S`).

Tiles are kept in the provider's cache folder, `cuzk-ortho-wmts/<TileMatrix>/<row>/<col>.jpg` (`.png` when
the service answers PNG), with a `.json` sidecar that holds the validators. A tile older than
`REVALIDATE_AFTER_S` is revalidated with `If-None-Match` / `If-Modified-Since`: a 304 refreshes its time and
fetches no body. The folder is bounded by `TILE_CACHE_CAP_BYTES`, the oldest tiles going first; its running
total is walked only when it is over the cap.

Pillow and requests are imported when they are first used, as the `gis` extra requires.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import numpy as np

from coypu_builder import __version__
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.imagery import Bounds, ImageryRaster, RasterLayer
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.gis.envelope import require
from coypu_builder.io.landxml.dialects import resolve_coordinates
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import check_keys, clip_from_query, clip_to_query
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import fetch_note, provider_context, sha256_file
from coypu_builder.io.sources.terms import CUZK_ATTRIBUTION, CUZK_LICENCE

PROVIDER_ID = "cuzk-ortho-wmts"
LAYER = "ORTOFOTO"
CAPABILITIES_URL = "https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO/MapServer/WMTS?service=WMTS&request=GetCapabilities"
ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
ALLOWED_HOSTS: tuple[str, ...] = ("ags.cuzk.gov.cz",)  # exactly this host; T-139 keeps its own, wider list
ALLOWED_PORTS: tuple[int | None, ...] = (None, 443)
HORIZONTAL_CRS = "EPSG:5514"
LICENCE = CUZK_LICENCE
ATTRIBUTION = CUZK_ATTRIBUTION
TITLE = "ČÚZK ortofoto (WMTS)"
DESCRIPTION = (
    "Aerial photos of the Czech Republic, streamed tile by tile while you look at them. "
    "Nothing is downloaded in advance."
)
LABEL = "ČÚZK orthophoto"
DEFAULT_BUFFER_M = 250.0
PIXEL_PER_SCALE_M = 0.00028  # the OGC 0.28 mm pixel: a TileMatrix pixel is its scale denominator times this
MAX_TILE_BYTES = 2 * 1024 * 1024
MAX_CAPABILITIES_BYTES = 8 * 1024 * 1024
MAX_DESCRIPTOR_BYTES = 1024 * 1024
MAX_MATRICES = 64
MAX_TILE_SIDE = 1024
MIN_PIXEL_M = 0.01  # a TileMatrix pixel, declared by the capabilities or a descriptor, lies in this range
MAX_PIXEL_M = 100_000.0
TARGET_TOLERANCE = 1.001  # a TileMatrix qualifies when its pixel is at most the target pixel times this
MAX_TILES_PER_RENDER = 64  # a render that needs more tiles fetches none, and degrades with one warning
MAX_MOSAIC_PX = 64_000_000  # decimal megapixels: the same refusal for a mosaic that is too large
NOTICE = "Licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Resampled for display."
REVALIDATE_AFTER_S = 30 * 86400.0
TILE_CONCURRENCY = 4
TILE_CACHE_CAP_BYTES = provider_cache.DEFAULT_CAP_BYTES
CONNECT_TIMEOUT_S = 4.0  # a tile's connect timeout
READ_TIMEOUT_S = 15.0  # a tile body is about 22 KB; a read stalled longer holds a fetch slot for too long
RENDER_DEADLINE_S = 15.0  # tiles not in by then degrade at once
BREAKER_TRIP_AFTER = 3  # exhausted transient failures in a row
BREAKER_COOL_DOWN_S = 30.0  # the first cool-down; it doubles after each failed probe, up to the maximum
BREAKER_COOL_DOWN_MAX_S = 300.0
DESCRIPTOR_FORMAT = "wmts-descriptor"
DESCRIPTOR_VERSION = 1
DESCRIPTOR_FIELDS = frozenset(
    {"format", "version", "service_url", "layer", "tile_matrix_set", "crs", "clip", "matrices"}
)
MATRIX_FIELDS = frozenset(
    {
        "identifier",
        "scale_denominator",
        "top_left",
        "tile_width",
        "tile_height",
        "matrix_width",
        "matrix_height",
    }
)
WMTS_NS = "{http://www.opengis.net/wmts/1.0}"
OWS_NS = "{http://www.opengis.net/ows/1.1}"
_SLOTS = threading.BoundedSemaphore(TILE_CONCURRENCY)  # process-wide: at most this many requests in flight
_TIMEOUT_S = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
_CACHE_LOCK = threading.Lock()  # the running byte totals of the tile folders, and the walks that correct them
_CACHE_TOTALS: dict[str, int] = {}  # tile folder -> image bytes it holds, seeded by one walk per process
_BREAKER_LOCK = threading.Lock()
_REPORTS = threading.local()  # the TileReport of the calling thread, if a `tile_report` is open in it
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_S = 1.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_CHUNK = 65536
_MAX_WARNINGS = 20
_EPS = 1e-9
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_EPSG_5514 = re.compile(r"EPSG:+5514\Z")
_sleep = time.sleep
log = logging.getLogger(__name__)
NO_TILE_TTL_S = (
    30 * 86400.0
)  # a 403 or 404 is remembered this long: no request is made for that tile meanwhile


class _Transient(Exception):
    """A failure worth retrying: a 5xx or 429, a timeout, a dropped connection or a truncated body. `short` is
    the phrase a person reads in a warning ("cannot connect"); `detail` is the exception text, for the log."""

    def __init__(self, short: str, detail: str = "") -> None:
        super().__init__(short)
        self.short = short
        self.detail = detail or short


class _Missing(OSError):
    """The service has no such resource (HTTP 403 or 404). Not retried, and an answer, not a failure."""


class _BadTile(Exception):
    """A fetched or stored tile that is not an image of the expected format and size."""


class _Exhausted(OSError):
    """A transient failure that the retries ran out on. It counts toward the host's breaker."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _BreakerOpen(OSError):
    """The host's breaker is open, or its half-open probe is out: no request is made."""


class _Deadline(OSError):
    """The render's deadline passed before the tile's next request."""


def _check_url(url: str) -> str:
    """Only an https URL (see `ALLOWED_SCHEMES`) on exactly one of `ALLOWED_HOSTS`, on a port in
    `ALLOWED_PORTS`, without userinfo. Raises `ValueError`."""

    def refuse(reason: str) -> ValueError:
        return ValueError(f"ČÚZK orthophoto: refused URL {url[:80]!r} ({reason})")

    if not url.isascii() or any(ch.isspace() or ch == "\\" for ch in url):
        raise refuse("unexpected characters")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise refuse("unparsable") from exc
    if parts.scheme not in ALLOWED_SCHEMES:
        raise refuse(f"scheme must be {' or '.join(ALLOWED_SCHEMES)}")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise refuse("userinfo is not allowed")
    if port not in ALLOWED_PORTS:
        raise refuse("port must be 443 or none")
    if host not in ALLOWED_HOSTS:
        raise refuse("not a ČÚZK host")
    return url


def _example_url(template: str) -> str:
    """The template with its tile placeholders filled by 0, for the allow-list; other placeholders refused."""
    url = template.replace("{TileMatrix}", "0").replace("{TileRow}", "0").replace("{TileCol}", "0")
    if "{" in url or "}" in url:
        raise ValueError(
            f"the tile URL template has a placeholder this client does not fill: {template[:80]!r}"
        )
    return url


def _fill(template: str, matrix: TileMatrix, row: int, col: int) -> str:
    return (
        template.replace("{TileMatrix}", matrix.identifier)
        .replace("{TileRow}", str(row))
        .replace("{TileCol}", str(col))
    )


@dataclass(frozen=True)
class TileMatrix:
    identifier: str
    scale_denominator: float
    top_left_e: float  # the north-west corner of tile (0, 0), in EPSG:5514
    top_left_n: float
    tile_width: int
    tile_height: int
    matrix_width: int
    matrix_height: int

    @property
    def pixel_m(self) -> float:
        return self.scale_denominator * PIXEL_PER_SCALE_M

    @property
    def tile_edge_e(self) -> float:
        return self.tile_width * self.pixel_m

    @property
    def tile_edge_n(self) -> float:
        return self.tile_height * self.pixel_m


@dataclass(frozen=True)
class WmtsSource:
    """The payload of one streamed orthophoto: the service's tile URL template, the layer and TileMatrixSet,
    the TileMatrices and the clip box, all in EPSG:5514. Built from a descriptor; nothing here is fetched."""

    service_url: str  # the tile URL template; {TileMatrix}, {TileRow} and {TileCol} are filled per tile
    layer: str
    tile_matrix_set: str
    matrices: tuple[TileMatrix, ...]
    clip: Bounds

    @property
    def resolution_m(self) -> float:
        """The finest pixel the service offers: the layer's declared resolution, for T-134's precedence."""
        return min(m.pixel_m for m in self.matrices)


def choose_matrix(matrices: Sequence[TileMatrix], target_m: float) -> TileMatrix:
    """The coarsest TileMatrix whose pixel is at most `target_m` x `TARGET_TOLERANCE`, else the finest.
    The tolerance is 0.1 %: ČÚZK's matrices sit 0.013 % above the powers of two (TileMatrix 11 is 1.00013 m),
    and a strict comparison would skip the matrix that fits every level, fetching four times the tiles."""
    limit = target_m * TARGET_TOLERANCE
    coarse_enough = [m for m in matrices if m.pixel_m <= limit]
    if coarse_enough:
        return max(coarse_enough, key=lambda m: m.pixel_m)
    return min(matrices, key=lambda m: m.pixel_m)


def tile_span(matrix: TileMatrix, bounds: Bounds) -> tuple[int, int, int, int]:
    """(first column, last column, first row, last row) of the tiles of `matrix` covering `bounds`, both ends
    inclusive and clipped to the matrix. It is computed, not listed: a hostile matrix gives a huge span, so
    the caller counts it before it builds any tile list."""
    min_e, min_n, max_e, max_n = bounds
    te, tn = matrix.tile_edge_e, matrix.tile_edge_n
    c0 = max(0, math.floor((min_e - matrix.top_left_e) / te + _EPS))
    c1 = min(matrix.matrix_width - 1, math.ceil((max_e - matrix.top_left_e) / te - _EPS) - 1)
    r0 = max(0, math.floor((matrix.top_left_n - max_n) / tn + _EPS))
    r1 = min(matrix.matrix_height - 1, math.ceil((matrix.top_left_n - min_n) / tn - _EPS) - 1)
    return c0, c1, r0, r1


def covering_tiles(matrix: TileMatrix, bounds: Bounds) -> tuple[range, range]:
    """The (columns, rows) of the tiles of `matrix` that cover `bounds`, clipped to the matrix."""
    c0, c1, r0, r1 = tile_span(matrix, bounds)
    return range(c0, c1 + 1), range(r0, r1 + 1)


# --- capabilities and the descriptor -----------------------------------------------------------------------


def _finite(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _positive_int(value: Any, what: str, limit: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
        raise ValueError(f"{what} must be an integer from 1 to {limit}, got {value!r}")
    return value


def _token(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _TOKEN.match(value):
        raise ValueError(f"{what} must be a plain identifier, got {value!r}")
    return value


def _matrix(
    identifier: Any,
    scale: Any,
    top_left: tuple[Any, Any],
    tile_width: Any,
    tile_height: Any,
    matrix_width: Any,
    matrix_height: Any,
) -> TileMatrix:
    ident = _token(identifier, "a TileMatrix identifier")
    scale_m = _finite(scale, f"TileMatrix {ident} ScaleDenominator")
    if scale_m <= 0.0:
        raise ValueError(f"TileMatrix {ident} ScaleDenominator must be > 0, got {scale_m}")
    pixel_m = scale_m * PIXEL_PER_SCALE_M
    if not MIN_PIXEL_M <= pixel_m <= MAX_PIXEL_M:
        raise ValueError(
            f"TileMatrix {ident} pixel must be {MIN_PIXEL_M:g} to {MAX_PIXEL_M:g} m, got {pixel_m:g} m"
        )
    width = _positive_int(tile_width, f"TileMatrix {ident} TileWidth", MAX_TILE_SIDE)
    height = _positive_int(tile_height, f"TileMatrix {ident} TileHeight", MAX_TILE_SIDE)
    if width != height:
        raise ValueError(f"TileMatrix {ident}: tiles must be square, got {width} x {height}")
    return TileMatrix(
        identifier=ident,
        scale_denominator=scale_m,
        top_left_e=_finite(top_left[0], f"TileMatrix {ident} TopLeftCorner"),
        top_left_n=_finite(top_left[1], f"TileMatrix {ident} TopLeftCorner"),
        tile_width=width,
        tile_height=height,
        matrix_width=_positive_int(matrix_width, f"TileMatrix {ident} MatrixWidth", 2**31 - 1),
        matrix_height=_positive_int(matrix_height, f"TileMatrix {ident} MatrixHeight", 2**31 - 1),
    )


def _clip(value: Any) -> Bounds:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("clip must be [min_e, min_n, max_e, max_n]")
    min_e, min_n, max_e, max_n = (_finite(v, "clip") for v in value)
    if not (min_e < max_e and min_n < max_n):
        raise ValueError(f"clip must be a box with min below max, got {value!r}")
    return (min_e, min_n, max_e, max_n)


def _xml_root(content: bytes) -> Any:
    defusedxml = require("defusedxml")
    safe_et = require("defusedxml.ElementTree")
    try:
        return safe_et.fromstring(content, forbid_dtd=True)
    except (defusedxml.DefusedXmlException, safe_et.ParseError) as exc:
        raise ValueError(f"the ČÚZK capabilities are not safe, readable XML: {exc}") from exc


def parse_capabilities(content: bytes, clip: Bounds) -> WmtsSource:
    """The layer `ORTOFOTO` of a WMTS GetCapabilities document, as a `WmtsSource` for `clip`. Its default
    style and TileMatrixSet fill the tile URL template, which must pass the allow-list. Else `ValueError`."""
    root = _xml_root(content)
    if root.tag != WMTS_NS + "Capabilities":
        raise ValueError("the ČÚZK capabilities are not a WMTS capabilities document")
    layer = next(
        (e for e in root.iter(WMTS_NS + "Layer") if e.findtext(OWS_NS + "Identifier") == LAYER), None
    )
    if layer is None:
        raise ValueError(f"the ČÚZK capabilities name no layer '{LAYER}'")
    link = layer.find(f"{WMTS_NS}TileMatrixSetLink/{WMTS_NS}TileMatrixSet")
    tms_id = _token((link.text or "").strip() if link is not None else None, "the TileMatrixSet of the layer")
    style = next(
        (
            s.findtext(OWS_NS + "Identifier")
            for s in layer.findall(WMTS_NS + "Style")
            if s.get("isDefault") == "true"
        ),
        None,
    )
    template = next(
        (
            r.get("template")
            for r in layer.findall(WMTS_NS + "ResourceURL")
            if r.get("resourceType") == "tile"
        ),
        None,
    )
    if not style or not template:
        raise ValueError("the layer names no default style or no tile resource")
    service_url = template.replace("{Style}", style).replace("{TileMatrixSet}", tms_id)
    _check_url(_example_url(service_url))
    tms = next(
        (t for t in root.iter(WMTS_NS + "TileMatrixSet") if t.findtext(OWS_NS + "Identifier") == tms_id), None
    )
    if tms is None:
        raise ValueError(f"the capabilities hold no TileMatrixSet '{tms_id}'")
    crs_names = [(c.text or "").strip() for c in tms.findall(OWS_NS + "SupportedCRS")]
    if not any(_EPSG_5514.search(name) for name in crs_names):
        raise ValueError(f"TileMatrixSet '{tms_id}' is not in EPSG:5514 ({crs_names})")
    matrices = []
    for element in tms.findall(WMTS_NS + "TileMatrix"):
        corner = (element.findtext(WMTS_NS + "TopLeftCorner") or "").split()
        if len(corner) != 2:
            raise ValueError("a TileMatrix holds no TopLeftCorner pair")
        tokens = np.array([[_finite_token(corner[0]), _finite_token(corner[1])]], dtype=np.float64)
        east, north = resolve_coordinates(tokens, ProjectCRS(HORIZONTAL_CRS), "auto", native_order="EN")[0]
        matrices.append(
            _matrix(
                element.findtext(OWS_NS + "Identifier"),
                _finite_token(element.findtext(WMTS_NS + "ScaleDenominator") or ""),
                (float(east), float(north)),
                _int_token(element.findtext(WMTS_NS + "TileWidth")),
                _int_token(element.findtext(WMTS_NS + "TileHeight")),
                _int_token(element.findtext(WMTS_NS + "MatrixWidth")),
                _int_token(element.findtext(WMTS_NS + "MatrixHeight")),
            )
        )
    if not matrices or len(matrices) > MAX_MATRICES:
        raise ValueError(f"the TileMatrixSet holds {len(matrices)} matrices; 1 to {MAX_MATRICES} are read")
    return WmtsSource(service_url, LAYER, tms_id, tuple(matrices), clip)


def _finite_token(text: str) -> float:
    try:
        return _finite(float(text), "a capabilities number")
    except ValueError as exc:
        raise ValueError(f"unreadable capabilities number {text!r}") from exc


def _int_token(text: str | None) -> int:
    try:
        return int(text or "")
    except ValueError as exc:
        raise ValueError(f"unreadable capabilities integer {text!r}") from exc


def descriptor_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / f"{LAYER}.wmts"


def write_descriptor(cache_dir: Path, source: WmtsSource) -> Path:
    """Write the descriptor of `source` into `cache_dir` as LF JSON, atomically. It holds no timestamp, so an
    unchanged service gives an unchanged descriptor and an unchanged content hash."""
    payload = {
        "format": DESCRIPTOR_FORMAT,
        "version": DESCRIPTOR_VERSION,
        "service_url": source.service_url,
        "layer": source.layer,
        "tile_matrix_set": source.tile_matrix_set,
        "crs": HORIZONTAL_CRS,
        "clip": list(source.clip),
        "matrices": [
            {
                "identifier": m.identifier,
                "scale_denominator": m.scale_denominator,
                "top_left": [m.top_left_e, m.top_left_n],
                "tile_width": m.tile_width,
                "tile_height": m.tile_height,
                "matrix_width": m.matrix_width,
                "matrix_height": m.matrix_height,
            }
            for m in source.matrices
        ],
    }
    text = json.dumps(payload, ensure_ascii=True, indent=1, sort_keys=True) + "\n"
    path = descriptor_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"~{uuid4().hex[:12]}.tmp")
    try:
        tmp.write_bytes(text.encode("ascii"))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def read_descriptor(path: Path) -> WmtsSource:
    """The `WmtsSource` a descriptor holds. A descriptor is data, not trust: its fields are checked, and its
    service URL must pass the allow-list before anything is requested. Raises `ValueError`."""
    path = Path(path)
    if path.stat().st_size > MAX_DESCRIPTOR_BYTES:
        raise ValueError(f"'{path.name}' is larger than a WMTS descriptor can be")
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"'{path.name}' is not a WMTS descriptor: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != DESCRIPTOR_FORMAT:
        raise ValueError(f"'{path.name}' is not a WMTS descriptor")
    if data.get("version") != DESCRIPTOR_VERSION or set(data) != DESCRIPTOR_FIELDS:
        raise ValueError(
            f"'{path.name}' is a WMTS descriptor with fields or a version this build does not read"
        )
    if data["crs"] != HORIZONTAL_CRS:
        raise ValueError(f"the descriptor is in {data['crs']!r}; the ČÚZK orthophoto is in {HORIZONTAL_CRS}")
    service_url = data["service_url"]
    if not isinstance(service_url, str):
        raise ValueError("the descriptor's service_url must be a string")
    _check_url(_example_url(service_url))
    layer = _token(data["layer"], "the descriptor's layer")
    tms = _token(data["tile_matrix_set"], "the descriptor's tile_matrix_set")
    clip = _clip(data["clip"])
    matrices = data["matrices"]
    if not isinstance(matrices, list) or not 1 <= len(matrices) <= MAX_MATRICES:
        raise ValueError(f"the descriptor must hold 1 to {MAX_MATRICES} matrices")
    parsed = []
    for item in matrices:
        if not isinstance(item, dict) or set(item) != MATRIX_FIELDS:
            raise ValueError("a descriptor matrix has the wrong fields")
        corner = item["top_left"]
        if not isinstance(corner, list) or len(corner) != 2:
            raise ValueError("a descriptor matrix's top_left must be two numbers")
        parsed.append(
            _matrix(
                item["identifier"],
                item["scale_denominator"],
                (corner[0], corner[1]),
                item["tile_width"],
                item["tile_height"],
                item["matrix_width"],
                item["matrix_height"],
            )
        )
    return WmtsSource(service_url, layer, tms, tuple(parsed), clip)


# --- the circuit breaker, per host ---------------------------------------------------------------------

_clock = time.monotonic  # injectable: a test replaces it to move the breakers' time without waiting


class _Breaker:
    """One host's breaker. Closed, it counts exhausted transient failures and trips after
    `BREAKER_TRIP_AFTER` in a row; a success resets the count. Open, it admits no request for its cool-down,
    `BREAKER_COOL_DOWN_S`, doubling after each failed probe up to `BREAKER_COOL_DOWN_MAX_S`. Then it is
    half-open: one probe, with one attempt, is admitted. Its success closes the breaker; its failure reopens
    it with the doubled cool-down."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "closed"  # "closed", "open" or "half_open"
        self.failures = 0
        self.cool_down_s = BREAKER_COOL_DOWN_S
        self.opened_at = 0.0
        self.reason = ""

    def admit(self) -> bool:
        """True when this request is the half-open probe, False for a request of a closed breaker. Raises
        `_BreakerOpen` when no request may be made now."""
        with self._lock:
            if self.state == "closed":
                return False
            if self.state == "open" and _clock() - self.opened_at >= self.cool_down_s:
                self.state = "half_open"
                return True
            raise _BreakerOpen(self._refusal())

    def check_retry(self) -> None:
        """Before a retry and again after its backoff: a breaker that opened meanwhile refuses it."""
        with self._lock:
            if self.state != "closed":
                raise _BreakerOpen(self._refusal())

    def recheck(self, probe: bool) -> None:
        """After a worker has waited for a fetch slot, before it sends: a request of a closed breaker is sent
        only while the breaker is still closed. The probe is the one request a half-open breaker admits."""
        with self._lock:
            if not probe and self.state != "closed":
                raise _BreakerOpen(self._refusal())

    def succeeded(self, probe: bool) -> None:
        with self._lock:
            if probe and self.state == "half_open":
                self._close()
            elif self.state == "closed":
                self.failures = 0

    def failed(self, reason: str, probe: bool) -> None:
        with self._lock:
            self.reason = reason
            if probe and self.state == "half_open":
                self.cool_down_s = min(2 * self.cool_down_s, BREAKER_COOL_DOWN_MAX_S)
                self._open()
            elif self.state == "closed":
                self.failures += 1
                if self.failures >= BREAKER_TRIP_AFTER:
                    self._open()

    def abandon(self, probe: bool) -> None:
        """A probe with no verdict (a refused URL, or the render's deadline) starts its cool-down again."""
        with self._lock:
            if probe and self.state == "half_open":
                self._open()

    def warning(self) -> str | None:
        """The one aggregated warning of an open or half-open breaker; None once it is closed."""
        with self._lock:
            if self.state == "closed":
                return None
            return f"ČÚZK orthophoto is not reachable ({self.reason}); shaded relief is shown; {self._when()}"

    def _open(self) -> None:
        self.state = "open"
        self.opened_at = _clock()

    def _close(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.cool_down_s = BREAKER_COOL_DOWN_S
        self.reason = ""

    def _when(self) -> str:
        if self.state == "half_open":
            return "a retry is in progress"
        left = self.opened_at + self.cool_down_s - _clock()
        return f"retrying in {max(0.0, left):.0f} s"

    def _refusal(self) -> str:
        return f"no request is made while the service cannot be reached ({self.reason}); {self._when()}"


_BREAKERS: dict[str, _Breaker] = {}  # host -> its breaker, for this process


def _breaker(host: str) -> _Breaker:
    with _BREAKER_LOCK:
        breaker = _BREAKERS.get(host)
        if breaker is None:
            breaker = _BREAKERS[host] = _Breaker()
        return breaker


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _Deadline("the render's deadline passed before the tile could be requested")


# --- HTTP ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    status: int
    body: bytes | None
    etag: str | None
    last_modified: str | None
    probe: bool = False  # whether this request was the breaker's half-open probe: the caller settles it


class _Client:
    """One requests session with the service's headers. Every hop is checked; every body is capped."""

    def __init__(self) -> None:
        self._requests = require("requests")
        self._session = self._requests.Session()
        self._session.headers["User-Agent"] = f"COYPU-Builder/{__version__} (CUZK orthophoto client)"
        self._session.headers["Accept-Encoding"] = "identity"  # the bytes must be the file's own

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def get(
        self, url: str, *, limit: int, headers: dict[str, str] | None = None, deadline: float | None = None
    ) -> _Reply:
        """One GET through the host's breaker, retried on transient failures with backoff. The outcomes:
        `_Missing` for a 403 or 404; `OSError` for a body over `limit` or another status; `_Exhausted` when
        the retries run out (it counts against the breaker); `_BreakerOpen` or `_Deadline` when no request is
        made; `ValueError` for a refused URL. A 403, 404 or other status is an answer, so the host counts as
        up. A reply of 200 or 304 is returned unsettled: the caller calls `_settle` once it has decoded
        it, so that a bad body is a failure and not a success first."""
        breaker = _breaker(_host_of(url))
        probe = breaker.admit()
        try:
            reply = self._retrying(
                url, breaker=breaker, limit=limit, headers=headers, probe=probe, deadline=deadline
            )
        except _Exhausted as exc:  # its reason is the short phrase; the attempts were logged
            breaker.failed(exc.reason, probe)
            raise
        except (_BreakerOpen, _Deadline, ValueError):
            breaker.abandon(probe)
            raise
        except OSError:
            breaker.succeeded(probe)
            raise
        except BaseException:
            breaker.abandon(probe)
            raise
        return replace(reply, probe=probe)

    def _retrying(
        self,
        url: str,
        *,
        breaker: _Breaker,
        limit: int,
        headers: dict[str, str] | None,
        probe: bool,
        deadline: float | None,
    ) -> _Reply:
        """The attempts of one tile. The probe of a half-open breaker makes a single attempt. Before a retry,
        and again after its backoff, the breaker is checked, so nothing is slept or requested while open."""
        attempts = 1 if probe else _MAX_ATTEMPTS
        reason = ""
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                breaker.check_retry()
                _sleep(_BACKOFF_BASE_S * 2 ** (attempt - 2))
                breaker.check_retry()
            _check_deadline(deadline)
            try:
                return self._once(
                    url, limit=limit, headers=headers, breaker=breaker, probe=probe, deadline=deadline
                )
            except _Transient as exc:
                reason = exc.short
                log.info("ČÚZK orthophoto: attempt %d of %d failed: %s", attempt, attempts, exc.detail)
        raise _Exhausted(reason)

    def _once(
        self,
        url: str,
        *,
        limit: int,
        headers: dict[str, str] | None,
        breaker: _Breaker,
        probe: bool,
        deadline: float | None,
    ) -> _Reply:
        current = url
        with _SLOTS:
            # a worker that waited for a fetch slot re-checks the breaker and the deadline before sending
            breaker.recheck(probe)
            _check_deadline(deadline)
            for _hop in range(_MAX_REDIRECTS + 1):
                _check_url(current)
                try:
                    response = self._session.request(
                        "GET",
                        current,
                        headers=headers,
                        stream=True,
                        timeout=_TIMEOUT_S,
                        allow_redirects=False,
                    )
                except (self._requests.Timeout, self._requests.ConnectionError) as exc:
                    short = "timed out" if isinstance(exc, self._requests.Timeout) else "cannot connect"
                    raise _Transient(short, f"{type(exc).__name__}: {exc}") from exc
                try:
                    location = response.headers.get("Location")
                    if response.status_code in _REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        continue
                    return self._reply(current, response, limit, deadline)
                finally:
                    response.close()
        raise OSError(f"ČÚZK orthophoto: {url}: more than {_MAX_REDIRECTS} redirects")

    def _reply(self, url: str, response: Any, limit: int, deadline: float | None) -> _Reply:
        status = response.status_code
        etag, last_modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        if status == 304:
            return _Reply(304, None, etag, last_modified)
        if status in (403, 404):
            raise _Missing(f"HTTP {status}")
        if status >= 500 or status == 429:
            raise _Transient(f"HTTP {status}")
        if status != 200:
            raise OSError(f"ČÚZK orthophoto: {url}: HTTP {status}")
        declared = response.headers.get("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            raise OSError(f"the tile's Content-Length {declared!r} is over the {limit} bytes allowed")
        body = bytearray()
        try:
            for chunk in response.iter_content(_CHUNK):
                _check_deadline(deadline)  # a slow body must not hold the render past its deadline
                body += chunk
                if len(body) > limit:
                    raise OSError(f"the body is over the {limit} bytes allowed")
        except self._requests.RequestException as exc:
            raise _Transient("connection lost", f"{type(exc).__name__}: {exc}") from exc
        return _Reply(200, bytes(body), etag, last_modified)


def _settle(url: str, reply: _Reply) -> None:
    """The host answered with a usable reply: a success for the breaker. A probe's success closes it."""
    _breaker(_host_of(url)).succeeded(reply.probe)


def _fetch_capabilities(client: _Client) -> bytes:
    reply = client.get(CAPABILITIES_URL, limit=MAX_CAPABILITIES_BYTES)
    _settle(CAPABILITIES_URL, reply)
    if reply.body is None:
        raise OSError("ČÚZK orthophoto: the capabilities request answered without a body")
    return reply.body


def _require_project_crs(context: Any) -> None:
    if context.project_crs.epsg != 5514:
        raise ValueError(
            f"the ČÚZK orthophoto is in {HORIZONTAL_CRS}; this project is in {context.project_crs.name}, "
            "so create the project in EPSG:5514"
        )


# --- the provider ----------------------------------------------------------------------------------------


class CuzkWmtsProvider:
    provider_id = PROVIDER_ID
    title = TITLE
    description = DESCRIPTION
    kinds = frozenset({SourceKind.IMAGERY})
    licence = LICENCE
    attribution = ATTRIBUTION
    notices: tuple[str, ...] = (NOTICE,)
    format_id = DESCRIPTOR_FORMAT

    def normalise_query(self, query: Any) -> dict[str, Any]:
        check_keys(query, ("clip",))
        return {"clip": clip_to_query(clip_from_query(query, DEFAULT_BUFFER_M))}

    def inspect(self, query: Any) -> InspectReport:
        context = provider_context()
        _require_project_crs(context)
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        bounds = _polygon_bounds(context.clip_polygon(clip))
        with _Client() as client:
            source = parse_capabilities(_fetch_capabilities(client), bounds)
        return InspectReport(
            format_id=DESCRIPTOR_FORMAT,
            kind=SourceKind.IMAGERY,
            declared_crs=HORIZONTAL_CRS,
            extent=bounds,
            counts={"tile_matrices": len(source.matrices)},
            suggested=ImportSettings(horizontal_crs=HORIZONTAL_CRS, clip=clip),
            licence_hint=LICENCE,
            warnings=(
                "streamed: nothing is downloaded at import; the tiles are fetched while the view shows them, "
                f"from {len(source.matrices)} tile matrices (finest pixel {source.resolution_m:.3f} m)",
            ),
        )

    def fetch(self, query: Any, cache_dir: Path) -> list[Path]:
        context = provider_context()
        _require_project_crs(context)
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        bounds = _polygon_bounds(context.clip_polygon(clip))
        with _Client() as client:
            source = parse_capabilities(_fetch_capabilities(client), bounds)
        path = write_descriptor(Path(cache_dir), source)
        fetch_note(
            f"{LAYER}: TileMatrixSet {source.tile_matrix_set}, {len(source.matrices)} matrices; only the "
            "capabilities document was read"
        )
        return [path]


def _polygon_bounds(polygon: Any) -> Bounds:
    min_e, min_n, max_e, max_n = (float(v) for v in polygon.bounds)
    return (min_e, min_n, max_e, max_n)


def _within(source: WmtsSource, polygon: Any) -> WmtsSource:
    """The source with its clip narrowed to the import clip's bounding box. The descriptor's clip is the
    widest area the orthophoto may be shown in; the import's own clip can only make that smaller."""
    min_e, min_n, max_e, max_n = _polygon_bounds(polygon)
    clip = (
        max(source.clip[0], min_e),
        max(source.clip[1], min_n),
        min(source.clip[2], max_e),
        min(source.clip[3], max_n),
    )
    if not (clip[0] < clip[2] and clip[1] < clip[3]):
        raise ValueError("the import clip does not overlap the orthophoto's clip in its descriptor")
    return replace(source, clip=clip)


# --- the reader ------------------------------------------------------------------------------------------


class WmtsDescriptorReader:
    format_id = DESCRIPTOR_FORMAT
    kinds = frozenset({SourceKind.IMAGERY})
    suffixes = (".wmts",)
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        try:
            with Path(path).open("rb") as handle:
                head = handle.read(4096)
        except OSError:
            return False
        return b'"wmts-descriptor"' in head

    def inspect(self, path: Path) -> InspectReport:
        source = read_descriptor(path)
        return InspectReport(
            format_id=DESCRIPTOR_FORMAT,
            kind=SourceKind.IMAGERY,
            declared_crs=HORIZONTAL_CRS,
            extent=source.clip,
            counts={"tile_matrices": len(source.matrices)},
            suggested=ImportSettings(horizontal_crs=HORIZONTAL_CRS),
            licence_hint=LICENCE,
            warnings=("streamed: the descriptor holds no tiles; they are fetched while the view shows them",),
        )

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> list[ImportedObject]:
        source = read_descriptor(path)
        if transform.project_crs.epsg != 5514:
            raise ValueError(
                f"the ČÚZK orthophoto is in {HORIZONTAL_CRS}; this project is in {transform.project_crs.name}"
            )
        if clip_polygon is not None:
            source = _within(source, clip_polygon)
        key = f"{source.layer}/{source.tile_matrix_set}"
        return [ImportedObject(key, sha256_file(path), source)]


# --- the layer -------------------------------------------------------------------------------------------


def _tile_paths(root: Path, matrix: TileMatrix, row: int, col: int) -> tuple[Path, Path]:
    folder = root / matrix.identifier / str(row)
    return folder / f"{col}.jpg", folder / f"{col}.png"


def _stored(root: Path, matrix: TileMatrix, row: int, col: int) -> Path | None:
    for path in _tile_paths(root, matrix, row, col):
        try:
            if path.is_file():
                return path
        except OSError:  # a disk problem is a miss, not an error of the render
            continue
    return None


def _sidecar(image: Path) -> Path:
    return image.with_suffix(".json")


def _decode(body: bytes, size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray, str]:
    """(RGB array, valid mask, format) of a JPEG or PNG of exactly `size`. The size is checked before the
    pixels load. A transparent pixel (alpha 0 in RGBA, LA or PA, or the transparency key of P, L or RGB) is
    not valid, so relief shows through there, as `io/imagery/raster_image.py` treats an RGBA source."""
    image_module = require("PIL.Image")
    try:
        with image_module.open(io.BytesIO(body)) as image:
            if image.format not in ("JPEG", "PNG"):
                raise _BadTile(f"format {image.format}")
            if image.size != size:
                raise _BadTile(f"size {image.size[0]} x {image.size[1]}, expected {size[0]} x {size[1]}")
            image.load()
            fmt = str(image.format)
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                valid = np.asarray(image.convert("RGBA"), dtype=np.uint8)[..., 3] > 0
            else:
                valid = np.ones((size[1], size[0]), dtype=bool)
            return np.asarray(image.convert("RGB"), dtype=np.uint8), valid, fmt
    except _BadTile:
        raise
    except Exception as exc:  # a decoder error is a failed fetch, never a crash
        raise _BadTile(f"not a decodable image ({type(exc).__name__})") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"~{uuid4().hex[:12]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _age(image: Path) -> float:
    """Seconds since the tile was fetched or revalidated; infinite when another thread has just evicted it."""
    try:
        return time.time() - image.stat().st_mtime
    except OSError:
        return math.inf


def _touch(image: Path) -> None:
    """Refresh the time of a revalidated tile; a tile evicted meanwhile is not an error."""
    try:
        os.utime(image, None)
    except OSError:
        pass


def _validators(image: Path) -> dict[str, str] | None:
    try:
        stored = json.loads(_sidecar(image).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):  # valid JSON that is not an object is a corrupt sidecar: ignored
        return None
    headers: dict[str, str] = {}
    if isinstance(stored.get("etag"), str):
        headers["If-None-Match"] = stored["etag"]
    if isinstance(stored.get("last_modified"), str):
        headers["If-Modified-Since"] = stored["last_modified"]
    return headers or None


def _discard(image: Path) -> None:
    """Delete a tile and its sidecar; a file that cannot be deleted is left, and is not an error."""
    for path in (image, _sidecar(image)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _load(image: Path, size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray] | None:
    """The stored tile as (pixels, valid mask), or None when it cannot be read. A tile that no longer
    decodes is discarded, and None returned."""
    try:
        body = image.read_bytes()
    except OSError:
        return None
    try:
        rgb, valid, _format = _decode(body, size)
    except _BadTile:
        _discard(image)
        return None
    return rgb, valid


def _store(root: Path, matrix: TileMatrix, row: int, col: int, body: bytes, fmt: str, reply: _Reply) -> Path:
    jpg, png = _tile_paths(root, matrix, row, col)
    target, other = (jpg, png) if fmt == "JPEG" else (png, jpg)
    replaced = _size_of(target) + _size_of(other)
    _atomic_write(target, body)
    other.unlink(missing_ok=True)
    validators = {"etag": reply.etag, "last_modified": reply.last_modified}
    _atomic_write(_sidecar(target), json.dumps(validators, sort_keys=True).encode("utf-8"))
    _count_bytes(root, len(body) - replaced)
    return target


def _count_bytes(root: Path, delta: int) -> None:
    with _CACHE_LOCK:
        key = str(root)
        if key in _CACHE_TOTALS:
            _CACHE_TOTALS[key] = max(0, _CACHE_TOTALS[key] + delta)


def _scan(root: Path) -> list[tuple[float, int, Path]]:
    """(modification time, size, path) of every tile image under `root`: one `os.scandir` pass, one stat per
    file. A file that a concurrent eviction removed meanwhile is skipped."""
    found: list[tuple[float, int, Path]] = []
    pending = [os.fspath(root)]
    while pending:
        folder = pending.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.name.endswith((".jpg", ".png")):
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        found.append((info.st_mtime, info.st_size, Path(entry.path)))
        except FileNotFoundError:
            continue
    return found


def _enforce_cap(root: Path, keep: set[Path]) -> None:
    """Delete the oldest tile images, outside `keep`, once the folder's running total is over
    `TILE_CACHE_CAP_BYTES`. The total is seeded by one walk per process, and the tree is walked again only
    when the total is over the cap. A disk error here is not the render's: the next walk corrects the cap."""
    try:
        with _CACHE_LOCK:
            key = str(root)
            if key in _CACHE_TOTALS and _CACHE_TOTALS[key] <= TILE_CACHE_CAP_BYTES:
                return
            found = _scan(root)  # the seed of the total, or the walk of a folder that is over the cap
            total = sum(size for _mtime, size, _path in found)
            _CACHE_TOTALS[key] = total
            if total <= TILE_CACHE_CAP_BYTES:
                return
            for _mtime, size, path in sorted(found, key=lambda item: item[0]):
                if total <= TILE_CACHE_CAP_BYTES:
                    break
                if path in keep:
                    continue
                _discard(path)
                total -= size
            _CACHE_TOTALS[key] = total
    except OSError:
        return


class WmtsLayer:
    """An `ImageryLayer` over one streamed orthophoto. Construction and `bounds()` touch no network. `render`
    fetches the tiles that cover the window and the clip, through the host's breaker, and runs in a worker
    thread. A render reports the tiles it could not give to the `tile_report` open in its thread."""

    network_bound = True  # its render waits for the network (F50)

    def __init__(self, source_id: str, source: WmtsSource) -> None:
        self.source_id = source_id
        self.source = source
        self._host = _host_of(source.service_url)
        self._warnings: dict[tuple[object, ...], str] = {}  # per tile, keyed so that it can be dropped
        self._lock = threading.Lock()

    @property
    def resolution_m(self) -> float:
        return self.source.resolution_m

    def bounds(self) -> Bounds:
        return self.source.clip

    def warning_messages(self) -> tuple[str, ...]:
        """The warnings this layer has collected while it rendered: one per tile that could not be used, until
        that tile loads, and, while the host's breaker is open, its one aggregated warning."""
        with self._lock:
            messages = tuple(self._warnings.values())
        aggregated = _breaker(self._host).warning()
        return messages if aggregated is None else (*messages, aggregated)

    def render(self, bounds: Bounds, size: int) -> tuple[np.ndarray, np.ndarray]:
        min_e, min_n, max_e, max_n = bounds
        edge = max_e - min_e
        if edge <= 0.0 or abs((max_n - min_n) - edge) > 1e-9 * edge:
            raise ValueError(f"render needs a square window, got {bounds}")
        rgb = np.zeros((size, size, 3), dtype=np.uint8)
        valid = np.zeros((size, size), dtype=bool)
        # the clip limits the tiles fetched: they are chosen for the part of the window inside the clip
        window = _clipped(bounds, self.source.clip)
        if window is None:
            return rgb, valid
        matrix = choose_matrix(self.source.matrices, edge / size)
        c0, c1, r0, r1 = tile_span(matrix, window)
        count = max(0, c1 - c0 + 1) * max(0, r1 - r0 + 1)  # counted from the span: nothing is listed yet
        if count == 0:
            return rgb, valid
        if count > MAX_TILES_PER_RENDER or count * matrix.tile_width * matrix.tile_height > MAX_MOSAIC_PX:
            self._note(
                ("refused",),
                f"{LABEL}: this view needs {count} tiles of TileMatrix {matrix.identifier}, more than "
                f"a render fetches ({MAX_TILES_PER_RENDER} tiles or {MAX_MOSAIC_PX} pixels); "
                "shaded relief is shown there",
            )
            _record_missing(count)
            return rgb, valid
        root = provider_cache.provider_cache_dir(PROVIDER_ID)
        provider_cache.ensure_outside_repository(root)
        keys = [(row, col) for row in range(r0, r1 + 1) for col in range(c0, c1 + 1)]
        results = self._fetch_all(root, matrix, keys)  # dict: key -> _Tile
        rows, cols = r1 - r0 + 1, c1 - c0 + 1
        mosaic_rgb = np.zeros((rows * matrix.tile_height, cols * matrix.tile_width, 3), dtype=np.uint8)
        mosaic_valid = np.zeros(mosaic_rgb.shape[:2], dtype=bool)
        written: set[Path] = set()
        missing = 0
        for row, col in keys:
            tile = results[(row, col)]
            if tile.image is not None:
                written.add(tile.image)
            if tile.pixels is None:
                if tile.absent:  # the service has no tile here: an answer, so nothing is missing
                    self._forget(matrix, row, col)
                else:
                    missing += 1
                continue
            self._forget(matrix, row, col)  # the tile loaded: its warning, if it had one, goes
            tile_rgb, tile_valid = tile.pixels
            top = (row - r0) * matrix.tile_height
            left = (col - c0) * matrix.tile_width
            mosaic_rgb[top : top + matrix.tile_height, left : left + matrix.tile_width] = tile_rgb
            mosaic_valid[top : top + matrix.tile_height, left : left + matrix.tile_width] = tile_valid
        if written:
            _enforce_cap(root, written)
        _record_missing(missing)
        raster = ImageryRaster(
            origin_e=matrix.top_left_e + c0 * matrix.tile_edge_e,
            origin_n=matrix.top_left_n - r0 * matrix.tile_edge_n,
            cell_e=matrix.pixel_m,
            cell_n=matrix.pixel_m,
            rgb=mosaic_rgb,
            valid=mosaic_valid,
            native_resolution_m=matrix.pixel_m,
        )
        out_rgb, out_valid = RasterLayer(self.source_id, raster).render(bounds, size)
        out_valid &= _clip_mask(bounds, size, self.source.clip)  # outside the clip is never valid
        out_rgb[~out_valid] = 0
        if missing == 0 and _breaker(self._host).warning() is None:
            with self._lock:  # a complete render with the breaker closed: no tile is failing any more
                self._warnings.clear()
        return out_rgb, out_valid

    def _fetch_all(
        self, root: Path, matrix: TileMatrix, keys: list[tuple[int, int]]
    ) -> dict[tuple[int, int], _Tile]:
        """Every tile of the render, fetched by at most `TILE_CONCURRENCY` workers. Each stored copy is read
        here, before any request, so that a tile whose answer does not come by the render's deadline is shown
        from its copy, stale or not; a tile with no copy degrades at once. The workers still running finish on
        their own."""
        deadline = time.monotonic() + RENDER_DEADLINE_S
        size = (matrix.tile_width, matrix.tile_height)
        copies: dict[tuple[int, int], tuple[Path | None, tuple[np.ndarray, np.ndarray] | None]] = {}
        for key in keys:
            stored = _stored(root, matrix, key[0], key[1])
            copies[key] = (stored, _load(stored, size) if stored is not None else None)
        client = _Client()
        pool = ThreadPoolExecutor(max_workers=TILE_CONCURRENCY)
        try:
            futures = {
                key: pool.submit(self._tile, client, root, matrix, key[0], key[1], deadline, *copies[key])
                for key in keys
            }
            done, _pending = wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
            results: dict[tuple[int, int], _Tile] = {}
            for key, future in futures.items():
                if future in done:
                    results[key] = future.result()
                elif copies[key][1] is not None:  # no answer by the deadline: an old copy beats none
                    results[key] = _Tile(copies[key][1])
                else:
                    self._fail(matrix, key[0], key[1], "the render's deadline passed")
                    results[key] = _Tile(None)
            return results
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            client.close()

    def _tile(
        self,
        client: _Client,
        root: Path,
        matrix: TileMatrix,
        row: int,
        col: int,
        deadline: float | None,
        stored: Path | None,
        cached: tuple[np.ndarray, np.ndarray] | None,
    ) -> _Tile:
        """The answer for one tile, with its stored copy (`stored`, `cached`) read before the request. A tile
        that cannot be had is missing and adds a warning, except while the breaker is open: the breaker's
        warning covers it then. A 403 or 404 is an answer, remembered for `NO_TILE_TTL_S` with no request
        meanwhile. A failed revalidation keeps the stale copy and counts toward the breaker; a bad body on a
        revalidation counts as a failure, not a success. A disk error never raises: the decoded tile is used
        uncached."""
        size = (matrix.tile_width, matrix.tile_height)
        marker = _marker_path(root, matrix, row, col)
        if stored is not None and cached is not None and _age(stored) < REVALIDATE_AFTER_S:
            return _Tile(cached)
        if cached is None and _age(marker) < NO_TILE_TTL_S:
            return _Tile(None, absent=True)  # the service said so recently: no request is made
        url = _fill(self.source.service_url, matrix, row, col)
        headers = _validators(stored) if stored is not None and cached is not None else None
        try:
            reply = client.get(url, limit=MAX_TILE_BYTES, headers=headers, deadline=deadline)
        except _Missing:
            if cached is not None:
                return _Tile(cached)
            _mark_absent(marker)
            return _Tile(None, absent=True)
        except _BreakerOpen:
            return _Tile(cached)  # no tile warning: the breaker's one warning says why
        except (OSError, ValueError) as exc:
            if cached is not None:  # an old copy is better than none when the service cannot be asked
                return _Tile(cached)
            self._fail(matrix, row, col, _reason(exc))
            return _Tile(None)
        if reply.status == 304 and cached is not None and stored is not None:
            _settle(url, reply)
            _touch(stored)  # revalidated: the time is refreshed, and no body was sent
            return _Tile(cached)
        if reply.body is None:
            _settle(url, reply)
            self._fail(matrix, row, col, "the service answered without a tile")
            return _Tile(None)
        try:
            rgb, valid, fmt = _decode(reply.body, size)
        except _BadTile as exc:
            log.info("ČÚZK orthophoto: tile %s/%d/%d is not an image: %s", matrix.identifier, row, col, exc)
            if cached is not None:  # a bad answer to a revalidation keeps the stale copy, and it is a failure
                _breaker(self._host).failed("unusable tile", reply.probe)
                return _Tile(cached)
            _settle(url, reply)  # the host answered; this one image is bad
            self._fail(matrix, row, col, "unusable tile")
            return _Tile(None)
        _settle(url, reply)
        _unmark_absent(marker)
        try:
            image: Path | None = _store(root, matrix, row, col, reply.body, fmt, reply)
        except OSError:
            image = None  # the tile is good; only its copy on disk failed
        return _Tile((rgb, valid), image)

    def _fail(self, matrix: TileMatrix, row: int, col: int, reason: str) -> None:
        self._note(
            ("tile", matrix.identifier, row, col),
            f"{LABEL}: tile {matrix.identifier}/{row}/{col} not loaded ({reason}); "
            "shaded relief is shown there",
        )

    def _forget(self, matrix: TileMatrix, row: int, col: int) -> None:
        with self._lock:
            self._warnings.pop(("tile", matrix.identifier, row, col), None)

    def _note(self, key: tuple[object, ...], message: str) -> None:
        with self._lock:
            if key in self._warnings or len(self._warnings) < _MAX_WARNINGS:
                self._warnings[key] = message


@dataclass(frozen=True)
class _Tile:
    """What one tile of a render is: its pixels and their validity when loaded, the image file written, and
    `absent` when the service has no tile there. An absent tile is not valid, and it is not missing."""

    pixels: tuple[np.ndarray, np.ndarray] | None
    image: Path | None = None
    absent: bool = False


def _marker_path(root: Path, matrix: TileMatrix, row: int, col: int) -> Path:
    """Where a tile's 403 or 404 is remembered: a small file beside where the tile would be."""
    return root / matrix.identifier / str(row) / f"{col}.absent"


def _mark_absent(marker: Path) -> None:
    try:
        _atomic_write(marker, b"{}\n")
    except OSError:  # a marker that cannot be written only costs a request later
        pass


def _unmark_absent(marker: Path) -> None:
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def _reason(exc: BaseException) -> str:
    """The short phrase a person reads in a tile's warning; the exception text is for the log."""
    if isinstance(exc, _Exhausted):
        return exc.reason
    if isinstance(exc, _Deadline):
        return "the render's deadline passed"
    return str(exc)


def _clipped(bounds: Bounds, clip: Bounds) -> Bounds | None:
    """The window intersected with the clip box, or None when they share no area."""
    box = (max(bounds[0], clip[0]), max(bounds[1], clip[1]), min(bounds[2], clip[2]), min(bounds[3], clip[3]))
    return box if box[0] < box[2] and box[1] < box[3] else None


def _clip_mask(bounds: Bounds, size: int, clip: Bounds) -> np.ndarray:
    """Which pixels of the window have their centre inside the clip box (row 0 is north)."""
    min_e, _min_n, max_e, max_n = bounds
    step = (max_e - min_e) / size
    centres = np.arange(size, dtype=np.float64) + 0.5
    inside_e = (min_e + centres * step >= clip[0]) & (min_e + centres * step <= clip[2])
    inside_n = (max_n - centres * step >= clip[1]) & (max_n - centres * step <= clip[3])
    return inside_n[:, None] & inside_e[None, :]


@dataclass
class TileReport:
    """How many tiles the renders of one thread could not give. A composition is complete when it is zero."""

    missing: int = 0


@contextmanager
def tile_report() -> Iterator[TileReport]:
    """Collect, for the calling thread, the tiles that the WMTS renders run in it could not give. Each thread
    has its own report, so renders that run in parallel do not mix, and a composition is complete when none
    is missing. Nested reports each see their own renders."""
    report = TileReport()
    previous = getattr(_REPORTS, "current", None)
    _REPORTS.current = report
    try:
        yield report
    finally:
        _REPORTS.current = previous


def _record_missing(count: int) -> None:
    report = getattr(_REPORTS, "current", None)
    if report is not None:
        report.missing += count
