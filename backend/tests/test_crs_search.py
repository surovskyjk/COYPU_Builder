"""T-136 Part A: `crs.search` ranks the EPSG CRSs by code and name, ignores diacritics, answers over the wire.

Read-only and offline: the EPSG list comes from the PROJ database that pyproj ships.
"""

from __future__ import annotations

from typing import Any

import pytest
from pyproj.database import query_crs_info
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.io import crs_search
from coypu_builder.protocol.messages import METHODS, Envelope, Stability
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame

CZECH_PRACTICE = (
    "EPSG:5514",
    "EPSG:5513",
    "EPSG:4326",
    "EPSG:4258",
    "EPSG:25833",
    "EPSG:25834",
    "EPSG:32633",
    "EPSG:32634",
    "EPSG:5705",
    "EPSG:3855",
)


def _codes(results) -> list[str]:
    return [entry.code for entry in results]


def test_codes_are_unique():
    numbers = [entry.number for entry in crs_search.catalogue()]
    assert len(numbers) == len(set(numbers))


def test_a_duplicated_code_keeps_the_row_with_the_widest_area():
    rows = [row for row in query_crs_info(auth_name="EPSG", allow_deprecated=True) if row.code == "25833"]
    assert len(rows) > 1  # PROJ has one row per area of use
    widest = max(rows, key=lambda row: crs_search._box_area(row.area_of_use))
    kept = next(entry for entry in crs_search.catalogue() if entry.number == 25833)
    assert kept.area == widest.area_of_use.name


def test_code_query_finds_it_first():
    codes = _codes(crs_search.search("2003"))
    assert codes[0] == "EPSG:2003"
    # EPSG:20034 matches by code prefix; the exact code comes first.
    assert "EPSG:20034" in codes
    assert codes.index("EPSG:2003") < codes.index("EPSG:20034")


def test_name_query_ignores_diacritics():
    plain = _codes(crs_search.search("krovak"))
    accented = _codes(crs_search.search("Křovák"))
    for found in (plain, accented):
        assert "EPSG:5514" in found
        assert "EPSG:5513" in found
    assert accented == plain


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("EPSG:5514 — S-JTSK / Krovak East North", "EPSG:5514"),
        ("epsg 5514", "EPSG:5514"),
        ("5514 krovak", "EPSG:5514"),
        ("sjtsk", "EPSG:5514"),
        ("wgs84", "EPSG:4326"),
    ],
)
def test_the_shown_value_finds_its_crs(query, expected):
    assert _codes(crs_search.search(query))[0] == expected


def test_code_matches_by_prefix():
    codes = _codes(crs_search.search("551", limit=200))
    assert "EPSG:5514" in codes
    # A code matches by prefix, so 4551 does not match "551" at all.
    assert "EPSG:4551" not in codes


def test_czech_codes_lead_their_group():
    assert _codes(crs_search.search("krovak"))[:2] == ["EPSG:5514", "EPSG:5513"]
    assert "EPSG:5514" in _codes(crs_search.search("S-JTSK"))[:2]


def test_short_query_boundary():
    assert _codes(crs_search.search("on"))[:2] == ["EPSG:25833", "EPSG:25834"]
    three = _codes(crs_search.search("one"))
    assert three[0] != "EPSG:25833"
    assert three.index("EPSG:25833") > 0


def test_vertical_filter():
    for query in ("Baltic", "Bpv"):
        results = crs_search.search(query, kind="vertical")
        assert "EPSG:5705" in _codes(results), query
        assert all(entry.kind == "vertical" for entry in results), query


def test_horizontal_kind():
    horizontal = _codes(crs_search.search("wgs 84", kind="horizontal", limit=200))
    assert "EPSG:4326" in horizontal
    assert "EPSG:4979" not in horizontal
    assert _codes(crs_search.search("wgs 84", kind="")) == _codes(crs_search.search("wgs 84"))


def test_short_query_puts_czech_codes_first():
    assert _codes(crs_search.search(""))[: len(CZECH_PRACTICE)] == list(CZECH_PRACTICE)
    assert _codes(crs_search.search("32"))[:2] == ["EPSG:32633", "EPSG:32634"]


def test_limit_is_respected_and_clamped():
    assert len(crs_search.search("", limit=5)) == 5
    assert len(crs_search.search("")) == crs_search.DEFAULT_LIMIT
    assert len(crs_search.search("", limit=0)) == 1
    assert len(crs_search.search("", limit=-3)) == 1
    assert len(crs_search.search("", limit=10_000)) == 200


def test_deprecated_crs_come_last():
    flags = [entry.deprecated for entry in crs_search.search("ntf", limit=200)]
    assert True in flags and False in flags
    assert flags == sorted(flags)


def test_utm_zone_33_lists_25833_once():
    codes = _codes(crs_search.search("utm zone 33", limit=200))
    assert codes.count("EPSG:25833") == 1


def test_the_catalogue_is_read_once(monkeypatch):
    crs_search.catalogue.cache_clear()
    calls: list[tuple] = []
    real = crs_search.query_crs_info

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(crs_search, "query_crs_info", counting)
    try:
        crs_search.search("5514")
        crs_search.search("krovak")
        assert len(calls) == 1
    finally:
        crs_search.catalogue.cache_clear()


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown kind 'geocentric'"):
        crs_search.search("x", kind="geocentric")


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None) -> Envelope:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())[0]


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def test_serve_builds_the_catalogue_before_any_request():
    crs_search.catalogue.cache_clear()
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        assert crs_search.catalogue.cache_info().currsize == 1
    finally:
        srv.close()
        await srv.wait_closed()


def test_crs_search_is_registered_as_experimental_and_read_only():
    spec = next(m for m in METHODS if m.name == "crs.search")
    assert spec.stability is Stability.EXPERIMENTAL
    assert spec.mutates is False


async def test_crs_search_over_the_wire(server_url):
    async with connect(server_url) as ws:
        early = await _call(ws, "0", "crs.search", {"query": "5514"})
        assert (early.type, early.error.code) == ("err", "E_NO_SESSION")
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})

        found = await _call(ws, "1", "crs.search", {"query": "Křovák", "limit": 3, "kind": "projected"})
        assert found.type == "res", found.error
        results = found.result["results"]
        assert len(results) == 3
        assert set(results[0]) == {"code", "name", "kind", "area", "deprecated"}
        assert {r["kind"] for r in results} == {"projected"}

        for kind in ("horizontal", ""):
            accepted = await _call(ws, "k", "crs.search", {"query": "wgs 84", "kind": kind})
            assert accepted.type == "res", (kind, accepted.error)

        for index, params in enumerate(
            (
                {},
                {"query": 5},
                {"query": "x", "limit": "ten"},
                {"query": "x", "kind": "geocentric"},
            )
        ):
            reply = await _call(ws, f"bad{index}", "crs.search", params)
            assert (reply.type, reply.error.code) == ("err", "E_BAD_PARAMS"), params

        assert (await _call(ws, "z", "session.ping", {})).type == "res"


async def test_crs_search_needs_no_project(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        found = await _call(ws, "1", "crs.search", {"query": "5514"})
        assert found.type == "res", found.error
        assert found.result["results"][0]["code"] == "EPSG:5514"
