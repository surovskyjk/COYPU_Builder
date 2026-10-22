"""Method dispatch: envelope params → domain calls → result fields + wire blobs (ADR 0003)."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgspec
import numpy as np
from pyproj.exceptions import ProjError

from coypu_builder import PROTOCOL_VERSION, __version__
from coypu_builder.domain.crs import (
    BasePoint,
    ProjectCRS,
    basis_to_godot,
    points_to_godot,
    quaternion_from_matrix,
)
from coypu_builder.domain.geometry.cant import RotationPivot
from coypu_builder.domain.kinematics import Stop, bake_run_table
from coypu_builder.domain.model.trainset import Trainset
from coypu_builder.domain.model.vehicle import CarSpec, TractionBand, VehicleDynamics, VehicleSpec
from coypu_builder.domain.sampling import bake_frame_table
from coypu_builder.domain.sources import (
    BBoxClip,
    CorridorClip,
    FileOrigin,
    ImportSettings,
    ProviderOrigin,
    SourceKind,
)
from coypu_builder.domain.terrain import TerrainGrid, TerrainPoints, TerrainTin
from coypu_builder.domain.terrain_surface import (
    StackSource,
    TerrainStack,
    source_resolution_m,
    stack_from_sources,
    surface_for_source,
)
from coypu_builder.io.catalogue.vehicles import Catalogue, load_catalogue, resolve
from coypu_builder.io.coypu.archive import CoypuProject, read_coypu
from coypu_builder.io.coypu.kinematics_csv import read_kinematics_csv, read_stops_csv
from coypu_builder.io.coypu.vehicles import dynamics_from_coypu, merge_dynamics
from coypu_builder.io.gis.envelope import (
    EnvelopeOptions,
    GisExtraMissing,
    check_overwrite,
    default_epsg,
    export_corridor_envelope,
    validate_output_crs,
)
from coypu_builder.io.landxml import LandXmlAlignment, read_landxml, to_alignment
from coypu_builder.io.mesh.terrain_tiles import (
    TerrainTile,
    bake_tile,
    check_level,
    tile_edge,
    tiles_for_coverage,
)
from coypu_builder.io.mesh.track import bake_track_mesh
from coypu_builder.io.providers import cache as provider_cache
from coypu_builder.io.providers import register_builtin_providers
from coypu_builder.io.sources.registry import InspectReport, plugin_report
from coypu_builder.io.sources.runner import (
    CrsRequired,
    NoAlignments,
    ReaderFailure,
    UnknownAlignment,
    clip_polygon,
    inspect_provider,
    inspect_source,
    pin_provider_source,
    run_import,
    run_provider_import,
)
from coypu_builder.io.terrain import register_builtin_readers
from coypu_builder.protocol.messages import (
    AlignmentEnvelopeParams,
    AlignmentEnvelopeResult,
    AlignmentFrameTableParams,
    AlignmentFrameTableResult,
    AlignmentSummary,
    AlignmentTrackMeshParams,
    AlignmentTrackMeshResult,
    BasePointDTO,
    BBoxClipDTO,
    CarSpecDTO,
    CatalogueVehiclesResult,
    CorridorClipDTO,
    ErrorCode,
    FileOriginDTO,
    ImportCoypuParams,
    ImportCoypuResult,
    ImportInspectParams,
    ImportInspectResult,
    ImportKinematicsParams,
    ImportKinematicsResult,
    ImportLandxmlParams,
    ImportLandxmlResult,
    ImportRunParams,
    ImportRunResult,
    ImportSettingsDTO,
    ObjectRecordDTO,
    PluginInfoDTO,
    PluginsListResult,
    ProjectInfoResult,
    ProjectNewParams,
    ProviderOriginDTO,
    RunGetParams,
    RunGetResult,
    RunListResult,
    RunSummary,
    SessionHelloParams,
    SessionHelloResult,
    SessionPingParams,
    SessionPingResult,
    SourceDatasetDTO,
    SourceGetParams,
    SourceListResult,
    SourceSummary,
    StopDTO,
    TerrainTileInfo,
    TerrainTileParams,
    TerrainTileResult,
    TerrainTilesParams,
    TerrainTilesResult,
    TrackMeshChunkInfo,
    TractionBandDTO,
    TrainsetCreateParams,
    TrainsetDTO,
    TrainsetGetParams,
    TrainsetSummary,
    VehicleDynamicsDTO,
    VehicleSpecDTO,
)
from coypu_builder.server.session import (
    AlignmentEntry,
    ProjectState,
    RunEntry,
    Session,
    SourceEntry,
    TerrainCache,
)

register_builtin_readers()
register_builtin_providers()

HandlerResult = tuple[dict[str, Any], dict[str, np.ndarray]]
# A handler is a plain function, or a coroutine function for work that must not block the event loop: the
# server runs those as tasks (ADR 0003, long calls never block the event loop).
Handler = Callable[[Session, "dict[str, Any] | None"], "HandlerResult | Awaitable[HandlerResult]"]


class ProtocolError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def _offload[T](session: Session, fn: Callable[..., T], *args: Any) -> T:
    """Run blocking `fn` in a worker thread so the event loop keeps answering pings (ADR 0003 addendum), with
    at most `MAX_EXECUTING` such threads per connection."""
    async with session.gate:
        return await asyncio.to_thread(fn, *args)


def _convert(params: dict[str, Any] | None, kind: type) -> Any:
    try:
        return msgspec.convert(params or {}, type=kind)
    except msgspec.ValidationError as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc


def _require_hello(session: Session) -> None:
    if not session.hello_received:
        raise ProtocolError(ErrorCode.NO_SESSION, "session.hello must be sent first")


def _require_project(session: Session) -> ProjectState:
    _require_hello(session)
    if session.project is None:
        raise ProtocolError(ErrorCode.NO_PROJECT, "no project open; call project.new first")
    return session.project


def _crs_str(crs: ProjectCRS | None) -> str | None:
    if crs is None:
        return None
    epsg = crs.epsg
    return f"EPSG:{epsg}" if epsg is not None else crs.name


def _base_point_dto(base: BasePoint | None) -> BasePointDTO | None:
    return BasePointDTO(base.easting, base.northing, base.height) if base is not None else None


def _project_info(project: ProjectState) -> ProjectInfoResult:
    summaries = tuple(
        AlignmentSummary(
            alignment_id=alignment_id,
            name=entry.alignment.name,
            mode=entry.alignment.mode.value,
            station_start=entry.alignment.station_start,
            station_end=entry.alignment.station_end,
            length=entry.alignment.length,
            warnings=entry.warnings,
        )
        for alignment_id, entry in project.alignments.items()
    )
    return ProjectInfoResult(
        project_id=project.project_id,
        crs=_crs_str(project.crs),
        base_point=_base_point_dto(project.base_point),
        alignments=summaries,
    )


def _token_matches(expected: str, supplied: str) -> bool:
    """Constant-time comparison; an empty expected token matches nothing, so it never disables the check."""
    if not expected:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def handle_session_hello(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    req = _convert(params, SessionHelloParams)
    if session.expected_token is not None and not _token_matches(session.expected_token, req.token):
        raise ProtocolError(ErrorCode.UNAUTHORIZED, "unauthorized")
    session.hello_received = True
    result = SessionHelloResult(
        session_id=session.session_id, protocol_version=PROTOCOL_VERSION, server_version=__version__
    )
    return msgspec.to_builtins(result), {}


def handle_session_ping(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    req = _convert(params, SessionPingParams)
    result = SessionPingResult(client_time_ms=req.client_time_ms, server_time_ms=time.time_ns() // 1_000_000)
    return msgspec.to_builtins(result), {}


def release_provider_pins(project: ProjectState | None) -> None:
    """Unpin, in the provider caches, the files of every provider source of `project` (T-133). A replaced or
    closed project keeps none of them: the cache may then evict those files again."""
    if project is None:
        return
    for source_id, entry in project.sources.items():
        origin = entry.dataset.origin
        if isinstance(origin, ProviderOrigin):
            cache = provider_cache.ProviderCache(provider_cache.provider_cache_dir(origin.provider_id))
            cache.unpin(source_id)


def handle_project_new(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    req = _convert(params, ProjectNewParams)
    crs = ProjectCRS(req.crs) if req.crs else None
    base = (
        BasePoint(req.base_point.easting, req.base_point.northing, req.base_point.height)
        if req.base_point is not None
        else None
    )
    release_provider_pins(session.project)
    session.project = ProjectState(project_id=str(uuid4()), crs=crs, base_point=base)
    return msgspec.to_builtins(_project_info(session.project)), {}


def handle_project_get(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    return msgspec.to_builtins(_project_info(project)), {}


def _import_alignments(
    project: ProjectState, raws: list[LandXmlAlignment], crs_override: str | None
) -> tuple[list[AlignmentSummary], list[str]]:
    """Shared by `import.landxml` and `import.coypu`: resolve the CRS, convert each raw alignment, register
    it on `project` and pick the project base point from the first one imported. `raws` must be non-empty;
    callers raise `E_EMPTY` themselves so the message can name the file vs. the archive asset."""
    crs = ProjectCRS(crs_override) if crs_override else project.crs
    if crs is None:
        epsg = raws[0].epsg
        if epsg is None:
            raise ProtocolError(ErrorCode.CRS_REQUIRED, "file carries no CRS; pass crs")
        crs = ProjectCRS(f"EPSG:{epsg}")
    if project.crs is None:
        project.crs = crs

    summaries = []
    alignment_ids = []
    for raw in raws:
        conversion = to_alignment(raw, crs)
        aln = conversion.alignment
        alignment_id = str(uuid4())
        project.alignments[alignment_id] = AlignmentEntry(aln, tuple(conversion.report.warnings))
        alignment_ids.append(alignment_id)
        if project.base_point is None:
            start_en = aln.horizontal.point(np.array([aln.station_start]))[0]
            start_h = float(aln.vertical.elevation(np.array([aln.station_start]))[0])
            project.base_point = BasePoint.rounded(float(start_en[0]), float(start_en[1]), start_h)
        summaries.append(
            AlignmentSummary(
                alignment_id=alignment_id,
                name=aln.name,
                mode=aln.mode.value,
                station_start=aln.station_start,
                station_end=aln.station_end,
                length=aln.length,
                warnings=tuple(conversion.report.warnings),
            )
        )
    return summaries, alignment_ids


def _stop_dto(stop: Stop) -> StopDTO:
    return StopDTO(station_m=stop.station_m, dwell_s=stop.dwell_s, name=stop.name)


def _stops_from_coypu(raw_stops: list[Any]) -> tuple[Stop, ...]:
    """`CoypuProject.stops` is the archive's already-decoded `[station_km, dwell_s, name]` rows -- there is
    no `io/` converter for this shape yet (only for the standalone stops CSV), so it is done here; see the
    T-114 closing report."""
    stops = []
    for row in raw_stops:
        station_km, dwell_s = float(row[0]), float(row[1])
        name = str(row[2]) if len(row) > 2 else ""
        stops.append(Stop(station_m=station_km * 1000.0, dwell_s=dwell_s, name=name))
    return tuple(stops)


def _traction_band_dto(band: TractionBand) -> TractionBandDTO:
    return TractionBandDTO(
        v_bottom_ms=band.v_bottom_ms, v_top_ms=band.v_top_ms, b0=band.b0, b1=band.b1, b2=band.b2
    )


def _vehicle_dynamics_dto(dynamics: VehicleDynamics) -> VehicleDynamicsDTO:
    return VehicleDynamicsDTO(
        mass_t=dynamics.mass_t,
        rotating_mass_factor=dynamics.rotating_mass_factor,
        max_speed_ms=dynamics.max_speed_ms,
        brake_decel_ms2=dynamics.brake_decel_ms2,
        max_tractive_force_kn=dynamics.max_tractive_force_kn,
        davis_a=dynamics.davis_a,
        davis_b=dynamics.davis_b,
        davis_c=dynamics.davis_c,
        traction_bands=tuple(_traction_band_dto(b) for b in dynamics.traction_bands),
    )


def _car_spec_dto(car: CarSpec) -> CarSpecDTO:
    return CarSpecDTO(
        name=car.name,
        length_m=car.length_m,
        width_m=car.width_m,
        height_m=car.height_m,
        floor_height_m=car.floor_height_m,
        bogie_pivot_distance_m=car.bogie_pivot_distance_m,
        bogie_wheelbase_m=car.bogie_wheelbase_m,
        wheel_diameter_m=car.wheel_diameter_m,
        mesh=car.mesh,
        color=car.color,
    )


def _vehicle_spec_dto(spec: VehicleSpec) -> VehicleSpecDTO:
    return VehicleSpecDTO(
        key=spec.key,
        name=spec.name,
        mode=spec.mode.value,
        gauge_mm=spec.gauge_mm,
        cars=tuple(_car_spec_dto(c) for c in spec.cars),
        coupling_gap_m=spec.coupling_gap_m,
        aliases=spec.aliases,
        dynamics=_vehicle_dynamics_dto(spec.dynamics) if spec.dynamics is not None else None,
        provenance=spec.provenance,
    )


def _trainset_dto(trainset: Trainset) -> TrainsetDTO:
    return TrainsetDTO(
        trainset_id=trainset.id,
        spec_key=trainset.spec_key,
        name=trainset.name,
        mode=trainset.mode.value,
        gauge_mm=trainset.gauge_mm,
        coupling_gap_m=trainset.coupling_gap_m,
        length_m=trainset.length_m,
        cars=tuple(_car_spec_dto(c) for c in trainset.cars),
    )


def _trainset_summary(trainset: Trainset) -> TrainsetSummary:
    return TrainsetSummary(
        trainset_id=trainset.id,
        spec_key=trainset.spec_key,
        name=trainset.name,
        mode=trainset.mode.value,
        car_count=len(trainset.cars),
        length_m=trainset.length_m,
    )


def _run_summary(run_id: str, entry: RunEntry) -> RunSummary:
    run = entry.run
    return RunSummary(
        run_id=run_id,
        name=run.name,
        alignment_id=entry.alignment_id,
        trainset_id=entry.trainset_id,
        direction=int(run.direction),
        station_start=float(run.station_m[0]),
        station_end=float(run.station_m[-1]),
        duration_s=float(run.time_s[-1]),
        sample_count=len(run.station_m),
        stop_count=len(run.stops),
        warnings=run.warnings,
    )


_catalogue_singleton: Catalogue | None = None


def _catalogue() -> Catalogue:
    """The vehicle catalogue is process-wide and immutable; load it once, not per session."""
    global _catalogue_singleton
    if _catalogue_singleton is None:
        _catalogue_singleton = load_catalogue()
    return _catalogue_singleton


def _trainset_for_vehicle(coypu_project: CoypuProject, index: int) -> tuple[Trainset | None, str | None]:
    """A COYPU vehicle name that matches no catalogue entry is a normal outcome (`resolve()`'s contract,
    T-113), not a reason to fail the whole import -- it comes back as a warning instead."""
    matched = dynamics_from_coypu(coypu_project, index)
    if matched is None:
        return None, None
    name, dynamics = matched
    spec = resolve(_catalogue(), name)
    if spec is None:
        return None, f"vehicle {index} ('{name}') has no matching catalogue entry"
    return Trainset.from_spec(merge_dynamics(spec, dynamics)), None


async def handle_import_landxml(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportLandxmlParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    raws = await _offload(session, read_landxml, path)
    if not raws:
        raise ProtocolError(ErrorCode.EMPTY, "file carries no <Alignment> elements")
    summaries, _alignment_ids = _import_alignments(project, raws, req.crs)
    return msgspec.to_builtins(ImportLandxmlResult(tuple(summaries))), {}


def _read_coypu_bundle(path: Path):
    """Blocking: everything `import.coypu` reads from the archive, with no project state touched. Returns the
    LandXML alignments, the stops, and per kinematics run its run, trainset and catalogue warning."""
    coypu_project = read_coypu(path)
    xml_texts = [text for name, text in coypu_project.raw_assets.items() if name.endswith(".xml")]
    if not xml_texts:
        raise ProtocolError(ErrorCode.EMPTY, "archive carries no embedded assets/*.xml LandXML")
    raws = [raw for text in xml_texts for raw in read_landxml(text.encode("utf-8"))]
    if not raws:
        raise ProtocolError(ErrorCode.EMPTY, "embedded LandXML carries no <Alignment> elements")
    stops = _stops_from_coypu(coypu_project.stops)
    runs = []
    for index, run in enumerate(coypu_project.kinematics_runs(stops=stops)):
        trainset, warning = _trainset_for_vehicle(coypu_project, index)
        runs.append((run, trainset, warning))
    return raws, stops, runs


async def handle_import_coypu(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportCoypuParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    raws, stops, bundle = await _offload(session, _read_coypu_bundle, path)
    alignment_summaries, alignment_ids = _import_alignments(project, raws, req.crs)
    sole_alignment_id = alignment_ids[0] if len(alignment_ids) == 1 else None

    project.stops = stops

    warnings: list[str] = []
    for summary in alignment_summaries:
        warnings.extend(summary.warnings)

    run_summaries = []
    trainset_summaries = []
    for run, trainset, warning in bundle:
        if warning is not None:
            warnings.append(warning)
        trainset_id = None
        if trainset is not None:
            project.trainsets[trainset.id] = trainset
            trainset_id = trainset.id
            trainset_summaries.append(_trainset_summary(trainset))
        run_id = str(uuid4())
        entry = RunEntry(run=run, source="coypu", alignment_id=sole_alignment_id, trainset_id=trainset_id)
        project.runs[run_id] = entry
        run_summaries.append(_run_summary(run_id, entry))
        warnings.extend(run.warnings)

    result = ImportCoypuResult(
        alignments=tuple(alignment_summaries),
        runs=tuple(run_summaries),
        trainsets=tuple(trainset_summaries),
        stops=tuple(_stop_dto(s) for s in stops),
        warnings=tuple(warnings),
    )
    return msgspec.to_builtins(result), {}


def _read_kinematics(path: Path, stops_path: Path | None):
    stops: tuple[Stop, ...] = ()
    if stops_path is not None:
        stops = read_stops_csv(stops_path)
    return read_kinematics_csv(path, stops=stops)


async def handle_import_kinematics(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportKinematicsParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    stops_path = None
    if req.stops_path is not None:
        stops_path = Path(req.stops_path)
        if not stops_path.is_file():
            raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {stops_path}")
    if req.alignment_id is not None and req.alignment_id not in project.alignments:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{req.alignment_id}'")
    run = await _offload(session, _read_kinematics, path, stops_path)
    run_id = str(uuid4())
    entry = RunEntry(run=run, source="csv", alignment_id=req.alignment_id)
    project.runs[run_id] = entry

    result = ImportKinematicsResult(
        runs=(_run_summary(run_id, entry),),
        stops=tuple(_stop_dto(s) for s in run.stops),
        warnings=run.warnings,
    )
    return msgspec.to_builtins(result), {}


def _clip_from_dto(dto: CorridorClipDTO | BBoxClipDTO | None) -> CorridorClip | BBoxClip | None:
    if dto is None:
        return None
    if isinstance(dto, BBoxClipDTO):
        return BBoxClip(dto.min_e, dto.min_n, dto.max_e, dto.max_n)
    return CorridorClip(dto.buffer_m, tuple(dto.alignment_ids), dto.station_from, dto.station_to)


def _clip_to_dto(clip: CorridorClip | BBoxClip | None) -> CorridorClipDTO | BBoxClipDTO | None:
    if clip is None:
        return None
    if isinstance(clip, BBoxClip):
        return BBoxClipDTO(clip.min_e, clip.min_n, clip.max_e, clip.max_n)
    return CorridorClipDTO(clip.buffer_m, tuple(clip.alignment_ids), clip.station_from, clip.station_to)


def _settings_from_dto(dto: ImportSettingsDTO) -> ImportSettings:
    try:
        return ImportSettings(
            horizontal_crs=dto.horizontal_crs,
            vertical_crs=dto.vertical_crs,
            coordinate_order=dto.coordinate_order,
            linear_unit_m=dto.linear_unit_m,
            clip=_clip_from_dto(dto.clip),
            point_classes=None if dto.point_classes is None else tuple(dto.point_classes),
            display_resolution_m=dto.display_resolution_m,
            nodata=dto.nodata,
        )
    except ValueError as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc


def _settings_to_dto(settings: ImportSettings) -> ImportSettingsDTO:
    return ImportSettingsDTO(
        horizontal_crs=settings.horizontal_crs,
        vertical_crs=settings.vertical_crs,
        coordinate_order=settings.coordinate_order,
        linear_unit_m=settings.linear_unit_m,
        clip=_clip_to_dto(settings.clip),
        point_classes=settings.point_classes,
        display_resolution_m=settings.display_resolution_m,
        nodata=settings.nodata,
    )


def _dataset_dto(entry: SourceEntry) -> SourceDatasetDTO:
    dataset = entry.dataset
    origin = dataset.origin
    origin_dto: FileOriginDTO | ProviderOriginDTO = (
        FileOriginDTO(origin.paths, origin.sha256)
        if isinstance(origin, FileOrigin)
        else ProviderOriginDTO(origin.provider_id, origin.query, origin.sha256)
    )
    return SourceDatasetDTO(
        id=dataset.id,
        kind=dataset.kind.value,
        name=dataset.name,
        format_id=dataset.format_id,
        origin=origin_dto,
        settings=_settings_to_dto(dataset.settings),
        declared_crs=dataset.declared_crs,
        effective_crs=dataset.effective_crs,
        transformation=dataset.transformation,
        transformation_accuracy_m=dataset.transformation_accuracy_m,
        licence=dataset.licence,
        attribution=dataset.attribution,
        imported_at=dataset.imported_at,
        tool_version=dataset.tool_version,
        objects=tuple(ObjectRecordDTO(o.key, o.id, o.content_hash) for o in dataset.objects),
        warnings=entry.warnings,
        resolution_m=entry.resolution_m,
    )


def _source_path(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"path must be absolute, got '{raw}'")
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    return path


def _source_paths(path: str | None, paths: tuple[str, ...] | None) -> list[Path]:
    """The one `path`, or every entry of `paths`; exactly one of the two must be given."""
    if (path is None) == (paths is None):
        raise ProtocolError(ErrorCode.BAD_PARAMS, "give exactly one of 'path', 'paths' and 'provider'")
    raw = [path] if path is not None else list(paths or ())
    if not raw:
        raise ProtocolError(ErrorCode.BAD_PARAMS, "'paths' must not be empty")
    if len(set(raw)) != len(raw):
        raise ProtocolError(ErrorCode.BAD_PARAMS, "'paths' lists a path twice")
    return [_source_path(item) for item in raw]


def _source_kind(raw: str) -> SourceKind:
    try:
        return SourceKind(raw)
    except ValueError as exc:
        known = ", ".join(k.value for k in SourceKind)
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"unknown kind '{raw}' (one of: {known})") from exc


def _inspect_result(report: InspectReport) -> ImportInspectResult:
    return ImportInspectResult(
        format_id=report.format_id,
        kind=report.kind.value,
        declared_crs=report.declared_crs,
        extent=None if report.extent is None else tuple(float(v) for v in report.extent),
        counts={str(k): int(v) for k, v in report.counts.items()},
        suggested_settings=_settings_to_dto(report.suggested),
        licence_hint=report.licence_hint,
        warnings=tuple(report.warnings),
    )


def _check_source_choice(
    path: str | None, paths: tuple[str, ...] | None, provider: str | None, query: dict[str, Any] | None
) -> None:
    if provider is not None and (path is not None or paths is not None):
        raise ProtocolError(ErrorCode.BAD_PARAMS, "give exactly one of 'path', 'paths' and 'provider'")
    if provider is None and query is not None:
        raise ProtocolError(ErrorCode.BAD_PARAMS, "'query' belongs to a 'provider' request")


def _provider_query(query: dict[str, Any] | None) -> dict[str, Any]:
    return dict(query or {})


def _provider_error(exc: Exception) -> ProtocolError:
    """The error code for what a provider inspect or import raised: the same mapping as for files, plus the
    corridor-clip cases."""
    if isinstance(exc, ProtocolError):
        return exc
    if isinstance(exc, CrsRequired):
        return ProtocolError(ErrorCode.CRS_REQUIRED, str(exc))
    if isinstance(exc, UnknownAlignment):
        return ProtocolError(ErrorCode.NOT_FOUND, str(exc))
    if isinstance(exc, NoAlignments):
        return ProtocolError(ErrorCode.EMPTY, str(exc))
    if isinstance(exc, (GisExtraMissing, ReaderFailure)):
        return ProtocolError(ErrorCode.INTERNAL, str(exc))
    if isinstance(exc, (ValueError, OSError, ProjError)):
        return ProtocolError(ErrorCode.BAD_PARAMS, str(exc))
    return ProtocolError(ErrorCode.INTERNAL, f"import failed: {type(exc).__name__}: {exc}")


def _project_with_crs(session: Session) -> ProjectState:
    project = _require_project(session)
    if project.crs is None:
        raise ProtocolError(
            ErrorCode.CRS_REQUIRED,
            "project has no CRS; import an alignment or open the project with a crs first",
        )
    return project


async def _inspect_provider(session: Session, req: ImportInspectParams) -> HandlerResult:
    project = _project_with_crs(session)
    plugin_report()
    alignments = {alignment_id: entry.alignment for alignment_id, entry in project.alignments.items()}
    try:
        _provider, report = await _offload(
            session, inspect_provider, req.provider, _provider_query(req.query), project.crs, alignments
        )
        result = _inspect_result(report)
    except Exception as exc:  # mapped to a wire error, never a dropped connection
        raise _provider_error(exc) from exc
    return msgspec.to_builtins(result), {}


async def handle_import_inspect(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    req = _convert(params, ImportInspectParams)
    _check_source_choice(req.path, req.paths, req.provider, req.query)
    if req.provider is not None:
        return await _inspect_provider(session, req)
    paths = _source_paths(req.path, req.paths)
    plugin_report()  # allow-listed plugin readers must be registered before one is looked up
    try:
        _reader, report = await _offload(session, inspect_source, paths, req.format_id)
        result = _inspect_result(report)
    except (GisExtraMissing, ReaderFailure) as exc:
        raise ProtocolError(ErrorCode.INTERNAL, str(exc)) from exc
    except (ValueError, OSError, ProjError) as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc
    except Exception as exc:  # a plugin's malformed answer must not drop the connection
        raise ProtocolError(ErrorCode.INTERNAL, f"import failed: {type(exc).__name__}: {exc}") from exc
    return msgspec.to_builtins(result), {}


def _run_import_with_coverage(paths, kind, settings, project_crs, alignments, name, format_id, previous):
    """Blocking: the import and the clip polygon it used (the source's coverage), from a snapshot of the
    alignments. Touches no project state."""
    outcome = run_import(
        paths, kind, settings, project_crs, alignments, name=name, format_id=format_id, previous=previous
    )
    return outcome, clip_polygon(settings.clip, alignments)


def _run_provider_with_coverage(
    provider_id, query, kind, settings, project_crs, alignments, name, format_id, previous
):
    """Blocking: a provider import (fetch, read, reconcile) and the clip polygon the readers used."""
    outcome = run_provider_import(
        provider_id,
        query,
        kind,
        settings,
        project_crs,
        alignments,
        name=name,
        format_id=format_id,
        previous=previous,
    )
    return outcome, clip_polygon(outcome.dataset.settings.clip, alignments)


async def handle_import_run(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportRunParams)
    _check_source_choice(req.path, req.paths, req.provider, req.query)
    paths = [] if req.provider is not None else _source_paths(req.path, req.paths)
    plugin_report()
    kind = _source_kind(req.kind)
    settings = _settings_from_dto(req.settings)
    previous: SourceEntry | None = None
    if req.reimport_of is not None:
        previous = project.sources.get(req.reimport_of)
        if previous is None:
            raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown source_id '{req.reimport_of}'")
    if project.crs is None:
        raise ProtocolError(
            ErrorCode.CRS_REQUIRED,
            "project has no CRS; import an alignment or open the project with a crs first",
        )
    alignments = {alignment_id: entry.alignment for alignment_id, entry in project.alignments.items()}
    if req.provider is not None:
        try:
            outcome, coverage = await _offload(
                session,
                _run_provider_with_coverage,
                req.provider,
                _provider_query(req.query),
                kind,
                settings,
                project.crs,
                alignments,
                req.name,
                req.format_id,
                previous.dataset if previous is not None else None,
            )
        except Exception as exc:  # mapped to a wire error, never a dropped connection
            raise _provider_error(exc) from exc
        return _store_import(project, outcome, coverage)
    try:
        outcome, coverage = await _offload(
            session,
            _run_import_with_coverage,
            paths,
            kind,
            settings,
            project.crs,
            alignments,
            req.name,
            req.format_id,
            previous.dataset if previous is not None else None,
        )
    except CrsRequired as exc:
        raise ProtocolError(ErrorCode.CRS_REQUIRED, str(exc)) from exc
    except UnknownAlignment as exc:
        raise ProtocolError(ErrorCode.NOT_FOUND, str(exc)) from exc
    except NoAlignments as exc:
        raise ProtocolError(ErrorCode.EMPTY, str(exc)) from exc
    except (GisExtraMissing, ReaderFailure) as exc:
        raise ProtocolError(ErrorCode.INTERNAL, str(exc)) from exc
    except (ValueError, OSError, ProjError) as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc
    except Exception as exc:  # a plugin's malformed answer must not drop the connection
        raise ProtocolError(ErrorCode.INTERNAL, f"import failed: {type(exc).__name__}: {exc}") from exc
    return _store_import(project, outcome, coverage)


def _store_import(project: ProjectState, outcome, coverage) -> HandlerResult:
    """On the event loop: put an import into the project and answer."""
    if coverage is not None:
        import shapely

        shapely.prepare(coverage)  # here on the loop, so worker threads never prepare it concurrently
    dataset = outcome.dataset
    project.sources[dataset.id] = SourceEntry(
        dataset, outcome.payloads, outcome.warnings, coverage, outcome.resolution_m
    )
    project.invalidate_terrain()
    pin_provider_source(outcome)  # on the loop: a connection that closed mid-import pins nothing

    recon = outcome.reconciliation
    result = ImportRunResult(
        source_id=dataset.id,
        name=dataset.name,
        effective_crs=dataset.effective_crs,
        transformation=dataset.transformation,
        transformation_accuracy_m=dataset.transformation_accuracy_m,
        added=len(recon.added),
        changed=len(recon.changed),
        unchanged=len(recon.unchanged),
        removed=len(recon.removed),
        warnings=outcome.warnings,
    )
    return msgspec.to_builtins(result), {}


def handle_source_list(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    summaries = tuple(
        SourceSummary(
            id=entry.dataset.id,
            kind=entry.dataset.kind.value,
            name=entry.dataset.name,
            format_id=entry.dataset.format_id,
            object_count=len(entry.dataset.objects),
            imported_at=entry.dataset.imported_at,
            attribution=entry.dataset.attribution,
        )
        for entry in project.sources.values()
    )
    return msgspec.to_builtins(SourceListResult(sources=summaries)), {}


def handle_source_get(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, SourceGetParams)
    entry = project.sources.get(req.source_id)
    if entry is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown source_id '{req.source_id}'")
    return msgspec.to_builtins(_dataset_dto(entry)), {}


def handle_plugins_list(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    infos, warnings = plugin_report()
    result = PluginsListResult(
        plugins=tuple(
            PluginInfoDTO(i.name, i.group, i.distribution, i.version, i.state, i.error) for i in infos
        ),
        warnings=warnings,
    )
    return msgspec.to_builtins(result), {}


async def handle_alignment_frame_table(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, AlignmentFrameTableParams)
    entry = project.alignments.get(req.alignment_id)
    if entry is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{req.alignment_id}'")
    pivot = RotationPivot(req.pivot) if req.pivot else None
    base = project.base_point or BasePoint(0.0, 0.0, 0.0)
    alignment = entry.alignment

    def work() -> HandlerResult:
        table = bake_frame_table(alignment, spacing_m=req.spacing_m, pivot=pivot)
        position = points_to_godot(table.origin, base, dtype=np.float32)
        rotation = quaternion_from_matrix(basis_to_godot(table.tangent, table.left, table.up)).astype(
            np.float32
        )
        blobs = {
            "station": table.station.astype(np.float32),
            "position": position,
            "rotation": rotation,
            "roll": table.roll.astype(np.float32),
            "pitch": table.pitch.astype(np.float32),
            "cant_mm": table.cant_mm.astype(np.float32),
            "curvature": table.curvature.astype(np.float32),
            "gradient": table.gradient.astype(np.float32),
            "elevation": table.elevation.astype(np.float32),
            "segment_index": table.segment_index,
        }
        result = AlignmentFrameTableResult(
            alignment_id=req.alignment_id,
            row_count=len(table),
            station_start=float(table.station[0]),
            station_end=float(table.station[-1]),
        )
        return msgspec.to_builtins(result), blobs

    return await _offload(session, work)


async def handle_alignment_track_mesh(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, AlignmentTrackMeshParams)
    entry = project.alignments.get(req.alignment_id)
    if entry is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{req.alignment_id}'")
    if req.chunk_length_m <= 0.0:
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"chunk_length_m must be > 0, got {req.chunk_length_m}")
    if req.spacing_m <= 0.0:
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"spacing_m must be > 0, got {req.spacing_m}")
    base = project.base_point or BasePoint(0.0, 0.0, 0.0)
    alignment = entry.alignment

    def work() -> HandlerResult:
        mesh_chunks = bake_track_mesh(alignment, chunk_length_m=req.chunk_length_m, spacing_m=req.spacing_m)
        if req.chunk_index is not None and not req.metadata_only:
            mesh_chunks = tuple(c for c in mesh_chunks if c.chunk_index == req.chunk_index)
            if not mesh_chunks:
                raise ProtocolError(ErrorCode.NOT_FOUND, f"no chunk with chunk_index {req.chunk_index}")

        infos: list[TrackMeshChunkInfo] = []
        blobs: dict[str, np.ndarray] = {}
        for chunk in mesh_chunks:
            tile_origin_godot = points_to_godot(chunk.tile_origin[None, :], base, dtype=np.float64)[0]
            infos.append(
                TrackMeshChunkInfo(
                    chunk_index=chunk.chunk_index,
                    surface=chunk.surface,
                    station_start=chunk.station_start,
                    station_end=chunk.station_end,
                    tile_origin=(
                        float(tile_origin_godot[0]),
                        float(tile_origin_godot[1]),
                        float(tile_origin_godot[2]),
                    ),
                    vertex_count=len(chunk.vertices),
                    index_count=len(chunk.indices),
                )
            )
            if req.metadata_only:
                continue
            suffix = f"{chunk.chunk_index}_{chunk.surface}"
            blobs[f"vertices_{suffix}"] = chunk.vertices
            blobs[f"normals_{suffix}"] = chunk.normals
            blobs[f"uvs_{suffix}"] = chunk.uvs
            blobs[f"indices_{suffix}"] = chunk.indices

        result = AlignmentTrackMeshResult(alignment_id=req.alignment_id, chunks=tuple(infos))
        return msgspec.to_builtins(result), blobs

    return await _offload(session, work)


# --- terrain display tiles (T-132) -----------------------------------------------------------------------

MAX_LISTED_TILES = 5000  # about 140-170 bytes each as JSON: keeps a `terrain.tiles` answer under 1 MB


def _source_coverage(coverage, bounds: tuple[float, float, float, float]):
    """The area a source is expected to cover: its clip polygon cut to the extent of its data, else that
    extent. None when the clip and the data do not meet."""
    if coverage is None:
        return bounds
    from shapely import box

    area = coverage.intersection(box(*bounds))
    return None if area.is_empty else area


def _clip_test(coverage):
    """A source's clip polygon as the `inside(e, n)` callable that `tin_from_points` expects; None when the
    source was imported without a clip."""
    if coverage is None:
        return None
    from shapely import intersects_xy

    def inside(e: np.ndarray, n: np.ndarray) -> np.ndarray:
        return intersects_xy(coverage, e, n)

    return inside


def _terrain_snapshot(project: ProjectState) -> tuple[tuple, ...]:
    """`(source_id, name, payloads, clip polygon, resolution_m, recency, from_provider)` of each terrain
    source, oldest first: what a worker thread builds from, taken on the event loop so an import cannot change
    the dicts under it. `recency` is the import order; precedence is decided from it, the resolution and
    `from_provider` in `_build_terrain` (T-133, ADR 0009 decision 6)."""
    snapshot = []
    for recency, (source_id, entry) in enumerate(project.sources.items()):
        if entry.dataset.kind != SourceKind.TERRAIN:
            continue
        payloads = [
            entry.payloads[o.id]
            for o in entry.dataset.objects
            if isinstance(entry.payloads.get(o.id), (TerrainPoints, TerrainGrid, TerrainTin))
        ]
        if payloads:
            from_provider = isinstance(entry.dataset.origin, ProviderOrigin)
            snapshot.append(
                (
                    source_id,
                    entry.dataset.name,
                    payloads,
                    entry.coverage,
                    entry.resolution_m,
                    recency,
                    from_provider,
                )
            )
    return tuple(snapshot)


SEAM_SPACING_M = 10.0  # distance between the points that sample a seam
MAX_SEAM_POINTS = 4000  # per source: a long boundary is sampled more sparsely, not more often


def _seam_points(area, resolution_m: float | None) -> np.ndarray | None:
    """Points along the boundary of a source's coverage, a little inside it (twice its resolution, so the
    source has data there): where it meets the sources below it. None when the coverage is too thin."""
    import shapely

    geometry = shapely.box(*area) if isinstance(area, tuple) else area
    inset = max(2.0 * (resolution_m or 1.0), 1.0)
    inner = geometry.buffer(-inset)
    if inner.is_empty:
        return None
    parts = [part for part in shapely.get_parts(inner.boundary) if part.length > 0.0]
    total = sum(part.length for part in parts)
    if total <= 0.0:
        return None
    spacing = max(SEAM_SPACING_M, total / MAX_SEAM_POINTS)
    found = []
    for part in parts:
        count = max(2, int(part.length / spacing))
        distances = np.linspace(0.0, part.length, count, endpoint=False)
        found.append(shapely.get_coordinates(shapely.line_interpolate_point(part, distances)))
    return np.vstack(found)


def _seam_warnings(
    stack: TerrainStack, names: dict[str, str], resolutions: dict[str, float | None], areas
) -> list[str]:
    """For each source and each source below it: the height step along the boundary of the upper one's
    coverage, where both have data (mean and 95th percentile of the absolute difference). The tiles are not
    blended (Phase 1); this tells a user how large the step they will see is."""
    out: list[str] = []
    ids = stack.source_ids
    for upper, upper_id in enumerate(ids):
        area = areas.get(upper_id)
        points = None if area is None else _seam_points(area, resolutions.get(upper_id))
        if points is None:
            continue
        upper_h = stack.surfaces[upper].heights(points[:, 0], points[:, 1])
        for lower in range(upper + 1, len(ids)):
            lower_h = stack.surfaces[lower].heights(points[:, 0], points[:, 1])
            both = np.isfinite(upper_h) & np.isfinite(lower_h)
            if not both.any():
                continue
            step = np.abs(upper_h[both] - lower_h[both])
            out.append(
                f"seam between '{names[upper_id]}' and '{names[ids[lower]]}' (heights are not blended): "
                f"|dH| mean {step.mean():.2f} m, 95th percentile {np.percentile(step, 95):.2f} m "
                f"over {int(both.sum())} points along the boundary of '{names[upper_id]}'"
            )
    return out


def _build_terrain(snapshot) -> tuple[TerrainStack, tuple, tuple[str, ...]]:
    """Blocking: the stack, per-source coverage and warnings. A source that cannot be triangulated is skipped
    with a warning. A source's point triangles are cut to its import clip (centroid test) and to edges of at
    most 30 m. The surfaces are ordered by declared resolution, finest first; within a resolution band a
    user's own file comes before a provider, then the newest import (`stack_from_sources`). The height step
    at each seam between two sources is reported. Runs in a worker thread (the triangulation releases the
    GIL)."""
    sources: list[StackSource] = []
    coverage = []
    warnings = []
    names: dict[str, str] = {}
    resolutions: dict[str, float | None] = {}
    areas: dict[str, object | None] = {}
    for source_id, name, payloads, clip, resolution, recency, from_provider in snapshot:
        try:
            surface = surface_for_source(payloads, inside=_clip_test(clip))
        except ValueError as exc:
            warnings.append(f"terrain source '{name}' skipped: {exc}")
            continue
        if resolution is None:
            resolution = source_resolution_m(payloads)  # a source that was set without an import
        sources.append(StackSource(source_id, surface, resolution, recency, from_provider))
        names[source_id], resolutions[source_id] = name, resolution
        area = _source_coverage(clip, surface.bounds())
        areas[source_id] = area
        if area is not None:
            coverage.append(area)
    stack = stack_from_sources(sources)
    stack.warm_up()  # the locators are built here, once, not by several baking threads at their first query
    warnings.extend(_seam_warnings(stack, names, resolutions, areas))
    return stack, tuple(coverage), tuple(warnings)


async def _build_into(session: Session, project: ProjectState, cache: TerrainCache, snapshot) -> None:
    try:
        async with project.terrain_build_lock:  # one Qhull at a time, so a superseded build is not doubled
            async with session.gate:
                stack, coverage, warnings = await asyncio.to_thread(_build_terrain, snapshot)
    except BaseException:
        cache.build = None
        raise
    # `cache` is this revision's own object: if the sources changed meanwhile it is no longer the project's
    # current cache, so filling it cannot make stale data current.
    cache.stack, cache.coverage, cache.warnings = stack, coverage, warnings
    cache.source_ids = stack.source_ids


async def _ensure_terrain(session: Session, project: ProjectState) -> TerrainCache:
    """The terrain cache current when the request was ordered, with its stack built off the event loop. The
    answer is for that revision even if an import supersedes it while it builds (the client then fetches with
    the old revision, gets `E_NOT_FOUND` and lists again). Concurrent callers for one revision share one
    build, and builds are serialised per project."""
    cache = project.terrain_cache()
    if cache.stack is None:
        if cache.build is None:
            snapshot = _terrain_snapshot(project)  # taken now, before any await
            cache.build = asyncio.ensure_future(_build_into(session, project, cache, snapshot))
            cache.build.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            await asyncio.shield(cache.build)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ProtocolError(
                ErrorCode.INTERNAL, f"terrain build failed: {type(exc).__name__}: {exc}"
            ) from exc
    return cache


async def _bake(session: Session, cache: TerrainCache, level: int, ix: int, iy: int) -> TerrainTile:
    """The tile, from the cache or baked in a worker thread. Concurrent requests for one tile of one
    revision share one bake."""
    tile = cache.get_tile(level, ix, iy)
    if tile is not None:
        return tile
    key = (level, ix, iy)
    future = cache.baking.get(key)
    if future is None:
        future = asyncio.ensure_future(_bake_into(session, cache, level, ix, iy))
        cache.baking[key] = future
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
    return await asyncio.shield(future)


async def _bake_into(session: Session, cache: TerrainCache, level: int, ix: int, iy: int) -> TerrainTile:
    key = (level, ix, iy)
    try:
        async with session.gate:
            tile = await asyncio.to_thread(bake_tile, cache.stack, level, ix, iy)
        cache.put_tile(tile)
        return tile
    finally:
        cache.baking.pop(key, None)


def _level_keys(cache: TerrainCache, level: int) -> tuple[tuple[int, int], ...]:
    keys = cache.level_keys.get(level)
    if keys is None:
        keys = tuple(tiles_for_coverage(level, cache.coverage))
        cache.level_keys[level] = keys
    return keys


def _tile_origin_godot(base: BasePoint, origin: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = points_to_godot(np.array([origin], dtype=np.float64), base, dtype=np.float64)[0]
    return (float(x), float(y), float(z))


def _level(raw: int) -> int:
    try:
        return check_level(raw)
    except ValueError as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc


async def handle_terrain_tiles(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, TerrainTilesParams)
    level = _level(req.level)
    base = project.base_point or BasePoint(0.0, 0.0, 0.0)  # as of this request's place in the order
    cache = await _ensure_terrain(session, project)
    keys = _level_keys(cache, level)
    if len(keys) > MAX_LISTED_TILES:
        raise ProtocolError(
            ErrorCode.BAD_PARAMS,
            f"level {level} has {len(keys)} tiles, more than the {MAX_LISTED_TILES} one answer lists; "
            "ask for a coarser level",
        )
    # tile_origin and valid_fraction come from the baked heights, so the first listing of a level bakes all
    # of its tiles (about 10 ms each at L0 for Kralupy), in worker threads; the metadata is kept for the
    # revision.
    missing = [key for key in keys if (level, *key) not in cache.meta]
    if missing:
        results = await asyncio.gather(
            *(_bake(session, cache, level, ix, iy) for ix, iy in missing), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
    infos = []
    for ix, iy in keys:
        origin, valid_fraction = cache.meta[(level, ix, iy)]
        infos.append(
            TerrainTileInfo(
                level=level,
                ix=ix,
                iy=iy,
                edge_m=tile_edge(level),
                tile_origin=_tile_origin_godot(base, origin),
                valid_fraction=valid_fraction,
            )
        )
    result = TerrainTilesResult(
        level=level,
        revision=cache.revision,
        tiles=tuple(infos),
        warnings=cache.warnings,
        source_ids=cache.source_ids,
    )
    return msgspec.to_builtins(result), {}


async def handle_terrain_tile(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, TerrainTileParams)
    level = _level(req.level)
    base = project.base_point or BasePoint(0.0, 0.0, 0.0)
    stale = ProtocolError(
        ErrorCode.NOT_FOUND, f"terrain revision {req.revision} is out of date; list the tiles again"
    )
    if req.revision != project.terrain_cache().revision:  # no build for a request that is already stale
        raise stale
    cache = await _ensure_terrain(session, project)
    if req.revision != cache.revision:
        raise stale
    if (req.ix, req.iy) not in set(_level_keys(cache, level)):
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no terrain tile {level}/{req.ix}/{req.iy}")
    tile = await _bake(session, cache, level, req.ix, req.iy)
    result = TerrainTileResult(
        level=tile.level,
        ix=tile.ix,
        iy=tile.iy,
        edge_m=tile.edge_m,
        tile_origin=_tile_origin_godot(base, tile.origin),
        valid_fraction=tile.valid_fraction,
        revision=cache.revision,
        source_ids=cache.source_ids,
    )
    return msgspec.to_builtins(result), {"heights": tile.heights, "source_index": tile.source_index}


def _repository_root() -> Path | None:
    """The nearest ancestor of the `coypu_builder` package that holds `.git`; None outside a checkout."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def _checked_output_path(raw: str) -> Path:
    """Envelope output must be an absolute path outside the repository: the server must not be steerable
    into overwriting tracked files (golden data, editor settings) through a relative or in-tree path."""
    path = Path(raw)
    if not path.is_absolute():
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"path must be absolute, got '{raw}'")
    root = _repository_root()
    if root is not None and path.resolve().is_relative_to(root):
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"path must be outside the repository ({root}): '{raw}'")
    return path


async def handle_alignment_envelope(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, AlignmentEnvelopeParams)
    if project.crs is None:
        raise ProtocolError(ErrorCode.CRS_REQUIRED, "project has no CRS; import an alignment first")
    if req.alignment_ids is None:
        entries = list(project.alignments.values())
        if not entries:
            raise ProtocolError(ErrorCode.EMPTY, "project has no alignments")
    else:
        if not req.alignment_ids:
            raise ProtocolError(ErrorCode.BAD_PARAMS, "alignment_ids must not be empty")
        entries = []
        for alignment_id in req.alignment_ids:
            entry = project.alignments.get(alignment_id)
            if entry is None:
                raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{alignment_id}'")
            entries.append(entry)
    if req.cap not in ("round", "flat"):
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"cap must be 'round' or 'flat', got '{req.cap}'")
    out_path = _checked_output_path(req.path)
    if default_epsg(out_path, project.crs) is None and req.epsg is None:
        raise ProtocolError(ErrorCode.BAD_PARAMS, "the project CRS has no EPSG code; pass epsg")
    alignments = [e.alignment for e in entries]
    project_crs = project.crs

    def work():
        if req.epsg is not None:
            validate_output_crs(req.epsg)
        check_overwrite(out_path, req.overwrite)
        options = EnvelopeOptions(
            buffer_m=req.buffer_m,
            station_from=req.station_from,
            station_to=req.station_to,
            cap="flat" if req.cap == "flat" else "round",
        )
        return export_corridor_envelope(
            alignments, options, project_crs, out_path, req.epsg, overwrite=req.overwrite
        )

    try:
        result = await _offload(session, work)
    except GisExtraMissing as exc:
        raise ProtocolError(ErrorCode.INTERNAL, str(exc)) from exc
    except (ValueError, ProjError) as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, str(exc)) from exc
    except OSError as exc:
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"cannot write {out_path}: {exc}") from exc

    wire = AlignmentEnvelopeResult(
        files=[str(f) for f in result.files],
        epsg=result.epsg,
        area_m2=result.area_m2,
        vertex_count=result.vertex_count,
        bounds=list(result.bounds),
    )
    return msgspec.to_builtins(wire), {}


def handle_run_list(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    summaries = tuple(_run_summary(run_id, entry) for run_id, entry in project.runs.items())
    return msgspec.to_builtins(RunListResult(runs=summaries)), {}


def handle_run_get(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, RunGetParams)
    if req.dt <= 0.0:
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"dt must be > 0, got {req.dt}")
    entry = project.runs.get(req.run_id)
    if entry is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown run_id '{req.run_id}'")

    table = bake_run_table(entry.run, dt=req.dt)
    blobs: dict[str, np.ndarray] = {
        "station": table.station.astype(np.float32),
        "speed": table.speed.astype(np.float32),
        "accel": table.accel.astype(np.float32),
    }
    if table.f_traction is not None:
        blobs["f_traction"] = table.f_traction.astype(np.float32)
    if table.f_braking is not None:
        blobs["f_braking"] = table.f_braking.astype(np.float32)
    if table.f_resistance is not None:
        blobs["f_resistance"] = table.f_resistance.astype(np.float32)

    result = RunGetResult(
        run_id=req.run_id,
        dt=table.dt,
        row_count=len(table),
        duration_s=table.duration,
        direction=int(table.direction),
        stops=tuple(_stop_dto(s) for s in table.stops),
    )
    return msgspec.to_builtins(result), blobs


def handle_catalogue_vehicles(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    vehicles = tuple(_vehicle_spec_dto(spec) for spec in _catalogue().values())
    return msgspec.to_builtins(CatalogueVehiclesResult(vehicles=vehicles)), {}


def handle_trainset_create(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, TrainsetCreateParams)
    if req.units < 1:
        raise ProtocolError(ErrorCode.BAD_PARAMS, f"units must be >= 1, got {req.units}")
    spec = resolve(_catalogue(), req.spec_key)
    if spec is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown catalogue vehicle '{req.spec_key}'")
    trainset = Trainset.from_spec(spec, units=req.units, name=req.name)
    project.trainsets[trainset.id] = trainset
    return msgspec.to_builtins(_trainset_dto(trainset)), {}


def handle_trainset_get(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, TrainsetGetParams)
    trainset = project.trainsets.get(req.trainset_id)
    if trainset is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown trainset_id '{req.trainset_id}'")
    return msgspec.to_builtins(_trainset_dto(trainset)), {}


DISPATCH: dict[str, Handler] = {
    "session.hello": handle_session_hello,
    "session.ping": handle_session_ping,
    "project.new": handle_project_new,
    "project.get": handle_project_get,
    "import.landxml": handle_import_landxml,
    "import.coypu": handle_import_coypu,
    "import.kinematics": handle_import_kinematics,
    "import.inspect": handle_import_inspect,
    "import.run": handle_import_run,
    "source.list": handle_source_list,
    "source.get": handle_source_get,
    "plugins.list": handle_plugins_list,
    "alignment.frame_table": handle_alignment_frame_table,
    "alignment.track_mesh": handle_alignment_track_mesh,
    "terrain.tiles": handle_terrain_tiles,
    "terrain.tile": handle_terrain_tile,
    "alignment.envelope": handle_alignment_envelope,
    "run.list": handle_run_list,
    "run.get": handle_run_get,
    "catalogue.vehicles": handle_catalogue_vehicles,
    "trainset.create": handle_trainset_create,
    "trainset.get": handle_trainset_get,
}
