"""`bake_track_mesh`: an alignment -> tile-local track-mesh chunks (ADR 0004, ADR 0001).

Two swept rails plus one default ballast prism, cut into `chunk_length_m`-long chunks that share their
boundary ring so neighbours meet with no seam. Every chunk's vertices are stored relative to its own
`tile_origin` -- never in absolute project coordinates -- which is the whole point of this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.crs import vector_to_godot
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.geometry.cant import RotationPivot
from coypu_builder.domain.lrs import frames
from coypu_builder.domain.sampling import bake_stations
from coypu_builder.io.mesh.profiles import Profile, ballast_profile, rail_profile
from coypu_builder.io.mesh.sweep import sweep_profile, tube_indices

_DUPLICATE_STATION_EPS_M = 1e-9


@dataclass(frozen=True)
class MeshChunk:
    chunk_index: int
    station_start: float
    station_end: float
    tile_origin: np.ndarray  # (3,) (E, N, H) — the chunk's local origin in the project CRS
    vertices: np.ndarray  # (n, 3) float32, RELATIVE TO tile_origin, in Godot axes
    normals: np.ndarray  # (n, 3) float32
    uvs: np.ndarray  # (n, 2) float32
    indices: np.ndarray  # (m,) int32, triangles, counter-clockwise when seen from outside
    surface: str  # "rail_left" | "rail_right" | "ballast"


def _chunk_boundaries(alignment: Alignment, chunk_length_m: float) -> np.ndarray:
    s0, s1 = alignment.station_start, alignment.station_end
    n_chunks = max(1, int(np.ceil((s1 - s0) / chunk_length_m)))
    boundaries = s0 + np.arange(n_chunks + 1, dtype=np.float64) * chunk_length_m
    boundaries[-1] = s1
    return np.clip(boundaries, s0, s1)


def _stations_with_boundaries(
    alignment: Alignment, spacing_m: float, max_chord_error_m: float | None, boundaries: np.ndarray
) -> np.ndarray:
    base = bake_stations(alignment, spacing_m, max_chord_error_m)
    merged = np.union1d(base, boundaries)
    keep = np.concatenate([[True], np.diff(merged) > _DUPLICATE_STATION_EPS_M])
    return merged[keep]


def bake_track_mesh(
    alignment: Alignment,
    *,
    chunk_length_m: float = 250.0,
    spacing_m: float = 1.0,
    max_chord_error_m: float = 0.002,
    gauge_mm: float | None = None,
    pivot: RotationPivot | None = None,
) -> tuple[MeshChunk, ...]:
    gauge_mm = gauge_mm if gauge_mm is not None else alignment.cant.gauge_mm
    pivot = pivot or alignment.cant.pivot

    boundaries = _chunk_boundaries(alignment, chunk_length_m)
    stations = _stations_with_boundaries(alignment, spacing_m, max_chord_error_m, boundaries)

    left_rail, right_rail = rail_profile(gauge_mm)
    surfaces: tuple[tuple[str, Profile], ...] = (
        ("rail_left", left_rail),
        ("rail_right", right_rail),
        ("ballast", ballast_profile(gauge_mm)),
    )

    chunks: list[MeshChunk] = []
    for chunk_index in range(len(boundaries) - 1):
        s_lo, s_hi = float(boundaries[chunk_index]), float(boundaries[chunk_index + 1])
        mask = (stations >= s_lo - _DUPLICATE_STATION_EPS_M) & (stations <= s_hi + _DUPLICATE_STATION_EPS_M)
        chunk_stations = stations[mask].copy()
        # Force bit-identical endpoints so this chunk's last ring and the next chunk's first ring are
        # computed from the exact same station value, regardless of which near-duplicate the merge kept.
        chunk_stations[0] = s_lo
        chunk_stations[-1] = s_hi

        chunk_frames = frames(alignment, chunk_stations, pivot)
        mid_origin = frames(alignment, np.array([(s_lo + s_hi) / 2.0]), pivot).origin[0]
        tile_origin = np.round(mid_origin)

        for surface, profile in surfaces:
            vertices, normals, uvs = sweep_profile(profile, chunk_frames)
            local = vertices - tile_origin
            chunks.append(
                MeshChunk(
                    chunk_index=chunk_index,
                    station_start=s_lo,
                    station_end=s_hi,
                    tile_origin=tile_origin,
                    vertices=vector_to_godot(local, dtype=np.float32),
                    normals=vector_to_godot(normals, dtype=np.float32),
                    uvs=uvs.astype(np.float32),
                    indices=tube_indices(len(chunk_frames), profile.points.shape[0]),
                    surface=surface,
                )
            )
    return tuple(chunks)
