"""Smoke test: full LandXML import and frame-table streaming over a real loopback WebSocket (ADR 0003)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame, unpack_blob

FIXTURES = Path(__file__).parent / "fixtures"
KRALUPY_XML = FIXTURES / "kralupy" / "kralupy_neratovice_092.xml"
KRALUPY_COYPU = FIXTURES / "kralupy" / "kralupy_neratovice_092.coypu"

FRAME_TABLE_BLOBS = {
    "station",
    "position",
    "rotation",
    "roll",
    "pitch",
    "cant_mm",
    "curvature",
    "gradient",
    "elevation",
    "segment_index",
}

RUN_GET_BLOBS = {"station", "speed", "accel", "f_traction", "f_braking", "f_resistance"}


async def _call(
    ws, request_id: str, method: str, params: dict[str, Any] | None = None
) -> tuple[Envelope, bytes]:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())


@pytest.fixture
async def server():
    srv = await serve(host="127.0.0.1", port=0)
    try:
        yield srv
    finally:
        srv.close()
        await srv.wait_closed()


@pytest.fixture
def server_url(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}"


async def test_full_import_and_frame_table_streaming(server_url):
    async with connect(server_url) as ws:
        hello, _ = await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})
        assert hello.type == "res"
        assert hello.result["protocol_version"] == PROTOCOL_VERSION

        project, _ = await _call(ws, "2", "project.new", {})
        assert project.type == "res"
        assert project.result["alignments"] == []

        imported, _ = await _call(ws, "3", "import.landxml", {"path": str(KRALUPY_XML)})
        assert imported.type == "res", imported.error
        alignments = imported.result["alignments"]
        assert len(alignments) == 1
        alignment = alignments[0]
        assert alignment["length"] > 0
        alignment_id = alignment["alignment_id"]

        frame_table, tail = await _call(
            ws, "4", "alignment.frame_table", {"alignment_id": alignment_id, "spacing_m": 25.0}
        )
        assert frame_table.type == "res", frame_table.error
        row_count = frame_table.result["row_count"]
        assert row_count > 0
        assert frame_table.result["station_start"] == pytest.approx(alignment["station_start"])
        assert frame_table.result["station_end"] == pytest.approx(alignment["station_end"])

        blob_by_name = {ref.name: unpack_blob(tail, ref) for ref in frame_table.blobs}
        assert set(blob_by_name) == FRAME_TABLE_BLOBS
        station = blob_by_name["station"]
        position = blob_by_name["position"]
        rotation = blob_by_name["rotation"]
        assert station.shape == (row_count,)
        assert position.shape == (row_count, 3)
        assert rotation.shape == (row_count, 4)
        assert station.dtype == np.float32
        assert np.all(np.diff(station) > 0)
        assert np.all(np.isfinite(position))
        # quaternions from quaternion_from_matrix are unit-length by construction.
        assert np.allclose(np.linalg.norm(rotation.astype(np.float64), axis=1), 1.0, atol=1e-4)

        # project.get reflects the imported alignment without re-importing.
        info, _ = await _call(ws, "5", "project.get")
        assert info.result["alignments"][0]["alignment_id"] == alignment_id

        missing, _ = await _call(ws, "6", "alignment.frame_table", {"alignment_id": "no-such-id"})
        assert missing.type == "err"
        assert missing.error.code == "E_NOT_FOUND"

        run_missing, _ = await _call(ws, "7", "run.get", {"run_id": "r1"})
        assert run_missing.type == "err"
        assert run_missing.error.code == "E_NOT_FOUND"


async def test_import_coypu_then_run_list_and_run_get(server_url):
    """T-114 acceptance: a full round trip over a real WebSocket covering import.coypu -> run.list ->
    run.get, asserting blob dtypes and shapes."""
    async with connect(server_url) as ws:
        await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})
        await _call(ws, "2", "project.new", {})

        imported, _ = await _call(ws, "3", "import.coypu", {"path": str(KRALUPY_COYPU)})
        assert imported.type == "res", imported.error
        assert len(imported.result["alignments"]) == 1
        assert len(imported.result["runs"]) >= 1
        assert len(imported.result["stops"]) == 6

        listed, _ = await _call(ws, "4", "run.list")
        assert listed.type == "res", listed.error
        assert {r["run_id"] for r in listed.result["runs"]} == {r["run_id"] for r in imported.result["runs"]}

        run_id = imported.result["runs"][0]["run_id"]
        got, tail = await _call(ws, "5", "run.get", {"run_id": run_id, "dt": 0.05})
        assert got.type == "res", got.error
        row_count = got.result["row_count"]
        dt = got.result["dt"]
        assert abs(row_count * dt - got.result["duration_s"]) <= dt + 1e-6

        blob_by_name = {ref.name: unpack_blob(tail, ref) for ref in got.blobs}
        assert set(blob_by_name) == RUN_GET_BLOBS
        for array in blob_by_name.values():
            assert array.dtype == np.float32
            assert array.shape == (row_count,)


async def test_project_new_rejects_unknown_method_before_hello(server_url):
    async with connect(server_url) as ws:
        envelope, _ = await _call(ws, "1", "project.new", {})
        assert envelope.type == "err"
        assert envelope.error.code == "E_NO_SESSION"
