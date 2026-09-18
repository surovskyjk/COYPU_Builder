import pytest

from coypu_builder.io.catalogue.vehicles import load_catalogue, resolve
from coypu_builder.io.coypu.vehicles import dynamics_from_coypu, merge_dynamics, read_vehicle_csv

_FULL_CSV = """Section,Col1,Col2,Col3,Col4,Col5,Col6
Meta,vehicleName,Test Vehicle
Meta,maxSpeedKmh,120
Meta,massTonnes,48.5
Meta,lengthM,25.5
Meta,brakeDecelMs2,0.45
Meta,maxTractiveForceKN,68.0
Meta,rotMassFactor,1.04
Param,Test Vehicle,1.04,48.5,25.5
Res,Test Vehicle,1.631,0,0.000601
Trac,Test Vehicle,0,21.3,68,0,0
Trac,Test Vehicle,21.3,37.8,111.8909091,-2.060606061,0
"""

_PARTIAL_CSV = """Section,Col1,Col2,Col3,Col4,Col5,Col6
Meta,vehicleName,Test Vehicle
Meta,maxSpeedKmh,120
Meta,massTonnes,48.5
Meta,lengthM,25.5
Meta,brakeDecelMs2,0.45
Meta,maxTractiveForceKN,68.0
Meta,rotMassFactor,1.04
Param,Test Vehicle,1.04,48.5,25.5
"""


def test_read_vehicle_csv_with_all_four_sections_parses_complete_dynamics(tmp_path):
    path = tmp_path / "full.csv"
    path.write_text(_FULL_CSV, encoding="utf-8")

    name, dynamics = read_vehicle_csv(path)

    assert name == "Test Vehicle"
    assert dynamics.mass_t == pytest.approx(48.5)
    assert dynamics.rotating_mass_factor == pytest.approx(1.04)
    assert dynamics.max_speed_ms == pytest.approx(120.0 / 3.6)
    assert dynamics.brake_decel_ms2 == pytest.approx(0.45)
    assert dynamics.max_tractive_force_kn == pytest.approx(68.0)
    assert dynamics.davis_a == pytest.approx(1.631)
    assert dynamics.davis_b == pytest.approx(0.0)
    assert dynamics.davis_c == pytest.approx(0.000601)

    assert len(dynamics.traction_bands) == 2
    band0, band1 = dynamics.traction_bands
    assert band0.v_bottom_ms == pytest.approx(0.0)
    assert band0.v_top_ms == pytest.approx(21.3 / 3.6)
    assert band0.b0 == pytest.approx(68.0)
    assert band0.b1 == pytest.approx(0.0)

    # F(v) must be numerically identical whether evaluated in the source's km/h basis or the stored m/s one.
    v_kmh = 30.0
    f_kmh = 111.8909091 + (-2.060606061) * v_kmh
    v_ms = v_kmh / 3.6
    f_ms = band1.b0 + band1.b1 * v_ms + band1.b2 * v_ms**2
    assert f_ms == pytest.approx(f_kmh)


def test_read_vehicle_csv_missing_res_and_trac_leaves_them_empty_not_zeroed(tmp_path):
    path = tmp_path / "partial.csv"
    path.write_text(_PARTIAL_CSV, encoding="utf-8")

    name, dynamics = read_vehicle_csv(path)

    assert name == "Test Vehicle"
    assert dynamics.mass_t == pytest.approx(48.5)
    assert dynamics.max_speed_ms == pytest.approx(120.0 / 3.6)
    assert dynamics.max_tractive_force_kn == pytest.approx(68.0)
    assert dynamics.davis_a is None
    assert dynamics.davis_b is None
    assert dynamics.davis_c is None
    assert dynamics.traction_bands == ()


def test_kralupy_vehicle_0_dynamics_match_trainparam_and_convert_units(kralupy_project):
    raw = kralupy_project.settings["vehicles"][0]
    train_param = raw["trainParam"][0]

    name, dynamics = dynamics_from_coypu(kralupy_project, 0)

    assert name == train_param[0] == "DMU BR 650 (CD 840)"
    assert dynamics.rotating_mass_factor == pytest.approx(train_param[1])
    assert dynamics.mass_t == pytest.approx(train_param[2])
    assert train_param[3] == pytest.approx(25.5)  # length_m: not part of VehicleDynamics, cross-checked here

    assert dynamics.max_speed_ms == pytest.approx(raw["trainMaxSpeed"] / 3.6)
    assert dynamics.brake_decel_ms2 == pytest.approx(raw["trainBrakeDecel"])

    # The archive's settings never state a maxTractiveForceKN (that is a CSV Meta-only field).
    assert dynamics.max_tractive_force_kn is None

    res = raw["trainRes"][0]
    assert dynamics.davis_a == pytest.approx(res[1])
    assert dynamics.davis_b == pytest.approx(res[2])
    assert dynamics.davis_c == pytest.approx(res[3])

    assert len(dynamics.traction_bands) == len(raw["trainTrac"])
    for band, source_row in zip(dynamics.traction_bands, raw["trainTrac"], strict=True):
        _, v_bottom_kmh, v_top_kmh, b0, b1, b2 = source_row
        assert band.v_bottom_ms == pytest.approx(v_bottom_kmh / 3.6)
        assert band.v_top_ms == pytest.approx(v_top_kmh / 3.6)
        v_mid_kmh = (v_bottom_kmh + v_top_kmh) / 2.0
        f_kmh = b0 + b1 * v_mid_kmh + b2 * v_mid_kmh**2
        v_mid_ms = v_mid_kmh / 3.6
        f_ms = band.b0 + band.b1 * v_mid_ms + band.b2 * v_mid_ms**2
        assert f_ms == pytest.approx(f_kmh)


def test_kralupy_vehicle_0_matches_a_catalogue_entry_by_name(kralupy_project):
    catalogue = load_catalogue()
    name, dynamics = dynamics_from_coypu(kralupy_project, 0)

    spec = resolve(catalogue, name)

    assert spec is not None
    assert spec.key == "dmu_br650_cd840"

    merged = merge_dynamics(spec, dynamics)
    assert merged.dynamics is dynamics
    assert merged.key == spec.key  # merge_dynamics only touches `dynamics`, geometry is untouched


def test_dynamics_from_coypu_reports_cleanly_when_index_has_no_vehicle(kralupy_project):
    assert dynamics_from_coypu(kralupy_project, 99) is None


def test_unmatched_coypu_vehicle_name_reports_cleanly_via_resolve(kralupy_project):
    catalogue = load_catalogue()
    assert resolve(catalogue, "a name COYPU would never produce") is None
