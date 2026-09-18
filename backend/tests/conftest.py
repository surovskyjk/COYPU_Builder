from pathlib import Path

import pytest

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.io.coypu import read_coypu
from coypu_builder.io.landxml import read_landxml, to_alignment
from fixtures.synthetic.tram_loop import build_tram_loop

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def kralupy_xml() -> Path:
    return FIXTURES / "kralupy" / "kralupy_neratovice_092.xml"


@pytest.fixture(scope="session")
def kralupy_coypu() -> Path:
    return FIXTURES / "kralupy" / "kralupy_neratovice_092.coypu"


@pytest.fixture(scope="session")
def kralupy_stops_csv() -> Path:
    return FIXTURES / "kralupy" / "kralupy_neratovice_092_stops.csv"


@pytest.fixture(scope="session")
def krovak() -> ProjectCRS:
    return ProjectCRS("EPSG:5514")


@pytest.fixture(scope="session")
def kralupy_raw(kralupy_xml):
    raws = read_landxml(kralupy_xml)
    assert len(raws) == 1
    return raws[0]


@pytest.fixture(scope="session")
def kralupy(kralupy_raw, krovak):
    return to_alignment(kralupy_raw, krovak)


@pytest.fixture(scope="session")
def kralupy_project(kralupy_coypu):
    return read_coypu(kralupy_coypu)


@pytest.fixture(scope="session")
def tram_fixture():
    return build_tram_loop()


@pytest.fixture(scope="session")
def tram_network(tram_fixture):
    return tram_fixture[0]


@pytest.fixture(scope="session")
def tram_alignments(tram_fixture):
    return tram_fixture[1]
