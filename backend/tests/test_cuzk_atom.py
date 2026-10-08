"""T-139: ČÚZK ATOM client — feed parsing, selection, conditional fetch, resumable downloads and the CLI.

Everything runs against a local HTTP server and synthetic feeds. The two tests marked `network` touch the live
ČÚZK services and run only with `-m network`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import shapely
from shapely.geometry import box

from coypu_builder.__main__ import main
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.io.gis import cuzk_atom
from coypu_builder.io.gis.cuzk_atom import (
    CuzkDataset,
    CuzkDownloadError,
    CuzkError,
    CuzkFeedError,
    CuzkHTTPError,
    CuzkSizeLimitError,
    SheetEntry,
    download,
    load_sheet_index,
    resolve_files,
    select_sheets,
)
from coypu_builder.io.gis.envelope import EnvelopeOptions, corridor_envelope, export_corridor_envelope

KROVAK = ProjectCRS("EPSG:5514")
REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLES = Path(os.environ.get("COYPU_TERRAIN_SAMPLES", r"D:\COYPU_Builder\Data\Terrain_Samples"))

# 2 x 2 synthetic sheets near Kralupy; each is 0.04 deg wide and 0.02 deg high.
LON0, LAT0, DLON, DLAT = 14.30, 50.20, 0.04, 0.02
GRID = {"TEST11": (0, 0), "TEST21": (1, 0), "TEST12": (0, 1), "TEST22": (1, 1)}


# --- local test server --------------------------------------------------------------------------------------


@dataclass
class Route:
    body: bytes
    etag: str | None = '"v1"'
    last_modified: str | None = "Mon, 19 Jan 2026 12:40:46 GMT"
    content_type: str = "application/octet-stream"
    get_body: bytes | None = None  # served on GET instead of `body` (HEAD still describes `body`)
    redirect: str | None = None  # answer 302 with this Location
    head_status: int | None = None  # answer HEAD with this status
    get_status: int | None = None  # answer GET with this status (HEAD still succeeds)


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    status: int = 0


@dataclass
class State:
    routes: dict[str, Route] = field(default_factory=dict)
    log: list[Seen] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    active: int = 0
    max_active: int = 0
    delay: float = 0.0
    fail_next: dict[str, list[int]] = field(default_factory=dict)
    cut_once: set[str] = field(default_factory=set)

    def requests(self, method: str | None = None, path: str | None = None) -> list[Seen]:
        return [r for r in self.log if (method is None or r.method == method) and path in (None, r.path)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def _handle(self) -> None:
        state: State = self.server.state
        self._released = False
        seen = Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
        with state.lock:
            state.log.append(seen)
            state.active += 1
            state.max_active = max(state.max_active, state.active)
        try:
            seen.status = self._serve(state, seen)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._release()

    def _release(self) -> None:
        """Count this request as finished just before its last byte leaves, so the client cannot start the
        next one while the server still counts this one as active."""
        if not self._released:
            self._released = True
            with self.server.state.lock:
                self.server.state.active -= 1

    def _reply(self, status: int, headers: dict[str, str], body: bytes = b"", cut: bool = False) -> int:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if self.command == "HEAD" or not body:
            self._release()
            self.end_headers()
            return status
        self.end_headers()
        state: State = self.server.state
        limit = len(body) // 2 if cut else len(body)
        for start in range(0, limit, 4096):
            if start + 4096 >= limit:
                self._release()
            self.wfile.write(body[start : min(start + 4096, limit)])
            self.wfile.flush()
            if state.delay and start + 4096 < limit:
                time.sleep(state.delay)
        return status

    def _serve(self, state: State, seen: Seen) -> int:
        with state.lock:
            queued = state.fail_next.get(self.path)
            status = queued.pop(0) if queued else None
        if status is not None:
            return self._reply(status, {"Content-Length": "0"})
        route = state.routes.get(self.path)
        if route is None:
            return self._reply(404, {"Content-Length": "0"})
        if route.redirect:
            return self._reply(302, {"Location": route.redirect, "Content-Length": "0"})
        forced = route.head_status if self.command == "HEAD" else route.get_status
        if forced:
            return self._reply(forced, {"Content-Length": "0"})
        base: dict[str, str] = {"Accept-Ranges": "bytes", "Content-Type": route.content_type}
        if route.etag:
            base["ETag"] = route.etag
        if route.last_modified:
            base["Last-Modified"] = route.last_modified
        if route.etag and seen.headers.get("if-none-match") == route.etag:
            return self._reply(304, base)
        body = route.get_body if (route.get_body is not None and self.command == "GET") else route.body
        total = len(body)
        range_header = seen.headers.get("range")
        if range_header and seen.headers.get("if-range", route.etag) == route.etag:
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            if start >= total:
                return self._reply(416, {**base, "Content-Range": f"bytes */{total}", "Content-Length": "0"})
            headers = {
                **base,
                "Content-Range": f"bytes {start}-{total - 1}/{total}",
                "Content-Length": str(total - start),
            }
            return self._reply(206, headers, body[start:])
        cut = self.command == "GET" and self.path in state.cut_once
        if cut:
            state.cut_once.discard(self.path)
        return self._reply(200, {**base, "Content-Length": str(total)}, body, cut=cut)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.state = State()
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    httpd.state.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield httpd.state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


DEFAULT_SCHEMES = cuzk_atom.ALLOWED_SCHEMES
DEFAULT_HOSTS = cuzk_atom.ALLOWED_HOSTS


@pytest.fixture(autouse=True)
def allow_local_server(monkeypatch):
    """The allow-list stays on; the tests only add the plain-http loopback server to it."""
    monkeypatch.setattr(cuzk_atom, "ALLOWED_SCHEMES", ("http", "https"))
    monkeypatch.setattr(cuzk_atom, "ALLOWED_HOSTS", ("127.0.0.1", *DEFAULT_HOSTS))


@pytest.fixture
def sent(monkeypatch):
    """Every URL a request was actually attempted against, before it leaves the process."""
    import requests

    urls: list[str] = []
    original = requests.Session.request

    def spy(self, method, url, **kwargs):
        urls.append(url)
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", spy)
    return urls


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Backoff sleeps are recorded, not waited out."""
    recorded: list[float] = []
    monkeypatch.setattr(cuzk_atom, "_sleep", recorded.append)
    return recorded


# --- synthetic feeds ----------------------------------------------------------------------------------------


def _polygon_text(lon0: float, lat0: float, lon1: float, lat1: float) -> str:
    """georss:polygon order: latitude first."""
    ring = [(lat0, lon0), (lat0, lon1), (lat1, lon1), (lat1, lon0), (lat0, lon0)]
    return " ".join(f"{lat} {lon}" for lat, lon in ring)


def _grid_bounds(code: str) -> tuple[float, float, float, float]:
    col, row = GRID[code]
    return LON0 + col * DLON, LAT0 + row * DLAT, LON0 + (col + 1) * DLON, LAT0 + (row + 1) * DLAT


def _entry_xml(ident: str, title: str, feed_url: str, polygon: str, updated: str) -> str:
    return f"""  <entry>
    <id>{feed_url}</id>
    <title>{title}</title>
    <updated>{updated}</updated>
    <link href="{feed_url}" rel="alternate" title="dataset feed" type="application/atom+xml" />
    <inspire_dls:spatial_dataset_identifier_code>{ident}</inspire_dls:spatial_dataset_identifier_code>
    <georss:polygon>{polygon}</georss:polygon>
  </entry>
"""


def _service_feed(entries: list[str]) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        '<feed xmlns:georss="http://www.georss.org/georss" '
        'xmlns:inspire_dls="http://inspire.ec.europa.eu/schemas/inspire_dls/1.0" '
        'xmlns="http://www.w3.org/2005/Atom">\n  <title>synthetic service</title>\n'
        + "".join(entries)
        + "</feed>\n"
    ).encode()


def _dataset_feed(file_url: str, length: int, polygon: str) -> bytes:
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns:georss="http://www.georss.org/georss" xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>{file_url}</id>
    <title>synthetic</title>
    <updated>2026-01-19T13:40:46+02:00</updated>
    <link href="{file_url}" length="{length}" rel="alternate" type="application/vnd.laszip" />
    <georss:polygon>{polygon}</georss:polygon>
  </entry>
</feed>
""".encode()


def _body(code: str, size: int = 9000) -> bytes:
    seed = hashlib.sha256(code.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def _publish(state: State, codes: list[str], dataset: CuzkDataset = CuzkDataset.DMR5G, sizes=None) -> None:
    """Serve a service feed for `codes` plus each sheet's dataset feed and ZIP; point SERVICE_FEEDS at it."""
    entries = []
    for code in codes:
        polygon = _polygon_text(*_grid_bounds(code))
        feed_path = f"/datasetFeeds/{code}.xml"
        file_path = f"/files/{code}.zip"
        body = _body(code, (sizes or {}).get(code, 9000))
        entries.append(
            _entry_xml(
                f"CZ-00025712-CUZK_{dataset.value}_{code}",
                f"Synthetic - mapový list: Sheet {code}",
                state.base + feed_path,
                polygon,
                "2026-01-19T13:40:46+02:00",
            )
        )
        state.routes[feed_path] = Route(
            _dataset_feed(state.base + file_path, len(body), polygon), etag=f'"feed-{code}"'
        )
        state.routes[file_path] = Route(body, etag=f'"zip-{code}"')
    state.routes["/service.xml"] = Route(_service_feed(entries), etag='"service-1"')


@pytest.fixture
def world(server, tmp_path, monkeypatch):
    monkeypatch.setitem(cuzk_atom.SERVICE_FEEDS, CuzkDataset.DMR5G, server.base + "/service.xml")
    _publish(server, list(GRID))
    server.cache = tmp_path / "cache"
    server.out = tmp_path / "out"
    return server


def _envelope_across(code_a: str, code_b: str):
    """A small box centred on the shared edge of two horizontally adjacent sheets, in S-JTSK."""
    lon_a0, lat0, lon_a1, lat1 = _grid_bounds(code_a)
    lon, lat = lon_a1, (lat0 + lat1) / 2
    x, y = KROVAK.from_crs("EPSG:4326", [lon], [lat])
    return box(x[0] - 300, y[0] - 300, x[0] + 300, y[0] + 300)


def _stamp() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


# --- 1. feed parsing ----------------------------------------------------------------------------------------


def test_parse_feed_entries_in_lonlat_order():
    entries = [
        _entry_xml(
            "CZ-00025712-CUZK_DMR5G-SJTSK_KRAV82",
            "Digitální model reliéfu - mapový list: Kralupy nad Vltavou 8-2",
            "https://atom.cuzk.gov.cz/KRAV82.xml",
            "50.2332 14.3198 50.2332 14.3583 50.2541 14.3583 50.2541 14.3198 50.2332 14.3198",
            "2026-01-19T13:40:46+02:00",
        )
    ]
    (sheet,) = cuzk_atom._parse_service_feed(_service_feed(entries), CuzkDataset.DMR5G)
    assert (sheet.code, sheet.edition, sheet.name) == ("KRAV82", None, "Kralupy nad Vltavou 8-2")
    assert sheet.dataset_id == "CZ-00025712-CUZK_DMR5G-SJTSK_KRAV82"
    assert sheet.dataset_feed_url == "https://atom.cuzk.gov.cz/KRAV82.xml"
    assert sheet.updated.isoformat() == "2026-01-19T13:40:46+02:00"
    assert sheet.outline_lonlat[0] == (14.3198, 50.2332)
    assert sheet.outline_lonlat[0] == sheet.outline_lonlat[-1] and len(sheet.outline_lonlat) == 5
    assert all(14.0 < lon < 15.0 and 50.0 < lat < 51.0 for lon, lat in sheet.outline_lonlat)


def test_orthophoto_edition_is_split_off():
    entries = [
        _entry_xml(
            "CZ-00025712-CUZK_ORTOFOTO_WRTO24.2025.KRAV82",
            "Ortofoto - mapový list: Kralupy nad Vltavou 8-2",
            "https://atom.cuzk.gov.cz/o.xml",
            _polygon_text(14.3198, 50.2332, 14.3583, 50.2541),
            "2026-03-11T08:42:29+02:00",
        )
    ]
    (sheet,) = cuzk_atom._parse_service_feed(_service_feed(entries), CuzkDataset.ORTOFOTO)
    assert (sheet.code, sheet.edition, sheet.stem) == ("KRAV82", "WRTO24.2025", "KRAV82.WRTO24.2025")
    assert cuzk_atom._split_identifier("CZ-00025712-CUZK_DMR4G-SJTSK-TIFF_BENE09") == ("BENE09", None)


def test_feed_with_entity_expansion_is_rejected():
    payload = b"""<?xml version="1.0"?>
<!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>
<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>"""
    with pytest.raises(CuzkFeedError, match="unsafe XML"):
        cuzk_atom._parse_service_feed(payload, CuzkDataset.DMR5G)
    with pytest.raises(CuzkFeedError):
        cuzk_atom._parse_service_feed(b"<feed", CuzkDataset.DMR5G)


def one(ident: str, link: str = "https://atom.cuzk.gov.cz/x.xml") -> bytes:
    return _service_feed(
        [
            _entry_xml(
                ident,
                "t - mapový list: x",
                link,
                _polygon_text(14.0, 50.0, 14.1, 50.1),
                "2026-01-01T00:00:00+00:00",
            )
        ]
    )


def test_unsafe_sheet_code_and_bad_links_are_rejected():
    with pytest.raises(CuzkFeedError, match="unsafe sheet code"):
        cuzk_atom._parse_service_feed(one("CZ-CUZK_DMR5G_..\\evil"), CuzkDataset.DMR5G)
    with pytest.raises(CuzkFeedError, match="refused URL"):
        cuzk_atom._parse_service_feed(one("CZ-CUZK_DMR5G_AB12", "file:///etc/passwd"), CuzkDataset.DMR5G)


GOOD_URLS = [
    "https://atom.cuzk.gov.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml",
    "https://openzu.cuzk.gov.cz/opendata/DMR5G/epsg-5514/KRAV82.zip",
    "https://atom.cuzk.cz/DMP1G-SJTSK/datasetFeeds/x.xml",
    "https://openzu.cuzk.cz/opendata/DMP1G/epsg-5514/KRAV82.zip",
    "https://cuzk.gov.cz/x",
    "https://ATOM.CUZK.GOV.CZ:443/x",
]
BAD_URLS = [
    ("http://atom.cuzk.gov.cz/x.xml", "scheme"),
    ("ftp://atom.cuzk.gov.cz/x.xml", "scheme"),
    ("https://evil.example/x.xml", "not a ČÚZK host"),
    ("https://127.0.0.1/x.xml", "not a ČÚZK host"),
    ("https://cuzk.gov.cz@evil.example/x.xml", "userinfo"),
    ("https://user:pw@atom.cuzk.gov.cz/x.xml", "userinfo"),
    ("https://cuzk.gov.cz.evil.com/x.xml", "not a ČÚZK host"),
    ("https://evilcuzk.gov.cz/x.xml", "not a ČÚZK host"),
    ("https://atom.cuzk.gov.cz.evil.com/x.xml", "not a ČÚZK host"),
    (r"https://evil.example\@atom.cuzk.gov.cz/x.xml", "unexpected characters"),
    ("https://atom.cuzk.gov.cz/x y.xml", "unexpected characters"),
    ("//atom.cuzk.gov.cz/x.xml", "scheme"),
]


@pytest.mark.parametrize("url", GOOD_URLS)
def test_cuzk_hosts_pass_the_allow_list(url, monkeypatch):
    monkeypatch.setattr(cuzk_atom, "ALLOWED_SCHEMES", DEFAULT_SCHEMES)
    monkeypatch.setattr(cuzk_atom, "ALLOWED_HOSTS", DEFAULT_HOSTS)
    assert cuzk_atom._check_url(url, "test") == url


@pytest.mark.parametrize(("url", "reason"), BAD_URLS)
def test_other_urls_are_refused_in_feeds_and_before_any_request(url, reason, monkeypatch, sent):
    monkeypatch.setattr(cuzk_atom, "ALLOWED_SCHEMES", DEFAULT_SCHEMES)
    monkeypatch.setattr(cuzk_atom, "ALLOWED_HOSTS", DEFAULT_HOSTS)
    with pytest.raises(CuzkFeedError, match=reason):
        cuzk_atom._check_url(url, "test")
    # a feed naming it as the dataset feed or as the file link is refused at parse time
    with pytest.raises(CuzkFeedError, match="refused URL"):
        cuzk_atom._parse_service_feed(one("CZ-CUZK_DMR5G_AB12", url), CuzkDataset.DMR5G)
    sheet = SheetEntry(
        CuzkDataset.DMR5G, "AB12", "x", None, "id", _stamp(), (), "https://atom.cuzk.gov.cz/f.xml"
    )
    with pytest.raises(CuzkFeedError, match="refused URL"):
        cuzk_atom._parse_dataset_feed(_dataset_feed(url, 10, _polygon_text(14.0, 50.0, 14.1, 50.1)), sheet)
    with pytest.raises(CuzkFeedError, match="refused URL"):
        cuzk_atom._request(cuzk_atom._session(), "GET", url)
    assert sent == []


# --- 2. selection -------------------------------------------------------------------------------------------


def test_selection_picks_exactly_the_two_overlapped_sheets(world):
    index = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    assert [s.code for s in index] == list(GRID)
    chosen = select_sheets(index, _envelope_across("TEST11", "TEST21"), KROVAK)
    assert {s.code for s in chosen} == {"TEST11", "TEST21"}
    assert {s.code for s in select_sheets(index, _envelope_across("TEST12", "TEST22"), KROVAK)} == {
        "TEST12",
        "TEST22",
    }


def test_swapped_axis_outline_would_select_nothing(world):
    index = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    envelope = _envelope_across("TEST11", "TEST21")
    assert len(select_sheets(index, envelope, KROVAK)) == 2
    assert all(14.0 < lon < 15.0 and 50.0 < lat < 51.0 for s in index for lon, lat in s.outline_lonlat)
    # The same outlines with latitude and longitude exchanged lie near 14 N 50 E, far outside Bohemia, so
    # they transform to non-finite or distant S-JTSK values and match no corridor.
    swapped = [replace(s, outline_lonlat=tuple((lat, lon) for lon, lat in s.outline_lonlat)) for s in index]
    assert select_sheets(swapped, envelope, KROVAK) == []


def test_sheets_only_touching_the_envelope_are_not_selected():
    wgs = ProjectCRS("EPSG:4326")
    ring = ((14.0, 50.0), (15.0, 50.0), (15.0, 51.0), (14.0, 51.0), (14.0, 50.0))
    sheet = SheetEntry(
        CuzkDataset.DMR5G, "T1", "T1", None, "id", _stamp(), ring, "https://example.test/f.xml"
    )
    assert select_sheets([sheet], box(15.0, 50.2, 16.0, 50.4), wgs) == []
    assert select_sheets([sheet], box(14.9, 50.2, 16.0, 50.4), wgs) == [sheet]
    assert select_sheets([], box(0, 0, 1, 1), wgs) == []


# --- 3. conditional fetch -----------------------------------------------------------------------------------


def test_second_index_load_is_a_304_and_skips_parsing(world, monkeypatch):
    calls = []
    real = cuzk_atom._parse_service_feed
    monkeypatch.setattr(cuzk_atom, "_parse_service_feed", lambda c, d: calls.append(1) or real(c, d))

    first = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    second = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    assert first == second and len(calls) == 1
    first_request, second_request = world.requests("GET", "/service.xml")
    assert "if-none-match" not in first_request.headers and first_request.status == 200
    assert second_request.headers["if-none-match"] == '"service-1"'
    assert second_request.headers["if-modified-since"] == "Mon, 19 Jan 2026 12:40:46 GMT"
    assert second_request.status == 304

    refreshed = load_sheet_index(CuzkDataset.DMR5G, world.cache, refresh=True)
    assert refreshed == first and len(calls) == 2
    assert "if-none-match" not in world.requests("GET", "/service.xml")[2].headers

    route = world.routes["/service.xml"]
    world.routes["/service.xml"] = Route(route.body, etag='"service-2"', last_modified=route.last_modified)
    load_sheet_index(CuzkDataset.DMR5G, world.cache)
    assert len(calls) == 3


def test_unsafe_feed_is_never_cached(world):
    world.routes["/service.xml"] = Route(
        b'<?xml version="1.0"?><!DOCTYPE f [<!ENTITY a "x">]><feed xmlns="http://www.w3.org/2005/Atom"/>'
    )
    with pytest.raises(CuzkFeedError):
        load_sheet_index(CuzkDataset.DMR5G, world.cache)
    assert not (world.cache / "DMR5G.index.json").exists()


def test_dataset_feeds_are_resolved_and_cached(world):
    index = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    files = resolve_files(index, world.cache)
    assert [f.sheet.code for f in files] == list(GRID)
    assert all(f.length == 9000 and f.url.startswith(world.base + "/files/") for f in files)
    assert files[0].media_type == "application/vnd.laszip"
    again = resolve_files(index, world.cache)
    assert again == files
    conditional = [r for r in world.requests("GET") if r.path.startswith("/datasetFeeds/TEST11")]
    assert [r.status for r in conditional] == [200, 304]
    assert conditional[1].headers["if-none-match"] == '"feed-TEST11"'


def test_user_agent_names_the_product_and_version(world):
    load_sheet_index(CuzkDataset.DMR5G, world.cache)
    agent = world.requests("GET", "/service.xml")[0].headers["user-agent"]
    assert agent.startswith("COYPU-Builder/") and cuzk_atom.__version__ in agent


# --- 4. resume and integrity --------------------------------------------------------------------------------


def _files(world, codes=None):
    index = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    sheets = [s for s in index if codes is None or s.code in codes]
    return resolve_files(sheets, world.cache)


def test_download_writes_files_and_manifest(world):
    files = _files(world, {"TEST11", "TEST21"})
    records = download(files, world.out)
    assert [r.file for r in records] == ["DMR5G/TEST11.zip", "DMR5G/TEST21.zip"]
    for record in records:
        data = (world.out / record.file).read_bytes()
        assert data == _body(record.code)
        assert record.sha256 == hashlib.sha256(data).hexdigest()
        assert record.length == len(data)
        assert record.etag == f'"zip-{record.code}"'
        assert record.last_modified == "Mon, 19 Jan 2026 12:40:46 GMT"
        assert record.feed_updated == "2026-01-19T13:40:46+02:00"
        assert record.downloaded_at.endswith("+00:00")
    manifest = json.loads((world.out / "cuzk_manifest.json").read_text(encoding="utf-8"))
    assert isinstance(manifest, list) and [m["code"] for m in manifest] == ["TEST11", "TEST21"]
    assert set(manifest[0]) == set(cuzk_atom.DownloadRecord.__dataclass_fields__)
    assert not list(world.out.rglob("*.part*"))


def test_transfer_cut_off_midway_completes_with_a_range_request(world, sleeps, monkeypatch):
    monkeypatch.setattr(cuzk_atom, "_CHUNK", 1024)
    world.cut_once.add("/files/TEST11.zip")
    (record,) = download(_files(world, {"TEST11"}), world.out)
    assert (world.out / record.file).read_bytes() == _body("TEST11")
    gets = world.requests("GET", "/files/TEST11.zip")
    assert len(gets) == 2 and "range" not in gets[0].headers
    resumed_at = int(gets[1].headers["range"].removeprefix("bytes=").rstrip("-"))
    assert 0 < resumed_at <= 4500
    assert gets[1].headers["if-range"] == '"zip-TEST11"'
    assert sleeps == [cuzk_atom._BACKOFF_BASE_S]
    assert record.sha256 == hashlib.sha256(_body("TEST11")).hexdigest()


def _leave_part(world, size: int, probe: cuzk_atom._Probe) -> Path:
    part = world.out / "DMR5G" / "TEST11.zip.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(_body("TEST11")[:size])
    cuzk_atom._atomic_write(
        cuzk_atom._validator_path(part),
        json.dumps({"etag": probe.etag, "last_modified": probe.last_modified}),
    )
    return part


def test_a_part_file_of_the_same_version_is_resumed_across_runs(world):
    route = world.routes["/files/TEST11.zip"]
    part = _leave_part(world, 1234, cuzk_atom._Probe(route.etag, route.last_modified, 9000))
    (record,) = download(_files(world, {"TEST11"}), world.out)
    (get,) = world.requests("GET", "/files/TEST11.zip")
    assert get.headers["range"] == "bytes=1234-" and get.headers["if-range"] == route.etag
    assert (world.out / record.file).read_bytes() == _body("TEST11")
    assert not part.exists() and not cuzk_atom._validator_path(part).exists()


def test_a_part_file_of_another_version_or_without_validators_is_restarted(world):
    route = world.routes["/files/TEST11.zip"]
    stale = cuzk_atom._Probe('"zip-TEST11-old"', route.last_modified, 9000)
    _leave_part(world, 1234, stale)
    (record,) = download(_files(world, {"TEST11"}), world.out)
    (get,) = world.requests("GET", "/files/TEST11.zip")
    assert "range" not in get.headers
    assert (world.out / record.file).read_bytes() == _body("TEST11")

    other = world.out / "DMR5G" / "TEST21.zip.part"
    other.write_bytes(b"bare partial file")
    (second,) = download(_files(world, {"TEST21"}), world.out)
    assert "range" not in world.requests("GET", "/files/TEST21.zip")[0].headers
    assert (world.out / second.file).read_bytes() == _body("TEST21")


def test_size_mismatch_fails_and_never_produces_a_zip(world):
    (file,) = _files(world, {"TEST11"})
    wrong = cuzk_atom.SheetFile(file.sheet, file.url, file.length + 5, file.media_type)
    with pytest.raises(CuzkDownloadError, match="9000 bytes but the feed says 9005"):
        download([wrong], world.out)
    assert not list(world.out.rglob("*.zip")) and not list(world.out.rglob("*.zip.part"))
    assert not (world.out / "cuzk_manifest.json").exists()
    assert not world.requests("GET", "/files/TEST11.zip")


def test_body_that_disagrees_with_the_head_never_becomes_a_zip(world):
    world.routes["/files/TEST11.zip"] = Route(_body("TEST11"), get_body=_body("TEST11")[:-10] + b"x" * 40)
    with pytest.raises(CuzkDownloadError, match="9030 bytes but the feed says 9000"):
        download(_files(world, {"TEST11"}), world.out)
    assert not list(world.out.rglob("*.zip")) and not list(world.out.rglob("*.part"))
    assert not (world.out / "cuzk_manifest.json").exists()


# --- 5. idempotence -----------------------------------------------------------------------------------------


def test_rerun_downloads_nothing_and_a_changed_etag_downloads_exactly_that_file(world):
    files = _files(world, {"TEST11", "TEST21", "TEST12"})
    first = download(files, world.out)
    gets = len(world.requests("GET", "/files/TEST11.zip")) + len(world.requests("GET", "/files/TEST21.zip"))
    gets += len(world.requests("GET", "/files/TEST12.zip"))
    assert gets == 3

    second = download(files, world.out)
    assert second == first
    assert sum(len(world.requests("GET", f"/files/{c}.zip")) for c in ("TEST11", "TEST21", "TEST12")) == 3

    world.routes["/files/TEST21.zip"] = Route(_body("TEST21")[::-1], etag='"zip-TEST21-b"')
    third = download(files, world.out)
    assert len(world.requests("GET", "/files/TEST21.zip")) == 2
    assert len(world.requests("GET", "/files/TEST11.zip")) == 1
    assert len(world.requests("GET", "/files/TEST12.zip")) == 1
    changed = {r.code: r for r in third}
    assert changed["TEST21"].etag == '"zip-TEST21-b"'
    assert changed["TEST21"].sha256 == hashlib.sha256(_body("TEST21")[::-1]).hexdigest()
    assert changed["TEST11"] == {r.code: r for r in first}["TEST11"]
    manifest = json.loads((world.out / "cuzk_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest) == 3


def test_manifest_keeps_entries_of_other_sheets(world):
    download(_files(world, {"TEST11"}), world.out)
    download(_files(world, {"TEST21"}), world.out)
    manifest = json.loads((world.out / "cuzk_manifest.json").read_text(encoding="utf-8"))
    assert sorted(m["code"] for m in manifest) == ["TEST11", "TEST21"]


# --- 6. size guard ------------------------------------------------------------------------------------------


def test_selection_over_the_size_limit_is_refused_with_its_total(world):
    files = _files(world)
    before = len(world.log)
    with pytest.raises(CuzkSizeLimitError, match=r"total 0\.04 MB, over the limit of 0\.01 MB"):
        download(files, world.out, max_total_mb=0.01)
    assert len(world.log) == before and not world.out.exists()
    assert len(download(files, world.out, max_total_mb=0.04)) == 4


# --- 7. politeness ------------------------------------------------------------------------------------------


def _many(world, count: int) -> list:
    codes = [f"S{i:03d}" for i in range(count)]
    index = []
    for code in codes:
        body = _body(code, 20000)
        world.routes[f"/files/{code}.zip"] = Route(body, etag=f'"zip-{code}"')
        sheet = SheetEntry(
            CuzkDataset.DMR5G, code, code, None, f"id-{code}", _stamp(), (), world.base + "/unused.xml"
        )
        index.append(cuzk_atom.SheetFile(sheet, world.base + f"/files/{code}.zip", len(body), "x/y"))
    return index


def test_concurrency_is_bounded(world):
    world.delay = 0.02
    files = _many(world, 6)
    download(files, world.out, concurrency=2)
    assert world.max_active == 2

    world.max_active, world.delay = 0, 0.01
    files = _many(world, 10)
    download(files, world.out / "wide", concurrency=9)
    assert 2 <= world.max_active <= cuzk_atom.MAX_CONCURRENCY

    with pytest.raises(ValueError, match="concurrency"):
        download(files, world.out, concurrency=0)


def test_5xx_is_retried_with_exponential_backoff_and_404_is_not(world, sleeps):
    world.fail_next["/files/TEST11.zip"] = [503, 503]
    (record,) = download(_files(world, {"TEST11"}), world.out)
    assert (world.out / record.file).exists()
    assert [r.method for r in world.requests(path="/files/TEST11.zip")] == ["HEAD", "HEAD", "HEAD", "GET"]
    assert sleeps == [cuzk_atom._BACKOFF_BASE_S, 2 * cuzk_atom._BACKOFF_BASE_S]

    sleeps.clear()
    world.fail_next["/files/TEST21.zip"] = [503, 503, 503]
    with pytest.raises(CuzkError, match="giving up after 3 attempts"):
        download(_files(world, {"TEST21"}), world.out)
    assert len(world.requests("HEAD", "/files/TEST21.zip")) == 3 and len(sleeps) == 2

    del world.routes["/files/TEST12.zip"]
    with pytest.raises(CuzkHTTPError) as caught:
        download(_files(world, {"TEST12"}), world.out)
    assert caught.value.status == 404 and len(world.requests(path="/files/TEST12.zip")) == 1
    assert len(sleeps) == 2


# --- extras: repository guard, cache dir, envelope files, CLI -----------------------------------------------


@pytest.mark.skipif(not (REPO_ROOT / ".git").exists(), reason="not a git checkout")
def test_download_refuses_a_directory_inside_the_repository(world):
    inside = REPO_ROOT / "backend" / "tests" / "_cuzk_never_written"
    with pytest.raises(CuzkDownloadError, match="inside|into the repository"):
        download(_files(world, {"TEST11"}), inside)
    assert not inside.exists() and not world.requests("GET", "/files/TEST11.zip")


def test_default_cache_dir_per_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(cuzk_atom.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cuzk_atom.default_cache_dir().parts[-3:] == ("COYPU Builder", "cache", "cuzk-atom")
    assert cuzk_atom.default_cache_dir().parts[: len(tmp_path.parts)] == tmp_path.parts
    monkeypatch.setattr(cuzk_atom.sys, "platform", "linux")
    assert cuzk_atom.default_cache_dir() == Path.home() / ".cache" / "coypu-builder" / "cuzk-atom"


def test_dataset_names_parse():
    assert CuzkDataset.parse("dmr4g") is CuzkDataset.DMR4G
    assert CuzkDataset.parse("DMR4G-TIFF") is CuzkDataset.DMR4G
    with pytest.raises(ValueError, match="unknown"):
        CuzkDataset.parse("zabaged")


def _big_sheet_feed(world) -> None:
    """One synthetic sheet that covers the whole Kralupy fixture, served as the DMR5G service."""
    polygon = _polygon_text(13.9, 49.9, 14.9, 50.5)
    feed_path, file_path = "/datasetFeeds/BIG.xml", "/files/BIG.zip"
    body = _body("BIG", 6000)
    world.routes["/service.xml"] = Route(
        _service_feed(
            [
                _entry_xml(
                    "CZ-00025712-CUZK_DMR5G-SJTSK_BIG01",
                    "t - mapový list: Big 0-1",
                    world.base + feed_path,
                    polygon,
                    "2026-01-19T13:40:46+02:00",
                )
            ]
        )
    )
    world.routes[feed_path] = Route(_dataset_feed(world.base + file_path, len(body), polygon))
    world.routes[file_path] = Route(body)


def test_cli_sheets_and_download_from_landxml(world, kralupy_xml, monkeypatch, capsys):
    monkeypatch.setattr(cuzk_atom, "default_cache_dir", lambda: world.cache)
    _big_sheet_feed(world)
    args = [str(kralupy_xml), "--buffer", "250", "--dataset", "DMR5G"]
    assert main(["cuzk", "sheets", *args]) == 0
    listing = capsys.readouterr().out
    assert "BIG01" in listing and "Big 0-1" in listing and "sheets:   1, total 0.01 MB" in listing

    assert main(["cuzk", "download", *args, "--out", str(world.out), "--concurrency", "1"]) == 0
    out = capsys.readouterr().out
    assert "1 downloaded, 0 up to date" in out
    assert main(["cuzk", "download", *args, "--out", str(world.out)]) == 0
    assert "0 downloaded, 1 up to date" in capsys.readouterr().out
    assert (world.out / "DMR5G" / "BIG01.zip").read_bytes() == _body("BIG", 6000)


def test_cli_refuses_bad_input(world, kralupy_xml, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cuzk_atom, "default_cache_dir", lambda: world.cache)
    _big_sheet_feed(world)
    base = ["cuzk", "sheets", "--dataset", "DMR5G"]
    assert main([*base]) == 1 and "exactly one of" in capsys.readouterr().err
    assert main([*base, str(kralupy_xml), "--envelope", str(tmp_path / "e.geojson")]) == 1
    assert main(["cuzk", "sheets", str(kralupy_xml), "--dataset", "NOPE"]) == 1
    assert "unknown" in capsys.readouterr().err
    if (REPO_ROOT / ".git").exists():
        inside = REPO_ROOT / "backend" / "_cuzk_never_written"
        code = main(["cuzk", "download", str(kralupy_xml), "--dataset", "DMR5G", "--out", str(inside)])
        assert code == 1 and "repository" in capsys.readouterr().err and not inside.exists()
    big = ["cuzk", "download", str(kralupy_xml), "--dataset", "DMR5G", "--out", str(tmp_path / "o")]
    assert main([*big, "--max-total-mb", "0.001"]) == 1
    assert "over the limit" in capsys.readouterr().err


@pytest.mark.parametrize("suffix", [".geojson", ".shp"])
def test_envelope_files_drive_the_selection(world, kralupy, monkeypatch, capsys, tmp_path, suffix):
    monkeypatch.setattr(cuzk_atom, "default_cache_dir", lambda: world.cache)
    _big_sheet_feed(world)
    path = tmp_path / f"corridor{suffix}"
    export_corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=250.0), KROVAK, path)

    from coypu_builder.cli.cuzk import read_envelope_file

    geometry, crs = read_envelope_file(path)
    expected = corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=250.0))
    assert (crs.epsg == 4326) == (suffix == ".geojson")
    xs, ys = KROVAK.from_crs(crs.crs, *shapely.get_coordinates(geometry).T)
    got = (xs.min(), ys.min(), xs.max(), ys.max())
    assert got == pytest.approx(expected.bounds, abs=0.5)

    assert main(["cuzk", "sheets", "--envelope", str(path), "--dataset", "DMR5G"]) == 0
    assert "BIG01" in capsys.readouterr().out


# --- hardening: redirects, HEAD fallback, dropped feeds, cleanup, unreadable envelopes ---------------------


def test_redirect_to_a_foreign_host_is_refused_before_it_is_requested(world, sent):
    world.routes["/files/TEST11.zip"] = Route(b"", redirect="https://evil.example/files/TEST11.zip")
    files = _files(world, {"TEST11"})
    sent.clear()
    with pytest.raises(CuzkFeedError, match="refused URL .*evil.example"):
        download(files, world.out)
    assert sent and not any("evil.example" in url for url in sent)
    assert not world.requests("GET", "/files/TEST11.zip") and not list(world.out.rglob("*.part*"))

    for target in ("http://cuzk.gov.cz@evil.example/x", "https://cuzk.gov.cz.evil.example/x"):
        world.routes["/service.xml"] = Route(b"", redirect=target)
        with pytest.raises(CuzkFeedError, match="refused URL"):
            load_sheet_index(CuzkDataset.DMR5G, world.cache, refresh=True)
    assert not any("evil.example" in url for url in sent)


def test_redirects_inside_the_allow_list_are_followed_up_to_a_limit(world, sent):
    world.routes["/files/TEST11.zip"] = Route(b"", redirect=world.base + "/files/TEST21.zip")
    (record,) = download(_files(world, {"TEST11"}), world.out)
    assert (world.out / record.file).read_bytes() == _body("TEST21")

    world.routes["/loop"] = Route(b"", redirect=world.base + "/loop")
    with pytest.raises(CuzkError, match="more than 5 redirects"):
        cuzk_atom._request(cuzk_atom._session(), "GET", world.base + "/loop")
    assert len(world.requests("GET", "/loop")) == cuzk_atom._MAX_REDIRECTS + 1


@pytest.mark.parametrize("status", [405, 501])
def test_head_that_the_server_does_not_support_falls_back_to_get(world, sleeps, status):
    world.routes["/files/TEST11.zip"].head_status = status
    (record,) = download(_files(world, {"TEST11"}), world.out)
    assert (world.out / record.file).read_bytes() == _body("TEST11")
    assert len(world.requests("HEAD", "/files/TEST11.zip")) == 1 and sleeps == []
    assert len(world.requests("GET", "/files/TEST11.zip")) == 2
    assert record.etag == '"zip-TEST11"'


def test_feed_dropped_mid_body_is_retried_like_a_timeout(world, sleeps):
    world.cut_once.add("/service.xml")
    index = load_sheet_index(CuzkDataset.DMR5G, world.cache)
    assert [s.code for s in index] == list(GRID)
    assert len(world.requests("GET", "/service.xml")) == 2
    assert sleeps == [cuzk_atom._BACKOFF_BASE_S]

    sleeps.clear()
    world.cut_once.add("/datasetFeeds/TEST11.xml")
    (file,) = resolve_files(index[:1], world.cache / "other")
    assert file.length == 9000 and sleeps == [cuzk_atom._BACKOFF_BASE_S]


def test_a_4xx_on_the_file_get_leaves_no_empty_part_or_validator(world):
    (file,) = _files(world, {"TEST11"})
    world.routes["/files/TEST11.zip"].get_status = 403
    with pytest.raises(CuzkHTTPError) as caught:
        download([file], world.out)
    assert caught.value.status == 403
    assert len(world.requests("HEAD", "/files/TEST11.zip")) == 1
    assert len(world.requests("GET", "/files/TEST11.zip")) == 1
    assert not list(world.out.rglob("*.part*")) and not list(world.out.rglob("*.zip"))


def test_a_failed_download_keeps_a_part_that_holds_data(world):
    route = world.routes["/files/TEST11.zip"]
    part = _leave_part(world, 1234, cuzk_atom._Probe(route.etag, route.last_modified, 9000))
    route.get_status = 403
    with pytest.raises(CuzkHTTPError):
        download(_files(world, {"TEST11"}), world.out)
    assert part.stat().st_size == 1234 and cuzk_atom._validator_path(part).exists()


@pytest.mark.parametrize(
    "document",
    [
        '{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":null}]}',
        '{"type":"Feature","properties":{},"geometry":null}',
        '{"type":"FeatureCollection","features":[]}',
        '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Polygon","coordinates":"x"}}]}',
        '{"type":"FeatureCollection"}',
        "not json at all",
    ],
)
def test_unreadable_envelope_files_are_a_one_line_error(world, monkeypatch, capsys, tmp_path, document):
    monkeypatch.setattr(cuzk_atom, "default_cache_dir", lambda: world.cache)
    path = tmp_path / "bad.geojson"
    path.write_text(document, encoding="utf-8")
    assert main(["cuzk", "sheets", "--envelope", str(path), "--dataset", "DMR5G"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and len(err.strip().splitlines()) == 1 and "Traceback" not in err


# --- 8. live checks (-m network) ----------------------------------------------------------------------------


@pytest.mark.network
def test_live_kralupy_dmr5g_selection_and_krav82_download(kralupy, tmp_path):
    cache = tmp_path / "cache"
    envelope = corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=250.0))

    started = time.perf_counter()
    index = load_sheet_index(CuzkDataset.DMR5G, cache)
    cold = time.perf_counter() - started
    started = time.perf_counter()
    again = load_sheet_index(CuzkDataset.DMR5G, cache)
    cached = time.perf_counter() - started
    assert again == index
    print(f"index load: cold {cold:.2f} s, cached {cached:.2f} s")
    assert len(index) > 16000

    sheets = select_sheets(index, envelope, KROVAK)
    codes = {s.code for s in sheets}
    assert 12 <= len(sheets) <= 16 and "KRAV82" in codes

    krav82 = resolve_files([s for s in sheets if s.code == "KRAV82"], cache)
    (record,) = download(krav82, tmp_path / "out")
    assert record.length == (tmp_path / "out" / record.file).stat().st_size
    sample = SAMPLES / "KRAV82.zip"
    if sample.is_file():
        assert sample.stat().st_size == record.length
        assert hashlib.sha256(sample.read_bytes()).hexdigest() == record.sha256


@pytest.mark.network
def test_live_orthophoto_selection_is_listed_and_blocked_by_the_size_guard(kralupy, tmp_path):
    cache = tmp_path / "cache"
    envelope = corridor_envelope([kralupy.alignment], EnvelopeOptions(buffer_m=250.0))
    sheets = select_sheets(load_sheet_index(CuzkDataset.ORTOFOTO, cache), envelope, KROVAK)
    files = resolve_files(sheets, cache)
    assert {s.edition for s in sheets} == {files[0].sheet.edition} and files[0].sheet.edition
    total_mb = sum(f.length for f in files) / 1e6
    print(f"orthophoto: {len(files)} sheets, {total_mb:.1f} MB")
    assert total_mb > 500.0
    with pytest.raises(CuzkSizeLimitError):
        download(files, tmp_path / "out")
    assert not (tmp_path / "out").exists()
