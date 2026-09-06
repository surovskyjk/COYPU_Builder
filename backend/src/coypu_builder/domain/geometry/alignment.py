from __future__ import annotations

from dataclasses import dataclass

from coypu_builder.domain.geometry.cant import CantProfile
from coypu_builder.domain.geometry.horizontal import HorizontalAlignment
from coypu_builder.domain.geometry.vertical import VerticalAlignment
from coypu_builder.domain.model.modes import Mode


@dataclass(frozen=True)
class Alignment:
    """Horizontal + vertical + cant sharing one absolute station axis (metres)."""

    horizontal: HorizontalAlignment
    vertical: VerticalAlignment
    cant: CantProfile
    name: str = ""
    mode: Mode = Mode.HEAVY_RAIL

    @property
    def station_start(self) -> float:
        return float(self.horizontal.stations[0])

    @property
    def station_end(self) -> float:
        return self.horizontal.station_end

    @property
    def length(self) -> float:
        return self.horizontal.length

    @classmethod
    def flat(cls, horizontal: HorizontalAlignment, elevation: float = 0.0, **kwargs) -> Alignment:
        s0, s1 = float(horizontal.stations[0]), horizontal.station_end
        return cls(
            horizontal, VerticalAlignment.constant(elevation, s0, s1), CantProfile.zero(s0, s1), **kwargs
        )
