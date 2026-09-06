"""Wire protocol Structs for the localhost WebSocket IPC (ADR 0003).

Envelope: `[u32 LE header_len][UTF-8 JSON header][blob 0][blob 1]…`. `Envelope` is the JSON header;
`blobs` points into the binary tail that follows it (little-endian, C-order, per `BlobRef`).
`protocol/messages.py` is the single source of truth for the wire shapes on both request and response
sides of every method below.
"""

from __future__ import annotations

from typing import Any, Literal

import msgspec

MessageType = Literal["req", "res", "evt", "err"]


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
