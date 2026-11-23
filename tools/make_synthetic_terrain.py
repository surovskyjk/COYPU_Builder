"""Writes the synthetic terrain fixtures of T-135 Part B: two LandXML TINs and an orthophoto with world file.

Both TINs sit on the straight part of the Kralupy track (stations 14 787 m to 16 011 m in
`backend/tests/fixtures/kralupy/kralupy_neratovice_092.xml`), so the scene is on the Kralupy
project's own ground. The fine TIN (about 50 m spacing) is a cutting: 0.4 m below the rails
within 8 m of the centreline, rising to 2.2 m above them beyond about 13 m. A 1.5 m mound stands
off the track, and a 16 m hill sits over the line where the train dwells (about station 15 757 m).
There the ground rises above a camera that is low beside the track, so from that shot the terrain
must be culled. The coarse TIN (about 150 m spacing) is wider and sits 1.7 m below the fine plateau,
so the seam between the two sources shows. The orthophoto covers the coarse extent: a flat-colour
pattern with a white "F" inside the fine TIN, which shows a flipped or mirrored drape. Every value
is a formula written here or a value read from the Kralupy profile; nothing is survey data.

Run from backend/ (Pillow comes with the `gis` extra, which `uv run` keeps):

    uv run python ../tools/make_synthetic_terrain.py

The output goes to client/tests/fixtures/terrain/ and is deterministic. Triangles split each grid
cell along one fixed diagonal, so the output does not depend on the qhull version. The XML and the
world file use LF line endings. Each file must stay under 100 kB.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[1]
ALIGNMENT = REPO / "backend" / "tests" / "fixtures" / "kralupy" / "kralupy_neratovice_092.xml"
OUT = REPO / "client" / "tests" / "fixtures" / "terrain"

LANDXML = "{http://www.landxml.org/schema/LandXML-1.2}"
# The straight `Line` of "Track 1" that the Kralupy file gives at station 14 786.769736 m, as N then E in the
# file's own magnitudes (its signs are stripped, as the file's header says).
LINE_STATION0_M = 14786.769736
LINE_START_NE = (1024435.252407, 746626.718416)
LINE_END_NE = (1025627.908637, 746349.585807)

# The fine TIN: along the straight, across it, on a lattice.
FINE_ALONG_FROM_M = -150.0
FINE_ALONG_BEYOND_M = 100.0
FINE_ALONG_STEP_M = 50.0
FINE_ACROSS_HALF_M = 250.0
FINE_ACROSS_STEP_M = 50.0
TRENCH_HALF_M = 8.0
FLOOR_M = -0.4  # ground under the track, relative to the rail
PLATEAU_M = 2.2  # ground beyond the cutting's slope, relative to the rail
CUT_SLOPE = 0.5  # rise per metre beyond the trench
MOUND_M = 1.5
MOUND_AT_ALONG_M = 700.0
MOUND_AT_ACROSS_M = 160.0
MOUND_SIGMA_M = 150.0
HILL_M = 16.0
HILL_AT_ALONG_M = 970.0  # about station 15 757 m, where the train dwells at 940 to 963 s
HILL_SIGMA_ALONG_M = 70.0
HILL_SIGMA_ACROSS_M = 160.0

# The coarse TIN: a wider, coarser surface a little below the fine plateau.
COARSE_ALONG_FROM_M = -900.0
COARSE_ALONG_TO_M = 2100.0
COARSE_ACROSS_HALF_M = 900.0
COARSE_STEP_M = 150.0
COARSE_LIFT_M = 0.5  # above the rail

# The orthophoto, over the coarse extent. The marker sits inside the fine TIN's hull.
PIXEL_M = 2.0
MARGIN_M = 50.0
BLOCK_M = 40.0
MARKER_ALONG_M = 50.0
MARKER_ACROSS_M = -180.0
MARKER_SIZE_PX = 30
MARKER_STROKE_PX = 6
MAX_BYTES = 100_000

PALETTE = np.array(
    [[96, 130, 74], [120, 150, 88], [160, 150, 96], [84, 112, 64], [140, 128, 84]], dtype=np.uint8
)


def profile_stations_and_elevations() -> tuple[np.ndarray, np.ndarray]:
    """The PVI stations and elevations of the Kralupy profile, which the TINs follow."""
    root = ElementTree.parse(ALIGNMENT).getroot()
    pvis = [pvi for pvi in root.iter(f"{LANDXML}PVI") if pvi.text]
    values = np.array([[float(token) for token in pvi.text.split()[:2]] for pvi in pvis])
    return values[:, 0], values[:, 1]


def plan_frame() -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Start point, along and across unit vectors and length of the straight, in N and E magnitudes."""
    start = np.array(LINE_START_NE)
    end = np.array(LINE_END_NE)
    length = float(np.linalg.norm(end - start))
    along = (end - start) / length
    across = np.array([-along[1], along[0]])
    return start, along, across, length


def rail_height(along: np.ndarray) -> np.ndarray:
    """The rail height in metres at each offset along the straight."""
    stations, elevations = profile_stations_and_elevations()
    return np.interp(LINE_STATION0_M + along, stations, elevations)


def to_ne(along: np.ndarray, across: np.ndarray) -> np.ndarray:
    """Plan positions (N, E in magnitudes) of (along, across) offsets from the straight's start."""
    start, along_unit, across_unit, _ = plan_frame()
    return start[None, :] + along[:, None] * along_unit[None, :] + across[:, None] * across_unit[None, :]


def lattice(along: np.ndarray, across: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The lattice of the given offsets: the flat offsets of each point, and two triangles per cell.

    Every cell is split along the same diagonal (from its low-along, low-across corner to its high corner).
    """
    along_grid, across_grid = np.meshgrid(along, across, indexing="ij")
    index = np.arange(along.size * across.size).reshape(along.size, across.size)
    p00 = index[:-1, :-1].ravel()
    p01 = index[:-1, 1:].ravel()
    p10 = index[1:, :-1].ravel()
    p11 = index[1:, 1:].ravel()
    first = np.column_stack([p00, p10, p11])
    second = np.column_stack([p00, p11, p01])
    triangles = np.stack([first, second], axis=1).reshape(-1, 3)
    return along_grid.ravel(), across_grid.ravel(), triangles


def fine_across_offsets() -> np.ndarray:
    """The across offsets of the fine TIN: its grid without the centreline, plus the trench's three lines."""
    grid = np.arange(-FINE_ACROSS_HALF_M, FINE_ACROSS_HALF_M + 1e-9, FINE_ACROSS_STEP_M)
    grid = grid[np.abs(grid) > TRENCH_HALF_M]
    return np.unique(np.concatenate([grid, [-TRENCH_HALF_M, 0.0, TRENCH_HALF_M]]))


def fine_heights(along: np.ndarray, across: np.ndarray) -> np.ndarray:
    """The fine TIN's heights: the cutting, the mound and the hill, all over the rail."""
    distance = np.abs(across)
    cut = np.where(
        distance < TRENCH_HALF_M,
        FLOOR_M,
        FLOOR_M + np.minimum(PLATEAU_M - FLOOR_M, CUT_SLOPE * (distance - TRENCH_HALF_M)),
    )
    mound = MOUND_M * np.exp(
        -((along - MOUND_AT_ALONG_M) ** 2 + (across - MOUND_AT_ACROSS_M) ** 2) / (2.0 * MOUND_SIGMA_M**2)
    )
    hill = HILL_M * np.exp(
        -((along - HILL_AT_ALONG_M) ** 2 / (2.0 * HILL_SIGMA_ALONG_M**2))
        - across**2 / (2.0 * HILL_SIGMA_ACROSS_M**2)
    )
    return rail_height(along) + cut + mound + hill


def fine_surface() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The fine TIN: plan positions (N, E), heights and triangles."""
    along = np.arange(FINE_ALONG_FROM_M, straight_length() + FINE_ALONG_BEYOND_M + 1e-9, FINE_ALONG_STEP_M)
    a, g, triangles = lattice(along, fine_across_offsets())
    return to_ne(a, g), fine_heights(a, g), triangles


def coarse_surface() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The coarse TIN: plan positions (N, E), heights and triangles."""
    along = np.arange(COARSE_ALONG_FROM_M, COARSE_ALONG_TO_M + 1e-9, COARSE_STEP_M)
    across = np.arange(-COARSE_ACROSS_HALF_M, COARSE_ACROSS_HALF_M + 1e-9, COARSE_STEP_M)
    a, g, triangles = lattice(along, across)
    return to_ne(a, g), rail_height(a) + COARSE_LIFT_M, triangles


def straight_length() -> float:
    return plan_frame()[3]


def tin_xml(name: str, description: str, ne: np.ndarray, heights: np.ndarray, triangles: np.ndarray) -> str:
    """A LandXML TIN in EPSG:5514 with positive magnitudes, as the Kralupy file writes them."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">',
        f'<Project name="{name}" desc="{description}"/>',
        '<Units><Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/></Units>',
        '<CoordinateSystem name="S-JTSK / Krovak East North" epsgCode="5514"/>',
        f'<Surfaces><Surface name="{name}"><Definition surfType="TIN"><Pnts>',
    ]
    for index, ((n, e), z) in enumerate(zip(ne, heights, strict=True), start=1):
        lines.append(f'<P id="{index}">{n:.4f} {e:.4f} {z:.4f}</P>')
    lines.append("</Pnts><Faces>")
    for a, b, c in triangles:
        lines.append(f"<F>{a + 1} {b + 1} {c + 1}</F>")
    lines.append("</Faces></Definition></Surface></Surfaces>")
    lines.append("</LandXML>")
    return "\n".join(lines) + "\n"


def orthophoto_frame(ne: np.ndarray) -> tuple[float, float, float, float]:
    """The north-up box of the orthophoto in EPSG:5514 (E, N negative), snapped to whole pixels."""
    east = -ne[:, 1]
    north = -ne[:, 0]
    e_min = np.floor((east.min() - MARGIN_M) / PIXEL_M) * PIXEL_M
    e_max = np.ceil((east.max() + MARGIN_M) / PIXEL_M) * PIXEL_M
    n_min = np.floor((north.min() - MARGIN_M) / PIXEL_M) * PIXEL_M
    n_max = np.ceil((north.max() + MARGIN_M) / PIXEL_M) * PIXEL_M
    return e_min, e_max, n_min, n_max


def orthophoto(e_min: float, e_max: float, n_min: float, n_max: float) -> np.ndarray:
    """The RGB image: 40 m field blocks in five colours, without any marker."""
    width = round((e_max - e_min) / PIXEL_M)
    height = round((n_max - n_min) / PIXEL_M)
    cols = np.arange(width)
    rows = np.arange(height)
    block_x = np.floor(cols * PIXEL_M / BLOCK_M).astype(np.int64)
    block_y = np.floor(rows * PIXEL_M / BLOCK_M).astype(np.int64)
    index = (block_x[None, :] * 7 + block_y[:, None] * 13 + (block_x[None, :] * block_y[:, None]) % 5) % 5
    return PALETTE[index]


def draw_marker(image: np.ndarray, e_min: float, n_max: float) -> None:
    """A white "F" at the marker's plan position: upright, with north at the top of the image."""
    start, along_unit, across_unit, _ = plan_frame()
    point = start + MARKER_ALONG_M * along_unit + MARKER_ACROSS_M * across_unit
    east = -point[1]
    north = -point[0]
    col = int((east - e_min) / PIXEL_M)
    row = int((n_max - north) / PIXEL_M)
    white = np.array([255, 255, 255], dtype=np.uint8)
    size = MARKER_SIZE_PX
    stroke = MARKER_STROKE_PX
    image[row : row + size, col : col + stroke] = white
    image[row : row + stroke, col : col + size] = white
    middle = row + size // 2 - stroke // 2
    image[middle : middle + stroke, col : col + size * 3 // 4] = white


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fine_ne, fine_h, fine_tri = fine_surface()
    coarse_ne, coarse_h, coarse_tri = coarse_surface()
    fine_xml = tin_xml(
        "Synthetic cutting (T-135 tests)",
        "Synthetic. Generated by tools/make_synthetic_terrain.py from the straight of the Kralupy alignment. "
        "Not survey data.",
        fine_ne,
        fine_h,
        fine_tri,
    )
    coarse_xml = tin_xml(
        "Synthetic coarse surface (T-135 tests)",
        "Synthetic. Generated by tools/make_synthetic_terrain.py. Not survey data.",
        coarse_ne,
        coarse_h,
        coarse_tri,
    )
    e_min, e_max, n_min, n_max = orthophoto_frame(coarse_ne)
    image = orthophoto(e_min, e_max, n_min, n_max)
    draw_marker(image, e_min, n_max)
    world = (
        f"{PIXEL_M:.6f}\n0.000000\n0.000000\n{-PIXEL_M:.6f}\n"
        f"{e_min + PIXEL_M / 2:.6f}\n{n_max - PIXEL_M / 2:.6f}\n"
    )

    fine_path = OUT / "synthetic_tin.xml"
    coarse_path = OUT / "synthetic_tin_coarse.xml"
    png_path = OUT / "synthetic_ortho.png"
    pgw_path = OUT / "synthetic_ortho.pgw"
    write_text(fine_path, fine_xml)
    write_text(coarse_path, coarse_xml)
    write_text(pgw_path, world)
    Image.fromarray(image).save(png_path, format="PNG", optimize=True)

    for path in (fine_path, coarse_path, png_path, pgw_path):
        size = path.stat().st_size
        if size > MAX_BYTES:
            raise SystemExit(f"{path.name} is {size} bytes, over the {MAX_BYTES} byte limit")
        print(f"{path.relative_to(REPO).as_posix()}: {size} bytes")
    print(
        f"fine points: {fine_ne.shape[0]}, coarse points: {coarse_ne.shape[0]}, "
        f"orthophoto: {image.shape[1]} x {image.shape[0]} px"
    )


if __name__ == "__main__":
    main()
