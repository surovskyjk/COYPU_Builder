"""T-114: the protocol surface for runs, catalogue and `.coypu` import, over a real loopback WebSocket
(ADR 0003). `test_server_smoke.py` covers the headline import.coypu -> run.list -> run.get round trip; this
file covers the rest of the new methods and their error paths.
"""

from __future__ import annotations

import zipfile
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
KRALUPY_COYPU = FIXTURES / "kralupy" / "kralupy_neratovice_092.coypu"

RUN_GET_BLOBS_WITH_FORCE = {"station", "speed", "accel", "f_traction", "f_braking", "f_resistance"}
RUN_GET_BLOBS_NO_FORCE = {"station", "speed", "accel"}

_BATCH_HEADER = "stationM,timeS,speedMs,accelMs2,forceTracKN,forceBrakeKN,forceResKN\n"
_GUI_EN_HEADER = (
    "stationing [km],Time [s],Speed [km/h],Accel [m/s2],Tractive Force [kN],Braking Force [kN],"
    "Resistance [kN]\n"
)


def _batch_csv(with_force: bool) -> str:
    force = "5.0,0.0,1.2" if with_force else ",,"
    rows = (f"{i * 100.0},{i * 10.0},10.0,0.0,{force}\n" for i in range(11))
    return _BATCH_HEADER + "".join(rows)


def _gui_en_csv(with_force: bool) -> str:
    force = "5.0,0.0,1.2" if with_force else ",,"
    rows = (f"{i * 0.1},{i * 10.0},36.0,0.0,{force}\n" for i in range(11))
    return _GUI_EN_HEADER + "".join(rows)


async def _call(
    ws, request_id: str, method: str, params: dict[str, Any] | None = None
) -> tuple[Envelope, bytes]:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())


async def _hello_and_project(ws) -> None:
    await _call(ws, "hello", "session.hello", {"client": "pytest", "client_version": "0"})
    await _call(ws, "project", "project.new", {})


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


async def test_import_coypu_matches_both_vehicles_to_the_catalogue_with_no_warnings(server_url):
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        imported, _ = await _call(ws, "1", "import.coypu", {"path": str(KRALUPY_COYPU)})
        assert imported.type == "res", imported.error
        result = imported.result

        assert len(result["alignments"]) == 1
        assert len(result["runs"]) == 2
        assert len(result["trainsets"]) == 2
        assert len(result["stops"]) == 6
        assert result["warnings"] == []

        trainset_keys = {t["spec_key"] for t in result["trainsets"]}
        assert trainset_keys == {"dmu_br650_cd840", "generic_bemu"}

        alignment_id = result["alignments"][0]["alignment_id"]
        for run in result["runs"]:
            assert run["alignment_id"] == alignment_id
            assert run["trainset_id"] in {t["trainset_id"] for t in result["trainsets"]}
            assert run["stop_count"] == 6
            assert run["sample_count"] > 0
            assert run["warnings"] == []


async def test_import_coypu_missing_file_returns_not_found(server_url):
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res, _ = await _call(ws, "1", "import.coypu", {"path": "no/such/file.coypu"})
        assert res.type == "err"
        assert res.error.code == "E_NOT_FOUND"


async def test_import_coypu_without_embedded_landxml_is_empty_not_a_fallback(server_url, tmp_path):
    path = tmp_path / "no_assets.coypu"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.json", "{}")

    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res, _ = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "err"
        assert res.error.code == "E_EMPTY"


async def test_run_get_unknown_run_and_non_positive_dt(server_url):
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        imported, _ = await _call(ws, "1", "import.coypu", {"path": str(KRALUPY_COYPU)})
        run_id = imported.result["runs"][0]["run_id"]

        missing, _ = await _call(ws, "2", "run.get", {"run_id": "no-such-run"})
        assert missing.type == "err"
        assert missing.error.code == "E_NOT_FOUND"

        bad_dt, _ = await _call(ws, "3", "run.get", {"run_id": run_id, "dt": 0.0})
        assert bad_dt.type == "err"
        assert bad_dt.error.code == "E_BAD_PARAMS"

        negative_dt, _ = await _call(ws, "4", "run.get", {"run_id": run_id, "dt": -1.0})
        assert negative_dt.type == "err"
        assert negative_dt.error.code == "E_BAD_PARAMS"


async def test_import_kinematics_dialects_produce_equivalent_run_summaries(server_url, tmp_path):
    batch_path = tmp_path / "batch.csv"
    batch_path.write_text(_batch_csv(with_force=True), encoding="utf-8")
    gui_path = tmp_path / "gui_en.csv"
    gui_path.write_text(_gui_en_csv(with_force=True), encoding="utf-8")

    async with connect(server_url) as ws:
        await _hello_and_project(ws)

        batch_res, _ = await _call(ws, "1", "import.kinematics", {"path": str(batch_path)})
        assert batch_res.type == "res", batch_res.error
        gui_res, _ = await _call(ws, "2", "import.kinematics", {"path": str(gui_path)})
        assert gui_res.type == "res", gui_res.error

        batch_run = batch_res.result["runs"][0]
        gui_run = gui_res.result["runs"][0]
        for field in ("direction", "station_start", "station_end", "duration_s", "sample_count"):
            assert batch_run[field] == pytest.approx(gui_run[field])


async def test_import_kinematics_without_force_columns_omits_force_blobs_not_zeros(server_url, tmp_path):
    path = tmp_path / "no_force.csv"
    path.write_text(_batch_csv(with_force=False), encoding="utf-8")

    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        imported, _ = await _call(ws, "1", "import.kinematics", {"path": str(path)})
        assert imported.type == "res", imported.error
        run_id = imported.result["runs"][0]["run_id"]

        got, tail = await _call(ws, "2", "run.get", {"run_id": run_id})
        assert got.type == "res", got.error
        assert {ref.name for ref in got.blobs} == RUN_GET_BLOBS_NO_FORCE

        row_count = got.result["row_count"]
        for ref in got.blobs:
            array = unpack_blob(tail, ref)
            assert array.dtype == np.float32
            assert array.shape == (row_count,)


async def test_import_kinematics_unknown_alignment_id_returns_not_found(server_url, tmp_path):
    path = tmp_path / "batch.csv"
    path.write_text(_batch_csv(with_force=True), encoding="utf-8")

    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res, _ = await _call(ws, "1", "import.kinematics", {"path": str(path), "alignment_id": "no-such-id"})
        assert res.type == "err"
        assert res.error.code == "E_NOT_FOUND"


async def test_catalogue_vehicles_lists_all_three_entries_without_a_project(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})
        res, _ = await _call(ws, "2", "catalogue.vehicles")
        assert res.type == "res", res.error
        keys = {v["key"] for v in res.result["vehicles"]}
        assert keys == {"dmu_br650_cd840", "generic_bemu", "tram_generic"}


async def test_trainset_create_with_units_then_get_returns_doubled_consist(server_url):
    async with connect(server_url) as ws:
        await _hello_and_project(ws)

        single, _ = await _call(ws, "1", "trainset.create", {"spec_key": "tram_generic", "units": 1})
        assert single.type == "res", single.error

        doubled, _ = await _call(ws, "2", "trainset.create", {"spec_key": "tram_generic", "units": 2})
        assert doubled.type == "res", doubled.error
        assert len(doubled.result["cars"]) == 2 * len(single.result["cars"])
        # `units` repeats the whole spec, so the same coupling gap also appears between the two units
        # (Trainset.from_spec's documented behaviour) -- not a plain doubling of length_m.
        expected_length = 2 * single.result["length_m"] + single.result["coupling_gap_m"]
        assert doubled.result["length_m"] == pytest.approx(expected_length)

        fetched, _ = await _call(ws, "3", "trainset.get", {"trainset_id": doubled.result["trainset_id"]})
        assert fetched.type == "res", fetched.error
        assert len(fetched.result["cars"]) == len(doubled.result["cars"])

        unknown_spec, _ = await _call(ws, "4", "trainset.create", {"spec_key": "does-not-exist"})
        assert unknown_spec.type == "err"
        assert unknown_spec.error.code == "E_NOT_FOUND"

        unknown_trainset, _ = await _call(ws, "5", "trainset.get", {"trainset_id": "no-such-id"})
        assert unknown_trainset.type == "err"
        assert unknown_trainset.error.code == "E_NOT_FOUND"
