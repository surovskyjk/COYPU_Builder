"""Catalogue-backed items placed by LRS anchor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from coypu_builder.domain.model.anchor import LrsAnchor
from coypu_builder.domain.model.ids import EntityId


@dataclass(frozen=True, slots=True)
class Asset:
    id: EntityId
    catalogue_id: str
    anchor: LrsAnchor
    layer_id: EntityId | None = None
    properties: Mapping[str, object] = field(default_factory=dict)
    name: str = ""
