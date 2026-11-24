"""Attribution and licence for file imports, and the file-name hints of `import.inspect` (F53, T-136 C1).

The synthetic reader and the wire helpers come from `test_import_framework.py`; nothing here copies them.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

import test_import_framework as framework
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.hints import ProductHints, product_hints
from coypu_builder.io.sources.registry import InspectReport
from test_import_framework import SyntheticXyzReader, _call, _file, _session, _settings

# The framework's autouse private registry and its live-server fixture, bound as this module's own fixtures.
isolated_registry = framework.isolated_registry
server_url = framework.server_url

CUZK = ProductHints(licence="CC BY 4.0", attribution="© ČÚZK")
SYNTHETIC_LICENCE = "synthetic (CC0)"


class _UnlicensedReader(SyntheticXyzReader):
    """The synthetic reader, with no licence hint of its own, so only the file-name hint can fill it."""

    def inspect(self, path: Path):
        return replace(super().inspect(path), licence_hint=None)


def _named(tmp_path: Path, name: str) -> Path:
    """A synthetic file under a product's file name. The test reader sniffs the content, not the name."""
    return Path(shutil.copy(_file(tmp_path, name="synthetic.xyz"), tmp_path / name))


@pytest.mark.parametrize(
    "names",
    [
        ["KRAV82.zip"],
        ["KRAV82.laz"],
        ["KRAV82.WRTO24.2025.zip"],
        ["KRAV82_x.laz"],
        ["KRAV82"],
    ],
)
def test_product_hints_recognise_sm5_sheet_codes(names):
    paths = [Path(name) for name in names]
    assert product_hints(paths, SourceKind.TERRAIN) == CUZK
    assert product_hints(paths, SourceKind.IMAGERY) == CUZK


def test_product_hints_refuse_mixed_names_other_kinds_and_non_codes():
    assert product_hints([Path("KRAV82.zip"), Path("terrain.zip")], SourceKind.TERRAIN) is None
    assert product_hints([Path("KRAV82.zip")], SourceKind.BUILDINGS) is None
    assert product_hints([Path("KRAV82.zip")], SourceKind.WATER) is None
    assert product_hints([Path("KRAV82x.laz")], SourceKind.TERRAIN) is None
    assert product_hints([Path("KRA82.tif")], SourceKind.TERRAIN) is None
    assert product_hints([Path("KRAV8.tif")], SourceKind.TERRAIN) is None
    assert product_hints([Path("krav82.tif")], SourceKind.TERRAIN) is None  # real codes are upper case
    assert product_hints([Path("KRAV82-x.laz")], SourceKind.TERRAIN) is None
    assert product_hints([Path("data01.tif")], SourceKind.TERRAIN) is None
    assert product_hints([Path("site02.laz")], SourceKind.TERRAIN) is None
    assert product_hints([], SourceKind.TERRAIN) is None
    assert product_hints(["KRAV82.zip"], SourceKind.TERRAIN) == CUZK  # a str is a path too


async def test_file_import_records_the_confirmed_attribution_and_licence(server_url, tmp_path):
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)
        credited = await _call(
            ws,
            "1",
            "import.run",
            {
                "path": str(path),
                "kind": "terrain",
                "settings": _settings(),
                "attribution": "© ČÚZK",
                "licence": "CC BY 4.0",
            },
        )
        assert credited.type == "res", credited.error
        got = await _call(ws, "2", "source.get", {"source_id": credited.result["source_id"]})
        assert (got.result["attribution"], got.result["licence"]) == ("© ČÚZK", "CC BY 4.0")
        plain = await _call(
            ws, "3", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings()}
        )
        assert plain.type == "res", plain.error
        got = await _call(ws, "4", "source.get", {"source_id": plain.result["source_id"]})
        assert (got.result["attribution"], got.result["licence"]) == ("", SYNTHETIC_LICENCE)


async def test_reimport_keeps_attribution_when_none_is_sent(server_url, tmp_path):
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)
        first = await _call(
            ws,
            "1",
            "import.run",
            {
                "path": str(path),
                "kind": "terrain",
                "settings": _settings(),
                "attribution": "© ČÚZK",
                "licence": "CC BY 4.0",
            },
        )
        assert first.type == "res", first.error
        source_id = first.result["source_id"]
        again = await _call(
            ws,
            "2",
            "import.run",
            {"path": str(path), "kind": "terrain", "settings": _settings(), "reimport_of": source_id},
        )
        assert again.type == "res", again.error
        assert again.result["source_id"] == source_id
        got = await _call(ws, "3", "source.get", {"source_id": source_id})
        assert (got.result["attribution"], got.result["licence"]) == ("© ČÚZK", "CC BY 4.0")
        renamed = await _call(
            ws,
            "4",
            "import.run",
            {
                "path": str(path),
                "kind": "terrain",
                "settings": _settings(),
                "reimport_of": source_id,
                "attribution": "© ČÚZK 2026",
            },
        )
        assert renamed.type == "res", renamed.error
        got = await _call(ws, "5", "source.get", {"source_id": source_id})
        assert (got.result["attribution"], got.result["licence"]) == ("© ČÚZK 2026", "CC BY 4.0")


async def test_provider_import_refuses_attribution(server_url):
    async with connect(server_url) as ws:
        await _session(ws)
        for extra in ({"attribution": "© ČÚZK"}, {"licence": "CC BY 4.0"}):
            refused = await _call(
                ws,
                "1",
                "import.run",
                {"provider": "cuzk-dmr5g", "kind": "terrain", "settings": _settings(), **extra},
            )
            assert (refused.type, refused.error.code) == ("err", "E_BAD_PARAMS")
            assert refused.error.message == "attribution and licence come from the provider"


async def test_inspect_hints_cuzk_sheets(server_url, tmp_path, monkeypatch):
    tif = _named(tmp_path, "KRAV82.tif")
    laz = _named(tmp_path, "KRAV82.laz")
    async with connect(server_url) as ws:
        await _session(ws)
        both = await _call(ws, "1", "import.inspect", {"paths": [str(tif), str(laz)], "kind": "terrain"})
        assert both.type == "res", both.error
        assert both.result["attribution_hint"] == "© ČÚZK"
        assert both.result["licence_hint"] == SYNTHETIC_LICENCE  # the reader's own hint is not overwritten
        monkeypatch.setitem(registry._READERS, "synthetic-xyz", _UnlicensedReader())
        unlicensed = await _call(ws, "2", "import.inspect", {"path": str(tif)})
        assert unlicensed.type == "res", unlicensed.error
        assert (unlicensed.result["attribution_hint"], unlicensed.result["licence_hint"]) == (
            "© ČÚZK",
            "CC BY 4.0",
        )


async def test_inspect_gives_no_hint_for_other_names(server_url, tmp_path, monkeypatch):
    monkeypatch.setitem(registry._READERS, "synthetic-xyz", _UnlicensedReader())
    async with connect(server_url) as ws:
        await _session(ws)
        for name in ("terrain.tif", "KRA82.tif", "KRAV8.tif"):
            inspected = await _call(ws, "1", "import.inspect", {"path": str(_named(tmp_path, name))})
            assert inspected.type == "res", inspected.error
            assert inspected.result["attribution_hint"] is None, name
            assert inspected.result["licence_hint"] is None, name


async def test_source_list_shows_the_recorded_attribution_and_licence(server_url, tmp_path):
    """The in-session half of the round trip; the save and reopen half belongs to T-145 (project.save)."""
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)
        ran = await _call(
            ws,
            "1",
            "import.run",
            {
                "path": str(path),
                "kind": "terrain",
                "settings": _settings(),
                "attribution": "© ČÚZK",
                "licence": "CC BY 4.0",
            },
        )
        assert ran.type == "res", ran.error
        listed = await _call(ws, "2", "source.list", {})
        (source,) = listed.result["sources"]
        assert (source["attribution"], source["licence"]) == ("© ČÚZK", "CC BY 4.0")


async def test_a_sheet_name_alone_records_no_attribution(server_url, tmp_path):
    """The hint is a guess for the dialog only: a KRAV82 file imported with nothing sent records no credit."""
    sheet = _named(tmp_path, "KRAV82.tif")
    async with connect(server_url) as ws:
        await _session(ws)
        ran = await _call(
            ws, "1", "import.run", {"path": str(sheet), "kind": "terrain", "settings": _settings()}
        )
        assert ran.type == "res", ran.error
        got = await _call(ws, "2", "source.get", {"source_id": ran.result["source_id"]})
        assert (got.result["attribution"], got.result["licence"]) == ("", SYNTHETIC_LICENCE)


class _FakeSheetProvider:
    """A provider that never touches the network: its inspect returns a fixed report with no licence hint."""

    provider_id = "fake-sheets"
    title = "Fake sheets"
    description = "test only"
    kinds = frozenset({SourceKind.TERRAIN})
    licence = "test licence"
    attribution = "test attribution"

    def inspect(self, query):
        return InspectReport(
            format_id="fake-sheets",
            kind=SourceKind.TERRAIN,
            declared_crs="EPSG:5514",
            extent=None,
            counts={},
            suggested=ImportSettings(horizontal_crs="EPSG:5514"),
            licence_hint=None,
            warnings=(),
        )

    def fetch(self, query, cache_dir):
        raise AssertionError("inspect must not fetch")


async def test_provider_inspect_gives_no_attribution_hint(server_url):
    registry.register_provider(_FakeSheetProvider())
    async with connect(server_url) as ws:
        await _session(ws)
        inspected = await _call(ws, "1", "import.inspect", {"provider": "fake-sheets", "query": {}})
        assert inspected.type == "res", inspected.error
        assert inspected.result["attribution_hint"] is None
        assert inspected.result["licence_hint"] is None
