"""Coordinate reference systems, the Project Base Point and the domain → Godot axis mapping.

Domain coordinates are float64 (E, N, H) in the project CRS. Everything that leaves for the client is
first made relative to the Project Base Point, then mapped to Godot's axes:

    godot.x = E - E0        (east)
    godot.y = H - H0        (up)
    godot.z = -(N - N0)     (Godot -Z is north)

which keeps the world right-handed. A frame (tangent, left, up) becomes a Godot Basis whose columns are
(right, up, back) so that -Z of the basis points along the tangent.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer

# S-JTSK / Křovák family. 2065 and 5513 use south/west-positive axes (the "engineering" Křovák X, Y);
# 5514 and 8353 are the east/north (negative-valued) variants. LandXML files in Czech practice carry the
# south/west-positive numbers regardless of the declared code — see io/landxml/dialects.py.
KROVAK_EPSG: frozenset[int] = frozenset({2065, 5221, 5513, 5514, 8352, 8353})


@dataclass(frozen=True, slots=True)
class BasePoint:
    """Project Base Point (E0, N0, H0) in the project CRS. Stored in the document, never implicit."""

    easting: float
    northing: float
    height: float = 0.0

    def as_array(self) -> np.ndarray:
        return np.array([self.easting, self.northing, self.height], dtype=np.float64)

    @classmethod
    def rounded(cls, easting: float, northing: float, height: float = 0.0, step: float = 100.0) -> BasePoint:
        return cls(round(easting / step) * step, round(northing / step) * step, round(height / step) * step)


@lru_cache(maxsize=64)
def _transformer(source: str, target: str) -> Transformer:
    return Transformer.from_crs(CRS.from_user_input(source), CRS.from_user_input(target), always_xy=True)


class ProjectCRS:
    """Any pyproj-resolvable CRS (EPSG code, 'EPSG:5514', WKT, PROJ string)."""

    __slots__ = ("crs",)

    def __init__(self, definition: str | int | CRS):
        self.crs = definition if isinstance(definition, CRS) else CRS.from_user_input(definition)

    @property
    def epsg(self) -> int | None:
        return self.crs.to_epsg()

    @property
    def name(self) -> str:
        return self.crs.name

    @property
    def is_projected(self) -> bool:
        return bool(self.crs.is_projected)

    @property
    def is_metric(self) -> bool:
        return all(axis.unit_name in ("metre", "meter", "m") for axis in self.crs.axis_info)

    @property
    def is_krovak(self) -> bool:
        epsg = self.epsg
        return epsg in KROVAK_EPSG if epsg is not None else "krovak" in self.crs.to_proj4().lower()

    def _key(self) -> str:
        epsg = self.epsg
        return f"EPSG:{epsg}" if epsg is not None else self.crs.to_wkt()

    def from_crs(self, source: str | int | CRS, x, y) -> tuple[np.ndarray, np.ndarray]:
        """Transform (x, y) given in `source` (always x=east/lon first) into this CRS."""
        tr = _transformer(ProjectCRS(source)._key(), self._key())
        e, n = tr.transform(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
        return np.asarray(e), np.asarray(n)

    def to_crs(self, target: str | int | CRS, e, n) -> tuple[np.ndarray, np.ndarray]:
        tr = _transformer(self._key(), ProjectCRS(target)._key())
        x, y = tr.transform(np.asarray(e, dtype=np.float64), np.asarray(n, dtype=np.float64))
        return np.asarray(x), np.asarray(y)

    def to_wgs84(self, e, n) -> tuple[np.ndarray, np.ndarray]:
        """Returns (lon, lat)."""
        return self.to_crs("EPSG:4326", e, n)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ProjectCRS) and self.crs == other.crs

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        epsg = self.epsg
        return f"ProjectCRS({'EPSG:' + str(epsg) if epsg else self.name!r})"


# --- domain (E, N, H) ⇄ local ⇄ Godot -------------------------------------------------------------------


def to_local(points_enh: np.ndarray, base: BasePoint) -> np.ndarray:
    """(n, 3) float64 project coordinates → float64 coordinates relative to the base point."""
    return np.asarray(points_enh, dtype=np.float64) - base.as_array()


def from_local(points_local: np.ndarray, base: BasePoint) -> np.ndarray:
    return np.asarray(points_local, dtype=np.float64) + base.as_array()


def vector_to_godot(v_enh: np.ndarray, dtype=np.float32) -> np.ndarray:
    """Axis mapping for directions or already-local positions: (E, N, H) → (E, H, -N)."""
    v = np.asarray(v_enh, dtype=np.float64)
    out = np.empty_like(v)
    out[..., 0] = v[..., 0]
    out[..., 1] = v[..., 2]
    out[..., 2] = -v[..., 1]
    return out.astype(dtype)


def vector_from_godot(v_godot: np.ndarray) -> np.ndarray:
    v = np.asarray(v_godot, dtype=np.float64)
    out = np.empty_like(v)
    out[..., 0] = v[..., 0]
    out[..., 1] = -v[..., 2]
    out[..., 2] = v[..., 1]
    return out


def points_to_godot(points_enh: np.ndarray, base: BasePoint, dtype=np.float32) -> np.ndarray:
    """Project coordinates → base-point-relative Godot positions (float32 by default, ready for the wire)."""
    return vector_to_godot(to_local(points_enh, base), dtype=dtype)


def basis_to_godot(tangent: np.ndarray, left: np.ndarray, up: np.ndarray) -> np.ndarray:
    """Frames (n, 3) in domain axes → (n, 3, 3) Godot rotation matrices with columns (right, up, back).

    right = -left, back = -tangent, so the basis' forward (-Z) is the tangent.
    """
    right = vector_to_godot(-np.asarray(left), dtype=np.float64)
    up_g = vector_to_godot(np.asarray(up), dtype=np.float64)
    back = vector_to_godot(-np.asarray(tangent), dtype=np.float64)
    return np.stack([right, up_g, back], axis=-1)


def quaternion_from_matrix(m: np.ndarray) -> np.ndarray:
    """(n, 3, 3) rotation matrices → (n, 4) unit quaternions in Godot component order (x, y, z, w)."""
    m = np.asarray(m, dtype=np.float64)
    single = m.ndim == 2
    if single:
        m = m[None]
    n = m.shape[0]
    q = np.empty((n, 4), dtype=np.float64)
    m00, m11, m22 = m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]
    trace = m00 + m11 + m22

    case0 = trace > 0
    case1 = (~case0) & (m00 >= m11) & (m00 >= m22)
    case2 = (~case0) & (~case1) & (m11 >= m22)
    case3 = ~(case0 | case1 | case2)

    s = np.sqrt(np.where(case0, trace + 1.0, 1.0)) * 2.0
    q[case0, 3] = 0.25 * s[case0]
    q[case0, 0] = (m[case0, 2, 1] - m[case0, 1, 2]) / s[case0]
    q[case0, 1] = (m[case0, 0, 2] - m[case0, 2, 0]) / s[case0]
    q[case0, 2] = (m[case0, 1, 0] - m[case0, 0, 1]) / s[case0]

    s = np.sqrt(np.where(case1, 1.0 + m00 - m11 - m22, 1.0)) * 2.0
    q[case1, 3] = (m[case1, 2, 1] - m[case1, 1, 2]) / s[case1]
    q[case1, 0] = 0.25 * s[case1]
    q[case1, 1] = (m[case1, 0, 1] + m[case1, 1, 0]) / s[case1]
    q[case1, 2] = (m[case1, 0, 2] + m[case1, 2, 0]) / s[case1]

    s = np.sqrt(np.where(case2, 1.0 + m11 - m00 - m22, 1.0)) * 2.0
    q[case2, 3] = (m[case2, 0, 2] - m[case2, 2, 0]) / s[case2]
    q[case2, 0] = (m[case2, 0, 1] + m[case2, 1, 0]) / s[case2]
    q[case2, 1] = 0.25 * s[case2]
    q[case2, 2] = (m[case2, 1, 2] + m[case2, 2, 1]) / s[case2]

    s = np.sqrt(np.where(case3, 1.0 + m22 - m00 - m11, 1.0)) * 2.0
    q[case3, 3] = (m[case3, 1, 0] - m[case3, 0, 1]) / s[case3]
    q[case3, 0] = (m[case3, 0, 2] + m[case3, 2, 0]) / s[case3]
    q[case3, 1] = (m[case3, 1, 2] + m[case3, 2, 1]) / s[case3]
    q[case3, 2] = 0.25 * s[case3]

    q /= np.linalg.norm(q, axis=1, keepdims=True)
    return q[0] if single else q
