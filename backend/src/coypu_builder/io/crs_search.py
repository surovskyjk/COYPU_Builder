"""The EPSG coordinate reference systems that `crs.search` searches (T-136 Part A).

The list is the EPSG authority of the PROJ database, read once per process through `pyproj.database` and kept
in memory, with one row per code (PROJ gives one row per area of use; the row with the widest area is kept).

A query is split into words. Punctuation-only words and `epsg` are dropped, and an `epsg:` prefix is stripped.
A CRS matches when every remaining word matches either its code (as a prefix: `551` finds 5514, not 4551) or
its name or an alias (as a substring, ignoring case and diacritics, with spaces and hyphens also ignored:
`sjtsk` finds `S-JTSK`, `wgs84` finds `WGS 84`). A few Czech-practice names that are not in the official name
are added as aliases (`Bpv` for EPSG:5705).

Results are ordered by the key in `search`: deprecated CRSs last; for a query of at most two characters (or
none), the Czech-practice codes first; then the match group (exact code, code prefix, exact name, name prefix,
the rest); then the Czech-practice codes again, within the group; then alphabetically by name and code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from pyproj.database import query_crs_info
from pyproj.enums import PJType

CRS_KINDS: Final[tuple[str, ...]] = ("projected", "geographic", "vertical", "compound")
# What `kind` may be: one of the kinds of a result, or "horizontal" (projected plus 2D geographic).
KIND_FILTERS: Final[tuple[str, ...]] = (*CRS_KINDS, "horizontal")
CZECH_PRACTICE_CODES: Final[tuple[int, ...]] = (
    5514,
    5513,
    4326,
    4258,
    25833,
    25834,
    32633,
    32634,
    5705,
    3855,
)
SHORT_QUERY_LENGTH: Final = 2  # a query this short (or empty) puts the Czech-practice codes first
DEFAULT_LIMIT: Final = 20
MIN_LIMIT: Final = 1
MAX_LIMIT: Final = 200
ALIASES: Final[dict[int, tuple[str, ...]]] = {5705: ("bpv",)}

_KIND_OF_TYPE: Final[dict[PJType, str]] = {
    PJType.PROJECTED_CRS: "projected",
    PJType.GEOGRAPHIC_2D_CRS: "geographic",
    PJType.GEOGRAPHIC_3D_CRS: "geographic",
    PJType.VERTICAL_CRS: "vertical",
    PJType.COMPOUND_CRS: "compound",
}


def fold(text: str) -> str:
    """Case- and diacritics-insensitive form of `text` (NFKD, combining marks removed, casefolded)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _squash(text: str) -> str:
    """The text with spaces and hyphens removed, so that `sjtsk` and `S-JTSK` compare equal."""
    return text.replace(" ", "").replace("-", "")


@dataclass(frozen=True, slots=True)
class CrsEntry:
    number: int
    name: str
    kind: str  # one of CRS_KINDS; a 3D geographic CRS is "geographic" with three_d set
    three_d: bool
    area: str | None
    deprecated: bool
    folded_name: str  # the name folded for matching
    search_text: str  # the folded name and the aliases, for matching words
    squashed_name: str  # folded_name with spaces and hyphens removed
    squashed_text: str  # search_text with spaces and hyphens removed

    @property
    def code(self) -> str:
        return f"EPSG:{self.number}"


def _width_deg(area) -> float:
    """The longitude span of an area of use, in degrees; an area across the antimeridian has west > east."""
    if area is None:
        return 0.0
    span = float(area.east) - float(area.west)
    if span < 0.0:
        span += 360.0
    return span


def _box_area(area) -> float:
    """The bounding box of an area of use, in square degrees: the ranking key of duplicate rows."""
    if area is None:
        return 0.0
    return _width_deg(area) * (float(area.north) - float(area.south))


@lru_cache(maxsize=1)
def catalogue() -> tuple[CrsEntry, ...]:
    """Every EPSG CRS of one of `CRS_KINDS`, one entry per code, deprecated ones included (flagged). PROJ
    gives one row per area of use; the row whose bounding box is widest is kept. Read once, then cached."""
    rows_by_code: dict[int, tuple[float, object]] = {}
    for row in query_crs_info(auth_name="EPSG", allow_deprecated=True):
        if _KIND_OF_TYPE.get(row.type) is None:
            continue
        number = int(row.code)
        score = _box_area(row.area_of_use)
        kept = rows_by_code.get(number)
        if kept is None or score > kept[0]:
            rows_by_code[number] = (score, row)
    entries: list[CrsEntry] = []
    for number in sorted(rows_by_code):
        row = rows_by_code[number][1]
        folded = fold(row.name)
        search_text = " ".join((folded, *ALIASES.get(number, ())))
        entries.append(
            CrsEntry(
                number=number,
                name=row.name,
                kind=_KIND_OF_TYPE[row.type],
                three_d=row.type is PJType.GEOGRAPHIC_3D_CRS,
                area=row.area_of_use.name if row.area_of_use is not None else None,
                deprecated=bool(row.deprecated),
                folded_name=folded,
                search_text=search_text,
                squashed_name=_squash(folded),
                squashed_text=_squash(search_text),
            )
        )
    return tuple(entries)


def _czech_rank(number: int) -> int:
    try:
        return CZECH_PRACTICE_CODES.index(number)
    except ValueError:
        return len(CZECH_PRACTICE_CODES)


def _words(folded_query: str) -> list[str]:
    """The query's words: punctuation-only words and `epsg` dropped, an `epsg:` prefix stripped."""
    words: list[str] = []
    for raw in folded_query.split():
        word = raw.removeprefix("epsg:")
        if word == "epsg" or not any(ch.isalnum() for ch in word):
            continue
        words.append(word)
    return words


def _word_matches(word: str, entry: CrsEntry) -> bool:
    return (
        str(entry.number).startswith(word)
        or word in entry.search_text
        or _squash(word) in entry.squashed_text
    )


def _group(entry: CrsEntry, joined: str) -> int:
    """0 exact code, 1 code prefix, 2 exact name, 3 name prefix, 4 the rest. An empty query is the rest."""
    if not joined:
        return 4
    if joined == str(entry.number):
        return 0
    if str(entry.number).startswith(joined):
        return 1
    if joined == entry.folded_name:
        return 2
    if entry.squashed_name.startswith(_squash(joined)):
        return 3
    return 4


def search(query: str, *, limit: int = DEFAULT_LIMIT, kind: str | None = None) -> list[CrsEntry]:
    """The CRSs matching `query`, best first, at most `limit` of them (clamped to 1..200).

    `kind` is one of `KIND_FILTERS` (`horizontal` is projected plus 2D geographic); `""` and `None` mean no
    filter. Any other kind raises `ValueError`.
    """
    if kind == "":
        kind = None
    if kind is not None and kind not in KIND_FILTERS:
        known = ", ".join(KIND_FILTERS)
        raise ValueError(f"unknown kind '{kind}' (one of: {known})")
    count = min(max(int(limit), MIN_LIMIT), MAX_LIMIT)
    needle = fold(query).strip()
    words = _words(needle)
    joined = " ".join(words)
    short = len(needle) <= SHORT_QUERY_LENGTH

    ranked: list[tuple[tuple, CrsEntry]] = []
    for entry in catalogue():
        if kind == "horizontal":
            if entry.kind == "vertical" or entry.kind == "compound" or entry.three_d:
                continue
        elif kind is not None and entry.kind != kind:
            continue
        if not all(_word_matches(word, entry) for word in words):
            continue
        czech = _czech_rank(entry.number)
        key = (
            entry.deprecated,
            czech if short else 0,
            _group(entry, joined),
            czech,
            entry.folded_name,
            entry.number,
        )
        ranked.append((key, entry))
    ranked.sort(key=lambda item: item[0])
    return [entry for _key, entry in ranked[:count]]
