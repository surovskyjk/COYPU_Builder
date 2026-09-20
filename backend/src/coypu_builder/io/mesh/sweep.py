"""Profile x frames -> an indexed triangle mesh: sweeping a closed 2-D cross-section along a moving track
frame (`domain/lrs.py: TrackFrames`). Positions come straight from `frames.origin`, `frames.left` and
`frames.up` -- this module never touches the horizontal/vertical/cant math itself, only the sweep.
"""

from __future__ import annotations

import numpy as np

from coypu_builder.domain.lrs import TrackFrames
from coypu_builder.io.mesh.profiles import Profile


def _edge_normals(points: np.ndarray) -> np.ndarray:
    """Unit outward 2-D normal per edge `j -> j+1` (wrapping). The sign is picked from the polygon's own
    winding (shoelace signed area), so `Profile.points` never has to be pre-oriented by its author."""
    nxt = np.roll(points, -1, axis=0)
    d = nxt - points
    signed_area = 0.5 * float(np.sum(points[:, 0] * nxt[:, 1] - nxt[:, 0] * points[:, 1]))
    sign = 1.0 if signed_area >= 0.0 else -1.0
    n = sign * np.column_stack([d[:, 1], -d[:, 0]])
    length = np.hypot(n[:, 0], n[:, 1])
    return n / np.where(length < 1e-15, 1.0, length)[:, None]


def _vertex_normals(points: np.ndarray) -> np.ndarray:
    """Per-vertex outward normal: the two adjacent edge normals, averaged and renormalised."""
    edge_n = _edge_normals(points)
    prev_n = np.roll(edge_n, 1, axis=0)
    v = edge_n + prev_n
    length = np.hypot(v[:, 0], v[:, 1])
    return v / np.where(length < 1e-15, 1.0, length)[:, None]


def _perimeter_u(points: np.ndarray) -> np.ndarray:
    d = np.diff(points, axis=0)
    seg = np.hypot(d[:, 0], d[:, 1])
    return np.concatenate([[0.0], np.cumsum(seg)])


def sweep_profile(profile: Profile, frames: TrackFrames) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """`profile.points` (k, 2) swept along every row of `frames` (n rows) -> `(vertices, normals, uvs)`,
    each float64 and row-major with the profile point index varying fastest: row `i*k + j` is frame `i`,
    profile point `j`. `vertices`/`normals` are absolute domain `(E, N, H)`; `normals` are unit vectors, the
    profile's own outward normal rotated into `(left, up)` (no tangential component -- an accepted
    simplification for a swept cross-section, see track-mesh.md). `uvs[:, 0]` is accumulated perimeter
    distance around the profile from point 0 (monotone, independent of `frames`); `uvs[:, 1]` is the row's
    absolute station, so it matches exactly at a shared chunk boundary and a repeating texture never seams.
    """
    k = profile.points.shape[0]
    n = len(frames)
    y = profile.points[:, 0]
    z = profile.points[:, 1]
    vertices = (
        frames.origin[:, None, :]
        + y[None, :, None] * frames.left[:, None, :]
        + z[None, :, None] * frames.up[:, None, :]
    ).reshape(n * k, 3)

    local_normals = _vertex_normals(profile.points)
    ny, nz = local_normals[:, 0], local_normals[:, 1]
    normals = (
        ny[None, :, None] * frames.left[:, None, :] + nz[None, :, None] * frames.up[:, None, :]
    ).reshape(n * k, 3)

    u = np.tile(_perimeter_u(profile.points), n)
    v = np.repeat(frames.station, k)
    uvs = np.column_stack([u, v])

    return vertices, normals, uvs


def tube_indices(n_stations: int, k: int) -> np.ndarray:
    """Flat `(m,)` int32 triangle indices for `n_stations` swept rings of `k` closed profile points: for
    ring `i` and profile point `j`, quad corners `a=(i,j)`, `b=(i,j+1)`, `c=(i+1,j)`, `d=(i+1,j+1)` split
    into triangles `(a, b, c)` and `(b, d, c)`. This winding is counter-clockwise as seen from outside, and
    matches `sweep_profile`'s outward normals, **only when `profile.points` itself runs
    counter-clockwise** (`y` as the x-axis, `z` as the y-axis) -- unlike the normals, which are correct for
    either winding, this fixed triangle order is not self-correcting, so every `Profile` in `profiles.py`
    is deliberately authored counter-clockwise.
    """
    i = np.arange(n_stations - 1)
    j = np.arange(k)
    jn = (j + 1) % k
    ii, jj = np.meshgrid(i, j, indexing="ij")
    _, jjn = np.meshgrid(i, jn, indexing="ij")
    a = (ii * k + jj).ravel()
    b = (ii * k + jjn).ravel()
    c = ((ii + 1) * k + jj).ravel()
    d = ((ii + 1) * k + jjn).ravel()
    tri1 = np.column_stack([a, b, c])
    tri2 = np.column_stack([b, d, c])
    return np.concatenate([tri1, tri2], axis=0).astype(np.int32).reshape(-1)
