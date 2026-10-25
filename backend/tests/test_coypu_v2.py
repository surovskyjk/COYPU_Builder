"""T-128: `.coypu` archive format 2 (line, stop records, stopping patterns, karto entry), version and size
guards. A format 2 archive is synthesised from the Kralupy version 1 fixture by `make_v2_archive`; every value
the version 2 keys carry comes from this file, never from a real archive.
"""

from __future__ import annotations

import json
import os
import re
import struct
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.io.coypu import archive as archive_module
from coypu_builder.io.coypu.archive import CoypuLine, CoypuStopRecord, read_coypu
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame

FIXTURES = Path(__file__).parent / "fixtures"
KRALUPY_COYPU = FIXTURES / "kralupy" / "kralupy_neratovice_092.coypu"
KRALUPY_ASSET = "assets/001_092_Kralupy_Neratovice.xml"

LINE = {"number": "S-SYN", "name": "Synthetic line", "category": "regional"}

# Stored in a shuffled station order, so the station ordering of a run's stops is observable.
# Fields: stopId, name, stationingKm, dwellTimeS, municipality, population, Os, Sp, Ex.
SYNTHETIC_RECORDS = (
    ("s004", "Delta", 14.0, 40.0, "South", 2600, True, False, True),
    ("s001", "Alpha", 0.5, 20.0, "North", 1500, True, True, True),
    ("s005", "Echo", 18.0, 0.0, "South", None, True, True, True),
    ("s002", "Bravo", 3.0, 25.0, "North", 800, True, False, False),
    ("s003", "Charlie", 8.0, 35.0, "Middle", 4200, True, True, False),
)

KARTO_SCHEMA_1 = {
    "schemaVersion": 1,
    "application": {"name": "COYPU", "version": "synthetic"},
    "units": {"length": "km"},
    "stops": [],
}


def _record(row: tuple) -> dict[str, Any]:
    stop_id, name, km, dwell, municipality, population, in_os, in_sp, in_ex = row
    return {
        "stopId": stop_id,
        "stopName": name,
        "stationingKm": km,
        "dwellTimeS": dwell,
        "municipalityName": municipality,
        "population": population,
        "stopInOs": in_os,
        "stopInSp": in_sp,
        "stopInEx": in_ex,
        "stopInSpIsManual": False,
        "stopInExIsManual": False,
    }


def make_v2_archive(tmp_path: Path, **changes: Any) -> Path:
    """A synthesised format 2 archive built from the Kralupy version 1 fixture.

    Keyword changes: `format_version` (default 2), `drop_format_version`, `active_stop_pattern` (default
    "Os"), `stop_records` (default SYNTHETIC_RECORDS; () for none), `line` (a dict with number, name and
    category), `direction_labels` (default True: every vehicle gets labels), `karto` (a dict is serialised, a
    str or bytes is written verbatim, None omits the entry), `chainage_map` (baseline_km and active_km arrays
    written to `calculationCache.landXmlDerived`) and `name` (the file name).
    """
    with zipfile.ZipFile(KRALUPY_COYPU) as source:
        payload = json.loads(source.read("project.json"))
        assets = {name: source.read(name) for name in source.namelist() if name != "project.json"}

    if changes.pop("drop_format_version", False):
        payload.pop("formatVersion", None)
    else:
        payload["formatVersion"] = changes.pop("format_version", 2)

    line = changes.pop("line", LINE)
    metadata = payload.setdefault("projectMetadata", {})
    metadata["lineNumber"] = line.get("number", "")
    metadata["lineName"] = line.get("name", "")
    metadata["lineCategory"] = line.get("category", "")

    records = changes.pop("stop_records", SYNTHETIC_RECORDS)
    active = changes.pop("active_stop_pattern", "Os")
    stops = payload.setdefault("stopsData", {})
    raw_records = changes.pop("raw_stop_records", None)
    stops["stopRecords"] = raw_records if raw_records is not None else [_record(row) for row in records]
    stops["activeStopPattern"] = active
    stops["spPopulationThreshold"] = 2000
    stops["isSpThresholdAuto"] = True
    # COYPU keeps the legacy list equal to the active pattern's records
    flag_index = {"Os": 6, "Sp": 7, "Ex": 8}.get(active)
    stops["trainStops"] = (
        [] if flag_index is None else [[row[2], row[3], row[1]] for row in records if row[flag_index]]
    )

    if changes.pop("run_reversed", False):
        payload["vehicleConfiguration"]["settingsData"]["runReversed"] = True

    if changes.pop("direction_labels", True):
        for vehicle in payload["vehicleConfiguration"]["settingsData"]["vehicles"]:
            vehicle["directionLabels"] = {
                "forward": "Towards the synthetic end",
                "reverse": "Towards the start",
            }

    chainage = changes.pop("chainage_map", None)
    if chainage is not None:
        landxml_derived = payload.setdefault("calculationCache", {}).setdefault("landXmlDerived", {})
        for key, values in zip(("chainageMapBaselineKm", "chainageMapActiveKm"), chainage, strict=True):
            landxml_derived[key] = {
                "__ndarray__": True,
                "dtype": "float64",
                "values": [float(v) for v in values],
            }

    karto = changes.pop("karto", None)
    if karto is None:
        karto_bytes = None
    elif isinstance(karto, bytes):
        karto_bytes = karto
    elif isinstance(karto, str):
        karto_bytes = karto.encode("utf-8")
    else:
        karto_bytes = json.dumps(karto).encode("utf-8")

    target = tmp_path / changes.pop("name", "v2.coypu")
    if changes:
        raise TypeError(f"make_v2_archive got unknown changes: {sorted(changes)}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("project.json", json.dumps(payload, ensure_ascii=False))
        for name, data in assets.items():
            out.writestr(name, data)
        if karto_bytes is not None:
            out.writestr("karto/dataset.json", karto_bytes)
    return target


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None):
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    envelope, _blob = decode_frame(await ws.recv())
    return envelope


async def _hello_and_project(ws) -> None:
    await _call(ws, "hello", "session.hello", {"client": "pytest", "client_version": "0"})
    await _call(ws, "project", "project.new", {})


@pytest.fixture
async def server():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield srv
    finally:
        srv.close()
        await srv.wait_closed()


@pytest.fixture
def server_url(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}"


# --- criterion 1: version 1 is unchanged ----------------------------------------------------------


def test_v1_import_is_unchanged():
    project = read_coypu(KRALUPY_COYPU)

    assert project.format_version == 1
    assert project.warnings == ()
    assert project.line == CoypuLine()
    assert project.stop_records == ()
    assert project.active_stop_pattern == "Os"
    assert project.run_reversed is False
    assert project.direction_labels == ({}, {})
    assert project.karto is None
    assert list(project.raw_assets) == [KRALUPY_ASSET]

    runs = project.kinematics_runs()
    assert len(runs) == 2
    for index, run in enumerate(runs):
        raw = project.kinematics(index)
        assert run.station_m.size == 18186
        np.testing.assert_array_equal(run.station_m, raw["station_m"])
        np.testing.assert_array_equal(run.time_s, raw["time_s"] - raw["time_s"][0])
        np.testing.assert_array_equal(run.speed_ms, np.abs(raw["speed_ms"]))
        np.testing.assert_array_equal(run.accel_ms2, raw["accel_ms2"])
        np.testing.assert_array_equal(run.f_traction_kn, raw["f_trac_kn"])
        np.testing.assert_array_equal(run.f_braking_kn, raw["f_brake_kn"])
        np.testing.assert_array_equal(run.f_resistance_kn, raw["f_res_kn"])
        assert int(run.direction) == 1
        assert run.warnings == ()
        # Version 1 has no stop records, so the run's stops are the dwell samples, exactly as before
        assert [s.station_m for s in run.stops] == [376.0, 5038.0, 7611.0, 10917.0, 15758.0, 18148.0]
        assert all(s.dwell_s == 30.0 and s.stop_id == "" and s.patterns == () for s in run.stops)


# --- criterion 2: version 2 imports ---------------------------------------------------------------


def test_v2_import_reads_line_stops_and_patterns(tmp_path):
    v1_runs = read_coypu(KRALUPY_COYPU).kinematics_runs()
    project = read_coypu(make_v2_archive(tmp_path))

    assert project.format_version == 2
    assert project.warnings == ()
    assert project.line == CoypuLine(number="S-SYN", name="Synthetic line", category="regional")

    records = project.record_stops()
    assert [s.name for s in records] == ["Delta", "Alpha", "Echo", "Bravo", "Charlie"]
    assert [s.stop_id for s in records] == ["s004", "s001", "s005", "s002", "s003"]
    assert records[1].patterns == ("Os", "Sp", "Ex")
    assert records[3].patterns == ("Os",)

    runs = project.kinematics_runs()
    assert len(runs) == len(v1_runs)
    for new, old in zip(runs, v1_runs, strict=True):
        np.testing.assert_array_equal(new.station_m, old.station_m)
        np.testing.assert_array_equal(new.time_s, old.time_s)
        np.testing.assert_array_equal(new.speed_ms, old.speed_ms)
        np.testing.assert_array_equal(new.accel_ms2, old.accel_ms2)

    # The active pattern is Os, which holds every record, and the run's stops are in station order
    run_stops = runs[0].stops
    assert [s.name for s in run_stops] == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    assert [s.station_m for s in run_stops] == [500.0, 3000.0, 8000.0, 14000.0, 18000.0]
    assert [s.stop_id for s in run_stops] == ["s001", "s002", "s003", "s004", "s005"]
    assert run_stops[0].patterns == ("Os", "Sp", "Ex")
    assert run_stops[4].dwell_s == 0.0


# --- criterion 3: pattern switch ------------------------------------------------------------------


def test_active_pattern_selects_the_run_stops(tmp_path):
    project = read_coypu(make_v2_archive(tmp_path, active_stop_pattern="Ex"))

    assert project.active_stop_pattern == "Ex"
    assert len(project.record_stops()) == 5
    run = project.kinematics_runs()[0]
    assert [s.name for s in run.stops] == ["Alpha", "Delta", "Echo"]
    assert [s.station_m for s in run.stops] == [500.0, 14000.0, 18000.0]
    assert all("Ex" in s.patterns for s in run.stops)


# --- criterion 4: version checks ------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [{"drop_format_version": True}, {"format_version": 0}, {"format_version": True}, {"format_version": "2"}],
    ids=["missing", "zero", "bool", "string"],
)
def test_bad_format_versions_are_refused(tmp_path, changes):
    path = make_v2_archive(tmp_path, **changes)
    with pytest.raises(ValueError, match="not a COYPU project file"):
        read_coypu(path)


def test_newer_format_warns(tmp_path):
    project = read_coypu(make_v2_archive(tmp_path, format_version=3))

    assert project.format_version == 3
    assert project.warnings == (
        "written by a newer COYPU (format 3); only the parts this version knows are read",
    )
    assert len(project.record_stops()) == 5


async def test_import_coypu_refuses_a_missing_version_as_bad_params(server_url, tmp_path):
    path = make_v2_archive(tmp_path, drop_format_version=True)
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "err"
        assert res.error.code == "E_BAD_PARAMS"
        assert "not a COYPU project file" in res.error.message


# --- criterion 5: size guards ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit_name", "entry_name", "changes"),
    [
        ("PROJECT_JSON_MAX_BYTES", "project.json", {}),
        ("KARTO_DATASET_MAX_BYTES", "karto/dataset.json", {"karto": KARTO_SCHEMA_1}),
        ("ASSETS_MAX_BYTES", KRALUPY_ASSET, {}),
    ],
    ids=["project-json", "karto", "assets"],
)
def test_oversized_entries_are_refused(tmp_path, monkeypatch, limit_name, entry_name, changes):
    path = make_v2_archive(tmp_path, **changes)
    monkeypatch.setattr(archive_module, limit_name, 10)

    with pytest.raises(ValueError, match=re.escape(entry_name)) as info:
        read_coypu(path)
    assert "bytes" in str(info.value)


def test_limits_are_the_documented_sizes():
    assert archive_module.PROJECT_JSON_MAX_BYTES == 256 * 1024 * 1024
    assert archive_module.KARTO_DATASET_MAX_BYTES == 64 * 1024 * 1024
    assert archive_module.ASSETS_MAX_BYTES == 256 * 1024 * 1024


# --- criterion 6: karto dataset -------------------------------------------------------------------


def test_karto_dataset_is_parsed_or_warned(tmp_path):
    parsed = read_coypu(make_v2_archive(tmp_path, karto=KARTO_SCHEMA_1, name="schema1.coypu"))
    assert parsed.karto == KARTO_SCHEMA_1
    assert parsed.warnings == ()

    wrong_schema = read_coypu(
        make_v2_archive(tmp_path, karto={**KARTO_SCHEMA_1, "schemaVersion": 2}, name="schema2.coypu")
    )
    assert wrong_schema.karto is None
    assert any("karto/dataset.json ignored" in w and "schemaVersion 2" in w for w in wrong_schema.warnings)

    invalid = read_coypu(make_v2_archive(tmp_path, karto='{"schemaVersion": 1,', name="invalid.coypu"))
    assert invalid.karto is None
    assert any("karto/dataset.json ignored" in w for w in invalid.warnings)

    not_finite = read_coypu(
        make_v2_archive(tmp_path, karto='{"schemaVersion": 1, "x": NaN}', name="nan.coypu")
    )
    assert not_finite.karto is None
    assert any("non-finite number NaN" in w for w in not_finite.warnings)


def test_no_karto_entry_is_silent(tmp_path):
    project = read_coypu(make_v2_archive(tmp_path))
    assert project.karto is None
    assert project.warnings == ()


# --- further contract fields: direction labels, run direction, unknown values ---------------------


def test_direction_labels_run_direction_and_unknown_values_are_kept(tmp_path):
    project = read_coypu(make_v2_archive(tmp_path))
    assert project.direction_labels == (
        {"forward": "Towards the synthetic end", "reverse": "Towards the start"},
        {"forward": "Towards the synthetic end", "reverse": "Towards the start"},
    )
    assert project.run_reversed is False

    odd = read_coypu(
        make_v2_archive(tmp_path, line={"number": "9", "name": "", "category": "suburban"}, name="odd.coypu")
    )
    assert odd.line.category == "suburban"
    assert odd.warnings == ("unknown line category 'suburban' kept as written",)

    unknown = read_coypu(make_v2_archive(tmp_path, active_stop_pattern="Zz", name="unknown.coypu"))
    assert unknown.active_stop_pattern == "Os"
    assert unknown.warnings == ("unknown activeStopPattern 'Zz'; using Os",)


# --- criterion 7: wire ----------------------------------------------------------------------------


async def test_import_coypu_result_carries_version_and_line(server_url, tmp_path):
    path = make_v2_archive(tmp_path)
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "res", res.error
        result = res.result

        assert result["format_version"] == 2
        assert result["line"] == {"number": "S-SYN", "name": "Synthetic line", "category": "regional"}
        assert result["warnings"] == []
        assert len(result["stops"]) == 5
        assert result["stops"][0] == {
            "station_m": 14000.0,
            "dwell_s": 40.0,
            "name": "Delta",
            "stop_id": "s004",
            "patterns": ["Os", "Ex"],
        }
        assert [run["stop_count"] for run in result["runs"]] == [5, 5]

        res_v1 = await _call(ws, "2", "import.coypu", {"path": str(KRALUPY_COYPU)})
        assert res_v1.type == "res", res_v1.error
        assert res_v1.result["format_version"] == 1
        assert res_v1.result["line"] is None
        assert all(stop["patterns"] == [] for stop in res_v1.result["stops"])


# --- chainage map of an optimised alignment, and a file that is not a ZIP ---------------------------


def test_an_optimised_run_is_mapped_back_to_the_embedded_chainage(tmp_path):
    # Active chainage is the baseline stretched by 0.1 % and shifted by 2 m; the map spans the run.
    baseline_km = np.array([-1.0, 9.0, 19.0])
    active_km = baseline_km * 1.001 + 0.002
    path = make_v2_archive(tmp_path, chainage_map=(baseline_km, active_km), name="optimised.coypu")
    project = read_coypu(path)

    raw = project.kinematics(0)
    run = project.kinematics_runs()[0]
    expected_m = (raw["station_m"] / 1000.0 - 0.002) / 1.001 * 1000.0
    np.testing.assert_allclose(run.station_m, expected_m, rtol=0.0, atol=1e-6)

    assert len(project.warnings) == 1
    assert project.warnings[0].startswith("COYPU optimised this alignment after import")
    assert "(largest shift 21.00 m)" in project.warnings[0]
    assert [s.station_m for s in run.stops] == [500.0, 3000.0, 8000.0, 14000.0, 18000.0]


def test_identity_map_changes_nothing(tmp_path):
    reference = read_coypu(KRALUPY_COYPU).kinematics_runs()
    baseline_km = np.array([-1.0, 9.0, 19.0])
    path = make_v2_archive(tmp_path, chainage_map=(baseline_km, baseline_km + 5e-7), name="identity.coypu")
    project = read_coypu(path)

    assert project.warnings == ()
    for new, old in zip(project.kinematics_runs(), reference, strict=True):
        np.testing.assert_array_equal(new.station_m, old.station_m)


def test_non_monotonic_map_is_not_applied_and_warns(tmp_path):
    reference = read_coypu(KRALUPY_COYPU).kinematics_runs()
    baseline_km = np.array([-1.0, 9.0, 12.0, 19.0])
    active_km = np.array([-1.0, 9.5, 9.0, 19.02])
    path = make_v2_archive(tmp_path, chainage_map=(baseline_km, active_km), name="nonmonotonic.coypu")
    project = read_coypu(path)

    assert project.warnings == ("chainage map is not monotonic; the run is left on COYPU's active chainage",)
    np.testing.assert_array_equal(project.kinematics_runs()[0].station_m, reference[0].station_m)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path):
    path = tmp_path / "plain.coypu"
    path.write_bytes(b"this is plain text, not a ZIP archive\n")
    with pytest.raises(ValueError, match="not a COYPU project file"):
        read_coypu(path)


async def test_import_coypu_refuses_a_file_that_is_not_a_zip_as_bad_params(server_url, tmp_path):
    path = tmp_path / "plain.coypu"
    path.write_bytes(b"this is plain text, not a ZIP archive\n")
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "err"
        assert res.error.code == "E_BAD_PARAMS"
        assert "not a COYPU project file" in res.error.message


# --- bounded reads, damaged archives, nested karto, stations beyond the map, gap tests ------------

INFLATED_BYTES = 64 * 1024 * 1024
PEAK_LIMIT_BYTES = 16 * 1024 * 1024


def make_lying_archive(path: Path, entry_name: str, inflated_bytes: int, declared: int = 1000) -> Path:
    """A ZIP whose directory entry and local header declare `declared` bytes for an entry that inflates to
    `inflated_bytes`."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        if entry_name != "project.json":
            out.writestr("project.json", '{"formatVersion": 2}')
        out.writestr(entry_name, b" " * inflated_bytes)
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        header_offset = archive.getinfo(entry_name).header_offset
    struct.pack_into("<I", raw, header_offset + 22, declared)
    position = 0
    while (position := raw.find(b"PK\x01\x02", position)) >= 0:
        name_length = struct.unpack_from("<H", raw, position + 28)[0]
        if bytes(raw[position + 46 : position + 46 + name_length]) == entry_name.encode():
            struct.pack_into("<I", raw, position + 24, declared)
            break
        position += 4
    else:
        raise AssertionError("central directory entry not found")
    path.write_bytes(bytes(raw))
    return path


@pytest.mark.parametrize("entry_name", ["karto/dataset.json", "assets/001_x.xml", "project.json"])
def test_an_understated_entry_size_cannot_inflate_past_the_limit(tmp_path, entry_name):
    path = make_lying_archive(tmp_path / "lying.coypu", entry_name, INFLATED_BYTES)
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            read_coypu(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < PEAK_LIMIT_BYTES


def test_a_deeply_nested_karto_entry_is_a_warning_not_an_error(tmp_path):
    path = tmp_path / "deep.coypu"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("project.json", '{"formatVersion": 2}')
        out.writestr("karto/dataset.json", b"[" * 200_000)
    project = read_coypu(path)
    assert project.karto is None
    assert any("karto/dataset.json ignored" in warning for warning in project.warnings)


def _entry_data_offset(raw: bytes, archive_path, name: str) -> tuple[int, int]:
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo(name)
    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    return info.header_offset + 30 + name_length + extra_length, info.compress_size


def _set_central_field(raw: bytearray, name: str, offset: int, fmt: str, value: int) -> None:
    position = raw.find(b"PK\x01\x02")
    while position >= 0:
        name_length = struct.unpack_from("<H", raw, position + 28)[0]
        if bytes(raw[position + 46 : position + 46 + name_length]) == name.encode():
            struct.pack_into(fmt, raw, position + offset, value)
            return
        position = raw.find(b"PK\x01\x02", position + 4)
    raise AssertionError("central directory entry not found")


@pytest.mark.parametrize("damage", ["corrupt-deflate", "encrypted", "unknown-method"])
def test_a_damaged_archive_is_refused_as_not_a_coypu_project(tmp_path, damage):
    path = make_v2_archive(tmp_path)
    raw = bytearray(path.read_bytes())
    if damage == "corrupt-deflate":
        start, _size = _entry_data_offset(bytes(raw), path, "project.json")
        raw[start + 100 : start + 140] = bytes(40)
    elif damage == "encrypted":
        _set_central_field(raw, "project.json", 8, "<H", 1)
    else:
        _set_central_field(raw, "project.json", 10, "<H", 99)
    path.write_bytes(bytes(raw))

    with pytest.raises(ValueError, match="not a COYPU project file"):
        read_coypu(path)


def test_a_project_json_that_is_not_json_is_refused(tmp_path):
    path = tmp_path / "not_json.coypu"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("project.json", b"not json")
    with pytest.raises(ValueError, match="not a COYPU project file"):
        read_coypu(path)


def test_an_archive_without_project_json_is_refused(tmp_path):
    path = tmp_path / "no_project.coypu"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("assets/001_x.xml", "<LandXML/>")
    with pytest.raises(ValueError, match="not a COYPU project file"):
        read_coypu(path)


def test_asset_sizes_are_limited_in_total_not_per_entry(tmp_path, monkeypatch):
    path = make_v2_archive(tmp_path)
    with zipfile.ZipFile(path) as archive:
        asset_bytes = archive.read(KRALUPY_ASSET)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("assets/002_copy.xml", asset_bytes)
    monkeypatch.setattr(archive_module, "ASSETS_MAX_BYTES", len(asset_bytes) + len(asset_bytes) // 2)

    with pytest.raises(ValueError, match=r"002_copy\.xml") as info:
        read_coypu(path)
    assert "bytes" in str(info.value)


def test_stations_beyond_the_chainage_map_keep_the_end_shift(tmp_path):
    # The map spans 2 km to 15 km, the run 0 m to 18 185 m; the active chainage is the baseline plus 50 m.
    baseline_km = np.array([2.0, 9.0, 15.0])
    path = make_v2_archive(tmp_path, chainage_map=(baseline_km, baseline_km + 0.05), name="narrow.coypu")
    raw = read_coypu(path).kinematics(0)["station_m"]
    run = read_coypu(path).kinematics_runs()[0]
    np.testing.assert_allclose(run.station_m, raw - 50.0, rtol=0.0, atol=1e-6)
    assert np.count_nonzero(run.station_m == run.station_m.min()) < 10


def test_chainage_arrays_of_different_length_are_not_applied_and_warn(tmp_path):
    reference = read_coypu(KRALUPY_COYPU).kinematics_runs()
    path = make_v2_archive(
        tmp_path,
        chainage_map=(np.array([-1.0, 9.0, 19.0]), np.array([-1.0, 9.5])),
        name="mismatch.coypu",
    )
    project = read_coypu(path)
    assert project.warnings == (
        "chainage map arrays differ in length; the run is left on COYPU's active chainage",
    )
    np.testing.assert_array_equal(project.kinematics_runs()[0].station_m, reference[0].station_m)


@pytest.mark.parametrize(
    "text",
    [
        '{"schemaVersion": 1, "x": NaN}',
        '{"schemaVersion": 1, "x": Infinity}',
        '{"schemaVersion": 1, "x": -Infinity}',
        '{"schemaVersion": 1, "x": 1e999}',
        '{"schemaVersion": true}',
        "[1, 2]",
    ],
    ids=["nan", "infinity", "negative-infinity", "float-overflow", "bool-schema", "not-an-object"],
)
def test_karto_edge_cases_are_ignored_with_a_warning(tmp_path, text):
    project = read_coypu(make_v2_archive(tmp_path, karto=text))
    assert project.karto is None
    assert any("karto/dataset.json ignored" in warning for warning in project.warnings)


async def test_import_coypu_appends_the_archive_warnings_and_a_partial_line(server_url, tmp_path):
    path = make_v2_archive(
        tmp_path,
        format_version=3,
        line={"number": "9", "name": "", "category": ""},
        name="partial.coypu",
    )
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "res", res.error
        assert res.result["format_version"] == 3
        assert res.result["line"] == {"number": "9", "name": "", "category": ""}
        assert res.result["warnings"] == [
            "written by a newer COYPU (format 3); only the parts this version knows are read"
        ]


def test_unusable_stop_records_are_skipped_and_flags_default_as_in_coypu(tmp_path):
    raw_records = [
        {"stopId": "s001", "stopName": "Only valid", "stationingKm": 1.5},
        {"stopId": "s002", "stopName": "No km", "stationingKm": None},
        {"stopId": "s003", "stopName": "Missing km"},
        "junk",
        {"stopId": "s004", "stopName": "NaN km", "stationingKm": float("nan")},
    ]
    project = read_coypu(make_v2_archive(tmp_path, raw_stop_records=raw_records, name="raw.coypu"))

    assert [record.stop_id for record in project.stop_records] == ["s001"]
    record = project.stop_records[0]
    assert record.patterns == ("Os",)
    assert record.dwell_s == 30.0
    assert project.warnings == ("4 stop record(s) without a usable stationingKm skipped",)


def test_stop_records_keep_municipality_population_and_dwell(tmp_path):
    project = read_coypu(make_v2_archive(tmp_path))
    by_id = {record.stop_id: record for record in project.stop_records}

    assert by_id["s001"] == CoypuStopRecord(
        stop_id="s001",
        name="Alpha",
        station_km=0.5,
        dwell_s=20.0,
        municipality="North",
        population=1500,
        patterns=("Os", "Sp", "Ex"),
    )
    assert by_id["s005"] == CoypuStopRecord(
        stop_id="s005",
        name="Echo",
        station_km=18.0,
        dwell_s=0.0,
        municipality="South",
        population=None,
        patterns=("Os", "Sp", "Ex"),
    )


def test_run_reversed_is_read_from_the_settings(tmp_path):
    assert read_coypu(make_v2_archive(tmp_path)).run_reversed is False
    assert (
        read_coypu(make_v2_archive(tmp_path, run_reversed=True, name="reversed.coypu")).run_reversed is True
    )


# --- criterion 8: live samples --------------------------------------------------------------------


async def test_real_coypu_samples_import(server_url):
    root = os.environ.get("COYPU_DATA")
    if not root:
        pytest.skip("COYPU_DATA is not set, so no ${COYPU_DATA}/Coypu_Samples/*.coypu files are read")
    samples = sorted(Path(root, "Coypu_Samples").glob("*.coypu"))
    if not samples:
        pytest.skip("no .coypu files in ${COYPU_DATA}/Coypu_Samples")

    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        for sample in samples:
            res = await _call(ws, "1", "import.coypu", {"path": str(sample)})
            assert res.type == "res", f"{sample.name}: {res.error}"
            result = res.result
            print(
                f"{sample.name}: format {result['format_version']}, runs {len(result['runs'])}, "
                f"stops {len(result['stops'])}, warnings {result['warnings']}"
            )
