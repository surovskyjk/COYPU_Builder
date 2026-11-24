"""Product recognisers for file imports (F53, T-136 C1): a file name that a known national product uses
suggests the product's licence and attribution.

A hint is a guess. The dialog shows it prefilled and editable, and the backend records nothing from it unless
`import.run` sends the values back. Pure: it reads the file names, never the files.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from coypu_builder.domain.sources import SourceKind
from coypu_builder.io.sources.terms import CUZK_ATTRIBUTION, CUZK_LICENCE

# A ČÚZK SM5 map-sheet code: four upper-case ASCII letters and two digits at the start of the name, followed
# by `.`, `_` or the end of the name, as in `KRAV82.zip`, `KRAV82.laz` or `KRAV82.WRTO24.2025.zip`. Real ČÚZK
# codes are upper case; `krav82.tif`, `KRAV82-x.laz`, `data01.tif` and `site02.laz` are not sheet codes.
SM5_SHEET_CODE = re.compile(r"[A-Z]{4}[0-9]{2}(?=[._]|\Z)")
_KINDS = frozenset({SourceKind.TERRAIN, SourceKind.IMAGERY})


@dataclass(frozen=True)
class ProductHints:
    licence: str
    attribution: str


def product_hints(paths: Sequence[Path | str], kind: SourceKind) -> ProductHints | None:
    """The ČÚZK hints when every file name starts with a map-sheet code and the kind is terrain or imagery;
    `None` otherwise, including for an empty list or any other kind."""
    if kind not in _KINDS or not paths:
        return None
    if not all(SM5_SHEET_CODE.match(Path(path).name) for path in paths):
        return None
    return ProductHints(CUZK_LICENCE, CUZK_ATTRIBUTION)
