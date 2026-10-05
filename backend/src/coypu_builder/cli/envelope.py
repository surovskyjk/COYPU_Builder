"""`envelope` command: write the corridor buffer polygon of a LandXML file as GeoJSON or Shapefile."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from pyproj.exceptions import ProjError

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.io.gis.envelope import (
    GEOJSON_SUFFIXES,
    SHAPEFILE_SUFFIX,
    EnvelopeOptions,
    GisExtraMissing,
    check_overwrite,
    export_corridor_envelope,
    validate_output_crs,
)
from coypu_builder.io.landxml.reader import read_landxml, to_alignment


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def run_envelope(
    path: str,
    output: str,
    buffer_m: float,
    alignment_names: Sequence[str],
    station_from: float | None,
    station_to: float | None,
    cap: str,
    epsg: int | None,
    crs: str | None,
    force: bool = False,
) -> int:
    out_path = Path(output)
    if out_path.suffix.lower() not in (*GEOJSON_SUFFIXES, SHAPEFILE_SUFFIX):
        return _fail(f"unsupported output format '{out_path.suffix}'; use .geojson, .json or .shp")
    if not Path(path).is_file():
        return _fail(f"no such file: {path}")
    try:
        raws = read_landxml(path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is reported as one line
        return _fail(f"cannot read {path}: {exc}")
    if not raws:
        return _fail("no <Alignment> found")
    crs_def = crs or (f"EPSG:{raws[0].epsg}" if raws[0].epsg else None)
    if crs_def is None:
        return _fail("file carries no CRS; pass --crs")
    try:
        project_crs = ProjectCRS(crs_def)
    except ProjError as exc:
        return _fail(f"unknown CRS: {exc}")
    try:
        if epsg is not None:
            validate_output_crs(epsg)
    except ValueError as exc:
        return _fail(str(exc))

    alignments = [to_alignment(raw, project_crs).alignment for raw in raws]
    if alignment_names:
        known = {a.name: a for a in alignments}
        missing = [n for n in alignment_names if n not in known]
        if missing:
            return _fail(f"no alignment named {', '.join(map(repr, missing))}; available: {sorted(known)}")
        alignments = [known[n] for n in dict.fromkeys(alignment_names)]

    try:
        options = EnvelopeOptions(
            buffer_m=buffer_m,
            station_from=station_from,
            station_to=station_to,
            cap="flat" if cap == "flat" else "round",
        )
        check_overwrite(out_path, force)
        result = export_corridor_envelope(alignments, options, project_crs, out_path, epsg, overwrite=force)
    except (GisExtraMissing, ValueError, ProjError, OSError) as exc:
        return _fail(str(exc))

    if out_path.suffix.lower() in GEOJSON_SUFFIXES and result.epsg != 4326:
        print(f"warning: EPSG:{result.epsg} GeoJSON is outside RFC 7946 (legacy crs member written)")

    print(f"file:        {path}")
    print(f"crs:         {project_crs}")
    for alignment in alignments:
        s0 = options.station_from if options.station_from is not None else alignment.station_start
        s1 = options.station_to if options.station_to is not None else alignment.station_end
        print(f"alignment:   '{alignment.name}', station {s0:.3f} -> {s1:.3f} m")
    print(f"buffer:      {options.buffer_m:g} m half-width, {options.cap} caps")
    print(f"area:        {result.area_m2 / 1e6:.4f} km2")
    print(f"vertices:    {result.vertex_count}")
    print(f"output crs:  EPSG:{result.epsg}")
    for written in result.files:
        print(f"wrote:       {written}")
    return 0
