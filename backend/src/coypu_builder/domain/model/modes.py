"""Transport modes. Everything in the LRS model is mode-agnostic; modes only tag semantics."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    HEAVY_RAIL = "heavy_rail"
    LIGHT_RAIL_TRAM = "light_rail_tram"
    TROLLEYBUS = "trolleybus"
    ROAD_SERVICE = "road_service"

    @property
    def is_rail(self) -> bool:
        return self in (Mode.HEAVY_RAIL, Mode.LIGHT_RAIL_TRAM)
