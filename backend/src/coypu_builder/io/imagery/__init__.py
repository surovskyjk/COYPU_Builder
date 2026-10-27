"""Imagery sources (T-134): readers of imagery files, and the factories that turn their payloads into the
layers `domain/imagery.py` composes.

`register_imagery_readers()` puts `RasterImageReader` on the reader registry of `io/sources`; it is
idempotent, and the server calls it at start-up next to `register_builtin_readers()`. A payload type is turned
into a layer by `layer_for_payload`, through the factory registered for its type. Part A registers
`ImageryRaster`; a provider (T-134 Part B) registers its own payload type the same way. Importing this package
imports no GIS library: Pillow and rasterio are imported by the modules that use them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coypu_builder.domain.imagery import ImageryLayer, ImageryRaster, RasterLayer
from coypu_builder.io.sources import registry

LayerFactory = Callable[[str, Any], ImageryLayer]

_LAYER_FACTORIES: dict[type, LayerFactory] = {}


def register_layer_factory(payload_type: type, factory: LayerFactory) -> None:
    """Make `factory(source_id, payload)` the way a payload of `payload_type` becomes a layer. A later
    registration for the same type replaces the earlier one."""
    _LAYER_FACTORIES[payload_type] = factory


def layer_for_payload(source_id: str, payload: object) -> ImageryLayer:
    """The layer for one imagery payload. Raises `ValueError` for a payload type nobody registered."""
    for payload_type, factory in _LAYER_FACTORIES.items():
        if isinstance(payload, payload_type):
            return factory(source_id, payload)
    raise ValueError(f"no imagery layer for a payload of type {type(payload).__name__}")


def register_imagery_readers() -> None:
    """Register the built-in imagery readers that are not registered yet."""
    from coypu_builder.io.imagery.raster_image import RasterImageReader

    known = {reader.format_id for reader in registry.readers()}
    if RasterImageReader.format_id not in known:
        registry.register_reader(RasterImageReader())


register_layer_factory(ImageryRaster, lambda source_id, raster: RasterLayer(source_id, raster))
