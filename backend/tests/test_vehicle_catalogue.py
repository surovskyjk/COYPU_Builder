import json

import pytest

from coypu_builder.domain.model import Mode, Trainset
from coypu_builder.io.catalogue.vehicles import (
    CATALOGUE_SCHEMA,
    VehicleSpecError,
    load_catalogue,
    load_vehicle_spec,
    resolve,
)


@pytest.fixture(scope="session")
def catalogue():
    return load_catalogue()


def test_default_catalogue_loads_with_no_argument(catalogue):
    assert not catalogue.errors
    assert {"dmu_br650_cd840", "generic_bemu", "tram_generic"} <= catalogue.keys()


def test_dmu_spec_round_trips_its_declared_fields(catalogue):
    spec = catalogue["dmu_br650_cd840"]
    assert spec.key == "dmu_br650_cd840"
    assert spec.name == "DMU BR 650 (CD 840)"
    assert spec.mode is Mode.HEAVY_RAIL
    assert spec.gauge_mm == 1435
    assert spec.aliases == ("dmu_br650_cd840", "Stadler Regio-Shuttle RS1")
    assert spec.coupling_gap_m == 0.0
    assert spec.dynamics is None
    assert len(spec.cars) == 1
    car = spec.cars[0]
    assert car.name == "RS1 railcar"
    assert car.length_m == 25.5
    assert car.width_m == 2.93
    assert car.height_m == 3.73
    assert car.floor_height_m == 0.6
    assert car.bogie_pivot_distance_m == 17.5
    assert car.bogie_wheelbase_m == 1.8
    assert car.wheel_diameter_m == 0.77
    assert car.mesh is None
    assert car.color == "#c8102e"
    assert spec.length_m == pytest.approx(25.5)


def test_generic_bemu_spec_round_trips_and_sums_two_cars(catalogue):
    spec = catalogue["generic_bemu"]
    assert spec.mode is Mode.HEAVY_RAIL
    assert len(spec.cars) == 2
    assert spec.length_m == pytest.approx(52.9)


def test_tram_generic_loads_and_length_matches_hand_computed_total(catalogue):
    spec = catalogue["tram_generic"]
    assert spec.mode is Mode.LIGHT_RAIL_TRAM
    assert spec.gauge_mm == 1000
    assert len(spec.cars) in (2, 3)
    provenance = spec.provenance.lower()
    named_entities = ("siemens", "stadler", "alstom", "skoda", "škoda", "caf", "bombardier", "dpp", "dpo")
    assert not any(entity in provenance for entity in named_entities)

    expected = sum(car.length_m for car in spec.cars) + spec.coupling_gap_m * (len(spec.cars) - 1)
    assert spec.length_m == pytest.approx(expected)


def test_malformed_files_are_reported_and_do_not_block_the_rest(tmp_path):
    good = {
        "schema": CATALOGUE_SCHEMA,
        "name": "Good Vehicle",
        "mode": "heavy_rail",
        "gauge_mm": 1435,
        "cars": [
            {
                "name": "Car A",
                "length_m": 20.0,
                "width_m": 3.0,
                "height_m": 4.0,
                "floor_height_m": 0.6,
                "bogie_pivot_distance_m": 14.0,
                "bogie_wheelbase_m": 2.0,
                "wheel_diameter_m": 0.8,
            }
        ],
    }
    bad_schema = {**good, "name": "Bad Schema", "schema": "not-a-real-schema/1"}
    negative_length = {
        **good,
        "name": "Negative Length",
        "cars": [{**good["cars"][0], "length_m": -5.0}],
    }
    pivot_exceeds_length = {
        **good,
        "name": "Pivot Exceeds Length",
        "cars": [{**good["cars"][0], "bogie_pivot_distance_m": 25.0}],
    }

    (tmp_path / "good.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "bad_schema.json").write_text(json.dumps(bad_schema), encoding="utf-8")
    (tmp_path / "negative_length.json").write_text(json.dumps(negative_length), encoding="utf-8")
    (tmp_path / "pivot_exceeds_length.json").write_text(json.dumps(pivot_exceeds_length), encoding="utf-8")

    catalogue = load_catalogue(tmp_path)

    assert set(catalogue.keys()) == {"good"}
    assert len(catalogue.errors) == 3

    messages = "\n".join(str(err) for err in catalogue.errors)
    assert str(tmp_path / "bad_schema.json") in messages
    assert "schema" in messages
    assert str(tmp_path / "negative_length.json") in messages
    assert "length_m" in messages
    assert str(tmp_path / "pivot_exceeds_length.json") in messages
    assert "bogie_pivot_distance_m" in messages

    for err in catalogue.errors:
        assert isinstance(err, VehicleSpecError)


def test_load_vehicle_spec_raises_with_file_and_field_for_a_single_malformed_file(tmp_path):
    path = tmp_path / "unknown_mode.json"
    unknown_mode = {
        "schema": CATALOGUE_SCHEMA,
        "name": "X",
        "mode": "spaceship",
        "gauge_mm": 1435,
        "cars": [
            {
                "name": "c",
                "length_m": 1,
                "width_m": 1,
                "height_m": 1,
                "floor_height_m": 1,
                "bogie_pivot_distance_m": 0.5,
                "bogie_wheelbase_m": 1,
                "wheel_diameter_m": 1,
            }
        ],
    }
    path.write_text(json.dumps(unknown_mode), encoding="utf-8")
    with pytest.raises(VehicleSpecError) as excinfo:
        load_vehicle_spec(path)
    assert str(path) in str(excinfo.value)
    assert "mode" in str(excinfo.value)


def test_resolve_matches_by_key_alias_and_name_case_and_whitespace_insensitively(catalogue):
    spec = catalogue["dmu_br650_cd840"]

    assert resolve(catalogue, "dmu_br650_cd840") is spec  # key
    assert resolve(catalogue, "Stadler Regio-Shuttle RS1") is spec  # alias, exact
    assert resolve(catalogue, "  stadler   regio-shuttle rs1  ") is spec  # alias, case + whitespace
    assert resolve(catalogue, "dmu br 650 (cd 840)".upper()) is spec  # name, case-insensitive
    assert resolve(catalogue, "  DMU   BR 650 (CD 840)  ") is spec  # name, whitespace-insensitive
    assert resolve(catalogue, "does-not-exist") is None


def test_trainset_from_spec_repeats_units_with_the_same_coupling_gap(catalogue):
    spec = catalogue["tram_generic"]
    trainset = Trainset.from_spec(spec, units=2)

    assert len(trainset.cars) == 2 * len(spec.cars)
    assert trainset.cars == spec.cars * 2
    assert trainset.length_m == pytest.approx(2 * spec.length_m + spec.coupling_gap_m)
    assert trainset.mode is spec.mode
    assert trainset.gauge_mm == spec.gauge_mm
    assert trainset.name == spec.name


def test_trainset_from_spec_single_unit_matches_spec_length(catalogue):
    spec = catalogue["generic_bemu"]
    trainset = Trainset.from_spec(spec, name="Custom name")

    assert len(trainset.cars) == len(spec.cars)
    assert trainset.length_m == pytest.approx(spec.length_m)
    assert trainset.name == "Custom name"
