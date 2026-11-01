"""VTK (BSD) implementation of RenderBackend.

MVP renders one actor per element. For 100k+ elements, batch meshes per
(category, material) into shared actors and use instancing for repeated types;
the RenderBackend interface already hides that from callers.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import vtkmodules.vtkInteractionStyle  # noqa: F401 - registers interactor styles
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401 - registers the OpenGL backend
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPlane, vtkPlaneCollection, vtkPolyData
from vtkmodules.vtkRenderingCore import vtkActor, vtkCellPicker, vtkPolyDataMapper, vtkPropPicker, vtkRenderer

from bimforge.geometry.mesh import Mesh
from bimforge.plugins.sdk import PluginInfo, RenderBackend, RenderStyle

HIGHLIGHT_COLOR = (1.0, 0.78, 0.1)


def _cells(indices: np.ndarray, per_cell: int) -> vtkCellArray:
    offsets = np.arange(0, indices.size + 1, per_cell, dtype=np.int64)
    cells = vtkCellArray()
    cells.SetData(
        numpy_to_vtkIdTypeArray(offsets, deep=True),
        numpy_to_vtkIdTypeArray(indices.ravel().astype(np.int64), deep=True),
    )
    return cells


def _points(xyz: np.ndarray) -> vtkPoints:
    points = vtkPoints()
    points.SetData(numpy_to_vtk(np.ascontiguousarray(xyz, dtype=np.float64), deep=True))
    return points


class VtkRenderBackend(RenderBackend):
    info = PluginInfo(id="bimforge.render.vtk", name="VTK", version="0.1.0", license="BSD-3-Clause")

    def __init__(self, renderer: vtkRenderer) -> None:
        self.renderer = renderer
        self._actors: dict[str, tuple[vtkActor, RenderStyle]] = {}
        self._keys_by_actor: dict[int, str] = {}
        self._hidden_categories: set[str] = set()
        self._highlighted: set[str] = set()
        self._hidden_keys: set[str] = set()
        self._picker = vtkPropPicker()
        self._surface_picker = vtkCellPicker()
        self._surface_picker.SetTolerance(0.0005)
        self._clip: vtkPlaneCollection | None = None

    def upsert_mesh(self, key: str, mesh: Mesh, style: RenderStyle) -> None:
        poly = vtkPolyData()
        poly.SetPoints(_points(mesh.vertices))
        poly.SetPolys(_cells(mesh.faces, 3))
        self._set_actor(key, poly, style, surface=True)

    def upsert_lines(self, key: str, segments: np.ndarray, style: RenderStyle) -> None:
        poly = vtkPolyData()
        poly.SetPoints(_points(segments.reshape(-1, 3)))
        poly.SetLines(_cells(np.arange(segments.shape[0] * 2), 2))
        self._set_actor(key, poly, style, surface=False)

    def _set_actor(self, key: str, poly: vtkPolyData, style: RenderStyle, surface: bool) -> None:
        self.remove(key)
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)
        if self._clip is not None:
            mapper.SetClippingPlanes(self._clip)
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetOpacity(style.opacity)
        if surface:
            prop.SetInterpolationToFlat()
            prop.SetEdgeVisibility(style.show_edges)
            prop.SetEdgeColor(0.25, 0.25, 0.25)
        else:
            actor.PickableOff()
        self._actors[key] = (actor, style)
        self._keys_by_actor[id(actor)] = key
        self._apply_appearance(key)
        self.renderer.AddActor(actor)

    def _apply_appearance(self, key: str) -> None:
        actor, style = self._actors[key]
        actor.GetProperty().SetColor(HIGHLIGHT_COLOR if key in self._highlighted else style.color)
        actor.SetVisibility(style.category not in self._hidden_categories and key not in self._hidden_keys)

    def remove(self, key: str) -> None:
        entry = self._actors.pop(key, None)
        if entry:
            self.renderer.RemoveActor(entry[0])
            self._keys_by_actor.pop(id(entry[0]), None)

    def clear(self, prefix: str = "") -> None:
        for key in [k for k in self._actors if k.startswith(prefix)]:
            self.remove(key)

    def set_category_visible(self, category: str, visible: bool) -> None:
        (self._hidden_categories.discard if visible else self._hidden_categories.add)(category)
        for key, (_, style) in self._actors.items():
            if style.category == category:
                self._apply_appearance(key)

    def set_highlight(self, keys: Iterable[str]) -> None:
        previous, self._highlighted = self._highlighted, set(keys)
        for key in previous | self._highlighted:
            if key in self._actors:
                self._apply_appearance(key)

    def set_hidden(self, keys: Iterable[str]) -> None:
        previous, self._hidden_keys = self._hidden_keys, set(keys)
        for key in previous ^ self._hidden_keys:
            if key in self._actors:
                self._apply_appearance(key)

    def pick(self, x: int, y: int) -> str | None:
        """x, y in VTK display coordinates (origin bottom-left)."""
        if not self._picker.Pick(x, y, 0, self.renderer):
            return None
        actor = self._picker.GetActor()
        return self._keys_by_actor.get(id(actor)) if actor is not None else None

    def pick_on_plane(self, x: int, y: int, z: float) -> tuple[float, float, float] | None:
        r = self.renderer
        ends = []
        for depth in (0.0, 1.0):
            r.SetDisplayPoint(x, y, depth)
            r.DisplayToWorld()
            wx, wy, wz, w = r.GetWorldPoint()
            ends.append(np.array([wx, wy, wz]) / (w or 1.0))
        direction = ends[1] - ends[0]
        if abs(direction[2]) < 1e-12:
            return None  # looking exactly along the plane
        t = (z - ends[0][2]) / direction[2]
        if t < 0:
            return None
        hit = ends[0] + t * direction
        return (float(hit[0]), float(hit[1]), float(z))

    def pick_surface(self, x: int, y: int) -> tuple[float, float, float] | None:
        if not self._surface_picker.Pick(x, y, 0, self.renderer) or self._surface_picker.GetActor() is None:
            return None
        return tuple(float(c) for c in self._surface_picker.GetPickPosition())  # type: ignore[return-value]

    def set_section_box(self, bounds) -> None:
        if bounds is None:
            self._clip = None
        else:
            xmin, xmax, ymin, ymax, zmin, zmax = bounds
            planes = vtkPlaneCollection()
            for origin, normal in (((xmin, 0, 0), (1, 0, 0)), ((xmax, 0, 0), (-1, 0, 0)),
                                   ((0, ymin, 0), (0, 1, 0)), ((0, ymax, 0), (0, -1, 0)),
                                   ((0, 0, zmin), (0, 0, 1)), ((0, 0, zmax), (0, 0, -1))):
                plane = vtkPlane()
                plane.SetOrigin(*origin)
                plane.SetNormal(*normal)
                planes.AddItem(plane)
            self._clip = planes
        for actor, _ in self._actors.values():
            mapper = actor.GetMapper()
            if self._clip is None:
                mapper.RemoveAllClippingPlanes()
            else:
                mapper.SetClippingPlanes(self._clip)

    def bounds(self, keys: Iterable[str]):
        boxes = [self._actors[k][0].GetBounds() for k in keys if k in self._actors]
        if not boxes:
            return None
        b = np.array(boxes)
        return (b[:, 0].min(), b[:, 1].max(), b[:, 2].min(), b[:, 3].max(), b[:, 4].min(), b[:, 5].max())

    def zoom_to(self, bounds) -> None:
        self.renderer.ResetCamera(*bounds)

    def fit_view(self) -> None:
        self.renderer.ResetCamera()

    def set_view(self, preset: str) -> None:
        directions = {"iso": (1, -1, 0.8), "top": (0, 0, 1), "front": (0, -1, 0)}
        camera = self.renderer.GetActiveCamera()
        camera.SetFocalPoint(0, 0, 0)
        camera.SetPosition(*directions[preset])
        camera.SetViewUp(*((0, 1, 0) if preset == "top" else (0, 0, 1)))
        self.renderer.ResetCamera()

    def render(self) -> None:
        window = self.renderer.GetRenderWindow()
        if window is not None:
            window.Render()
