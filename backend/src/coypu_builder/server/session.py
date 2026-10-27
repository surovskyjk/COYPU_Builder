"""Per-connection server state: one `Session` per WebSocket, at most one open project (ADR 0003)."""

from __future__ import annotations

import asyncio
import itertools
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from coypu_builder.domain.crs import BasePoint, ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.kinematics import KinematicsRun, Stop
from coypu_builder.domain.model.ids import EntityId
from coypu_builder.domain.model.trainset import Trainset
from coypu_builder.domain.sources import SourceDataset, SourceKind
from coypu_builder.domain.terrain_surface import TerrainStack
from coypu_builder.io.mesh.imagery_tiles import ImageryTile
from coypu_builder.io.mesh.terrain_tiles import TerrainTile

TILE_CACHE_SIZE = 512
IMAGERY_TILE_CACHE_SIZE = 64
MAX_EXECUTING = 4  # coroutine requests of one connection that do their blocking work at once
_REVISIONS = itertools.count(1)  # process-wide, so a revision is never reused across projects


@dataclass
class AlignmentEntry:
    alignment: Alignment
    warnings: tuple[str, ...] = ()


@dataclass
class RunEntry:
    run: KinematicsRun
    source: str  # "coypu" | "csv"
    alignment_id: EntityId | None = None
    trainset_id: EntityId | None = None


@dataclass
class SourceEntry:
    dataset: SourceDataset
    payloads: dict[EntityId, object]  # reader-specific, keyed by the object's GUID
    warnings: tuple[str, ...] = ()
    coverage: object | None = None  # shapely Polygon / MultiPolygon of the import clip; None = no clip
    resolution_m: float | None = None  # declared resolution of a terrain source (T-133); None = not measured


@dataclass
class TerrainCache:
    """What is derived from a project's terrain sources, valid for one `revision` (T-132)."""

    revision: int = field(default_factory=lambda: next(_REVISIONS))
    fingerprint: tuple | None = None
    stack: TerrainStack | None = None  # built lazily, off the event loop
    build: Any = None  # the asyncio future of the build in flight, shared by concurrent requests
    baking: dict[tuple[int, int, int], Any] = field(default_factory=dict)  # tile bakes in flight
    coverage: tuple = ()  # per source: a bounds box or a shapely geometry
    source_ids: tuple[str, ...] = ()  # the stack's sources in precedence order: what `source_index` indexes
    warnings: tuple[str, ...] = ()
    level_keys: dict[int, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    meta: dict[tuple[int, int, int], tuple[tuple[float, float, float], float]] = field(default_factory=dict)
    tiles: OrderedDict[tuple[int, int, int, int], TerrainTile] = field(default_factory=OrderedDict)
    max_tiles: int = TILE_CACHE_SIZE

    def get_tile(self, level: int, ix: int, iy: int) -> TerrainTile | None:
        key = (self.revision, level, ix, iy)
        tile = self.tiles.get(key)
        if tile is not None:
            self.tiles.move_to_end(key)
        return tile

    def put_tile(self, tile: TerrainTile) -> None:
        self.tiles[(self.revision, tile.level, tile.ix, tile.iy)] = tile
        self.meta[(tile.level, tile.ix, tile.iy)] = (tile.origin, tile.valid_fraction)
        while len(self.tiles) > self.max_tiles:
            self.tiles.popitem(last=False)


@dataclass
class ImageryCache:
    """What is composed from a project's imagery sources and its terrain, valid for one `revision` (T-134).

    `layers` (precedence order), `attributions`, `notices` and `warnings` are filled from the sources as they
    are when the handlers first use the cache (`layers is None` until then); `heights_range` is the relief
    ramp, built off the event loop with the terrain stack. The fingerprint is `(terrain revision, imagery
    sources)`, so the revision changes when either changes."""

    revision: int = field(default_factory=lambda: next(_REVISIONS))
    fingerprint: tuple | None = None
    layers: tuple | None = None
    attributions: dict[str, str] = field(default_factory=dict)
    notices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    heights_range: tuple[float, float] | None = None
    range_ready: bool = False
    build: Any = None  # the asyncio future of the relief ramp in flight
    composing: dict[tuple[int, int, int], Any] = field(default_factory=dict)  # tile composes in flight
    tiles: OrderedDict[tuple[int, int, int], ImageryTile] = field(default_factory=OrderedDict)
    max_tiles: int = IMAGERY_TILE_CACHE_SIZE

    def get_tile(self, level: int, ix: int, iy: int) -> ImageryTile | None:
        key = (level, ix, iy)
        tile = self.tiles.get(key)
        if tile is not None:
            self.tiles.move_to_end(key)
        return tile

    def put_tile(self, tile: ImageryTile) -> None:
        self.tiles[(tile.level, tile.ix, tile.iy)] = tile
        while len(self.tiles) > self.max_tiles:
            self.tiles.popitem(last=False)


@dataclass
class ProjectState:
    project_id: str
    crs: ProjectCRS | None = None
    base_point: BasePoint | None = None
    alignments: dict[str, AlignmentEntry] = field(default_factory=dict)
    runs: dict[str, RunEntry] = field(default_factory=dict)
    trainsets: dict[str, Trainset] = field(default_factory=dict)
    stops: tuple[Stop, ...] = ()
    sources: dict[EntityId, SourceEntry] = field(default_factory=dict)
    terrain: TerrainCache = field(default_factory=TerrainCache, repr=False)
    imagery: ImageryCache = field(default_factory=ImageryCache, repr=False)
    terrain_build_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)  # one Qhull at a time

    def _terrain_fingerprint(self) -> tuple:
        return tuple(
            (source_id, id(entry), id(entry.payloads))
            for source_id, entry in self.sources.items()
            if entry.dataset.kind == SourceKind.TERRAIN
        )

    def _imagery_fingerprint(self) -> tuple:
        return (
            self.terrain_cache().revision,
            tuple(
                (source_id, id(entry), id(entry.payloads))
                for source_id, entry in self.sources.items()
                if entry.dataset.kind == SourceKind.IMAGERY
            ),
        )

    def invalidate_terrain(self) -> None:
        """Drop everything derived from the sources; the next terrain call gets a new revision."""
        self.terrain = TerrainCache(fingerprint=self._terrain_fingerprint())

    def invalidate_imagery(self) -> None:
        """Drop what is composed from the imagery sources; the terrain stack is kept."""
        self.imagery = ImageryCache(fingerprint=self._imagery_fingerprint())

    def terrain_cache(self) -> TerrainCache:
        """The terrain cache, replaced first if the terrain sources changed since it was built. The handlers
        call `invalidate_terrain` on an import; the fingerprint also catches a source set directly."""
        fingerprint = self._terrain_fingerprint()
        if self.terrain.fingerprint != fingerprint:
            self.terrain = TerrainCache(fingerprint=fingerprint)
        return self.terrain

    def imagery_cache(self) -> ImageryCache:
        """The imagery cache, replaced when the terrain revision or the imagery sources changed. An
        imagery-only change keeps the terrain cache and its stack."""
        fingerprint = self._imagery_fingerprint()
        if self.imagery.fingerprint != fingerprint:
            self.imagery = ImageryCache(fingerprint=fingerprint)
        return self.imagery


@dataclass
class Session:
    session_id: str
    expected_token: str | None  # required (fails closed); None only in the test-only no-token mode
    hello_received: bool = False
    project: ProjectState | None = None
    # Work that runs off the event loop passes through `gate`: at most MAX_EXECUTING threads of one
    # connection at a time.
    gate: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_EXECUTING), repr=False)
