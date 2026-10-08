# ČÚZK ATOM download

ČÚZK publishes its open spatial data through INSPIRE ATOM download services, free of charge and organised by
SM5 map sheet. The backend uses them in two steps: the service feed is the **sheet index**, so a corridor
envelope (`docs/data-contracts/corridor-envelope.md`) selects exactly the sheets it touches; the per-sheet
**dataset feed** then names the one ZIP to download. The code is `io/gis/cuzk_atom.py` (all network access) and
`cli/cuzk.py`. Both need the `gis` extra (`uv sync --extra gis`; `requests`, `shapely` and `defusedxml`).

Importing a downloaded file into a project is not part of this client; the readers and providers of T-131,
T-133 and T-134 do that, and record the manifest entry as provenance (ADR 0009, decision 3).

## Datasets

| `CuzkDataset` | Service feed | Content |
|---|---|---|
| `DMR5G` | `https://atom.cuzk.gov.cz/DMR5G-SJTSK/DMR5G-SJTSK.xml` | LAZ, ground points, S-JTSK, about 2.3 MB per sheet |
| `DMR4G` (`DMR4G-TIFF`) | `https://atom.cuzk.gov.cz/DMR4G-SJTSK-TIFF/DMR4G-SJTSK-TIFF.xml` | GeoTIFF, 5 m grid, S-JTSK |
| `DMP1G` | `https://atom.cuzk.gov.cz/DMP1G-SJTSK/DMP1G-SJTSK.xml` | LAZ, surface including buildings and vegetation, S-JTSK |
| `ORTOFOTO` | `https://atom.cuzk.gov.cz/ORTOFOTO/ORTOFOTO.xml` | JPEG with world files, about 58 MB per sheet |

All four cover about 16,300 sheets. The orthophoto for the Kralupy corridor (15 sheets at a 250 m buffer) is
about 850 MB, so it is opt-in: raise `--max-total-mb` deliberately.

## Feed structure

**Service feed** (about 24 MB, served gzip-compressed): an Atom feed with one `entry` per sheet.

| Element | Meaning |
|---|---|
| `inspire_dls:spatial_dataset_identifier_code` | `CZ-00025712-CUZK_DMR5G-SJTSK_KRAV82`; the last `_`-separated token is the sheet code. For the orthophoto the token carries an edition, `WRTO24.2025.KRAV82`, split at the last `.` into edition `WRTO24.2025` and code `KRAV82` |
| `title` | `... - mapový list: Kralupy nad Vltavou 8-2`; the text after `mapový list:` is the sheet name |
| `updated` | when the sheet was last published (ISO 8601) |
| `georss:polygon` | the sheet outline in WGS 84 as **`latitude longitude`** pairs, closed. The client stores `(longitude, latitude)` and transforms with x = longitude first |
| `link rel="alternate"` | the sheet's dataset feed |

**Dataset feed** (a few hundred bytes): one `entry` whose `link rel="alternate"` carries the ZIP's `href`,
`length` in bytes and `type` (`application/vnd.laszip`, `image/tiff`, `image/jpeg`).

**File server**: sends `Content-Length`, `ETag` and `Last-Modified`, answers `HEAD` and accepts byte ranges.

The feeds say "žádné podmínky neplatí" (no conditions apply). ČÚZK's open data is licensed CC BY 4.0, so every
use is attributed "© ČÚZK" (ADR 0009, decision 9).

Notes from the live services (checked 2026-10-06):

- The DMP 1G service feed is served from `atom.cuzk.gov.cz`, but its entries and file links name
  `atom.cuzk.cz` and `openzu.cuzk.cz`, which is why the allow-list below covers both `cuzk.gov.cz` and
  `cuzk.cz`. The client follows the links it finds, as long as they pass that list.
- `updated` stamps carry a `+02:00` offset all year round; treat them as ordering information only.
- The service feed's `ETag` differs between a gzip and a plain response. Only the validator the client itself
  received is ever sent back, so conditional requests still hit.

## Safety

Every feed comes from the network and is parsed with `defusedxml`, with DTDs, entity expansion and external
references refused (a billion-laughs feed raises `CuzkFeedError`). Sheet codes and editions must match
`[A-Za-z0-9][A-Za-z0-9._-]*` because they become file names.

**URL allow-list.** Feeds carry no checksum, so TLS to a ČÚZK host is the only proof that a download is ČÚZK's.
Every URL taken from a feed (dataset-feed links and file links) and every request the client sends, including
each redirect hop, must therefore:

- use `https` (`ALLOWED_SCHEMES`);
- carry no userinfo (`https://cuzk.gov.cz@evil.example/` is refused);
- name a host that is `cuzk.gov.cz` or `cuzk.cz`, or ends with `.cuzk.gov.cz` or `.cuzk.cz`
  (`ALLOWED_HOSTS`, matched on the dot boundary, so `cuzk.gov.cz.evil.com` and `evilcuzk.gov.cz` fail);
- contain no whitespace, backslash or non-ASCII character, which parsers disagree about.

Redirects are followed by hand, at most 5 hops, and each `Location` is checked before it is requested; a
refused URL raises `CuzkFeedError` and nothing is sent to it. The two module-level tuples are the only seam:
tests widen them to reach a local server and nothing else changes them. The DMP 1G links on `atom.cuzk.cz` and
`openzu.cuzk.cz` pass this list.

## Cache

`default_cache_dir()` is `%LOCALAPPDATA%\COYPU Builder\cache\cuzk-atom\` on Windows and
`~/.cache/coypu-builder/cuzk-atom/` elsewhere.

- `<DATASET>.index.json`: the parsed sheet index with the `ETag` and `Last-Modified` of the feed it came from.
  `load_sheet_index` sends them back as `If-None-Match` and `If-Modified-Since`. A 304 returns the cached index
  without parsing 24 MB; `refresh=True` ignores the cache.
- `<DATASET>/feeds/<code>.json`: one dataset feed's resolved file link, revalidated the same way by
  `resolve_files`.

## Download

`download(files, out_dir, ...)` writes `<out_dir>/<DATASET>/<code>[.<edition>].zip` and maintains
`<out_dir>/cuzk_manifest.json`.

1. **Size guard.** Before any request, the selected files' lengths are summed; over `max_total_mb` (decimal MB,
   default 500) the call raises `CuzkSizeLimitError` naming the total.
2. **Probe.** One `HEAD` per file (a server that answers 405 or 501 is probed with a `GET` whose body is not
   read) gives the server's `ETag`, `Last-Modified` and `Content-Length`. A length that
   differs from the feed's is refused before anything is transferred.
3. **Skip.** A file whose manifest entry still matches the server's `ETag` (or `Last-Modified` when there is no
   `ETag`), the feed's length and the file on disk is not fetched again.
4. **Transfer** into `<name>.zip.part`, resuming with `Range` (and `If-Range`) when a partial file exists. The
   `ETag` and `Last-Modified` seen when the partial file was started are stored beside it
   (`<name>.zip.part.validator`); a partial file is continued only when they still match, otherwise it restarts.
5. **Verify.** The final size must equal the feed's `length` and the server's `Content-Length`; only then is the
   `.part` renamed to `.zip`. A mismatch deletes the `.part` and raises `CuzkDownloadError`.
   A 4xx on the transfer fails at once and removes a `.part` that holds no data; one that holds data is kept
   for the next resume.
6. **Record.** The SHA-256 and the validators go into the manifest, which is rewritten atomically after every
   file.

`out_dir` must be outside the repository checkout: the `.gitignore` does not cover `*.zip`.

### Manifest

`cuzk_manifest.json` is a JSON list of `DownloadRecord`, sorted by `file`:

| Field | Meaning |
|---|---|
| `dataset`, `code`, `edition` | what was downloaded (`edition` is null for terrain) |
| `url` | the file URL |
| `file` | path relative to the output directory, with `/` separators |
| `length` | size in bytes |
| `sha256` | hex digest of the file |
| `etag`, `last_modified` | the server's validators, or null |
| `feed_updated` | the sheet's `updated` stamp in the feed, ISO 8601 |
| `downloaded_at` | ISO 8601 UTC |

## Politeness

- At most `concurrency` simultaneous downloads (default 2, capped at 4).
- `User-Agent: COYPU-Builder/<version> (CUZK ATOM client)`.
- 5xx responses, timeouts and dropped connections (including a body cut off mid-transfer) are retried with exponential backoff (1 s, then 2 s), at most
  3 attempts per request or transfer. 4xx fails immediately.
- Feeds are fetched conditionally and the index is cached, so a repeat run costs one small request per sheet.
- No test touches the network unless selected with `-m network`; CI never selects it.

## Command line

```text
coypu-builder-backend cuzk sheets   (<file.xml> [--buffer 250] [--from S] [--to S] [--alignment NAME]... [--crs C]
                                     | --envelope <file.geojson|file.shp>) --dataset DMR5G [--refresh]
coypu-builder-backend cuzk download (same selection) --dataset DMR5G --out <dir>
                                    [--max-total-mb 500] [--concurrency 2] [--refresh]
```

`sheets` prints one line per sheet (code, edition, file size, name) and the total. `--envelope` takes the file
written by the `envelope` command; a GeoJSON without a `crs` member is WGS 84, a Shapefile needs its `.prj`.
`--dataset` accepts `DMR5G`, `DMR4G` (or `DMR4G-TIFF`), `DMP1G` and `ORTOFOTO`.
