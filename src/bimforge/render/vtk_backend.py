"""VTK (BSD) implementation of RenderBackend.

Meshes that look alike are drawn as one object, so the frame rate depends on how many triangles
there are and hardly at all on how many elements. Because a click then can't be answered by asking
which object was hit, picking tests the view ray against the triangles directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import vtkmodules.vtkInteractionStyle  # noqa: F401 - registers interactor styles
import vtkmodules.vtkRenderingFreeType  # noqa: F401 - registers text drawing
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401 - registers the OpenGL backend
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray
from vtkmodules.vtkCommonCore import vtkPoints
from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPlane, vtkPlaneCollection, vtkPolyData
from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget
from vtkmodules.vtkIOImage import vtkPNGWriter
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkRenderingCore import (
    vtkActor,
    vtkBillboardTextActor3D,
    vtkMapper,
    vtkPolyDataMapper,
    vtkRenderer,
    vtkWindowToImageFilter,
)

from bimforge.geometry.mesh import Mesh
from bimforge.geometry.mesh_ops import feature_edges, fit_camera, main_extent, ray_hits
from bimforge.plugins.sdk import PluginInfo, RenderBackend, RenderStyle

HIGHLIGHT_COLOR = (1.0, 0.78, 0.1)
EDGE_COLOR = (0.16, 0.18, 0.22)
XRAY_OPACITY = 0.15
WIREFRAME_OPACITY = 0.004  # all but invisible, but its depth still gives the lines their order
FARTHEST_VIEW = 4.0  # how far out zooming can go, in fitted views
NEAREST_VIEW = 0.0015  # how far in, as a share of the model's size
NEAREST_METRES = 0.5
LOOSE_FOCUS = 0.7  # how far past the model's edge the centre of the view can go, in half view heights
LABEL_CELL = (150, 62)  # px: one label per patch of screen, so they never pile up
ZOOM_REACH = 8.0  # how many view distances away the cursor's point may be for zooming towards it
LABEL_MIN_PIXELS = 45  # a label shows once the narrow side of its room is this wide on screen
VIEW_DIRECTIONS = {"iso": (1, -1, 0.8), "top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0),
                   "back": (0, 1, 0), "left": (-1, 0, 0), "right": (1, 0, 0)}


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


def _triangles(vertices: np.ndarray, faces: np.ndarray) -> vtkPolyData:
    poly = vtkPolyData()
    poly.SetPoints(_points(vertices))
    poly.SetPolys(_cells(faces, 3))
    return poly


def _lines(segments: np.ndarray) -> vtkPolyData:
    poly = vtkPolyData()
    poly.SetPoints(_points(segments.reshape(-1, 3)))
    poly.SetLines(_cells(np.arange(segments.shape[0] * 2), 2))
    return poly


@dataclass(eq=False)
class _Item:
    key: str
    mesh: Mesh
    style: RenderStyle
    box: tuple[float, ...]
    edges: np.ndarray | None = None  # worked out when first needed


@dataclass(eq=False)
class _Batch:
    """Everything of one look, drawn together. Rebuilt only when its contents change."""

    style: RenderStyle
    surface: vtkActor
    outline: vtkActor
    items: dict[str, _Item] = field(default_factory=dict)
    dirty: bool = True
    # What a click tests against, one row per triangle:
    corner: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    side1: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    side2: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    owner: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    owner_keys: list[str] = field(default_factory=list)


class VtkRenderBackend(RenderBackend):
    info = PluginInfo(id="bimforge.render.vtk", name="VTK", version="0.2.0", license="BSD-3-Clause")

    def __init__(self, renderer: vtkRenderer) -> None:
        self.renderer = renderer
        self._items: dict[str, _Item] = {}
        self._batches: dict[RenderStyle, _Batch] = {}
        self._selected_batch: _Batch | None = None
        self._selected_dirty = False
        self._lines: dict[str, tuple[vtkActor, RenderStyle]] = {}
        self._labels: dict[str, vtkBillboardTextActor3D] = {}
        self._hidden_categories: set[str] = set()
        self._highlighted: set[str] = set()
        self._hidden_keys: set[str] = set()
        self._clip: vtkPlaneCollection | None = None
        self._clip_box: tuple[np.ndarray, np.ndarray] | None = None
        self._display_style = "edges"
        self._grid: list[vtkActor] = []
        self._axes: vtkOrientationMarkerWidget | None = None
        vtkMapper.SetResolveCoincidentTopologyToPolygonOffset()  # edges sit cleanly on their faces
        self._frame: tuple[float, ...] | None = None
        self._frame_dirty = True
        self._underlay_frame: tuple[float, ...] | None = None
        renderer.AddObserver("StartEvent", lambda *_: self._before_frame())

    # --- contents ------------------------------------------------------------------

    def _new_actor(self, poly: vtkPolyData, surface: bool) -> vtkActor:
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.ScalarVisibilityOff()
        if self._clip is not None:
            mapper.SetClippingPlanes(self._clip)
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.PickableOff()
        if surface:
            prop = actor.GetProperty()
            prop.SetInterpolationToFlat()
            prop.SetAmbient(0.3)
            prop.SetDiffuse(0.75)
        self.renderer.AddActor(actor)
        return actor

    def _new_batch(self, style: RenderStyle) -> _Batch:
        return _Batch(style, self._new_actor(vtkPolyData(), True), self._new_actor(vtkPolyData(), False))

    def _batch_for(self, style: RenderStyle) -> _Batch:
        batch = self._batches.get(style)
        if batch is None:
            batch = self._batches[style] = self._new_batch(style)
            self._apply_batch_look(batch)
        return batch

    def upsert_mesh(self, key: str, mesh: Mesh, style: RenderStyle) -> None:
        self._remove_item(key)
        low, high = mesh.bounds()
        item = self._items[key] = _Item(key, mesh, style, (low[0], high[0], low[1], high[1], low[2], high[2]))
        self._frame_dirty = True
        self._place(item)
        self._update_label(key)

    def _in_view(self, key: str) -> bool:
        return key not in self._hidden_keys

    def _place(self, item: _Item) -> None:
        """An item is drawn by its style's batch, unless it is selected or hidden."""
        if item.key in self._highlighted:
            self._selected_dirty = True
        elif self._in_view(item.key):
            batch = self._batch_for(item.style)
            batch.items[item.key] = item
            batch.dirty = True

    def _remove_item(self, key: str) -> None:
        item = self._items.pop(key, None)
        if item is None:
            return
        self._frame_dirty = True
        batch = self._batches.get(item.style)
        if batch is not None and batch.items.pop(key, None) is not None:
            batch.dirty = True
        if key in self._highlighted:
            self._selected_dirty = True

    def upsert_lines(self, key: str, segments: np.ndarray, style: RenderStyle) -> None:
        self._remove_lines(key)
        actor = self._new_actor(_lines(segments), False)
        actor.GetProperty().SetColor(*style.color)
        actor.GetProperty().SetOpacity(style.opacity)
        actor.SetVisibility(style.category not in self._hidden_categories)
        self._lines[key] = (actor, style)
        if style.category == "underlay":
            lo, hi = segments.min(axis=1), segments.max(axis=1)
            self._underlay_frame = main_extent(np.column_stack([lo[:, 0], hi[:, 0], lo[:, 1], hi[:, 1], lo[:, 2], hi[:, 2]]))
            self._frame_dirty = True

    def _remove_lines(self, key: str) -> None:
        entry = self._lines.pop(key, None)
        if entry is not None:
            self.renderer.RemoveActor(entry[0])
            if entry[1].category == "underlay":
                self._underlay_frame = None
                self._frame_dirty = True

    def remove(self, key: str) -> None:
        self._remove_item(key)
        self._remove_lines(key)
        label = self._labels.pop(key, None)
        if label is not None:
            self.renderer.RemoveActor(label)

    def clear(self, prefix: str = "") -> None:
        for key in [k for k in (*self._items, *self._lines, *self._labels) if k.startswith(prefix)]:
            self.remove(key)

    def set_label(self, key: str, text: str, position: tuple[float, float, float]) -> None:
        """Text that floats at a point and always faces the viewer; it follows the visibility of `key`."""
        label = self._labels.get(key)
        if label is None:
            label = vtkBillboardTextActor3D()
            label.PickableOff()
            prop = label.GetTextProperty()
            prop.SetFontSize(13)
            prop.SetColor(0.08, 0.09, 0.11)
            prop.SetBackgroundColor(1, 1, 1)
            prop.SetBackgroundOpacity(0.7)
            prop.SetJustificationToCentered()
            self._labels[key] = label
            self.renderer.AddActor(label)
        label.SetInput(text)
        label.SetPosition(*position)
        self._update_label(key)

    def _update_label(self, key: str) -> None:
        label = self._labels.get(key)
        if label is not None:
            label.SetVisibility(self.is_shown(key) and self._label_fits(key))

    def _pixels_per_metre(self) -> float:
        camera = self.renderer.GetActiveCamera()
        height = max(self.renderer.GetSize()[1], 1)
        if camera.GetParallelProjection():
            visible = 2 * camera.GetParallelScale()
        else:
            visible = 2 * camera.GetDistance() * np.tan(np.radians(camera.GetViewAngle() / 2))
        return height / max(visible, 1e-9)

    def _label_fits(self, key: str) -> bool:
        item = self._items.get(key)
        if item is None:
            return False
        narrow = min(item.box[1] - item.box[0], item.box[3] - item.box[2])
        return bool(narrow * self._pixels_per_metre() >= LABEL_MIN_PIXELS)

    def _declutter_labels(self) -> None:
        """Runs before each frame. Too small a room gets no label, and in a crowd the biggest room's wins."""
        renderer = self.renderer
        taken: set[tuple[int, int]] = set()
        fitting = [k for k in self._labels if self.is_shown(k) and self._label_fits(k)]
        for key in sorted(fitting, key=self._room_size, reverse=True):
            label = self._labels[key]
            renderer.SetWorldPoint(*label.GetPosition(), 1.0)
            renderer.WorldToDisplay()
            x, y, _ = renderer.GetDisplayPoint()
            cell = (int(x // LABEL_CELL[0]), int(y // LABEL_CELL[1]))
            free = cell not in taken
            if free:
                taken.add(cell)
            if bool(label.GetVisibility()) != free:
                label.SetVisibility(free)
        for key in self._labels.keys() - set(fitting):
            if self._labels[key].GetVisibility():
                self._labels[key].SetVisibility(False)

    def _room_size(self, key: str) -> float:
        box = self._items[key].box
        return (box[1] - box[0]) * (box[3] - box[2])

    def _before_frame(self) -> None:
        self._keep_in_reach()
        self._set_clipping()
        self._declutter_labels()

    def frame_bounds(self) -> tuple[float, ...] | None:
        """What the view is about: the model's main body, or the drawing if nothing has been detected."""
        if self._frame_dirty:
            boxes = [item.box for key, item in self._items.items() if self.is_shown(key)]
            self._frame = main_extent(np.array(boxes)) if boxes else self._underlay_frame
            self._frame_dirty = False
        return self._frame

    def _model_size(self, frame: tuple[float, ...]) -> float:
        return max(float(np.linalg.norm([frame[1] - frame[0], frame[3] - frame[2], frame[5] - frame[4]])), 1.0)

    def _keep_in_reach(self) -> None:
        """However the view is moved, the model stays where a move or two can bring it back."""
        frame = self.frame_bounds()
        if frame is None:
            return
        camera = self.renderer.GetActiveCamera()
        focal, position = np.array(camera.GetFocalPoint()), np.array(camera.GetPosition())
        size = self._model_size(frame)
        # Measured in what is on screen now, so some of the model stays in view at any zoom.
        half_view = camera.GetParallelScale() if camera.GetParallelProjection() else camera.GetDistance() * np.tan(np.radians(camera.GetViewAngle() / 2))
        loose = LOOSE_FOCUS * half_view
        low, high = np.array(frame[0::2]) - loose, np.array(frame[1::2]) + loose
        low[2], high[2] = frame[4], frame[5]  # across the plan only: height added to that would leave the screen
        clamped = np.clip(focal, low, high)
        if np.any(clamped != focal):
            position = position + (clamped - focal)
            focal = clamped
            camera.SetFocalPoint(*focal)
            camera.SetPosition(*position)
        nearest = max(size * NEAREST_VIEW, NEAREST_METRES)
        if camera.GetParallelProjection():
            camera.SetParallelScale(float(np.clip(camera.GetParallelScale(), nearest / 2, FARTHEST_VIEW * size)))
            return
        distance = camera.GetDistance()
        farthest = FARTHEST_VIEW * size / max(np.tan(np.radians(camera.GetViewAngle() / 2)), 1e-3)
        allowed = float(np.clip(distance, nearest, farthest))
        if allowed != distance:
            camera.SetPosition(*(focal + (position - focal) / distance * allowed))

    def _set_clipping(self) -> None:
        """Depth range from the model, not from everything drawn: a stray object 5 km away would
        otherwise push the near limit out and slice off whatever is close to the camera."""
        frame = self.frame_bounds()
        if frame is None:
            return
        pad = self._model_size(frame)
        self.renderer.ResetCameraClippingRange(frame[0] - pad, frame[1] + pad, frame[2] - pad, frame[3] + pad,
                                               frame[4] - pad, frame[5] + pad)

    # --- drawing the batches ---------------------------------------------------------

    def _rebuild(self, batch: _Batch, items: list[_Item]) -> None:
        vertices, faces, base = [], [], 0
        keys, counts = [], []
        for item in items:
            vertices.append(item.mesh.vertices)
            faces.append(item.mesh.faces + base)
            base += len(item.mesh.vertices)
            keys.append(item.key)
            counts.append(len(item.mesh.faces))
        all_vertices = np.vstack(vertices) if vertices else np.zeros((0, 3))
        all_faces = np.vstack(faces) if faces else np.zeros((0, 3), dtype=np.int64)
        batch.surface.GetMapper().SetInputData(_triangles(all_vertices, all_faces))
        for item in items:
            if item.edges is None:
                item.edges = feature_edges(item.mesh.vertices, item.mesh.faces)
        outlines = [item.edges for item in items if len(item.edges)]
        batch.outline.GetMapper().SetInputData(_lines(np.vstack(outlines)) if outlines else vtkPolyData())
        corner = all_vertices[all_faces[:, 0]]
        batch.corner, batch.side1, batch.side2 = corner, all_vertices[all_faces[:, 1]] - corner, all_vertices[all_faces[:, 2]] - corner
        batch.owner = np.repeat(np.arange(len(keys)), counts)
        batch.owner_keys = keys
        batch.dirty = False

    def _flush(self) -> None:
        """Bring the batches up to date; many changes in a row cost one rebuild."""
        for batch in self._batches.values():
            if batch.dirty:
                self._rebuild(batch, list(batch.items.values()))
        if self._selected_dirty:
            if self._selected_batch is None:
                self._selected_batch = self._new_batch(RenderStyle())
            self._rebuild(self._selected_batch, self._selected_items())
            self._selected_dirty = False
            self._apply_selected_look()

    def _selected_items(self) -> list[_Item]:
        return [self._items[k] for k in self._highlighted if k in self._items and self.is_shown(k)]

    def _surface_opacity(self, style: RenderStyle) -> float:
        if self._display_style == "wireframe":
            return WIREFRAME_OPACITY
        return min(style.opacity, XRAY_OPACITY) if self._display_style == "xray" else style.opacity

    def _apply_batch_look(self, batch: _Batch) -> None:
        style = batch.style
        shown = style.category not in self._hidden_categories
        prop = batch.surface.GetProperty()
        prop.SetColor((1.0, 1.0, 1.0) if self._display_style == "hidden" else style.color)
        prop.SetOpacity(self._surface_opacity(style))
        batch.surface.SetVisibility(shown)
        outline = batch.outline.GetProperty()
        outline.SetColor(*EDGE_COLOR)
        outline.SetOpacity(1.0 if style.opacity > 0.9 else 0.6)
        batch.outline.SetVisibility(shown and self._display_style != "shaded")

    def _apply_selected_look(self) -> None:
        batch = self._selected_batch
        if batch is None:
            return
        prop = batch.surface.GetProperty()
        prop.SetColor(*HIGHLIGHT_COLOR)
        prop.SetOpacity(self._surface_opacity(RenderStyle()))
        outline = batch.outline.GetProperty()
        outline.SetColor(*HIGHLIGHT_COLOR)
        outline.SetLineWidth(2.0)

    def set_display_style(self, style: str) -> None:
        self._display_style = style
        for batch in self._batches.values():
            self._apply_batch_look(batch)
        self._apply_selected_look()

    def is_shown(self, key: str) -> bool:
        item = self._items.get(key)
        return item is not None and self._in_view(key) and item.style.category not in self._hidden_categories

    def set_category_visible(self, category: str, visible: bool) -> None:
        (self._hidden_categories.discard if visible else self._hidden_categories.add)(category)
        for batch in self._batches.values():
            if batch.style.category == category:
                self._apply_batch_look(batch)
        for actor, style in self._lines.values():
            if style.category == category:
                actor.SetVisibility(visible)
        for key in self._labels:
            self._update_label(key)
        self._selected_dirty = bool(self._highlighted)
        self._frame_dirty = True

    def _rehome(self, keys: Iterable[str]) -> None:
        """Move items to where they now belong after their selected or hidden state changed."""
        for key in keys:
            item = self._items.get(key)
            if item is None:
                continue
            batch = self._batches.get(item.style)
            if key in self._highlighted or not self._in_view(key):
                if batch is not None and batch.items.pop(key, None) is not None:
                    batch.dirty = True
            else:
                self._place(item)
            self._update_label(key)

    def set_highlight(self, keys: Iterable[str]) -> None:
        previous, self._highlighted = self._highlighted, set(keys)
        self._rehome(previous ^ self._highlighted)
        self._selected_dirty = True

    def set_hidden(self, keys: Iterable[str]) -> None:
        previous, self._hidden_keys = self._hidden_keys, set(keys)
        self._rehome(previous ^ self._hidden_keys)
        self._selected_dirty = bool(self._highlighted)
        self._frame_dirty = True

    # --- picking -------------------------------------------------------------------

    def _ray(self, x: int, y: int) -> tuple[np.ndarray, np.ndarray]:
        r = self.renderer
        ends = []
        for depth in (0.0, 1.0):
            r.SetDisplayPoint(x, y, depth)
            r.DisplayToWorld()
            wx, wy, wz, w = r.GetWorldPoint()
            ends.append(np.array([wx, wy, wz]) / (w or 1.0))
        return ends[0], ends[1] - ends[0]

    def _pick(self, x: int, y: int) -> tuple[str, np.ndarray] | None:
        self._flush()
        origin, direction = self._ray(x, y)
        batches = [b for b in self._batches.values() if b.style.category not in self._hidden_categories]
        if self._selected_batch is not None:
            batches.append(self._selected_batch)
        best: tuple[float, _Batch, int] | None = None
        for batch in batches:
            distances = ray_hits(origin, direction, batch.corner, batch.side1, batch.side2)
            if not len(distances):
                continue
            if self._clip_box is not None:
                near = np.flatnonzero(np.isfinite(distances))
                points = origin + distances[near, None] * direction
                outside = np.any((points < self._clip_box[0]) | (points > self._clip_box[1]), axis=1)
                distances[near[outside]] = np.inf
            nearest = int(np.argmin(distances))
            if np.isfinite(distances[nearest]) and (best is None or distances[nearest] < best[0]):
                best = (float(distances[nearest]), batch, nearest)
        if best is None:
            return None
        distance, batch, triangle = best
        return batch.owner_keys[batch.owner[triangle]], origin + distance * direction

    def pick(self, x: int, y: int) -> str | None:
        """x, y in VTK display coordinates (origin bottom-left)."""
        hit = self._pick(x, y)
        return hit[0] if hit else None

    def pick_on_plane(self, x: int, y: int, z: float) -> tuple[float, float, float] | None:
        origin, direction = self._ray(x, y)
        if abs(direction[2]) < 1e-12:
            return None  # looking exactly along the plane
        t = (z - origin[2]) / direction[2]
        if t < 0:
            return None
        hit = origin + t * direction
        return (float(hit[0]), float(hit[1]), float(z))

    def pick_surface(self, x: int, y: int) -> tuple[float, float, float] | None:
        hit = self._pick(x, y)
        return tuple(float(c) for c in hit[1]) if hit else None  # type: ignore[return-value]

    # --- view ----------------------------------------------------------------------

    def set_section_box(self, bounds) -> None:
        if bounds is None:
            self._clip = self._clip_box = None
        else:
            xmin, xmax, ymin, ymax, zmin, zmax = bounds
            self._clip_box = (np.array([xmin, ymin, zmin]), np.array([xmax, ymax, zmax]))
            planes = vtkPlaneCollection()
            for origin, normal in (((xmin, 0, 0), (1, 0, 0)), ((xmax, 0, 0), (-1, 0, 0)),
                                   ((0, ymin, 0), (0, 1, 0)), ((0, ymax, 0), (0, -1, 0)),
                                   ((0, 0, zmin), (0, 0, 1)), ((0, 0, zmax), (0, 0, -1))):
                plane = vtkPlane()
                plane.SetOrigin(*origin)
                plane.SetNormal(*normal)
                planes.AddItem(plane)
            self._clip = planes
        batches = [*self._batches.values(), *([self._selected_batch] if self._selected_batch else [])]
        actors = [a for b in batches for a in (b.surface, b.outline)] + [a for a, _ in self._lines.values()]
        for actor in actors:
            mapper = actor.GetMapper()
            if self._clip is None:
                mapper.RemoveAllClippingPlanes()
            else:
                mapper.SetClippingPlanes(self._clip)

    def bounds(self, keys: Iterable[str]):
        boxes = [self._items[k].box for k in keys if k in self._items]
        if not boxes:
            return None
        b = np.array(boxes)
        return (b[:, 0].min(), b[:, 1].max(), b[:, 2].min(), b[:, 3].max(), b[:, 4].min(), b[:, 5].max())

    def visible_bounds(self) -> dict[str, tuple]:
        """The box around each element that can currently be seen."""
        return {key: item.box for key, item in self._items.items() if self.is_shown(key)}

    def pan_by(self, across: float, along: float) -> None:
        """Slide the view by shares of its width and height."""
        camera = self.renderer.GetActiveCamera()
        focal, position = np.array(camera.GetFocalPoint()), np.array(camera.GetPosition())
        forward = focal - position
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, camera.GetViewUp())
        right /= np.linalg.norm(right)
        upward = np.cross(right, forward)
        width, height = self.renderer.GetSize()
        half = camera.GetParallelScale() if camera.GetParallelProjection() else camera.GetDistance() * np.tan(np.radians(camera.GetViewAngle() / 2))
        shift = right * across * 2 * half * width / max(height, 1) + upward * along * 2 * half
        camera.SetFocalPoint(*(focal + shift))
        camera.SetPosition(*(position + shift))

    def zoom_centre(self, factor: float) -> None:
        width, height = self.renderer.GetSize()
        focal = self.renderer.GetActiveCamera().GetFocalPoint()
        self.zoom_at(width // 2, height // 2, focal[2], factor)

    def zoom_at(self, x: int, y: int, z: float, factor: float) -> None:
        """Zoom keeping the point under the cursor still. factor > 1 goes closer.

        The point is on the working level. Near the horizon that point is too far off to be useful,
        so the zoom falls back to the middle of the view.
        """
        camera = self.renderer.GetActiveCamera()
        position, focal = np.array(camera.GetPosition()), np.array(camera.GetFocalPoint())
        hit = self.pick_on_plane(x, y, z)
        anchor = np.array(hit) if hit is not None else focal
        if np.linalg.norm(anchor - focal) > ZOOM_REACH * max(camera.GetDistance(), 1e-6):
            anchor = focal
        camera.SetPosition(*(anchor + (position - anchor) / factor))
        camera.SetFocalPoint(*(anchor + (focal - anchor) / factor))
        if camera.GetParallelProjection():
            camera.SetParallelScale(camera.GetParallelScale() / factor)
        self.renderer.ResetCameraClippingRange()

    def zoom_to(self, bounds) -> None:
        camera = self.renderer.GetActiveCamera()
        focal, position = np.array(camera.GetFocalPoint()), np.array(camera.GetPosition())
        direction = focal - position
        if np.linalg.norm(direction) == 0:
            direction = np.array([1.0, -1.0, -0.8])
        camera.OrthogonalizeViewUp()
        width, height = self.renderer.GetSize()
        aspect = width / height if width > 0 and height > 0 else 1.5
        target, distance, half_height = fit_camera(bounds, direction, np.array(camera.GetViewUp()),
                                                   float(np.tan(np.radians(camera.GetViewAngle() / 2))), aspect)
        forward = direction / np.linalg.norm(direction)
        if camera.GetParallelProjection():
            camera.SetParallelScale(max(half_height, 1e-3))
            size = self._model_size(tuple(bounds))
            distance = max(distance, size)
        camera.SetFocalPoint(*target)
        camera.SetPosition(*(target - forward * max(distance, 1e-3)))
        self._set_clipping()

    def fit_view(self) -> None:
        self._reset_camera()

    def set_view(self, preset: str) -> None:
        camera = self.renderer.GetActiveCamera()
        camera.SetFocalPoint(0, 0, 0)
        camera.SetPosition(*VIEW_DIRECTIONS[preset])
        camera.SetViewUp(*((0, 1, 0) if preset in ("top", "bottom") else (0, 0, 1)))
        self._reset_camera()

    def set_orthographic(self, enabled: bool) -> None:
        self.renderer.GetActiveCamera().SetParallelProjection(enabled)
        self._reset_camera()

    def set_background(self, top, bottom) -> None:
        self.renderer.GradientBackgroundOn()
        self.renderer.SetBackground(*bottom)
        self.renderer.SetBackground2(*top)

    def set_grid(self, minor, major=None) -> None:
        for actor in self._grid:
            self.renderer.RemoveActor(actor)
        self._grid = []
        for lines, color, opacity in ((minor, (0.5, 0.55, 0.62), 0.28), (major, (0.42, 0.47, 0.55), 0.6)):
            if lines is None or not len(lines):
                continue
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(_lines(lines))
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.PickableOff()
            actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetOpacity(opacity)
            self.renderer.AddActor(actor)
            self._grid.append(actor)

    def show_axes(self, interactor) -> None:
        """The small X/Y/Z indicator in the corner of the view."""
        axes = vtkAxesActor()
        axes.SetShaftTypeToCylinder()
        axes.SetCylinderRadius(0.04)
        axes.SetConeRadius(0.25)
        for caption in (axes.GetXAxisCaptionActor2D(), axes.GetYAxisCaptionActor2D(), axes.GetZAxisCaptionActor2D()):
            caption.GetCaptionTextProperty().SetFontSize(14)
            caption.GetCaptionTextProperty().ShadowOff()
        self._axes = vtkOrientationMarkerWidget()
        self._axes.SetOrientationMarker(axes)
        self._axes.SetInteractor(interactor)
        self._axes.SetViewport(0.0, 0.0, 0.11, 0.17)
        self._axes.SetEnabled(1)
        self._axes.InteractiveOff()

    def save_image(self, path: Path) -> None:
        self.render()
        image = vtkWindowToImageFilter()
        image.SetInput(self.renderer.GetRenderWindow())
        image.ReadFrontBufferOff()
        image.Update()
        writer = vtkPNGWriter()
        writer.SetFileName(str(path))
        writer.SetInputConnection(image.GetOutputPort())
        writer.Write()

    def _reset_camera(self) -> None:
        """Fill the window with the model. The grid only counts if there is nothing else to look at."""
        self._flush()
        frame = self.frame_bounds()
        if frame is None and self._grid:
            boxes = [a.GetBounds() for a in self._grid]
            frame = (min(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes),
                     max(b[3] for b in boxes), 0.0, 0.0)
        if frame is None:
            self.renderer.ResetCamera()
            return
        self.zoom_to(frame)

    def render(self) -> None:
        self._flush()
        window = self.renderer.GetRenderWindow()
        if window is not None:
            window.Render()
