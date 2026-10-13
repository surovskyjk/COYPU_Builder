"""T-130: the context-data import framework -- settings, CRS resolution, GUID reconciliation, provenance,
plugin loading and the five protocol methods -- exercised end to end with a test-only reader on
synthetic terrain.

Nothing here touches the network, a real data file or the real per-user config: `config_path()` always points
into `tmp_path`, and fake entry points never install anything.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shapely
from websockets.asyncio.client import connect

from coypu_builder import PROTOCOL_VERSION, __version__, config
from coypu_builder.config import UserConfig, config_path, load_user_config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.model.ids import new_id
from coypu_builder.domain.sources import (
    BBoxClip,
    CorridorClip,
    FileOrigin,
    ImportSettings,
    ObjectRecord,
    SourceKind,
    reconcile,
)
from coypu_builder.io.gis.envelope import EnvelopeOptions, corridor_envelope
from coypu_builder.io.landxml.dialects import resolve_coordinates
from coypu_builder.io.sources import registry
from coypu_builder.io.sources.registry import (
    PROVIDER_GROUP,
    READER_GROUP,
    CrsTransform,
    ImportedObject,
    InspectReport,
    ReaderFailure,
    load_plugins,
    reader_for,
    register_provider,
    register_reader,
)
from coypu_builder.io.sources.runner import (
    CrsRequired,
    NoAlignments,
    UnknownAlignment,
    inspect_source,
    resolve_transform,
    run_import,
)
from coypu_builder.protocol.messages import METHODS, Envelope, Stability
from coypu_builder.server import serve
from coypu_builder.server.codec import decode_frame, encode_frame
from fixtures.synthetic import terrain
from fixtures.synthetic.terrain import (
    KRAV82_BBOX,
    is_synthetic,
    read_synthetic,
    sample_grid,
    sample_irregular,
    split_sheets,
    surface,
    write_synthetic,
)

KROVAK = ProjectCRS("EPSG:5514")
KRALUPY_XML = Path(__file__).parent / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_BBOX = (-746800.0, -1025300.0, -746400.0, -1024900.0)
UUID_HEX = 32


class SyntheticXyzReader:
    """The test-only reader for the synthetic text format. Objects are sheets; the key is the file name plus
    the sheet index, the payload the clipped (n, 3) float64 points in the project CRS."""

    format_id = "synthetic-xyz"
    kinds = frozenset({SourceKind.TERRAIN})
    suffixes = (".xyz",)

    def sniff(self, path: Path) -> bool:
        return is_synthetic(path)

    def inspect(self, path: Path) -> InspectReport:
        data = read_synthetic(path)
        rows = [s for s in data.sheets.values() if len(s)]
        tokens = np.vstack(rows) if rows else np.empty((0, 3))
        extent = (
            (
                float(tokens[:, 0].min()),
                float(tokens[:, 1].min()),
                float(tokens[:, 0].max()),
                float(tokens[:, 1].max()),
            )
            if len(tokens)
            else None
        )
        return InspectReport(
            format_id=self.format_id,
            kind=SourceKind.TERRAIN,
            declared_crs=data.declared_crs,
            extent=extent,
            counts={"points": len(tokens), "sheets": len(data.sheets)},
            suggested=ImportSettings(horizontal_crs=data.declared_crs),
            licence_hint="synthetic (CC0)",
            warnings=(),
        )

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> Iterable[ImportedObject]:
        for index, tokens in read_synthetic(path).sheets.items():
            if settings.nodata is not None:
                tokens = tokens[tokens[:, 2] != settings.nodata]
            points = transform(tokens)
            if clip_polygon is not None:
                points = points[shapely.contains_xy(clip_polygon, points[:, 0], points[:, 1])]
            if len(points) == 0:
                continue
            digest = hashlib.sha256(np.ascontiguousarray(points).tobytes()).hexdigest()
            yield ImportedObject(f"{path.name}#{index}", digest, points)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch, tmp_path):
    """A private registry with only the test reader, and a user config that cannot be the real one."""
    monkeypatch.setattr(registry, "_READERS", {})
    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    fake_config = tmp_path / "user-config" / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: fake_config)
    register_reader(SyntheticXyzReader())


def _points(bbox=LOCAL_BBOX, spacing=10.0, seed=1) -> np.ndarray:
    return sample_irregular(bbox, spacing, seed)


def _file(tmp_path: Path, points=None, declared="EPSG:5514", name="terrain.xyz", **kwargs) -> Path:
    return write_synthetic(tmp_path / name, _points() if points is None else points, declared, **kwargs)


def _import(path: Path, settings: ImportSettings | None = None, alignments=None, **kwargs):
    return run_import(
        path, SourceKind.TERRAIN, settings or ImportSettings(), KROVAK, alignments or {}, **kwargs
    )


def _payload(outcome) -> np.ndarray:
    parts = [outcome.payloads[record.id] for record in outcome.dataset.objects]
    return np.vstack(parts) if parts else np.empty((0, 3))


def _sorted(points: np.ndarray) -> np.ndarray:
    return points[np.lexsort((points[:, 1], points[:, 0]))]


# --- the synthetic terrain itself ---------------------------------------------------------------------------


def test_surface_is_analytic_and_deterministic():
    e0, n0 = terrain.E0, terrain.N0
    far = surface(e0 - 200.0 + 5000.0, n0 + 150.0 - 5000.0)  # far from hill and ridge: the plane alone
    assert far == pytest.approx(terrain.BASE_H + terrain.GRADIENT_E * 4800.0 + terrain.GRADIENT_N * -4850.0)
    hill_top = surface(*terrain.HILL_CENTRE)
    flank = surface(terrain.HILL_CENTRE[0] + 900.0, terrain.HILL_CENTRE[1])
    assert hill_top - flank > terrain.HILL_AMPLITUDE * 0.9
    mid = np.mean([terrain.RIDGE_START, terrain.RIDGE_END], axis=0)
    assert surface(*mid) - surface(mid[0] + 250.0, mid[1] - 250.0) > terrain.RIDGE_AMPLITUDE * 0.5
    a = sample_irregular(LOCAL_BBOX, 10.0, seed=7)
    b = sample_irregular(LOCAL_BBOX, 10.0, seed=7)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, sample_irregular(LOCAL_BBOX, 10.0, seed=8))
    assert np.allclose(a[:, 2], surface(a[:, 0], a[:, 1]), rtol=0, atol=0)
    assert len(a) == 40 * 40


def test_grid_and_sheets():
    grid = sample_grid(LOCAL_BBOX, 25.0)
    assert grid.z.shape == (len(grid.n), len(grid.e)) == (17, 17)
    assert grid.z[3, 5] == surface(grid.e[5], grid.n[3])
    assert len(grid.points()) == 17 * 17
    sheets = split_sheets(KRAV82_BBOX, 3, 2)
    assert len(sheets) == 6
    assert sheets[0][:2] == (KRAV82_BBOX[0], KRAV82_BBOX[1])
    assert sheets[2][2] == KRAV82_BBOX[2] and sheets[5][3] == KRAV82_BBOX[3]
    assert sheets[0][2] == sheets[1][0] and sheets[0][3] == sheets[3][1]


def test_synthetic_file_round_trip(tmp_path):
    pts = _points()
    path = write_synthetic(tmp_path / "a.xyz", {0: pts[:10], 4: pts[10:20]}, "EPSG:5514")
    data = read_synthetic(path)
    assert data.declared_crs == "EPSG:5514"
    assert sorted(data.sheets) == [0, 4]
    assert np.array_equal(data.sheets[4][:, [1, 0, 2]], pts[10:20])  # tokens are (N, E, H)
    assert read_synthetic(write_synthetic(tmp_path / "b.xyz", pts, None)).declared_crs is None


# --- reconcile ----------------------------------------------------------------------------------------------


def test_reconcile_keeps_guids_of_matched_keys_and_mints_new_ones():
    previous = (
        ObjectRecord("a", "id-a", "h1"),
        ObjectRecord("b", "id-b", "h2"),
        ObjectRecord("c", "id-c", "h3"),
    )
    minted = iter(["new-1"])
    result = reconcile(previous, {"a": "h1", "b": "CHANGED", "d": "h4"}, make_id=lambda: next(minted))
    assert result.unchanged == ("a",)
    assert result.changed == ("b",)
    assert result.added == ("d",)
    assert result.removed == ("c",)
    assert {o.key: o.id for o in result.objects} == {"a": "id-a", "b": "id-b", "d": "new-1"}
    assert {o.key: o.content_hash for o in result.objects}["b"] == "CHANGED"


def test_reconcile_against_nothing_adds_everything_with_uuid4_hex():
    result = reconcile((), {"x": "h", "y": "h"})
    assert result.added == ("x", "y") and not (result.changed or result.unchanged or result.removed)
    ids = [o.id for o in result.objects]
    assert len(set(ids)) == 2
    for guid in ids:
        assert len(guid) == UUID_HEX and int(guid, 16) >= 0 and guid == guid.lower()
        assert guid[12] == "4"  # uuid4 version nibble


def test_reconcile_refuses_duplicate_previous_keys():
    with pytest.raises(ValueError, match="duplicate"):
        reconcile((ObjectRecord("a", "1", "h"), ObjectRecord("a", "2", "h")), {})


def test_settings_validate_themselves():
    with pytest.raises(ValueError, match="coordinate_order"):
        ImportSettings(coordinate_order="XY")
    with pytest.raises(ValueError, match="linear_unit_m"):
        ImportSettings(linear_unit_m=0.0)
    with pytest.raises(ValueError, match="empty clip box"):
        BBoxClip(1.0, 0.0, 0.0, 1.0)
    with pytest.raises(ValueError, match="buffer_m"):
        CorridorClip(buffer_m=0.0)
    with pytest.raises(ValueError, match="exactly one"):
        CorridorClip(100.0, alignment_ids=("a", "b"), station_from=1.0)


# --- criterion 1: CRS ---------------------------------------------------------------------------------------


def test_inspect_reports_a_missing_crs_with_a_warning(tmp_path):
    path = _file(tmp_path, declared=None)
    _reader, report = inspect_source(path)
    assert report.declared_crs is None
    assert any("no coordinate reference system" in w for w in report.warnings)
    assert report.counts["points"] == 1600


def test_import_without_any_crs_is_refused_and_with_one_it_succeeds(tmp_path):
    path = _file(tmp_path, declared=None)
    with pytest.raises(CrsRequired):
        _import(path)
    outcome = _import(path, ImportSettings(horizontal_crs="EPSG:5514"))
    assert outcome.dataset.effective_crs == "EPSG:5514"
    assert outcome.dataset.declared_crs is None
    assert outcome.dataset.transformation == "none"
    assert np.allclose(_sorted(_payload(outcome)), _sorted(_points()), atol=1e-6)


def test_declared_utm_file_lands_on_its_krovak_twin(tmp_path):
    twin = _points()
    e, n = KROVAK.to_crs("EPSG:32633", twin[:, 0], twin[:, 1])
    utm = np.column_stack([e, n, twin[:, 2]])
    path = _file(tmp_path, utm, declared="EPSG:32633", name="utm.xyz")
    outcome = _import(path)
    assert outcome.dataset.effective_crs == "EPSG:32633"
    assert outcome.dataset.transformation != "none"
    assert "Krovak" in outcome.dataset.transformation
    assert outcome.dataset.transformation_accuracy_m is None or outcome.dataset.transformation_accuracy_m > 0
    assert not any("overrides" in w for w in outcome.warnings)
    landed = _payload(outcome)
    assert np.abs(landed - twin).max() < 0.01


def test_overriding_a_declared_crs_is_warned_and_recorded(tmp_path):
    path = _file(tmp_path, declared="EPSG:5513", krovak_positive=True)
    outcome = _import(path, ImportSettings(horizontal_crs="EPSG:5514"))
    assert outcome.dataset.declared_crs == "EPSG:5513"
    assert outcome.dataset.effective_crs == "EPSG:5514"
    assert any("overrides" in w and "EPSG:5513" in w for w in outcome.warnings)
    same = _import(path, ImportSettings(horizontal_crs="EPSG:5513"))
    assert not any("overrides" in w for w in same.warnings)
    assert same.dataset.effective_crs == "EPSG:5513" and same.dataset.transformation == "none"
    assert np.abs(_payload(same) - _payload(outcome)).max() < 1e-6


def test_unusable_crs_settings_are_value_errors(tmp_path):
    path = _file(tmp_path)
    with pytest.raises(ValueError, match="horizontal_crs"):
        _import(path, ImportSettings(horizontal_crs="EPSG:not-a-code"))
    with pytest.raises(ValueError, match="horizontal 2D"):
        _import(path, ImportSettings(horizontal_crs="EPSG:5705"))
    with pytest.raises(ValueError, match="vertical_crs"):
        _import(path, ImportSettings(vertical_crs="nonsense"))


def test_linear_unit_and_coordinate_order_apply(tmp_path):
    pts = _points()
    twin_e, twin_n = KROVAK.to_crs("EPSG:32633", pts[:, 0], pts[:, 1])
    path = write_synthetic(
        tmp_path / "en.xyz", np.column_stack([twin_e, twin_n, pts[:, 2]]), "EPSG:32633", order="EN"
    )
    landed = _payload(_import(path, ImportSettings(coordinate_order="EN")))
    assert np.abs(landed - pts).max() < 0.01
    swapped = _payload(_import(path, ImportSettings(coordinate_order="NE", horizontal_crs="EPSG:32633")))
    assert np.abs(swapped[:, :2] - pts[:, :2]).max() > 1.0  # reading E as N is visibly wrong, never silent

    centimetres = write_synthetic(tmp_path / "cm.xyz", pts * 100.0, "EPSG:5514")
    scaled = _payload(_import(centimetres, ImportSettings(linear_unit_m=0.01)))
    assert np.abs(_sorted(scaled) - _sorted(pts)).max() < 1e-6


def test_nodata_is_applied_by_the_reader(tmp_path):
    pts = _points()
    pts[:5, 2] = -9999.0
    outcome = _import(_file(tmp_path, pts), ImportSettings(nodata=-9999.0))
    assert len(_payload(outcome)) == len(pts) - 5


# --- criterion 2: Krovak signs ------------------------------------------------------------------------------


def test_positive_krovak_tokens_land_on_the_negative_east_north_twin(tmp_path):
    pts = _points()
    negative = _import(_file(tmp_path, pts, name="neg.xyz"))
    positive = _import(_file(tmp_path, pts, name="pos.xyz", krovak_positive=True))
    a, b = _payload(negative), _payload(positive)
    assert np.abs(a - b).max() < 1e-6
    assert np.abs(a - pts).max() < 1e-6
    assert (a[:, 0] < 0).all() and (a[:, 1] < 0).all()


# --- criterion 3: identity ----------------------------------------------------------------------------------


def _three_sheets():
    pts = _points(spacing=20.0)
    third = len(pts) // 3
    return {0: pts[:third], 1: pts[third : 2 * third], 2: pts[2 * third :]}


def test_first_import_uses_uuid4_guids_and_reimport_of_an_identical_file_changes_nothing(tmp_path):
    path = write_synthetic(tmp_path / "t.xyz", _three_sheets(), "EPSG:5514")
    first = _import(path)
    ids = [o.id for o in first.dataset.objects]
    assert [o.key for o in first.dataset.objects] == ["t.xyz#0", "t.xyz#1", "t.xyz#2"]
    assert len(set(ids)) == 3
    assert all(len(i) == UUID_HEX and i[12] == "4" for i in ids)
    assert set(first.payloads) == set(ids)
    assert (len(first.reconciliation.added), len(first.reconciliation.unchanged)) == (3, 0)

    again = _import(path, previous=first.dataset)
    assert [o.id for o in again.dataset.objects] == ids
    assert again.dataset.id == first.dataset.id
    rec = again.reconciliation
    assert (len(rec.added), len(rec.changed), len(rec.unchanged), len(rec.removed)) == (0, 0, 3, 0)


def test_reimport_with_one_changed_one_removed_and_one_added(tmp_path):
    sheets = _three_sheets()
    path = write_synthetic(tmp_path / "t.xyz", sheets, "EPSG:5514")
    first = _import(path)
    ids = {o.key: o.id for o in first.dataset.objects}

    edited = {0: sheets[0], 1: sheets[1] + np.array([0.0, 0.0, 0.5]), 3: _points(spacing=40.0, seed=9)}
    write_synthetic(path, edited, "EPSG:5514")
    second = _import(path, previous=first.dataset)
    rec = second.reconciliation
    assert (rec.unchanged, rec.changed, rec.added, rec.removed) == (
        ("t.xyz#0",),
        ("t.xyz#1",),
        ("t.xyz#3",),
        ("t.xyz#2",),
    )
    by_key = {o.key: o.id for o in second.dataset.objects}
    assert by_key["t.xyz#0"] == ids["t.xyz#0"]
    assert by_key["t.xyz#1"] == ids["t.xyz#1"]
    assert by_key["t.xyz#3"] not in ids.values()
    assert ids["t.xyz#2"] not in by_key.values()
    assert set(second.payloads) == set(by_key.values())


def test_two_independent_imports_of_one_file_have_disjoint_guids(tmp_path):
    path = write_synthetic(tmp_path / "t.xyz", _three_sheets(), "EPSG:5514")
    a, b = _import(path), _import(path)
    assert a.dataset.id != b.dataset.id
    assert not {o.id for o in a.dataset.objects} & {o.id for o in b.dataset.objects}
    assert [o.content_hash for o in a.dataset.objects] == [o.content_hash for o in b.dataset.objects]


def test_reimport_must_match_kind_and_format(tmp_path):
    path = _file(tmp_path)
    first = _import(path)
    with pytest.raises(ValueError, match="does not read imagery"):
        run_import(path, SourceKind.IMAGERY, ImportSettings(), KROVAK, {}, previous=first.dataset)
    other = first.dataset.__class__(**{**first.dataset.__dict__, "format_id": "other-format"})
    with pytest.raises(ValueError, match="other-format"):
        _import(path, previous=other)


# --- criterion 4: provenance --------------------------------------------------------------------------------


def test_the_record_carries_provenance(tmp_path):
    path = _file(tmp_path)
    settings = ImportSettings(
        horizontal_crs="EPSG:5514",
        vertical_crs="EPSG:5705",
        clip=BBoxClip(*LOCAL_BBOX),
        point_classes=(2, 9),
        display_resolution_m=5.0,
        nodata=-9999.0,
    )
    outcome = _import(path, settings, name="Kralupy test")
    d = outcome.dataset
    assert isinstance(d.origin, FileOrigin)
    assert d.origin.paths == (str(path),)
    assert d.origin.sha256 == (hashlib.sha256(path.read_bytes()).hexdigest(),)
    assert d.settings == settings
    assert d.tool_version == f"coypu-builder {__version__}"
    stamp = datetime.fromisoformat(d.imported_at)
    assert stamp.utcoffset().total_seconds() == 0
    assert d.licence == "synthetic (CC0)"
    assert d.attribution == ""
    assert d.name == "Kralupy test"
    assert d.kind is SourceKind.TERRAIN and d.format_id == "synthetic-xyz"
    assert _import(path).dataset.name == "terrain"


# --- criterion 6: clip --------------------------------------------------------------------------------------


def _inside_ring(ring: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Even-odd ray casting against one closed ring: an independent point-in-polygon test."""
    inside = np.zeros(len(x), dtype=bool)
    x1, y1 = ring[:-1, 0], ring[:-1, 1]
    x2, y2 = ring[1:, 0], ring[1:, 1]
    for ax, ay, bx, by in zip(x1, y1, x2, y2, strict=True):
        crosses = (ay > y) != (by > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = ax + (y - ay) * (bx - ax) / (by - ay)
        inside ^= crosses & (x < x_at)
    return inside


def _inside_polygon(polygon, points: np.ndarray) -> np.ndarray:
    parts = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
    result = np.zeros(len(points), dtype=bool)
    for part in parts:
        inside = _inside_ring(np.asarray(part.exterior.coords), points[:, 0], points[:, 1])
        for hole in part.interiors:
            inside &= ~_inside_ring(np.asarray(hole.coords), points[:, 0], points[:, 1])
        result |= inside
    return result


def test_bbox_clip_keeps_exactly_the_points_inside(tmp_path):
    pts = _points(KRAV82_BBOX, spacing=40.0)
    path = _file(tmp_path, pts)
    clip = BBoxClip(-747000.0, -1025500.0, -746000.0, -1024500.0)
    kept = _payload(_import(path, ImportSettings(clip=clip)))
    expected = (
        (pts[:, 0] >= clip.min_e)
        & (pts[:, 0] <= clip.max_e)
        & (pts[:, 1] >= clip.min_n)
        & (pts[:, 1] <= clip.max_n)
    )
    assert 0 < expected.sum() < len(pts)
    assert len(kept) == expected.sum()
    assert np.allclose(_sorted(kept), _sorted(pts[expected]), atol=1e-6)


def test_corridor_clip_keeps_exactly_the_points_inside_the_corridor_polygon(tmp_path, kralupy):
    alignment = kralupy.alignment
    box = (-746800.0, -1024800.0, -746350.0, -1024000.0)
    pts = _points(box, spacing=6.0, seed=3)
    path = _file(tmp_path, pts)
    clip = CorridorClip(buffer_m=60.0, station_from=14100.0, station_to=14600.0)
    polygon = corridor_envelope([alignment], EnvelopeOptions(60.0, station_from=14100.0, station_to=14600.0))
    expected = _inside_polygon(polygon, pts)
    assert 0 < expected.sum() < len(pts)
    outcome = _import(path, ImportSettings(clip=clip), alignments={"aln": alignment})
    kept = _payload(outcome)
    assert len(kept) == expected.sum()
    assert np.allclose(_sorted(kept), _sorted(pts[expected]), atol=1e-6)

    everything = _import(
        path, ImportSettings(clip=CorridorClip(buffer_m=60.0)), alignments={"aln": alignment}
    )
    assert len(_payload(everything)) > len(kept)


def test_corridor_clip_errors(tmp_path, kralupy):
    path = _file(tmp_path)
    with pytest.raises(NoAlignments):
        _import(path, ImportSettings(clip=CorridorClip(buffer_m=50.0)))
    with pytest.raises(UnknownAlignment):
        _import(
            path, ImportSettings(clip=CorridorClip(50.0, ("nope",))), alignments={"aln": kralupy.alignment}
        )
    two = {"a": kralupy.alignment, "b": kralupy.alignment}
    with pytest.raises(ValueError, match="exactly one"):
        _import(
            path,
            ImportSettings(clip=CorridorClip(50.0, station_from=14100.0, station_to=14200.0)),
            alignments=two,
        )


# --- registry -----------------------------------------------------------------------------------------------


def test_reader_lookup(tmp_path):
    path = _file(tmp_path)
    assert reader_for(path).format_id == "synthetic-xyz"
    assert reader_for(path, "synthetic-xyz").format_id == "synthetic-xyz"
    with pytest.raises(ValueError, match="unknown format_id"):
        reader_for(path, "nope")
    other = tmp_path / "other.xyz"
    other.write_text("not a synthetic file\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no registered reader"):
        reader_for(other)
    with pytest.raises(ValueError, match="already registered"):
        register_reader(SyntheticXyzReader())
    with pytest.raises(ValueError, match="format_id"):
        register_reader(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="provider_id"):
        register_provider(object())  # type: ignore[arg-type]


def test_run_import_wraps_a_reader_bug_and_refuses_relative_paths(tmp_path):
    class Broken(SyntheticXyzReader):
        format_id = "broken"

        def read(self, *args, **kwargs):
            raise KeyError("bug")

    register_reader(Broken())
    path = _file(tmp_path)
    from coypu_builder.io.sources.runner import ReaderFailure

    with pytest.raises(ReaderFailure, match="KeyError"):
        _import(path, format_id="broken")
    with pytest.raises(ValueError, match="absolute"):
        _import(Path("relative.xyz"))


def test_resolve_transform_directly():
    t = resolve_transform(ImportSettings(), "EPSG:5514", KROVAK)
    assert t.identical and t.description == "none" and t.effective_crs == "EPSG:5514"
    assert np.array_equal(t(np.array([[-1.0, -2.0, 3.0]])), np.array([[-2.0, -1.0, 3.0]]))
    with pytest.raises(CrsRequired):
        resolve_transform(ImportSettings(), None, KROVAK)


# --- configuration ------------------------------------------------------------------------------------------


def test_config_path_per_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert config_path() == tmp_path / "roaming" / "COYPU Builder" / "config.toml"
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config_path() == tmp_path / "xdg" / "coypu-builder" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert config_path() == Path.home() / ".config" / "coypu-builder" / "config.toml"


def test_missing_config_gives_the_defaults(tmp_path):
    cfg, warnings = load_user_config(tmp_path / "nope" / "config.toml")
    assert cfg == UserConfig() and warnings == ()


def test_malformed_config_gives_the_defaults_and_a_warning(tmp_path):
    bad = tmp_path / "config.toml"
    bad.write_text("[plugins\nallow = [", encoding="utf-8")
    cfg, warnings = load_user_config(bad)
    assert cfg == UserConfig()
    assert len(warnings) == 1 and "not valid TOML" in warnings[0]

    bad.write_bytes(b"\xff\xfe\x00")
    assert load_user_config(bad)[0] == UserConfig()
    bad.write_text('[plugins]\nallow = "example-reader"\n', encoding="utf-8")
    cfg, warnings = load_user_config(bad)
    assert cfg == UserConfig() and "list of strings" in warnings[0]
    bad.write_text("plugins = 3\n", encoding="utf-8")
    assert "table" in load_user_config(bad)[1][0]


def test_config_allow_list_is_read(tmp_path):
    good = tmp_path / "config.toml"
    good.write_text('[plugins]\nallow = ["a-reader", "b-provider"]\n[other]\nx = 1\n', encoding="utf-8")
    assert load_user_config(good) == (UserConfig(plugins_allow=("a-reader", "b-provider")), ())


# --- criterion 5: plugins -----------------------------------------------------------------------------------


class FakeEntryPoint:
    """Stands in for `importlib.metadata.EntryPoint`; nothing is installed into the environment."""

    def __init__(
        self, name: str, group: str, module: str, attr: str | None = None, dist=("fake-dist", "1.2.3")
    ):
        self.name, self.group, self.module, self.attr = name, group, module, attr
        self.dist = type("Dist", (), {"name": dist[0], "version": dist[1]})()
        self.loads = 0

    def load(self):
        import importlib

        self.loads += 1
        module = importlib.import_module(self.module)
        return getattr(module, self.attr) if self.attr else module


OK_PLUGIN = """
from pathlib import Path

from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.sources.registry import ImportedObject, InspectReport
import numpy as np


class FakeReader:
    format_id = "fake-ok"
    kinds = frozenset({SourceKind.TERRAIN})
    suffixes = (".fok",)

    def sniff(self, path):
        return Path(path).suffix == ".fok"

    def inspect(self, path):
        return InspectReport("fake-ok", SourceKind.TERRAIN, "EPSG:5514", None, {"points": 2},
                             ImportSettings(), "plugin licence", ())

    def read(self, path, settings, transform, clip_polygon):
        yield ImportedObject("only", "hash-1", np.zeros((2, 3)))
"""


def _install_plugin_modules(tmp_path, monkeypatch) -> tuple[Path, list[str]]:
    mods = tmp_path / "plugin-modules"
    mods.mkdir()
    flag = tmp_path / "blocked-was-imported.flag"
    (mods / "fakeplug_blocked.py").write_text(
        textwrap.dedent(f"""
            from pathlib import Path
            Path({str(flag)!r}).write_text("imported")
            class Reader:
                format_id = "blocked"
        """).lstrip(),
        encoding="utf-8",
    )
    (mods / "fakeplug_ok.py").write_text(OK_PLUGIN, encoding="utf-8")
    (mods / "fakeplug_boom.py").write_text('raise RuntimeError("boom on import")\n', encoding="utf-8")
    (mods / "fakeplug_noattrs.py").write_text("class Reader:\n    format_id = 'x'\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(mods))
    names = ["fakeplug_blocked", "fakeplug_ok", "fakeplug_boom", "fakeplug_noattrs"]
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return flag, names


def _fake_entry_points(monkeypatch, eps: list[FakeEntryPoint]):
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep for ep in eps if ep.group == group])


def test_plugin_states_and_the_never_imported_sentinel(tmp_path, monkeypatch):
    flag, names = _install_plugin_modules(tmp_path, monkeypatch)
    eps = [
        FakeEntryPoint("blocked-reader", READER_GROUP, "fakeplug_blocked", "Reader"),
        FakeEntryPoint("ok-reader", READER_GROUP, "fakeplug_ok", "FakeReader"),
        FakeEntryPoint("boom-reader", READER_GROUP, "fakeplug_boom"),
        FakeEntryPoint("noattrs-reader", READER_GROUP, "fakeplug_noattrs", "Reader"),
    ]
    _fake_entry_points(monkeypatch, eps)
    infos = load_plugins(UserConfig(plugins_allow=("ok-reader", "boom-reader", "noattrs-reader")))
    state = {i.name: i for i in infos}

    assert state["blocked-reader"].state == "available" and state["blocked-reader"].error is None
    assert eps[0].loads == 0
    assert not flag.exists()
    assert "fakeplug_blocked" not in sys.modules

    assert state["ok-reader"].state == "loaded"
    assert (state["ok-reader"].distribution, state["ok-reader"].version) == ("fake-dist", "1.2.3")
    assert state["ok-reader"].group == READER_GROUP
    assert registry.reader_for(tmp_path / "x.fok").format_id == "fake-ok"

    assert state["boom-reader"].state == "failed"
    assert state["boom-reader"].error == "RuntimeError: boom on import"
    assert state["noattrs-reader"].state == "failed" and "lacks" in state["noattrs-reader"].error
    assert [i.name for i in infos] == sorted(i.name for i in infos)

    for name in names:
        sys.modules.pop(name, None)


def test_providers_are_discovered_in_their_own_group(tmp_path, monkeypatch):
    mods = tmp_path / "prov"
    mods.mkdir()
    (mods / "fakeplug_provider.py").write_text(
        textwrap.dedent("""
            from coypu_builder.domain.sources import SourceKind

            class Provider:
                provider_id = "fake-provider"
                kinds = frozenset({SourceKind.TERRAIN})
                licence = "CC0"
                attribution = "nobody"

                def inspect(self, query):
                    raise NotImplementedError

                def fetch(self, query, cache_dir):
                    return []
        """).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(mods))
    monkeypatch.delitem(sys.modules, "fakeplug_provider", raising=False)
    _fake_entry_points(monkeypatch, [FakeEntryPoint("p", PROVIDER_GROUP, "fakeplug_provider", "Provider")])
    assert load_plugins(UserConfig(plugins_allow=("p",)))[0].state == "loaded"
    assert [p.provider_id for p in registry.providers()] == ["fake-provider"]
    sys.modules.pop("fakeplug_provider", None)


def test_duplicate_plugin_registration_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    _install_plugin_modules(tmp_path, monkeypatch)
    ep = FakeEntryPoint("ok-reader", READER_GROUP, "fakeplug_ok", "FakeReader")
    _fake_entry_points(monkeypatch, [ep])
    cfg = UserConfig(plugins_allow=("ok-reader",))
    assert load_plugins(cfg)[0].state == "loaded"
    second = load_plugins(cfg)[0]
    assert second.state == "failed" and "already registered" in second.error
    sys.modules.pop("fakeplug_ok", None)


# --- criterion 7: a live server -----------------------------------------------------------------------------


async def _call(ws, request_id: str, method: str, params: dict[str, Any] | None = None) -> Envelope:
    envelope = Envelope(v=PROTOCOL_VERSION, id=request_id, type="req", method=method, params=params)
    await ws.send(encode_frame(envelope))
    return decode_frame(await ws.recv())[0]


@pytest.fixture
async def server_url():
    srv = await serve(host="127.0.0.1", port=0, allow_no_token=True)
    try:
        yield f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        await srv.wait_closed()


async def _session(ws, crs: str | None = "EPSG:5514") -> None:
    await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
    opened = await _call(ws, "p", "project.new", {"crs": crs} if crs else {})
    assert opened.type == "res", opened.error


def _settings(**overrides) -> dict[str, Any]:
    return {"horizontal_crs": "EPSG:5514", **overrides}


async def test_inspect_run_list_get_over_the_wire(server_url, tmp_path):
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)

        inspected = await _call(ws, "1", "import.inspect", {"path": str(path)})
        assert inspected.type == "res", inspected.error
        r = inspected.result
        assert (r["format_id"], r["kind"], r["declared_crs"]) == ("synthetic-xyz", "terrain", "EPSG:5514")
        assert r["counts"] == {"points": 1600, "sheets": 1}
        assert r["suggested_settings"]["horizontal_crs"] == "EPSG:5514"
        assert r["suggested_settings"]["clip"] is None
        assert r["licence_hint"] == "synthetic (CC0)"
        assert len(r["extent"]) == 4

        settings = _settings(
            clip={
                "type": "bbox",
                "min_e": -746700.0,
                "min_n": -1025200.0,
                "max_e": -746500.0,
                "max_n": -1025000.0,
            }
        )
        ran = await _call(
            ws, "2", "import.run", {"path": str(path), "kind": "terrain", "settings": settings, "name": "T"}
        )
        assert ran.type == "res", ran.error
        run = ran.result
        assert (run["name"], run["effective_crs"], run["transformation"]) == ("T", "EPSG:5514", "none")
        assert run["transformation_accuracy_m"] == 0.0
        assert (run["added"], run["changed"], run["unchanged"], run["removed"]) == (1, 0, 0, 0)

        listed = await _call(ws, "3", "source.list")
        assert listed.type == "res", listed.error
        (summary,) = listed.result["sources"]
        assert summary == {
            "id": run["source_id"],
            "kind": "terrain",
            "name": "T",
            "format_id": "synthetic-xyz",
            "object_count": 1,
            "imported_at": summary["imported_at"],
            "attribution": "",
        }

        got = await _call(ws, "4", "source.get", {"source_id": run["source_id"]})
        assert got.type == "res", got.error
        d = got.result
        assert d["origin"] == {
            "type": "file",
            "paths": [str(path)],
            "sha256": [hashlib.sha256(path.read_bytes()).hexdigest()],
        }
        assert d["settings"]["clip"] == settings["clip"]
        assert d["settings"]["horizontal_crs"] == "EPSG:5514"
        assert d["tool_version"] == f"coypu-builder {__version__}"
        assert d["licence"] == "synthetic (CC0)"
        assert datetime.fromisoformat(d["imported_at"])
        assert [o["key"] for o in d["objects"]] == ["terrain.xyz#0"]
        assert len(d["objects"][0]["id"]) == UUID_HEX and d["objects"][0]["content_hash"]

        plugins = await _call(ws, "5", "plugins.list")
        assert plugins.type == "res", plugins.error
        assert plugins.result == {"plugins": [], "warnings": []}


async def test_crs_is_refused_not_guessed_over_the_wire(server_url, tmp_path):
    path = _file(tmp_path, declared=None)
    async with connect(server_url) as ws:
        await _session(ws)
        inspected = await _call(ws, "1", "import.inspect", {"path": str(path)})
        assert inspected.result["declared_crs"] is None
        assert any("no coordinate reference system" in w for w in inspected.result["warnings"])

        refused = await _call(ws, "2", "import.run", {"path": str(path), "kind": "terrain", "settings": {}})
        assert (refused.type, refused.error.code) == ("err", "E_CRS_REQUIRED")
        ok = await _call(
            ws, "3", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings()}
        )
        assert ok.type == "res", ok.error
        assert ok.result["transformation"] == "none"
        assert (await _call(ws, "4", "session.ping", {})).type == "res"

    async with connect(server_url) as ws:
        await _session(ws, crs=None)
        no_crs = await _call(
            ws, "1", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings()}
        )
        assert no_crs.error.code == "E_CRS_REQUIRED"


async def test_override_warning_over_the_wire(server_url, tmp_path):
    path = _file(tmp_path, declared="EPSG:5513", krovak_positive=True)
    async with connect(server_url) as ws:
        await _session(ws)
        ran = await _call(
            ws, "1", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings()}
        )
        assert any("overrides" in w for w in ran.result["warnings"])
        got = await _call(ws, "2", "source.get", {"source_id": ran.result["source_id"]})
        assert got.result["declared_crs"] == "EPSG:5513" and got.result["effective_crs"] == "EPSG:5514"
        assert got.result["warnings"] == ran.result["warnings"]


async def test_reimport_over_the_wire_keeps_guids(server_url, tmp_path):
    sheets = _three_sheets()
    path = write_synthetic(tmp_path / "t.xyz", sheets, "EPSG:5514")
    async with connect(server_url) as ws:
        await _session(ws)
        params = {"path": str(path), "kind": "terrain", "settings": _settings()}
        first = await _call(ws, "1", "import.run", params)
        guids = {
            o["key"]: o["id"]
            for o in (await _call(ws, "2", "source.get", {"source_id": first.result["source_id"]})).result[
                "objects"
            ]
        }

        same = await _call(ws, "3", "import.run", {**params, "reimport_of": first.result["source_id"]})
        assert same.type == "res", same.error
        assert same.result["source_id"] == first.result["source_id"]
        assert (
            same.result["added"],
            same.result["changed"],
            same.result["unchanged"],
            same.result["removed"],
        ) == (0, 0, 3, 0)

        write_synthetic(
            path,
            {0: sheets[0], 1: sheets[1] + np.array([0.0, 0.0, 1.0]), 3: _points(spacing=40.0, seed=9)},
            "EPSG:5514",
        )
        edited = await _call(ws, "4", "import.run", {**params, "reimport_of": first.result["source_id"]})
        r = edited.result
        assert (r["added"], r["changed"], r["unchanged"], r["removed"]) == (1, 1, 1, 1)
        after = {
            o["key"]: o["id"]
            for o in (await _call(ws, "5", "source.get", {"source_id": r["source_id"]})).result["objects"]
        }
        assert after["t.xyz#0"] == guids["t.xyz#0"] and after["t.xyz#1"] == guids["t.xyz#1"]
        assert after["t.xyz#3"] not in guids.values()
        assert (await _call(ws, "6", "source.list")).result["sources"].__len__() == 1

        fresh = await _call(ws, "7", "import.run", params)
        assert fresh.result["source_id"] != first.result["source_id"]
        fresh_ids = {
            o["id"]
            for o in (await _call(ws, "8", "source.get", {"source_id": fresh.result["source_id"]})).result[
                "objects"
            ]
        }
        assert not fresh_ids & set(after.values())


async def test_corridor_clip_over_the_wire_round_trips_the_tagged_union(server_url, tmp_path):
    path = _file(tmp_path, _points((-746800.0, -1024800.0, -746350.0, -1024000.0), spacing=6.0, seed=3))
    async with connect(server_url) as ws:
        await _session(ws, crs=None)
        landxml = await _call(ws, "0", "import.landxml", {"path": str(KRALUPY_XML)})
        assert landxml.type == "res", landxml.error
        alignment_id = landxml.result["alignments"][0]["alignment_id"]
        clip = {
            "type": "corridor",
            "buffer_m": 60.0,
            "alignment_ids": [alignment_id],
            "station_from": 14100.0,
            "station_to": 14600.0,
        }
        ran = await _call(
            ws, "1", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings(clip=clip)}
        )
        assert ran.type == "res", ran.error
        got = await _call(ws, "2", "source.get", {"source_id": ran.result["source_id"]})
        assert got.result["settings"]["clip"] == clip

        unknown = {**clip, "alignment_ids": ["no-such"]}
        bad = await _call(
            ws, "3", "import.run", {"path": str(path), "kind": "terrain", "settings": _settings(clip=unknown)}
        )
        assert bad.error.code == "E_NOT_FOUND"


async def test_bad_requests_return_errors_and_the_connection_survives(server_url, tmp_path):
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)
        valid = {"path": str(path), "kind": "terrain", "settings": _settings()}
        cases = [
            ("import.inspect", {"path": "relative.xyz"}, "E_BAD_PARAMS"),
            ("import.inspect", {"path": str(tmp_path / "missing.xyz")}, "E_NOT_FOUND"),
            ("import.inspect", {}, "E_BAD_PARAMS"),
            ("import.inspect", {"path": str(path), "format_id": "nope"}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "kind": "fish"}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "path": "relative.xyz"}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "settings": _settings(coordinate_order="XY")}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "settings": _settings(linear_unit_m=-1.0)}, "E_BAD_PARAMS"),
            (
                "import.run",
                {**valid, "settings": _settings(horizontal_crs="EPSG:not-a-code")},
                "E_BAD_PARAMS",
            ),
            ("import.run", {**valid, "settings": {"clip": {"type": "blob"}}}, "E_BAD_PARAMS"),
            (
                "import.run",
                {
                    **valid,
                    "settings": _settings(
                        clip={"type": "bbox", "min_e": 1, "min_n": 1, "max_e": 0, "max_n": 0}
                    ),
                },
                "E_BAD_PARAMS",
            ),
            (
                "import.run",
                {**valid, "settings": _settings(clip={"type": "corridor", "buffer_m": 50.0})},
                "E_EMPTY",
            ),
            ("import.run", {**valid, "kind": "imagery"}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "format_id": "nope"}, "E_BAD_PARAMS"),
            ("import.run", {**valid, "reimport_of": "no-such-source"}, "E_NOT_FOUND"),
            ("import.run", {**valid, "path": str(tmp_path / "missing.xyz")}, "E_NOT_FOUND"),
            ("source.get", {"source_id": "no-such-source"}, "E_NOT_FOUND"),
            ("source.get", {}, "E_BAD_PARAMS"),
        ]
        for index, (method, params, code) in enumerate(cases):
            reply = await _call(ws, f"c{index}", method, params)
            assert (reply.type, reply.error.code) == ("err", code), (method, params, reply)
        assert (await _call(ws, "z", "session.ping", {})).type == "res"
        assert (await _call(ws, "y", "import.run", valid)).type == "res"

    async with connect(server_url) as ws:
        for method in ("import.inspect", "import.run", "source.list", "source.get", "plugins.list"):
            reply = await _call(ws, "n", method, {})
            assert (reply.type, reply.error.code) == ("err", "E_NO_SESSION")
        await _call(ws, "h", "session.hello", {"client": "pytest", "client_version": "0"})
        for method in ("import.run", "source.list", "source.get"):
            reply = await _call(
                ws, "n", method, {"path": str(path), "kind": "terrain", "settings": {}, "source_id": "x"}
            )
            assert reply.error.code == "E_NO_PROJECT", method


async def test_a_reimport_of_another_format_is_refused(server_url, tmp_path):
    class Other(SyntheticXyzReader):
        format_id = "synthetic-xyz-b"
        suffixes = (".xyzb",)

        def sniff(self, path):
            return False

    register_reader(Other())
    path = _file(tmp_path)
    async with connect(server_url) as ws:
        await _session(ws)
        params = {"path": str(path), "kind": "terrain", "settings": _settings()}
        first = await _call(ws, "1", "import.run", params)
        bad = await _call(
            ws,
            "2",
            "import.run",
            {**params, "format_id": "synthetic-xyz-b", "reimport_of": first.result["source_id"]},
        )
        assert (bad.type, bad.error.code) == ("err", "E_BAD_PARAMS")
        assert (await _call(ws, "3", "source.list")).result["sources"][0]["format_id"] == "synthetic-xyz"


async def test_a_reader_bug_is_an_internal_error_and_the_server_keeps_serving(server_url, tmp_path):
    class BrokenRead(SyntheticXyzReader):
        format_id = "broken-read"
        suffixes = (".brk",)

        def sniff(self, path):
            return False

        def read(self, *args, **kwargs):
            raise KeyError("bug")

    class BrokenInspect(BrokenRead):
        format_id = "broken-inspect"

        def inspect(self, path):
            raise RuntimeError("inspect bug")

    register_reader(BrokenRead())
    register_reader(BrokenInspect())
    path = _file(tmp_path)
    params = {"path": str(path), "kind": "terrain", "settings": _settings()}
    async with connect(server_url) as ws:
        await _session(ws)
        run = await _call(ws, "1", "import.run", {**params, "format_id": "broken-read"})
        assert run.error.code == "E_INTERNAL" and "KeyError" in run.error.message
        run = await _call(ws, "2", "import.run", {**params, "format_id": "broken-inspect"})
        assert run.error.code == "E_INTERNAL" and "RuntimeError" in run.error.message
        inspected = await _call(ws, "3", "import.inspect", {"path": str(path), "format_id": "broken-inspect"})
        assert inspected.error.code == "E_INTERNAL"
        assert (await _call(ws, "4", "session.ping", {})).type == "res"
        assert (await _call(ws, "5", "source.list")).result["sources"] == []


async def test_plugins_list_reports_every_state_and_an_allow_listed_reader_imports(
    server_url, tmp_path, monkeypatch
):
    flag, names = _install_plugin_modules(tmp_path, monkeypatch)
    _fake_entry_points(
        monkeypatch,
        [
            FakeEntryPoint("blocked-reader", READER_GROUP, "fakeplug_blocked", "Reader"),
            FakeEntryPoint("ok-reader", READER_GROUP, "fakeplug_ok", "FakeReader"),
            FakeEntryPoint("boom-reader", READER_GROUP, "fakeplug_boom"),
        ],
    )
    cfg = tmp_path / "user-config" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[plugins]\nallow = ["ok-reader", "boom-reader"]\n', encoding="utf-8")
    fok = tmp_path / "data.fok"
    fok.write_text("x", encoding="utf-8")

    async with connect(server_url) as ws:
        await _session(ws)
        plugins = await _call(ws, "1", "plugins.list")
        assert plugins.type == "res", plugins.error
        listed = {p["name"]: p for p in plugins.result["plugins"]}
        assert {n: p["state"] for n, p in listed.items()} == {
            "blocked-reader": "available",
            "boom-reader": "failed",
            "ok-reader": "loaded",
        }
        assert listed["boom-reader"]["error"] == "RuntimeError: boom on import"
        assert listed["ok-reader"]["distribution"] == "fake-dist"
        assert plugins.result["warnings"] == []
        print(json.dumps(plugins.result["plugins"], indent=1))
        assert not flag.exists()

        ran = await _call(ws, "2", "import.run", {"path": str(fok), "kind": "terrain", "settings": {}})
        assert ran.type == "res", ran.error
        assert ran.result["added"] == 1
        got = await _call(ws, "3", "source.get", {"source_id": ran.result["source_id"]})
        assert got.result["licence"] == "plugin licence" and got.result["format_id"] == "fake-ok"
        assert (await _call(ws, "4", "session.ping", {})).type == "res"
    for name in names:
        sys.modules.pop(name, None)


async def test_an_allow_listed_plugin_reader_works_before_plugins_list_is_ever_called(
    server_url, tmp_path, monkeypatch
):
    _flag, names = _install_plugin_modules(tmp_path, monkeypatch)
    _fake_entry_points(monkeypatch, [FakeEntryPoint("ok-reader", READER_GROUP, "fakeplug_ok", "FakeReader")])
    cfg = tmp_path / "user-config" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[plugins]\nallow = ["ok-reader"]\n', encoding="utf-8")
    fok = tmp_path / "data.fok"
    fok.write_text("x", encoding="utf-8")
    async with connect(server_url) as ws:
        await _session(ws)
        inspected = await _call(ws, "1", "import.inspect", {"path": str(fok)})
        assert inspected.type == "res", inspected.error
        assert inspected.result["format_id"] == "fake-ok"
    for name in names:
        sys.modules.pop(name, None)


def test_startup_loads_plugins_and_reports_problems_on_stderr(tmp_path, monkeypatch, capsys):
    from coypu_builder.__main__ import _load_plugins

    _flag, names = _install_plugin_modules(tmp_path, monkeypatch)
    _fake_entry_points(monkeypatch, [FakeEntryPoint("boom-reader", READER_GROUP, "fakeplug_boom")])
    cfg = tmp_path / "user-config" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text('[plugins]\nallow = ["boom-reader"]\n[plugins.x\n', encoding="utf-8")
    _load_plugins()
    first = capsys.readouterr()
    assert first.out == "" and "not valid TOML" in first.err

    monkeypatch.setattr(registry, "_PLUGIN_REPORT", None)
    cfg.write_text('[plugins]\nallow = ["boom-reader"]\n', encoding="utf-8")
    _load_plugins()
    second = capsys.readouterr()
    assert (
        second.out == ""
        and "plugin 'boom-reader' failed to load (RuntimeError: boom on import)" in second.err
    )
    for name in names:
        sys.modules.pop(name, None)


async def test_a_malformed_user_config_is_reported_by_plugins_list(server_url, tmp_path):
    cfg = tmp_path / "user-config" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("[plugins\n", encoding="utf-8")
    async with connect(server_url) as ws:
        await _session(ws)
        plugins = await _call(ws, "1", "plugins.list")
        assert plugins.result["plugins"] == []
        assert len(plugins.result["warnings"]) == 1 and "not valid TOML" in plugins.result["warnings"][0]


def test_the_new_methods_are_experimental_in_the_registry_and_the_docs():
    new = {"import.inspect", "import.run", "source.list", "source.get", "plugins.list"}
    specs = {s.name: s for s in METHODS}
    assert new <= set(specs)
    assert all(specs[name].stability is Stability.EXPERIMENTAL for name in new)
    docs = (REPO_ROOT / "docs" / "protocol" / "ipc.md").read_text(encoding="utf-8")
    for name in new:
        section = docs.split(f"### `{name}`", 1)[1].split("###", 1)[0]
        assert "**Stability**: `experimental`" in section


def test_generated_docs_check_exits_zero():
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "gen_protocol_docs.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT / "backend",
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_new_ids_are_never_derived_from_keys():
    assert new_id() != new_id()
    a = reconcile((), {"same-key": "h"})
    b = reconcile((), {"same-key": "h"})
    assert a.objects[0].id != b.objects[0].id


# --- review round: order vs sign, hardening ---


@pytest.mark.parametrize("native", ["NE", "EN"])
@pytest.mark.parametrize("written", ["NE", "EN"])
@pytest.mark.parametrize("positive", [True, False], ids=["positive-krovak", "negative-east-north"])
@pytest.mark.parametrize("order", ["auto", "EN", "NE"])
def test_token_order_and_krovak_sign_are_independent(order, positive, written, native):
    e, n = -746500.0, -1025000.0
    east_axis, north_axis = (-e, -n) if positive else (e, n)
    tokens = (north_axis, east_axis) if written == "NE" else (east_axis, north_axis)
    got = resolve_coordinates(np.array([tokens]), KROVAK, order, native)[0]
    effective = native if order == "auto" else order
    expected = (e, n) if effective == written else (n, e)  # a wrong order swaps, visibly
    assert tuple(got) == expected


def test_an_explicit_order_is_honoured_for_krovak_east_north_tokens():
    """The reviewer's case: (E, N) tokens with coordinate_order EN on EPSG:5514 are unchanged."""
    tokens = np.array([[-746500.0, -1025000.0]])
    assert resolve_coordinates(tokens, KROVAK, "EN").tolist() == [[-746500.0, -1025000.0]]
    assert resolve_coordinates(tokens, KROVAK, "NE").tolist() == [[-1025000.0, -746500.0]]
    assert resolve_coordinates(tokens, KROVAK, "auto").tolist() == [
        [-1025000.0, -746500.0]
    ]  # LandXML default
    t = resolve_transform(ImportSettings(coordinate_order="EN"), "EPSG:5514", KROVAK)
    assert t(np.array([[-746500.0, -1025000.0, 177.0]])).tolist() == [[-746500.0, -1025000.0, 177.0]]


def test_landxml_auto_behaviour_is_unchanged():
    utm = ProjectCRS("EPSG:32633")
    assert resolve_coordinates(np.array([[5500100.0, 450010.0]]), utm).tolist() == [[450010.0, 5500100.0]]
    assert resolve_coordinates(np.array([[1025000.0, 746500.0]]), KROVAK).tolist() == [
        [-746500.0, -1025000.0]
    ]
    with pytest.raises(ValueError, match="coordinate order"):
        resolve_coordinates(np.array([[1.0, 2.0]]), KROVAK, "XY")


class EnNativeReader(SyntheticXyzReader):
    """A reader whose files are (east, north) like LAS and GeoTIFF: `auto` means EN for it."""

    format_id = "synthetic-xyz-en"
    suffixes = (".xyzen",)
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        return False


@pytest.mark.parametrize("positive", [False, True], ids=["negative", "positive"])
def test_a_reader_with_native_order_en_lands_correctly_on_auto(tmp_path, positive):
    register_reader(EnNativeReader())
    pts = _points()
    path = _file(tmp_path, pts, order="EN", krovak_positive=positive)
    landed = _payload(_import(path, format_id="synthetic-xyz-en"))
    assert np.abs(landed - pts).max() < 1e-6
    swapped = _payload(_import(path))  # the NE-native reader reads the same file the wrong way round
    assert np.abs(swapped[:, :2] - pts[:, :2]).max() > 1.0
    explicit = _payload(_import(path, ImportSettings(coordinate_order="EN")))
    assert np.abs(explicit - pts).max() < 1e-6


@pytest.mark.parametrize(
    ("declared", "twin"), [("EPSG:5513", "EPSG:5514"), ("EPSG:2065", "EPSG:5221"), ("EPSG:8352", "EPSG:8353")]
)
def test_krovak_twin_pairs_land_on_identical_points(tmp_path, declared, twin):
    project = ProjectCRS(twin)
    pts = _points()
    positive = write_synthetic(tmp_path / "pos.xyz", pts, declared, krovak_positive=True)
    negative = write_synthetic(tmp_path / "neg.xyz", pts, twin)
    a = run_import(positive, SourceKind.TERRAIN, ImportSettings(), project, {})
    b = run_import(negative, SourceKind.TERRAIN, ImportSettings(), project, {})
    assert a.dataset.transformation == "none" and b.dataset.transformation == "none"
    assert a.dataset.effective_crs == declared and b.dataset.effective_crs == twin
    assert np.array_equal(_payload(a), _payload(b))
    assert np.abs(_payload(a) - pts).max() < 1e-6


def test_the_no_crs_warning_is_always_added(tmp_path):
    class Chatty(SyntheticXyzReader):
        format_id = "chatty"
        suffixes = (".chat",)

        def sniff(self, path):
            return False

        def inspect(self, path):
            report = super().inspect(path)
            return InspectReport(
                report.format_id,
                report.kind,
                None,
                report.extent,
                report.counts,
                report.suggested,
                report.licence_hint,
                ("cannot parse the CRS block of this file",),
            )

    register_reader(Chatty())
    _reader, report = inspect_source(_file(tmp_path, declared=None), "chatty")
    assert "cannot parse the CRS block of this file" in report.warnings
    assert sum("no coordinate reference system" in w for w in report.warnings) == 1


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_numbers_are_rejected(bad):
    for kwargs in ({"linear_unit_m": bad}, {"display_resolution_m": bad}, {"nodata": bad}):
        with pytest.raises(ValueError, match="finite"):
            ImportSettings(**kwargs)
    with pytest.raises(ValueError, match="finite"):
        BBoxClip(bad, 0.0, 1.0, 1.0)
    with pytest.raises(ValueError, match="finite"):
        BBoxClip(0.0, 0.0, 1.0, bad)
    with pytest.raises(ValueError, match="buffer_m"):
        CorridorClip(buffer_m=bad)
    with pytest.raises(ValueError, match="finite"):
        CorridorClip(100.0, ("a",), station_from=bad)
    with pytest.raises(ValueError, match="finite"):
        CorridorClip(100.0, ("a",), station_to=bad)


def test_a_utf8_bom_does_not_discard_the_allow_list(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + b'[plugins]\nallow = ["a-reader"]\n')
    assert load_user_config(path) == (UserConfig(plugins_allow=("a-reader",)), ())


def test_a_raising_sniff_is_a_reader_failure_not_a_crash(tmp_path):
    class RaisingSniff(SyntheticXyzReader):
        format_id = "raising-sniff"
        suffixes = (".dat",)

        def sniff(self, path):
            raise OSError("sniff bug")

    register_reader(RaisingSniff())
    other = tmp_path / "x.dat"
    other.write_text("unrecognised", encoding="utf-8")
    with pytest.raises(ReaderFailure, match="raising-sniff"):
        reader_for(other)
    assert reader_for(_file(tmp_path)).format_id == "synthetic-xyz"  # another reader still wins


async def test_a_raising_sniff_and_a_malformed_yield_keep_the_connection_alive(server_url, tmp_path):
    class RaisingSniff(SyntheticXyzReader):
        format_id = "raising-sniff"
        suffixes = (".dat",)

        def sniff(self, path):
            raise RuntimeError("sniff bug")

    class Malformed(SyntheticXyzReader):
        format_id = "malformed"
        suffixes = (".mal",)

        def sniff(self, path):
            return False

        def read(self, path, settings, transform, clip_polygon):
            yield ("not", "an", "ImportedObject")

    class NoReport(Malformed):
        format_id = "no-report"

        def inspect(self, path):
            return None

    register_reader(RaisingSniff())
    register_reader(Malformed())
    register_reader(NoReport())
    unknown = tmp_path / "x.dat"
    unknown.write_text("unrecognised", encoding="utf-8")
    path = _file(tmp_path)
    run = {"kind": "terrain", "settings": _settings()}
    async with connect(server_url) as ws:
        await _session(ws)
        cases = [
            ("import.inspect", {"path": str(unknown)}),
            ("import.run", {"path": str(unknown), **run}),
            ("import.run", {"path": str(path), **run, "format_id": "malformed"}),
            ("import.inspect", {"path": str(path), "format_id": "no-report"}),
            ("import.run", {"path": str(path), **run, "format_id": "no-report"}),
        ]
        for index, (method, params) in enumerate(cases):
            reply = await _call(ws, f"c{index}", method, params)
            assert (reply.type, reply.error.code) == ("err", "E_INTERNAL"), (method, params, reply)
            assert (await _call(ws, f"p{index}", "session.ping", {})).type == "res"
        assert (await _call(ws, "ok", "import.run", {"path": str(path), **run})).type == "res"


def test_entry_point_enumeration_failure_is_a_reported_plugin_failure(monkeypatch, capsys):
    def broken(group):
        raise RuntimeError("corrupt metadata")

    monkeypatch.setattr(registry, "entry_points", broken)
    infos = load_plugins(UserConfig(plugins_allow=("anything",)))
    assert [i.state for i in infos] == ["failed", "failed"]
    assert all("corrupt metadata" in i.error for i in infos)
    assert {i.group for i in infos} == {READER_GROUP, PROVIDER_GROUP}

    from coypu_builder.__main__ import _load_plugins

    _load_plugins()
    assert "failed to load" in capsys.readouterr().err


async def test_plugins_list_survives_corrupt_entry_point_metadata(server_url, tmp_path, monkeypatch):
    def broken(group):
        raise RuntimeError("corrupt metadata")

    monkeypatch.setattr(registry, "entry_points", broken)
    async with connect(server_url) as ws:
        await _session(ws)
        listed = await _call(ws, "1", "plugins.list")
        assert listed.type == "res", listed.error
        assert {p["state"] for p in listed.result["plugins"]} == {"failed"}
        assert (await _call(ws, "2", "session.ping", {})).type == "res"
        inspected = await _call(ws, "3", "import.inspect", {"path": str(_file(tmp_path))})
        assert inspected.type == "res", inspected.error
