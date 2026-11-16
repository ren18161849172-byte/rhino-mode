# SPDX-License-Identifier: GPL-3.0-or-later
"""Object snaps (Osnap) for picking points, as Rhino's persistent osnaps.

``SnapTargets`` collects, per command and per object when the mouse first
comes near it, what can be snapped to in the visible objects, in world space
and as numpy arrays:

* curves: vertices (End), segment midpoints (Mid), the center of closed
  polylines (Cen), segments (Near, Int, Perp), and arcs and circles made by
  R2B, also inside joined curves (Cen, Mid of arcs, Quad, Tan, Perp, Near;
  see polycurve.py); the joints of joined curves (End)
* meshes: vertices (End), edge midpoints (Mid), edges (Near, Int, Perp)
* empties: their location (Point)

``snap()`` then finds, within a radius around the mouse, the closest snap of
the enabled modes, as Rhino does; Near is only used when nothing else is in
the radius. Int snaps to where two segments cross in the view: the true
intersection if they meet in 3D, otherwise the apparent one on the segment
nearer to the viewer.
"""

import math
from dataclasses import dataclass
from itertools import combinations

import bpy
import numpy as np
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Vector, geometry

from . import polycurve
from .projection import project, to_world

MODES = (
    ('END', "End", "Ends of curves and segments, and mesh vertices"),
    ('NEAR', "Near", "The closest point on a curve or edge"),
    ('POINT', "Point", "Point objects (empties)"),
    ('MID', "Mid", "Midpoints of segments and edges"),
    ('CEN', "Cen", "Centers of circles, arcs and closed polylines"),
    ('INT', "Int", "Intersections of curves and edges"),
    ('PERP', "Perp", "Perpendicular to a curve from the previous point"),
    ('TAN', "Tan", "Tangent to a circle or arc from the previous point"),
    ('QUAD', "Quad", "Quadrant points of circles and arcs"),
)
LABELS = {mode: label for mode, label, _ in MODES}
# Typed during a command: snap the next point with this mode only.
ONE_SHOT = {label.lower(): mode for mode, label, _ in MODES}
DEFAULT_MODES = {'END', 'POINT', 'MID', 'CEN', 'INT'}

# Custom property on curve objects that are circles or arcs: center, x_axis,
# y_axis (local space), radius, start and end angles (radians).
ARC_PROPERTY = polycurve.ARC_PROPERTY

# Distance under which two segments meet in 3D, relative to their length.
INTERSECTION_TOLERANCE = 1e-6
# Segments near the mouse that are tested against each other for Int.
INTERSECTION_CANDIDATES = 24


@dataclass(frozen=True)
class Arc:
    center: Vector
    x_axis: Vector
    y_axis: Vector
    radius: float
    start: float = 0.0
    end: float = 2 * math.pi

    @property
    def normal(self):
        return self.x_axis.cross(self.y_axis)

    def at(self, angle):
        return self.center + (self.x_axis * math.cos(angle) + self.y_axis * math.sin(angle)) * self.radius

    def angle_of(self, point):
        offset = point - self.center
        return math.atan2(offset.dot(self.y_axis), offset.dot(self.x_axis))

    def contains(self, angle):
        if self.end - self.start >= 2 * math.pi - 1e-9:
            return True
        return (angle - self.start) % (2 * math.pi) <= (self.end - self.start) + 1e-9


def _arcs_of(obj):
    matrix = obj.matrix_world
    linear = matrix.to_3x3()
    arcs = []
    for data in polycurve.arcs_of(obj):
        x_axis, y_axis = linear @ Vector(data["x_axis"]), linear @ Vector(data["y_axis"])
        # The object's scale in the arc's own plane (not its x scale: an arc
        # drawn in the Right viewport lies in y and z). Scaled unevenly
        # there, it is an ellipse: no arc to snap to.
        scale = x_axis.length
        if scale < 1e-12 or abs(y_axis.length - scale) > 1e-5 * scale or abs(x_axis.dot(y_axis)) > 1e-5 * scale * scale:
            continue
        arcs.append(Arc(matrix @ Vector(data["center"]), x_axis / scale, y_axis / y_axis.length,
                        data["radius"] * scale, data["start"], data["end"]))
    return arcs


def object_edges(obj, depsgraph):
    """World-space edges (N x 2 x 3) of an object's evaluated geometry, for
    drawing it (e.g. while it is moved); a small cross for empties."""
    if obj.type == 'EMPTY':
        size = obj.empty_display_size
        location = np.array(tuple(obj.matrix_world.translation))
        axes = np.eye(3) * size
        return np.stack([np.stack([location - a, location + a]) for a in axes])
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError:
        return np.empty((0, 2, 3))
    try:
        if mesh is None or not len(mesh.vertices) or not len(mesh.edges):
            return np.empty((0, 2, 3))
        local = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", local)
        vertices = to_world(local.reshape(-1, 3).astype(np.float64), evaluated.matrix_world)
        indices = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", indices)
        return vertices[indices.reshape(-1, 2)]
    finally:
        evaluated.to_mesh_clear()


def _points_array(points):
    return np.array([tuple(p) for p in points], dtype=np.float64).reshape(-1, 3)


# Meshes and curves with more snap targets than this are sorted into a grid
# of cells; a mouse move then looks only at the cells near the mouse.
TILE_TARGETS = 4096
# Targets per cell, roughly.
TARGETS_PER_CELL = 1024
_NO_POINTS = np.empty((0, 3))
_NO_SEGMENTS = np.empty((0, 2, 3))


def _near_boxes(corners, region, rv3d, mouse, radius):
    """Which boxes (N x 8 x 3 world-space corners) can have something within
    ``radius`` pixels of ``mouse`` in the view: their projected rectangle,
    grown by the radius, holds the mouse. Boxes partly behind the viewer are
    kept, boxes wholly behind it are not."""
    if not len(corners):
        return np.zeros(0, dtype=bool)
    xy = project(corners.reshape(-1, 3), region, rv3d).reshape(-1, 8, 2)
    behind = np.isnan(xy[..., 0])
    low = np.where(behind[..., None], np.inf, xy).min(axis=1)
    high = np.where(behind[..., None], -np.inf, xy).max(axis=1)
    inside = ((low[:, 0] - radius <= mouse[0]) & (mouse[0] <= high[:, 0] + radius)
              & (low[:, 1] - radius <= mouse[1]) & (mouse[1] <= high[:, 1] + radius))
    return (inside | behind.any(axis=1)) & ~behind.all(axis=1)


def _box_corners(low, high):
    """Corners (N x 8 x 3) of axis-aligned boxes from their lows and highs (N x 3)."""
    pick = np.array([[i & 1, (i >> 1) & 1, (i >> 2) & 1] for i in range(8)], dtype=bool)
    return np.where(pick[None, :, :], high[:, None, :], low[:, None, :])


class _Cells:
    """An object's snap targets sorted into a grid of cells over its
    bounding box (segments by their middle; a cell's box holds both ends)."""

    def __init__(self, points, segments, low, high):
        """``low``, ``high``: the object's bounding box (world space); targets
        outside it go to the nearest cell."""
        count = sum(len(a) for a in points.values()) + 2 * len(segments)
        extent = np.maximum(high - low, 1e-9)
        wanted = max(1, count // TARGETS_PER_CELL)
        larger = np.sort(extent)[1:]  # Flat objects (terrain) are cut in their plane.
        size = max(math.sqrt(larger[0] * larger[1] / wanted), extent.max() / 512)
        shape = np.maximum(np.ceil(extent / size).astype(np.int64), 1)

        def cell_of(xyz):
            ijk = np.clip(((xyz - low) / size).astype(np.int64), 0, shape - 1)
            return (ijk[:, 0] * shape[1] + ijk[:, 1]) * shape[2] + ijk[:, 2]

        self._sorted = {}  # mode or 'SEGMENTS' -> (targets sorted by cell, their cells)
        middles = points.get('MID')
        if len(segments):
            cells = cell_of(segments.mean(axis=1))
            order = np.argsort(cells)
            self._sorted['SEGMENTS'] = (segments[order], cells[order])
            if middles is not None and len(middles) == len(segments):
                # A mesh's Mid points are its edges' middles, in the same order.
                self._sorted['MID'] = (middles[order], cells[order])
        for mode, array in points.items():
            if len(array) and mode not in self._sorted:
                cells = cell_of(array)
                order = np.argsort(cells)
                self._sorted[mode] = (array[order], cells[order])
        # The occupied cells (each kind's cells are sorted already).
        occupied = [cells[np.r_[True, cells[1:] != cells[:-1]]] for _, cells in self._sorted.values()]
        self._cells = occupied[0] if len(occupied) == 1 else np.unique(np.concatenate(occupied))
        low_corner = np.full((len(self._cells), 3), np.inf)
        high_corner = np.full((len(self._cells), 3), -np.inf)
        self._ranges = {}
        for mode, (array, cells) in self._sorted.items():
            starts = np.searchsorted(cells, self._cells, side='left')
            ends = np.searchsorted(cells, self._cells, side='right')
            self._ranges[mode] = (starts, ends)
            used = ends > starts
            lows = np.minimum(array[:, 0], array[:, 1]) if array.ndim == 3 else array
            highs = np.maximum(array[:, 0], array[:, 1]) if array.ndim == 3 else array
            low_corner[used] = np.minimum(low_corner[used], np.minimum.reduceat(lows, starts[used], axis=0))
            high_corner[used] = np.maximum(high_corner[used], np.maximum.reduceat(highs, starts[used], axis=0))
        self._corners = _box_corners(low_corner, high_corner)

    def near(self, region, rv3d, mouse, radius):
        picked = np.flatnonzero(_near_boxes(self._corners, region, rv3d, mouse, radius))
        found = {}
        for mode, (array, _cells) in self._sorted.items():
            starts, ends = self._ranges[mode]
            parts = [array[a:b] for a, b in zip(starts[picked], ends[picked]) if b > a]
            if parts:
                found[mode] = np.concatenate(parts)
        segments = found.pop('SEGMENTS', _NO_SEGMENTS)
        return found, segments


class _ObjectTargets:
    """The snap targets of one object (world space): points by mode and
    segments, in cells when there are many."""

    def __init__(self, points, segments, corners):
        self.points = {mode: np.concatenate(found) for mode, found in points.items() if found}
        self.segments = np.concatenate(segments) if segments else _NO_SEGMENTS
        count = sum(len(a) for a in self.points.values()) + len(self.segments)
        self.cells = (_Cells(self.points, self.segments, corners.min(axis=0), corners.max(axis=0))
                      if count > TILE_TARGETS else None)

    def near(self, region, rv3d, mouse, radius):
        """(points by mode, segments) that can be within the radius of the mouse."""
        if self.cells is None:
            return self.points, self.segments
        return self.cells.near(region, rv3d, mouse, radius)


class SnapTargets:
    """What can be snapped to in the visible objects (world space).

    Only the objects' bounding boxes are read when a command starts; an
    object's points and segments are read the first time the mouse comes near
    it in a view, and kept for the rest of the command. In a heavy scene
    (millions of points) a command starts at once and a mouse move looks
    only at what is near the mouse."""

    def __init__(self, context):
        depsgraph = context.evaluated_depsgraph_get()
        self.arcs = []
        empties = []
        self._names = []
        corners = []
        for obj in context.visible_objects:
            if obj.type in ('MESH', 'CURVE'):
                evaluated = obj.evaluated_get(depsgraph)
                corners.append(to_world(np.array([tuple(c) for c in evaluated.bound_box], dtype=np.float64),
                                        evaluated.matrix_world))
                self._names.append(obj.name)
                if obj.type == 'CURVE':
                    self.arcs += _arcs_of(obj)
            elif obj.type == 'EMPTY':
                empties.append(_points_array([obj.matrix_world.translation]))
        self._corners = np.array(corners, dtype=np.float64).reshape(-1, 8, 3)
        self._points = np.concatenate(empties) if empties else _NO_POINTS
        self._objects = {}  # name -> _ObjectTargets, read when first needed

    def _object_targets(self, index):
        name = self._names[index]
        found = self._objects.get(name)
        if found is None:
            obj = bpy.data.objects.get(name)
            points = {'END': [], 'MID': [], 'CEN': []}
            segments = []
            if obj is not None:
                evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
                if obj.type == 'MESH':
                    self._add_mesh(evaluated, points, segments)
                else:
                    self._add_curve(obj, evaluated, points, segments)
            found = self._objects[name] = _ObjectTargets(points, segments, self._corners[index])
        return found

    def near(self, region, rv3d, mouse, radius):
        """(points by mode, segments, owners) of the objects that can be within
        ``radius`` pixels of the mouse, for one mouse position; ``owners``:
        which object (an index) each segment is from."""
        points = {'END': [], 'MID': [], 'CEN': [], 'POINT': [self._points]}
        segments, owners = [], []
        for index in np.flatnonzero(_near_boxes(self._corners, region, rv3d, mouse, radius)):
            found, found_segments = self._object_targets(index).near(region, rv3d, mouse, radius)
            for mode, array in found.items():
                points[mode].append(array)
            if len(found_segments):
                segments.append(found_segments)
                owners.append(np.full(len(found_segments), index))
        points = {mode: np.concatenate(found) if found else _NO_POINTS for mode, found in points.items()}
        if not segments:
            return points, _NO_SEGMENTS, np.empty(0, dtype=np.int64)
        return points, np.concatenate(segments), np.concatenate(owners)

    @staticmethod
    def _add_mesh(evaluated, points, segments):
        mesh = evaluated.data
        if not len(mesh.vertices):
            return
        local = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", local)
        vertices = to_world(local.reshape(-1, 3).astype(np.float64), evaluated.matrix_world)
        points['END'].append(vertices)
        if len(mesh.edges):
            indices = np.empty(len(mesh.edges) * 2, dtype=np.int32)
            mesh.edges.foreach_get("vertices", indices)
            edges = vertices[indices.reshape(-1, 2)]
            segments.append(edges)
            points['MID'].append(edges.mean(axis=1))

    @staticmethod
    def _add_curve(obj, evaluated, points, segments):
        smooth = False
        for spline in obj.data.splines:
            if spline.type != 'POLY':
                smooth = True
                ends = ([spline.bezier_points[0].co, spline.bezier_points[-1].co] if spline.type == 'BEZIER'
                        else [spline.points[0].co.xyz, spline.points[-1].co.xyz])
                if not spline.use_cyclic_u:
                    points['END'].append(to_world(_points_array(ends), obj.matrix_world))
                continue
            vertices = to_world(_points_array(p.co.xyz for p in spline.points), obj.matrix_world)
            points['END'].append(vertices)
            if len(vertices) < 2:
                continue
            pairs = np.stack([vertices[:-1], vertices[1:]], axis=1)
            if spline.use_cyclic_u and len(vertices) >= 3:
                pairs = np.concatenate([pairs, np.stack([vertices[-1:], vertices[:1]], axis=1)])
                points['CEN'].append(vertices.mean(axis=0, keepdims=True))
            segments.append(pairs)
            points['MID'].append(pairs.mean(axis=1))
        joints = polycurve.joints_of(obj)
        if len(joints):
            points['END'].append(to_world(joints, obj.matrix_world))
        if smooth:
            # Bezier and NURBS splines: their tessellation for Near, Int, Perp.
            mesh = evaluated.to_mesh()
            try:
                SnapTargets._add_mesh_edges(mesh, evaluated.matrix_world, segments)
            finally:
                evaluated.to_mesh_clear()

    @staticmethod
    def _add_mesh_edges(mesh, matrix_world, segments):
        if not len(mesh.vertices) or not len(mesh.edges):
            return
        local = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", local)
        vertices = to_world(local.reshape(-1, 3).astype(np.float64), matrix_world)
        indices = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", indices)
        segments.append(vertices[indices.reshape(-1, 2)])

    def snap(self, region, rv3d, mouse, modes, radius, previous=None, extra_points=(), extra_segments=()):
        """(point, mode) of the snap closest to the mouse (region coordinates)
        within ``radius`` pixels, or None. ``extra_*``: geometry of the
        command in progress (e.g. the polyline being drawn)."""
        mouse = np.array(mouse, dtype=np.float64)
        best = []

        def consider(distance, mode, point):
            if distance <= radius and (not best or distance < best[0][0]):
                best[:] = [(distance, mode, Vector(point))]

        points, segments, owners = self.near(region, rv3d, mouse, radius)
        extra_points = _points_array(extra_points)
        extra_segments = np.array([(tuple(a), tuple(b)) for a, b in extra_segments], dtype=np.float64).reshape(-1, 2, 3)
        for mode in ('END', 'MID', 'CEN', 'POINT'):
            if mode not in modes:
                continue
            candidates = points[mode]
            if mode == 'END':
                candidates = np.concatenate([candidates, extra_points])
            elif mode == 'MID':
                candidates = np.concatenate([candidates, extra_segments.mean(axis=1)])
            if len(candidates):
                xy = project(candidates, region, rv3d)
                distance = np.hypot(xy[:, 0] - mouse[0], xy[:, 1] - mouse[1])
                distance[np.isnan(distance)] = np.inf
                index = int(np.argmin(distance))
                consider(distance[index], mode, candidates[index])

        segments = np.concatenate([segments, extra_segments])
        owners = np.concatenate([owners, np.full(len(extra_segments), -1)])
        near, starts, ends = self._segments_near(segments, region, rv3d, mouse, radius)
        if 'INT' in modes:
            self._intersections(segments, owners, near[:INTERSECTION_CANDIDATES], starts, ends, region, rv3d, mouse,
                                consider)
        if 'PERP' in modes and previous is not None:
            for index in near:
                a, b = Vector(segments[index][0]), Vector(segments[index][1])
                foot, t = geometry.intersect_point_line(previous, a, b)
                if 0.0 < t < 1.0:
                    self._consider_point(foot, 'PERP', region, rv3d, mouse, consider)
        self._arc_snaps(modes, previous, region, rv3d, mouse, consider)
        if best:
            return best[0][2], best[0][1]

        if 'NEAR' in modes:
            ray_origin, ray_direction = self._ray(region, rv3d, mouse)
            if len(near):
                a, b = Vector(segments[near[0]][0]), Vector(segments[near[0]][1])
                return self._closest_on_segment(a, b, ray_origin, ray_direction), 'NEAR'
            for arc in self.arcs:
                hit = geometry.intersect_line_plane(ray_origin, ray_origin + ray_direction, arc.center, arc.normal)
                if hit is not None and (hit - arc.center).length > 1e-12:
                    angle = arc.angle_of(hit)
                    if arc.contains(angle):
                        point = arc.at(angle)
                        xy = project([tuple(point)], region, rv3d)[0]
                        if np.hypot(*(xy - mouse)) <= radius:
                            return point, 'NEAR'
        return None

    # --- Helpers -----------------------------------------------------------------

    @staticmethod
    def _segments_near(segments, region, rv3d, mouse, radius):
        """Indices of segments within ``radius`` of the mouse in the view,
        closest first, and the segments' projected start and end points."""
        if not len(segments):
            return [], None, None
        projected = project(segments.reshape(-1, 3), region, rv3d).reshape(-1, 2, 2)
        starts, ends = projected[:, 0], projected[:, 1]
        direction = ends - starts
        length2 = (direction ** 2).sum(axis=1)
        with np.errstate(divide='ignore', invalid='ignore'):
            t = np.clip(((mouse - starts) * direction).sum(axis=1) / length2, 0.0, 1.0)
        closest = starts + direction * t[:, None]
        distance = np.hypot(closest[:, 0] - mouse[0], closest[:, 1] - mouse[1])
        distance[np.isnan(distance)] = np.inf
        inside = np.nonzero(distance <= radius)[0]
        return list(inside[np.argsort(distance[inside])]), starts, ends

    @staticmethod
    def _ray(region, rv3d, mouse):
        return region_2d_to_origin_3d(region, rv3d, mouse), region_2d_to_vector_3d(region, rv3d, mouse)

    @staticmethod
    def _closest_on_segment(a, b, ray_origin, ray_direction):
        """The point of segment ab closest to the view ray."""
        found = geometry.intersect_line_line(a, b, ray_origin, ray_origin + ray_direction)
        if found is None:  # Parallel to the ray: the segment looks like a point.
            return a.copy()
        _, t = geometry.intersect_point_line(found[0], a, b)
        return a.lerp(b, min(max(t, 0.0), 1.0))

    @staticmethod
    def _consider_point(point, mode, region, rv3d, mouse, consider):
        xy = project([tuple(point)], region, rv3d)[0]
        if not np.isnan(xy).any():
            consider(float(np.hypot(*(xy - mouse))), mode, point)

    def _intersections(self, segments, owners, near, starts, ends, region, rv3d, mouse, consider):
        for i, j in combinations(near, 2):
            size = max(float(np.linalg.norm(segments[i][1] - segments[i][0])),
                       float(np.linalg.norm(segments[j][1] - segments[j][0])), 1e-12)
            # Neighboring segments of one object (a curve's tessellation, a
            # polyline's sides) meet at their shared point: not an intersection
            # (else every tessellation point of a curve would be one, nearer
            # than the End where its parts meet). Two curves meeting end to
            # end still intersect there, as in Rhino.
            if owners[i] == owners[j]:
                gaps = np.linalg.norm(segments[i][:, None, :] - segments[j][None, :, :], axis=2)
                if gaps.min() <= 1e-9 * size:
                    continue
            crossing = geometry.intersect_line_line_2d(Vector(starts[i]), Vector(ends[i]),
                                                       Vector(starts[j]), Vector(ends[j]))
            if crossing is None:
                continue
            ray_origin, ray_direction = self._ray(region, rv3d, crossing)
            a = self._closest_on_segment(Vector(segments[i][0]), Vector(segments[i][1]), ray_origin, ray_direction)
            b = self._closest_on_segment(Vector(segments[j][0]), Vector(segments[j][1]), ray_origin, ray_direction)
            if (a - b).length > INTERSECTION_TOLERANCE * size:
                # Apparent intersection: the point on the segment nearer the viewer.
                a = min(a, b, key=lambda p: (p - ray_origin).dot(ray_direction))
            consider(float(np.hypot(crossing.x - mouse[0], crossing.y - mouse[1])), 'INT', a)

    def _arc_snaps(self, modes, previous, region, rv3d, mouse, consider):
        for arc in self.arcs:
            if 'CEN' in modes:
                self._consider_point(arc.center, 'CEN', region, rv3d, mouse, consider)
            if 'MID' in modes and arc.end - arc.start < 2 * math.pi - 1e-9:
                self._consider_point(arc.at((arc.start + arc.end) / 2), 'MID', region, rv3d, mouse, consider)
            if 'QUAD' in modes:
                for quarter in range(4):
                    angle = quarter * math.pi / 2
                    if arc.contains(angle):
                        self._consider_point(arc.at(angle), 'QUAD', region, rv3d, mouse, consider)
            if previous is None:
                continue
            offset = previous - arc.center
            local = Vector((offset.dot(arc.x_axis), offset.dot(arc.y_axis)))
            if local.length < 1e-12:
                continue
            toward = math.atan2(local.y, local.x)
            if 'PERP' in modes:
                for angle in (toward, toward + math.pi):
                    if arc.contains(angle % (2 * math.pi)):
                        self._consider_point(arc.at(angle), 'PERP', region, rv3d, mouse, consider)
            if 'TAN' in modes and local.length > arc.radius:
                spread = math.acos(arc.radius / local.length)
                for angle in (toward + spread, toward - spread):
                    if arc.contains(angle % (2 * math.pi)):
                        self._consider_point(arc.at(angle), 'TAN', region, rv3d, mouse, consider)
