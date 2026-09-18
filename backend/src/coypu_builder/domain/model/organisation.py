"""Cross-cutting organisation of entities: display layers and work-breakdown structure."""

from __future__ import annotations

from dataclasses import dataclass

from coypu_builder.domain.model.ids import EntityId


@dataclass(frozen=True, slots=True)
class Layer:
    id: EntityId
    name: str
    visible: bool = True
    opacity: float = 1.0
    parent_id: EntityId | None = None


@dataclass(frozen=True, slots=True)
class WbsNode:
    id: EntityId
    code: str
    name: str
    parent_id: EntityId | None = None
