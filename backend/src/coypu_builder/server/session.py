"""Per-connection server state: one `Session` per WebSocket, at most one open project (ADR 0003)."""

from __future__ import annotations

from dataclasses import dataclass, field

from coypu_builder.domain.crs import BasePoint, ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment
from coypu_builder.domain.kinematics import KinematicsRun, Stop
from coypu_builder.domain.model.ids import EntityId
from coypu_builder.domain.model.trainset import Trainset


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
class ProjectState:
    project_id: str
    crs: ProjectCRS | None = None
    base_point: BasePoint | None = None
    alignments: dict[str, AlignmentEntry] = field(default_factory=dict)
    runs: dict[str, RunEntry] = field(default_factory=dict)
    trainsets: dict[str, Trainset] = field(default_factory=dict)
    stops: tuple[Stop, ...] = ()


@dataclass
class Session:
    session_id: str
    expected_token: str = ""
    hello_received: bool = False
    project: ProjectState | None = None
