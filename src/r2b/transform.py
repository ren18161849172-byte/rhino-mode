# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style transform commands (MODE feature): Move, Copy, Rotate, Scale
and Mirror, for objects and, in mesh and curve Edit Mode, for the selected
points (curve control points with their handles, or single handles).

They pick their points like the drawing commands (GetPoints), so Osnap,
typed coordinates, Ortho and constraints make them exact: Move goes from a
base point to a target point, Rotate turns about a center by a typed angle
or two reference points, and so on. Without a selection they first ask for
objects (or points). Dragging (see selection.py) uses the same move, from the
grabbed point to where the button is released. In mesh Edit Mode the
selected points move (Mirror mirrors them in place; Copy is Blender's
duplicate); with Proportional Editing on, the points around them follow
with Blender's falloff (Rhino's SoftMove). While picking, the outline is
drawn transformed; nothing changes until the command ends (Esc leaves
everything as it was).
"""

import heapq
import math
import random
from types import SimpleNamespace

import bmesh
import numpy as np

import bpy
from mathutils import Matrix, Vector

from . import command_line, cplane, getpoint, osnap
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-9


# Blender's proportional editing falloffs, of t = 1 - distance / size.
_FALLOFFS = {
    'SMOOTH': lambda t: 3 * t * t - 2 * t * t * t,
    'SPHERE': lambda t: math.sqrt(max(2 * t - t * t, 0.0)),
    'ROOT': math.sqrt,
    'INVERSE_SQUARE': lambda t: t * (2 - t),
    'SHARP': lambda t: t * t,
    'LINEAR': lambda t: t,
    'CONSTANT': lambda t: 1.0,
}


def _across_triangle(v0, v1, v2, dist1, dist2):
    """The distance at ``v0`` from the distances at ``v1`` and ``v2`` across
    their triangle: Blender's geodesic_distance_propagate_across_triangle, so
    that Connected Only measures along the surface (not only along edges)."""
    v10 = v0 - v1
    v12 = v2 - v1
    if dist1 != 0.0 and dist2 != 0.0:
        d12 = v12.length
        if d12 * d12 > 0.0:
            u = v12 / d12
            n = v12.cross(v10)
            if n.length_squared > 0.0:
                n.normalize()
            v = n.cross(u)
            x0, y0 = v10.dot(u), abs(v10.dot(v))
            # A virtual source at those distances from v1 and v2.
            a = (dist1 * dist1 - dist2 * dist2 + d12 * d12) / (2.0 * d12)
            hh = dist1 * dist1 - a * a
            if hh > 0.0:
                h = math.sqrt(hh)
                # Only if the line from it to v0 crosses the edge v1 v2.
                x_intercept = a + h * (x0 - a) / (y0 + h)
                if 0.0 <= x_intercept <= d12:
                    return math.hypot(x0 - a, y0 + h)
    return min(dist1 + v10.length, dist2 + (v0 - v2).length)


def _connected_distances(mesh, world, chosen, limit):
    """{vertex index: distance} along the surface from the selected points
    (``chosen``) to the points connected to them, up to ``limit``: along
    edges and across faces, with Blender's rules for Connected Only."""
    verts = mesh.verts
    best = {i: 0.0 for i in chosen}
    queue = [(0.0, i) for i in chosen]
    heapq.heapify(queue)

    def reach(j, distance, *sources):
        # As Blender: only points farther than the ones it comes from.
        if (distance <= limit and distance < best.get(j, math.inf) and all(best[k] < best.get(j, math.inf)
                                                                              for k in sources)
                and not verts[j].hide and not verts[j].select):
            best[j] = distance
            heapq.heappush(queue, (distance, j))

    while queue:
        d, i = heapq.heappop(queue)
        if d > best[i]:
            continue
        vertex = verts[i]
        for edge in vertex.link_edges:
            if edge.hide:
                continue
            k = edge.other_vert(vertex).index
            reach(k, d + (world[k] - world[i]).length, i)
            if k not in best:
                continue
            for face in edge.link_faces:
                if face.hide:
                    continue
                for other in face.verts:
                    o = other.index
                    if o != i and o != k:
                        reach(o, _across_triangle(world[o], world[i], world[k], best[i], best[k]), i, k)
    return best


def proportional_weights(obj, mesh, chosen, settings, view_direction=None):
    """{vertex index: weight} of the unselected points that follow the
    selected ones (``chosen``) with Proportional Editing: Blender's falloff
    over the world-space distance to the nearest selected point, along the
    surface with Connected Only, across the view (``view_direction``) with
    Projected from View."""
    size = settings.proportional_size
    falloff = _FALLOFFS.get(settings.proportional_edit_falloff, _FALLOFFS['SMOOTH'])
    if settings.proportional_edit_falloff == 'RANDOM':
        falloff = lambda t, index=None: t * random.Random(index).random()  # noqa: E731
    matrix = obj.matrix_world
    mesh.verts.ensure_lookup_table()
    world = [matrix @ v.co for v in mesh.verts]
    selected = set(chosen)
    distance = {}
    if settings.use_proportional_connected:
        # Twice the size: across a face a point can be nearer than both
        # points it is reached from.
        best = _connected_distances(mesh, world, chosen, 2.0 * size)
        distance = {i: d for i, d in best.items() if i not in selected and d <= size}
    else:
        from mathutils.kdtree import KDTree

        if settings.use_proportional_projected and view_direction is not None:
            toward = view_direction.normalized()
            world = [co - toward * co.dot(toward) for co in world]
        tree = KDTree(len(chosen))
        for n, i in enumerate(chosen):
            tree.insert(world[i], n)
        tree.balance()
        low = Vector([min(world[i][k] for i in chosen) - size for k in range(3)])
        high = Vector([max(world[i][k] for i in chosen) + size for k in range(3)])
        for v in mesh.verts:
            if v.index in selected or v.hide:
                continue
            co = world[v.index]
            if all(low[k] <= co[k] <= high[k] for k in range(3)):
                _, _, d = tree.find(co)
                if d <= size:
                    distance[v.index] = d
    weights = {}
    for i, d in distance.items():
        t = 1.0 - d / size if size > 0 else 0.0
        w = falloff(t, i) if settings.proportional_edit_falloff == 'RANDOM' else falloff(t)
        if w > 0.0:
            weights[i] = w
    return weights


def _turn_of(matrix):
    """(axis, angle, center, shift) when ``matrix`` turns about a line (a
    rotation, perhaps with a move along the line), else None. Numpy arrays."""
    rotation = np.array(matrix.to_3x3())
    if abs(np.linalg.det(rotation) - 1.0) > 1e-6 or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        return None
    axis, angle = matrix.to_3x3().to_quaternion().to_axis_angle()
    if abs(angle) < 1e-9:
        return None
    if angle > math.pi:
        axis, angle = -axis, 2 * math.pi - angle
    axis = np.array(axis)
    offset = np.array(matrix.translation)
    shift = float(axis @ offset)
    center = np.linalg.lstsq(np.eye(3) - rotation, offset - shift * axis, rcond=None)[0]
    return axis, angle, center, shift


def transform_points(matrix, points, weights, turn=None):
    """World-space ``points`` (N x 3) transformed by ``matrix``, each by its
    weight (0 to 1), as Blender's proportional editing does it: a turn turns
    them by the weight times its angle about the same axis (``turn``, see
    _turn_of, when the angle is known, e.g. typed); a move, scaling or
    mirroring takes them that part of the way."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    weights = np.asarray(weights, dtype=np.float64)
    full = points @ np.array(matrix.to_3x3()).T + np.array(matrix.translation)
    result = np.where((weights >= 1.0)[:, None], full, points)
    part = (weights > 0.0) & (weights < 1.0)
    if not part.any():
        return result
    w = weights[part]
    if turn is None:
        turn = _turn_of(matrix)
    if turn is None:
        result[part] = points[part] + (full[part] - points[part]) * w[:, None]
        return result
    axis, angle, center, shift = turn
    turned = (w * angle)[:, None]
    v = points[part] - center
    cos, sin = np.cos(turned), np.sin(turned)
    result[part] = (center + v * cos + np.cross(axis, v) * sin + np.outer(v @ axis, axis) * (1 - cos)
                    + np.outer(w * shift, axis))
    return result


def _without_selected_ancestors(objects):
    """Objects whose parents are not also in ``objects`` (moving a parent
    moves its children)."""
    chosen = set(objects)

    def has_chosen_ancestor(obj):
        parent = obj.parent
        while parent is not None:
            if parent in chosen:
                return True
            parent = parent.parent
        return False

    return [obj for obj in objects if not has_chosen_ancestor(obj)]


# Edit Mode modes whose selected points the commands transform.
EDIT_MODES = {'EDIT_MESH', 'EDIT_CURVE'}


def selected_curve_points(context):
    """[(object, [(spline, point, attributes)])] of the selected curve points
    in Edit Mode: a Bezier control point brings its handles along."""
    found = []
    # (One object for each curve: objects sharing their data are in Edit
    # Mode together, and its points would be moved once for each of them.)
    for obj in context.objects_in_mode_unique_data:
        if obj.type != 'CURVE':
            continue
        chosen = []
        for spline in obj.data.splines:
            if spline.type == 'BEZIER':
                for point in spline.bezier_points:
                    if point.hide:
                        continue
                    if point.select_control_point:
                        chosen.append((spline, point, ("co", "handle_left", "handle_right")))
                    else:
                        handles = tuple(name for name, picked in (("handle_left", point.select_left_handle),
                                                                  ("handle_right", point.select_right_handle))
                                        if picked)
                        if handles:
                            chosen.append((spline, point, handles))
            else:
                chosen += [(spline, point, ("co",)) for point in spline.points if point.select and not point.hide]
        if chosen:
            found.append((obj, chosen))
    return found


def curve_point_positions(obj, spline, point, attributes):
    """World positions of a curve point's transformed attributes."""
    matrix = obj.matrix_world
    if spline.type == 'BEZIER':
        return [matrix @ getattr(point, name) for name in attributes]
    return [matrix @ point.co.xyz]


def has_shear(matrix):
    """Whether an object matrix cannot be stored as location, rotation and
    scale (e.g. a rotated object scaled along a world axis)."""
    location, rotation, scale = matrix.decompose()
    rebuilt = Matrix.LocRotScale(location, rotation, scale)
    size = max(1.0, max(abs(s) for s in scale))
    return max(abs(a - b) for row, other in zip(matrix, rebuilt) for a, b in zip(row, other)) > 1e-6 * size


def set_world_matrix(obj, matrix):
    """Give an object a new world matrix. Blender keeps object transforms as
    location, rotation and scale; a shear would be lost (and a zero scale
    would leave a degenerate object), so it goes into the object's mesh or
    curve data instead (exact), the object keeping its transform."""
    flat = abs(matrix.to_3x3().determinant()) < 1e-12
    if not (flat or has_shear(matrix)) or obj.type not in {'MESH', 'CURVE'} or obj.data is None:
        obj.matrix_world = matrix
        return
    from . import polycurve

    delta = obj.matrix_world.inverted() @ matrix  # In the object's own space.
    carried = matrix @ obj.matrix_world.inverted()  # The same, in world space.
    children = [(child, child.matrix_world.copy()) for child in obj.children]
    if obj.data.users > 1:
        obj.data = obj.data.copy()  # Do not change other objects using it.
    obj.data.transform(delta)
    if obj.type == 'MESH' and delta.to_3x3().determinant() < -1e-9:
        obj.data.flip_normals()  # A mirror image is inside out.
    if obj.type == 'CURVE':
        polycurve.transform_tags(obj, delta)
    # The object's own transform stays, so its children would stay behind
    # (a parent's matrix carries them; this one did not change).
    for child, world in children:
        set_world_matrix(child, carried @ world)


def object_center(obj):
    """World center of an object's bounding box."""
    return obj.matrix_world @ (sum((Vector(corner) for corner in obj.bound_box), Vector()) / 8)


def _about(point, matrix):
    """``matrix`` (3x3 or 4x4) applied about ``point``."""
    return Matrix.Translation(point) @ matrix.to_4x4() @ Matrix.Translation(-point)


class _TransformObjects(GetPoints):
    """Base for the transform commands."""

    verb = "transform"
    # Whether unselected mesh points follow with Proportional Editing (not
    # for commands that set each point on its own, e.g. SetPt).
    proportional = True

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode in {'OBJECT'} | EDIT_MODES

    def invoke(self, context, event):
        self.selected = []
        self.objects = []
        self.points_to_move = []  # Mesh Edit Mode: (object, vertex indices).
        self.following = []  # Mesh Edit Mode, Proportional Editing: (object, {vertex index: weight}).
        self.turn = None  # A typed rotation (see _turn_of), so the points that follow turn as in Blender.
        self.curve_points = []  # Curve Edit Mode: see selected_curve_points.
        self.outline = np.empty((0, 3))
        self.moving = np.empty(0)  # How much each outline point moves (0 to 1).
        self.preview_matrix = None
        self.edit_kind = context.mode
        self.edit_mode = context.mode in EDIT_MODES
        return super().invoke(context, event)

    def needs_selection(self, context):
        if self.edit_kind == 'EDIT_CURVE':
            return not selected_curve_points(context)
        if self.edit_mode:
            return not any(v.select for obj in context.objects_in_mode if obj.type == 'MESH'
                           for v in bmesh.from_edit_mesh(obj.data).verts)
        return not context.selected_objects

    def selection_done(self, context):
        # The mode now: picking a face with Ctrl+Shift while the command
        # asks for objects goes into Edit Mode, and the face is what moves.
        self.edit_kind = context.mode
        self.edit_mode = context.mode in EDIT_MODES
        if self.edit_kind == 'EDIT_CURVE':
            return self._curve_points_selected(context)
        if self.edit_mode:
            return self._points_selected(context)
        selected = list(context.selected_objects)
        if not selected:
            command_line.echo(self.window, f"Select objects to {self.verb}", 'ERROR')
            return False
        self.selected = selected
        self.objects = _without_selected_ancestors(selected)
        depsgraph = context.evaluated_depsgraph_get()
        edges = [osnap.object_edges(obj, depsgraph) for obj in selected]
        self.outline = np.concatenate(edges).reshape(-1, 3) if edges else np.empty((0, 3))
        self.outline_ranges = []  # (start, end, object) of each object's edges in the outline.
        start = 0
        for obj, found in zip(selected, edges):
            end = start + len(found.reshape(-1, 3))
            self.outline_ranges.append((start, end, obj))
            start = end
        self.moving = np.ones(len(self.outline))
        self.preview_matrix = None
        return True

    def _points_selected(self, context):
        """Edit Mode: the selected points, and the edges they are on as the
        outline (only their selected ends move). With Proportional Editing
        the points around them follow, partly."""
        outline, moving = [], []
        settings = context.tool_settings
        use_proportional = self.proportional and settings.use_proportional_edit
        rv3d = getattr(context, "region_data", None)
        view_direction = (rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
                          if isinstance(rv3d, bpy.types.RegionView3D) else None)
        for obj in context.objects_in_mode_unique_data:  # Shared meshes once.
            if obj.type != 'MESH':
                continue
            mesh = bmesh.from_edit_mesh(obj.data)
            chosen = [v.index for v in mesh.verts if v.select]
            if not chosen:
                continue
            self.points_to_move.append((obj, chosen))
            weights = proportional_weights(obj, mesh, chosen, settings, view_direction) if use_proportional else {}
            if weights:
                self.following.append((obj, weights))
            matrix = obj.matrix_world
            for edge in mesh.edges:
                a, b = edge.verts
                if a.select or b.select or a.index in weights or b.index in weights:
                    for vertex in edge.verts:
                        outline.append(tuple(matrix @ vertex.co))
                        moving.append(1.0 if vertex.select else weights.get(vertex.index, 0.0))
        if not self.points_to_move:
            command_line.echo(self.window, f"Select points to {self.verb}", 'ERROR')
            return False
        self.outline = np.array(outline, dtype=np.float64).reshape(-1, 3)
        self.moving = np.array(moving, dtype=np.float64)
        self.preview_matrix = None
        return True

    def _curve_points_selected(self, context):
        """Curve Edit Mode: the selected points, and as the outline the
        control polygon and Bezier handle lines next to them."""
        self.curve_points = selected_curve_points(context)
        if not self.curve_points:
            command_line.echo(self.window, f"Select points to {self.verb}", 'ERROR')
            return False
        outline, moving = [], []

        def line(a, b):
            outline.extend((tuple(a[0]), tuple(b[0])))
            moving.extend((a[1], b[1]))

        for obj, chosen in self.curve_points:
            matrix = obj.matrix_world
            picked = {(point.as_pointer(), name) for _, point, names in chosen for name in names}

            def at(point, name):
                position = point.co.xyz if name == "co" and not hasattr(point, "handle_left") else getattr(point, name)
                return matrix @ position, (point.as_pointer(), name) in picked

            for spline in obj.data.splines:
                bezier = spline.type == 'BEZIER'
                points = list(spline.bezier_points if bezier else spline.points)
                if bezier:
                    for point in points:
                        for handle in ("handle_left", "handle_right"):
                            line(at(point, "co"), at(point, handle))
                pairs = list(zip(points[:-1], points[1:]))
                if spline.use_cyclic_u and len(points) > 2:
                    pairs.append((points[-1], points[0]))
                for a, b in pairs:
                    first, second = at(a, "co"), at(b, "co")
                    if first[1] or second[1]:
                        line(first, second)
        self.outline = np.array(outline, dtype=np.float64).reshape(-1, 3)
        self.moving = np.array(moving, dtype=bool)
        self.preview_matrix = None
        return True

    def prompt(self):
        if self.selecting:
            what = "points" if self.edit_mode else "objects"
            return f"Select {what} to {self.verb}. Press Enter when done"
        return self.prompts()[min(len(self.points), len(self.prompts()) - 1)]

    def prompts(self):
        raise NotImplementedError

    def matrix_to(self, point):
        """The transform if ``point`` were the next point, or None."""
        return None

    def after_track(self):
        self.preview_matrix = self.matrix_to(self.current) if self.current is not None else None

    def _outline(self, matrix):
        outline = self.outline.copy()
        if self.rigid_objects():
            for start, end, obj in getattr(self, "outline_ranges", ()):
                outline[start:end] += np.array(self.matrix_for(obj, matrix).translation)
            return outline.astype(np.float32)
        return transform_points(matrix, outline, self.moving).astype(np.float32)

    def rigid_objects(self):
        """Whether objects only move (Rigid=Yes): their centers are
        transformed, their shapes are not."""
        return False

    def matrix_for(self, obj, matrix):
        """The world transform ``matrix`` gives ``obj`` (only a translation of
        its center with Rigid=Yes)."""
        if not self.rigid_objects():
            return matrix
        center = object_center(obj)
        return Matrix.Translation(matrix @ center - center)

    def selection_center(self, context):
        """The center of the bounding box of what is transformed."""
        if self.edit_kind == 'EDIT_CURVE':
            points = [p for obj, chosen in self.curve_points for spline, point, names in chosen
                      for p in curve_point_positions(obj, spline, point, names)]
        elif self.edit_mode:
            points = []
            for obj, indices in self.points_to_move:
                mesh = bmesh.from_edit_mesh(obj.data)
                mesh.verts.ensure_lookup_table()
                points += [obj.matrix_world @ mesh.verts[i].co for i in indices]
        else:
            points = [obj.matrix_world @ Vector(corner) for obj in self.selected for corner in obj.bound_box]
        if not points:
            return None
        low = Vector([min(p[i] for p in points) for i in range(3)])
        high = Vector([max(p[i] for p in points) for i in range(3)])
        return (low + high) / 2

    def preview(self):
        lines = []
        if self.points and self.current is not None:
            lines.append(([self.points[0], self.current], 'guide'))
        if self.preview_matrix is not None and len(self.outline):
            lines.append((self._outline(self.preview_matrix), 'shape'))
        return lines

    def snap_geometry(self):
        return self.points, []

    def on_enter(self, context):
        return CANCEL

    def apply(self, matrix, copy=False, turn=None):
        """Transform what was selected by ``matrix`` (world space). ``turn``:
        the rotation it is, with its angle as given (see _turn_of)."""
        if self.edit_kind == 'EDIT_CURVE':
            for obj, chosen in self.curve_points:
                local = obj.matrix_world.inverted() @ matrix @ obj.matrix_world
                for spline, point, names in chosen:
                    if spline.type == 'BEZIER':
                        for name in names:
                            setattr(point, name, local @ getattr(point, name))
                    else:
                        point.co = (*(local @ point.co.xyz), point.co.w)
                obj.data.update_tag()
            return
        if self.edit_mode:
            following = {obj.as_pointer(): weights for obj, weights in self.following}
            mirrored = matrix.to_3x3().determinant() < 0
            for obj, indices in self.points_to_move:
                mesh = bmesh.from_edit_mesh(obj.data)
                mesh.verts.ensure_lookup_table()
                local = obj.matrix_world.inverted() @ matrix @ obj.matrix_world
                for index in indices:
                    mesh.verts[index].co = local @ mesh.verts[index].co
                if mirrored:
                    # A mirror image is inside out: turn the faces that
                    # moved as a whole (Mirror, a scale past zero).
                    moved = set(indices)
                    turned = [f for f in mesh.faces if all(v.index in moved for v in f.verts)]
                    if turned:
                        bmesh.ops.reverse_faces(mesh, faces=turned)
                weights = following.get(obj.as_pointer())
                if weights:
                    world, inverse = obj.matrix_world, obj.matrix_world.inverted()
                    chosen = list(weights)
                    moved = transform_points(matrix, [world @ mesh.verts[i].co for i in chosen],
                                             [weights[i] for i in chosen], turn)
                    for index, point in zip(chosen, moved):
                        mesh.verts[index].co = inverse @ Vector(point)
                mesh.normal_update()  # MoveFace and the coplanar pick read the faces' normals.
                bmesh.update_edit_mesh(obj.data)
            return
        if not copy:
            for obj in self.objects:
                set_world_matrix(obj, self.matrix_for(obj, matrix) @ obj.matrix_world)
            return
        copies = {}
        for obj in self.selected:
            duplicate = obj.copy()
            if obj.data is not None:
                duplicate.data = obj.data.copy()
            for collection in obj.users_collection:
                collection.objects.link(duplicate)
            copies[obj] = duplicate
        for obj, duplicate in copies.items():
            if obj.parent in copies:
                duplicate.parent = copies[obj.parent]
            duplicate.select_set(False)
        for obj in self.objects:
            set_world_matrix(copies[obj], self.matrix_for(obj, matrix) @ obj.matrix_world)


class _Move(_TransformObjects):
    """Shared by Move and Drag (registered operator classes cannot be
    subclassed: Blender would lose the parent's Python class)."""

    verb = "move"

    def prompts(self):
        return ["Point to move from", "Point to move to"]

    def matrix_to(self, point):
        return Matrix.Translation(point - self.points[0]) if len(self.points) == 1 else None

    def on_point(self, context):
        return FINISH if len(self.points) == 2 else None

    def create(self, context):
        self.apply(Matrix.Translation(self.points[1] - self.points[0]))


class R2B_OT_move(_Move, bpy.types.Operator):
    """Move objects from a base point to a target point"""

    bl_idname = "r2b.move"
    bl_label = "Move"


class R2B_OT_drag_move(_Move, bpy.types.Operator):
    """Drag the selected objects: from the point grabbed (snapped if an
    object snap is close to it) to where the button is released (snapped)"""

    bl_idname = "r2b.drag_move"
    bl_label = "Drag"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}
    pick_on_release = True

    def invoke(self, context, event):
        result = super().invoke(context, event)
        if 'RUNNING_MODAL' not in result:
            return result
        region, rv3d = context.region, context.region_data
        pressed = (event.mouse_prev_press_x - region.x, event.mouse_prev_press_y - region.y)
        snapped = self._osnap(context, region, rv3d, pressed, event.alt)
        self.points = [snapped[0] if snapped else cplane.point_under_mouse(region, rv3d, self.plane, pressed)]
        self._show_prompt()
        self._track(context, event)
        return result


class R2B_OT_soft_move(_Move, bpy.types.Operator):
    """Move mesh points with the points around them following (Rhino's
    SoftMove): Move with Blender's Proportional Editing on for this command,
    within Radius (Blender's proportional size)"""

    bl_idname = "r2b.soft_move"
    bl_label = "SoftMove"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'EDIT_MESH'

    def invoke(self, context, event):
        tool = context.tool_settings
        self.turned_on = not tool.use_proportional_edit
        tool.use_proportional_edit = True
        self.asking_radius = False
        running = False
        try:
            result = super().invoke(context, event)
            running = 'RUNNING_MODAL' in result
            return result
        finally:
            if not running and self.turned_on:
                tool.use_proportional_edit = False  # Did not start (or was done at once).

    def prompt(self):
        size = bpy.context.tool_settings.proportional_size
        if self.asking_radius:
            return f"Radius <{size:g}>"
        if self.selecting:
            return super().prompt()
        return super().prompt() + f" ( Radius={size:g} )"

    def on_option(self, context, name):
        if "radius".startswith(name):
            self.asking_radius = True
            return True
        return False

    def on_text(self, context, text):
        if not self.asking_radius:
            return None
        self.asking_radius = False
        if text:
            try:
                radius = float(text)
            except ValueError:
                command_line.echo(self.window, "Type a radius", 'ERROR')
                return True
            if radius <= 0:
                command_line.echo(self.window, "The radius must be more than 0", 'ERROR')
                return True
            context.tool_settings.proportional_size = radius
            # The points that follow, again for the new radius.
            self.points_to_move, self.following = [], []
            self._points_selected(context)
        return True

    def _release(self):
        running = getpoint._active is self
        super()._release()
        if running and self.turned_on:
            bpy.context.tool_settings.use_proportional_edit = False


class R2B_OT_move_face(_Move, bpy.types.Operator):
    """Move faces picked with Ctrl+Shift (the faces around stretch); with
    DirectionConstraint=Normal along the first face's normal"""

    bl_idname = "r2b.move_face"
    bl_label = "MoveFace"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'EDIT_MESH'

    def invoke(self, context, event):
        self.along_normal = False
        self.face_normal = None
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not any(f.select for obj in context.objects_in_mode if obj.type == 'MESH'
                       for f in bmesh.from_edit_mesh(obj.data).faces)

    def selection_done(self, context):
        for obj in context.objects_in_mode:
            if obj.type != 'MESH':
                continue
            face = next((f for f in bmesh.from_edit_mesh(obj.data).faces if f.select), None)
            if face is not None:
                # (The inverse transpose: square to the face under an uneven scale too.)
                self.face_normal = (obj.matrix_world.to_3x3().inverted_safe().transposed() @ face.normal).normalized()
                break
        if self.face_normal is None:
            command_line.echo(self.window, "Select faces (Ctrl+Shift+click)", 'ERROR')
            return False
        return super().selection_done(context)

    def prompt(self):
        if self.selecting:
            return "Select faces to move (Ctrl+Shift+click). Press Enter when done"
        constraint = "Normal" if self.along_normal else "None"
        return super().prompt() + f" ( DirectionConstraint={constraint} )"

    def on_option(self, context, name):
        if "directionconstraint".startswith(name):
            self.along_normal = not self.along_normal
            return True
        return False

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if not (self.along_normal and len(self.points) == 1 and self.face_normal is not None):
            return point
        from . import solids

        return solids.elevator(context, SimpleNamespace(normal=self.face_normal), self.points[0], region, rv3d,
                               mouse, point, snapped)[0]

    def on_point(self, context):
        if self.along_normal and len(self.points) == 2:
            start = self.points[0]
            self.points[1] = start + self.face_normal * (self.points[1] - start).dot(self.face_normal)
        return super().on_point(context)


class R2B_OT_copy(_TransformObjects, bpy.types.Operator):
    """Copy objects from a base point to one or more target points"""

    bl_idname = "r2b.copy"
    bl_label = "Copy"
    verb = "copy"

    def prompts(self):
        return ["Point to copy from", "Point to copy to. Press Enter when done"]

    def matrix_to(self, point):
        return Matrix.Translation(point - self.points[0]) if self.points else None

    def preview(self):
        lines = super().preview()
        for target in self.points[1:]:
            lines.append((self._outline(Matrix.Translation(target - self.points[0])), 'shape'))
        return lines

    def on_enter(self, context):
        return FINISH if len(self.points) >= 2 else CANCEL

    def create(self, context):
        for target in self.points[1:]:
            self.apply(Matrix.Translation(target - self.points[0]), copy=True)


class _AboutCenter(_TransformObjects):
    """Rotate: a center, then a number or two reference points."""

    def on_point(self, context):
        if len(self.points) == 1:
            self.center_plane = self.plane  # The CPlane where the center was picked.
        if len(self.points) == 3:
            matrix = self.matrix_to(self.points.pop())
            if matrix is None:
                command_line.echo(self.window, "The reference points must differ from the center", 'ERROR')
                return None
            self.result = matrix
            self.turn = None
            return FINISH
        return None

    def preview(self):
        lines = super().preview()
        if len(self.points) == 2 and self.current is not None:
            lines.append(([self.points[1], self.points[0], self.current], 'guide'))
        return lines

    def create(self, context):
        self.apply(self.result, turn=self.turn)


class R2B_OT_rotate(_AboutCenter, bpy.types.Operator):
    """Rotate objects about a center, in the CPlane"""

    bl_idname = "r2b.rotate"
    bl_label = "Rotate"
    verb = "rotate"

    def prompts(self):
        return ["Center of rotation", "Angle or first reference point", "Second reference point"]

    def _angle_of(self, point):
        local = self.center_plane.to_local(point) - self.center_plane.to_local(self.points[0])
        if math.hypot(local.x, local.y) < TOLERANCE:
            return None
        return math.atan2(local.y, local.x)

    def _rotation(self, angle):
        return _about(self.points[0], Matrix.Rotation(angle, 4, self.center_plane.normal))

    def matrix_to(self, point):
        if len(self.points) != 2:
            return None
        start, end = self._angle_of(self.points[1]), self._angle_of(point)
        if start is None or end is None:
            return None
        return self._rotation(end - start)

    def on_number(self, context, value):
        if len(self.points) != 1:
            return None
        angle = math.radians(value)
        self.result = self._rotation(angle)
        self.turn = (np.array(self.center_plane.normal.normalized()), angle, np.array(self.points[0]), 0.0)
        return FINISH


# Whether the note on uneven object scale was shown (once per session).
_uneven_note_shown = False


def note_uneven_scale(window, objects):
    """Once: a scaling left an object scaled unevenly, as a Blender object
    scale (Rhino has none, it scales the geometry): say what that means and
    how to make it geometry (found reproducing a tutorial: after Scale1D the
    terrain had scale 5, 5, 2.5 and sculpting warned about it)."""
    global _uneven_note_shown
    if _uneven_note_shown:
        return False
    for obj in objects:
        scale = obj.scale
        if obj.type in {'MESH', 'CURVE'} and not math.isclose(min(scale), max(scale), rel_tol=1e-6):
            _uneven_note_shown = True
            command_line.echo(window, f"{obj.name} is now scaled unevenly ({scale.x:g}, {scale.y:g}, {scale.z:g}), "
                              "as a Blender object scale: sculpting, modifiers and particles work best after "
                              "ApplyScale, which puts the scale into the geometry, as Rhino does.", 'INFO')
            return True
    return False


class _Scaling(_TransformObjects):
    """Scale, Scale1D, Scale2D and ScaleNU: an origin (Enter: the center of
    the selection), then a scale factor or two reference points. Options:
    Copy (objects get scaled copies) and Rigid (objects only move with the
    scaling, their shapes stay)."""

    verb = "scale"
    allow_zero = True  # A zero factor flattens (Rhino 8); not for uniform 3D scaling.
    last_factor = 1.0  # Enter at the factor prompt uses the last factor given.

    def invoke(self, context, event):
        self.copy = False
        self.rigid = False
        self.factor = None  # A typed factor still waiting for its direction (Scale1D).
        self.scale_plane = None  # The CPlane where the factor or first reference point was given.
        return super().invoke(context, event)

    def rigid_objects(self):
        return self.rigid and not self.edit_mode

    def option_text(self):
        if self.edit_mode:
            return ""
        return f" ( Copy={'Yes' if self.copy else 'No'} Rigid={'Yes' if self.rigid else 'No'} )"

    def prompt(self):
        if self.selecting:
            return super().prompt()
        return self.step_prompt() + self.option_text()

    def step_prompt(self):
        if not self.points:
            return "Origin point. Press Enter for automatic"
        if len(self.points) == 1:
            return f"Scale factor or first reference point <{type(self).last_factor:.3f}>"
        return "Second reference point"

    def on_option(self, context, name):
        if self.edit_mode:
            return False
        if "copy".startswith(name):
            self.copy = not self.copy
            return True
        if "rigid".startswith(name):
            self.rigid = not self.rigid
            return True
        return False

    def on_enter(self, context):
        if not self.points:
            center = self.selection_center(context)
            if center is None:
                return CANCEL
            command_line.echo(self.window, "Origin point: center of the selection", 'INPUT')
            self.points.append(center)
            self.on_point(context)
            self._show_prompt()
            return None
        if len(self.points) == 1 and self.factor is None:
            result = self.use_factor(context, type(self).last_factor)
            if result is True:
                self._show_prompt()
                return None
            return result
        return CANCEL

    def on_point(self, context):
        if len(self.points) == 1:
            self.center_plane = self.plane  # The CPlane where the origin was picked.
            return None
        if len(self.points) == 2 and self.scale_plane is None:
            self.scale_plane = self.plane
        return self.on_reference(context)

    def on_reference(self, context):
        """A reference point was added (``self.points[1:]``)."""
        if len(self.points) < 3:
            if self.reference_length(self.points[1]) is None:
                self.points.pop()
                command_line.echo(self.window, "The reference point must differ from the origin", 'ERROR')
            return None
        matrix = self.matrix_to(self.points.pop())
        if matrix is None:
            command_line.echo(self.window, "The second reference point must differ from the origin", 'ERROR')
            return None
        self.result = matrix
        return FINISH

    def reference_length(self, point):
        """How long ``point`` is from the origin, as the scaling measures it
        (None if too short to scale from)."""
        length = (point - self.points[0]).length
        return length if length > TOLERANCE else None

    def measure(self, point):
        """The length of ``point`` from the origin to compare with the first
        reference point's (may be zero)."""
        return (point - self.points[0]).length

    def scaling(self, factor):
        """The scaling matrix about the origin for ``factor``."""
        raise NotImplementedError

    def matrix_to(self, point):
        if len(self.points) != 2:
            return None
        reference = self.reference_length(self.points[1])
        if reference is None:
            return None
        factor = self.measure(point) / reference
        if factor < 0 or (factor < TOLERANCE and not self.allow_zero):
            return None
        return self.scaling(factor)

    def check_factor(self, factor):
        if factor < 0 or (factor == 0 and not self.allow_zero):
            command_line.echo(self.window, "The scale must be more than zero" if not self.allow_zero
                              else "The scale must not be negative", 'ERROR')
            return False
        return True

    def use_factor(self, context, factor):
        """A factor was given at the factor prompt."""
        if self.scale_plane is None:
            self.scale_plane = self.plane
        type(self).last_factor = factor
        self.result = self.scaling(factor)
        return FINISH

    def on_number(self, context, value):
        if not self.points:
            return None
        if not self.check_factor(value):
            return True
        if len(self.points) == 1:
            return self.use_factor(context, value)
        reference = self.reference_length(self.points[1])  # A length for the second reference point.
        if reference is None:
            return None
        self.result = self.scaling(value / reference)
        return FINISH

    def preview(self):
        lines = super().preview()
        if len(self.points) == 2 and self.current is not None:
            lines.append(([self.points[1], self.points[0], self.current], 'guide'))
        return lines

    def create(self, context):
        self.apply(self.result, copy=self.copy and not self.edit_mode)
        if not self.edit_mode and not self.copy and not self.rigid_objects():
            note_uneven_scale(self.window, self.objects)


class R2B_OT_scale(_Scaling, bpy.types.Operator):
    """Scale objects about an origin point, the same in all directions"""

    bl_idname = "r2b.scale"
    bl_label = "Scale"
    allow_zero = False

    def scaling(self, factor):
        return _about(self.points[0], Matrix.Diagonal((factor, factor, factor)))


def _directional(direction, factor):
    """Scaling by ``factor`` along a unit ``direction`` (3x3)."""
    return Matrix.Identity(3) + (factor - 1) * Matrix([[a * b for b in direction] for a in direction])


class R2B_OT_scale1d(_Scaling, bpy.types.Operator):
    """Scale objects in one direction: a factor and a direction, or two
    reference points"""

    bl_idname = "r2b.scale1d"
    bl_label = "Scale1D"

    def step_prompt(self):
        if self.factor is not None:
            return "Scale direction"
        return super().step_prompt()

    def direction(self):
        """The unit direction of the scaling: to the first reference point."""
        vector = self.points[1] - self.points[0]
        return vector.normalized() if vector.length > TOLERANCE else None

    def measure(self, point):
        return (point - self.points[0]).dot(self.direction())

    def scaling(self, factor, direction=None):
        direction = direction or self.direction()
        return _about(self.points[0], _directional(direction, factor))

    def use_factor(self, context, factor):
        type(self).last_factor = factor
        self.factor = factor  # Then the direction.
        return True

    def on_enter(self, context):
        if self.factor is not None:
            return CANCEL
        return super().on_enter(context)

    def on_point(self, context):
        if self.factor is not None and len(self.points) == 2:
            direction = self.direction()
            self.points.pop()
            if direction is None:
                command_line.echo(self.window, "The direction must differ from the origin", 'ERROR')
                return None
            self.result = self.scaling(self.factor, direction)
            return FINISH
        return super().on_point(context)

    def matrix_to(self, point):
        if self.factor is not None and len(self.points) == 1:
            vector = point - self.points[0]
            return self.scaling(self.factor, vector.normalized()) if vector.length > TOLERANCE else None
        return super().matrix_to(point)


def _flatten_onto(normal):
    """Projection onto the plane with ``normal`` (3x3)."""
    return Matrix.Identity(3) - Matrix([[a * b for b in normal] for a in normal])


class R2B_OT_scale2d(_Scaling, bpy.types.Operator):
    """Scale objects in the construction plane, the same in both of its
    directions"""

    bl_idname = "r2b.scale2d"
    bl_label = "Scale2D"

    def _in_plane(self, vector):
        normal = (self.scale_plane or self.plane).normal
        return vector - normal * vector.dot(normal)

    def reference_length(self, point):
        length = self._in_plane(point - self.points[0]).length
        return length if length > TOLERANCE else None

    def measure(self, point):
        return self._in_plane(point - self.points[0]).length

    def scaling(self, factor):
        # Scale by factor everywhere, then undo it along the CPlane normal.
        normal = (self.scale_plane or self.plane).normal
        return _about(self.points[0], _directional(normal, 1 / factor) @ Matrix.Diagonal((factor,) * 3)
                      if factor > TOLERANCE else _flatten_onto(normal))


class R2B_OT_scale_nu(_Scaling, bpy.types.Operator):
    """Scale objects by different factors along the construction plane's x,
    y and z (or the world's)"""

    bl_idname = "r2b.scale_nu"
    bl_label = "ScaleNU"

    def invoke(self, context, event):
        self.factors = []  # Given so far, for x, y, z.
        self.world = False
        return super().invoke(context, event)

    def option_text(self):
        if self.edit_mode:
            return f" ( WorldCoordinates={'Yes' if self.world else 'No'} )"
        return super().option_text()[:-2] + f" WorldCoordinates={'Yes' if self.world else 'No'} )"

    def on_option(self, context, name):
        if "worldcoordinates".startswith(name):
            self.world = not self.world
            return True
        return super().on_option(context, name)

    def axes(self):
        if self.world:
            return Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))
        plane = getattr(self, "center_plane", self.plane)
        return plane.x_axis, plane.y_axis, plane.normal

    def axis(self):
        return self.axes()[len(self.factors)]

    def step_prompt(self):
        if not self.points:
            return super().step_prompt()
        name = "XYZ"[len(self.factors)]
        if len(self.points) == 1:
            return f"{name} scale factor or first reference point <1.000>"
        return f"{name}: second reference point"

    def reference_length(self, point):
        length = (point - self.points[0]).dot(self.axis())
        return length if abs(length) > TOLERANCE else None

    def measure(self, point):
        return (point - self.points[0]).dot(self.axis())

    def scaling(self, factor):
        factors = (self.factors + [factor] + [1.0, 1.0, 1.0])[:3]
        matrix = Matrix(((0, 0, 0), (0, 0, 0), (0, 0, 0)))
        for axis, f in zip(self.axes(), factors):
            matrix += f * Matrix([[a * b for b in axis] for a in axis])
        return _about(self.points[0], matrix)

    def matrix_to(self, point):
        if len(self.points) != 2:
            return None
        reference = self.reference_length(self.points[1])
        if reference is None:
            return None
        factor = self.measure(point) / reference
        return self.scaling(factor) if factor >= 0 else None

    def next_axis(self, factor):
        self.factors.append(factor)
        del self.points[1:]
        if len(self.factors) < 3:
            return True
        self.result = self.scaling(self.factors.pop())
        return FINISH

    def use_factor(self, context, factor):
        return self.next_axis(factor)

    def on_enter(self, context):
        if not self.points:
            return super().on_enter(context)
        if len(self.points) == 1:
            result = self.next_axis(1.0)
            if result == FINISH:
                return FINISH
            self._show_prompt()
            return None
        return CANCEL

    def on_reference(self, context):
        if len(self.points) < 3:
            if self.reference_length(self.points[1]) is None:
                self.points.pop()
                command_line.echo(self.window, f"The reference point must be off the origin along "
                                               f"{'XYZ'[len(self.factors)]}", 'ERROR')
            return None
        point = self.points.pop()
        factor = self.measure(point) / self.reference_length(self.points[1])
        if factor < 0:
            command_line.echo(self.window, "The scale must not be negative", 'ERROR')
            return None
        return self.next_axis(factor)

    def on_number(self, context, value):
        if not self.points:
            return None
        if not self.check_factor(value):
            return True
        if len(self.points) == 1:
            return self.next_axis(value)
        reference = self.reference_length(self.points[1])  # A length for the second reference point.
        return self.next_axis(value / abs(reference)) if reference is not None else None


class R2B_OT_mirror(_TransformObjects, bpy.types.Operator):
    """Mirror objects across a plane through a line and the CPlane normal"""

    bl_idname = "r2b.mirror"
    bl_label = "Mirror"
    verb = "mirror"

    def invoke(self, context, event):
        self.copy = context.mode == 'OBJECT'  # Rhino's default: Copy=Yes (points mirror in place).
        return super().invoke(context, event)

    def prompts(self):
        if self.edit_mode:
            return ["Start of mirror plane", "End of mirror plane"]
        copy = "Yes" if self.copy else "No"
        return [f"Start of mirror plane ( Copy={copy} )", f"End of mirror plane ( Copy={copy} )"]

    def matrix_to(self, point):
        if len(self.points) != 1:
            return None
        normal = (point - self.points[0]).cross(self.mirror_plane.normal)
        if normal.length < TOLERANCE:
            return None
        normal.normalize()
        reflection = Matrix.Identity(3) - 2 * Matrix([[a * b for b in normal] for a in normal])
        return _about(self.points[0], reflection)

    def on_point(self, context):
        if len(self.points) == 1:
            self.mirror_plane = self.plane  # The CPlane where the line starts.
        if len(self.points) == 2:
            matrix = self.matrix_to(self.points.pop())
            if matrix is None:
                command_line.echo(self.window, "The end of the mirror plane must differ from its start", 'ERROR')
                return None
            self.result = matrix
            return FINISH
        return None

    def on_option(self, context, name):
        if not self.edit_mode and "copy".startswith(name):
            self.copy = not self.copy
            return True
        return False

    def create(self, context):
        self.apply(self.result, copy=self.copy)


_classes = (R2B_OT_move, R2B_OT_soft_move, R2B_OT_drag_move, R2B_OT_move_face, R2B_OT_copy, R2B_OT_rotate, R2B_OT_scale, R2B_OT_scale1d,
            R2B_OT_scale2d, R2B_OT_scale_nu, R2B_OT_mirror)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
