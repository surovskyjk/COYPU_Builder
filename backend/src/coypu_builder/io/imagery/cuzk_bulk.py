"""The ČÚZK orthophoto sheets provider, `cuzk-ortho-sheets` (T-134, Part B): whole sheets for offline work.

It selects the ORTOFOTO sheets that the query's clip touches (T-139's `select_sheets` over the clip polygon),
downloads them into the provider cache with T-139's `download` and hands the ZIPs to `CuzkSheetReader`. That
reader reads each ZIP as `raster-image` does, but keys the object by the sheet code: a ČÚZK sheet's member is
named by its edition (`WRTO24.2025.KRAV82.jpg`), so `raster-image`'s key would be the edition-prefixed stem.
Nothing is downloaded in bulk without an explicit size: `max_total_mb` defaults to 200 MB, and above it the
download is refused before any sheet is requested, with the total reported. The ČÚZK orthophoto is CC BY 4.0,
attributed "© ČÚZK"; its sheets are EPSG:5514.

Only `io/gis/cuzk_atom.py` talks to ČÚZK feeds and files: the https scheme, the host allow-list and the
`defusedxml` parsing are its. The selection, cache and error mapping are those of the terrain providers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.gis import cuzk_atom
from coypu_builder.io.gis.cuzk_atom import CuzkDataset
from coypu_builder.io.imagery.cuzk_wmts import NOTICE
from coypu_builder.io.imagery.raster_image import RasterImageReader
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import check_keys, clip_from_query, clip_to_query, max_total_mb
from coypu_builder.io.providers.cuzk_terrain import discard_corrupted, service_errors
from coypu_builder.io.sources.registry import CrsTransform, ImportedObject, InspectReport
from coypu_builder.io.sources.runner import fetch_note, provider_context

PROVIDER_ID = "cuzk-ortho-sheets"
FORMAT_ID = "cuzk-ortho-sheet"
_CODE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
HORIZONTAL_CRS = "EPSG:5514"
LICENCE = "CC BY 4.0"
ATTRIBUTION = "© ČÚZK"
TITLE = "ČÚZK ortofoto (sheets)"
DESCRIPTION = (
    "Aerial photos of the Czech Republic, downloaded one map sheet at a time for offline work. "
    "Large: check the size first."
)
DEFAULT_BUFFER_M = 250.0
DEFAULT_MAX_TOTAL_MB = 200.0
QUERY_FIELDS = ("clip", "max_total_mb")
CONCURRENCY = 2
MAX_SHEETS = 200  # a clip that selects more sheets is refused before any sheet is looked up


def sheet_code(path: Path) -> str:
    """The sheet code of a cached sheet file: `KRAV82.WRTO24.2025.zip` is `KRAV82`; the edition follows.
    Raises `ValueError` for a name that does not lead with a code."""
    code = Path(path).name.split(".", 1)[0]
    if not _CODE.match(code):
        raise ValueError(f"'{Path(path).name}' does not start with a ČÚZK sheet code")
    return code


class CuzkSheetReader:
    """A ČÚZK ORTOFOTO sheet ZIP, read as `raster-image` reads it, with its object keyed by the sheet code.
    Only the provider chooses this reader, by `FORMAT_ID`; sniffing never picks it for a named file."""

    format_id = FORMAT_ID
    kinds = frozenset({SourceKind.IMAGERY})
    suffixes = (".zip",)
    native_order = "EN"

    def sniff(self, path: Path) -> bool:
        return False

    def inspect(self, path: Path) -> InspectReport:
        return RasterImageReader().inspect(path)

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> list[ImportedObject]:
        code = sheet_code(path)
        found = RasterImageReader().read(path, settings, transform, clip_polygon)
        return [ImportedObject(code, item.content_hash, item.payload) for item in found]


class CuzkOrthoSheetsProvider:
    provider_id = PROVIDER_ID
    title = TITLE
    description = DESCRIPTION
    kinds = frozenset({SourceKind.IMAGERY})
    licence = LICENCE
    attribution = ATTRIBUTION
    notices: tuple[str, ...] = (NOTICE,)
    format_id = FORMAT_ID

    def normalise_query(self, query: Mapping[str, Any]) -> dict[str, Any]:
        check_keys(query, QUERY_FIELDS)
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        return {"clip": clip_to_query(clip), "max_total_mb": max_total_mb(query, DEFAULT_MAX_TOTAL_MB)}

    def _select(self, query: Mapping[str, Any], directory: Path):
        context = provider_context()
        clip = clip_from_query(query, DEFAULT_BUFFER_M)
        polygon = context.clip_polygon(clip)
        with service_errors():
            index = cuzk_atom.load_sheet_index(CuzkDataset.ORTOFOTO, directory)
            sheets = cuzk_atom.select_sheets(index, polygon, context.project_crs)
        if len(sheets) > MAX_SHEETS:
            raise ValueError(
                f"the clip selects {len(sheets)} ČÚZK orthophoto sheets, more than the {MAX_SHEETS} "
                "one import reads; use a smaller clip"
            )
        return clip, sheets

    def inspect(self, query: Mapping[str, Any]) -> InspectReport:
        directory = provider_cache.provider_cache_dir(self.provider_id)
        clip, sheets = self._select(query, directory)
        limit = max_total_mb(query, DEFAULT_MAX_TOTAL_MB)
        warnings = [
            "the orthophoto sheets are JPEG with world files and carry no CRS: horizontal_crs is EPSG:5514"
        ]
        extent = None
        total = 0
        if sheets:
            with service_errors():
                files = cuzk_atom.resolve_files(sheets, directory)
            total = sum(f.length for f in files)
            lon = np.array([p[0] for s in sheets for p in s.outline_lonlat])
            lat = np.array([p[1] for s in sheets for p in s.outline_lonlat])
            x, y = ProjectCRS(HORIZONTAL_CRS).from_crs("EPSG:4326", lon, lat)
            extent = (float(x.min()), float(y.min()), float(x.max()), float(y.max()))
            warnings.insert(0, f"{len(sheets)} sheet(s), {total / 1e6:.1f} MB to download")
            if total > limit * 1_000_000:
                warnings.append(
                    f"the selection is {total / 1e6:.1f} MB, over max_total_mb {limit:g}: the import will be "
                    "refused unless the limit is raised"
                )
        else:
            warnings.append("no orthophoto sheet overlaps the clip region")
        return InspectReport(
            format_id=self.format_id,
            kind=SourceKind.IMAGERY,
            declared_crs=HORIZONTAL_CRS,
            extent=extent,
            counts={"sheets": len(sheets), "bytes": total},
            suggested=ImportSettings(horizontal_crs=HORIZONTAL_CRS, clip=clip),
            licence_hint=self.licence,
            warnings=tuple(warnings),
        )

    def fetch(self, query: Mapping[str, Any], cache_dir: Path) -> list[Path]:
        directory = Path(cache_dir)
        _clip, sheets = self._select(query, directory)
        if not sheets:
            raise ValueError("no ČÚZK orthophoto sheet overlaps the clip region")
        cache = provider_cache.ProviderCache(directory)
        with service_errors():
            files = cuzk_atom.resolve_files(sheets, directory)
            discard_corrupted(files, directory)
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
        editions = ", ".join(f"{r.code} {r.edition}" for r in records if r.edition)
        if editions:
            fetch_note(f"orthophoto editions: {editions}")
        names = [record.file for record in records]
        cache.touch(names)
        cache.evict(protect=names)
        return [cache.path(name) for name in names]
