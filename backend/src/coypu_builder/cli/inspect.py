"""`inspect` command: parse a LandXML file headlessly and print what Builder understood."""

from __future__ import annotations

import numpy as np

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sampling import bake_frame_table
from coypu_builder.io.landxml.reader import read_landxml, to_alignment


def run_inspect(path: str, crs: str | None, spacing: float) -> int:
    raws = read_landxml(path)
    if not raws:
        print("no <Alignment> found")
        return 1
    first = raws[0]
    crs_def = crs or (f"EPSG:{first.epsg}" if first.epsg else None)
    if crs_def is None:
        print("file carries no CRS; pass --crs")
        return 1
    project_crs = ProjectCRS(crs_def)
    print(f"file:        {path}")
    print(
        f"application: {first.application_name} {first.application_version} (dialect: {first.dialect.name})"
    )
    print(f"crs:         {project_crs}")
    for raw in raws:
        result = to_alignment(raw, project_crs)
        aln = result.alignment
        print(
            f"\nalignment '{aln.name}': {len(aln.horizontal.segments)} elements, "
            f"station {aln.station_start:.3f} → {aln.station_end:.3f} m (length {aln.length:.3f} m)"
        )
        print(
            f"  profile PVIs: {len(aln.vertical.pvi_stations)}, cant stations: {len(aln.cant.stations)}, "
            f"pivot: {aln.cant.pivot.value}"
        )
        report = result.report
        print(
            f"  end-point closure max: {report.max_end_deviation_m:.6f} m, "
            f"max heading jump: {np.degrees(report.max_heading_jump_rad):.6f}°"
        )
        for warning in report.warnings[:10]:
            print(f"  warning: {warning}")
        table = bake_frame_table(aln, spacing_m=spacing)
        print(
            f"  frame table @ {spacing:g} m: {len(table.station)} rows; "
            f"elevation {table.elevation.min():.2f}–{table.elevation.max():.2f} m; "
            f"|curvature| max {np.abs(table.curvature).max():.6f} 1/m"
        )
    return 0
