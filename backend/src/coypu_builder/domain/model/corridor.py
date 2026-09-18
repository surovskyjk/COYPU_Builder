"""Corridor and cross-section interval types. Declared only — section geometry is Phase 2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from coypu_builder.domain.model.ids import EntityId


@dataclass(frozen=True, slots=True)
class CrossSectionInterval:
    s_start: float
    s_end: float
    section_id: str
    parameters: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Corridor:
    id: EntityId
    alignment_id: EntityId
    intervals: tuple[CrossSectionInterval, ...] = ()
    name: str = ""
