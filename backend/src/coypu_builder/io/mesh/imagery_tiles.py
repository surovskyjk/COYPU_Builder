"""Imagery display tiles: composition onto the terrain tile grid, and the JPEG encoding (T-134, Part A).

An imagery tile shares the grid of `io/mesh/terrain_tiles.py`: the same level, `ix`, `iy` and edge. It is
`IMAGE_SIZE` x `IMAGE_SIZE` pixels, so level 0 is 1 m per pixel, and row 0 is the north edge. Imagery layers
are composed first; a pixel neither layer covers is shaded relief from the analysis surface, sampled at the
pixel centres and at a one-pixel border (so the hillshade is defined at the edge). A pixel that neither covers
is transparent and black. The format is described in `docs/data-contracts/imagery-tiles.md`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.imagery import ImageryLayer, compose_layers
from coypu_builder.domain.relief import relief
from coypu_builder.domain.terrain_surface import TerrainStack
from coypu_builder.io.gis.envelope import GisExtraMissing
from coypu_builder.io.mesh.terrain_tiles import check_level, tile_bounds, tile_edge

IMAGE_SIZE = 512
JPEG_QUALITY = 85
HEIGHT_RANGE_SAMPLES = 256  # per side, over the stack's bounds
RELIEF = "relief"  # the source id of pixels that are shaded relief rather than imagery


@dataclass(frozen=True, eq=False)
class ImageryTile:
    level: int
    ix: int
    iy: int
    edge_m: float
    jpeg: bytes  # RGB, IMAGE_SIZE squared, quality 85, baseline, no EXIF
    alpha: np.ndarray | None  # (IMAGE_SIZE, IMAGE_SIZE) uint8 0 or 255, or None when every pixel is 255
    source_ids: tuple[str, ...]  # imagery sources that won a pixel, in precedence order, then "relief" if any


def height_range(stack: TerrainStack) -> tuple[float, float] | None:
    """The 2nd and 98th percentile of the finite heights sampled on a `HEIGHT_RANGE_SAMPLES` lattice over the
    stack's bounds; None when there are none. The colour ramp of the relief is this range, for the whole
    imagery revision, so neighbouring tiles agree."""
    if not stack.surfaces:
        return None
    min_e, min_n, max_e, max_n = stack.bounds()
    ee, nn = np.meshgrid(
        np.linspace(min_e, max_e, HEIGHT_RANGE_SAMPLES), np.linspace(min_n, max_n, HEIGHT_RANGE_SAMPLES)
    )
    heights = stack.heights(ee, nn)
    finite = heights[np.isfinite(heights)]
    if finite.size == 0:
        return None
    low, high = np.percentile(finite, [2.0, 98.0])
    return float(low), float(high)


def compose_tile(
    layers: Sequence[ImageryLayer],
    stack: TerrainStack,
    heights_range: tuple[float, float] | None,
    level: int,
    ix: int,
    iy: int,
) -> ImageryTile:
    """The imagery tile at `(level, ix, iy)`: the imagery layers first (`compose_layers`), then shaded relief
    from `stack` where no imagery is valid, with the relief ramp over `heights_range`. Blocking (numpy and
    Pillow release the GIL only in part), so it runs in a worker thread."""
    check_level(level)
    edge = tile_edge(level)
    bounds = tile_bounds(level, ix, iy)
    rgb, valid, winners = compose_layers(layers, bounds, IMAGE_SIZE)
    source_ids = list(winners)
    if heights_range is not None and stack.surfaces:
        low, high = heights_range
        pixel = edge / IMAGE_SIZE
        min_e, _min_n, _max_e, max_n = bounds
        steps = np.arange(-1, IMAGE_SIZE + 1, dtype=np.float64) + 0.5  # pixel centres, plus the border
        ee, nn = np.meshgrid(min_e + steps * pixel, max_n - steps * pixel)
        shade_rgb, shade_valid = relief(stack.heights(ee, nn), pixel, low, high)
        fill = shade_valid & ~valid
        if fill.any():
            rgb[fill] = shade_rgb[fill]
            valid = valid | fill
            source_ids.append(RELIEF)
    rgb[~valid] = 0
    alpha = None if bool(valid.all()) else valid.astype(np.uint8) * 255
    return ImageryTile(
        level=level,
        ix=ix,
        iy=iy,
        edge_m=edge,
        jpeg=encode_jpeg(rgb),
        alpha=alpha,
        source_ids=tuple(dict.fromkeys(source_ids)),
    )


def encode_jpeg(rgb: np.ndarray, quality: int = JPEG_QUALITY) -> bytes:
    """Baseline JPEG of an (h, w, 3) uint8 array, without EXIF. Pillow is imported here, not at load time."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'pillow' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, dtype=np.uint8)).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
