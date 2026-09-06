"""Regenerate shared/golden/*.json from the backend implementation.

Run from backend/: `uv run python ../tools/make_golden.py`. Both the pytest suite and the Godot gdUnit4 suite
consume these files, so the client's re-implementations (origin mapping, frame interpolation, trainset chain)
are pinned to the backend's float64 reference.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from coypu_builder.domain.crs import (
    BasePoint,
    ProjectCRS,
    basis_to_godot,
    points_to_godot,
    quaternion_from_matrix,
)
from coypu_builder.domain.lrs import frames
from coypu_builder.io.landxml import read_landxml, to_alignment

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "shared" / "golden"
KRALUPY = ROOT / "backend" / "tests" / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"


def origin_mapping() -> dict:
    base = BasePoint(-733000.0, -1024800.0, 100.0)
    samples = np.array(
        [
            [-733000.0, -1024800.0, 100.0],
            [-732990.0, -1024780.0, 105.0],
            [-733123.456, -1024612.789, 171.25],
            [-700000.0, -1000000.0, 0.0],
        ]
    )
    godot = points_to_godot(samples, base, dtype=np.float64)
    frames_domain = [
        ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
        ([0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
        ([0.6, 0.8, 0.0], [-0.8, 0.6, 0.0], [0.0, 0.0, 1.0]),
    ]
    basis_rows = []
    for t, left, u in frames_domain:
        m = basis_to_godot(np.array([t]), np.array([left]), np.array([u]))[0]
        basis_rows.append(
            {
                "tangent": t,
                "left": left,
                "up": u,
                "godot_basis_columns": m.T.tolist(),
                "godot_quaternion_xyzw": quaternion_from_matrix(m).tolist(),
            }
        )
    return {
        "base_point": {"easting": base.easting, "northing": base.northing, "height": base.height},
        "points": [{"enh": s.tolist(), "godot": g.tolist()} for s, g in zip(samples, godot, strict=True)],
        "frames": basis_rows,
    }


def frame_eval() -> dict:
    raw = read_landxml(KRALUPY)[0]
    crs = ProjectCRS(f"EPSG:{raw.epsg}")
    aln = to_alignment(raw, crs).alignment
    base = BasePoint.rounded(
        *aln.horizontal.point(aln.station_start)[0], aln.vertical.elevation(aln.station_start)[0]
    )
    stations = np.array([0.0, 3.901454, 53.916823, 1000.0, 5038.0, 9123.45, 16300.0, 18184.971666])
    fr = frames(aln, stations)
    pos = points_to_godot(fr.origin, base, dtype=np.float64)
    quat = quaternion_from_matrix(basis_to_godot(fr.tangent, fr.left, fr.up))
    return {
        "source": KRALUPY.name,
        "crs": f"EPSG:{raw.epsg}",
        "base_point": {"easting": base.easting, "northing": base.northing, "height": base.height},
        "rows": [
            {
                "station": float(s),
                "godot_position": p.tolist(),
                "godot_quaternion_xyzw": q.tolist(),
                "heading": float(h),
                "roll": float(r),
                "pitch": float(pi),
                "cant_mm": float(c),
                "curvature": float(k),
            }
            for s, p, q, h, r, pi, c, k in zip(
                stations, pos, quat, fr.heading, fr.roll, fr.pitch, fr.cant_mm, fr.curvature, strict=True
            )
        ],
    }


def main() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, payload in (("origin_mapping.json", origin_mapping()), ("frame_eval.json", frame_eval())):
        (GOLDEN / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {GOLDEN / name}")


if __name__ == "__main__":
    main()
