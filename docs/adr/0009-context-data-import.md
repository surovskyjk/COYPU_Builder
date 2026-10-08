# ADR 0009 — Context data import: explicit settings, stable identity, provenance

Status: accepted (2026-10-06)

## Context

Phase 1 M3 brings terrain and imagery; M5 brings buildings, vegetation and water bodies. Each arrives either
from a file the user supplies (a ČÚZK DMR 5G point cloud, a GeoTIFF, a LandXML surface, a building dataset)
or from a generic open-data provider that is low resolution but fast and available everywhere.

Files are often silent or wrong about their coordinate system. The first real sample, DMR 5G sheet KRAV82
(LAS 1.4, LASzip), carries no CRS record at all; its values are S-JTSK / Krovak East North only by inference
from their range. Imported objects must also stay traceable through their lifecycle: re-import of an updated
source, save and reopen, and IFC export in Phase 3.

## Decision

1. **Import is two steps: inspect, then import.** `inspect` reads a source cheaply and reports its format,
   declared CRS (or that none is declared), extent, counts and a proposed `ImportSettings`. The user confirms
   or overrides the settings, and `import` runs with exactly those. A source with no declared CRS and no
   user-supplied one is refused with a clear error, never guessed. An override of a declared CRS is recorded
   and reported.
2. **`ImportSettings` are explicit and stored with the result:** horizontal CRS (EPSG code or WKT), vertical
   CRS or datum, coordinate order and sign convention (the Křovák conventions of `io/landxml/dialects.py`
   apply to every format), linear unit, clip region (a corridor buffer in metres or a bounding box), point
   class filter for point clouds, display resolution and nodata handling. Reprojection to the project CRS goes
   through `domain/crs.py`; the transformation pyproj actually used, and its stated accuracy, are recorded.
3. **Identity.** Every imported object receives an `EntityId` (uuid4, as in ADR 0005 and
   `docs/data-contracts/entity-model.md`) at its first import. Every object belongs to a `SourceDataset`
   record, which has its own `EntityId` and holds the kind (terrain, imagery, buildings, vegetation, water),
   the origin (file path and content hash, or provider id and query), licence and attribution, the
   `ImportSettings`, the transformation used, the import time and the tool version. Each object stores its
   native source key: an OSM element id, a RÚIAN code, a file plus feature index, or a tile id.
4. **Re-import keeps GUIDs.** Re-importing a `SourceDataset` matches objects on their native key. Matched
   objects keep their GUID; unmatched ones are added or removed, and the import report lists added, changed and
   removed objects. GUIDs are never derived from source keys, because two projects or two design variants
   importing the same object must not collide when federated as IFC.
5. **Terrain has an analysis surface and a display surface.** The backend keeps the source surface as the
   truth for every ground query (sampling, cut/fill, draping): a TIN over the original points for point
   sources (DMR 5G, LandXML surfaces) and the native grid for raster sources, clipped to the corridor buffer.
   The client renders regular heightfield tiles with an LOD pyramid derived from it. Display tiles are never
   used for analysis.
6. **Sources layer by precedence.** Detailed user files take precedence over generic providers where both
   cover the ground, and the boundary between them is kept so the client can show which source applies where.
7. **Providers are interchangeable with files.** Generic open-data providers sit behind the same interface as
   file readers, fetch through the disk cache, are bounded to the corridor buffer and are never on the CI
   path. CI uses synthetic sources generated in code. Each provider's licence is verified in its task spec
   before it ships. Each provider also declares an https-only host allow-list. A URL taken from fetched
   content (a feed, a capabilities document) and every redirect hop must pass it before any request is sent,
   because fetched content is untrusted input that names further downloads. Network XML is parsed with
   `defusedxml`. T-139's ČÚZK client is the first provider to follow these rules.
8. **Source data stays outside the repository.** The project stores the `SourceDataset` record and a
   reference to the source, never the source data itself.
9. **Attribution is visible.** The client shows the attribution of every source contributing to the view.

## Consequences

- Import gains a confirmation step, but nothing lands in the wrong place silently.
- Persistence gains a `source_dataset` table in M4 (T-145). M5 brings ADR 0005's `entity` and `blob` tables
  forward for context features only; the track entity set still settles in Phase 2.
- Point clouds add `laspy` with a LAZ backend to the `gis` extra (ADR 0008). These readers, and M5's vector
  readers, are imported lazily inside their `io/` modules, like every other heavy optional dependency.
- Phase 3's IFC export carries these GUIDs as `GlobalId`s, so context objects keep one identity from import
  to export.
