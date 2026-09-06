import numpy as np

from coypu_builder.domain.geometry import Alignment, CircularArc, HorizontalAlignment, Line
from coypu_builder.domain.sampling import bake_frame_table, bake_stations


def _alignment():
    line = Line(0.0, 0.0, 0.0, 95.5)
    arc = CircularArc(*line.end, 0.0, 300.0, 1 / 300.0)
    tail = Line(*arc.end, arc.heading_end, 50.0)
    return Alignment.flat(HorizontalAlignment((line, arc, tail), station_start=10.0), elevation=5.0)


def test_bake_stations_contains_boundaries_and_respects_chord_error():
    aln = _alignment()
    st = bake_stations(aln, spacing_m=10.0, max_chord_error_m=0.002)
    assert st[0] == 10.0 and st[-1] == aln.station_end
    assert {10.0, 105.5, 405.5}.issubset(set(st.tolist()))
    inside = st[(st >= 105.5) & (st <= 405.5)]
    assert np.max(np.diff(inside)) <= np.sqrt(8 * 0.002 * 300) + 1e-9
    outside = st[st < 105.5]
    assert np.max(np.diff(outside)) <= 10.0 + 1e-9
    coarse = bake_stations(aln, spacing_m=10.0, max_chord_error_m=None)
    assert len(coarse) < len(st)


def test_frame_table_shape_and_segment_index():
    table = bake_frame_table(_alignment(), spacing_m=1.0)
    n = len(table)
    for arr in (table.origin, table.tangent, table.left, table.up):
        assert arr.shape == (n, 3)
    assert table.segment_index.tolist()[0] == 0 and table.segment_index.tolist()[-1] == 2
    assert np.all(table.elevation == 5.0)
