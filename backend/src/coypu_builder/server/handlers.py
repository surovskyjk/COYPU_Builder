"""Method dispatch: envelope params → domain calls → result fields + wire blobs (ADR 0003)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgspec
import numpy as np

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
from coypu_builder.io.catalogue.vehicles import Catalogue, load_catalogue, resolve
from coypu_builder.io.coypu.archive import CoypuProject, read_coypu
from coypu_builder.io.coypu.kinematics_csv import read_kinematics_csv, read_stops_csv
from coypu_builder.io.coypu.vehicles import dynamics_from_coypu, merge_dynamics
from coypu_builder.io.landxml import LandXmlAlignment, read_landxml, to_alignment
from coypu_builder.protocol.messages import (
    AlignmentFrameTableParams,
    AlignmentFrameTableResult,
    AlignmentSummary,
    BasePointDTO,
    CarSpecDTO,
    CatalogueVehiclesResult,
    ErrorCode,
    ImportCoypuParams,
    ImportCoypuResult,
    ImportKinematicsParams,
    ImportKinematicsResult,
    ImportLandxmlParams,
    ImportLandxmlResult,
    ProjectInfoResult,
    ProjectNewParams,
    RunGetParams,
    RunGetResult,
    RunListResult,
    RunSummary,
    SessionHelloParams,
    SessionHelloResult,
    SessionPingParams,
    SessionPingResult,
    StopDTO,
    TractionBandDTO,
    TrainsetCreateParams,
    TrainsetDTO,
    TrainsetGetParams,
    TrainsetSummary,
    VehicleDynamicsDTO,
    VehicleSpecDTO,
)
from coypu_builder.server.session import AlignmentEntry, ProjectState, RunEntry, Session

HandlerResult = tuple[dict[str, Any], dict[str, np.ndarray]]
Handler = Callable[[Session, "dict[str, Any] | None"], HandlerResult]


class ProtocolError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


def handle_session_hello(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    req = _convert(params, SessionHelloParams)
    if session.expected_token and req.token != session.expected_token:
        raise ProtocolError(ErrorCode.UNAUTHORIZED, "token mismatch")
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


def handle_project_new(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_hello(session)
    req = _convert(params, ProjectNewParams)
    crs = ProjectCRS(req.crs) if req.crs else None
    base = (
        BasePoint(req.base_point.easting, req.base_point.northing, req.base_point.height)
        if req.base_point is not None
        else None
    )
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


def handle_import_landxml(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportLandxmlParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    raws = read_landxml(path)
    if not raws:
        raise ProtocolError(ErrorCode.EMPTY, "file carries no <Alignment> elements")

    summaries, _alignment_ids = _import_alignments(project, raws, req.crs)
    return msgspec.to_builtins(ImportLandxmlResult(tuple(summaries))), {}


def handle_import_coypu(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportCoypuParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    coypu_project = read_coypu(path)

    xml_texts = [text for name, text in coypu_project.raw_assets.items() if name.endswith(".xml")]
    if not xml_texts:
        raise ProtocolError(ErrorCode.EMPTY, "archive carries no embedded assets/*.xml LandXML")
    raws = [raw for text in xml_texts for raw in read_landxml(text.encode("utf-8"))]
    if not raws:
        raise ProtocolError(ErrorCode.EMPTY, "embedded LandXML carries no <Alignment> elements")

    alignment_summaries, alignment_ids = _import_alignments(project, raws, req.crs)
    sole_alignment_id = alignment_ids[0] if len(alignment_ids) == 1 else None

    stops = _stops_from_coypu(coypu_project.stops)
    project.stops = stops

    warnings: list[str] = []
    for summary in alignment_summaries:
        warnings.extend(summary.warnings)

    run_summaries = []
    trainset_summaries = []
    for index, run in enumerate(coypu_project.kinematics_runs(stops=stops)):
        trainset, warning = _trainset_for_vehicle(coypu_project, index)
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


def handle_import_kinematics(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportKinematicsParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    if req.alignment_id is not None and req.alignment_id not in project.alignments:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{req.alignment_id}'")

    stops: tuple[Stop, ...] = ()
    if req.stops_path is not None:
        stops_path = Path(req.stops_path)
        if not stops_path.is_file():
            raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {stops_path}")
        stops = read_stops_csv(stops_path)

    run = read_kinematics_csv(path, stops=stops)
    run_id = str(uuid4())
    entry = RunEntry(run=run, source="csv", alignment_id=req.alignment_id)
    project.runs[run_id] = entry

    result = ImportKinematicsResult(
        runs=(_run_summary(run_id, entry),),
        stops=tuple(_stop_dto(s) for s in run.stops),
        warnings=run.warnings,
    )
    return msgspec.to_builtins(result), {}


def handle_alignment_frame_table(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, AlignmentFrameTableParams)
    entry = project.alignments.get(req.alignment_id)
    if entry is None:
        raise ProtocolError(ErrorCode.NOT_FOUND, f"unknown alignment_id '{req.alignment_id}'")
    pivot = RotationPivot(req.pivot) if req.pivot else None
    table = bake_frame_table(entry.alignment, spacing_m=req.spacing_m, pivot=pivot)
    base = project.base_point or BasePoint(0.0, 0.0, 0.0)

    position = points_to_godot(table.origin, base, dtype=np.float32)
    rotation = quaternion_from_matrix(basis_to_godot(table.tangent, table.left, table.up)).astype(np.float32)
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
    "alignment.frame_table": handle_alignment_frame_table,
    "run.list": handle_run_list,
    "run.get": handle_run_get,
    "catalogue.vehicles": handle_catalogue_vehicles,
    "trainset.create": handle_trainset_create,
    "trainset.get": handle_trainset_get,
}
