import numpy as np
import pytest

from coypu_builder.__main__ import main
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.geometry import RotationPivot
from coypu_builder.io.landxml import detect_dialect, read_landxml, to_alignment

COYPU_STYLE = b"""<?xml version="1.0" encoding="utf-8"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <Units><Metric linearUnit="meter"/></Units>
  <CoordinateSystem name="UTM 33N" desc="EPSG:32633"/>
  <Application name="COYPU" version="2.0"/>
  <Alignments name="A">
    <Alignment name="A" length="257.0796" staStart="100.0000">
      <CoordGeom name="A">
        <Line staStart="100.0000" length="100.0000">
          <Start>5500000.0000 450000.0000</Start>
          <End>5500100.0000 450000.0000</End>
        </Line>
        <Curve staStart="200.0000" length="157.0796" radius="100.0000" rot="cw" crvType="arc">
          <Start>5500100.0000 450000.0000</Start>
          <Center>5500100.0000 450100.0000</Center>
          <End>5500200.0000 450100.0000</End>
        </Curve>
      </CoordGeom>
      <Profile name="A">
        <ProfAlign name="A">
          <PVI>100.0000 300.0000</PVI>
          <CircCurve length="50.0000" radius="5000.0000">200.0000 301.0000</CircCurve>
          <PVI>357.0796 301.0000</PVI>
        </ProfAlign>
      </Profile>
      <Cant name="A" gauge="1435" rotationPoint="insideRail" equilibriumConstant="11.8" speed="120.0">
        <CantStation station="100.0000" appliedCant="0.0" speed="120.0"/>
        <CantStation station="200.0000" appliedCant="120.0" speed="120.0"/>
        <CantStation station="357.0796" appliedCant="120.0" speed="120.0"/>
      </Cant>
    </Alignment>
  </Alignments>
</LandXML>
"""


def test_detect_dialects():
    assert detect_dialect("COYPU Feeder").name == "coypu_feeder"
    assert detect_dialect("COYPU").name == "coypu"
    assert detect_dialect("Rail", "4.2").name == "rail_export"
    assert detect_dialect("Civil 3D").name == "generic"


def test_feeder_file_metadata(kralupy_raw):
    assert kralupy_raw.dialect.name == "coypu_feeder"
    assert kralupy_raw.epsg == 5514
    assert kralupy_raw.name == "Track 1"
    assert kralupy_raw.sta_start == 0.0
    assert len(kralupy_raw.elements) == 65
    assert kralupy_raw.cant is not None and kralupy_raw.cant.gauge == pytest.approx(1.435)
    assert [item.kind for item in kralupy_raw.profile[:3]] == ["PVI", "PVI", "ParaCurve"]


def test_coypu_dialect_with_utm_northing_easting_tokens():
    raws = read_landxml(COYPU_STYLE)
    raw = raws[0]
    assert raw.dialect.name == "coypu"
    assert raw.epsg == 32633
    result = to_alignment(raw, ProjectCRS("EPSG:32633"))
    aln = result.alignment
    line, arc = aln.horizontal.segments
    assert line.start.tolist() == [450000.0, 5500000.0]
    assert line.heading_start == pytest.approx(np.pi / 2)
    assert line.length == 100.0
    assert arc.kind == "arc" and arc.curvature == pytest.approx(-0.01)
    assert arc.length == pytest.approx(157.0796)
    assert result.report.max_end_deviation_m < 1e-3
    assert aln.cant.gauge_mm == 1435.0
    assert aln.cant.pivot is RotationPivot.LOW_RAIL
    assert aln.cant.cant([150.0, 300.0]).tolist() == pytest.approx([60.0, 120.0])
    assert [c.kind for c in aln.vertical.curves] == ["circular"]
    assert aln.vertical.elevation(200.0)[0] < 301.0


def test_inspect_cli_runs(kralupy_xml, capsys):
    assert main(["inspect", str(kralupy_xml), "--spacing", "500"]) == 0
    out = capsys.readouterr().out
    assert "alignment 'Track 1': 65 elements" in out
    assert "dialect: coypu_feeder" in out
