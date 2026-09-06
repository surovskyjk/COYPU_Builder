"""Per-connection server state: one `Session` per WebSocket, at most one open project (ADR 0003)."""

from __future__ import annotations

from dataclasses import dataclass, field

from coypu_builder.domain.crs import BasePoint, ProjectCRS
from coypu_builder.domain.geometry.alignment import Alignment


@dataclass
class AlignmentEntry:
    alignment: Alignment
    warnings: tuple[str, ...] = ()


@dataclass
class ProjectState:
    project_id: str
    crs: ProjectCRS | None = None
    base_point: BasePoint | None = None
    alignments: dict[str, AlignmentEntry] = field(default_factory=dict)


@dataclass
class Session:
    session_id: str
    expected_token: str = ""
    hello_received: bool = False
    project: ProjectState | None = None
