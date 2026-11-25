"""Writes three small synthetic context-data samples for the import dialog (T-136 Part C2).

The dialog's live test and its capture import them. The surface is an analytic formula, so nothing here is
copied from real data. The grid is 64 x 64 cells of 2 m at S-JTSK-like coordinates, with a no-data corner:

- sample_terrain_nocrs.tif: float32 heights with no coordinate system, so the inspection shows the no-CRS
  notice;
- sample_terrain_5514.tif: the same heights, declared as EPSG:5514;
- sample_ortho_5514.tif: three-band uint8 imagery over the same extent, declared as EPSG:5514.

Run through the backend's environment (rasterio and numpy come with the `gis` extra):

    uv run --directory backend python -X utf8 ../tools/write_import_samples.py <out_dir>

The output folder must lie outside the repository; the script refuses one inside it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine, from_origin

REPOSITORY = Path(__file__).resolve().parents[1]
CELLS = 64
CELL_M = 2.0
WEST_E = -742000.0
SOUTH_N = -1040000.0
# The float32 minimum, the sample files' no-data value. It is exactly representable in float32.
NODATA = -3.4028234663852886e38
NODATA_CORNER_CELLS = 8
EPSG_S_JTSK = 5514

NOCRS_NAME = "sample_terrain_nocrs.tif"
TERRAIN_NAME = "sample_terrain_5514.tif"
ORTHO_NAME = "sample_ortho_5514.tif"


def surface() -> np.ndarray:
    """The analytic heights in metres, row 0 on the north edge, as float32."""
    cols = np.arange(CELLS, dtype=np.float64)
    rows = np.arange(CELLS, dtype=np.float64)
    east = WEST_E + (cols[np.newaxis, :] + 0.5) * CELL_M
    north = SOUTH_N + (CELLS - rows[:, np.newaxis] - 0.5) * CELL_M
    height = 210.0 + 6.0 * np.sin(2.0 * np.pi * east / 90.0) + 4.0 * np.cos(2.0 * np.pi * north / 70.0)
    return height.astype(np.float32)


def orthophoto_bands(heights: np.ndarray) -> np.ndarray:
    """Three uint8 bands, a colour ramp over the heights."""
    scaled = (heights - heights.min()) / (heights.max() - heights.min())
    red = 90.0 + 120.0 * scaled
    green = 150.0 - 60.0 * scaled
    blue = 80.0 + 40.0 * (1.0 - scaled)
    return np.clip(np.stack([red, green, blue]), 0.0, 255.0).astype(np.uint8)


def _transform() -> Affine:
    return from_origin(WEST_E, SOUTH_N + CELLS * CELL_M, CELL_M, CELL_M)


def write_terrain(path: Path, heights: np.ndarray, crs: CRS | None) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=CELLS,
        height=CELLS,
        count=1,
        dtype="float32",
        transform=_transform(),
        crs=crs,
        nodata=NODATA,
    ) as dst:
        dst.write(heights, 1)


def write_orthophoto(path: Path, bands: np.ndarray, crs: CRS) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=CELLS,
        height=CELLS,
        count=3,
        dtype="uint8",
        transform=_transform(),
        crs=crs,
    ) as dst:
        dst.write(bands)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic import samples to a folder.")
    parser.add_argument("out_dir", type=Path, help="the folder to write into; created when missing")
    args = parser.parse_args(argv)
    out_dir = args.out_dir.resolve()
    if out_dir.is_relative_to(REPOSITORY):
        print("refused: the sample folder must lie outside the repository", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    clean = surface()
    terrain = clean.copy()
    terrain[:NODATA_CORNER_CELLS, :NODATA_CORNER_CELLS] = NODATA
    crs = CRS.from_epsg(EPSG_S_JTSK)
    write_terrain(out_dir / NOCRS_NAME, terrain, None)
    write_terrain(out_dir / TERRAIN_NAME, terrain, crs)
    write_orthophoto(out_dir / ORTHO_NAME, orthophoto_bands(clean), crs)
    for name in (NOCRS_NAME, TERRAIN_NAME, ORTHO_NAME):
        print(f"wrote {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
