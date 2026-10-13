"""T-132: the terrain analysis surfaces -- TIN from points or as delivered, grid, stack, sheet seams.

Every input is generated here from T-130's synthetic surface; nothing reads real data. The TIN accuracy test
derives its bound from the surface's curvature (see `hessian_norm_bound`).
"""

from __future__ import annotations

from pathlib import Path

import laspy
import numpy as np
import pytest
import shapely
from scipy.interpolate import LinearNDInterpolator, RegularGridInterpolator
from scipy.spatial import Delaunay

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.domain.terrain import TerrainGrid, TerrainPoints, TerrainTin
from coypu_builder.domain.terrain_surface import (
    DEFAULT_MAX_EDGE_M,
    GridSurface,
    TerrainStack,
    TinSurface,
    _TriangleLocator,
    surface_for_source,
    tin_from_points,
)
from coypu_builder.io.sources.runner import run_import
from coypu_builder.io.terrain import register_builtin_readers
from fixtures.synthetic import terrain as synthetic
from fixtures.synthetic.terrain import (
    points_in_bbox,
    sample_irregular,
    split_sheets,
    surface,
)

KROVAK = ProjectCRS("EPSG:5514")
HILL_BOX = (-747000.0, -1025400.0, -746400.0, -1024600.0)  # holds the hill and one end of the ridge


@pytest.fixture(autouse=True)
def readers():
    register_builtin_readers()


def _rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def _uniform(box, count: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = _rng(seed)
    return rng.uniform(box[0], box[2], count), rng.uniform(box[1], box[3], count)


def _longest_edge(tin: TinSurface) -> float:
    xy = tin.points[:, :2]
    a, b, c = (xy[tin.triangles[:, k]] for k in range(3))
    return float(np.sqrt(max(((p - q) ** 2).sum(axis=1).max() for p, q in ((a, b), (b, c), (c, a)))))


# --- criterion 1: TIN accuracy ---------------------------------------------------------------------------


def hessian_norm_bound() -> float:
    """An upper bound on the spectral norm of the Hessian of `surface` anywhere.

    The plane has none. A 2-D Gaussian bump A exp(-r^2 / 2 s^2) has Hessian eigenvalues
    (A/s^2) exp(-r^2/2s^2) (r^2/s^2 - 1) radially and -(A/s^2) exp(-r^2/2s^2) tangentially, never above A/s^2
    in magnitude. The ridge is the same bump in the distance to a segment, with the same bound A/w^2.
    """
    return (
        synthetic.HILL_AMPLITUDE / synthetic.HILL_SIGMA**2
        + synthetic.RIDGE_AMPLITUDE / synthetic.RIDGE_WIDTH**2
    )


def test_tin_error_stays_within_the_linear_interpolation_bound():
    """For a triangle with vertices v_i and a point p = sum(l_i v_i), Taylor's theorem about p gives
    I f(p) - f(p) = 1/2 sum l_i (v_i - p)^T H(xi_i) (v_i - p), because sum l_i (v_i - p) = 0 removes the
    gradient. Also sum l_i |v_i - p|^2 = sum_{i<j} l_i l_j |v_i - v_j|^2 <= h^2 / 3 for l summing to 1, so
    |error| <= h^2 M / 6 with h the longest edge of the triangle the point lies in and M the Hessian bound
    above. The bound is checked per query against its own triangle, not against the longest edge of the
    whole TIN, so an error of a few centimetres in an ordinary 3 m triangle fails."""
    sample = sample_irregular(HILL_BOX, 3.4, seed=11)
    tin = tin_from_points(sample)
    h_max = _longest_edge(tin)

    margin = 2.0 * h_max
    interior = (HILL_BOX[0] + margin, HILL_BOX[1] + margin, HILL_BOX[2] - margin, HILL_BOX[3] - margin)
    e, n = _uniform(interior, 20000, seed=3)
    got = tin.heights(e, n)
    assert not np.isnan(got).any()
    error = np.abs(got - surface(e, n))

    # each query's own triangle, found by an independent Delaunay of the same points
    centre = np.array([np.mean(HILL_BOX[::2]), np.mean(HILL_BOX[1::2])])
    reference = Delaunay(tin.points[:, :2] - centre)
    simplex = reference.find_simplex(np.column_stack([e, n]) - centre)
    assert (simplex >= 0).all()
    corners = reference.points[reference.simplices[simplex]]  # (q, 3, 2)
    edges = np.stack(
        [corners[:, 0] - corners[:, 1], corners[:, 1] - corners[:, 2], corners[:, 2] - corners[:, 0]]
    )
    h_own = np.linalg.norm(edges, axis=2).max(axis=0)
    bound = h_own**2 * hessian_norm_bound() / 6.0
    assert (error <= bound).all()
    # the bound is meaningful, not vacuous, at DMR 5G density: a centimetre for a typical triangle, and a
    # 5 cm error in the stored heights breaks it for most queries
    assert np.median(bound) < 0.02
    shifted = TinSurface(tin.points + np.array([0.0, 0.0, 0.05]), tin.triangles)
    assert (np.abs(shifted.heights(e, n) - surface(e, n)) > bound).mean() > 0.9
    assert error.max() < h_max**2 * hessian_norm_bound() / 6.0 / 3.0


def test_the_default_edge_limit_is_30_m_and_can_be_overridden():
    assert DEFAULT_MAX_EDGE_M == 30.0
    sample = sample_irregular(HILL_BOX, 3.4, seed=12)
    default = tin_from_points(sample)
    assert _longest_edge(default) <= 30.0 and default.dropped_by_clip == 0
    tight = tin_from_points(sample, max_edge_m=6.0)
    assert _longest_edge(tight) <= 6.0
    assert tight.dropped_triangles > default.dropped_triangles
    assert len(tin_from_points(sample, max_edge_m=100.0).triangles) >= len(default.triangles)


def test_a_tin_agrees_with_an_independent_delaunay_interpolator_wherever_it_has_data():
    sample = sample_irregular(HILL_BOX, 6.0, seed=5)
    tin = tin_from_points(sample, max_edge_m=1e6)  # nothing dropped: the whole convex hull
    assert tin.dropped_triangles == 0
    centre = np.array([np.mean(HILL_BOX[::2]), np.mean(HILL_BOX[1::2])])
    reference = LinearNDInterpolator(sample[:, :2] - centre, sample[:, 2])
    e, n = _uniform(HILL_BOX, 50000, seed=6)
    got = tin.heights(e, n)
    want = reference(np.column_stack([e, n]) - centre)
    covered = ~np.isnan(want)
    assert covered.sum() > 40000
    assert np.abs(got[covered] - want[covered]).max() < 1e-9
    # a point just inside the reference's hull but outside ours (or the reverse) can only sit on the boundary
    assert (np.isnan(got) & covered).sum() <= 5


def test_a_tin_keeps_the_shape_of_its_query_and_answers_nan_for_nan():
    sample = sample_irregular(HILL_BOX, 8.0, seed=2)
    tin = tin_from_points(sample)
    e = np.full((3, 4), np.mean(HILL_BOX[::2]))
    n = np.full((3, 4), np.mean(HILL_BOX[1::2]))
    n[1, 2] = np.nan
    e[2, 3] = np.inf
    got = tin.heights(e, n)
    assert got.shape == (3, 4) and got.dtype == np.float64
    assert np.isnan(got[1, 2]) and np.isnan(got[2, 3]) and np.isfinite(got[0, 0])
    assert tin.heights(e[0, 0], n[0, 0]).shape == ()
    assert tin.heights(np.empty(0), np.empty(0)).shape == (0,)


def test_a_vertex_returns_its_own_height_and_duplicates_are_averaged():
    sample = sample_irregular(HILL_BOX, 10.0, seed=4)
    with_duplicate = np.vstack([sample, sample[7] + np.array([0.0, 0.0, 2.0])])
    tin = tin_from_points(with_duplicate)
    assert len(tin.points) == len(sample)
    probe = tin.points[::50]
    assert np.abs(tin.heights(probe[:, 0], probe[:, 1]) - probe[:, 2]).max() < 1e-9
    row = np.nonzero((tin.points[:, 0] == sample[7, 0]) & (tin.points[:, 1] == sample[7, 1]))[0][0]
    assert tin.points[row, 2] == pytest.approx(sample[7, 2] + 1.0, abs=1e-12)


def test_the_triangulation_does_not_depend_on_the_order_of_the_points():
    sample = sample_irregular(HILL_BOX, 8.0, seed=9)
    shuffled = sample[_rng(1).permutation(len(sample))]
    a, b = tin_from_points(sample), tin_from_points(shuffled)
    assert np.array_equal(a.points, b.points) and np.array_equal(a.triangles, b.triangles)


def test_a_tin_cannot_be_built_from_too_few_or_collinear_points():
    with pytest.raises(ValueError, match="at least 3"):
        tin_from_points(np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]))
    line = np.column_stack([np.arange(10.0), np.arange(10.0) * 2.0, np.zeros(10)])
    with pytest.raises(ValueError, match="collinear"):
        tin_from_points(line)
    with pytest.raises(ValueError, match="max_edge_m"):
        tin_from_points(sample_irregular(HILL_BOX, 50.0, seed=1), max_edge_m=-1.0)
    with pytest.raises(ValueError, match="too sparse"):
        tin_from_points(sample_irregular(HILL_BOX, 50.0, seed=1), max_edge_m=0.5)


def test_tin_triangles_are_counter_clockwise_and_every_point_is_used():
    tin = tin_from_points(sample_irregular(HILL_BOX, 5.0, seed=8))
    xy = tin.points[:, :2]
    a, b, c = (xy[tin.triangles[:, k]] for k in range(3))
    area2 = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    assert (area2 > 0.0).all()
    assert np.unique(tin.triangles).size == len(tin.points)


def test_a_tin_as_delivered_keeps_its_triangles():
    """Two triangles of a unit square split along the anti-diagonal, which Delaunay would not choose for a
    skewed height field: the surface must follow the given diagonal, not a re-triangulation."""
    points = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 10.0], [10.0, 10.0, 0.0], [0.0, 10.0, 10.0]])
    triangles = np.array([[0, 1, 3], [1, 2, 3]], dtype=np.int32)  # diagonal from (10,0) to (0,10)
    surf = surface_for_source([TerrainTin(points, triangles)])
    assert isinstance(surf, TinSurface)
    # the given diagonal joins (10, 0) and (0, 10), both at 10; the other diagonal would give 0 at (5, 5)
    assert surf.heights(np.array([5.0]), np.array([5.0]))[0] == pytest.approx(10.0)
    assert surf.heights(np.array([2.0]), np.array([2.0]))[0] == pytest.approx(
        4.0
    )  # z = x + y in the first triangle
    assert np.isnan(surf.heights(np.array([11.0, -0.1]), np.array([5.0, 5.0]))).all()
    assert surf.bounds() == (0.0, 0.0, 10.0, 10.0)


def test_point_location_scales_to_a_million_queries_without_a_python_loop_over_points():
    sample = sample_irregular(HILL_BOX, 6.0, seed=13)
    tin = tin_from_points(sample)
    e, n = _uniform(HILL_BOX, 1_000_000, seed=14)
    got = tin.heights(e, n)
    assert np.isfinite(got).mean() > 0.95


def test_a_delivered_tin_with_long_hull_slivers_is_located_correctly_with_bounded_bins():
    """A corridor TIN as a surveyor delivers it can have hull triangles thousands of metres long. Binning each
    triangle's bounding box into every cell it covers would make millions of entries; the locator moves such a
    triangle to a coarser grid instead, so every triangle has at most MAX_CELLS_PER_TRIANGLE entries."""
    rng = _rng(7)
    core = np.column_stack([rng.uniform(0.0, 300.0, 3000), rng.uniform(0.0, 40.0, 3000)])
    far = np.array([[-6000.0, -900.0], [6000.0, 800.0], [-5000.0, 1500.0], [5200.0, -1400.0]])
    xy = np.vstack([core, far])
    z = 100.0 + 0.01 * xy[:, 0] + 0.02 * xy[:, 1]
    centre = xy.mean(axis=0)
    simplices = Delaunay(xy - centre).simplices
    tin = surface_for_source([TerrainTin(np.column_stack([xy, z]), simplices.astype(np.int32))])
    assert isinstance(tin, TinSurface)

    a, b, c = (xy[simplices[:, k]] for k in range(3))
    extent = np.maximum.reduce([np.abs(p - q).max(axis=1) for p, q in ((a, b), (b, c), (c, a))])
    assert extent.max() > 5000.0 and np.median(extent) < 20.0  # slivers next to small triangles

    locator = tin._locator
    assert len(locator.levels) > 1
    assert locator.entry_count <= locator.MAX_CELLS_PER_TRIANGLE * len(simplices)
    assert locator.entry_count < 5 * len(simplices)  # in practice far fewer than the cap

    reference = LinearNDInterpolator(xy - centre, z)
    qe = np.concatenate([rng.uniform(0.0, 300.0, 4000), rng.uniform(-5000.0, 5000.0, 4000)])
    qn = np.concatenate([rng.uniform(0.0, 40.0, 4000), rng.uniform(-1200.0, 1200.0, 4000)])
    want = reference(np.column_stack([qe, qn]) - centre)
    got = tin.heights(qe, qn)
    covered = ~np.isnan(want)
    assert covered.sum() > 6000
    assert np.abs(got[covered] - want[covered]).max() < 1e-9
    assert (np.isnan(got) & covered).sum() <= 2 and (np.isfinite(got) & ~covered).sum() <= 2


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_tin_with_a_non_finite_vertex_is_refused_and_the_locator_never_loops(bad):
    points = np.array([[0.0, 0.0, 1.0], [10.0, 0.0, 2.0], [0.0, 10.0, 3.0], [10.0, 10.0, 4.0]])
    triangles = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int32)
    for column in (0, 1, 2):
        broken = points.copy()
        broken[3, column] = bad
        with pytest.raises(ValueError, match="finite"):
            TerrainTin(broken, triangles)
        with pytest.raises(ValueError, match="finite"):
            TinSurface(broken, triangles)
    # the locator's own guard, for a caller that bypasses both checks: it stops, it does not spin
    broken = points.copy()
    broken[3, :2] = bad
    with np.errstate(all="ignore"), pytest.raises(ValueError, match="cannot be binned"):
        _TriangleLocator(broken, triangles)


def test_non_finite_points_of_a_cloud_are_dropped_before_triangulating():
    sample = sample_irregular(HILL_BOX, 8.0, seed=2)
    dirty = np.vstack([sample, [[np.nan, 1.0, 2.0], [np.inf, 1.0, 2.0], [1.0, 2.0, np.nan]]])
    clean = tin_from_points(sample)
    again = tin_from_points(dirty)
    assert np.array_equal(clean.points, again.points) and np.array_equal(clean.triangles, again.triangles)


# --- criterion 2: no triangle bridges a gap --------------------------------------------------------------


def _u_shaped_clip(gap_m: float = 60.0) -> shapely.Polygon:
    """A concave corridor: two 40 m arms joined by a 40 m base, with a `gap_m` wide gap between the arms."""
    e0, n0 = -747000.0, -1025400.0
    right = e0 + 40.0 + gap_m
    return shapely.Polygon(
        [
            (e0, n0),
            (right + 40.0, n0),
            (right + 40.0, n0 + 400.0),
            (right, n0 + 400.0),
            (right, n0 + 40.0),
            (e0 + 40.0, n0 + 40.0),
            (e0 + 40.0, n0 + 400.0),
            (e0, n0 + 400.0),
        ]
    )


def _inside(polygon):
    return lambda e, n: shapely.intersects_xy(polygon, e, n)


def _cloud_in(clip, spacing: float, seed: int) -> np.ndarray:
    sample = sample_irregular(clip.bounds, spacing, seed=seed)
    return sample[shapely.contains_xy(clip, sample[:, 0], sample[:, 1])]


def test_no_triangle_bridges_the_gap_of_a_concave_clip():
    clip = _u_shaped_clip()
    spacing = 3.0
    cloud = _cloud_in(clip, spacing, seed=21)
    tin = tin_from_points(cloud, inside=_inside(clip))
    h = _longest_edge(tin)
    assert tin.dropped_by_clip > 0 and tin.dropped_triangles >= tin.dropped_by_clip

    hull = shapely.convex_hull(shapely.MultiPoint(cloud[:, :2]))
    e, n = _uniform(clip.bounds, 60000, seed=22)
    heights = tin.heights(e, n)
    in_hull = shapely.contains_xy(hull, e, n)
    distance = shapely.distance(clip, shapely.points(e, n))

    # a kept triangle has its vertices in the clip and edges of at most h, so it reaches at most h beyond it
    outside = in_hull & (distance > h)
    assert outside.sum() > 1000
    assert np.isnan(heights[outside]).all()
    # the clip rule goes further than that bound: the gap is empty down to a few spacings from the arms
    gap = in_hull & (distance > 2.0 * spacing)
    assert gap.sum() > 1000 and np.isnan(heights[gap]).all()

    depth = shapely.distance(clip.boundary, shapely.points(e, n))
    deep = shapely.contains_xy(clip, e, n) & (depth > 2.0 * spacing)
    assert deep.sum() > 1000
    assert np.isfinite(heights[deep]).all()


def test_a_60_m_gap_stays_empty_under_the_30_m_edge_default_without_any_clip():
    clip = _u_shaped_clip(gap_m=60.0)
    cloud = _cloud_in(clip, 3.0, seed=23)
    tin = tin_from_points(cloud)  # no clip rule: the edge limit alone
    assert tin.dropped_by_clip == 0 and tin.dropped_triangles > 0
    centre = clip.bounds[0] + 40.0 + 30.0  # 30 m from each arm
    n = np.linspace(clip.bounds[1] + 100.0, clip.bounds[3] - 10.0, 200)
    for e in (centre - 1.0, centre, centre + 1.0):
        assert np.isnan(tin.heights(np.full_like(n, e), n)).all()
    # a triangle reaching the centre would need a vertex on each arm, so an edge of at least 60 m
    assert _longest_edge(tin) <= DEFAULT_MAX_EDGE_M


def test_the_clip_rule_alone_stops_a_short_edged_bridge_across_a_concave_fold():
    clip = _u_shaped_clip(gap_m=20.0)  # narrower than any edge limit that matters
    spacing = 3.0
    cloud = _cloud_in(clip, spacing, seed=24)
    e, n = _uniform(clip.bounds, 60000, seed=25)
    middle = clip.bounds[0] + 40.0 + 10.0
    probe_n = np.linspace(clip.bounds[1] + 100.0, clip.bounds[3] - 10.0, 200)

    edge_only = tin_from_points(cloud, max_edge_m=1e6)
    assert np.isfinite(edge_only.heights(np.full_like(probe_n, middle), probe_n)).all()  # bridged

    clip_only = tin_from_points(cloud, max_edge_m=1e6, inside=_inside(clip))
    assert clip_only.dropped_by_clip > 0 and clip_only.dropped_triangles == clip_only.dropped_by_clip
    assert np.isnan(clip_only.heights(np.full_like(probe_n, middle), probe_n)).all()
    distance = shapely.distance(clip, shapely.points(e, n))
    in_hull = shapely.contains_xy(shapely.convex_hull(shapely.MultiPoint(cloud[:, :2])), e, n)
    # The centroid test is not an exact point-in-clip test: a stray triangle across the open end of the fold
    # can keep a few points within three spacings of the clip. Nothing farther, and almost nothing nearer.
    beyond = in_hull & (distance > spacing)
    kept = beyond & np.isfinite(clip_only.heights(e, n))
    assert beyond.sum() > 2000 and kept.sum() <= 0.002 * beyond.sum()
    assert distance[kept].max() < 3.0 * spacing


def test_a_triangle_with_short_edges_and_its_centroid_outside_the_clip_is_dropped():
    """A 10 m square with a slot cut into its top edge (x 4-6, y 5-10). The triangle (0,10) (10,10) (5,4) has
    edges of 10 m and 8 m, well inside any limit, but its centroid (5, 8) lies in the slot."""
    clip = shapely.Polygon([(0, 0), (10, 0), (10, 10), (6, 10), (6, 5), (4, 5), (4, 10), (0, 10)])
    points = np.array([[0, 10, 5.0], [10, 10, 5.0], [5, 4, 1.0], [0, 0, 1.0], [10, 0, 1.0]]) + [
        1000.0,
        2000.0,
        0,
    ]
    shifted = shapely.affinity.translate(clip, 1000.0, 2000.0)
    probe_e, probe_n = np.array([1005.0]), np.array([2007.5])

    bridged = tin_from_points(points, max_edge_m=100.0)
    assert bridged.dropped_triangles == 0 and np.isfinite(bridged.heights(probe_e, probe_n)).all()

    cut = tin_from_points(points, max_edge_m=100.0, inside=_inside(shifted))
    assert cut.dropped_by_clip == cut.dropped_triangles == 1
    assert np.isnan(cut.heights(probe_e, probe_n)).all()
    assert np.isfinite(cut.heights(np.array([1005.0]), np.array([2002.0]))).all()  # the rest is intact


# --- criterion 3: sheet seams ----------------------------------------------------------------------------


def _write_las(path: Path, points: np.ndarray) -> Path:
    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = [1e-6] * 3
    header.offsets = [float(np.round(points[:, 0].mean())), float(np.round(points[:, 1].mean())), 150.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = points[:, 0], points[:, 1], points[:, 2]
    las.write(path)
    return path


def _payloads(paths) -> list[TerrainPoints]:
    outcome = run_import(paths, SourceKind.TERRAIN, ImportSettings(horizontal_crs="EPSG:5514"), KROVAK, {})
    return [outcome.payloads[record.id] for record in outcome.dataset.objects]


def test_a_2x2_sheet_split_has_no_seam(tmp_path):
    bbox = (-747650.0, -1026200.0, -746850.0, -1025500.0)
    sample = sample_irregular(bbox, 5.0, seed=31)
    boxes = split_sheets(bbox, 2, 2)
    sheets = [points_in_bbox(sample, box, upper_open=True) for box in boxes]
    whole = _write_las(tmp_path / "whole.las", np.vstack(sheets))
    parts = [_write_las(tmp_path / f"S{i}.las", s) for i, s in enumerate(sheets)]

    split = surface_for_source(_payloads(parts))
    unsplit = surface_for_source(_payloads([whole]))

    mid_e, mid_n = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
    along_n = np.linspace(bbox[1] + 10.0, bbox[3] - 10.0, 4000)
    along_e = np.linspace(bbox[0] + 10.0, bbox[2] - 10.0, 4000)
    seam_e = np.concatenate([np.full_like(along_n, boxes[0][2]), along_e])
    seam_n = np.concatenate([along_n, np.full_like(along_e, boxes[0][3])])
    a, b = split.heights(seam_e, seam_n), unsplit.heights(seam_e, seam_n)
    assert np.isfinite(a).all() and np.isfinite(b).all()
    assert np.abs(a - b).max() <= 1e-9
    assert mid_e == pytest.approx(boxes[0][2]) and mid_n == pytest.approx(boxes[0][3])

    # sheet by sheet the result would have a hole or a kink along the seam: it does not
    gap_e = np.full(2000, boxes[0][2])
    assert np.isfinite(split.heights(gap_e, along_n[:2000])).all()


# --- criterion 4: grid -----------------------------------------------------------------------------------


def _grid(rows=8, cols=10, cell=20.0, origin_e=-746600.0, origin_n=-1024700.0, seed=0) -> TerrainGrid:
    rng = _rng(seed)
    return TerrainGrid(origin_e, origin_n, cell, cell, rng.uniform(100.0, 200.0, (rows, cols)))


def test_a_grid_is_exact_at_cell_centres_and_bilinear_between_them():
    grid = _grid()
    surf = GridSurface(grid)
    centres_e, centres_n = grid.centres()
    ee, nn = np.meshgrid(centres_e, centres_n)
    assert np.array_equal(surf.heights(ee, nn), grid.heights)

    reference = RegularGridInterpolator(
        (centres_n[::-1], centres_e), grid.heights[::-1], method="linear", bounds_error=True
    )
    e, n = _uniform((centres_e[0], centres_n[-1], centres_e[-1], centres_n[0]), 5000, seed=2)
    assert np.abs(surf.heights(e, n) - reference(np.column_stack([n, e]))).max() < 1e-9


def test_a_grid_is_nan_outside_and_uses_the_nearest_centre_in_the_outer_half_cell():
    grid = _grid()
    surf = GridSurface(grid)
    min_e, min_n, max_e, max_n = surf.bounds()
    assert (min_e, max_n) == (grid.origin_e, grid.origin_n)
    assert np.isnan(
        surf.heights(
            np.array([min_e - 0.01, max_e + 0.01, 0.5 * (min_e + max_e)]),
            np.array([max_n - 5.0, max_n - 5.0, max_n + 0.01]),
        )
    ).all()
    corner = surf.heights(np.array([min_e + 1.0]), np.array([max_n - 1.0]))[0]
    assert corner == grid.heights[0, 0]
    assert surf.heights(np.array([max_e]), np.array([min_n]))[0] == grid.heights[-1, -1]


def test_nodata_propagates_from_any_corner_of_the_four_cells():
    grid = _grid()
    heights = grid.heights.copy()
    heights[3, 4] = np.nan
    surf = GridSurface(TerrainGrid(grid.origin_e, grid.origin_n, grid.cell_e, grid.cell_n, heights))
    centres_e, centres_n = grid.centres()
    # a point in each of the four quadrants around the nodata centre has it as a corner
    for de, dn in ((-5.0, -5.0), (5.0, -5.0), (-5.0, 5.0), (5.0, 5.0)):
        assert np.isnan(surf.heights(np.array([centres_e[4] + de]), np.array([centres_n[3] + dn]))[0])
    # two cells away is clean
    assert np.isfinite(surf.heights(np.array([centres_e[6] + 5.0]), np.array([centres_n[3] + 5.0]))[0])
    assert np.isnan(surf.heights(np.array([centres_e[4]]), np.array([centres_n[3]]))[0])


def test_aligned_grids_of_one_cell_size_are_one_mosaic_without_a_seam():
    whole = _grid(rows=6, cols=12, seed=3)
    west = TerrainGrid(whole.origin_e, whole.origin_n, 20.0, 20.0, whole.heights[:, :6].copy())
    east = TerrainGrid(whole.origin_e + 6 * 20.0, whole.origin_n, 20.0, 20.0, whole.heights[:, 6:].copy())
    merged = surface_for_source([east, west])  # order must not matter without overlap
    assert isinstance(merged, GridSurface)
    assert np.array_equal(merged.grid.heights, whole.heights)
    e, n = _uniform(whole.bounds, 5000, seed=4)
    assert np.abs(merged.heights(e, n) - GridSurface(whole).heights(e, n)).max() == 0.0


def test_overlapping_grid_cells_take_the_first_sheet_and_gaps_stay_nodata():
    a = TerrainGrid(0.0, 100.0, 10.0, 10.0, np.full((4, 4), 1.0))
    b = TerrainGrid(20.0, 100.0, 10.0, 10.0, np.full((4, 4), 2.0))
    c = TerrainGrid(100.0, 100.0, 10.0, 10.0, np.full((2, 2), 3.0))
    merged = surface_for_source([a, b, c])
    assert isinstance(merged, GridSurface)
    row0 = merged.grid.heights[0]
    assert list(row0[:4]) == [1.0, 1.0, 1.0, 1.0] and row0[4] == 2.0 and row0[5] == 2.0
    assert np.isnan(row0[6]) and row0[10] == 3.0


def test_grids_off_each_others_lattice_or_far_apart_are_stacked_not_resampled():
    a = TerrainGrid(0.0, 100.0, 10.0, 10.0, np.full((4, 4), 1.0))
    shifted = TerrainGrid(3.0, 100.0, 10.0, 10.0, np.full((4, 4), 2.0))
    coarse = TerrainGrid(0.0, 100.0, 25.0, 25.0, np.full((2, 2), 3.0))
    far = TerrainGrid(1.0e6, 100.0, 10.0, 10.0, np.full((4, 4), 4.0))
    for other in (shifted, coarse, far):
        stacked = surface_for_source([a, other])
        assert isinstance(stacked, TerrainStack) and len(stacked.surfaces) == 2
        assert stacked.surfaces[0].grid is a and stacked.surfaces[1].grid is other


def test_mixed_kinds_stack_in_order_of_first_appearance():
    grid = _grid()
    points = sample_irregular(HILL_BOX, 8.0, seed=3)
    cloud = TerrainPoints(points, None)
    stack = surface_for_source([grid, cloud, TerrainPoints(points[:5].copy(), None)])
    assert isinstance(stack, TerrainStack)
    assert isinstance(stack.surfaces[0], GridSurface) and isinstance(stack.surfaces[1], TinSurface)
    assert len(stack.surfaces[1].points) == len(points)
    with pytest.raises(ValueError):
        surface_for_source([])


# --- criterion 5: stack ----------------------------------------------------------------------------------


def test_in_a_stack_the_first_surface_wins_and_the_second_fills_what_the_first_lacks():
    first = GridSurface(TerrainGrid(0.0, 100.0, 10.0, 10.0, np.full((10, 5), 1.0)))  # E 0-50, N 0-100
    second = GridSurface(TerrainGrid(30.0, 100.0, 10.0, 10.0, np.full((10, 7), 2.0)))  # E 30-100
    stack = TerrainStack((first, second))
    e = np.array([10.0, 45.0, 70.0, 150.0, np.nan])
    n = np.array([50.0, 50.0, 50.0, 50.0, 50.0])
    got = stack.heights(e, n)
    assert got[:3].tolist() == [1.0, 1.0, 2.0]
    assert np.isnan(got[3:]).all()
    assert TerrainStack((second, first)).heights(e, n)[:3].tolist() == [1.0, 2.0, 2.0]
    assert stack.bounds() == (0.0, 0.0, 100.0, 100.0)
    assert np.isnan(TerrainStack(()).heights(e, n)).all()
    with pytest.raises(ValueError):
        TerrainStack(()).bounds()


def test_a_stack_holes_in_the_first_surface_fall_through():
    holed = _grid(seed=5).heights.copy()
    holed[2:4, 2:4] = np.nan
    first = GridSurface(TerrainGrid(-746600.0, -1024700.0, 20.0, 20.0, holed))
    second = GridSurface(TerrainGrid(-746600.0, -1024700.0, 20.0, 20.0, np.full(holed.shape, 7.0)))
    stack = TerrainStack((first, second))
    e = np.array([-746600.0 + 2.5 * 20.0 + 3.0, -746600.0 + 8.5 * 20.0])
    n = np.array([-1024700.0 - 2.5 * 20.0 - 3.0, -1024700.0 - 6.5 * 20.0])
    assert stack.heights(e, n).tolist() == [7.0, holed[6, 8]]
