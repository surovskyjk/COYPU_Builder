"""Imagery sources (T-134): readers of imagery files, and the factories that turn their payloads into the
layers `domain/imagery.py` composes.

`register_imagery_readers()` puts `RasterImageReader`, the WMTS descriptor reader and the ČÚZK sheet reader on
the reader registry of `io/sources`, and the two ČÚZK orthophoto providers (T-134 Part B) on its provider
registry. It is idempotent, and the server calls it at start-up next to `register_builtin_readers()`. A
payload type is turned into a layer by `layer_for_payload`, through the factory registered for its type:
Part A registers `ImageryRaster`, Part B registers `WmtsSource`. Importing this package imports no GIS
library: Pillow, requests and rasterio are imported by the modules that use them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from coypu_builder.domain.imagery import ImageryLayer, ImageryRaster, RasterLayer
from coypu_builder.io.imagery.cuzk_wmts import WmtsLayer, WmtsSource, tile_report
from coypu_builder.io.sources import registry

__all__ = ["layer_for_payload", "register_imagery_readers", "register_layer_factory", "tile_report"]

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
    """Register the built-in imagery readers and providers that are not registered yet."""
    from coypu_builder.io.imagery.cuzk_bulk import CuzkOrthoSheetsProvider, CuzkSheetReader
    from coypu_builder.io.imagery.cuzk_wmts import CuzkWmtsProvider, WmtsDescriptorReader
    from coypu_builder.io.imagery.raster_image import RasterImageReader

    known_readers = {reader.format_id for reader in registry.readers()}
    for reader in (RasterImageReader(), WmtsDescriptorReader(), CuzkSheetReader()):
        if reader.format_id not in known_readers:
            registry.register_reader(reader)
    known_providers = {provider.provider_id for provider in registry.providers()}
    for provider in (CuzkWmtsProvider(), CuzkOrthoSheetsProvider()):
        if provider.provider_id not in known_providers:
            registry.register_provider(provider)


register_layer_factory(ImageryRaster, lambda source_id, raster: RasterLayer(source_id, raster))
register_layer_factory(WmtsSource, lambda source_id, source: WmtsLayer(source_id, source))
