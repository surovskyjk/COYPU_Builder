import csv

import numpy as np
import pytest

from coypu_builder.domain.kinematics import RunDirection, Stop, normalise_run
from coypu_builder.io.coypu.kinematics_csv import read_kinematics_csv, read_stops_csv
from fixtures.synthetic.kinematics import reversed_run_raw

# --- helpers to synthesise CSV text from the real Kralupy .coypu kinematics arrays (AC2) ----------------


def _write_batch_csv(path, station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["stationM", "timeS", "speedMs", "accelMs2", "forceTracKN", "forceBrakeKN", "forceResKN"]
        )
        for row in zip(station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res, strict=True):
            writer.writerow(row)


_GUI_HEADERS = {
    "en": (
        "stationing [km]",
        "Time [s]",
        "Speed [km/h]",
        "Accel [m/s2]",
        "Tractive Force [kN]",
        "Braking Force [kN]",
        "Resistance [kN]",
    ),
    "cz": (
        "staničení [km]",
        "Čas [s]",
        "Rychlost [km/h]",
        "Accel [m/s2]",
        "Trakční síla [kN]",
        "Brzdná síla [kN]",
        "Jízdní odpor [kN]",
    ),
    "de": (
        "Streckenkilometer [km]",
        "Zeit [s]",
        "Geschwindigkeit [km/h]",
        "Accel [m/s2]",
        "Zugkraft [kN]",
        "Bremskraft [kN]",
        "Widerstand [kN]",
    ),
}


def _write_gui_csv(path, lang, station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_GUI_HEADERS[lang])
        rows = zip(station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res, strict=True)
        for s, t, v, a, ft, fb, fr in rows:
            writer.writerow(
                [
                    f"{s / 1000.0:.3f}",
                    f"{t:.1f}",
                    f"{v * 3.6:.1f}",
                    f"{a:.3f}",
                    f"{ft:.1f}",
                    f"{fb:.1f}",
                    f"{fr:.1f}",
                ]
            )


# --- AC1: Kralupy run 0 normalises cleanly ----------------------------------------------------------------


def test_kralupy_run_normalises_with_no_warnings(kralupy_project, kralupy_stops_csv, kralupy):
    stops = read_stops_csv(kralupy_stops_csv)
    run = kralupy_project.kinematics_run(0, stops=stops)

    assert run.warnings == ()
    assert np.all(np.diff(run.time_s) >= 0.0)
    assert run.time_s[0] == 0.0
    # COYPU's fixed 1 m kinematics grid is not clipped to the alignment's exact length (station_end here is
    # 18184.971666 m while the last kinematics row sits at the next whole metre, 18185.0 m) — allow one grid
    # step of slack rather than a mathematically strict bound.
    assert run.station_m.min() >= kralupy.alignment.station_start
    assert run.station_m.max() <= kralupy.alignment.station_end + 1.0


def test_kralupy_dwell_detection_recovers_all_six_stops(kralupy_project, kralupy_stops_csv):
    stops_csv = read_stops_csv(kralupy_stops_csv)
    run = kralupy_project.kinematics_run(0, stops=stops_csv)

    assert len(run.stops) == len(stops_csv) == 6
    for detected, reference in zip(
        sorted(run.stops, key=lambda s: s.station_m),
        sorted(stops_csv, key=lambda s: s.station_m),
        strict=True,
    ):
        assert detected.station_m == pytest.approx(reference.station_m, abs=5.0)
        assert detected.name == reference.name


# --- AC2: GUI-dialect and batch-dialect CSVs of the same run normalise the same way ------------------------


def test_batch_and_gui_dialect_round_trip(kralupy_project, tmp_path):
    raw = kralupy_project.kinematics(0)
    station_m, time_s = raw["station_m"], raw["time_s"]
    speed_ms, accel_ms2 = raw["speed_ms"], raw["accel_ms2"]
    f_trac, f_brake, f_res = raw["f_trac_kn"], raw["f_brake_kn"], raw["f_res_kn"]

    batch_path = tmp_path / "batch.csv"
    gui_path = tmp_path / "gui.csv"
    _write_batch_csv(batch_path, station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res)
    _write_gui_csv(gui_path, "en", station_m, time_s, speed_ms, accel_ms2, f_trac, f_brake, f_res)

    batch_run = read_kinematics_csv(batch_path)
    gui_run = read_kinematics_csv(gui_path)

    assert batch_run.direction == gui_run.direction == RunDirection.FORWARD
    # km, 3 decimals -> 1 mm
    np.testing.assert_allclose(batch_run.station_m, gui_run.station_m, atol=1e-3)
    np.testing.assert_allclose(batch_run.time_s, gui_run.time_s, atol=0.05)  # time rounded to 0.1 s
    # km/h, 1 decimal -> ~0.03 m/s
    np.testing.assert_allclose(batch_run.speed_ms, gui_run.speed_ms, atol=0.03)
    np.testing.assert_allclose(batch_run.station_m, station_m, atol=1e-9)


# --- AC3: localised GUI headers detected in EN, CZ, DE --------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "cz", "de"])
def test_gui_dialect_header_detected(tmp_path, lang):
    path = tmp_path / f"gui_{lang}.csv"
    _write_gui_csv(
        path,
        lang,
        station_m=np.array([0.0, 1000.0]),
        time_s=np.array([0.0, 100.0]),
        speed_ms=np.array([0.0, 10.0]),
        accel_ms2=np.array([0.0, 0.0]),
        f_trac=np.array([0.0, 0.0]),
        f_brake=np.array([0.0, 0.0]),
        f_res=np.array([0.0, 0.0]),
    )
    run = read_kinematics_csv(path)
    assert run.station_m.tolist() == pytest.approx([0.0, 1000.0], abs=1.0)
    assert run.speed_ms.tolist() == pytest.approx([0.0, 10.0], abs=0.03)


def test_unrecognised_header_raises_clear_error(tmp_path):
    path = tmp_path / "bogus.csv"
    path.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognised kinematics CSV header"):
        read_kinematics_csv(path)


# --- AC4: a reversed run --------------------------------------------------------------------------------


def test_reversed_run_yields_reverse_direction_and_nonnegative_speed():
    raw = reversed_run_raw()
    run = normalise_run(raw["station_m"], raw["time_s"], raw["speed_ms"], raw["accel_ms2"])

    assert run.direction == RunDirection.REVERSE
    assert np.all(run.speed_ms >= 0.0)
    np.testing.assert_array_equal(run.station_m, raw["station_m"])  # source order preserved, not reversed


# --- normalisation rules that need their own test, per the task file --------------------------------------


def test_missing_time_column_is_rejected():
    with pytest.raises(ValueError, match="time"):
        normalise_run(
            station_m=np.array([0.0, 1.0]),
            time_s=None,
            speed_ms=np.array([0.0, 1.0]),
            accel_ms2=np.array([0.0, 0.0]),
        )


def test_empty_gui_time_column_is_rejected(tmp_path):
    path = tmp_path / "no_times.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_GUI_HEADERS["en"])
        writer.writerow(["0.000", "", "0.0", "0.0", "0.0", "0.0", "0.0"])
        writer.writerow(["1.000", "", "10.0", "0.0", "0.0", "0.0", "0.0"])
    with pytest.raises(ValueError, match="no time data"):
        read_kinematics_csv(path)


def test_non_monotonic_time_is_a_warning_plus_stable_sort():
    station_m = np.array([0.0, 10.0, 20.0, 30.0])
    time_s = np.array([0.0, 2.0, 1.0, 3.0])  # rows 1 and 2 swapped
    speed_ms = np.array([0.0, 10.0, 10.0, 10.0])
    accel_ms2 = np.zeros(4)

    run = normalise_run(station_m, time_s, speed_ms, accel_ms2)

    assert len(run.warnings) == 1
    assert "not monotonic" in run.warnings[0]
    assert np.all(np.diff(run.time_s) >= 0.0)
    assert run.time_s[0] == 0.0


def test_missing_force_columns_are_none_not_zero():
    run = normalise_run(
        station_m=np.array([0.0, 10.0]),
        time_s=np.array([0.0, 1.0]),
        speed_ms=np.array([0.0, 10.0]),
        accel_ms2=np.array([0.0, 0.0]),
    )
    assert run.f_traction_kn is None
    assert run.f_braking_kn is None
    assert run.f_resistance_kn is None


def test_speed_is_taken_as_a_magnitude():
    run = normalise_run(
        station_m=np.array([10.0, 0.0]),
        time_s=np.array([0.0, 1.0]),
        speed_ms=np.array([-10.0, -10.0]),
        accel_ms2=np.array([0.0, 0.0]),
    )
    assert np.all(run.speed_ms >= 0.0)
    assert run.direction == RunDirection.REVERSE


def test_dwell_kept_verbatim_when_source_carries_it():
    station_m = np.array([0.0, 100.0, 100.0, 200.0])
    time_s = np.array([0.0, 10.0, 40.0, 50.0])
    speed_ms = np.array([10.0, 0.0, 0.0, 10.0])
    accel_ms2 = np.zeros(4)
    dwell_s = np.array([0.0, 0.0, 30.0, 0.0])

    run = normalise_run(station_m, time_s, speed_ms, accel_ms2, dwell_s=dwell_s)

    assert run.stops == (Stop(station_m=100.0, dwell_s=30.0),)


def test_time_jump_dwell_detection_recovers_a_synthetic_stop():
    # A 1 m/step grid (as COYPU's batch export produces): each normal step takes at most 2 s (the 0.5 m/s
    # creep-speed floor over 1 m); a single 40 s step at the same 1 m spacing is unambiguously a dwell.
    n = 20
    station_m = np.arange(n, dtype=np.float64)
    time_s = np.arange(n, dtype=np.float64) * 1.2
    dwell_index = 10
    time_s[dwell_index:] += 40.0
    speed_ms = np.full(n, 5.0)
    accel_ms2 = np.zeros(n)

    run = normalise_run(station_m, time_s, speed_ms, accel_ms2)

    assert len(run.stops) == 1
    assert run.stops[0].station_m == pytest.approx(float(dwell_index))
    assert run.stops[0].dwell_s == pytest.approx(40.0 - (1.0 / 0.5 - 1.2), abs=1e-6)


def test_dwell_stop_name_matched_from_reference_within_tolerance():
    station_m = np.array([0.0, 100.0, 100.0, 200.0])
    time_s = np.array([0.0, 10.0, 40.0, 50.0])
    speed_ms = np.array([10.0, 0.0, 0.0, 10.0])
    accel_ms2 = np.zeros(4)
    dwell_s = np.array([0.0, 0.0, 30.0, 0.0])
    reference = (Stop(station_m=102.0, dwell_s=30.0, name="Somewhere"),)

    run = normalise_run(station_m, time_s, speed_ms, accel_ms2, dwell_s=dwell_s, stops=reference)

    assert run.stops[0].name == "Somewhere"
