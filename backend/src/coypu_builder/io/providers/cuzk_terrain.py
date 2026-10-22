"""The ČÚZK terrain providers, `cuzk-dmr5g` (ground points) and `cuzk-dmr4g` (5 m grid), over T-139.

Both select the SM5 map sheets that the query's clip touches (`select_sheets` over the clip polygon), download
them into the provider cache and hand the ZIPs to the LAS or GeoTIFF reader of T-131. The object key of every
imported object is the sheet code. Heights are Bpv (EPSG:5705); they are recorded, not transformed (ADR 0009).

Only this module and `io/gis/cuzk_atom.py` talk to ČÚZK: the https scheme, the host allow-list and the
`defusedxml` parsing of every feed are `cuzk_atom`'s. The ČÚZK data is CC BY 4.0, attributed "© ČÚZK".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.gis import cuzk_atom
from coypu_builder.io.gis.cuzk_atom import CuzkDataset, CuzkError, CuzkSizeLimitError
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import (
    check_keys,
    clip_from_query,
    clip_to_query,
    max_total_mb,
)
from coypu_builder.io.sources.registry import InspectReport
from coypu_builder.io.sources.runner import provider_context, sha256_file

HORIZONTAL_CRS = "EPSG:5514"
VERTICAL_CRS = "EPSG:5705"
DEFAULT_BUFFER_M = 250.0
DEFAULT_MAX_TOTAL_MB = 500.0
QUERY_FIELDS = ("clip", "max_total_mb")
CONCURRENCY = 2
MAX_SHEETS = 200  # a clip that selects more sheets is refused before any sheet is looked up


@contextmanager
def _service_errors() -> Iterator[None]:
    """What the ATOM client raises, as the exceptions the import handlers know: a limit the user can raise is
    a `ValueError`, a service or network failure an `OSError`."""
    try:
        yield
    except CuzkSizeLimitError as exc:
        raise ValueError(str(exc)) from exc
    except CuzkError as exc:
        raise OSError(f"ČÚZK download service: {exc}") from exc
    except Exception as exc:
        import requests

        if isinstance(exc, requests.RequestException):
            raise OSError(f"ČÚZK download service: {type(exc).__name__}: {exc}") from exc
        raise


class CuzkTerrainProvider:
    kinds = frozenset({SourceKind.TERRAIN})
    licence = "CC BY 4.0"
    attribution = "© ČÚZK"

    def __init__(self, provider_id: str, title: str, dataset: CuzkDataset, format_id: str, note: str):
        self.provider_id = provider_id
        self.title = title
        self.format_id = format_id
        self._dataset = dataset
        self._note = note

    # -- query ---------------------------------------------------------------------------------------------

    def normalise_query(self, query: Mapping[str, Any]) -> dict[str, Any]:
        check_keys(query, QUERY_FIELDS)
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        return {"clip": clip_to_query(clip), "max_total_mb": max_total_mb(query, DEFAULT_MAX_TOTAL_MB)}

    def _select(self, query: Mapping[str, Any], directory: Path):
        context = provider_context()
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        polygon = context.clip_polygon(clip)
        with _service_errors():
            index = cuzk_atom.load_sheet_index(self._dataset, directory)
            sheets = cuzk_atom.select_sheets(index, polygon, context.project_crs)
        if len(sheets) > MAX_SHEETS:
            raise ValueError(
                f"the clip selects {len(sheets)} ČÚZK sheets, more than the {MAX_SHEETS} one import reads; "
                "use a smaller clip"
            )
        return clip, sheets

    # -- the SourceProvider seam -----------------------------------------------------------------------------

    def inspect(self, query: Mapping[str, Any]) -> InspectReport:
        directory = provider_cache.provider_cache_dir(self.provider_id)
        clip, sheets = self._select(query, directory)
        limit = max_total_mb(query, DEFAULT_MAX_TOTAL_MB)
        warnings = [self._note]
        extent = None
        total = 0
        if sheets:
            with _service_errors():
                files = cuzk_atom.resolve_files(sheets, directory)
            total = sum(f.length for f in files)
            lon = np.array([p[0] for s in sheets for p in s.outline_lonlat])
            lat = np.array([p[1] for s in sheets for p in s.outline_lonlat])
            from coypu_builder.domain.crs import ProjectCRS

            x, y = ProjectCRS(HORIZONTAL_CRS).from_crs("EPSG:4326", lon, lat)
            extent = (float(x.min()), float(y.min()), float(x.max()), float(y.max()))
            warnings.insert(0, f"{len(sheets)} sheet(s), {total / 1e6:.1f} MB to download")
            if total > limit * 1_000_000:
                warnings.append(
                    f"the selection is {total / 1e6:.1f} MB, over max_total_mb {limit:g}: the import "
                    "will be refused unless the limit is raised"
                )
        else:
            warnings.append("no sheet overlaps the clip region")
        return InspectReport(
            format_id=self.format_id,
            kind=SourceKind.TERRAIN,
            declared_crs=HORIZONTAL_CRS,
            extent=extent,
            counts={"sheets": len(sheets), "bytes": total},
            suggested=ImportSettings(
                horizontal_crs=HORIZONTAL_CRS, vertical_crs=VERTICAL_CRS, clip=clip, point_classes=None
            ),
            licence_hint=self.licence,
            warnings=tuple(warnings),
        )

    def fetch(self, query: Mapping[str, Any], cache_dir: Path) -> list[Path]:
        directory = Path(cache_dir)
        _clip, sheets = self._select(query, directory)
        if not sheets:
            raise ValueError("no ČÚZK sheet overlaps the clip region")
        cache = provider_cache.ProviderCache(directory)
        with _service_errors():
            files = cuzk_atom.resolve_files(sheets, directory)
            _discard_corrupted(files, directory)
            records = cuzk_atom.download(
                files,
                directory,
                max_total_mb=max_total_mb(query, DEFAULT_MAX_TOTAL_MB),
                concurrency=CONCURRENCY,
            )
        for record in records:
            cache.record(
                record.file,
                url=record.url,
                sha256=record.sha256,
                etag=record.etag,
                last_modified=record.last_modified,
            )
        names = [record.file for record in records]
        cache.touch(names)
        cache.evict(protect=names)
        return [cache.path(name) for name in names]


def _discard_corrupted(files: list[cuzk_atom.SheetFile], directory: Path) -> None:
    """Delete a cached ZIP whose SHA-256 is no longer the one its manifest record holds, so that the download
    fetches it again. A ZIP without a record, or one that is missing, is left to the download."""
    recorded = {record.file: record.sha256 for record in cuzk_atom.read_manifest(directory)}
    for f in files:
        file = f"{f.sheet.dataset.value}/{f.sheet.stem}.zip"
        path = directory / file
        expected = recorded.get(file)
        if expected is not None and path.is_file() and sha256_file(path) != expected:
            path.unlink()


def cuzk_dmr5g_provider() -> CuzkTerrainProvider:
    return CuzkTerrainProvider(
        "cuzk-dmr5g",
        "ČÚZK DMR 5G",
        CuzkDataset.DMR5G,
        "las",
        "DMR 5G is a point cloud of ground key points, all of LAS class 8: keep every class (no filter is "
        "suggested). Heights are Bpv (EPSG:5705) and are not transformed",
    )


def cuzk_dmr4g_provider() -> CuzkTerrainProvider:
    return CuzkTerrainProvider(
        "cuzk-dmr4g",
        "ČÚZK DMR 4G",
        CuzkDataset.DMR4G,
        "geotiff",
        "DMR 4G is a 5 m height grid of the ground. Heights are Bpv (EPSG:5705) and are not transformed",
    )
