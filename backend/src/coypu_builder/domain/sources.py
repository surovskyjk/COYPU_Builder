"""Context-data sources: import settings, clip regions, source records and GUID reconciliation (ADR 0009).

Pure: numbers, strings and `EntityId`s only. Reading files, resolving coordinate systems and clipping live in
`io/sources`. A `SourceDataset` is the provenance record of one import (what came from where, under which
settings and licence); its `ObjectRecord`s tie every imported object's own source key to a GUID that survives
re-import. GUIDs are never derived from keys.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from coypu_builder.domain.model.ids import EntityId, new_id

COORDINATE_ORDERS = ("auto", "EN", "NE")
MAX_CORRIDOR_BUFFER_M = 5000.0


class SourceKind(StrEnum):
    TERRAIN = "terrain"
    IMAGERY = "imagery"
    BUILDINGS = "buildings"
    VEGETATION = "vegetation"
    WATER = "water"


@dataclass(frozen=True)
class CorridorClip:
    """Keep what lies within `buffer_m` of the alignments' plan centrelines (the corridor envelope)."""

    buffer_m: float
    alignment_ids: tuple[EntityId, ...] = ()  # empty = every alignment in the project
    station_from: float | None = None  # only with exactly one alignment
    station_to: float | None = None

    def __post_init__(self) -> None:
        if not (math.isfinite(self.buffer_m) and 0.0 < self.buffer_m <= MAX_CORRIDOR_BUFFER_M):
            raise ValueError(
                f"buffer_m must be > 0 and at most {MAX_CORRIDOR_BUFFER_M:g} m, got {self.buffer_m}"
            )
        for name in ("station_from", "station_to"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite, got {value}")
        ranged = self.station_from is not None or self.station_to is not None
        if ranged and len(self.alignment_ids) > 1:
            raise ValueError("a station range is only valid with exactly one alignment")


@dataclass(frozen=True)
class BBoxClip:
    """An axis-aligned box in the project CRS."""

    min_e: float
    min_n: float
    max_e: float
    max_n: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.min_e, self.min_n, self.max_e, self.max_n)):
            raise ValueError("clip box bounds must be finite")
        if not (self.min_e < self.max_e and self.min_n < self.max_n):
            raise ValueError(
                f"empty clip box: ({self.min_e}, {self.min_n}) to ({self.max_e}, {self.max_n}) must have "
                "min < max on both axes"
            )


ClipRegion = CorridorClip | BBoxClip


@dataclass(frozen=True)
class ImportSettings:
    horizontal_crs: str | None = None  # EPSG code, WKT or PROJ; None = the source's declared CRS
    vertical_crs: str | None = None  # e.g. "EPSG:5705"; recorded only, heights are not transformed in Phase 1
    coordinate_order: str = "auto"  # "auto" | "EN" | "NE", as io/landxml/dialects.py
    linear_unit_m: float = 1.0  # source unit in metres
    clip: ClipRegion | None = None
    point_classes: tuple[int, ...] | None = None  # point-cloud classification codes to keep; None = all
    display_resolution_m: float | None = None
    nodata: float | None = None

    def __post_init__(self) -> None:
        if self.coordinate_order not in COORDINATE_ORDERS:
            raise ValueError(
                f"coordinate_order must be one of {COORDINATE_ORDERS}, got '{self.coordinate_order}'"
            )
        if not (math.isfinite(self.linear_unit_m) and self.linear_unit_m > 0.0):
            raise ValueError(f"linear_unit_m must be finite and > 0, got {self.linear_unit_m}")
        resolution = self.display_resolution_m
        if resolution is not None and not (math.isfinite(resolution) and resolution > 0.0):
            raise ValueError(f"display_resolution_m must be finite and > 0, got {resolution}")
        if self.nodata is not None and not math.isfinite(self.nodata):
            # NaN and inf cannot ride the JSON wire and a NaN would break "settings exactly as confirmed"
            # (NaN != NaN); a reader that meets a NaN nodata marker in a raster handles it natively.
            raise ValueError(f"nodata must be finite, got {self.nodata}")


@dataclass(frozen=True)
class FileOrigin:
    paths: tuple[str, ...]  # absolute
    sha256: tuple[str, ...]


@dataclass(frozen=True)
class ProviderOrigin:
    provider_id: str
    query: str  # canonical JSON of the request
    sha256: tuple[str, ...] = ()  # of every file the provider fetched, in the order they were read


@dataclass(frozen=True)
class ObjectRecord:
    key: str  # the object's own source key
    id: EntityId
    content_hash: str


@dataclass(frozen=True)
class SourceDataset:
    id: EntityId
    kind: SourceKind
    name: str
    format_id: str
    origin: FileOrigin | ProviderOrigin
    settings: ImportSettings  # exactly as confirmed
    declared_crs: str | None  # what the source itself said
    effective_crs: str  # what was used
    transformation: str  # pyproj's description, or "none" for an identical CRS
    transformation_accuracy_m: float | None
    licence: str
    attribution: str
    imported_at: str  # ISO 8601 UTC
    tool_version: str
    objects: tuple[ObjectRecord, ...]


@dataclass(frozen=True)
class Reconciliation:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...]
    objects: tuple[ObjectRecord, ...]  # the new list: matched keys keep their GUID


def reconcile(
    previous: Sequence[ObjectRecord],
    current: Mapping[str, str],
    make_id: Callable[[], EntityId] = new_id,
) -> Reconciliation:
    """Match a re-import to the previous import on the objects' own keys.

    `current` maps key -> content hash. A matched key keeps its GUID whether or not its hash changed; an
    unmatched current key gets a fresh GUID from `make_id`; a previous key absent from `current` is removed.
    `objects` follows the order of `current`.
    """
    known: dict[str, ObjectRecord] = {}
    for record in previous:
        if record.key in known:
            raise ValueError(f"duplicate object key '{record.key}' in the previous import")
        known[record.key] = record

    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    objects: list[ObjectRecord] = []
    for key, content_hash in current.items():
        match = known.get(key)
        if match is None:
            added.append(key)
            objects.append(ObjectRecord(key, make_id(), content_hash))
            continue
        (unchanged if match.content_hash == content_hash else changed).append(key)
        objects.append(ObjectRecord(key, match.id, content_hash))
    removed = tuple(key for key in known if key not in current)
    return Reconciliation(tuple(added), tuple(changed), tuple(unchanged), removed, tuple(objects))
