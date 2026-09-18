"""Synthetic tram network — a street section feeding a closed terminal loop — built purely in code.

Exists to exercise ADR 0006's non-heavy-rail requirement: 1000 mm gauge with a matching superelevation
base, a degree-3 `TURNOUT` where the loop diverges from the street, and a `BUFFER_STOP` at the street's
dead end. Geometric realism is not the goal; the loop closes on itself (line, then a full-circle arc,
then the line retraced in reverse) purely so its two ends can share one node.

Coordinates are arbitrary local metres in a nominal EPSG:5514-style projected CRS — not real positions.
"""

from __future__ import annotations

import numpy as np

from coypu_builder.domain.geometry import (
    Alignment,
    CantProfile,
    CircularArc,
    HorizontalAlignment,
    Line,
    VerticalAlignment,
)
from coypu_builder.domain.model.ids import EntityId, new_id
from coypu_builder.domain.model.modes import Mode
from coypu_builder.domain.model.network import Junction, JunctionKind, Link, Network, Node

GAUGE_MM = 1000.0
SUPERELEVATION_BASE_MM = 1100.0
"""Rail-head centre distance for 1000 mm gauge; a plausible synthetic value, not a cited standard."""

ELEVATION_M = 200.0
LOOP_RADIUS_M = 30.0


def _point(xy: np.ndarray) -> tuple[float, float, float]:
    return float(xy[0]), float(xy[1]), ELEVATION_M


def build_tram_loop() -> tuple[Network, dict[EntityId, Alignment]]:
    """Four nodes, four links: `terminus` (BUFFER_STOP) — `street_mid` — `loop_entry` (TURNOUT, degree 3)
    — `loop_far_side`, the last two joined back to `loop_entry` by the two halves of the terminal loop.
    """
    line1 = Line(0.0, 0.0, 0.0, 60.0)
    arc = CircularArc(*line1.end, line1.heading_end, 40.0, 1.0 / 120.0)
    line2 = Line(*arc.end, arc.heading_end, 50.0)
    street_horizontal = HorizontalAlignment((line1, arc, line2))
    street = Alignment(
        street_horizontal,
        VerticalAlignment.constant(ELEVATION_M, street_horizontal.stations[0], street_horizontal.station_end),
        CantProfile(
            np.array([0.0, 60.0, 70.0, 90.0, 100.0, 150.0]),
            np.array([0.0, 0.0, 40.0, 40.0, 0.0, 0.0]),
            gauge_mm=GAUGE_MM,
            superelevation_base_mm=SUPERELEVATION_BASE_MM,
        ),
        name="street",
        mode=Mode.LIGHT_RAIL_TRAM,
    )

    circumference = 2.0 * np.pi * LOOP_RADIUS_M
    arc_start_s, arc_end_s = 20.0, 20.0 + circumference
    loop_in = Line(*line2.end, line2.heading_end, 20.0)
    loop_arc = CircularArc(*loop_in.end, loop_in.heading_end, circumference, 1.0 / LOOP_RADIUS_M)
    loop_out = Line(*loop_arc.end, loop_arc.heading_end + np.pi, 20.0)
    loop_horizontal = HorizontalAlignment((loop_in, loop_arc, loop_out))
    loop = Alignment(
        loop_horizontal,
        VerticalAlignment.constant(ELEVATION_M, loop_horizontal.stations[0], loop_horizontal.station_end),
        CantProfile(
            np.array([0.0, arc_start_s, arc_start_s + 10.0, arc_end_s - 10.0, arc_end_s, arc_end_s + 20.0]),
            np.array([0.0, 0.0, 55.0, 55.0, 0.0, 0.0]),
            gauge_mm=GAUGE_MM,
            superelevation_base_mm=SUPERELEVATION_BASE_MM,
        ),
        name="terminal_loop",
        mode=Mode.LIGHT_RAIL_TRAM,
    )

    street_id, loop_id = new_id(), new_id()

    node_terminus = Node(
        new_id(), (0.0, 0.0, ELEVATION_M), junction=Junction(JunctionKind.BUFFER_STOP), name="terminus"
    )
    node_mid = Node(new_id(), _point(arc.end), name="street_mid")
    node_entry = Node(
        new_id(),
        _point(line2.end),
        junction=Junction(JunctionKind.TURNOUT, {"hand": "left", "radius_m": LOOP_RADIUS_M}),
        name="loop_entry",
    )
    far_side_station = (arc_start_s + arc_end_s) / 2.0
    node_far_side = Node(new_id(), _point(loop_horizontal.point([far_side_station])[0]), name="loop_far_side")

    loop_end_s = loop_horizontal.station_end
    links = (
        Link(new_id(), street_id, node_terminus.id, node_mid.id, 0.0, 100.0, name="street_in"),
        Link(new_id(), street_id, node_mid.id, node_entry.id, 100.0, 150.0, name="street_out"),
        Link(new_id(), loop_id, node_entry.id, node_far_side.id, 0.0, far_side_station, name="loop_out"),
        Link(
            new_id(), loop_id, node_far_side.id, node_entry.id, far_side_station, loop_end_s, name="loop_back"
        ),
    )

    network = Network(
        id=new_id(),
        mode=Mode.LIGHT_RAIL_TRAM,
        nodes=(node_terminus, node_mid, node_entry, node_far_side),
        links=links,
        name="tram_loop",
    )
    return network, {street_id: street, loop_id: loop}
