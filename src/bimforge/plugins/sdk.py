"""Plugin SDK: the interfaces every extension implements.

A plugin is a class exposing `info: PluginInfo` and implementing one of the
abstract base classes below. It is registered through a Python entry point in
one of the groups listed in bimforge.plugins.registry.ENTRY_POINT_GROUPS.

Contract rules:
* Importers and exporters never touch the UI and never mutate a live Project
  passed to them by the application (exporters only read it).
* AI models return proposals with confidences; they never apply changes.
* Render backends receive meshes and style keys, never domain objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from bimforge.core.model import Point2, Project
from bimforge.drawing.primitives import Drawing2D
from bimforge.geometry.mesh import Mesh


@dataclass(frozen=True)
class PluginInfo:
    id: str  # reverse-DNS style, globally unique
    name: str
    version: str
    api_version: int = 1  # PLUGIN_API_VERSION the plugin was built against
    vendor: str = ""
    license: str = ""


class Plugin(ABC):
    info: ClassVar[PluginInfo]

    @classmethod
    def available(cls) -> bool:
        """False when an optional native dependency (SDK, model file) is missing."""
        return True


# --- Formats -------------------------------------------------------------------


@dataclass(kw_only=True)
class ImportResult:
    """Importers return 2D drawings (to be interpreted) and/or semantic models (IFC, RVT)."""

    drawing: Drawing2D | None = None
    project: Project | None = None
    warnings: list[str] = field(default_factory=list)


class Importer(Plugin):
    extensions: ClassVar[tuple[str, ...]]

    def can_import(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    @abstractmethod
    def import_file(self, path: Path, options: Mapping[str, Any] | None = None) -> ImportResult: ...


class Exporter(Plugin):
    extensions: ClassVar[tuple[str, ...]]

    def can_export(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    @abstractmethod
    def export(self, project: Project, path: Path, options: Mapping[str, Any] | None = None) -> list[str]:
        """Write the project; return warnings about data that could not be represented."""


# --- Geometry ------------------------------------------------------------------


class GeometryKernel(Plugin):
    capabilities: ClassVar[frozenset[str]]  # e.g. {"extrude", "boolean", "sweep", "step"}

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def extrude(self, profile: Sequence[Point2], z0: float, height: float) -> Mesh:
        """Extrude a simple polygon vertically from z0 by height."""

    def boolean_difference(self, a: Mesh, b: Mesh) -> Mesh:
        raise NotImplementedError(f"{self.info.name} does not support booleans")

    def sweep(self, profile: Sequence[Point2], path: Sequence[tuple[float, float, float]]) -> Mesh:
        raise NotImplementedError(f"{self.info.name} does not support sweeps")


# --- Rendering -----------------------------------------------------------------


@dataclass(frozen=True)
class RenderStyle:
    color: tuple[float, float, float] = (0.8, 0.8, 0.8)
    opacity: float = 1.0
    category: str = "architecture"  # used for discipline visibility filters
    show_edges: bool = False


class RenderBackend(Plugin):
    @abstractmethod
    def upsert_mesh(self, key: str, mesh: Mesh, style: RenderStyle) -> None: ...

    @abstractmethod
    def upsert_lines(self, key: str, segments: np.ndarray, style: RenderStyle) -> None:
        """segments: (N, 2, 3) array of line endpoints."""

    @abstractmethod
    def remove(self, key: str) -> None: ...

    @abstractmethod
    def clear(self, prefix: str = "") -> None: ...

    @abstractmethod
    def set_category_visible(self, category: str, visible: bool) -> None: ...

    @abstractmethod
    def set_highlight(self, keys: Iterable[str]) -> None: ...

    @abstractmethod
    def set_hidden(self, keys: Iterable[str]) -> None:
        """Hide exactly these keys (e.g. elements on other levels); replaces the previous set."""

    @abstractmethod
    def pick(self, x: int, y: int) -> str | None:
        """Key under the given viewport pixel, if any."""

    @abstractmethod
    def pick_on_plane(self, x: int, y: int, z: float) -> tuple[float, float, float] | None:
        """World point where the view ray through pixel (x, y) meets the horizontal plane at height z."""

    @abstractmethod
    def pick_surface(self, x: int, y: int) -> tuple[float, float, float] | None:
        """World point on the visible surface under pixel (x, y)."""

    @abstractmethod
    def set_section_box(self, bounds: tuple[float, float, float, float, float, float] | None) -> None:
        """Clip everything outside (xmin, xmax, ymin, ymax, zmin, zmax); None removes the box."""

    @abstractmethod
    def bounds(self, keys: Iterable[str]) -> tuple[float, float, float, float, float, float] | None: ...

    @abstractmethod
    def zoom_to(self, bounds: tuple[float, float, float, float, float, float]) -> None: ...

    @abstractmethod
    def fit_view(self) -> None: ...

    @abstractmethod
    def set_view(self, preset: Literal["iso", "top", "front"]) -> None: ...

    @abstractmethod
    def render(self) -> None: ...


# --- AI ------------------------------------------------------------------------

AITask = Literal["symbol_detection", "wall_segmentation", "ocr", "layer_classification", "room_segmentation"]


@dataclass(kw_only=True)
class InferenceInput:
    drawing: Drawing2D | None = None
    image: np.ndarray | None = None  # (H, W, C) uint8 raster
    pixel_size: float | None = None  # drawing units per pixel, to map detections back
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(kw_only=True)
class Detection:
    label: str  # e.g. "door", "wc", "socket", or a layer role
    confidence: float
    polygon: list[Point2] = field(default_factory=list)  # drawing units
    attributes: dict[str, Any] = field(default_factory=dict)  # OCR text, rotation...
    source_handles: list[str] = field(default_factory=list)


class AIModel(Plugin):
    task: ClassVar[AITask]
    labels: ClassVar[tuple[str, ...]]
    runtime: ClassVar[Literal["onnx", "torch", "cloud", "rules"]]

    @abstractmethod
    def predict(self, data: InferenceInput) -> list[Detection]: ...


@dataclass(kw_only=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None


@dataclass(kw_only=True)
class ToolCall:
    id: str
    name: str  # a registered command name
    arguments: dict[str, Any]


@dataclass(kw_only=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(Plugin):
    is_local: ClassVar[bool]  # cloud providers require explicit user consent per project

    @abstractmethod
    def complete(self, messages: Sequence[ChatMessage], tools: Sequence[dict[str, Any]]) -> LLMResponse:
        """tools are JSON schemas generated from the command registry."""
