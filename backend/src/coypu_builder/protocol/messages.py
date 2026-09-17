"""Wire protocol Structs for the localhost WebSocket IPC (ADR 0003).

Envelope: `[u32 LE header_len][UTF-8 JSON header][blob 0][blob 1]…`. `Envelope` is the JSON header;
`blobs` points into the binary tail that follows it (little-endian, C-order, per `BlobRef`).
`protocol/messages.py` is the single source of truth for the wire shapes on both request and response
sides of every method below.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import msgspec

MessageType = Literal["req", "res", "evt", "err"]


class ErrorCode(StrEnum):
    """Wire values for `ErrorInfo.code`. Every raise site in `server/` uses these, never a bare string."""

    BAD_FRAME = "E_BAD_FRAME"
    BAD_PARAMS = "E_BAD_PARAMS"
    UNKNOWN_METHOD = "E_UNKNOWN_METHOD"
    UNAUTHORIZED = "E_UNAUTHORIZED"
    NO_SESSION = "E_NO_SESSION"
    NO_PROJECT = "E_NO_PROJECT"
    NOT_FOUND = "E_NOT_FOUND"
    EMPTY = "E_EMPTY"
    CRS_REQUIRED = "E_CRS_REQUIRED"
    INTERNAL = "E_INTERNAL"


class BlobRef(msgspec.Struct, frozen=True):
    name: str
    dtype: str
    shape: tuple[int, ...]
    offset: int
    length: int


class ErrorInfo(msgspec.Struct, frozen=True):
    code: str
    message: str


class Envelope(msgspec.Struct, frozen=True, omit_defaults=True):
    v: int
    id: str
    type: MessageType
    method: str = ""
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    blobs: tuple[BlobRef, ...] = ()


# --- session.* --------------------------------------------------------------------------------------------


class SessionHelloParams(msgspec.Struct, frozen=True):
    client: str
    client_version: str
    token: str = ""


class SessionHelloResult(msgspec.Struct, frozen=True):
    session_id: str
    protocol_version: int
    server_version: str


class SessionPingParams(msgspec.Struct, frozen=True):
    client_time_ms: int = 0


class SessionPingResult(msgspec.Struct, frozen=True):
    client_time_ms: int
    server_time_ms: int


# --- project.* ----------------------------------------------------------------------------------------


class BasePointDTO(msgspec.Struct, frozen=True):
    easting: float
    northing: float
    height: float = 0.0


class AlignmentSummary(msgspec.Struct, frozen=True):
    alignment_id: str
    name: str
    mode: str
    station_start: float
    station_end: float
    length: float
    warnings: tuple[str, ...] = ()


class ProjectNewParams(msgspec.Struct, frozen=True):
    crs: str | None = None
    base_point: BasePointDTO | None = None


class ProjectInfoResult(msgspec.Struct, frozen=True):
    project_id: str
    crs: str | None
    base_point: BasePointDTO | None
    alignments: tuple[AlignmentSummary, ...] = ()


# --- import.* -----------------------------------------------------------------------------------------


class ImportLandxmlParams(msgspec.Struct, frozen=True):
    path: str
    crs: str | None = None


class ImportLandxmlResult(msgspec.Struct, frozen=True):
    alignments: tuple[AlignmentSummary, ...]


# --- alignment.frame_table ------------------------------------------------------------------------------
# Result carries only metadata; the frame table itself rides the blobs (station, position, rotation, roll,
# pitch, cant_mm, curvature, gradient, elevation, segment_index) so the client streams it as typed arrays.


class AlignmentFrameTableParams(msgspec.Struct, frozen=True):
    alignment_id: str
    spacing_m: float = 1.0
    pivot: str | None = None


class AlignmentFrameTableResult(msgspec.Struct, frozen=True):
    alignment_id: str
    row_count: int
    station_start: float
    station_end: float


# --- run.get --------------------------------------------------------------------------------------------


class RunGetParams(msgspec.Struct, frozen=True):
    run_id: str


# --- method registry ------------------------------------------------------------------------------------
# The single source of truth for the method table (ADR 0003). `tools/gen_protocol_docs.py` renders
# `docs/protocol/ipc.md` from this; nothing generates into this file.


class BlobSpec(msgspec.Struct, frozen=True):
    name: str
    dtype: str  # numpy dtype string as it appears on the wire, e.g. "<f4", "<i4"
    shape: str  # documentation shape, e.g. "(n,)" or "(n, 3)" or "(n, 4)"
    description: str


class MethodSpec(msgspec.Struct, frozen=True):
    name: str
    summary: str  # one line, imperative
    params: type | None
    result: type | None
    blobs: tuple[BlobSpec, ...] = ()
    errors: tuple[ErrorCode, ...] = ()


METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        name="session.hello",
        summary="Authenticate the connection and negotiate the protocol version.",
        params=SessionHelloParams,
        result=SessionHelloResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.UNAUTHORIZED),
    ),
    MethodSpec(
        name="session.ping",
        summary="Liveness probe for the client heartbeat (ADR 0003, 2 s interval).",
        params=SessionPingParams,
        result=SessionPingResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION),
    ),
    MethodSpec(
        name="project.new",
        summary="Open a new, empty project, optionally pinning its CRS and base point.",
        params=ProjectNewParams,
        result=ProjectInfoResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION),
    ),
    MethodSpec(
        name="project.get",
        summary="Return the current project's state.",
        params=None,
        result=ProjectInfoResult,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT),
    ),
    MethodSpec(
        name="import.landxml",
        summary="Import alignments from a LandXML file into the current project.",
        params=ImportLandxmlParams,
        result=ImportLandxmlResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.EMPTY,
            ErrorCode.CRS_REQUIRED,
        ),
    ),
    MethodSpec(
        name="alignment.frame_table",
        summary="Bake a dense, render-ready frame table for one alignment.",
        params=AlignmentFrameTableParams,
        result=AlignmentFrameTableResult,
        blobs=(
            BlobSpec("station", "<f4", "(n,)", "Absolute station along the alignment [m]."),
            BlobSpec(
                "position",
                "<f4",
                "(n, 3)",
                "Track-plane centre in Godot axes, relative to the project base point [m].",
            ),
            BlobSpec(
                "rotation",
                "<f4",
                "(n, 4)",
                "Orientation quaternion (x, y, z, w) mapping the local (tangent, left, up) frame into "
                "Godot axes.",
            ),
            BlobSpec("roll", "<f4", "(n,)", "Roll about the tangent from cant [rad]."),
            BlobSpec(
                "pitch", "<f4", "(n,)", "Pitch from the vertical profile gradient [rad], positive climbing."
            ),
            BlobSpec("cant_mm", "<f4", "(n,)", "Superelevation [mm]."),
            BlobSpec("curvature", "<f4", "(n,)", "Signed horizontal curvature [1/m]."),
            BlobSpec("gradient", "<f4", "(n,)", "Vertical gradient [dimensionless]."),
            BlobSpec("elevation", "<f4", "(n,)", "Profile elevation at the pivot [m]."),
            BlobSpec("segment_index", "<i4", "(n,)", "Index of the horizontal segment containing this row."),
        ),
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
    MethodSpec(
        name="run.get",
        summary="Fetch a baked kinematics run (not yet implemented).",
        params=RunGetParams,
        result=None,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
)
