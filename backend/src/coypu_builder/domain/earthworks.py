"""Earthworks: a ground line under the centreline, cut and fill per station from a formation template (T-137).

The section at each station is a line across the track, horizontal and perpendicular to the plan heading,
with positive offsets to the left. The formation is level across at `elevation(s) - formation_depth_m`, the
profile at the centreline (cant is ignored in Phase 1). Each side leaves the formation at its edge, rising at
`1 / cut_slope` where the ground at the edge is at or above formation level and falling at `1 / fill_slope`
otherwise, until the template meets the ground (its daylight point).

Cut is the area between the ground and the template where the ground is higher, fill where the template is
higher, both integrated piecewise linearly between samples from daylight to daylight. The ground is taken as
linear between samples, so a planar ground gives the exact area. The conventions are written up in
`docs/data-contracts/earthworks.md`.

Pure numpy, float64 throughout, and no I/O: the heights come in as a callable
(`TerrainStack.heights_and_source` in the handler).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.terrain_surface import NO_SOURCE

FLAG_NO_GROUND = 1  # no ground under the centreline: no area, ground_h is NaN
FLAG_OPEN_SIDE = 2  # a side whose template finds no daylight within max_half_width_m
FLAG_PARTIAL = 4  # some ground across the section is missing: the areas use only the ground there is

MAX_SAMPLES_PER_SIDE = 10_000  # bounds the cost of one section: max_half_width_m / sample_spacing_m
_CHUNK_POINTS = 200_000  # sample points per call to the heights callable
MAX_PROFILE_STATIONS = 100_000  # the work budget of one profile: stations ...
MAX_PROFILE_SAMPLES = 12_000_000  # ... and terrain samples, stations times samples per section (about 20 s)
_STATION_TOLERANCE_M = 1e-6  # a chainage this close to an end of the range is that end, not a row of its own


@dataclass(frozen=True)
class FormationTemplate:
    formation_width_m: float = 6.0  # at formation level, centred on the track axis
    formation_depth_m: float = 0.8  # rail top to formation level
    cut_slope: float = 1.5  # horizontal : vertical
    fill_slope: float = 1.5
    sample_spacing_m: float = 0.5  # across-track sampling for areas
    max_half_width_m: float = 60.0  # stop searching for a daylight point beyond this offset

    def __post_init__(self) -> None:
        for name in (
            "formation_width_m",
            "formation_depth_m",
            "cut_slope",
            "fill_slope",
            "sample_spacing_m",
            "max_half_width_m",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0.0):
                raise ValueError(f"{name} must be finite and > 0, got {value}")
        if self.max_half_width_m <= self.formation_width_m / 2.0:
            raise ValueError(
                f"max_half_width_m ({self.max_half_width_m}) must exceed half the formation width "
                f"({self.formation_width_m / 2.0})"
            )
        if self.max_half_width_m / self.sample_spacing_m > MAX_SAMPLES_PER_SIDE:
            raise ValueError(
                f"more than {MAX_SAMPLES_PER_SIDE} samples per side: raise sample_spacing_m or lower "
                "max_half_width_m"
            )


@dataclass(frozen=True, eq=False)
class GroundProfile:
    station: np.ndarray  # (n,) absolute stations [m]
    rail_h: np.ndarray  # (n,) profile elevation at the centreline [m]
    ground_h: np.ndarray  # (n,) ground under the centreline [m]; NaN = no ground
    source: np.ndarray  # (n,) int16, index of the stack surface under the centreline; NO_SOURCE where none
    cut_area: np.ndarray  # (n,) [m^2]
    fill_area: np.ndarray  # (n,) [m^2]
    flags: np.ndarray  # (n,) uint8, FLAG_* bits
    cut_volume_m3: float
    fill_volume_m3: float
    no_ground_spans: tuple[tuple[float, float], ...]  # (first, last) station of each run of FLAG_NO_GROUND


Heights = Callable[[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]


def section_offsets(template: FormationTemplate) -> np.ndarray:
    """Across-track offsets from -max_half_width_m to +max_half_width_m at sample_spacing_m, including 0 and
    +-formation_width_m / 2 exactly (and +-max_half_width_m). Strictly increasing."""
    half = template.formation_width_m / 2.0
    limit = template.max_half_width_m
    spacing = template.sample_spacing_m
    tolerance = 1e-9 * spacing
    grid = np.arange(0.0, limit, spacing)
    grid = grid[(np.abs(grid - half) > tolerance) & (limit - grid > tolerance)]
    positive = np.sort(np.concatenate([grid, [half, limit]]))
    return np.concatenate([-positive[:0:-1], positive])


def station_grid(start: float, end: float, spacing_m: float) -> np.ndarray:
    """Stations on round chainages: `start`, then every integer multiple of `spacing_m` strictly between, then
    `end`. Multiples are computed as `k * spacing_m`, so there is no drift. A multiple within 1e-6 m of either
    end is that end, not a row of its own, so only the last row may sit closer than `spacing_m` to its
    neighbour."""
    if not (math.isfinite(start) and math.isfinite(end) and start < end):
        raise ValueError(f"start must be below end, got {start} and {end}")
    if not (math.isfinite(spacing_m) and spacing_m > 0.0):
        raise ValueError(f"spacing_m must be finite and > 0, got {spacing_m}")
    tolerance = _STATION_TOLERANCE_M / spacing_m  # in units of the spacing
    first = math.floor(start / spacing_m + tolerance) + 1
    last = math.ceil(end / spacing_m - tolerance) - 1
    inner = np.arange(first, last + 1, dtype=np.float64) * spacing_m
    return np.concatenate([[start], inner, [end]]).astype(np.float64)


def check_work_budget(station_count: int, template: FormationTemplate) -> None:
    """Refuses a profile before any terrain is queried: at most MAX_PROFILE_STATIONS stations, and at most
    MAX_PROFILE_SAMPLES terrain samples (stations times samples per section). Raises ValueError, naming the
    change that brings the request under the limits."""
    samples = station_count * len(section_offsets(template))
    if station_count > MAX_PROFILE_STATIONS or samples > MAX_PROFILE_SAMPLES:
        raise ValueError(
            f"the profile needs {station_count} stations and {samples} terrain samples; the limits are "
            f"{MAX_PROFILE_STATIONS} stations and {MAX_PROFILE_SAMPLES} samples. Use a coarser spacing_m, a "
            "shorter station range or a coarser sample_spacing_m"
        )


def _index_of(values: np.ndarray, value: float) -> int:
    return int(np.argmin(np.abs(values - value)))


def _linear_parts(width: float, da: float, db: float) -> tuple[float, float]:
    """(cut, fill) over a piece of `width` on which ground minus template runs linearly from `da` to `db`:
    the integrals of max(0, d) and max(0, -d), split exactly at the zero crossing."""
    if da >= 0.0 and db >= 0.0:
        return (da + db) * width / 2.0, 0.0
    if da <= 0.0 and db <= 0.0:
        return 0.0, -(da + db) * width / 2.0
    f = da / (da - db)  # fraction of the piece from `da` to the zero crossing
    if da > 0.0:
        return da * f * width / 2.0, -db * (1.0 - f) * width / 2.0
    return db * (1.0 - f) * width / 2.0, -da * f * width / 2.0


def section_areas(
    offsets: np.ndarray, ground: np.ndarray, formation_h: float, template: FormationTemplate
) -> tuple[float, float, int]:
    """(cut_area, fill_area, flags) of one cross-section; `ground` holds heights at `offsets`, NaN = none.

    Each side is walked outward from its formation edge, starting after the edge sample, to the first finite
    sample where its template meets the ground (flag 2 if there is none within the section). Pairs of
    neighbouring samples with finite ends are integrated from the centre out to that daylight; the pair that
    holds the daylight is cut at its
    interpolated zero. A pair with a NaN end is skipped and sets flag 4. A side whose ground at the edge is
    NaN is skipped as a whole (flag 4). NaN at offset 0 gives (0, 0, flag 1)."""
    x = np.asarray(offsets, dtype=np.float64)
    g = np.asarray(ground, dtype=np.float64)
    if x.ndim != 1 or g.shape != x.shape or x.size < 3 or np.any(np.diff(x) <= 0.0):
        raise ValueError("offsets must be a strictly increasing 1-D array, with one ground height each")
    if not math.isfinite(formation_h):
        raise ValueError(f"formation_h must be finite, got {formation_h}")
    centre = _index_of(x, 0.0)
    if not math.isfinite(g[centre]):
        return 0.0, 0.0, FLAG_NO_GROUND

    half = template.formation_width_m / 2.0
    edges = {1: _index_of(x, half), -1: _index_of(x, -half)}
    template_h = np.full(x.shape, float(formation_h))
    flags = 0
    cut_total = fill_total = 0.0

    for side, edge in edges.items():
        if not math.isfinite(g[edge]):
            flags |= FLAG_PARTIAL
            continue
        cut = g[edge] >= formation_h
        sign = 1.0 if cut else -1.0
        slope = template.cut_slope if cut else template.fill_slope
        order = np.arange(edge, len(x)) if side > 0 else np.arange(edge, -1, -1)
        template_h[order] = formation_h + sign * (np.abs(x[order]) - half) / slope
        d = g[order] - template_h[order]
        s = sign * d  # positive while the ground is on the side of the template the side started on
        hits = (
            np.nonzero(np.isfinite(s[1:]) & (s[1:] <= 0.0))[0] + 1
        )  # the search starts after the edge sample
        found = hits.size > 0
        if found:
            last = int(hits[0])
        else:
            last = len(order) - 1
            flags |= FLAG_OPEN_SIDE
        for k in range(last):
            a, b = int(order[k]), int(order[k + 1])
            if not (math.isfinite(g[a]) and math.isfinite(g[b])):
                flags |= FLAG_PARTIAL
                continue
            width = abs(x[b] - x[a])
            da, db = float(d[k]), float(d[k + 1])
            if found and k == last - 1 and s[k + 1] < 0.0:
                f = da / (da - db)  # the daylight lies inside this pair: integrate only up to it
                width *= f
                db = 0.0
            c, fl = _linear_parts(width, da, db)
            cut_total += c
            fill_total += fl

    for k in range(edges[-1], edges[1]):  # the formation between the two edges; the sides are done above
        a, b = k, k + 1
        if not (math.isfinite(g[a]) and math.isfinite(g[b])):
            flags |= FLAG_PARTIAL
            continue
        c, fl = _linear_parts(x[b] - x[a], g[a] - template_h[a], g[b] - template_h[b])
        cut_total += c
        fill_total += fl
    return cut_total, fill_total, flags


def end_area_volume(stations: np.ndarray, areas: np.ndarray, valid: np.ndarray) -> float:
    """Volume by average end area: the mean of the areas at each pair of consecutive stations, times the
    distance between them. A pair is used only when both of its stations are valid; a station that is not
    valid is never bridged."""
    s = np.asarray(stations, dtype=np.float64)
    a = np.asarray(areas, dtype=np.float64)
    v = np.asarray(valid, dtype=bool)
    if not (s.shape == a.shape == v.shape) or s.ndim != 1:
        raise ValueError("stations, areas and valid must be 1-D arrays of one length")
    pair = v[:-1] & v[1:]
    return float(np.sum(((a[:-1] + a[1:]) / 2.0 * np.diff(s))[pair]))


def no_ground_spans(stations: np.ndarray, no_ground: np.ndarray) -> tuple[tuple[float, float], ...]:
    """Maximal runs of consecutive no-ground stations, as (first station, last station)."""
    s = np.asarray(stations, dtype=np.float64)
    mask = np.asarray(no_ground, dtype=bool)
    if s.shape != mask.shape or s.ndim != 1:
        raise ValueError("stations and no_ground must be 1-D arrays of one length")
    spans: list[tuple[float, float]] = []
    start: int | None = None
    for i, flagged in enumerate(mask):
        if flagged and start is None:
            start = i
        elif not flagged and start is not None:
            spans.append((float(s[start]), float(s[i - 1])))
            start = None
    if start is not None:
        spans.append((float(s[start]), float(s[-1])))
    return tuple(spans)


def section_line(alignment: Alignment, stations: np.ndarray) -> tuple[np.ndarray, ...]:
    """(E, N, across E, across N, rail height) at each station. The centre is the horizontal alignment point,
    and the rail height the vertical profile's elevation, read from `alignment.horizontal` and
    `alignment.vertical` directly. The across direction is horizontal, `(-sin heading, cos heading)`, positive
    to the left. Cant is ignored. `frames()` gives the same centre with `pivot=CENTERLINE`, but it runs a slow
    per-station search when there is cant (F46), which earthworks do not need."""
    s = np.asarray(stations, dtype=np.float64)
    xy = alignment.horizontal.point(s)
    heading = alignment.horizontal.heading(s)
    rail_h = alignment.vertical.elevation(s)
    return xy[:, 0], xy[:, 1], -np.sin(heading), np.cos(heading), rail_h


def ground_profile(
    alignment: Alignment, heights: Heights, stations: np.ndarray, template: FormationTemplate
) -> GroundProfile:
    """The ground line under the centreline and the cut and fill per station, with the volumes between them.

    `heights(e, n)` is `TerrainStack.heights_and_source`: `(heights, source_index)` at the points, NaN where
    there is no ground. The section line is `section_line`, and the formation level is
    `elevation - formation_depth_m`. `stations` must be strictly increasing. The work budget is checked before
    any terrain is queried (`check_work_budget`)."""
    s = np.asarray(stations, dtype=np.float64)
    if s.ndim != 1 or s.size == 0 or np.any(np.diff(s) <= 0.0):
        raise ValueError("stations must be a non-empty, strictly increasing 1-D array")
    check_work_budget(len(s), template)
    centre_e, centre_n, across_e, across_n, rail_h = section_line(alignment, s)
    offsets = section_offsets(template)
    formation_h = rail_h - template.formation_depth_m
    centre = _index_of(offsets, 0.0)

    count = len(s)
    ground_h = np.full(count, np.nan)
    source = np.full(count, NO_SOURCE, dtype=np.int16)
    cut = np.zeros(count)
    fill = np.zeros(count)
    flags = np.zeros(count, dtype=np.uint8)
    rows = max(1, _CHUNK_POINTS // len(offsets))
    for start in range(0, count, rows):
        stop = min(count, start + rows)
        e = (centre_e[start:stop, None] + offsets[None, :] * across_e[start:stop, None]).ravel()
        n = (centre_n[start:stop, None] + offsets[None, :] * across_n[start:stop, None]).ravel()
        h, src = heights(e, n)
        h = np.asarray(h, dtype=np.float64).reshape(stop - start, len(offsets))
        src = np.asarray(src).reshape(stop - start, len(offsets))
        for row in range(stop - start):
            i = start + row
            cut[i], fill[i], flags[i] = section_areas(offsets, h[row], formation_h[i], template)
        ground_h[start:stop] = h[:, centre]
        source[start:stop] = src[:, centre].astype(np.int16)

    valid = (flags & FLAG_NO_GROUND) == 0
    return GroundProfile(
        station=s,
        rail_h=rail_h,
        ground_h=ground_h,
        source=source,
        cut_area=cut,
        fill_area=fill,
        flags=flags,
        cut_volume_m3=end_area_volume(s, cut, valid),
        fill_volume_m3=end_area_volume(s, fill, valid),
        no_ground_spans=no_ground_spans(s, ~valid),
    )
