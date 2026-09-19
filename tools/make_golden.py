"""Regenerate shared/golden/*.json from the backend implementation.

Run from backend/: `uv run python ../tools/make_golden.py`. Both the pytest suite and the Godot gdUnit4 suite
consume these files, so the client's re-implementations (origin mapping, frame interpolation, trainset chain)
are pinned to the backend's float64 reference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from coypu_builder.domain.crs import (
    BasePoint,
    ProjectCRS,
    basis_to_godot,
    points_to_godot,
    quaternion_from_matrix,
)
from coypu_builder.domain.kinematics import bake_run_table, pose_trainset
from coypu_builder.domain.lrs import frames
from coypu_builder.domain.model import Trainset, new_id
from coypu_builder.io.catalogue.vehicles import load_catalogue
from coypu_builder.io.coypu import read_coypu
from coypu_builder.io.coypu.kinematics_csv import read_stops_csv
from coypu_builder.io.landxml import read_landxml, to_alignment

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "shared" / "golden"
KRALUPY = ROOT / "backend" / "tests" / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"
KRALUPY_COYPU = ROOT / "backend" / "tests" / "fixtures" / "kralupy" / "kralupy_neratovice_092.coypu"
KRALUPY_STOPS_CSV = ROOT / "backend" / "tests" / "fixtures" / "kralupy" / "kralupy_neratovice_092_stops.csv"
BACKEND_TESTS = ROOT / "backend" / "tests"


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


def run_table() -> dict:
    project = read_coypu(KRALUPY_COYPU)
    stops = read_stops_csv(KRALUPY_STOPS_CSV)
    run = project.kinematics_run(0, stops=stops)
    table = bake_run_table(run)

    grid_t = np.arange(len(table), dtype=np.float64) * table.dt
    grid_t[-1] = table.duration

    n = len(table)
    max_speed_index = int(np.argmax(table.speed))

    dwell_index = None
    if table.stops:
        stop_station = table.stops[0].station_m
        near = np.nonzero((np.abs(table.station - stop_station) < 0.5) & (table.speed < 0.05))[0]
        if len(near) > 0:
            dwell_index = int(near[len(near) // 2])

    spread = np.linspace(0, n - 1, 18, dtype=np.int64).tolist()
    indices = sorted({*spread, max_speed_index, *([dwell_index] if dwell_index is not None else [])})

    return {
        "source": f"{KRALUPY_COYPU.name} vehicle 0",
        "dt": table.dt,
        "row_count": n,
        "duration": table.duration,
        "max_speed_index": max_speed_index,
        "dwell_index": dwell_index,
        "samples": [
            {
                "index": i,
                "t": float(grid_t[i]),
                "station": float(table.station[i]),
                "speed": float(table.speed[i]),
                "accel": float(table.accel[i]),
            }
            for i in indices
        ],
    }


def _build_tram_loop():
    """`tests/fixtures/synthetic/tram_loop.py` is a test module, not a package under `src/` -- this tool is a
    dev-only script (never shipped, ADR 0008), so a `sys.path` insert is how it reuses that fixture instead
    of hand-writing a second canted alignment that would drift from it (F10)."""
    tests_dir = str(BACKEND_TESTS)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    from fixtures.synthetic.tram_loop import ELEVATION_M, build_tram_loop

    return build_tram_loop(), ELEVATION_M


def tram_block() -> dict:
    """F10: a second, clearly separated golden block from the synthetic tram loop, which actually cants --
    unlike the Kralupy block above, whose LandXML cant is a documented zero placeholder. This is the block
    that pins mean-roll averaging and the Gram-Schmidt correction in `_car_pose`.

    tram_loop.py's vertical alignment is exactly flat (`VerticalAlignment.constant`) on both the street and
    the loop, so a "cant and non-zero gradient coexist" sample (as F10 originally asked for) does not exist
    anywhere in this fixture. What actually makes the Gram-Schmidt step non-trivial is a *changing* roll or
    curvature between a car's two pivots, not gradient specifically -- verified below: every constant-cant,
    constant-curvature sample (the two "full cant" ones) gives an exact no-op (the projection removed is
    <1e-16, i.e. floating-point noise), while every cant-ramp sample gives a genuine, non-zero correction.
    The two ramp samples are this block's real proof; the loop sample repeats it at a second, tighter (30 m
    vs. 120 m) radius. See the T-112 Follow-up F10 closing report for the numbers.
    """
    (_, alignments), elevation = _build_tram_loop()
    street_id, loop_id = list(alignments.keys())
    street, loop = alignments[street_id], alignments[loop_id]

    spec = load_catalogue()["tram_generic"]
    trainset = Trainset(
        id=new_id(),
        spec_key=spec.key,
        cars=spec.cars[:2],
        coupling_gap_m=spec.coupling_gap_m,
        mode=spec.mode,
        gauge_mm=spec.gauge_mm,
    )
    base = BasePoint(0.0, 0.0, elevation)

    # (alignment label, alignment, station_lead) -- a zero-cant control; cant ramping up then down on the
    # street (the samples that pin mean-roll averaging: lead/trail rolls differ by ~13-14 mrad); full cant
    # inside the street's constant-curvature arc (both pivots equal -- a no-op, included anyway because it
    # is still the direct fix for F10's "every roll is zero" finding); and a tighter-radius ramp on the loop.
    samples = [
        ("street", street, 20.0),
        ("street", street, 65.0),
        ("street", street, 80.0),
        ("street", street, 95.0),
        ("loop", loop, 25.0),
    ]

    payload_samples = []
    for label, aln, station_lead in samples:
        pose = pose_trainset(aln, trainset, station_lead, direction=1)
        cars = []
        for car in pose.cars:
            lead_pos = points_to_godot(car.lead.position[None, :], base, dtype=np.float64)[0]
            trail_pos = points_to_godot(car.trail.position[None, :], base, dtype=np.float64)[0]
            body_pos = points_to_godot(car.origin[None, :], base, dtype=np.float64)[0]
            body_basis = basis_to_godot(car.forward[None, :], car.left[None, :], car.up[None, :])
            body_quat = quaternion_from_matrix(body_basis)[0]
            cars.append(
                {
                    "index": car.index,
                    "lead_pivot": {
                        "station": car.lead.station,
                        "roll": car.lead.roll,
                        "godot_position": lead_pos.tolist(),
                    },
                    "trail_pivot": {
                        "station": car.trail.station,
                        "roll": car.trail.roll,
                        "godot_position": trail_pos.tolist(),
                    },
                    "body": {
                        "godot_position": body_pos.tolist(),
                        "godot_quaternion_xyzw": body_quat.tolist(),
                        "roll": car.roll,
                    },
                }
            )
        payload_samples.append(
            {
                "alignment": label,
                "station_lead": station_lead,
                "direction": 1,
                "clamped": pose.clamped,
                "cars": cars,
            }
        )

    return {
        "source": "tests/fixtures/synthetic/tram_loop.py:build_tram_loop",
        "crs": None,  # arbitrary local metres, not a real projected CRS -- see tram_loop.py
        "base_point": {"easting": base.easting, "northing": base.northing, "height": base.height},
        "trainset": {
            "spec_key": trainset.spec_key,
            "name": trainset.name,
            "mode": trainset.mode.value,
            "gauge_mm": trainset.gauge_mm,
            "coupling_gap_m": trainset.coupling_gap_m,
            "cars": [
                {
                    "name": c.name,
                    "length_m": c.length_m,
                    "width_m": c.width_m,
                    "height_m": c.height_m,
                    "floor_height_m": c.floor_height_m,
                    "bogie_pivot_distance_m": c.bogie_pivot_distance_m,
                    "bogie_wheelbase_m": c.bogie_wheelbase_m,
                    "wheel_diameter_m": c.wheel_diameter_m,
                    "color": c.color,
                }
                for c in trainset.cars
            ],
        },
        "samples": payload_samples,
    }


def trainset_chain() -> dict:
    raw = read_landxml(KRALUPY)[0]
    crs = ProjectCRS(f"EPSG:{raw.epsg}")
    aln = to_alignment(raw, crs).alignment
    base = BasePoint.rounded(
        *aln.horizontal.point(aln.station_start)[0], aln.vertical.elevation(aln.station_start)[0]
    )

    trainset = Trainset.from_spec(load_catalogue()["dmu_br650_cd840"], units=3)

    # (station_lead, direction) -- eight-plus samples chosen to exercise a distinct regime each; see the
    # T-112 closing report for the justification of every station.
    samples = [
        (100.0, 1),  # start of the route
        (8260.0, 1),  # long straight (6615-9905 m), well clear of any transition
        (16850.0, 1),  # sharpest curve on the file, R ~= 300 m
        (16790.0, 1),  # clothoid ramping into that same sharpest curve
        (3850.0, 1),  # consist straddles the arc(14)/clothoid(15)/line(16) boundary
        (16034.09, 1),  # nominal cant-ramp station (Kralupy's cant block is a zero placeholder -- see report)
        (2860.255557, 1),  # vertical curve PVI, horizontally flat
        (18199.971666, 1),  # 15 m past the alignment end: exercises clamping
        (8260.0, -1),  # the long straight again, reversed, to pin the direction sign
    ]

    payload_samples = []
    for station_lead, direction in samples:
        pose = pose_trainset(aln, trainset, station_lead, direction=direction)
        cars = []
        for car in pose.cars:
            lead_pos = points_to_godot(car.lead.position[None, :], base, dtype=np.float64)[0]
            trail_pos = points_to_godot(car.trail.position[None, :], base, dtype=np.float64)[0]
            body_pos = points_to_godot(car.origin[None, :], base, dtype=np.float64)[0]
            body_basis = basis_to_godot(car.forward[None, :], car.left[None, :], car.up[None, :])
            body_quat = quaternion_from_matrix(body_basis)[0]
            cars.append(
                {
                    "index": car.index,
                    "lead_pivot": {"station": car.lead.station, "godot_position": lead_pos.tolist()},
                    "trail_pivot": {"station": car.trail.station, "godot_position": trail_pos.tolist()},
                    "body": {
                        "godot_position": body_pos.tolist(),
                        "godot_quaternion_xyzw": body_quat.tolist(),
                        "roll": car.roll,
                    },
                }
            )
        payload_samples.append(
            {
                "station_lead": station_lead,
                "direction": direction,
                "clamped": pose.clamped,
                "cars": cars,
            }
        )

    return {
        "source": KRALUPY.name,
        "crs": f"EPSG:{raw.epsg}",
        "base_point": {"easting": base.easting, "northing": base.northing, "height": base.height},
        "trainset": {
            "spec_key": trainset.spec_key,
            "name": trainset.name,
            "mode": trainset.mode.value,
            "gauge_mm": trainset.gauge_mm,
            "coupling_gap_m": trainset.coupling_gap_m,
            "cars": [
                {
                    "name": c.name,
                    "length_m": c.length_m,
                    "width_m": c.width_m,
                    "height_m": c.height_m,
                    "floor_height_m": c.floor_height_m,
                    "bogie_pivot_distance_m": c.bogie_pivot_distance_m,
                    "bogie_wheelbase_m": c.bogie_wheelbase_m,
                    "wheel_diameter_m": c.wheel_diameter_m,
                    "color": c.color,
                }
                for c in trainset.cars
            ],
        },
        "samples": payload_samples,
        "tram_block": tram_block(),
    }


def main() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    payloads = (
        ("origin_mapping.json", origin_mapping()),
        ("frame_eval.json", frame_eval()),
        ("run_table.json", run_table()),
        ("trainset_chain.json", trainset_chain()),
    )
    for name, payload in payloads:
        (GOLDEN / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {GOLDEN / name}")


if __name__ == "__main__":
    main()
