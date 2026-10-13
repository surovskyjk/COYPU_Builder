# Import framework

How every context source (terrain, imagery, later buildings, vegetation and water) enters a project. The
decisions are in ADR 0009 (explicit settings, identity, provenance) and ADR 0010 decision 4 (plugins). The code
is `domain/sources.py` (settings and records, pure), `io/sources/registry.py` (readers, providers, plugins),
`io/sources/runner.py` (CRS resolution, clipping, the import itself) and `config.py` (per-user configuration).
Format-specific readers are separate tasks; this framework only fixes the seams they plug into.

## The flow

1. **Inspect** (`import.inspect`). A reader reports what the source holds: format, declared CRS, extent,
   counts, a proposed `ImportSettings`, a licence hint and warnings. A source that declares no CRS is reported
   with `declared_crs: null` and a warning, whatever the reader says.
2. **Confirm.** The caller keeps or overrides the proposed settings.
3. **Import** (`import.run`) with exactly those settings. They are stored verbatim in the `SourceDataset`.

A source with no declared CRS and no `settings.horizontal_crs` is refused with `E_CRS_REQUIRED`; it is never
guessed. The project must also have a CRS (open it with one, or import an alignment first), otherwise
`import.run` also answers `E_CRS_REQUIRED`.

## `ImportSettings`

| Field | Meaning |
|---|---|
| `horizontal_crs` | EPSG code, WKT or PROJ string. `null` = the source's declared CRS. Must be a horizontal 2D CRS; a compound CRS contributes its horizontal part and a warning. |
| `vertical_crs` | e.g. `"EPSG:5705"`. Validated and recorded; heights are not transformed in Phase 1. |
| `coordinate_order` | `auto`, `EN` or `NE`: which token holds the east-axis and which the north-axis value (`NE` = north first). `auto` means the reader's native order (below). Applied through `io/landxml/dialects.py: resolve_coordinates`, which also holds the Křovák sign rule. |
| `linear_unit_m` | Source unit in metres; scales all three columns. Must be finite and > 0. |
| `clip` | `CorridorClip` or `BBoxClip`, below. |
| `point_classes` | Point-cloud classification codes to keep; `null` = all. Used by point-cloud readers. |
| `display_resolution_m` | Target resolution of display tiles; recorded for T-132. |
| `nodata` | Value that marks a missing height. Must be finite: NaN and infinity cannot ride the JSON wire and a NaN would make the stored settings unequal to themselves, so a reader handles a NaN marker in a raster natively. |

Invalid values (an unknown `coordinate_order`, a non-positive or non-finite unit, resolution, `nodata`, buffer
or box bound, an empty box) raise `ValueError` when the settings are constructed, which the handlers map to
`E_BAD_PARAMS`.

### Token order and the Křovák sign

Order and sign are independent. The order says which token is the east-axis value and which the north-axis
value: `NE` = `(north-axis, east-axis)`, `EN` = `(east-axis, north-axis)`. `auto` is the reader's **native
order**: a reader declares it with a `native_order` attribute (`"NE"` when absent, as for LandXML; point-cloud
and raster readers declare `"EN"`). For a Křovák CRS the east-axis value is Y (westing, positive) or E
(negative) and the north-axis value is X (southing, positive) or N (negative). The sign rule then applies to
the values: when every value is positive they are negated into `(E, N)`, otherwise they already are East-North.
So `coordinate_order="EN"` with EPSG:5514 `(E, N)` tokens is read as written, and a wrong order swaps the axes
visibly (about 280 km at Kralupy) instead of passing silently. See also `landxml-dialects.md`.

### Clip regions

Both are resolved to a shapely polygon in the project CRS, which readers receive as `clip_polygon`:

- `CorridorClip(buffer_m, alignment_ids=(), station_from=None, station_to=None)` goes through T-138's
  `corridor_envelope`, so the polygon is the one the portal export shows (`docs/data-contracts/corridor-envelope.md`).
  Empty `alignment_ids` means every alignment in the project; a station range needs exactly one alignment.
  An unknown id is `E_NOT_FOUND`; a project without alignments is `E_EMPTY`.
- `BBoxClip(min_e, min_n, max_e, max_n)` is a box in the project CRS.

Needs the backend's `gis` extra (shapely); without it the call fails with an error naming `uv sync --extra gis`.

## Coordinate transform

`resolve_transform(settings, declared_crs, project_crs, probe=None) -> CrsTransform`:

- the source CRS is `settings.horizontal_crs`, else `declared_crs`, else `CrsRequired`;
- a reader calls the transform with `(n, 2)` or `(n, 3)` raw tokens in file order; it scales them by
  `linear_unit_m`, reads them as (E, N) through `resolve_coordinates` (the order rule above, with the reader's
  `native_order`), then reprojects to the project CRS through `domain/crs.py`. Heights are scaled only;
- for the Křovák families whose axes are south/west positive (EPSG:5513, 2065, 8352) the sign rule yields
  East-North values, so they are reprojected from the East-North twin (5514, 5221, 8353). This changes
  axis signs only, never the datum, so declaring EPSG:5513 and EPSG:5514 land on the same points;
- an identical source and project CRS is `transformation: "none"`, accuracy 0.0 m;
- otherwise the record holds pyproj's description and stated accuracy of the operation applied at the
  project's location (the clip's centroid, else the start of the first alignment, else the centre of the
  project CRS's area of use). pyproj's own `Transformer.description` is "unavailable until proj_trans is called"
  when several operations compete, so the applied one is identified from `TransformerGroup`;
- overriding a declared CRS with a different one adds a warning to the import result and the record;
- `import.inspect` always adds the "declares no coordinate reference system" warning when `declared_crs` is
  null, in addition to whatever the reader reported.

## Records and identity

`SourceDataset` (`domain/sources.py`) holds: `id`, `kind`, `name`, `format_id`, `origin` (`FileOrigin` with the
absolute paths and SHA-256 of every input file, or `ProviderOrigin` with provider id and canonical query JSON),
`settings` as confirmed, `declared_crs`, `effective_crs`, `transformation`, `transformation_accuracy_m`,
`licence`, `attribution`, `imported_at` (ISO 8601 UTC, whole seconds), `tool_version`
(`coypu-builder <version>`) and `objects`. The server also keeps the import's `warnings` with the source, and
`source.get` returns them.

For a file, `licence` is the reader's `licence_hint` (or `"unknown"`) and `attribution` is empty; a provider
supplies both.

Each `ObjectRecord` is `(key, id, content_hash)`:

- `key` is the object's own source key (a file plus sheet or feature index, a tile id, an OSM element id);
- `id` is a uuid4 hex `EntityId` minted at first import. **GUIDs are never derived from keys**, so two projects
  importing the same object do not collide when federated as IFC;
- `content_hash` is the reader's hash of the imported object, so a change shows up as `changed`.

`reconcile(previous, current, make_id)` matches a re-import on keys: a matched key keeps its GUID whether or not
its hash changed; a new key gets a fresh GUID; a key missing from the new import is `removed`. Only
`reimport_of` reconciles. Two separate imports of one file are two sources with disjoint GUIDs. A re-import
keeps the source's id, replaces its record and payloads, and leaves the old state untouched if it fails.
`reimport_of` must name a source of the same kind and format.

## Readers and providers

A `SourceReader` has `format_id`, `kinds`, `suffixes`, `sniff(path)`, `inspect(path)` and
`read(path, settings, transform, clip_polygon)`, which yields `ImportedObject(key, content_hash, payload)`.
`reader_for(path, format_id=None)` picks the named reader, else the first registered whose `sniff` accepts the
file (suffix matches first), else raises `ValueError`. Built-in readers register directly with
`register_reader`; the three terrain readers (`las`, `geotiff`, `landxml-surface`) are described in
`terrain-payloads.md`. A `SourceProvider` (`provider_id`, `kinds`, `licence`, `attribution`,
`inspect`, `fetch`) sits behind the same seam; its files go through the readers (ADR 0009 decision 7).

A reader's `ValueError` and `OSError` become `E_BAD_PARAMS`. Everything else is a reader bug and becomes
`E_INTERNAL`, never a dropped connection: any other exception from `sniff`, `inspect` or `read`, a malformed
inspect report, and an object that is not an `ImportedObject` with string `key` and `content_hash`. A raising
`sniff` is skipped in favour of another reader that matches; it is only reported when nothing else does.

## Plugins

Third-party readers and providers are Python entry points in the groups `coypu_builder.readers` and
`coypu_builder.providers`. The entry point may name a reader or provider instance, a class, or a zero-argument
factory.

| State | Meaning |
|---|---|
| `available` | discovered but not in the allow-list. `EntryPoint.load()` is never called, so none of its code runs. |
| `loaded` | allow-listed, imported and registered. |
| `failed` | allow-listed but raised while loading or registering (including a duplicate `format_id`); `error` holds `ExceptionType: message`. Loading continues with the others. |

Plugins are trusted code. They are loaded once per process: at backend startup (problems go to stderr, never to
the stdout startup line) and lazily before the first `import.inspect`, `import.run` or `plugins.list`.

### Per-user `config.toml`

Windows: `%APPDATA%\COYPU Builder\config.toml`. Elsewhere: `$XDG_CONFIG_HOME/coypu-builder/config.toml`,
defaulting to `~/.config/coypu-builder/config.toml`. The backend only reads it.

```toml
[plugins]
allow = ["example-reader"]        # entry-point names
```

A missing file gives the defaults. An unreadable or malformed file, or a wrongly typed value, gives the
defaults and a warning, which `plugins.list` returns in `warnings`. The file is decoded as UTF-8 with an
optional BOM, so a file saved by Windows PowerShell keeps its allow-list.

If the package metadata itself cannot be enumerated, each plugin group is reported as one `failed` entry named
`<group>`; the backend keeps serving.

## Protocol

All five methods are `experimental` (`docs/protocol/ipc.md`): `import.inspect`, `import.run`, `source.list`,
`source.get`, `plugins.list`. Paths must be absolute; `import.inspect` and `import.run` take either `path` or `paths` (several files of one
format are one source, one object per file; see `terrain-payloads.md`). `ImportSettingsDTO.clip` is a tagged union on `type`:
`{"type": "corridor", ...}` or `{"type": "bbox", ...}`, or `null`. `SourceDatasetDTO.origin` is tagged
`"file"` or `"provider"`.

| Condition | Error |
|---|---|
| relative path, unknown `kind`, unknown `format_id`, no reader fits, invalid settings, reader `ValueError` | `E_BAD_PARAMS` |
| missing file, unknown `reimport_of`, `source_id` or clip `alignment_ids` | `E_NOT_FOUND` |
| no declared CRS and none in the settings, or no project CRS | `E_CRS_REQUIRED` |
| corridor clip in a project without alignments | `E_EMPTY` |
| no project open (`import.run`, `source.*`) | `E_NO_PROJECT` |
| reader bug, missing `gis` extra | `E_INTERNAL` |

## Synthetic terrain for tests

`backend/tests/fixtures/synthetic/terrain.py` is an analytic surface over the Kralupy KRAV82 area (EPSG:5514):
`surface(e, n)` plus `sample_irregular`, `sample_grid`, `split_sheets` and a plain-text test file format
(`write_synthetic`, `read_synthetic`). The format is test-only, and so is the `SyntheticXyzReader` in
`tests/test_import_framework.py`. Every later M3 test reuses them.
