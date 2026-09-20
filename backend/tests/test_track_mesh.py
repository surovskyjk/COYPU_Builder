"""T-120: the backend track mesh baker -- swept rails and ballast, tile-local chunking (ADR 0004).

Domain-level tests exercise `bake_track_mesh` directly at float64 precision where the acceptance criteria
need it (chunk-boundary agreement, rail-head placement, triangle winding); the last group goes over a real
loopback WebSocket (ADR 0003) to prove the `alignment.track_mesh` wire method end to end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION
from coypu_builder.domain.crs import vector_from_godot
from coypu_builder.domain.lrs import frames
from coypu_builder.io.mesh.profiles import RAIL_HEAD_WIDTH_M, ballast_profile, rail_profile
from coypu_builder.io.mesh.sweep import sweep_profile
from coypu_builder.io.mesh.track import bake_track_mesh
from coypu_builder.protocol.messages import Envelope
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame, unpack_blob

FIXTURES = Path(__file__).parent / "fixtures"
KRALUPY_XML = FIXTURES / "kralupy" / "kralupy_neratovice_092.xml"

SURFACES = ("rail_left", "rail_right", "ballast")


def _profiles(gauge_mm: float) -> dict[str, Any]:
    left, right = rail_profile(gauge_mm)
    return {"rail_left": left, "rail_right": right, "ballast": ballast_profile(gauge_mm)}


def _by_surface(chunks, surface: str):
    return sorted((c for c in chunks if c.surface == surface), key=lambda c: c.chunk_index)


# --- ADR 0004: chunk boundaries share their ring, vertices are tile-local ----------------------------------


def test_chunks_share_boundary_ring_with_no_gap(kralupy):
    aln = kralupy.alignment
    chunk_length_m = 3000.0
    chunks = bake_track_mesh(aln, chunk_length_m=chunk_length_m, spacing_m=10.0)
    k_by_surface = {name: p.points.shape[0] for name, p in _profiles(aln.cant.gauge_mm).items()}

    for surface in SURFACES:
        group = _by_surface(chunks, surface)
        k = k_by_surface[surface]
        assert len(group) > 1
        for a, b in zip(group, group[1:], strict=False):
            # bake_track_mesh forces both chunks' shared cut to the identical float64 station value, so the
            # domain-space ring geometry they represent is exactly (not just approximately) the same --
            # this exact-equality check *is* the "agree to 1e-9 m" bound: the difference is exactly zero.
            assert a.station_end == b.station_start

            # The actual wire data (tile-local float32) must still reconstruct to the same world position,
            # within float32's own resolution at this chunk length -- i.e. no visible seam.
            last_ring = vector_from_godot(a.vertices[-k:].astype(np.float64)) + a.tile_origin
            first_ring = vector_from_godot(b.vertices[:k].astype(np.float64)) + b.tile_origin
            assert np.allclose(last_ring, first_ring, atol=1e-3)


def test_vertex_magnitude_is_bounded_by_chunk_length(kralupy):
    """The whole ADR 0004 safeguard: no chunk may carry a vertex far from its own tile origin."""
    aln = kralupy.alignment
    chunk_length_m = 250.0
    chunks = bake_track_mesh(aln, chunk_length_m=chunk_length_m, spacing_m=10.0)
    assert len(chunks) > 0
    for c in chunks:
        magnitude = np.linalg.norm(c.vertices.astype(np.float64), axis=1)
        assert np.max(magnitude) < chunk_length_m, (c.chunk_index, c.surface, float(np.max(magnitude)))


# --- rail head placement -------------------------------------------------------------------------------


def _rail_head_offsets(alignment, station: float) -> dict[str, float]:
    """Offset of each rail's head-top centre from the track-plane centre, along `left`, measured from the
    emitted (float64, pre-cast) sweep vertices directly -- independent of chunking or the float32 wire cast.
    """
    fr = frames(alignment, np.array([station]))
    offsets = {}
    for name, profile in zip(("left", "right"), rail_profile(alignment.cant.gauge_mm), strict=True):
        vertices, _, _ = sweep_profile(profile, fr)
        head_top = vertices[[0, 1]].mean(axis=0)  # profile points 0, 1: the two head-top corners
        offsets[name] = float(np.dot(head_top - fr.origin[0], fr.left[0]))
    return offsets


def test_rail_head_centres_sit_at_half_gauge_plus_half_head_width(kralupy, tram_alignments):
    expected_heavy = (1435.0 / 1000.0 + RAIL_HEAD_WIDTH_M) / 2.0
    for station in (8260.0, 16850.0):  # long straight; sharpest curve on the file (R ~= 300 m)
        offsets = _rail_head_offsets(kralupy.alignment, station)
        assert offsets["left"] == pytest.approx(expected_heavy, abs=1e-9)
        assert offsets["right"] == pytest.approx(-expected_heavy, abs=1e-9)

    loop = next(a for a in tram_alignments.values() if a.name == "terminal_loop")
    expected_tram = (1000.0 / 1000.0 + RAIL_HEAD_WIDTH_M) / 2.0
    offsets = _rail_head_offsets(loop, 100.0)  # inside the loop's full-cant (55 mm) plateau
    assert offsets["left"] == pytest.approx(expected_tram, abs=1e-9)
    assert offsets["right"] == pytest.approx(-expected_tram, abs=1e-9)


def test_gauge_mm_override_changes_rail_spacing(kralupy):
    aln = kralupy.alignment
    default_chunk = next(
        c
        for c in bake_track_mesh(aln, chunk_length_m=6000.0, spacing_m=25.0)
        if c.surface == "rail_left" and c.chunk_index == 0
    )
    narrow_chunk = next(
        c
        for c in bake_track_mesh(aln, chunk_length_m=6000.0, spacing_m=25.0, gauge_mm=1000.0)
        if c.surface == "rail_left" and c.chunk_index == 0
    )
    assert not np.allclose(default_chunk.vertices, narrow_chunk.vertices)


# --- triangle winding and degeneracy -------------------------------------------------------------------


def test_triangles_wind_ccw_outward_with_no_degenerates(kralupy):
    aln = kralupy.alignment
    chunks = bake_track_mesh(aln, chunk_length_m=6000.0, spacing_m=25.0)
    assert len(chunks) > 0
    for c in chunks:
        v = c.vertices.astype(np.float64)
        n = c.normals.astype(np.float64)
        tri = c.indices.reshape(-1, 3)
        e1 = v[tri[:, 1]] - v[tri[:, 0]]
        e2 = v[tri[:, 2]] - v[tri[:, 0]]
        face_normal = np.cross(e1, e2)
        area = 0.5 * np.linalg.norm(face_normal, axis=1)
        assert np.all(area > 1e-12), (c.chunk_index, c.surface, float(area.min()))
        # CCW as seen from outside <=> the face normal (right-hand rule) agrees with the outward vertex
        # normal `sweep_profile` computed for that same corner.
        agreement = np.sum(face_normal * n[tri[:, 0]], axis=1)
        assert np.all(agreement > 0.0), (c.chunk_index, c.surface, float(agreement.min()))


# --- UVs -----------------------------------------------------------------------------------------------


def test_uv_monotone_across_profile_and_v_equals_station_at_chunk_boundary(kralupy):
    aln = kralupy.alignment
    chunks = _by_surface(bake_track_mesh(aln, chunk_length_m=3000.0, spacing_m=25.0), "rail_left")
    k = rail_profile(aln.cant.gauge_mm)[0].points.shape[0]

    first_ring_u = chunks[0].uvs[:k, 0].astype(np.float64)
    assert np.all(np.diff(first_ring_u) > 0.0)

    a, b = chunks[0], chunks[1]
    v_last = a.uvs[-k:, 1]
    v_first = b.uvs[:k, 1]
    assert np.array_equal(v_last, v_first)
    assert np.allclose(v_last.astype(np.float64), a.station_end, atol=1e-2)


# --- ADR 0006: mode-agnostic, including through the tram fixture's junction node --------------------------


def test_tram_fixture_bakes_through_both_junction_alignments(tram_alignments):
    for alignment in tram_alignments.values():
        chunks = bake_track_mesh(alignment, chunk_length_m=100.0, spacing_m=1.0)
        assert len(chunks) > 0
        for c in chunks:
            assert c.vertices.shape[0] > 0
            assert np.all(np.isfinite(c.vertices))
            assert np.all(np.isfinite(c.normals))


# --- wire method -----------------------------------------------------------------------------------------

BLOB_FIELDS = ("vertices", "normals", "uvs", "indices")


async def _call(
    ws, request_id: str, method: str, params: dict[str, Any] | None = None
) -> tuple[Envelope, bytes]:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())


@pytest.fixture
async def server():
    srv = await serve(host="127.0.0.1", port=0)
    try:
        yield srv
    finally:
        srv.close()
        await srv.wait_closed()


@pytest.fixture
def server_url(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"ws://127.0.0.1:{port}"


async def test_alignment_track_mesh_blobs_match_declared_counts(server_url):
    # Track-mesh payloads run far larger than the other methods' (this is the method the task expects a
    # client to page with `chunk_index`, not stream through the default 1 MiB websocket frame limit) --
    # raise it for this test the way a real client requesting several chunks at once would have to.
    async with connect(server_url, max_size=None) as ws:
        await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})
        await _call(ws, "2", "project.new", {})
        imported, _ = await _call(ws, "3", "import.landxml", {"path": str(KRALUPY_XML)})
        assert imported.type == "res", imported.error
        alignment_id = imported.result["alignments"][0]["alignment_id"]

        res, tail = await _call(
            ws,
            "4",
            "alignment.track_mesh",
            {"alignment_id": alignment_id, "chunk_length_m": 4000.0, "spacing_m": 25.0},
        )
        assert res.type == "res", res.error
        chunks_info = res.result["chunks"]
        assert len(chunks_info) > 0
        assert {c["surface"] for c in chunks_info} == set(SURFACES)

        blob_by_name = {ref.name: (ref, unpack_blob(tail, ref)) for ref in res.blobs}
        for info in chunks_info:
            suffix = f"{info['chunk_index']}_{info['surface']}"
            for field, dtype in (
                ("vertices", np.float32),
                ("normals", np.float32),
                ("uvs", np.float32),
                ("indices", np.int32),
            ):
                ref, array = blob_by_name[f"{field}_{suffix}"]
                assert array.dtype == dtype
            vertices = blob_by_name[f"vertices_{suffix}"][1]
            normals = blob_by_name[f"normals_{suffix}"][1]
            uvs = blob_by_name[f"uvs_{suffix}"][1]
            indices = blob_by_name[f"indices_{suffix}"][1]
            assert vertices.shape == (info["vertex_count"], 3)
            assert normals.shape == (info["vertex_count"], 3)
            assert uvs.shape == (info["vertex_count"], 2)
            assert indices.shape == (info["index_count"],)
            assert int(indices.max()) < info["vertex_count"]

        # chunk_index pages the response down to one chunk's entries (one per surface).
        single, _ = await _call(
            ws,
            "5",
            "alignment.track_mesh",
            {"alignment_id": alignment_id, "chunk_length_m": 4000.0, "chunk_index": 0},
        )
        assert single.type == "res", single.error
        assert {c["chunk_index"] for c in single.result["chunks"]} == {0}
        assert len(single.result["chunks"]) == len(SURFACES)
        assert len(single.blobs) == len(SURFACES) * len(BLOB_FIELDS)


async def test_alignment_track_mesh_error_paths(server_url):
    async with connect(server_url) as ws:
        await _call(ws, "1", "session.hello", {"client": "pytest", "client_version": "0"})
        await _call(ws, "2", "project.new", {})
        imported, _ = await _call(ws, "3", "import.landxml", {"path": str(KRALUPY_XML)})
        alignment_id = imported.result["alignments"][0]["alignment_id"]

        missing, _ = await _call(ws, "4", "alignment.track_mesh", {"alignment_id": "no-such-id"})
        assert missing.type == "err"
        assert missing.error.code == "E_NOT_FOUND"

        bad_chunk_length, _ = await _call(
            ws, "5", "alignment.track_mesh", {"alignment_id": alignment_id, "chunk_length_m": 0.0}
        )
        assert bad_chunk_length.type == "err"
        assert bad_chunk_length.error.code == "E_BAD_PARAMS"

        bad_spacing, _ = await _call(
            ws, "6", "alignment.track_mesh", {"alignment_id": alignment_id, "spacing_m": -1.0}
        )
        assert bad_spacing.type == "err"
        assert bad_spacing.error.code == "E_BAD_PARAMS"

        bad_index, _ = await _call(
            ws, "7", "alignment.track_mesh", {"alignment_id": alignment_id, "chunk_index": 9999}
        )
        assert bad_index.type == "err"
        assert bad_index.error.code == "E_NOT_FOUND"
