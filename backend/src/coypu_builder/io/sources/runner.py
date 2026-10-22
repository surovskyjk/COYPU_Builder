"""Inspect and import orchestration: CRS resolution, clip geometry, hashing, reconciliation (ADR 0009).

The flow is the one the ADR fixes: `inspect_source` reports what a source declares and proposes settings; the
caller confirms or overrides them; `run_import` resolves the coordinate transform from exactly those settings,
clips, reads, reconciles GUIDs against an earlier import when asked, and returns the `SourceDataset` record
with the payloads. Nothing here touches the project or the wire format.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from uuid import uuid4

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from pyproj.transformer import TransformerGroup

from coypu_builder import __version__
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.model.ids import EntityId, new_id
from coypu_builder.domain.sources import (
    BBoxClip,
    ClipRegion,
    FileOrigin,
    ImportSettings,
    ProviderOrigin,
    Reconciliation,
    SourceDataset,
    SourceKind,
    reconcile,
)
from coypu_builder.domain.terrain import TerrainGrid, TerrainPoints, TerrainTin
from coypu_builder.domain.terrain_surface import source_resolution_m
from coypu_builder.io.gis.envelope import EnvelopeOptions, GisExtraMissing, corridor_envelope
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import canonical_query, clip_from_query
from coypu_builder.io.sources.registry import (
    CrsTransform,
    ImportedObject,
    InspectReport,
    ReaderFailure,
    SourceProvider,
    SourceReader,
    providers,
    reader_for,
)
from coypu_builder.io.terrain.archive import extraction_scope, release

_HASH_CHUNK = 1 << 20
_NOTES: ContextVar[list[str] | None] = ContextVar("import_notes", default=None)
_FETCH_NOTES: ContextVar[list[str] | None] = ContextVar("fetch_notes", default=None)
# The Krovak variants whose positive-valued X/Y axes `resolve_coordinates` turns into East-North values; the
# resolved coordinates are then in the East-North twin of the declared CRS, which is what gets reprojected.
_KROVAK_EAST_NORTH = {5513: 5514, 2065: 5221, 8352: 8353}
UNKNOWN_LICENCE = "unknown"


class CrsRequired(Exception):
    """The source declares no CRS and the settings give none; the handler maps this to `E_CRS_REQUIRED`."""


class UnknownAlignment(LookupError):
    """A corridor clip names an alignment the project does not have."""


class NoAlignments(ValueError):
    """A corridor clip needs at least one alignment and the project has none."""


@dataclass(frozen=True)
class ImportOutcome:
    dataset: SourceDataset
    payloads: dict[EntityId, object]  # keyed by the object's GUID
    reconciliation: Reconciliation
    warnings: tuple[str, ...]
    resolution_m: float | None = None  # terrain: the finest declared resolution of the objects (T-133)
    cache_files: tuple[str, ...] = ()  # provider imports: the cache-relative files read (pinned when stored)


def note(message: str) -> None:
    """Called by a reader while it reads: a warning for the import in progress, for example why a file
    contributed no object. Ignored outside an import."""
    notes = _NOTES.get()
    if notes is not None and message not in notes:
        notes.append(message)


def fetch_note(message: str) -> None:
    """Called by a provider while it fetches: a warning for the import in progress, for example a tile that
    was skipped, or what the data is. It is stored with the source. Ignored outside a provider import."""
    notes = _FETCH_NOTES.get()
    if notes is not None and message not in notes:
        notes.append(message)


def _call_reader[T](label: str, call: Callable[[], T]) -> T:
    try:
        return call()
    except (ValueError, OSError, ProjError, GisExtraMissing, CrsRequired):
        raise
    except Exception as exc:
        raise ReaderFailure(f"{label} failed: {type(exc).__name__}: {exc}") from exc


def as_paths(path: Path | str | Sequence[Path | str]) -> tuple[Path, ...]:
    """One path or a list of them as a tuple of distinct `Path`s. Raises `ValueError` for none or a repeat."""
    items = [Path(path)] if isinstance(path, (str, Path)) else [Path(p) for p in path]
    if not items:
        raise ValueError("no path given")
    seen: set[Path] = set()
    for item in items:
        if item in seen:
            raise ValueError(f"path '{item}' is given twice")
        seen.add(item)
    return tuple(items)


def reader_for_all(paths: Sequence[Path], format_id: str | None = None) -> SourceReader:
    """The one reader that fits every file; files that need different readers are refused."""
    readers = {id(r): r for r in (reader_for(p, format_id) for p in paths)}
    if len(readers) > 1:
        names = ", ".join(sorted(r.format_id for r in readers.values()))
        raise ValueError(f"the files are of different formats ({names}); import one format at a time")
    return next(iter(readers.values()))


def inspect_source(
    path: Path | str | Sequence[Path | str], format_id: str | None = None
) -> tuple[SourceReader, InspectReport]:
    """Pick the reader and ask it what the source holds. A source with no declared CRS gets a warning here,
    whatever the reader says, because import will refuse it unless the settings supply one. Several paths
    are one source: their counts are summed, their extents united, and files declaring different CRSs are
    reported with no declared CRS and a warning."""
    paths = as_paths(path)
    reader = reader_for_all(paths, format_id)
    with extraction_scope():
        report = _merge_reports(paths, [_inspect(reader, p) for p in paths])
    if report.declared_crs is None:
        warning = (
            "the source declares no coordinate reference system; set horizontal_crs in the import settings, "
            "it will not be guessed"
        )
        if warning not in report.warnings:
            report = _replace_warnings(report, (*report.warnings, warning))
    return reader, report


def _merge_reports(paths: Sequence[Path], reports: Sequence[InspectReport]) -> InspectReport:
    if len(reports) == 1:
        return reports[0]
    first = reports[0]
    declared = {r.declared_crs for r in reports}
    warnings: dict[str, list[str]] = {}
    for path, report in zip(paths, reports, strict=True):
        for warning in report.warnings:
            warnings.setdefault(warning, []).append(path.name)
    merged: list[str] = []
    for warning, names in warnings.items():
        if len(names) == len(reports):
            merged.append(warning)
        else:
            shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            merged.append(f"{warning} [{shown}]")
    declared_crs = first.declared_crs
    if len(declared) > 1:
        declared_crs = None
        listing = ", ".join(sorted(d if d is not None else "none" for d in declared))
        merged.append(
            f"the files declare different coordinate reference systems ({listing}); none is assumed"
        )
    counts: dict[str, int] = {}
    for report in reports:
        for key, value in report.counts.items():
            counts[key] = counts.get(key, 0) + int(value)
    extents = [r.extent for r in reports if r.extent is not None]
    extent = (
        (
            min(e[0] for e in extents),
            min(e[1] for e in extents),
            max(e[2] for e in extents),
            max(e[3] for e in extents),
        )
        if extents
        else None
    )
    licences = {r.licence_hint for r in reports}
    suggested = first.suggested
    if suggested.horizontal_crs != declared_crs:
        suggested = replace(suggested, horizontal_crs=declared_crs)
    return InspectReport(
        first.format_id,
        first.kind,
        declared_crs,
        extent,
        counts,
        suggested,
        first.licence_hint if len(licences) == 1 else None,
        tuple(merged),
    )


def _inspect(reader: SourceReader, path: Path) -> InspectReport:
    report = _call_reader(f"reader '{reader.format_id}'", lambda: reader.inspect(path))
    if not isinstance(report, InspectReport) or not isinstance(report.kind, SourceKind):
        raise ReaderFailure(f"reader '{reader.format_id}' returned a malformed inspect report")
    return report


def _replace_warnings(report: InspectReport, warnings: tuple[str, ...]) -> InspectReport:
    return InspectReport(
        report.format_id,
        report.kind,
        report.declared_crs,
        report.extent,
        report.counts,
        report.suggested,
        report.licence_hint,
        warnings,
    )


# --- coordinates -------------------------------------------------------------------------------------------


def _parse_crs(text: str, what: str) -> CRS:
    try:
        return CRS.from_user_input(text)
    except CRSError as exc:
        raise ValueError(f"{what} '{text}' is not a coordinate reference system pyproj understands") from exc


def _horizontal(crs: CRS, what: str, warnings: list[str]) -> CRS:
    if crs.is_compound:
        warnings.append(
            f"{what} is a compound CRS; its vertical part is recorded but heights are not transformed"
        )
        crs = crs.sub_crs_list[0]
    if not (crs.is_projected or crs.is_geographic) or len(crs.axis_info) != 2 or crs.is_vertical:
        raise ValueError(f"{what} must be a horizontal 2D CRS (projected or geographic)")
    return crs


def _crs_string(crs: CRS) -> str:
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg is not None else crs.to_wkt()


def _operation(
    source: CRS, target: CRS, probe: tuple[float, float] | None = None
) -> tuple[str, float | None]:
    """Description and stated accuracy [m] of the operation pyproj applies between two CRSs.

    pyproj's `Transformer.description` stays "unavailable until proj_trans is called" whenever several
    operations compete, so the applied one is identified instead: the candidate in `TransformerGroup` that
    reproduces the result of `Transformer.from_crs` at `probe` (a point in the target CRS, normally where the
    project lies), or failing that at the centre of the target CRS's area of use.
    """
    chosen = Transformer.from_crs(source, target, always_xy=True)
    candidates = TransformerGroup(source, target, always_xy=True).transformers
    point = None
    if probe is not None:
        point = Transformer.from_crs(target, source, always_xy=True).transform(*probe)
    elif target.area_of_use is not None:
        area = target.area_of_use
        lon, lat = (area.west + area.east) / 2.0, (area.south + area.north) / 2.0
        point = Transformer.from_crs("EPSG:4326", source, always_xy=True).transform(lon, lat)
    if point is not None and np.all(np.isfinite(point)):
        reference = chosen.transform(*point)
        for candidate in candidates:
            if np.allclose(candidate.transform(*point), reference, rtol=0.0, atol=1e-6):
                return _described(candidate)
    return _described(candidates[0] if candidates else chosen)


def _described(operation: Transformer) -> tuple[str, float | None]:
    description = operation.description or "unnamed operation"
    accuracy = operation.accuracy
    return description, (float(accuracy) if accuracy is not None and accuracy >= 0.0 else None)


def resolve_transform(
    settings: ImportSettings,
    declared_crs: str | None,
    project_crs: ProjectCRS,
    probe: tuple[float, float] | None = None,
    native_order: str = "NE",
) -> CrsTransform:
    """The coordinate transform for these settings.

    The source CRS is `settings.horizontal_crs`, or failing that `declared_crs`; with neither, `CrsRequired`.
    `probe` is a project-CRS point where the project lies; the recorded operation is the one pyproj applies
    there. `native_order` ("NE" or "EN") is what `coordinate_order="auto"` means for the reader: LandXML
    is "NE", point-cloud and raster readers declare "EN" (a `native_order` attribute on the reader).
    `coordinate_order` is applied through `io/landxml/dialects.py`, the unit through `linear_unit_m`. The
    pyproj operation description and accuracy are recorded, and overriding a declared CRS is warned about.
    """
    chosen = settings.horizontal_crs or declared_crs
    if chosen is None:
        raise CrsRequired(
            "the source declares no coordinate reference system and the import settings give none; "
            "set horizontal_crs"
        )
    warnings: list[str] = []
    source = _horizontal(
        _parse_crs(chosen, "horizontal_crs" if settings.horizontal_crs else "declared CRS"),
        "the source CRS",
        warnings,
    )
    if settings.horizontal_crs is not None and declared_crs is not None:
        try:
            same = _parse_crs(declared_crs, "declared CRS") == _parse_crs(
                settings.horizontal_crs, "horizontal_crs"
            )
        except ValueError:
            same = False
            warnings.append(f"the declared CRS '{declared_crs}' could not be parsed")
        if not same:
            warnings.append(
                f"horizontal_crs '{settings.horizontal_crs}' overrides the CRS the source declares "
                f"('{declared_crs}')"
            )
    if settings.vertical_crs is not None:
        _parse_crs(settings.vertical_crs, "vertical_crs")

    twin = _KROVAK_EAST_NORTH.get(source.to_epsg() or 0)
    source_crs = ProjectCRS(CRS.from_epsg(twin) if twin is not None else source)
    if source_crs == project_crs:
        description, accuracy = "none", 0.0
    else:
        try:
            description, accuracy = _operation(source_crs.crs, project_crs.crs, probe)
        except ProjError as exc:
            raise ValueError(
                f"cannot transform from {_crs_string(source)} to the project CRS: {exc}"
            ) from exc
    return CrsTransform(
        source_crs=source_crs,
        project_crs=project_crs,
        coordinate_order=settings.coordinate_order,
        linear_unit_m=settings.linear_unit_m,
        effective_crs=_crs_string(source),
        declared_crs=declared_crs,
        description=description,
        accuracy_m=None if accuracy is None else float(accuracy),
        warnings=tuple(warnings),
        native_order=native_order,
    )


# --- clip --------------------------------------------------------------------------------------------------


def _shapely():
    try:
        return import_module("shapely")
    except ImportError as exc:
        raise GisExtraMissing(
            "the 'shapely' package is not installed; install the optional GIS dependencies with "
            "`uv sync --extra gis`"
        ) from exc


def clip_polygon(clip: ClipRegion | None, alignments: Mapping[str, Alignment]):
    """The clip as a shapely Polygon or MultiPolygon in the project CRS, or `None` for no clip. A corridor
    goes through the corridor envelope of T-138, so its geometry is the one the portal export shows."""
    if clip is None:
        return None
    if isinstance(clip, BBoxClip):
        return _shapely().box(clip.min_e, clip.min_n, clip.max_e, clip.max_n)
    if clip.alignment_ids:
        missing = [i for i in clip.alignment_ids if i not in alignments]
        if missing:
            raise UnknownAlignment(f"unknown alignment_id '{missing[0]}'")
        chosen = [alignments[i] for i in clip.alignment_ids]
    else:
        chosen = list(alignments.values())
    if not chosen:
        raise NoAlignments("a corridor clip needs at least one alignment, and the project has none")
    options = EnvelopeOptions(clip.buffer_m, station_from=clip.station_from, station_to=clip.station_to)
    return corridor_envelope(chosen, options)


# --- import ------------------------------------------------------------------------------------------------


def _probe(polygon, alignments: Mapping[str, Alignment]) -> tuple[float, float] | None:
    """A project-CRS point inside the area the import concerns: the clip's centroid, else the start of the
    first alignment, else nothing."""
    if polygon is not None:
        centre = polygon.representative_point()
        return float(centre.x), float(centre.y)
    for alignment in alignments.values():
        e, n = alignment.horizontal.point(np.array([alignment.station_start]))[0]
        return float(e), float(n)
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def run_import(
    path: Path | str | Sequence[Path | str],
    kind: SourceKind,
    settings: ImportSettings,
    project_crs: ProjectCRS,
    alignments: Mapping[str, Alignment],
    *,
    name: str | None = None,
    format_id: str | None = None,
    previous: SourceDataset | None = None,
    make_id: Callable[[], EntityId] = new_id,
    provider: tuple[str, str] | None = None,
    licence: str | None = None,
    attribution: str = "",
) -> ImportOutcome:
    """Import one source, a file or a list of files read by the same reader, with the confirmed `settings`.

    With `previous`, objects are matched to that import on their own keys, so matched objects keep their GUID
    and the dataset keeps its id; without it every GUID is new. `previous` must have the same kind and
    format. `provider = (provider_id, canonical query JSON)` records a `ProviderOrigin` (with the file hashes)
    instead of a `FileOrigin`, and `licence` / `attribution` are the provider's; see `run_provider_import`.
    Raises `ValueError` for bad input, `CrsRequired`, `UnknownAlignment`, `NoAlignments`, `GisExtraMissing`,
    `OSError` and `ReaderFailure`.
    """
    paths = as_paths(path)
    for item in paths:
        if not item.is_absolute():
            raise ValueError(f"path must be absolute, got '{item}'")
    reader = reader_for_all(paths, format_id)
    if kind not in reader.kinds:
        raise ValueError(f"reader '{reader.format_id}' does not read {kind.value} sources")
    if previous is not None and (previous.kind != kind or previous.format_id != reader.format_id):
        raise ValueError(
            f"reimport_of is a {previous.kind.value} source read as '{previous.format_id}', "
            f"not {kind.value} as '{reader.format_id}'"
        )

    notes: list[str] = []
    with extraction_scope():
        report = _merge_reports(paths, [_inspect(reader, p) for p in paths])
        polygon = clip_polygon(settings.clip, alignments)
        transform = resolve_transform(
            settings,
            report.declared_crs,
            project_crs,
            _probe(polygon, alignments),
            native_order=getattr(reader, "native_order", "NE"),
        )
        digests = tuple(sha256_file(p) for p in paths)

        def read_all() -> list[ImportedObject]:
            found: list[ImportedObject] = []
            for p in paths:
                found.extend(reader.read(p, settings, transform, polygon))
                release(p)
            return found

        token = _NOTES.set(notes)
        try:
            objects = _call_reader(f"reader '{reader.format_id}'", read_all)
        finally:
            _NOTES.reset(token)
    hashes: dict[str, str] = {}
    payloads_by_key: dict[str, object] = {}
    for item in objects:
        if not (
            isinstance(getattr(item, "key", None), str)
            and isinstance(getattr(item, "content_hash", None), str)
            and hasattr(item, "payload")
        ):
            raise ReaderFailure(
                f"reader '{reader.format_id}' yielded {type(item).__name__}, not an ImportedObject "
                "with a string key and content_hash"
            )
        if item.key in hashes:
            raise ValueError(f"reader '{reader.format_id}' produced the object key '{item.key}' twice")
        hashes[item.key] = item.content_hash
        payloads_by_key[item.key] = item.payload

    reconciliation = reconcile(previous.objects if previous is not None else (), hashes, make_id)
    payloads = {record.id: payloads_by_key[record.key] for record in reconciliation.objects}
    dataset = SourceDataset(
        id=previous.id if previous is not None else make_id(),
        kind=kind,
        name=name or (previous.name if previous is not None else _default_name(paths)),
        format_id=reader.format_id,
        origin=(
            FileOrigin(paths=tuple(str(p) for p in paths), sha256=digests)
            if provider is None
            else ProviderOrigin(provider[0], provider[1], digests)
        ),
        settings=settings,
        declared_crs=report.declared_crs,
        effective_crs=transform.effective_crs,
        transformation=transform.description,
        transformation_accuracy_m=transform.accuracy_m,
        licence=licence or report.licence_hint or UNKNOWN_LICENCE,
        attribution=attribution,
        imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tool_version=f"coypu-builder {__version__}",
        objects=reconciliation.objects,
    )
    warnings = [*transform.warnings, *report.warnings, *notes]
    if not objects:
        warnings.append(
            "the import produced no objects: every file was empty after the class filter, nodata handling "
            "and clip (see the warnings above)"
        )
    terrain = [p for p in payloads_by_key.values() if isinstance(p, (TerrainPoints, TerrainGrid, TerrainTin))]
    resolution = source_resolution_m(terrain) if kind == SourceKind.TERRAIN and terrain else None
    return ImportOutcome(dataset, payloads, reconciliation, tuple(warnings), resolution)


def _default_name(paths: Sequence[Path]) -> str:
    """The file's stem; for several files, the folder they share, else the first file's stem."""
    if len(paths) > 1 and len({p.parent for p in paths}) == 1 and paths[0].parent.name:
        return paths[0].parent.name
    return paths[0].stem


# --- providers (T-133)  ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderContext:
    """What a built-in provider needs from the project to turn a query into ground: the project CRS and the
    alignments a corridor clip is built around. Set for the duration of `inspect` and `fetch`."""

    project_crs: ProjectCRS
    alignments: Mapping[str, Alignment]

    def clip_polygon(self, clip: ClipRegion):
        """The clip as a shapely geometry in the project CRS (see `clip_polygon`)."""
        polygon = clip_polygon(clip, self.alignments)
        if polygon is None or polygon.is_empty:
            raise ValueError("the clip region is empty")
        return polygon


_PROVIDER_CONTEXT: ContextVar[ProviderContext | None] = ContextVar("provider_context", default=None)


def provider_context() -> ProviderContext:
    """The context of the provider call in progress. Raises `ValueError` outside one."""
    context = _PROVIDER_CONTEXT.get()
    if context is None:
        raise ValueError("a provider can only be used while a project import is running")
    return context


def find_provider(provider_id: str) -> SourceProvider:
    for provider in providers():
        if provider.provider_id == provider_id:
            return provider
    known = ", ".join(sorted(p.provider_id for p in providers())) or "none registered"
    raise ValueError(f"unknown provider '{provider_id}' (known: {known})")


def _normalised_query(provider: SourceProvider, query: Mapping[str, object]) -> dict[str, object]:
    """The query with the provider's defaults written out, so the recorded request reproduces the import. A
    provider without `normalise_query` keeps the query as given. It must be JSON."""
    normalise = getattr(provider, "normalise_query", None)
    result = dict(normalise(query) if normalise is not None else query)
    try:
        canonical_query(result)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the provider query must be JSON: {exc}") from exc
    return result


def inspect_provider(
    provider_id: str,
    query: Mapping[str, object],
    project_crs: ProjectCRS,
    alignments: Mapping[str, Alignment],
) -> tuple[SourceProvider, InspectReport]:
    """Ask a provider what its `query` selects: sheets or tiles, sizes, extent, warnings and the settings it
    proposes. Network access happens here (index files, never the data), so call it off the event loop."""
    provider = find_provider(provider_id)
    normal = _normalised_query(provider, query)
    token = _PROVIDER_CONTEXT.set(ProviderContext(project_crs, alignments))
    try:
        report = _call_reader(f"provider '{provider_id}'", lambda: provider.inspect(normal))
    finally:
        _PROVIDER_CONTEXT.reset(token)
    if not isinstance(report, InspectReport) or not isinstance(report.kind, SourceKind):
        raise ReaderFailure(f"provider '{provider_id}' returned a malformed inspect report")
    return provider, report


def run_provider_import(
    provider_id: str,
    query: Mapping[str, object],
    kind: SourceKind,
    settings: ImportSettings,
    project_crs: ProjectCRS,
    alignments: Mapping[str, Alignment],
    *,
    name: str | None = None,
    format_id: str | None = None,
    previous: SourceDataset | None = None,
    make_id: Callable[[], EntityId] = new_id,
    cache_dir: Path | None = None,
) -> ImportOutcome:
    """Import what a provider's `query` selects: `fetch` into the provider cache, then the ordinary readers
    and reconciliation of `run_import`.

    The dataset records a `ProviderOrigin` (provider id, canonical JSON of the query with its defaults
    written out, and the SHA-256 of every fetched file) and the provider's licence and attribution. When
    `settings.clip` is None the query's own clip is used, so the readers keep only what the query asked for.
    Re-importing with `previous` fetches again (the cache revalidates what it holds) and reconciles on the
    objects' keys. The fetched files are pinned in the cache for the life of the source.
    Raises what `run_import` does, and `ValueError` for an unknown provider, kind or query.
    """
    provider = find_provider(provider_id)
    if kind not in provider.kinds:
        raise ValueError(f"provider '{provider_id}' does not supply {kind.value} sources")
    normal = _normalised_query(provider, query)
    if settings.clip is None and "clip" in normal:
        settings = replace(settings, clip=clip_from_query(normal, 250.0))
    directory = Path(cache_dir) if cache_dir is not None else provider_cache.provider_cache_dir(provider_id)
    cache = provider_cache.ProviderCache(directory)
    pending = f"pending:{uuid4().hex}"
    token = _PROVIDER_CONTEXT.set(ProviderContext(project_crs, alignments))
    fetch_notes: list[str] = []
    note_token = _FETCH_NOTES.set(fetch_notes)
    try:
        try:
            fetched = _call_reader(f"provider '{provider_id}'", lambda: provider.fetch(normal, directory))
        finally:
            _FETCH_NOTES.reset(note_token)
        if not isinstance(fetched, (list, tuple)) or not fetched:
            raise ReaderFailure(f"provider '{provider_id}' fetched no files")
        paths = [Path(p) for p in fetched]
        files = [cache.relative(p) for p in paths]
        cache.pin(pending, files)
        outcome = run_import(
            paths,
            kind,
            settings,
            project_crs,
            alignments,
            name=name or (None if previous is not None else getattr(provider, "title", provider_id)),
            format_id=format_id or getattr(provider, "format_id", None),
            previous=previous,
            make_id=make_id,
            provider=(provider_id, canonical_query(normal)),
            licence=provider.licence,
            attribution=provider.attribution,
        )
        return replace(outcome, warnings=(*fetch_notes, *outcome.warnings), cache_files=tuple(files))
    finally:
        _PROVIDER_CONTEXT.reset(token)
        cache.unpin(pending)


def pin_provider_source(outcome: ImportOutcome) -> None:
    """Pin the cache files of a provider import to its dataset, once the import is stored in a project.
    It runs on the event loop, never in the worker thread: a connection closed mid-import keeps no pin."""
    origin = outcome.dataset.origin
    if isinstance(origin, ProviderOrigin) and outcome.cache_files:
        directory = provider_cache.provider_cache_dir(origin.provider_id)
        provider_cache.ProviderCache(directory).pin(outcome.dataset.id, list(outcome.cache_files))
