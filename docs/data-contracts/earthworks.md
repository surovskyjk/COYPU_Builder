# Earthworks: ground line, cut and fill, volumes (T-137, Part A)

What `alignment.ground_profile` returns, and the rules behind it. The code is `domain/earthworks.py` (pure
numpy, no I/O, no protocol), called by the `alignment.ground_profile` handler with the stack's
`heights_and_source`. Every ground query uses the analysis surface of `terrain-tiles.md`; display tiles are
never used here. Stations are absolute metres (`coordinate-conventions.md`).

## Stations

Stations fall on round chainages: `station_from`, then every integer multiple `k · spacing_m` strictly between
the ends, then `station_to`. Multiples are computed as `k · spacing_m`, so nothing drifts. A multiple within
1e-6 m of either end is that end, not a row of its own. A 10 m profile from 12 700 therefore reads 12 700,
12 710, …, whatever the alignment's start station, and only the last row may sit closer than `spacing_m` to its
neighbour. `station_grid` is the domain function; the handler uses it after checking the range.

## Work budget

Before any terrain is queried, a request is refused with `E_BAD_PARAMS` when `rows × samples per section` is
more than 12 000 000 (about 20 s of terrain queries) or `rows` is more than 100 000. The message names the
change to make: a coarser `spacing_m`, a shorter station range, or a coarser `sample_spacing_m`. The Kralupy
line at 1 m with the default template is about 4.4 M samples, so it fits.

## The section

- **Centre and rail height.** The centre of the section at station `s` is the horizontal alignment point,
  read from `alignment.horizontal`. The rail height is the vertical profile's elevation at `s`, read from
  `alignment.vertical`. The code does not call `frames()`: it gives the same values with
  `pivot=CENTERLINE`, but it runs a slow per-station search whenever there is cant (ROADMAP F46). A test asserts
  that the two agree to 1e-9 on a curved, graded and cant-bearing alignment.
- **Across.** The section runs horizontally across the track, perpendicular to the plan heading θ:
  `(E, N) = centre + x · (−sin θ, cos θ)`. Positive offsets `x` are to the left of travel, as in the LRS.
  With the symmetric Phase 1 template the sign is not observable: swapping the two sides leaves every area and
  volume unchanged. It matters from Phase 2's asymmetric sections on.
- **Samples.** Offsets run from `−max_half_width_m` to `+max_half_width_m` at `sample_spacing_m`, and include
  0, `±formation_width_m / 2` and the two limits exactly. `section_offsets` returns them.
- **Cant is ignored** in Phase 1: the formation is level across, and the centre is the centreline, so the
  roll of the track plane does not move the section.

## The formation template

| Parameter | Default | Meaning |
|---|---|---|
| `formation_width_m` (W) | 6.0 | width at formation level, centred on the track axis |
| `formation_depth_m` | 0.8 | rail top to formation level |
| `cut_slope` | 1.5 | horizontal : vertical on a cut side |
| `fill_slope` | 1.5 | horizontal : vertical on a fill side |
| `sample_spacing_m` | 0.5 | across-track sampling for the areas |
| `max_half_width_m` | 60.0 | how far a side looks for its daylight point |

The result echoes the effective template, defaults included (`template`), so a client can draw the formation
level `rail_h − formation_depth_m`. `FormationTemplateDTO`'s defaults equal `FormationTemplate`'s, by a test.

- **Formation level** at station `s`: `elevation(s) − formation_depth_m`, the profile at the rail-top pivot.
  The formation is level across the section.
- **Template.** Between `±W/2` the template is at formation level. Beyond `W/2` each side is classed by the
  ground at its formation edge: **cut** if that ground is at or above formation level, where the template
  rises at `1 / cut_slope`; **fill** otherwise, where it falls at `1 / fill_slope`.
- **Daylight.** Each side is walked outward from its edge, starting after the edge sample, to the first sample
  where its template meets the ground. The crossing is found by linear interpolation between samples. Ground
  exactly at formation level at the edge, rising beyond it, is therefore a cut side that daylights further
  out, not a zero-width one; flat ground at formation level still gives 0.
- **Open side (flag 2).** A side with no daylight point in the ground that exists ends at `max_half_width_m`.

The parameter checks are: each value finite and positive, `max_half_width_m` greater than `W / 2`, and at
most 10 000 samples per side (`max_half_width_m / sample_spacing_m`).

## Areas

- **Cut area** is the area between the ground and the template where the ground is higher:
  `∫ max(0, ground − template) dx`.
- **Fill area** is the area between the template and the ground where the template is higher:
  `∫ max(0, template − ground) dx`.
- The integral runs from the **daylight of one side to the daylight of the other**, piecewise linearly
  between samples. Each pair of neighbouring samples is split at a sign change of `ground − template`, and
  the pair that holds a daylight is cut at its interpolated zero, so a planar ground gives the exact area.
  Beyond a daylight nothing is integrated.
- **Flat ground 2 m above formation** gives the trapezoid `(W + 2 · 1.5 · 2) · 2 = 18 m²` of cut; 2 m below
  gives 18 m² of fill. With `cut_slope` 2 and `fill_slope` 1 the same grounds give 20 m² and 16 m².

## Flags

| Bit | Meaning |
|---|---|
| 1 | no ground under the centreline. It comes alone, or with 8: both areas are 0 and `ground_h` is NaN |
| 2 | no daylight point in the ground that exists on a side. This also holds where the ground ends before `max_half_width_m`, for example at a clip edge, and then comes with flag 4. It does not mean steep terrain |
| 4 | partial: some ground across the section is missing. A pair with a NaN end is skipped; a side whose edge has no ground is skipped entirely |
| 8 | a surface model covers the centreline at a flag-1 station (F57). Flag 1 comes alone, or with 8, and never 8 alone |

A section with ground on only one side is flagged 4, and its areas use only the ground that exists.

## Surface models

A surface model (for example Copernicus GLO-30) follows treetops, rooftops and water surfaces, so it is not
ground. Earthworks sample bare-ground surfaces only: `alignment.ground_profile` calls the stack with
`bare_ground=True`, which skips every surface whose source is marked `surface_model` (`import-framework.md`).
Where no bare ground covers the centreline but a surface model does, the station is flag 1 with flag 8, and
`source` names that surface model. Its `ground_h` stays NaN and its areas stay 0, so volumes and no-ground spans
treat it exactly as flag 1. The result therefore says why there is no ground, and the surface model's height
never becomes a cut or fill. The terrain tiles, the source map and the overlay still use surface models.

## Volumes and no-ground spans

- **Volume by average end area.** For consecutive stations `s_i`, `s_{i+1}` both without flag 1:
  `V = Σ (A_i + A_{i+1}) / 2 · (s_{i+1} − s_i)`. Cut and fill are summed separately. A pair with a flag-1
  station at either end is never used, so a no-ground span is never bridged. The rule is exact for an area
  that changes linearly along the alignment.
- **No-ground spans.** Each run of consecutive flag-1 stations is reported as `[first, last]`: the first and
  the last flag-1 station, so a single such station is `[s, s]`. The ground is unknown between the valid
  station before a span and the valid station after it, and the volumes skip both of those intervals. The
  profile panel hatches from the previous valid station to the next one. For Kralupy at 10 m this is about
  `[16 660, 16 800]`; at 1 m the gap runs from about 16 659 to 16 802. That is the Vltava bridge, where DMR 5G
  has no ground points on the water.

## Source

`source` is the index of the stack surface that answered at the centreline, from
`TerrainStack.heights_and_source` (`-1` where none did); on a flag-8 station it is the surface model's index,
the only answer there. On the wire it is `<u1`, with 255 for none, and it
indexes `source_ids`. `source_names` gives the dataset names in the same order, and `attributions` maps each
source id to its attribution. `terrain_revision` is the revision the profile was computed on, the key the
client caches it under.

## Idealised profile

The Kralupy fixture's vertical profile is idealised: COYPU Feeder had no DMR 5G when it was made. The
numbers therefore show large nominal cut (terrain up to about +17 m over the rail near km 13). That is test
data, not a design error. An alignment is marked `profile_idealised` by its import
(`import.landxml`, `import.coypu`; default `false`) and the flag is returned in `alignment` summaries. It is
never keyed on a fixture name.

## Wire

`alignment.ground_profile` (`experimental`, `mutates: false`, off the event loop): see `docs/protocol/ipc.md`.
