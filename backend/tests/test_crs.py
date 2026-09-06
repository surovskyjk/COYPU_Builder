import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from coypu_builder.domain.crs import (
    BasePoint,
    ProjectCRS,
    basis_to_godot,
    from_local,
    points_to_godot,
    quaternion_from_matrix,
    to_local,
    vector_from_godot,
    vector_to_godot,
)
from coypu_builder.io.landxml.dialects import resolve_coordinates


def test_project_crs_properties(krovak):
    assert krovak.epsg == 5514
    assert krovak.is_krovak
    assert krovak.is_projected
    assert krovak.is_metric
    utm = ProjectCRS(32633)
    assert not utm.is_krovak
    assert utm.epsg == 32633
    assert ProjectCRS("EPSG:4326").is_metric is False
    assert krovak == ProjectCRS(5514)


def test_krovak_positive_tokens_map_to_negative_east_north(krovak):
    en = resolve_coordinates(np.array([[1024754.350001, 732995.803073]]), krovak)
    assert en[0, 0] == pytest.approx(-732995.803073)
    assert en[0, 1] == pytest.approx(-1024754.350001)
    lon, lat = krovak.to_wgs84(en[:, 0], en[:, 1])
    # First point of the Kralupy dense polyline as projected by COYPU (pyproj) — same start point.
    assert lat[0] == pytest.approx(50.262078911289784, abs=1e-7)
    assert lon[0] == pytest.approx(14.52270140232449, abs=1e-7)


def test_krovak_negative_tokens_are_true_epsg_5514(krovak):
    en = resolve_coordinates(np.array([[-1024754.35, -732995.80]]), krovak)
    assert en[0, 0] == pytest.approx(-732995.80)
    assert en[0, 1] == pytest.approx(-1024754.35)


def test_default_order_is_northing_easting_for_other_crs():
    utm = ProjectCRS(32633)
    en = resolve_coordinates(np.array([[5500100.0, 450010.0]]), utm)
    assert en[0].tolist() == [450010.0, 5500100.0]
    en = resolve_coordinates(np.array([[450010.0, 5500100.0]]), utm, coordinate_order="EN")
    assert en[0].tolist() == [450010.0, 5500100.0]


def test_local_and_godot_axis_mapping():
    base = BasePoint(-733000.0, -1024800.0, 160.0)
    pts = np.array([[-732990.0, -1024780.0, 165.0]])
    local = to_local(pts, base)
    assert local.tolist() == [[10.0, 20.0, 5.0]]
    assert from_local(local, base).tolist() == pts.tolist()
    g = points_to_godot(pts, base)
    assert g.dtype == np.float32
    assert g.tolist() == [[10.0, 5.0, -20.0]]
    north = vector_to_godot(np.array([0.0, 1.0, 0.0]), dtype=np.float64)
    assert north.tolist() == [0.0, 0.0, -1.0]
    assert vector_from_godot(north).tolist() == [0.0, 1.0, 0.0]
    assert BasePoint.rounded(-732995.8, -1024754.35, 165.58) == BasePoint(-733000.0, -1024800.0, 200.0)


def test_basis_to_godot_is_right_handed_and_forward_is_tangent():
    tangent = np.array([[1.0, 0.0, 0.0]])
    left = np.array([[0.0, 1.0, 0.0]])
    up = np.array([[0.0, 0.0, 1.0]])
    m = basis_to_godot(tangent, left, up)[0]
    assert np.linalg.det(m) == pytest.approx(1.0)
    forward = m @ np.array([0.0, 0.0, -1.0])
    assert forward.tolist() == pytest.approx(vector_to_godot(tangent[0], dtype=np.float64).tolist())
    assert (m @ np.array([0.0, 1.0, 0.0])).tolist() == pytest.approx([0.0, 1.0, 0.0])


def test_quaternion_from_matrix_matches_scipy_up_to_sign():
    rng = np.random.default_rng(7)
    rots = Rotation.random(200, random_state=rng)
    mats = rots.as_matrix()
    ours = quaternion_from_matrix(mats)
    ref = rots.as_quat()  # scalar-last (x, y, z, w) — Godot order
    dots = np.abs((ours * ref).sum(axis=1))
    assert np.allclose(dots, 1.0, atol=1e-9)
    assert quaternion_from_matrix(np.eye(3)).tolist() == pytest.approx([0.0, 0.0, 0.0, 1.0])
