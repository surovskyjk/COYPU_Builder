"""T-129: an optimised `.coypu` archive imports COYPU's optimised alignment, keeps its run on the
active chainage and maps the stops onto it. A gate that fails falls back to T-128's map-back.

The optimised archive is synthesised from the Kralupy version 1 fixture by `make_optimised_archive`: every
element X token moves 0.5 m, every station and the dense axis move 2.0 m along the chainage, and the chainage
map is the baseline shifted by 0.002 km. Every value comes from this file or from the fixture, never from a
real archive.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pyproj import Transformer
from websockets.asyncio.client import connect

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.io.coypu import geometry
from coypu_builder.io.coypu.archive import MAP_BACK_WARNING_PREFIX, baseline_to_active_m, read_coypu
from coypu_builder.io.coypu.geometry import (
    GATE_ARRAYS,
    GATE_CHAINAGE,
    GATE_CONTINUITY,
    GATE_CONVERSION,
    GATE_DENSE,
    GATE_SEVERAL_ALIGNMENTS,
    elements_from_arrays,
    optimised_alignment,
    simulated_stops_warning,
)
from coypu_builder.io.landxml.reader import read_landxml, to_alignment
from coypu_builder.server import serve
from coypu_builder.server.handlers import _read_coypu_bundle, _stops_from_coypu
from test_coypu_v2 import (
    KRALUPY_ASSET,
    KRALUPY_COYPU,
    SYNTHETIC_RECORDS,
    _call,
    _hello_and_project,
    _record,
    make_v2_archive,
)

SHIFT_ALONG_KM = 0.002
SHIFT_ALONG_M = 2.0
SHIFT_ACROSS_M = 0.5
X_TOKEN_KEYS = (
    "lineStartX",
    "lineEndX",
    "spiralStartX",
    "spiralPIX",
    "spiralEndX",
    "curveStartX",
    "curveCenterX",
    "curveEndX",
)
STATION_KEYS = ("stationHorizontal", "lineStationStart", "spiralStationStart", "curveStationStart")
OPTIMISED_PREFIX = "COYPU optimised this alignment: Builder uses the optimised geometry from the archive ("
KRALUPY_ELEMENT_COUNT = 65


def _ndarray(values: Any) -> dict[str, Any]:
    return {"__ndarray__": True, "dtype": "float64", "values": [float(v) for v in values]}


def make_optimised_archive(
    tmp_path: Path,
    *,
    name: str = "optimised.coypu",
    drop_keys: tuple[str, ...] = (),
    break_element: tuple[str, int, float] | None = None,
    break_station: tuple[int, float] | None = None,
    dense_north_shift_m: float = -SHIFT_ACROSS_M,
    stop_records: bool = False,
    simulated_stops_km: list[float] | None = None,
    extra_assets: dict[str, bytes] | None = None,
    without_calculated_cant: bool = False,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """A synthetic optimised archive built from the Kralupy version 1 fixture.

    `drop_keys` removes landXml arrays. `break_element` = (key, index, delta) moves one element array entry,
    `break_station` = (index, delta_km) moves one entry of stationHorizontal.
    `dense_north_shift_m` moves COYPU's dense axis across the track. The default -0.5 m follows the 0.5 m
    move of the X token (north is -X); 0.0 leaves the dense axis where the file had it. `stop_records`
    writes format 2 stop records (T-128's synthetic records). `simulated_stops_km` writes COYPU's
    `simulatedTrainStops`, in active km. `extra_assets` adds embedded assets (the archive then embeds several
    alignments). `without_calculated_cant` drops COYPU's `stationCantPossible` and `cantPossible`. `mutate`
    edits the payload last, before it is written."""
    with zipfile.ZipFile(KRALUPY_COYPU) as source:
        payload = json.loads(source.read("project.json"))
        assets = {name: source.read(name) for name in source.namelist() if name != "project.json"}
    assets.update(extra_assets or {})
    landxml = payload["alignmentsData"]["landXml"]
    derived = payload["calculationCache"]["landXmlDerived"]

    stations_km = np.asarray(landxml["stationHorizontal"]["values"], dtype=float)
    for key in X_TOKEN_KEYS:
        landxml[key] = _ndarray(np.asarray(landxml[key]["values"], dtype=float) + SHIFT_ACROSS_M)
    for key in STATION_KEYS:
        landxml[key] = _ndarray(np.asarray(landxml[key]["values"], dtype=float) + SHIFT_ALONG_KM)
    derived["stationCantPossible"] = _ndarray(
        np.asarray(derived["stationCantPossible"]["values"], dtype=float) + SHIFT_ALONG_KM
    )
    # COYPU simulates after its last optimisation, so the stored run is on the active chainage (metres)
    data_storage = payload["calculationCache"]["dataStorage"]
    for key in [key for key in data_storage if key.startswith("kinematicsStationM_")]:
        data_storage[key] = _ndarray(np.asarray(data_storage[key]["values"], dtype=float) + SHIFT_ALONG_M)

    inverse = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
    forward = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
    dense = np.asarray(landxml["denseAlignment"], dtype=float)
    east, north = inverse.transform(dense[:, 2], dense[:, 1])
    lon, lat = forward.transform(np.asarray(east), np.asarray(north) + dense_north_shift_m)
    landxml["denseAlignment"] = [
        [float(station) + SHIFT_ALONG_KM, float(la), float(lo)]
        for station, la, lo in zip(dense[:, 0], lat, lon, strict=True)
    ]

    baseline_km = np.unique(stations_km)
    derived["chainageMapBaselineKm"] = _ndarray(baseline_km)
    derived["chainageMapActiveKm"] = _ndarray(baseline_km + SHIFT_ALONG_KM)

    if break_element is not None:
        key, index, delta = break_element
        values = np.asarray(landxml[key]["values"], dtype=float)
        values[index] += delta
        landxml[key] = _ndarray(values)
    if break_station is not None:
        index, delta_km = break_station
        stations = np.asarray(landxml["stationHorizontal"]["values"], dtype=float)
        stations[index] += delta_km
        landxml["stationHorizontal"] = _ndarray(stations)
    for key in drop_keys:
        landxml.pop(key)
    if without_calculated_cant:
        derived.pop("stationCantPossible")
        derived.pop("cantPossible")
    if mutate is not None:
        mutate(payload)
    if stop_records:
        payload["formatVersion"] = 2
        payload["stopsData"]["stopRecords"] = [_record(row) for row in SYNTHETIC_RECORDS]
        payload["stopsData"]["activeStopPattern"] = "Os"
    if simulated_stops_km is not None:
        payload["calculationCache"]["dataStorage"]["simulatedTrainStops"] = [
            [float(km), 30.0, "simulated"] for km in simulated_stops_km
        ]

    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("project.json", json.dumps(payload, ensure_ascii=False))
        for asset_name, data in assets.items():
            out.writestr(asset_name, data)
    return target


def _embedded_alignment():
    project = read_coypu(KRALUPY_COYPU)
    return project, read_landxml(project.raw_assets[KRALUPY_ASSET].encode("utf-8"))[0]


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


# --- criterion 1: the element builder rebuilds the embedded alignment ----------------------------------


def test_kralupy_elements_rebuild_the_embedded_alignment():
    project, original = _embedded_alignment()

    rebuilt, gate = elements_from_arrays(project, original)

    assert gate is None
    assert rebuilt is not None
    crs = ProjectCRS("EPSG:5514")
    embedded = to_alignment(original, crs).alignment
    built = to_alignment(rebuilt, crs).alignment
    assert built.station_start == pytest.approx(embedded.station_start, abs=1e-6)
    assert built.station_end == pytest.approx(embedded.station_end, abs=1e-6)
    np.testing.assert_allclose(built.horizontal.stations, embedded.horizontal.stations, rtol=0.0, atol=1e-6)
    sample = np.arange(embedded.station_start, embedded.station_end, 1.0)
    np.testing.assert_allclose(
        built.horizontal.point(sample), embedded.horizontal.point(sample), rtol=0.0, atol=1e-6
    )


def test_kralupy_dense_axis_is_within_the_gate():
    project, original = _embedded_alignment()
    rebuilt, _gate = elements_from_arrays(project, original)

    deviation_m = geometry._dense_deviation_m(project, rebuilt, ProjectCRS("EPSG:5514"))

    assert deviation_m is not None
    assert deviation_m < geometry.DENSE_TOLERANCE_M
    # The gate is 20 mm; the pyproj round trip on this fixture is about 1.8 mm, so a 5 mm bound still holds
    assert deviation_m < 5e-3


# --- criterion 2: an optimised archive uses the optimised geometry -------------------------------------


def _optimised_notice(warnings: tuple[str, ...]) -> str:
    matches = [warning for warning in warnings if warning.startswith(OPTIMISED_PREFIX)]
    assert len(matches) == 1, warnings
    return matches[0]


def test_an_optimised_archive_uses_the_optimised_geometry(tmp_path):
    path = make_optimised_archive(tmp_path)
    project, original = _embedded_alignment()
    raws, shown_stops, runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    imported = raws[0]
    assert imported.sta_start == pytest.approx(original.sta_start + SHIFT_ALONG_M, abs=1e-6)
    crs = ProjectCRS("EPSG:5514")
    before = to_alignment(original, crs).alignment.horizontal.point(np.array([original.sta_start]))[0]
    after = to_alignment(imported, crs).alignment.horizontal.point(np.array([imported.sta_start]))[0]
    np.testing.assert_allclose(after - before, [0.0, -SHIFT_ACROSS_M], rtol=0.0, atol=1e-6)

    np.testing.assert_array_equal(runs[0][0].station_m, read_coypu(path).kinematics(0)["station_m"])

    stops_m = np.array([stop.station_m for stop in _stops_from_coypu(project)])
    np.testing.assert_allclose(
        [stop.station_m for stop in shown_stops], stops_m + SHIFT_ALONG_M, rtol=0.0, atol=1e-6
    )
    np.testing.assert_allclose(
        [stop.station_m for stop in runs[0][0].stops], stops_m + SHIFT_ALONG_M, rtol=0.0, atol=1e-6
    )

    assert len(imported.profile) == len(original.profile) > 0
    for shifted, item in zip(imported.profile, original.profile, strict=True):
        assert shifted.station == pytest.approx(item.station + SHIFT_ALONG_M, abs=1e-6)
        assert shifted.elevation == item.elevation
        assert shifted.length == item.length
        assert shifted.radius == item.radius

    notice = _optimised_notice(warnings)
    assert f"({KRALUPY_ELEMENT_COUNT} elements; " in notice
    deviation_mm = float(re.search(r"dense axis ([0-9.]+) mm", notice).group(1))
    assert deviation_mm < geometry.DENSE_TOLERANCE_M * 1000.0
    assert not any(warning.startswith(MAP_BACK_WARNING_PREFIX) for warning in warnings)


def test_the_optimised_cant_is_the_calculated_one(tmp_path):
    path = make_optimised_archive(tmp_path)
    raws, _stops, _runs, _meta = _read_coypu_bundle(path)
    derived = read_coypu(path).landxml_derived

    np.testing.assert_array_equal(np.asarray(raws[0].cant.values), np.asarray(derived["cantPossible"]))
    np.testing.assert_allclose(
        np.asarray(raws[0].cant.stations),
        np.asarray(derived["stationCantPossible"]) * 1000.0,
        rtol=0.0,
        atol=1e-9,
    )


def test_stop_records_are_mapped_onto_the_optimised_chainage(tmp_path):
    path = make_optimised_archive(tmp_path, stop_records=True)
    project = read_coypu(path)
    _raws, shown_stops, runs, (version, _line, _warnings) = _read_coypu_bundle(path)

    assert version == 2
    pattern_m = sorted(
        record.station_km * 1000.0 for record in project.stop_records if "Os" in record.patterns
    )
    np.testing.assert_allclose(
        [stop.station_m for stop in runs[0][0].stops],
        np.array(pattern_m) + SHIFT_ALONG_M,
        rtol=0.0,
        atol=1e-6,
    )
    assert [stop.stop_id for stop in shown_stops] == [record.stop_id for record in project.stop_records]


def test_simulated_stops_that_disagree_warn(tmp_path):
    active_km = [0.378, 5.040, 7.613, 10.919, 15.760, 18.150]
    agreeing = make_optimised_archive(tmp_path, name="agree.coypu", simulated_stops_km=active_km)
    assert simulated_stops_warning(read_coypu(agreeing)) is None

    off_by_5mm = [0.378, 5.040005, 7.613, 10.919, 15.760, 18.150]
    disagreeing = make_optimised_archive(tmp_path, name="disagree.coypu", simulated_stops_km=off_by_5mm)
    warning = simulated_stops_warning(read_coypu(disagreeing))
    assert warning is not None
    assert warning.startswith("the stops Builder maps onto the optimised chainage differ")
    assert "5.000 mm" in warning


# --- criterion 3: each gate fails once and falls back --------------------------------------------------


def _assert_falls_back(path: Path, gate: str) -> None:
    raws, _stops, runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    assert (
        f"optimised geometry not used: {gate}; the run was mapped back to the original geometry" in warnings
    )
    assert not any(warning.startswith(OPTIMISED_PREFIX) for warning in warnings)
    assert any(warning.startswith(MAP_BACK_WARNING_PREFIX) for warning in warnings)
    _project, original = _embedded_alignment()
    assert raws[0].sta_start == original.sta_start
    np.testing.assert_array_equal(runs[0][0].station_m, read_coypu(path).kinematics_runs()[0].station_m)


def test_missing_arrays_fall_back(tmp_path):
    path = make_optimised_archive(tmp_path, name="missing.coypu", drop_keys=("spiralRadiusStart",))
    _assert_falls_back(path, GATE_ARRAYS)


def test_a_continuity_gap_falls_back(tmp_path):
    path = make_optimised_archive(tmp_path, name="gap.coypu", break_element=("spiralStartX", 3, 0.5))
    _assert_falls_back(path, GATE_CONTINUITY)


def test_a_station_gap_falls_back(tmp_path):
    path = make_optimised_archive(tmp_path, name="station_gap.coypu", break_station=(3, 0.00001))
    _assert_falls_back(path, GATE_CONTINUITY)


def test_a_dense_axis_mismatch_falls_back(tmp_path):
    # The dense axis stays 0.5 m from the rebuilt axis, far beyond the 20 mm gate
    path = make_optimised_archive(tmp_path, name="dense.coypu", dense_north_shift_m=0.0)
    _assert_falls_back(path, GATE_DENSE)


def test_several_embedded_alignments_fall_back(tmp_path):
    with zipfile.ZipFile(KRALUPY_COYPU) as source:
        asset = source.read(KRALUPY_ASSET)
    path = make_optimised_archive(tmp_path, name="several.coypu", extra_assets={"assets/002_copy.xml": asset})
    raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    assert len(raws) == 2
    assert (
        "optimised geometry not used: the archive embeds several alignments; "
        "the run was mapped back to the original geometry"
    ) in warnings
    _assert_falls_back(path, GATE_SEVERAL_ALIGNMENTS)


def test_the_notice_names_the_calculated_cant(tmp_path):
    path = make_optimised_archive(tmp_path)
    _raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    notice = _optimised_notice(warnings)
    assert notice.endswith(
        "with the original file's profile re-stationed onto it and COYPU's calculated cant"
    )


def test_the_notice_names_the_restationed_cant_without_a_calculation(tmp_path):
    path = make_optimised_archive(tmp_path, name="no_calculation.coypu", without_calculated_cant=True)
    _project, original = _embedded_alignment()
    raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    notice = _optimised_notice(warnings)
    assert notice.endswith(
        "with the original file's profile re-stationed onto it and the original cant re-stationed onto it"
    )
    np.testing.assert_array_equal(np.asarray(raws[0].cant.values), np.asarray(original.cant.values))
    np.testing.assert_allclose(
        np.asarray(raws[0].cant.stations),
        np.asarray(original.cant.stations) + SHIFT_ALONG_M,
        rtol=0.0,
        atol=1e-6,
    )


# --- criterion 4: archives that are not optimised are unchanged ----------------------------------------


def test_non_optimised_archives_are_unchanged(tmp_path):
    for path in (KRALUPY_COYPU, make_v2_archive(tmp_path)):
        project = read_coypu(path)
        original = read_landxml(project.raw_assets[KRALUPY_ASSET].encode("utf-8"))[0]
        assert optimised_alignment(project, original) == (None, ())

        raws, shown_stops, runs, (version, _line, warnings) = _read_coypu_bundle(path)
        stops = _stops_from_coypu(project)
        reference = project.kinematics_runs(stops=stops)
        assert warnings == project.warnings
        assert shown_stops == stops
        assert raws[0].elements == original.elements
        assert version == project.format_version
        for (run, _trainset, _warning), old in zip(runs, reference, strict=True):
            np.testing.assert_array_equal(run.station_m, old.station_m)
            np.testing.assert_array_equal(run.time_s, old.time_s)
            np.testing.assert_array_equal(run.speed_ms, old.speed_ms)
            assert run.stops == old.stops


# --- criterion 5: over the wire ------------------------------------------------------------------------


async def test_import_coypu_uses_the_optimised_alignment_over_the_wire(server_url, tmp_path):
    path = make_optimised_archive(tmp_path)
    _project, original = _embedded_alignment()
    async with connect(server_url) as ws:
        await _hello_and_project(ws)
        res = await _call(ws, "1", "import.coypu", {"path": str(path)})
        assert res.type == "res", res.error
        result = res.result

        alignment = result["alignments"][0]
        assert alignment["station_start"] == pytest.approx(original.sta_start + SHIFT_ALONG_M, abs=1e-6)
        assert alignment["length"] == pytest.approx(18184.971666, abs=1e-3)
        assert any(warning.startswith(OPTIMISED_PREFIX) for warning in result["warnings"])
        assert not any(warning.startswith(MAP_BACK_WARNING_PREFIX) for warning in result["warnings"])


# --- review additions -----------------------------------------------------------------------------------


def _landxml(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["alignmentsData"]["landXml"]


def _derived(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["calculationCache"]["landXmlDerived"]


def test_stations_beyond_the_map_keep_the_shift_of_the_nearest_end():
    baseline_km = np.array([1.0, 2.0, 3.0])
    active_km = np.array([1.1, 2.3, 3.2])

    mapped = baseline_to_active_m(np.array([500.0, 1000.0, 1500.0, 3000.0, 3500.0]), baseline_km, active_km)

    np.testing.assert_allclose(mapped, [600.0, 1100.0, 1700.0, 3200.0, 3700.0], rtol=0.0, atol=1e-9)


def test_stop_names_survive_a_chainage_shift_beyond_the_name_tolerance(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "SHIFT_ALONG_KM", 0.012)
    monkeypatch.setattr(sys.modules[__name__], "SHIFT_ALONG_M", 12.0)
    path = make_optimised_archive(tmp_path)
    expected = [stop.name for stop in _stops_from_coypu(read_coypu(path))]
    _raws, _stops, runs, _meta = _read_coypu_bundle(path)

    assert expected and all(expected)
    assert [stop.name for stop in runs[0][0].stops] == expected


def test_the_bundle_reports_simulated_stops_that_disagree(tmp_path):
    path = make_optimised_archive(
        tmp_path, simulated_stops_km=[0.378, 5.040005, 7.613, 10.919, 15.760, 18.150]
    )
    _raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    assert any(w.startswith("the stops Builder maps onto the optimised chainage differ") for w in warnings)


def test_a_simulated_stop_count_that_differs_warns(tmp_path):
    path = make_optimised_archive(tmp_path, simulated_stops_km=[0.378])

    warning = simulated_stops_warning(read_coypu(path))

    assert warning == (
        "COYPU simulated 1 stop(s) but the archive schedules 6; "
        "the stops Builder maps are not those simulated"
    )


def test_a_calculated_cant_of_the_wrong_length_is_not_used(tmp_path):
    def shorten(payload):
        _derived(payload)["cantPossible"]["values"].pop()

    path = make_optimised_archive(tmp_path, mutate=shorten)
    _project, original = _embedded_alignment()
    raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    assert _optimised_notice(warnings).endswith("and the original cant re-stationed onto it")
    assert len(raws[0].cant.stations) == len(raws[0].cant.values) == len(original.cant.values)


def test_a_non_finite_calculated_cant_is_not_used(tmp_path):
    def nan_value(payload):
        _derived(payload)["cantPossible"]["values"][5] = float("nan")

    path = make_optimised_archive(tmp_path, mutate=nan_value)
    _raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(path)

    assert _optimised_notice(warnings).endswith("and the original cant re-stationed onto it")


def test_the_dense_gate_is_20_mm(tmp_path):
    inside = make_optimised_archive(
        tmp_path, name="inside.coypu", dense_north_shift_m=-SHIFT_ACROSS_M + 0.010
    )
    outside = make_optimised_archive(
        tmp_path, name="outside.coypu", dense_north_shift_m=-SHIFT_ACROSS_M + 0.040
    )

    _raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(inside)
    assert _optimised_notice(warnings)
    _assert_falls_back(outside, GATE_DENSE)


def test_the_continuity_gate_is_1_mm(tmp_path):
    inside = make_optimised_archive(
        tmp_path, name="inside.coypu", break_element=("spiralStartX", 3, 0.0005), break_station=(3, 5e-7)
    )
    outside_xy = make_optimised_archive(
        tmp_path, name="outside_xy.coypu", break_element=("spiralStartX", 3, 0.003)
    )
    outside_station = make_optimised_archive(tmp_path, name="outside_sta.coypu", break_station=(3, 2e-6))

    _raws, _stops, _runs, (_version, _line, warnings) = _read_coypu_bundle(inside)
    assert _optimised_notice(warnings)
    _assert_falls_back(outside_xy, GATE_CONTINUITY)
    _assert_falls_back(outside_station, GATE_CONTINUITY)


def test_a_zero_length_spiral_falls_back(tmp_path):
    def zero(payload):
        _landxml(payload)["spiralLength"]["values"][3] = 0.0

    _assert_falls_back(make_optimised_archive(tmp_path, mutate=zero), GATE_CONVERSION)


def test_a_chainage_map_with_repeated_baseline_nodes_falls_back(tmp_path):
    def repeat(payload):
        values = _derived(payload)["chainageMapBaselineKm"]["values"]
        values[5] = values[4]

    _assert_falls_back(make_optimised_archive(tmp_path, mutate=repeat), GATE_CHAINAGE)


def test_a_non_finite_station_falls_back(tmp_path):
    def last_end(payload):
        _landxml(payload)["stationHorizontal"]["values"][-1] = float("nan")

    _assert_falls_back(make_optimised_archive(tmp_path, mutate=last_end), GATE_ARRAYS)


def test_an_unknown_archive_crs_falls_back(tmp_path):
    def crs(payload):
        payload["projectMetadata"]["epsgCode"] = "EPSG:99999"

    _assert_falls_back(make_optimised_archive(tmp_path, mutate=crs), geometry.GATE_CRS)


def test_non_finite_dense_points_fall_back(tmp_path):
    def nan_point(payload):
        _landxml(payload)["denseAlignment"][100][1] = float("nan")

    _assert_falls_back(make_optimised_archive(tmp_path, mutate=nan_point), GATE_DENSE)
