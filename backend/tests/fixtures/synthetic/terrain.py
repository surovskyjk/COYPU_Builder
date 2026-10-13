"""Analytic synthetic terrain over the Kralupy KRAV82 area, and a test-only point file format.

The surface is a gentle plane plus one Gaussian hill and one linear ridge, so every M3 test can check a
sampled height against `surface(e, n)` exactly. It lies over Kralupy stations 12.7-17.5 km and stays within
a few metres of the rail there (the bridge at about km 16.7 dips below it), so cut/fill against it has
known, non-trivial answers. Nothing here is derived from real survey data.

    z(e, n) = BASE_H + GRADIENT_E (e - E0) + GRADIENT_N (n - N0)
              + HILL_AMPLITUDE exp(-|p - HILL_CENTRE|^2 / (2 HILL_SIGMA^2))
              + RIDGE_AMPLITUDE exp(-d(p, RIDGE_SEGMENT)^2 / (2 RIDGE_WIDTH^2))

with `d` the distance to the ridge's line segment. All coordinates are EPSG:5514 (S-JTSK / Krovak East North).

The test-only file format (`write_synthetic`, `read_synthetic`) is plain text so a test can build any file
in a few lines: a magic line, an optional `crs:` line, then one `sheet <index> <rows>` block per sheet whose
rows are three raw tokens `t1 t2 h`. For Krovak the tokens follow the LandXML convention of
`io/landxml/dialects.py` ((N, E) order; `krovak_positive` writes the engineering X/Y values (-N, -E)), which
is also the default order of every other CRS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

CRS = "EPSG:5514"
MAGIC = "COYPU-SYNTHETIC-XYZ 1"

BBox = tuple[float, float, float, float]  # min_e, min_n, max_e, max_n

KRAV82_BBOX: BBox = (-747650.0, -1026200.0, -744850.0, -1023800.0)

E0, N0 = -746250.0, -1025000.0
BASE_H = 178.5
GRADIENT_E = -0.0009
GRADIENT_N = -0.0009

HILL_CENTRE = (-746750.0, -1024750.0)
HILL_AMPLITUDE = 4.0
HILL_SIGMA = 140.0

RIDGE_START = (-747300.0, -1025900.0)
RIDGE_END = (-746600.0, -1025300.0)
RIDGE_AMPLITUDE = 2.5
RIDGE_WIDTH = 35.0


def surface(e, n) -> np.ndarray:
    """Terrain height [m] at project coordinates (e, n); scalars or arrays."""
    e = np.asarray(e, dtype=np.float64)
    n = np.asarray(n, dtype=np.float64)
    plane = BASE_H + GRADIENT_E * (e - E0) + GRADIENT_N * (n - N0)
    hill = HILL_AMPLITUDE * np.exp(
        -((e - HILL_CENTRE[0]) ** 2 + (n - HILL_CENTRE[1]) ** 2) / (2.0 * HILL_SIGMA**2)
    )
    ax, ay = RIDGE_START
    dx, dy = RIDGE_END[0] - ax, RIDGE_END[1] - ay
    t = np.clip(((e - ax) * dx + (n - ay) * dy) / (dx * dx + dy * dy), 0.0, 1.0)
    d2 = (e - (ax + t * dx)) ** 2 + (n - (ay + t * dy)) ** 2
    ridge = RIDGE_AMPLITUDE * np.exp(-d2 / (2.0 * RIDGE_WIDTH**2))
    return plane + hill + ridge


def sample_irregular(bbox: BBox, mean_spacing_m: float, seed: int) -> np.ndarray:
    """(n, 3) float64 (E, N, H): one point at a uniformly random position inside every cell of a regular grid
    of `mean_spacing_m` cells, so the mean spacing is `mean_spacing_m` and the cloud is irregular.
    Deterministic for a given `seed`."""
    min_e, min_n, max_e, max_n = bbox
    nx = max(1, int(np.floor((max_e - min_e) / mean_spacing_m)))
    ny = max(1, int(np.floor((max_n - min_n) / mean_spacing_m)))
    cell_e, cell_n = (max_e - min_e) / nx, (max_n - min_n) / ny
    rng = np.random.default_rng(seed)
    ii, jj = np.meshgrid(np.arange(nx), np.arange(ny))
    e = min_e + (ii.ravel() + rng.random(nx * ny)) * cell_e
    n = min_n + (jj.ravel() + rng.random(nx * ny)) * cell_n
    return np.column_stack([e, n, surface(e, n)])


@dataclass(frozen=True)
class GridSample:
    """A regular grid of heights: `z[j, i]` is the height at `(e[i], n[j])`, both axes ascending."""

    e: np.ndarray
    n: np.ndarray
    z: np.ndarray

    def points(self) -> np.ndarray:
        ee, nn = np.meshgrid(self.e, self.n)
        return np.column_stack([ee.ravel(), nn.ravel(), self.z.ravel()])


def sample_grid(bbox: BBox, cell_m: float) -> GridSample:
    """Heights at the nodes `min + k * cell_m` that fall inside `bbox` (both edges included)."""
    min_e, min_n, max_e, max_n = bbox
    e = min_e + cell_m * np.arange(int(np.floor((max_e - min_e) / cell_m + 1e-9)) + 1)
    n = min_n + cell_m * np.arange(int(np.floor((max_n - min_n) / cell_m + 1e-9)) + 1)
    ee, nn = np.meshgrid(e, n)
    return GridSample(e=e, n=n, z=surface(ee, nn))


def split_sheets(bbox: BBox, nx: int, ny: int) -> list[BBox]:
    """`nx` x `ny` sheet boxes tiling `bbox` exactly (neighbours share an edge), row by row from the south."""
    min_e, min_n, max_e, max_n = bbox
    es = np.linspace(min_e, max_e, nx + 1)
    ns = np.linspace(min_n, max_n, ny + 1)
    return [
        (float(es[i]), float(ns[j]), float(es[i + 1]), float(ns[j + 1])) for j in range(ny) for i in range(nx)
    ]


def points_in_bbox(points: np.ndarray, bbox: BBox, *, upper_open: bool = False) -> np.ndarray:
    """Rows of `points` inside `bbox`; with `upper_open` the upper edges are excluded so that adjacent sheets
    partition a cloud without duplicates."""
    min_e, min_n, max_e, max_n = bbox
    if upper_open:
        mask = (
            (points[:, 0] >= min_e)
            & (points[:, 0] < max_e)
            & (points[:, 1] >= min_n)
            & (points[:, 1] < max_n)
        )
    else:
        mask = (
            (points[:, 0] >= min_e)
            & (points[:, 0] <= max_e)
            & (points[:, 1] >= min_n)
            & (points[:, 1] <= max_n)
        )
    return points[mask]


# --- the test-only file format --------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticFile:
    declared_crs: str | None
    sheets: dict[int, np.ndarray]  # raw (rows, 3) tokens (t1, t2, h) by sheet index


def write_synthetic(
    path: Path,
    points: np.ndarray | Sequence[np.ndarray] | Mapping[int, np.ndarray],
    declared_crs: str | None,
    krovak_positive: bool = False,
    order: str = "NE",
) -> Path:
    """Write `points` ((n, 3) E, N, H in the declared CRS; a sequence of such arrays, one per sheet numbered
    from 0; or a mapping of sheet index to array, for files whose sheets are not contiguous).

    Tokens are written in (N, E) order, or (E, N) with `order="EN"`. With `krovak_positive` the values are
    the positive engineering ones (-N, -E) instead. `declared_crs=None` omits the `crs:` line."""
    if isinstance(points, Mapping):
        sheets = {int(k): v for k, v in points.items()}
    elif isinstance(points, np.ndarray) and points.ndim == 2:
        sheets = {0: points}
    else:
        sheets = dict(enumerate(points))
    lines = [MAGIC]
    if declared_crs is not None:
        lines.append(f"crs: {declared_crs}")
    for index, sheet in sheets.items():
        sheet = np.asarray(sheet, dtype=np.float64).reshape(-1, 3)
        e, n, h = sheet[:, 0], sheet[:, 1], sheet[:, 2]
        east, north = (-e, -n) if krovak_positive else (e, n)
        t1, t2 = (east, north) if order == "EN" else (north, east)
        lines.append(f"sheet {index} {len(sheet)}")
        lines.extend(f"{float(a)!r} {float(b)!r} {float(c)!r}" for a, b, c in zip(t1, t2, h, strict=True))
    path = Path(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def is_synthetic(path: Path) -> bool:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            return handle.readline().strip() == MAGIC
    except (OSError, UnicodeDecodeError):
        return False


def read_synthetic(path: Path) -> SyntheticFile:
    with Path(path).open(encoding="utf-8") as handle:
        lines = [line.rstrip("\n") for line in handle]
    if not lines or lines[0] != MAGIC:
        raise ValueError(f"{path} is not a synthetic xyz file")
    declared = None
    sheets: dict[int, np.ndarray] = {}
    i = 1
    if i < len(lines) and lines[i].startswith("crs: "):
        declared = lines[i][len("crs: ") :]
        i += 1
    while i < len(lines):
        header = lines[i].split()
        if len(header) != 3 or header[0] != "sheet":
            raise ValueError(f"line {i + 1}: expected 'sheet <index> <rows>'")
        rows = int(header[2])
        body = lines[i + 1 : i + 1 + rows]
        if len(body) != rows:
            raise ValueError(f"sheet {header[1]}: expected {rows} rows, found {len(body)}")
        sheets[int(header[1])] = np.array(
            [[float(v) for v in row.split()] for row in body], dtype=np.float64
        ).reshape(-1, 3)
        i += 1 + rows
    return SyntheticFile(declared, sheets)
