"""Built-in context-data providers (T-133): ČÚZK DMR 5G and DMR 4G, and the Copernicus DEM GLO-30.

A provider sits behind the same seam as a file reader (`io/sources/registry.py: SourceProvider`): `inspect`
answers from the service's index, `fetch` downloads into the provider cache (`cache.py`), and the files then
go through the ordinary readers. `register_builtin_providers()` puts the three on the provider registry; it is
idempotent and the server calls it at start-up. Importing this package imports none of the network or GIS
libraries: each provider module imports its own when it is first used.

The helpers here read a provider `query`, the JSON object a client sends with `provider`: a `clip` in the wire
form of `ImportSettings.clip` and provider-specific limits.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from coypu_builder.domain.sources import BBoxClip, ClipRegion, CorridorClip
from coypu_builder.io.sources import registry


def canonical_query(query: Mapping[str, Any]) -> str:
    """The request as canonical JSON (sorted keys, no spaces): what `ProviderOrigin.query` records."""
    return json.dumps(query, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _number(source: Mapping[str, Any], name: str, *, required: bool = True) -> float | None:
    value = source.get(name)
    if value is None:
        if required:
            raise ValueError(f"clip.{name} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"clip.{name} must be a finite number, got {value!r}")
    return float(value)


def clip_from_query(query: Mapping[str, Any], default_buffer_m: float) -> ClipRegion:
    """The query's `clip` as a domain clip; absent, a corridor of `default_buffer_m` around every alignment.
    The wire shape is the one of `ImportSettings.clip`: `{"type": "corridor", "buffer_m": ...,
    "alignment_ids": [...], "station_from": ..., "station_to": ...}` or `{"type": "bbox", "min_e": ...}`.
    Raises `ValueError` for anything else."""
    raw = query.get("clip")
    if raw is None:
        return CorridorClip(default_buffer_m)
    if not isinstance(raw, Mapping):
        raise ValueError("clip must be an object with a 'type' of 'corridor' or 'bbox'")
    kind = raw.get("type")
    if kind == "corridor":
        ids = raw.get("alignment_ids")
        ids = () if ids is None else ids
        if not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) for i in ids):
            raise ValueError("clip.alignment_ids must be a list of strings")
        buffer_m = _number(raw, "buffer_m", required=False)
        return CorridorClip(
            default_buffer_m if buffer_m is None else buffer_m,
            tuple(ids),
            _number(raw, "station_from", required=False),
            _number(raw, "station_to", required=False),
        )
    if kind == "bbox":
        return BBoxClip(*(float(_number(raw, n)) for n in ("min_e", "min_n", "max_e", "max_n")))  # type: ignore[arg-type]
    raise ValueError(f"clip.type must be 'corridor' or 'bbox', got {kind!r}")


def clip_to_query(clip: ClipRegion) -> dict[str, Any]:
    if isinstance(clip, BBoxClip):
        return {
            "type": "bbox",
            "min_e": clip.min_e,
            "min_n": clip.min_n,
            "max_e": clip.max_e,
            "max_n": clip.max_n,
        }
    out: dict[str, Any] = {"type": "corridor", "buffer_m": clip.buffer_m}
    if clip.alignment_ids:
        out["alignment_ids"] = list(clip.alignment_ids)
    if clip.station_from is not None:
        out["station_from"] = clip.station_from
    if clip.station_to is not None:
        out["station_to"] = clip.station_to
    return out


def check_keys(query: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(query) - set(allowed))
    if unknown:
        raise ValueError(
            f"unknown query field(s) {', '.join(unknown)}; this provider takes {', '.join(allowed)}"
        )


def max_total_mb(query: Mapping[str, Any], default: float) -> float:
    value = query.get("max_total_mb", default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not (math.isfinite(value) and value > 0)
    ):
        raise ValueError(f"max_total_mb must be a number > 0, got {value!r}")
    return float(value)


def register_builtin_providers() -> None:
    """Register the built-in providers that are not registered yet."""
    from coypu_builder.io.providers.copernicus import CopernicusGlo30Provider
    from coypu_builder.io.providers.cuzk_terrain import cuzk_dmr4g_provider, cuzk_dmr5g_provider

    known = {p.provider_id for p in registry.providers()}
    for provider in (cuzk_dmr5g_provider(), cuzk_dmr4g_provider(), CopernicusGlo30Provider()):
        if provider.provider_id not in known:
            registry.register_provider(provider)
