"""Readers, providers and plugin discovery for context-data sources (ADR 0009, ADR 0010 decision 4).

Built-in readers register directly. Third-party readers and providers are Python entry points in the groups
`coypu_builder.readers` and `coypu_builder.providers`; an entry point is imported only when its name is in the
user's allow-list, so a discovered plugin that is not listed never runs any of its code. Plugins are trusted
code, but a plugin that fails to load is recorded and never stops the backend.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from coypu_builder.config import UserConfig, load_user_config
from coypu_builder.domain.crs import ProjectCRS
from coypu_builder.domain.sources import ImportSettings, SourceKind
from coypu_builder.io.landxml.dialects import resolve_coordinates

READER_GROUP = "coypu_builder.readers"
PROVIDER_GROUP = "coypu_builder.providers"


class ReaderFailure(Exception):
    """A reader or provider raised something other than a `ValueError` or `OSError`, or returned something
    malformed: a bug in the plugin, not bad input. The handlers map this to `E_INTERNAL`."""


@dataclass(frozen=True)
class CrsTransform:
    """Raw source coordinates -> project CRS, as resolved from the confirmed import settings.

    Calling it with `(n, 2)` or `(n, 3)` source tokens (the first two columns in the source's file order, the
    third a height) returns the same shape in the project CRS: tokens are scaled by `linear_unit_m`, read as
    (E, N) through the coordinate-order rule of `io/landxml/dialects.py`, then reprojected. Heights are only
    scaled, never transformed (Phase 1).
    """

    source_crs: ProjectCRS
    project_crs: ProjectCRS
    coordinate_order: str
    linear_unit_m: float
    effective_crs: str  # what was used, as a string
    declared_crs: str | None
    description: str  # pyproj's operation description, or "none" for an identical CRS
    accuracy_m: float | None
    warnings: tuple[str, ...] = ()
    native_order: str = "NE"  # what `coordinate_order="auto"` means for this reader: "NE" or "EN"

    @property
    def identical(self) -> bool:
        return self.source_crs == self.project_crs

    def __call__(self, points: np.ndarray) -> np.ndarray:
        pts = np.asarray(points, dtype=np.float64)
        width = pts.shape[-1] if pts.ndim == 2 else 0
        if width not in (2, 3):
            raise ValueError(f"expected (n, 2) or (n, 3) points, got shape {pts.shape}")
        scaled = pts * self.linear_unit_m
        en = resolve_coordinates(scaled[:, :2], self.source_crs, self.coordinate_order, self.native_order)
        if not self.identical:
            e, n = self.project_crs.from_crs(self.source_crs.crs, en[:, 0], en[:, 1])
            en = np.column_stack([e, n])
        return en if width == 2 else np.column_stack([en, scaled[:, 2]])


@dataclass(frozen=True)
class InspectReport:
    format_id: str
    kind: SourceKind
    declared_crs: str | None
    extent: tuple[float, float, float, float] | None  # in the declared CRS, or raw coordinates if none
    counts: Mapping[str, int]  # e.g. {"points": 433396}
    suggested: ImportSettings
    licence_hint: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ImportedObject:
    key: str
    content_hash: str
    payload: object  # reader-specific; T-131 defines terrain payloads


class SourceReader(Protocol):
    format_id: str
    kinds: frozenset[SourceKind]
    suffixes: tuple[str, ...]

    def sniff(self, path: Path) -> bool: ...

    def inspect(self, path: Path) -> InspectReport: ...

    def read(
        self, path: Path, settings: ImportSettings, transform: CrsTransform, clip_polygon: object | None
    ) -> Iterable[ImportedObject]: ...


class SourceProvider(Protocol):
    provider_id: str
    kinds: frozenset[SourceKind]
    licence: str
    attribution: str

    def inspect(self, query: Mapping[str, Any]) -> InspectReport: ...

    def fetch(self, query: Mapping[str, Any], cache_dir: Path) -> list[Path]:
        """Download into `cache_dir`; the files then go through the readers."""
        ...


_READERS: dict[str, SourceReader] = {}
_PROVIDERS: dict[str, SourceProvider] = {}


def register_reader(reader: SourceReader) -> None:
    format_id = getattr(reader, "format_id", None)
    if not isinstance(format_id, str) or not format_id:
        raise ValueError("a reader needs a non-empty string format_id")
    for attribute in ("kinds", "suffixes", "sniff", "inspect", "read"):
        if not hasattr(reader, attribute):
            raise ValueError(f"reader '{format_id}' lacks '{attribute}'")
    suffixes = reader.suffixes
    if isinstance(suffixes, str) or not all(isinstance(x, str) for x in suffixes):
        raise ValueError(f"reader '{format_id}': suffixes must be a tuple of strings")
    if format_id in _READERS:
        raise ValueError(f"a reader with format_id '{format_id}' is already registered")
    _READERS[format_id] = reader


def register_provider(provider: SourceProvider) -> None:
    provider_id = getattr(provider, "provider_id", None)
    if not isinstance(provider_id, str) or not provider_id:
        raise ValueError("a provider needs a non-empty string provider_id")
    for attribute in ("kinds", "licence", "attribution", "inspect", "fetch"):
        if not hasattr(provider, attribute):
            raise ValueError(f"provider '{provider_id}' lacks '{attribute}'")
    if provider_id in _PROVIDERS:
        raise ValueError(f"a provider with provider_id '{provider_id}' is already registered")
    _PROVIDERS[provider_id] = provider


def readers() -> tuple[SourceReader, ...]:
    return tuple(_READERS.values())


def providers() -> tuple[SourceProvider, ...]:
    return tuple(_PROVIDERS.values())


def reader_for(path: Path, format_id: str | None = None) -> SourceReader:
    """The reader for `path`: the one named by `format_id`, else the first registered reader (suffix matches
    first) whose `sniff` accepts the file. Raises `ValueError` if none fits, and `ReaderFailure` if a
    `sniff` raised and no other reader matched."""
    if format_id is not None:
        reader = _READERS.get(format_id)
        if reader is None:
            known = ", ".join(sorted(_READERS)) or "none registered"
            raise ValueError(f"unknown format_id '{format_id}' (known: {known})")
        return reader
    suffix = Path(path).suffix.lower()
    ordered = sorted(_READERS.values(), key=lambda r: suffix not in {s.lower() for s in r.suffixes})
    broken: list[str] = []
    for reader in ordered:
        try:
            if reader.sniff(Path(path)):
                return reader
        except Exception as exc:  # a plugin's bug must not take the connection down
            broken.append(f"reader '{reader.format_id}' sniff failed: {type(exc).__name__}: {exc}")
    if broken:
        raise ReaderFailure("; ".join(broken))
    raise ValueError(f"no registered reader recognises '{Path(path).name}'")


@dataclass(frozen=True)
class PluginInfo:
    name: str  # entry-point name
    group: str  # "coypu_builder.readers" | "coypu_builder.providers"
    distribution: str
    version: str
    state: str  # "loaded" | "available" (not allow-listed) | "failed"
    error: str | None


def _instantiate(loaded: object) -> object:
    """An entry point may name a reader or provider instance, a class, or a zero-argument factory."""
    is_instance = hasattr(loaded, "format_id") or hasattr(loaded, "provider_id")
    if isinstance(loaded, type) or (callable(loaded) and not is_instance):
        return loaded()
    return loaded


def load_plugins(config: UserConfig) -> list[PluginInfo]:
    """Discover reader and provider entry points and import the allow-listed ones.

    A plugin that is not allow-listed is reported `available` and its `EntryPoint.load()` is never called. A
    plugin that raises while loading or registering is reported `failed` with its error; the rest still load.
    """
    allowed = set(config.plugins_allow)
    infos: list[PluginInfo] = []
    for group, register in ((READER_GROUP, register_reader), (PROVIDER_GROUP, register_provider)):
        try:
            discovered = sorted(entry_points(group=group), key=lambda e: e.name)
        except Exception as exc:  # corrupt package metadata
            error = f"cannot enumerate entry points: {type(exc).__name__}: {exc}"
            infos.append(PluginInfo(f"<{group}>", group, "", "", "failed", error))
            continue
        for ep in discovered:
            dist = getattr(ep, "dist", None)
            distribution = str(getattr(dist, "name", None) or "")
            version = str(getattr(dist, "version", None) or "")
            if ep.name not in allowed:
                infos.append(PluginInfo(ep.name, group, distribution, version, "available", None))
                continue
            try:
                register(_instantiate(ep.load()))
            except (Exception, SystemExit) as exc:
                error = f"{type(exc).__name__}: {exc}"
                infos.append(PluginInfo(ep.name, group, distribution, version, "failed", error))
            else:
                infos.append(PluginInfo(ep.name, group, distribution, version, "loaded", None))
    return infos


_PLUGIN_REPORT: tuple[list[PluginInfo], tuple[str, ...]] | None = None


def plugin_report() -> tuple[list[PluginInfo], tuple[str, ...]]:
    """(plugin infos, config warnings) for this process. The user config is read and the allow-listed plugins
    are loaded on the first call only."""
    global _PLUGIN_REPORT
    if _PLUGIN_REPORT is None:
        try:
            config, warnings = load_user_config()
            _PLUGIN_REPORT = (load_plugins(config), warnings)
        except Exception as exc:  # never let plugin loading stop the backend
            failed = PluginInfo("<plugins>", "", "", "", "failed", f"{type(exc).__name__}: {exc}")
            _PLUGIN_REPORT = ([failed], ())
    infos, warnings = _PLUGIN_REPORT
    return list(infos), warnings
