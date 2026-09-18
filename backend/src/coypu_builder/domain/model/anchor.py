"""The universal placement for every positioned entity: an offset into an alignment's LRS."""

from __future__ import annotations

from dataclasses import dataclass

from coypu_builder.domain.model.ids import EntityId


@dataclass(frozen=True, slots=True)
class LrsAnchor:
    alignment_id: EntityId
    s: float
    y: float = 0.0
    z: float = 0.0
    heading_offset: float = 0.0
