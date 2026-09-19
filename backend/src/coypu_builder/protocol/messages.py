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


# --- catalogue.* / vehicle DTOs -------------------------------------------------------------------------
# `VehicleSpec`/`CarSpec`/`VehicleDynamics`/`TractionBand` (domain/model/vehicle.py) flattened into wire
# Structs -- domain objects never cross the boundary (T-114 DTO discipline).


class TractionBandDTO(msgspec.Struct, frozen=True):
    v_bottom_ms: float
    v_top_ms: float
    b0: float
    b1: float
    b2: float


class VehicleDynamicsDTO(msgspec.Struct, frozen=True):
    mass_t: float
    rotating_mass_factor: float
    max_speed_ms: float
    brake_decel_ms2: float
    max_tractive_force_kn: float | None = None
    davis_a: float | None = None
    davis_b: float | None = None
    davis_c: float | None = None
    traction_bands: tuple[TractionBandDTO, ...] = ()


class CarSpecDTO(msgspec.Struct, frozen=True):
    name: str
    length_m: float
    width_m: float
    height_m: float
    floor_height_m: float
    bogie_pivot_distance_m: float
    bogie_wheelbase_m: float
    wheel_diameter_m: float
    mesh: str | None = None
    color: str = "#808080"


class VehicleSpecDTO(msgspec.Struct, frozen=True):
    key: str
    name: str
    mode: str
    gauge_mm: float
    cars: tuple[CarSpecDTO, ...]
    coupling_gap_m: float = 0.0
    aliases: tuple[str, ...] = ()
    dynamics: VehicleDynamicsDTO | None = None
    provenance: str = ""


class CatalogueVehiclesResult(msgspec.Struct, frozen=True):
    vehicles: tuple[VehicleSpecDTO, ...]


# --- trainset.* -------------------------------------------------------------------------------------------
# The assembled consist, cars already resolved and flattened front to back. No poses: ADR 0007 puts posing
# in the client; T-112's backend chain (`domain.kinematics.trainset`) is a reference only.


class TrainsetSummary(msgspec.Struct, frozen=True):
    trainset_id: str
    spec_key: str
    name: str
    mode: str
    car_count: int
    length_m: float


class TrainsetDTO(msgspec.Struct, frozen=True):
    trainset_id: str
    spec_key: str
    name: str
    mode: str
    gauge_mm: float
    coupling_gap_m: float
    length_m: float
    cars: tuple[CarSpecDTO, ...]


class TrainsetGetParams(msgspec.Struct, frozen=True):
    trainset_id: str


class TrainsetCreateParams(msgspec.Struct, frozen=True):
    spec_key: str
    units: int = 1
    name: str = ""


# --- run.* ------------------------------------------------------------------------------------------------


class StopDTO(msgspec.Struct, frozen=True):
    station_m: float
    dwell_s: float
    name: str = ""


class RunSummary(msgspec.Struct, frozen=True):
    run_id: str
    name: str
    alignment_id: str | None
    trainset_id: str | None
    direction: int
    station_start: float
    station_end: float
    duration_s: float
    sample_count: int
    stop_count: int
    warnings: tuple[str, ...] = ()


class RunListResult(msgspec.Struct, frozen=True):
    runs: tuple[RunSummary, ...]


class RunGetParams(msgspec.Struct, frozen=True):
    run_id: str
    dt: float = 0.05


class RunGetResult(msgspec.Struct, frozen=True):
    run_id: str
    dt: float
    row_count: int
    duration_s: float
    direction: int
    stops: tuple[StopDTO, ...]


# --- import.coypu / import.kinematics --------------------------------------------------------------------


class ImportCoypuParams(msgspec.Struct, frozen=True):
    path: str
    crs: str | None = None


class ImportCoypuResult(msgspec.Struct, frozen=True):
    alignments: tuple[AlignmentSummary, ...]
    runs: tuple[RunSummary, ...]
    trainsets: tuple[TrainsetSummary, ...]
    stops: tuple[StopDTO, ...]
    warnings: tuple[str, ...] = ()


class ImportKinematicsParams(msgspec.Struct, frozen=True):
    path: str
    alignment_id: str | None = None
    stops_path: str | None = None


class ImportKinematicsResult(msgspec.Struct, frozen=True):
    runs: tuple[RunSummary, ...]
    stops: tuple[StopDTO, ...]
    warnings: tuple[str, ...] = ()


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
        name="import.coypu",
        summary=(
            "Import a .coypu archive: its embedded LandXML alignment(s), every kinematics run normalised "
            "and attached to a trainset where the vehicle name matches a catalogue entry, and its stops."
        ),
        params=ImportCoypuParams,
        result=ImportCoypuResult,
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
        name="import.kinematics",
        summary=(
            "Import a standalone kinematics CSV (either dialect) attached to an already-imported alignment."
        ),
        params=ImportKinematicsParams,
        result=ImportKinematicsResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
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
        name="run.list",
        summary="List the kinematics runs attached to the current project, without their baked tables.",
        params=None,
        result=RunListResult,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT),
    ),
    MethodSpec(
        name="run.get",
        summary=(
            "Fetch a kinematics run resampled onto a uniform time grid, as float32 blobs. Called once per "
            "run (ADR 0007), never per frame. There is no `time` blob: the table is time-uniform, so the "
            "client reconstructs t = i*dt from `dt` in the result."
        ),
        params=RunGetParams,
        result=RunGetResult,
        blobs=(
            BlobSpec(
                "station",
                "<f4",
                "(n,)",
                "Absolute station along the run [m]; float32 resolves to about 1 mm at Kralupy's "
                "~18000 m stations, which is enough for lookup, not for geometry.",
            ),
            BlobSpec("speed", "<f4", "(n,)", "Speed magnitude [m/s]."),
            BlobSpec("accel", "<f4", "(n,)", "Signed acceleration [m/s^2]."),
            BlobSpec(
                "f_traction",
                "<f4",
                "(n,)",
                "Tractive force [kN]. Omitted from the blob list entirely, never zero-filled, when the "
                "source run carried none.",
            ),
            BlobSpec(
                "f_braking",
                "<f4",
                "(n,)",
                "Braking force [kN]. Omitted from the blob list entirely, never zero-filled, when the "
                "source run carried none.",
            ),
            BlobSpec(
                "f_resistance",
                "<f4",
                "(n,)",
                "Resistance force [kN]. Omitted from the blob list entirely, never zero-filled, when the "
                "source run carried none.",
            ),
        ),
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
    MethodSpec(
        name="catalogue.vehicles",
        summary=(
            "Return the process-wide vehicle catalogue as a flat JSON projection (cars and dynamics, no "
            "blobs)."
        ),
        params=None,
        result=CatalogueVehiclesResult,
        errors=(ErrorCode.NO_SESSION,),
    ),
    MethodSpec(
        name="trainset.create",
        summary="Assemble a consist from a catalogue key and attach it to the current project.",
        params=TrainsetCreateParams,
        result=TrainsetDTO,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
    MethodSpec(
        name="trainset.get",
        summary="Fetch an assembled consist by id, cars already resolved. No poses (ADR 0007).",
        params=TrainsetGetParams,
        result=TrainsetDTO,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
)
