"""Baking alignments into dense, render-ready frame tables for the client."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.geometry.cant import RotationPivot
from coypu_builder.domain.lrs import TrackFrames, frames


@dataclass(frozen=True)
class FrameTable(TrackFrames):
    segment_index: np.ndarray  # (n,) int32 horizontal segment index per row


def bake_stations(
    alignment: Alignment, spacing_m: float = 1.0, max_chord_error_m: float | None = 0.002
) -> np.ndarray:
    """Uniform stations plus every geometric key station (element boundaries, BVC/EVC, cant stations).

    With `max_chord_error_m`, curved elements are refined so that a chord between neighbouring samples
    deviates from the arc by at most that much (spacing = sqrt(8·e·R)).
    """
    h = alignment.horizontal
    s0, s1 = float(h.stations[0]), float(h.stations[-1])
    pieces = [
        np.arange(s0, s1, spacing_m),
        h.stations,
        alignment.vertical.key_stations(),
        alignment.cant.key_stations(),
    ]
    if max_chord_error_m:
        for seg, a, b in zip(h.segments, h.stations[:-1], h.stations[1:], strict=True):
            kmax = max(abs(seg.curvature_start), abs(seg.curvature_end))
            if kmax <= 0:
                continue
            fine = np.sqrt(8.0 * max_chord_error_m / kmax)
            if fine < spacing_m:
                pieces.append(np.arange(a, b, fine))
    stations = np.unique(np.concatenate(pieces + [np.array([s1])]))
    stations = stations[(stations >= s0) & (stations <= s1)]
    # Collapse near-duplicates produced by float noise between grid and key stations.
    keep = np.concatenate([[True], np.diff(stations) > 1e-9])
    return stations[keep]


def bake_frame_table(
    alignment: Alignment,
    spacing_m: float = 1.0,
    pivot: RotationPivot | None = None,
    max_chord_error_m: float | None = 0.002,
) -> FrameTable:
    stations = bake_stations(alignment, spacing_m, max_chord_error_m)
    fr = frames(alignment, stations, pivot)
    seg = alignment.horizontal.segment_index(stations).astype(np.int32)
    return FrameTable(
        fr.station,
        fr.origin,
        fr.tangent,
        fr.left,
        fr.up,
        fr.heading,
        fr.roll,
        fr.pitch,
        fr.cant_mm,
        fr.curvature,
        fr.gradient,
        fr.elevation,
        seg,
    )
