"""Discovers and holds plugin instances."""

from __future__ import annotations

import logging
from importlib.metadata import entry_points
from pathlib import Path
from typing import TypeVar

from bimforge import PLUGIN_API_VERSION
from bimforge.plugins.sdk import AIModel, Exporter, GeometryKernel, Importer, LLMProvider, Plugin, RenderBackend

log = logging.getLogger(__name__)

ENTRY_POINT_GROUPS: dict[str, type[Plugin]] = {
    "bimforge.importers": Importer,
    "bimforge.exporters": Exporter,
    "bimforge.kernels": GeometryKernel,
    "bimforge.render_backends": RenderBackend,
    "bimforge.ai_models": AIModel,
    "bimforge.llm_providers": LLMProvider,
}

P = TypeVar("P", bound=Plugin)


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[type[Plugin], dict[str, Plugin]] = {base: {} for base in ENTRY_POINT_GROUPS.values()}
        self.errors: list[str] = []

    def register(self, plugin: Plugin) -> None:
        base = next((b for b in self._plugins if isinstance(plugin, b)), None)
        if base is None:
            raise TypeError(f"{type(plugin).__name__} does not implement a known plugin interface")
        if plugin.info.api_version > PLUGIN_API_VERSION:
            raise ValueError(f"{plugin.info.id} needs plugin API {plugin.info.api_version}")
        self._plugins[base][plugin.info.id] = plugin

    def all(self, base: type[P]) -> list[P]:
        return list(self._plugins[base].values())  # type: ignore[arg-type]

    def importer_for(self, path: Path) -> Importer:
        for importer in self.all(Importer):
            if importer.can_import(path):
                return importer
        raise LookupError(f"No importer for '{path.suffix}' files")

    def exporter_for(self, path: Path) -> Exporter:
        for exporter in self.all(Exporter):
            if exporter.can_export(path):
                return exporter
        raise LookupError(f"No exporter for '{path.suffix}' files")

    def kernel(self) -> GeometryKernel:
        kernels = self.all(GeometryKernel)
        if not kernels:
            raise LookupError("No geometry kernel registered")
        # Prefer the most capable kernel (e.g. OCCT over the built-in prism kernel).
        return max(kernels, key=lambda k: len(k.capabilities))

    def discover(self) -> None:
        """Load entry-point plugins. A broken plugin is logged and skipped, never fatal."""
        for group, base in ENTRY_POINT_GROUPS.items():
            for ep in entry_points(group=group):
                try:
                    cls = ep.load()
                    if not (isinstance(cls, type) and issubclass(cls, base)):
                        raise TypeError(f"{ep.value} is not a {base.__name__}")
                    if cls.available() and cls.info.id not in self._plugins[base]:
                        self.register(cls())
                except Exception as exc:  # noqa: BLE001 - isolate third-party failures
                    message = f"Plugin '{ep.name}' ({group}) failed to load: {exc}"
                    log.warning(message)
                    self.errors.append(message)

    @classmethod
    def with_builtins(cls, discover: bool = True) -> PluginRegistry:
        from bimforge.adapters.dwg_importer import DwgImporter
        from bimforge.adapters.dxf_importer import DxfImporter
        from bimforge.adapters.ifc_exporter import IfcExporter
        from bimforge.adapters.ifc_importer import IfcImporter
        from bimforge.adapters.pdf_importer import PdfImporter
        from bimforge.geometry.kernel import NumpyKernel

        registry = cls()
        for plugin_cls in (DxfImporter, DwgImporter, PdfImporter, IfcImporter, IfcExporter, NumpyKernel):
            if plugin_cls.available():
                registry.register(plugin_cls())
        if discover:
            registry.discover()
        return registry
