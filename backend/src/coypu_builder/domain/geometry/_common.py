from __future__ import annotations

import numpy as np

CURVATURE_EPS = 1e-12
"""Below this |curvature| (1/m) an element is treated as straight (R > 10^12 m)."""


def s_array(s) -> np.ndarray:
    return np.atleast_1d(np.asarray(s, dtype=np.float64))


def wrap_angle(a: np.ndarray | float) -> np.ndarray | float:
    """Wrap to (-pi, pi]."""
    return (np.asarray(a) + np.pi) % (2.0 * np.pi) - np.pi
