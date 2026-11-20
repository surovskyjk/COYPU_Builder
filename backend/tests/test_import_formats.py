"""T-136 Part A: `import.formats` lists every registered reader and provider, with what the dialog needs.

Offline: the readers and providers are the registry's own. `default_query` only normalises the query and
fetches nothing, so the network is never touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.domain.sources import SourceKind
from coypu_builder.io.providers.copernicus import LIABILITY_NOTICE
from coypu_builder.io.sources import registry
from coypu_builder.protocol.messages import METHODS, Envelope, Stability
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame

KRALUPY_XML = Path(__file__).parent / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"
CUZK_DEFAULT = {"clip": {"type": "corridor", "buffer_m": 250.0}, "max_total_mb": 500.0}
COPERNICUS_DEFAULT = {"clip": {"type": "corridor", "buffer_m": 2000.0}}


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


def _providers_by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["provider_id"]: p for p in result["providers"]}


def test_import_formats_is_registered_as_experimental_and_read_only():
    spec = next(m for m in METHODS if m.name == "import.formats")
    assert spec.stability is Stability.EXPERIMENTAL
    assert spec.mutates is False


def test_every_provider_has_a_title_and_a_description():
    for provider in registry.providers():
        assert provider.title.strip(), provider.provider_id
        assert provider.description.strip(), provider.provider_id


async def test_import_formats_lists_every_reader_and_provider(server_url):
    async with connect(server_url) as ws:
        early = await _call(ws, "0", "import.formats", {})
        assert (early.type, early.error.code) == ("err", "E_NO_SESSION")
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})

        listed = await _call(ws, "1", "import.formats", {})
        assert listed.type == "res", listed.error
        result = listed.result

        expected_readers = [
            {
                "format_id": reader.format_id,
                "kinds": sorted(kind.value for kind in reader.kinds),
                "suffixes": list(reader.suffixes),
            }
            for reader in sorted(registry.readers(), key=lambda r: r.format_id)
        ]
        assert result["readers"] == expected_readers
        assert [p["provider_id"] for p in result["providers"]] == sorted(
            p.provider_id for p in registry.providers()
        )

        by_id = _providers_by_id(result)
        assert set(by_id["copernicus-glo30"]) == {
            "provider_id",
            "title",
            "description",
            "kinds",
            "licence",
            "attribution",
            "notices",
            "default_query",
        }
        assert LIABILITY_NOTICE in by_id["copernicus-glo30"]["notices"]
        assert by_id["cuzk-dmr5g"]["notices"] == []
        assert by_id["cuzk-dmr5g"]["kinds"] == ["terrain"]
        readers_by_id = {r["format_id"]: r for r in result["readers"]}
        assert readers_by_id["las"]["suffixes"] == [".las", ".laz", ".zip"]


def test_provider_descriptions_are_the_specified_sentences():
    expected = {
        "cuzk-dmr5g": (
            "Bare-ground height points from the Czech national laser scan, downloaded one map sheet at a "
            "time. Czech Republic only."
        ),
        "cuzk-dmr4g": (
            "A 5 m grid of bare-ground heights, downloaded one map sheet at a time. Czech Republic only."
        ),
        "copernicus-glo30": (
            "Free worldwide heights on a grid of about 30 m. They follow treetops and rooftops, not bare "
            "ground, so use them only where better ground data is missing."
        ),
    }
    described = {p.provider_id: p.description for p in registry.providers()}
    for provider_id, sentence in expected.items():
        assert described[provider_id] == sentence, provider_id


def test_reader_suffixes_must_be_lower_case_with_a_dot(monkeypatch):
    class Reader:
        kinds = frozenset({SourceKind.TERRAIN})

        def __init__(self, format_id: str, suffixes: tuple[str, ...]):
            self.format_id = format_id
            self.suffixes = suffixes

        def sniff(self, path):  # pragma: no cover - never reached by this test
            raise AssertionError("sniff is not called")

        def inspect(self, path):  # pragma: no cover - never reached by this test
            raise AssertionError("inspect is not called")

        def read(self, path, settings, transform, clip_polygon):  # pragma: no cover
            raise AssertionError("read is not called")

    monkeypatch.setattr(registry, "_READERS", {})
    for index, suffixes in enumerate((("LAS",), ("las",), (".LAS",), (".",))):
        with pytest.raises(ValueError, match="lower case with a leading dot"):
            registry.register_reader(Reader(f"bad-{index}", suffixes))
    assert registry.readers() == ()
    registry.register_reader(Reader("good", (".las", ".laz", ".zip")))
    assert [r.format_id for r in registry.readers()] == ["good"]


async def test_default_query_is_the_same_with_no_project_and_with_a_project_open(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})

        without = await _call(ws, "1", "import.formats", {})
        assert without.type == "res", without.error
        by_id = _providers_by_id(without.result)
        assert by_id["cuzk-dmr5g"]["default_query"] == CUZK_DEFAULT
        assert by_id["cuzk-dmr4g"]["default_query"] == CUZK_DEFAULT
        assert by_id["copernicus-glo30"]["default_query"] == COPERNICUS_DEFAULT

        opened = await _call(ws, "2", "project.new", {"crs": "EPSG:5514"})
        assert opened.type == "res", opened.error
        landxml = await _call(ws, "3", "import.landxml", {"path": str(KRALUPY_XML)})
        assert landxml.type == "res", landxml.error

        with_project = await _call(ws, "4", "import.formats", {})
        assert with_project.type == "res", with_project.error
        assert _providers_by_id(with_project.result) == by_id


async def test_default_query_is_empty_where_a_provider_needs_a_project(server_url, monkeypatch):
    class NeedsProject:
        provider_id = "test-needs-project"
        title = "Needs a project"
        description = "A provider whose query normalisation needs an open project."
        kinds = frozenset({SourceKind.TERRAIN})
        licence = "test"
        attribution = "test"

        def normalise_query(self, query):
            raise ValueError("a provider can only be used while a project import is running")

        def inspect(self, query):  # pragma: no cover - never reached by this test
            raise AssertionError("inspect is not called")

        def fetch(self, query, cache_dir):  # pragma: no cover - never reached by this test
            raise AssertionError("fetch is not called")

    monkeypatch.setitem(registry._PROVIDERS, NeedsProject.provider_id, NeedsProject())
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        listed = await _call(ws, "1", "import.formats", {})
        assert listed.type == "res", listed.error
        assert _providers_by_id(listed.result)["test-needs-project"]["default_query"] == {}


class _FakeProvider:
    provider_id = ""
    title = "Fake provider"
    description = "A provider made for a test."
    kinds = frozenset({SourceKind.TERRAIN})
    licence = "test"
    attribution = "test"

    def inspect(self, query):  # pragma: no cover - never reached by these tests
        raise AssertionError("inspect is not called")

    def fetch(self, query, cache_dir):  # pragma: no cover - never reached by these tests
        raise AssertionError("fetch is not called")


async def test_a_broken_plugin_provider_does_not_break_the_list(server_url, monkeypatch):
    class RaisesOnNormalise(_FakeProvider):
        provider_id = "test-raises"

        def normalise_query(self, query):
            raise RuntimeError("the plugin's own bug")

    class ReturnsNonJson(_FakeProvider):
        provider_id = "test-non-json"

        def normalise_query(self, query):
            return {"clip": object()}

    class PlainStringNotice(_FakeProvider):
        provider_id = "test-plain-notice"
        notices = "one notice, as a plain string"

    for provider in (RaisesOnNormalise(), ReturnsNonJson(), PlainStringNotice()):
        monkeypatch.setitem(registry._PROVIDERS, provider.provider_id, provider)
    async with connect(server_url) as ws:
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        listed = await _call(ws, "1", "import.formats", {})
        assert listed.type == "res", listed.error
        by_id = _providers_by_id(listed.result)
        assert by_id["test-raises"]["default_query"] == {}
        assert by_id["test-non-json"]["default_query"] == {}
        assert by_id["test-plain-notice"]["notices"] == ["one notice, as a plain string"]
        assert by_id["copernicus-glo30"]["default_query"] == COPERNICUS_DEFAULT
        assert by_id["cuzk-dmr5g"]["default_query"] == CUZK_DEFAULT
