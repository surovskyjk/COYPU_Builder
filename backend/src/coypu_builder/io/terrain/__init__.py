"""Terrain readers: LAS/LAZ point clouds, GeoTIFF/COG height grids and LandXML `<Surface>` TIN surfaces.

`register_builtin_readers()` puts the three on the reader registry of `io/sources`; it is idempotent, and the
server calls it at start-up. Importing this package imports none of the heavy optional dependencies (laspy,
rasterio, defusedxml): each reader imports its own when it is first used.
"""

from __future__ import annotations

from coypu_builder.io.sources import registry


def register_builtin_readers() -> None:
    """Register the built-in terrain readers that are not registered yet."""
    from coypu_builder.io.terrain.landxml_surface import LandXmlSurfaceReader
    from coypu_builder.io.terrain.las import LasReader
    from coypu_builder.io.terrain.raster import GeoTiffReader

    known = {reader.format_id for reader in registry.readers()}
    for reader in (LasReader(), GeoTiffReader(), LandXmlSurfaceReader()):
        if reader.format_id not in known:
            registry.register_reader(reader)
