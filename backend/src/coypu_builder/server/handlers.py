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
from coypu_builder.domain.sampling import bake_frame_table
from coypu_builder.io.landxml import read_landxml, to_alignment
from coypu_builder.protocol.messages import (
    AlignmentFrameTableParams,
    AlignmentFrameTableResult,
    AlignmentSummary,
    BasePointDTO,
    ErrorCode,
    ImportLandxmlParams,
    ImportLandxmlResult,
    ProjectInfoResult,
    ProjectNewParams,
    RunGetParams,
    SessionHelloParams,
    SessionHelloResult,
    SessionPingParams,
    SessionPingResult,
)
from coypu_builder.server.session import AlignmentEntry, ProjectState, Session

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


def handle_import_landxml(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    project = _require_project(session)
    req = _convert(params, ImportLandxmlParams)
    path = Path(req.path)
    if not path.is_file():
        raise ProtocolError(ErrorCode.NOT_FOUND, f"no such file: {path}")
    raws = read_landxml(path)
    if not raws:
        raise ProtocolError(ErrorCode.EMPTY, "file carries no <Alignment> elements")

    crs = ProjectCRS(req.crs) if req.crs else project.crs
    if crs is None:
        epsg = raws[0].epsg
        if epsg is None:
            raise ProtocolError(ErrorCode.CRS_REQUIRED, "file carries no CRS; pass crs")
        crs = ProjectCRS(f"EPSG:{epsg}")
    if project.crs is None:
        project.crs = crs

    summaries = []
    for raw in raws:
        conversion = to_alignment(raw, crs)
        aln = conversion.alignment
        alignment_id = str(uuid4())
        project.alignments[alignment_id] = AlignmentEntry(aln, tuple(conversion.report.warnings))
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
    return msgspec.to_builtins(ImportLandxmlResult(tuple(summaries))), {}


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


def handle_run_get(session: Session, params: dict[str, Any] | None) -> HandlerResult:
    _require_project(session)
    req = _convert(params, RunGetParams)
    raise ProtocolError(
        ErrorCode.NOT_FOUND, f"no run '{req.run_id}': kinematics runs are not implemented yet"
    )


DISPATCH: dict[str, Handler] = {
    "session.hello": handle_session_hello,
    "session.ping": handle_session_ping,
    "project.new": handle_project_new,
    "project.get": handle_project_get,
    "import.landxml": handle_import_landxml,
    "alignment.frame_table": handle_alignment_frame_table,
    "run.get": handle_run_get,
}
