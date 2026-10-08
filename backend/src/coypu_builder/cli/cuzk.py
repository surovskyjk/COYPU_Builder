"""`cuzk` command: list and download the ČÚZK map sheets a corridor touches."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ATTRIBUTION = "© ČÚZK (CC BY 4.0)"


def add_cuzk_parser(sub: Any) -> None:
    cuzk = sub.add_parser("cuzk", help="list or download ČÚZK open-data map sheets for a corridor")
    actions = cuzk.add_subparsers(dest="cuzk_command", required=True)
    for name, help_text in (
        ("sheets", "list the sheets a corridor touches, with file sizes"),
        ("download", "download the sheets a corridor touches"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("path", nargs="?", default=None, help="LandXML file (or use --envelope)")
        action.add_argument(
            "--envelope", default=None, help="GeoJSON or Shapefile written by the `envelope` command"
        )
        action.add_argument("--dataset", required=True, help="DMR5G, DMR4G, DMP1G or ORTOFOTO")
        action.add_argument("--buffer", type=float, default=250.0, help="half-width in metres (LandXML)")
        action.add_argument("--from", dest="station_from", type=float, default=None, help="start station [m]")
        action.add_argument("--to", dest="station_to", type=float, default=None, help="end station [m]")
        action.add_argument(
            "--alignment", action="append", default=[], metavar="NAME", help="alignment name; repeatable"
        )
        action.add_argument(
            "--crs", default=None, help="project CRS (EPSG code, WKT or PROJ string); default: from file"
        )
        action.add_argument("--refresh", action="store_true", help="ignore the cached sheet index")
        if name == "download":
            action.add_argument("--out", required=True, help="output directory (outside the repository)")
            action.add_argument("--max-total-mb", type=float, default=500.0, help="refuse larger selections")
            action.add_argument("--concurrency", type=int, default=2, help="simultaneous downloads (1-4)")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def read_envelope_file(path: Path):
    """Read a GeoJSON or Shapefile envelope; returns (shapely geometry, ProjectCRS of its coordinates)."""
    from pyproj import CRS

    from coypu_builder.domain.crs import ProjectCRS
    from coypu_builder.io.gis.envelope import GEOJSON_SUFFIXES, SHAPEFILE_SUFFIX, _require

    shapely = _require("shapely")
    suffix = path.suffix.lower()
    if suffix in GEOJSON_SUFFIXES:
        document = json.loads(path.read_text(encoding="utf-8"))
        crs_name = ((document.get("crs") or {}).get("properties") or {}).get("name")
        crs = ProjectCRS(crs_name if crs_name else "EPSG:4326")
        items = document["features"] if document.get("type") == "FeatureCollection" else [document]
        raw = [item.get("geometry") if item.get("type") == "Feature" else item for item in items]
        geometries = [shapely.geometry.shape(g) for g in raw if g]
    elif suffix == SHAPEFILE_SUFFIX:
        shapefile = _require("shapefile")
        prj = path.with_suffix(".prj")
        if not prj.is_file():
            raise ValueError(f"{prj.name} is missing; the Shapefile carries no CRS")
        crs = ProjectCRS(CRS.from_wkt(prj.read_text(encoding="utf-8")))
        with shapefile.Reader(str(path)) as reader:
            geometries = [shapely.geometry.shape(s.__geo_interface__) for s in reader.shapes()]
    else:
        raise ValueError(f"unsupported envelope format '{path.suffix}'; use .geojson, .json or .shp")
    geometries = [g for g in geometries if not g.is_empty]
    if not geometries:
        raise ValueError(f"{path.name} contains no geometry")
    return shapely.union_all(geometries), crs


def _corridor_from_landxml(args: argparse.Namespace):
    from pyproj.exceptions import ProjError

    from coypu_builder.domain.crs import ProjectCRS
    from coypu_builder.io.gis.envelope import EnvelopeOptions, corridor_envelope
    from coypu_builder.io.landxml.reader import read_landxml, to_alignment

    if not Path(args.path).is_file():
        raise ValueError(f"no such file: {args.path}")
    try:
        raws = read_landxml(args.path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is reported as one line
        raise ValueError(f"cannot read {args.path}: {exc}") from exc
    if not raws:
        raise ValueError("no <Alignment> found")
    crs_def = args.crs or (f"EPSG:{raws[0].epsg}" if raws[0].epsg else None)
    if crs_def is None:
        raise ValueError("file carries no CRS; pass --crs")
    try:
        project_crs = ProjectCRS(crs_def)
    except ProjError as exc:
        raise ValueError(f"unknown CRS: {exc}") from exc
    alignments = [to_alignment(raw, project_crs).alignment for raw in raws]
    if args.alignment:
        known = {a.name: a for a in alignments}
        missing = [n for n in args.alignment if n not in known]
        if missing:
            raise ValueError(
                f"no alignment named {', '.join(map(repr, missing))}; available: {sorted(known)}"
            )
        alignments = [known[n] for n in dict.fromkeys(args.alignment)]
    options = EnvelopeOptions(
        buffer_m=args.buffer, station_from=args.station_from, station_to=args.station_to
    )
    return corridor_envelope(alignments, options), project_crs


def _mb(size: float) -> str:
    return f"{size / 1e6:.2f} MB"


def _select(args: argparse.Namespace, dataset, cache_dir: Path):
    """Index, selection and resolved files for the command line's corridor."""
    from coypu_builder.io.gis import cuzk_atom

    if bool(args.path) == bool(args.envelope):
        raise ValueError("give exactly one of <file.xml> or --envelope <file>")
    if args.envelope:
        try:
            envelope, crs = read_envelope_file(Path(args.envelope))
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - any unreadable envelope is reported as one line
            raise ValueError(f"cannot read envelope {args.envelope}: {type(exc).__name__}: {exc}") from exc
    else:
        envelope, crs = _corridor_from_landxml(args)

    started = time.perf_counter()
    index = cuzk_atom.load_sheet_index(dataset, cache_dir, refresh=args.refresh)
    print(f"index:    {len(index)} {dataset.value} sheets in {time.perf_counter() - started:.2f} s")
    sheets = cuzk_atom.select_sheets(index, envelope, crs)
    files = cuzk_atom.resolve_files(sheets, cache_dir)
    return files


def run_cuzk(args: argparse.Namespace) -> int:
    from pyproj.exceptions import ProjError

    from coypu_builder.io.gis import cuzk_atom
    from coypu_builder.io.gis.envelope import GisExtraMissing

    for stream in (sys.stdout, sys.stderr):  # sheet names carry diacritics a legacy console may lack
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    try:
        dataset = cuzk_atom.CuzkDataset.parse(args.dataset)
        cache_dir = cuzk_atom.default_cache_dir()
        out_dir = Path(args.out) if args.cuzk_command == "download" else None
        if out_dir is not None:
            cuzk_atom.ensure_outside_repository(out_dir)
        files = _select(args, dataset, cache_dir)
        if args.cuzk_command == "sheets":
            return _print_sheets(dataset, files)
        return _run_download(args, files, out_dir)
    except (cuzk_atom.CuzkError, GisExtraMissing, ValueError, ProjError, OSError) as exc:
        return _fail(str(exc))


def _print_sheets(dataset, files: Sequence) -> int:
    print(f"{'code':<9}{'edition':<14}{'size':>11}  name")
    for f in files:
        sheet = f.sheet
        print(f"{sheet.code:<9}{sheet.edition or '-':<14}{_mb(f.length):>11}  {sheet.name}")
    print(f"sheets:   {len(files)}, total {_mb(sum(f.length for f in files))}")
    print(f"source:   {ATTRIBUTION}")
    return 0


def _run_download(args: argparse.Namespace, files: Sequence, out_dir: Path) -> int:
    from coypu_builder.io.gis import cuzk_atom

    if not files:
        print("no sheets selected; nothing to download")
        return 0
    before = {r.file: r.downloaded_at for r in cuzk_atom.read_manifest(out_dir)}
    started = time.perf_counter()
    progress_state: dict[str, int] = {}

    def progress(name: str, done: int, total: int) -> None:
        step = progress_state.get(name, -1)
        if done == total and step != total:
            progress_state[name] = total
            print(f"  {name}  {_mb(total)}", flush=True)

    records = cuzk_atom.download(
        files,
        out_dir,
        max_total_mb=args.max_total_mb,
        concurrency=args.concurrency,
        progress=progress,
    )
    fetched = [r for r in records if before.get(r.file) != r.downloaded_at]
    print(f"sheets:   {len(records)} ({len(fetched)} downloaded, {len(records) - len(fetched)} up to date)")
    print(f"total:    {_mb(sum(r.length for r in records))}")
    print(f"elapsed:  {time.perf_counter() - started:.1f} s")
    print(f"manifest: {out_dir / cuzk_atom.MANIFEST_NAME}")
    print(f"source:   {ATTRIBUTION}")
    return 0
