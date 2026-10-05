# T-139 — ČÚZK ATOM client: sheet selection and download

**Milestone:** P1.M3 · **Depends on:** T-138 · **Blocks:** T-133 (ČÚZK terrain providers), T-134 (ČÚZK
orthophoto), T-151 (building heights from DMP 1G)

## Context

The Kralupy corridor needs 12–16 ČÚZK map sheets of terrain, and selecting them by hand in a web viewer is
slow and error-prone. ČÚZK publishes free INSPIRE ATOM download services organised by SM5 map sheet. Each
service feed is also a sheet index: every entry carries the sheet's code, name, `updated` timestamp and outline
polygon. With T-138's corridor envelope, the backend can therefore work out exactly which sheets a corridor
touches and download them.

This task builds that client as a library and a CLI. Turning the downloads into imports is T-133's and T-134's
job, but the manifest written here is the provenance those imports record (ADR 0009, decision 3).

Facts checked on 2026-10-06 (see the ROADMAP M3 section):

- Service feeds are about 24 MB with 16,301 entries each.
- Entries link a per-sheet dataset feed, which links one ZIP on `openzu.cuzk.gov.cz` with a `length`
  attribute.
- The file server sends `Content-Length`, `ETag` and `Last-Modified`, and accepts byte ranges.
- The rights statement reads "žádné podmínky neplatí" (no conditions apply), and ČÚZK's open data is licensed
  CC BY 4.0. Attribution: "© ČÚZK".

## Preconditions

- T-138: `io/gis/envelope.py: corridor_envelope(alignments, options)` and `EnvelopeOptions`, the `envelope`
  CLI, and the `gis` extra installed in CI.
- `domain/crs.py` for reprojection.
- `.gitignore` does not ignore `*.zip`, so downloads must never land inside the repository (see
  Invariants).

Read before starting: `docs/adr/0009-context-data-import.md`, `docs/data-contracts/corridor-envelope.md`
(from T-138), the "ČÚZK download service" paragraph under P1.M3 in `ROADMAP.md`.

## Deliverables

| Path | Action |
|---|---|
| `backend/src/coypu_builder/io/gis/cuzk_atom.py` | new — feeds, sheet index, selection, download |
| `backend/src/coypu_builder/cli/cuzk.py` | new — `cuzk sheets` and `cuzk download` |
| `backend/src/coypu_builder/__main__.py` | register the subcommand |
| `backend/pyproject.toml`, `backend/uv.lock` | add `defusedxml` to the `gis` extra |
| `backend/tests/test_cuzk_atom.py` | new — synthetic feeds and a local HTTP test server, no network |
| `backend/tests/conftest.py` | register a `network` marker, deselected by default |
| `docs/data-contracts/cuzk-atom.md` | new — datasets, feed structure, manifest format, politeness rules |
| `CLAUDE.md` | one `cuzk` example line in Commands |

## Contract

```python
# io/gis/cuzk_atom.py — requests, shapely and defusedxml are imported inside functions
class CuzkDataset(StrEnum):
    DMR5G = "DMR5G"            # LAZ, ground points, S-JTSK
    DMR4G = "DMR4G-TIFF"       # GeoTIFF, 5 m grid, S-JTSK
    DMP1G = "DMP1G"            # LAZ, surface incl. buildings and vegetation, S-JTSK
    ORTOFOTO = "ORTOFOTO"      # JPEG + world files

SERVICE_FEEDS: Mapping[CuzkDataset, str] = {
    CuzkDataset.DMR5G: "https://atom.cuzk.gov.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml",
    CuzkDataset.DMR4G: "https://atom.cuzk.gov.cz/DMR4G-SJTSK-TIFF/DMR4G-SJTSK-TIFF.xml",
    CuzkDataset.DMP1G: "https://atom.cuzk.gov.cz/DMP1G-SJTSK/DMP1G-SJTSK.xml",
    CuzkDataset.ORTOFOTO: "https://atom.cuzk.gov.cz/ORTOFOTO/ORTOFOTO.xml",
}

@dataclass(frozen=True)
class SheetEntry:
    dataset: CuzkDataset
    code: str                     # e.g. "KRAV82"
    name: str                     # e.g. "Kralupy nad Vltavou 8-2", from the entry title
    edition: str | None           # orthophoto only, e.g. "WRTO24.2025"; None for terrain
    dataset_id: str               # inspire_dls:spatial_dataset_identifier_code
    updated: datetime
    outline_lonlat: tuple[tuple[float, float], ...]   # closed ring, (longitude, latitude)
    dataset_feed_url: str

@dataclass(frozen=True)
class SheetFile:
    sheet: SheetEntry
    url: str
    length: int                   # bytes, from the dataset feed's link
    media_type: str

@dataclass(frozen=True)
class DownloadRecord:             # one manifest entry
    dataset: str
    code: str
    edition: str | None
    url: str
    file: str                     # relative to the output directory
    length: int
    sha256: str
    etag: str | None
    last_modified: str | None
    feed_updated: str             # ISO 8601
    downloaded_at: str            # ISO 8601 UTC

def default_cache_dir() -> Path: ...
def load_sheet_index(dataset: CuzkDataset, cache_dir: Path, *, refresh: bool = False) -> list[SheetEntry]: ...
def select_sheets(index: Sequence[SheetEntry], envelope, project_crs: ProjectCRS) -> list[SheetEntry]: ...
def resolve_files(sheets: Sequence[SheetEntry], cache_dir: Path) -> list[SheetFile]: ...
def download(files: Sequence[SheetFile], out_dir: Path, *, max_total_mb: float = 500.0,
             concurrency: int = 2, progress: Callable[[str, int, int], None] | None = None) -> list[DownloadRecord]: ...
```

- **`georss:polygon` lists `latitude longitude` pairs** (`50.2332 14.3198 …`). Store them as
  `(longitude, latitude)` and transform with `always_xy=True`. A swapped axis selects sheets in the wrong
  country without any error, so test it explicitly.
- The sheet code is the identifier's last `_`-separated token, with any orthophoto edition prefix split off at
  the last `.`; for example, `CZ-00025712-CUZK_ORTOFOTO_WRTO24.2025.KRAV82` gives code `KRAV82` and edition
  `WRTO24.2025`.
- **Parse every downloaded feed with `defusedxml`**, never with the stdlib parser directly. The feeds come
  from the network.
- Caching: the cache directory defaults to `%LOCALAPPDATA%\COYPU Builder\cache\cuzk-atom\` on Windows and
  `~/.cache/coypu-builder/cuzk-atom/` elsewhere. Fetch service feeds with `If-None-Match` and
  `If-Modified-Since`, keep a 304 response as a cache hit, and persist the parsed index so a cache hit does not
  re-parse 24 MB.
- Download:
  - write to `<out>/<dataset>/<code>[.<edition>].zip` through a `.part` file, resuming with a `Range` request
    when one exists;
  - check the final size against the feed's `length` and the server's `Content-Length`;
  - record SHA-256;
  - maintain `<out>/cuzk_manifest.json`, a list of `DownloadRecord`;
  - on a rerun, skip a file whose manifest entry still matches the server's `ETag` and length.
- Politeness:
  - at most `concurrency` simultaneous downloads (default 2, capped at 4);
  - a `User-Agent` naming COYPU Builder and its version;
  - retry 5xx and timeouts with exponential backoff, at most 3 attempts;
  - fail fast on 4xx.
- **Size guard:** before downloading, sum the selected files' lengths. If the total exceeds `max_total_mb`,
  refuse and report the total; the user raises the limit explicitly. One orthophoto sheet is about 58 MB.

### CLI

```text
coypu-builder-backend cuzk sheets   (<file.xml> [--buffer 250] [--from S] [--to S] | --envelope <file>)
                                    --dataset DMR5G [--refresh]
coypu-builder-backend cuzk download (same selection) --dataset DMR5G --out <dir> [--max-total-mb 500]
                                    [--concurrency 2]
```

`sheets` prints one line per sheet (code, name, edition, file size) and the total size. `--envelope` accepts a
GeoJSON or Shapefile written by T-138's `envelope` command. `download` refuses an `--out` inside the
repository.

## Invariants

- Nothing is downloaded into the repository; tests use `tmp_path` and a local server.
- No test touches the network unless explicitly selected with `-m network`. CI never selects it.
- The client does not import anything into a project and adds no protocol method (see Out of scope).
- All network access lives in `io/gis/cuzk_atom.py`; nothing in `domain/` changes.

## Acceptance criteria

1. **Feed parsing (synthetic feed):** entries become `SheetEntry` objects with lon/lat in the right order,
   orthophoto editions split off, and `updated` parsed. A feed carrying an entity-expansion payload is
   rejected.
2. **Selection (synthetic):** an envelope that crosses two of four synthetic sheets selects exactly those two.
   A swapped-axis outline would select none, and a test proves the code does not.
3. **Conditional fetch:** against the local test server, a second `load_sheet_index` sends `If-None-Match`,
   gets 304, and returns the cached index without re-parsing.
4. **Resume and integrity:**
   - a transfer the server cuts off midway completes with a `Range` request;
   - a size mismatch fails, and the `.part` file is never renamed to `.zip`;
   - the manifest records SHA-256, `ETag` and `Last-Modified`.
5. **Idempotence:** rerunning `download` with an unchanged server downloads nothing. Changing a file's
   `ETag` on the server makes exactly that file download again.
6. **Size guard:** a selection over `max_total_mb` is refused, and the message reports the total.
7. **Politeness:** the server never sees more concurrent requests than `concurrency`; a 503 is retried with
   backoff; a 404 fails without retry.
8. **Live check (`-m network`, run in Verification, not in CI):**
   - Kralupy at a 250 m buffer selects between 12 and 16 DMR 5G sheets, including `KRAV82`.
   - Downloading them into `D:\COYPU_Builder\Data\Terrain_Samples\cuzk\` succeeds.
   - The downloaded `KRAV82` ZIP has the same size as `D:\COYPU_Builder\Data\Terrain_Samples\KRAV82.zip`, or
     the report explains the difference.
9. Both suites are green on Windows and on Linux CI, with no previously passing test removed, skipped or
   weakened.

## Out of scope

- Importing downloaded files into a project — T-131 readers, T-133 and T-134 providers.
- A protocol method and the client-side source picker — T-133 and T-136.
- Other ČÚZK ATOM datasets (ZABAGED, RÚIAN) — T-154.
- Orthophoto tiling — T-134.

## Verification

```bash
cd backend
uv sync --extra gis
uv run ruff check . ../tools && uv run ruff format --check . ../tools
uv run pytest -q
uv run pytest -q -m network tests/test_cuzk_atom.py
uv run coypu-builder-backend cuzk sheets tests/fixtures/kralupy/kralupy_neratovice_092.xml --buffer 250 --dataset DMR5G
uv run coypu-builder-backend cuzk sheets tests/fixtures/kralupy/kralupy_neratovice_092.xml --buffer 250 --dataset ORTOFOTO
uv run coypu-builder-backend cuzk download tests/fixtures/kralupy/kralupy_neratovice_092.xml --buffer 250 --dataset DMR5G --out D:/COYPU_Builder/Data/Terrain_Samples/cuzk
```

Do not download the orthophoto; list it only, to report its total size.

```bash
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client --editor --quit
tools\godot\Godot_v4.7.2-stable_win64_console.exe --headless --path client -s addons/gdUnit4/bin/GdUnitCmdTool.gd -a tests --ignoreHeadlessMode
```

## Report back

State:
- the Kralupy sheet list for DMR 5G with its total size, and the orthophoto total;
- the index load time, cold and cached;
- download wall-clock time;
- the `KRAV82` size comparison;
- how the axis-order test is built;
- anything in the live feeds that differed from this spec's description.
