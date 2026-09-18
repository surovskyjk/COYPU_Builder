"""Entity identity. ADR 0005 maps these to IFC GlobalId via ifcopenshell.guid.compress at export time —
that mapping lives in io/, never here, since domain/ stays free of optional heavy dependencies.
"""

from __future__ import annotations

import uuid

EntityId = str


def new_id() -> EntityId:
    return uuid.uuid4().hex
