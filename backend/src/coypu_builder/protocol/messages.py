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
    BUSY = "E_BUSY"  # too many long requests in flight on this connection; retry later


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


# --- import.inspect / import.run / source.* / plugins.list ----------------------------------------------
# Context-data import (ADR 0009): inspect a source, confirm the settings, import. The domain types of
# `domain/sources.py` flattened into wire Structs.


class CorridorClipDTO(msgspec.Struct, frozen=True, tag_field="type", tag="corridor"):
    buffer_m: float
    alignment_ids: tuple[str, ...] = ()  # empty = every alignment in the project
    station_from: float | None = None  # only with exactly one alignment
    station_to: float | None = None


class BBoxClipDTO(msgspec.Struct, frozen=True, tag_field="type", tag="bbox"):
    min_e: float  # project CRS
    min_n: float
    max_e: float
    max_n: float


class ImportSettingsDTO(msgspec.Struct, frozen=True):
    horizontal_crs: str | None = None  # EPSG code, WKT or PROJ; None = the source's declared CRS
    vertical_crs: str | None = None  # recorded only; heights are not transformed in Phase 1
    coordinate_order: str = "auto"  # "auto" | "EN" | "NE"
    linear_unit_m: float = 1.0
    clip: CorridorClipDTO | BBoxClipDTO | None = None
    point_classes: tuple[int, ...] | None = None
    display_resolution_m: float | None = None
    nodata: float | None = None


class ImportInspectParams(msgspec.Struct, frozen=True):
    path: str | None = None  # absolute; exactly one of `path`, `paths` and `provider`
    format_id: str | None = None  # None = sniffed
    paths: tuple[str, ...] | None = None  # absolute; several files of one format are one source
    provider: str | None = None  # an online provider id, e.g. "cuzk-dmr5g"; needs a project with a CRS
    query: dict[str, Any] | None = None  # the provider's request: `clip`, limits (see terrain-providers.md)


class ImportInspectResult(msgspec.Struct, frozen=True):
    format_id: str
    kind: str
    declared_crs: str | None
    extent: tuple[float, float, float, float] | None
    counts: dict[str, int]
    suggested_settings: ImportSettingsDTO
    licence_hint: str | None
    warnings: tuple[str, ...] = ()


class ImportRunParams(msgspec.Struct, frozen=True, kw_only=True):
    path: str | None = None  # absolute; exactly one of `path`, `paths` and `provider`
    paths: tuple[str, ...] | None = None  # absolute; several files of one format are one source
    provider: str | None = None  # an online provider id; its files are fetched into the provider cache
    query: dict[str, Any] | None = None  # the provider's request: `clip`, limits
    kind: str  # "terrain" | "imagery" | "buildings" | "vegetation" | "water"
    settings: ImportSettingsDTO
    name: str | None = None
    format_id: str | None = None
    reimport_of: str | None = None  # a source_id of the same kind and format; keeps its GUIDs


class ImportRunResult(msgspec.Struct, frozen=True):
    source_id: str
    name: str
    effective_crs: str
    transformation: str
    transformation_accuracy_m: float | None
    added: int
    changed: int
    unchanged: int
    removed: int
    warnings: tuple[str, ...] = ()


class SourceSummary(msgspec.Struct, frozen=True):
    id: str
    kind: str
    name: str
    format_id: str
    object_count: int
    imported_at: str
    attribution: str


class SourceListResult(msgspec.Struct, frozen=True):
    sources: tuple[SourceSummary, ...]


class SourceGetParams(msgspec.Struct, frozen=True):
    source_id: str


class FileOriginDTO(msgspec.Struct, frozen=True, tag_field="type", tag="file"):
    paths: tuple[str, ...]
    sha256: tuple[str, ...]


class ProviderOriginDTO(msgspec.Struct, frozen=True, tag_field="type", tag="provider"):
    provider_id: str
    query: str  # canonical JSON of the request, defaults written out
    sha256: tuple[str, ...] = ()  # of every file the provider fetched


class ObjectRecordDTO(msgspec.Struct, frozen=True):
    key: str
    id: str
    content_hash: str


class SourceDatasetDTO(msgspec.Struct, frozen=True):
    id: str
    kind: str
    name: str
    format_id: str
    origin: FileOriginDTO | ProviderOriginDTO
    settings: ImportSettingsDTO
    declared_crs: str | None
    effective_crs: str
    transformation: str
    transformation_accuracy_m: float | None
    licence: str
    attribution: str
    imported_at: str
    tool_version: str
    objects: tuple[ObjectRecordDTO, ...]
    warnings: tuple[str, ...] = ()
    resolution_m: float | None = None  # terrain: the declared resolution that orders the sources


class PluginInfoDTO(msgspec.Struct, frozen=True):
    name: str
    group: str
    distribution: str
    version: str
    state: str  # "loaded" | "available" | "failed"
    error: str | None = None


class PluginsListResult(msgspec.Struct, frozen=True):
    plugins: tuple[PluginInfoDTO, ...]
    warnings: tuple[str, ...] = ()  # problems with the per-user config.toml


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


# --- alignment.track_mesh --------------------------------------------------------------------------------
# Result carries only per-chunk metadata; geometry rides the blobs, four per (chunk_index, surface) pair --
# see the `alignment.track_mesh` BlobSpec descriptions below for the exact naming scheme.


class AlignmentTrackMeshParams(msgspec.Struct, frozen=True):
    alignment_id: str
    chunk_length_m: float = 250.0
    spacing_m: float = 1.0
    chunk_index: int | None = None  # None = all chunks
    metadata_only: bool = False  # True = every chunk's info, no blobs (the cheap "how many, and where?")


class TrackMeshChunkInfo(msgspec.Struct, frozen=True):
    chunk_index: int
    surface: str
    station_start: float
    station_end: float
    tile_origin: tuple[float, float, float]  # Godot axes, relative to the project base point
    vertex_count: int
    index_count: int


class AlignmentTrackMeshResult(msgspec.Struct, frozen=True):
    alignment_id: str
    chunks: tuple[TrackMeshChunkInfo, ...]


# --- terrain.tiles / terrain.tile ------------------------------------------------------------------------
# Display heightfield tiles (T-132): list a level, then fetch tiles one at a time (the F18 paging pattern).
# See docs/data-contracts/terrain-tiles.md.


class TerrainTilesParams(msgspec.Struct, frozen=True):
    level: int = 0  # 0 to 4; edge 512 m * 2**level


class TerrainTileInfo(msgspec.Struct, frozen=True):
    level: int
    ix: int
    iy: int
    edge_m: float
    tile_origin: tuple[
        float, float, float
    ]  # Godot axes, relative to the base point; y = minimum valid height
    valid_fraction: float


class TerrainTilesResult(msgspec.Struct, frozen=True):
    level: int
    revision: int
    tiles: tuple[TerrainTileInfo, ...]
    warnings: tuple[str, ...] = ()  # sources skipped, and the height step at each seam between sources
    source_ids: tuple[
        str, ...
    ] = ()  # the sources in precedence order, finest first: what `source_index` indexes


class TerrainTileParams(msgspec.Struct, frozen=True):
    level: int
    ix: int
    iy: int
    revision: int  # from the `terrain.tiles` that listed this tile


class TerrainTileResult(msgspec.Struct, frozen=True):
    level: int
    ix: int
    iy: int
    edge_m: float
    tile_origin: tuple[float, float, float]
    valid_fraction: float
    revision: int
    source_ids: tuple[str, ...] = ()  # the sources in precedence order for this revision


# --- alignment.envelope ----------------------------------------------------------------------------------
# Writes a corridor buffer polygon (GeoJSON or Shapefile, by the suffix of `path`) and returns metadata only.


class AlignmentEnvelopeParams(msgspec.Struct, frozen=True):
    path: str  # absolute output file outside the repository; the suffix decides the format
    buffer_m: float = 250.0
    alignment_ids: list[str] | None = None  # None = every alignment in the project
    station_from: float | None = None
    station_to: float | None = None
    epsg: int | None = None  # None = 4326 for GeoJSON, the project CRS for Shapefile
    cap: str = "round"
    overwrite: bool = False  # True = replace existing output files (every Shapefile part)


class AlignmentEnvelopeResult(msgspec.Struct, frozen=True):
    files: list[str]
    epsg: int
    area_m2: float
    vertex_count: int
    bounds: list[float]


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


class Stability(StrEnum):
    """How much a caller may rely on a method (ADR 0010, decision 1)."""

    INTERNAL = "internal"  # the Godot client only; may change any time
    EXPERIMENTAL = "experimental"  # usable externally; may change with a release note
    STABLE = "stable"  # changes only with a PROTOCOL_VERSION bump and a deprecation period


class MethodSpec(msgspec.Struct, frozen=True):
    name: str
    summary: str  # one line, imperative
    stability: Stability
    mutates: bool  # changes session or project state: see the dispatch rules in docs/protocol/ipc.md
    params: type | None
    result: type | None
    blobs: tuple[BlobSpec, ...] = ()
    errors: tuple[ErrorCode, ...] = ()


METHODS: tuple[MethodSpec, ...] = (
    MethodSpec(
        name="session.hello",
        summary="Authenticate the connection and negotiate the protocol version.",
        stability=Stability.STABLE,
        mutates=True,
        params=SessionHelloParams,
        result=SessionHelloResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.UNAUTHORIZED),
    ),
    MethodSpec(
        name="session.ping",
        summary="Liveness probe for the client heartbeat (ADR 0003, 2 s interval).",
        stability=Stability.STABLE,
        mutates=False,
        params=SessionPingParams,
        result=SessionPingResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION),
    ),
    MethodSpec(
        name="project.new",
        summary="Open a new, empty project, optionally pinning its CRS and base point.",
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=ProjectNewParams,
        result=ProjectInfoResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION),
    ),
    MethodSpec(
        name="project.get",
        summary="Return the current project's state.",
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=None,
        result=ProjectInfoResult,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT),
    ),
    MethodSpec(
        name="import.landxml",
        summary="Import alignments from a LandXML file into the current project.",
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=ImportLandxmlParams,
        result=ImportLandxmlResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.EMPTY,
            ErrorCode.CRS_REQUIRED,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="import.coypu",
        summary=(
            "Import a .coypu archive: its embedded LandXML alignment(s), every kinematics run normalised "
            "and attached to a trainset where the vehicle name matches a catalogue entry, and its stops."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=ImportCoypuParams,
        result=ImportCoypuResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.EMPTY,
            ErrorCode.CRS_REQUIRED,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="import.kinematics",
        summary=(
            "Import a standalone kinematics CSV (either dialect) attached to an already-imported alignment."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=ImportKinematicsParams,
        result=ImportKinematicsResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="import.inspect",
        summary=(
            "Read a context-data source cheaply (terrain, imagery, later buildings, vegetation, water) and "
            "report its format, declared CRS (null when it has none, with a warning), extent, counts and "
            "proposed import settings. Step one of ADR 0009's inspect, confirm, import. Give exactly one "
            "of `path` and `paths` (several files of one format, e.g. the sheets of one survey, are one "
            "source: counts are summed, extents united, and differing declared CRSs are warned about and "
            "reported as null); every path must be absolute. `format_id` picks a reader instead of "
            "sniffing; a ZIP holding a single supported file reads like the bare file. Needs no project. "
            "Instead of files, `provider` (with an optional `query`) inspects an online source: the report "
            "counts the sheets or tiles the query's clip selects, their size, and proposes the settings "
            "(including the clip); it fetches index files only, never the data, and needs a project with a "
            "CRS (`E_NO_PROJECT`, `E_CRS_REQUIRED`). An unknown provider or a bad query is `E_BAD_PARAMS`; "
            "a service that cannot be reached is `E_BAD_PARAMS` with the reason."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=ImportInspectParams,
        result=ImportInspectResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NOT_FOUND,
            ErrorCode.INTERNAL,
            ErrorCode.BUSY,
            ErrorCode.NO_PROJECT,
            ErrorCode.CRS_REQUIRED,
            ErrorCode.EMPTY,
        ),
    ),
    MethodSpec(
        name="import.run",
        summary=(
            "Import a context-data source with the confirmed `settings`, exactly as given, and record it as "
            "a `SourceDataset` with licence, attribution and provenance. A source with no declared CRS "
            "and no `settings.horizontal_crs` is refused with `E_CRS_REQUIRED`, never guessed; the "
            "project must have a CRS. Every object gets a GUID. `reimport_of` names an earlier source of "
            "the same kind and format: objects are matched on their own source key, matched ones keep "
            "their GUID, and the source keeps its id. Without it the import is a new source with all-new "
            "GUIDs. Counts report added, changed, unchanged and removed objects. Give exactly one of "
            "`path` and `paths`; every path must be absolute. With `paths` all files must resolve to the "
            "same reader (mixed formats are `E_BAD_PARAMS`) and each file yields its own objects. "
            "`provider` (with `query`) is the third alternative: exactly one of `path`, `paths` and "
            "`provider`. The provider's files are fetched into its per-user cache (never the repository or "
            "the data folder), read by the ordinary readers, and the source records a provider origin "
            "(provider id, canonical query, file hashes), the provider's licence and attribution. A clip "
            "left out of `settings` defaults to the query's. `reimport_of` fetches again, reconciles on "
            "the object keys (sheet codes, tile names) and keeps the GUIDs. The result shape is the same."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=ImportRunParams,
        result=ImportRunResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.EMPTY,
            ErrorCode.CRS_REQUIRED,
            ErrorCode.INTERNAL,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="source.list",
        summary="List the context-data sources imported into the current project, one summary each.",
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=None,
        result=SourceListResult,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT),
    ),
    MethodSpec(
        name="source.get",
        summary=(
            "Fetch one source's full provenance record: origin with file hashes, the settings as confirmed, "
            "declared and effective CRS, the transformation used, licence, attribution, import time, tool "
            "version, every object's key and GUID, and for terrain the declared resolution [m] that orders "
            "the sources (finest first)."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=SourceGetParams,
        result=SourceDatasetDTO,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
    MethodSpec(
        name="plugins.list",
        summary=(
            "List the reader and provider plugins found as entry points, with their state: `loaded` "
            "(allow-listed and imported), `available` (found but not in the user's allow-list, never "
            "imported) or `failed` (allow-listed but raised on load, with the error). Also reports problems "
            "with the per-user `config.toml`."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=None,
        result=PluginsListResult,
        errors=(ErrorCode.NO_SESSION,),
    ),
    MethodSpec(
        name="alignment.frame_table",
        summary="Bake a dense, render-ready frame table for one alignment.",
        stability=Stability.EXPERIMENTAL,
        mutates=False,
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
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="alignment.track_mesh",
        summary=(
            "Bake two swept rails plus one default ballast prism for one alignment, chunked and tile-local "
            "(ADR 0004): every vertex is relative to its own chunk's tile_origin, never in absolute "
            "project coordinates. `metadata_only` returns every chunk's info and no blobs, so a client can "
            "learn the chunk count before paging by `chunk_index`."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=AlignmentTrackMeshParams,
        result=AlignmentTrackMeshResult,
        blobs=(
            BlobSpec(
                "vertices_<i>_<surface>",
                "<f4",
                "(n, 3)",
                "Naming scheme, one quadruplet of blobs per (chunk_index, surface) pair actually present in "
                "`chunks`: `<i>` is that entry's `chunk_index`, `<surface>` its `surface` "
                "('rail_left' | 'rail_right' | 'ballast'), e.g. `vertices_3_ballast`. Positions in Godot "
                "axes, relative to that entry's `tile_origin` -- never absolute.",
            ),
            BlobSpec(
                "normals_<i>_<surface>",
                "<f4",
                "(n, 3)",
                "Same naming scheme as `vertices_<i>_<surface>`; unit outward normals in Godot axes.",
            ),
            BlobSpec(
                "uvs_<i>_<surface>",
                "<f4",
                "(n, 2)",
                "Same naming scheme. `u` is accumulated perimeter distance around the profile; `v` is "
                "absolute station [m], so it matches exactly across a shared chunk boundary.",
            ),
            BlobSpec(
                "indices_<i>_<surface>",
                "<i4",
                "(m,)",
                "Same naming scheme. Triangle indices into that same blob's vertices, wound "
                "counter-clockwise as seen from outside.",
            ),
        ),
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="terrain.tiles",
        summary=(
            "List the terrain display tiles of one level (0 to 4; edge 512 m * 2**level) that cover the "
            "project's terrain sources, metadata only: `level`, `ix`, `iy`, `edge_m`, `tile_origin` "
            "(Godot axes relative to the base point; its y is the tile's minimum valid height) and "
            "`valid_fraction`, plus the `revision` that changes whenever the sources change. Tiles are "
            "regular 129 x 129 heightfields derived from the analysis surface, for display only. A project "
            "without terrain returns an empty list. Page by calling `terrain.tile` for each listed tile. "
            "`source_ids` names the terrain sources in precedence order (finest declared resolution first, "
            "the newest import first among equals): the index into it is what each tile's `source_index` "
            "blob holds. `warnings` also report the height step along each seam between two sources, as "
            "mean and 95th percentile of the absolute difference."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=TerrainTilesParams,
        result=TerrainTilesResult,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.BUSY),
    ),
    MethodSpec(
        name="terrain.tile",
        summary=(
            "Fetch one terrain display tile listed by `terrain.tiles`: its metadata plus the `heights` and "
            "`source_index` blobs. `revision` must be the one the list returned; a stale revision, or a "
            "tile not in the list, is `E_NOT_FOUND`, and the client re-lists instead of mixing old and new "
            "tiles. `source_ids` is the ordered source list of that revision."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=TerrainTileParams,
        result=TerrainTileResult,
        blobs=(
            BlobSpec(
                "heights",
                "<f4",
                "(129, 129)",
                "Heights in metres relative to the y of `tile_origin`; row 0 is the south edge, column 0 the "
                "west edge, spacing `edge_m / 128`. NaN where the tile has no data. Position the node at "
                "`tile_origin`; the origin is never folded into the values.",
            ),
            BlobSpec(
                "source_index",
                "<u1",
                "(129, 129)",
                "Which source answered at each sample, laid out like `heights`: an index into `source_ids` "
                "(0 = the finest source), 255 where no source has data. One byte per sample (byte order does "
                "not apply); written as `<u1` on the wire, as the other blobs are written `<`.",
            ),
        ),
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="alignment.envelope",
        summary=(
            "Write the corridor envelope -- a buffer polygon of `buffer_m` half-width around the plan "
            "centreline of the chosen alignments -- as GeoJSON (.geojson, .json) or Shapefile (.shp), and "
            "return its metadata. `path` must be absolute and outside the repository, `buffer_m` at most "
            "5000 m, `epsg` a projected or geographic 2D CRS, and an existing file is only replaced when "
            "`overwrite` is true. A station range needs exactly one alignment. Requires the backend's "
            "`gis` extra; without it the call fails with an error that names `uv sync --extra gis`."
        ),
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=AlignmentEnvelopeParams,
        result=AlignmentEnvelopeResult,
        errors=(
            ErrorCode.BAD_PARAMS,
            ErrorCode.NO_SESSION,
            ErrorCode.NO_PROJECT,
            ErrorCode.NOT_FOUND,
            ErrorCode.EMPTY,
            ErrorCode.CRS_REQUIRED,
            ErrorCode.INTERNAL,
            ErrorCode.BUSY,
        ),
    ),
    MethodSpec(
        name="run.list",
        summary="List the kinematics runs attached to the current project, without their baked tables.",
        stability=Stability.EXPERIMENTAL,
        mutates=False,
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
        stability=Stability.EXPERIMENTAL,
        mutates=False,
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
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=None,
        result=CatalogueVehiclesResult,
        errors=(ErrorCode.NO_SESSION,),
    ),
    MethodSpec(
        name="trainset.create",
        summary="Assemble a consist from a catalogue key and attach it to the current project.",
        stability=Stability.EXPERIMENTAL,
        mutates=True,
        params=TrainsetCreateParams,
        result=TrainsetDTO,
        errors=(ErrorCode.BAD_PARAMS, ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
    MethodSpec(
        name="trainset.get",
        summary="Fetch an assembled consist by id, cars already resolved. No poses (ADR 0007).",
        stability=Stability.EXPERIMENTAL,
        mutates=False,
        params=TrainsetGetParams,
        result=TrainsetDTO,
        errors=(ErrorCode.NO_SESSION, ErrorCode.NO_PROJECT, ErrorCode.NOT_FOUND),
    ),
)
