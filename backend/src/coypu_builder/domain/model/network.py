"""Topological network: nodes, links and junction semantics. Mode-agnostic — see ADR 0006."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from coypu_builder.domain.model.ids import EntityId
from coypu_builder.domain.model.modes import Mode


class JunctionKind(StrEnum):
    TURNOUT = "turnout"
    CROSSING = "crossing"
    SLIP = "slip"
    INTERSECTION = "intersection"
    BUFFER_STOP = "buffer_stop"
    CONNECTION = "connection"


@dataclass(frozen=True, slots=True)
class Junction:
    kind: JunctionKind
    properties: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Node:
    id: EntityId
    position: tuple[float, float, float]
    junction: Junction | None = None
    name: str = ""


@dataclass(frozen=True, slots=True)
class Link:
    id: EntityId
    alignment_id: EntityId
    start_node: EntityId
    end_node: EntityId
    s_start: float
    s_end: float
    name: str = ""


@dataclass(frozen=True, slots=True)
class Network:
    id: EntityId
    mode: Mode
    nodes: tuple[Node, ...]
    links: tuple[Link, ...]
    name: str = ""
