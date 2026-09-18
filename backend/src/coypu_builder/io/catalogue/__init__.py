"""Catalogue loaders: `shared/catalogue/*` is consumed by both the backend and the Godot client."""

from coypu_builder.io.catalogue.vehicles import (
    CATALOGUE_SCHEMA,
    Catalogue,
    VehicleSpecError,
    catalogue_root,
    load_catalogue,
    load_vehicle_spec,
    resolve,
)

__all__ = [
    "CATALOGUE_SCHEMA",
    "Catalogue",
    "VehicleSpecError",
    "catalogue_root",
    "load_catalogue",
    "load_vehicle_spec",
    "resolve",
]
