"""The ČÚZK licence and attribution, in one place (ADR 0009, decision 9).

A leaf module: it imports nothing from `io/`, so the ČÚZK providers, the file-name hints and the CLI can all
take the terms from here without an import cycle.
"""

from __future__ import annotations

CUZK_LICENCE = "CC BY 4.0"
CUZK_ATTRIBUTION = "© ČÚZK"
