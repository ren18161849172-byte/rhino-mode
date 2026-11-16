# SPDX-License-Identifier: GPL-3.0-or-later
"""The CPlane command and the grid of custom CPlanes (MODE feature).

CPlane sets the construction plane of the active viewport (the one last
typed or worked in), or of all viewports with the All option: a picked or
typed point moves the origin, 3Point sets origin, x axis direction and
orientation, Elevation moves the plane along its normal (a distance or the
height of a point), Object aligns it with the selected planar curve or mesh,
View makes it face the view, World goes back to a world plane (the view's
own with Enter, or Top, Bottom, Front, Back, Right, Left), Undo to the plane
before. A viewport with its own plane shows that plane's grid and axes
instead of Blender's grid.
"""

import math

import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

import bpy
from mathutils import Matrix, Vector

from . import command_line, cplane, osnap, settings
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-9
GRID_LINES = 20  # Lines on each side of the origin.
MAJOR_EVERY = 5

# Earlier CPlanes of each viewport, for Undo: (screen, area index) -> [CPlane or None].
_history = {}
_draw_handle = None


def _viewport_key(screen, area):
    return screen.as_pointer(), cplane._area_index(screen, area)


def plane_of_object(obj, depsgraph):
    """The plane of a planar curve (a circle's or arc's own) or of a mesh's
    first face, or None."""
    if obj.type == 'CURVE':
        arcs = osnap._arcs_of(obj)
        if arcs:
            arc = arcs[0]
            return cplane.CPlane("Object", arc.center, arc.x_axis, arc.y_axis)
        spline = obj.data.splines[0] if obj.data.splines else None
        points = ([obj.matrix_world @ p.co for p in spline.bezier_points] if spline and spline.type == 'BEZIER'
                  else [obj.matrix_world @ p.co.xyz for p in spline.points] if spline else [])
        if len(points) < 3:
            return None
        normal = Vector()
        for a, b in zip(points, points[1:] + points[:1]):  # Newell's method.
            normal += Vector(((a.y - b.y) * (a.z + b.z), (a.z - b.z) * (a.x + b.x), (a.x - b.x) * (a.y + b.y)))
        if normal.length < TOLERANCE:
            return None
        normal.normalize()
        center = sum(points, Vector()) / len(points)
        if max(abs((p - center).dot(normal)) for p in points) > 1e-6 * max((p - center).length for p in points):
            return None  # Not planar.
        x_axis = (points[1] - points[0]) - normal * (points[1] - points[0]).dot(normal)
        if x_axis.length < TOLERANCE:
            return None
        x_axis.normalize()
        return cplane.CPlane("Object", center, x_axis, normal.cross(x_axis))
    if obj.type == 'MESH':
        mesh = obj.evaluated_get(depsgraph).data
        if not len(mesh.polygons):
            return None
        face = mesh.polygons[0]
        matrix = obj.matrix_world
        normal = (matrix.to_3x3().inverted_safe().transposed() @ face.normal).normalized()
        corners = [matrix @ mesh.vertices[i].co for i in face.vertices]
        x_axis = (corners[1] - corners[0]).normalized()
        return cplane.CPlane("Object", matrix @ face.center, x_axis, normal.cross(x_axis))
    return None


class R2B_OT_cplane(GetPoints, bpy.types.Operator):
    """Set the construction plane of the active viewport"""

    bl_idname = "r2b.cplane"
    bl_label = "CPlane"

    @classmethod
    def poll(cls, context):
        return super().poll(context) and context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        self.how = 'ORIGIN'  # Or '3POINT', 'VIEW', 'ELEVATION', 'ROTATE'.
        self.axis = 'Z'  # Rotate: the CPlane axis to turn about.
        self.asking_world = False
        self.all = False
        self.result = None
        self.screen = context.window.screen
        self.area = context.area
        return super().invoke(context, event)

    def prompt(self):
        if self.asking_world:
            return "World plane ( Top Bottom Front Back Right Left ), Enter for the view's own"
        if self.how == 'VIEW':
            return "CPlane origin (the plane faces the view)"
        if self.how == 'ELEVATION':
            return "CPlane elevation (a distance, or a point at the new height)"
        if self.how == '3POINT':
            return ("CPlane origin", "X axis direction", "CPlane orientation")[len(self.points)]
        if self.how == 'ROTATE':
            if self.points:
                return "Second reference point"
            return f"Angle or first reference point ( Axis={self.axis} )"
        return f"CPlane origin ( All={'Yes' if self.all else 'No'} 3Point Elevation Object Rotate View World Undo )"

    def _areas(self):
        """The viewports to set: the active one, or all with All=Yes."""
        if self.all:
            return [a for a in self.screen.areas if a.type == 'VIEW_3D']
        return [self.area]

    def _three_point(self):
        if len(self.points) == 2 and (self.points[1] - self.points[0]).length < TOLERANCE:
            self.points.pop()
            command_line.echo(self.window, "The x axis needs a direction", 'ERROR')
        elif len(self.points) == 3:
            origin, along, toward = self.points
            x_axis = (along - origin).normalized()
            normal = x_axis.cross(toward - origin)
            if normal.length < TOLERANCE:
                self.points.pop()
                command_line.echo(self.window, "The third point must not be on the x axis", 'ERROR')
                return None
            normal.normalize()
            self.result = cplane.CPlane("Custom", origin, x_axis, normal.cross(x_axis))
            return FINISH
        return None

    def on_option(self, context, name):
        if self.asking_world:
            if name in cplane.WORLD_PLANES:
                self.result = cplane.WORLD_PLANES[name]
                return FINISH
            return False
        if self.how == 'ROTATE' and not self.points:
            if "axis".startswith(name):
                self.axis = {'X': 'Y', 'Y': 'Z', 'Z': 'X'}[self.axis]
                return True
            if name in ("x", "y", "z"):
                self.axis = name.upper()
                return True
            return False
        if self.points:
            return False
        if "all".startswith(name):
            self.all = not self.all
            return True
        if "3point".startswith(name):
            self.how = '3POINT'
            return True
        if "elevation".startswith(name):
            self.how = 'ELEVATION'
            return True
        if "view".startswith(name):
            self.how = 'VIEW'
            return True
        if "rotate".startswith(name):
            self.how = 'ROTATE'
            return True
        if "object".startswith(name):
            selected = list(context.selected_objects)
            active = context.view_layer.objects.active
            obj = active if active in selected else (selected[0] if selected else None)
            plane = plane_of_object(obj, context.evaluated_depsgraph_get()) if obj else None
            if plane is None:
                command_line.echo(self.window, "Select a planar curve or a mesh first", 'ERROR')
                return True
            self.result = plane
            return FINISH
        if "world".startswith(name):
            self.asking_world = True
            return True
        if "undo".startswith(name):
            if not any(_history.get(_viewport_key(self.screen, area)) for area in self._areas()):
                command_line.echo(self.window, "No earlier CPlane", 'ERROR')
                return True
            self.result = 'UNDO'
            return FINISH
        return False

    def on_enter(self, context):
        if self.asking_world:
            self.result = 'WORLD'  # Each viewport's own world plane.
            return FINISH
        return CANCEL

    def on_number(self, context, value):
        if self.how == 'ROTATE':
            self.result = self._rotated(math.radians(value))
            return FINISH
        if self.how != 'ELEVATION':
            return None
        self.result = self._elevated(value)
        return FINISH

    def _rotation_axis(self):
        current = cplane.for_area(self.screen, self.area)
        return current, {'X': current.x_axis, 'Y': current.y_axis, 'Z': current.normal}[self.axis]

    def _rotated(self, angle):
        """The CPlane turned by ``angle`` (radians) about its own axis."""
        current, axis = self._rotation_axis()
        turn = Matrix.Rotation(angle, 3, axis)
        return cplane.CPlane("Custom", current.origin, turn @ current.x_axis, turn @ current.y_axis)

    def _reference_angle(self, point):
        """Angle of a point about the rotation axis through the origin, in a
        plane across the axis; None on the axis."""
        current, axis = self._rotation_axis()
        offset = point - current.origin
        offset -= axis * offset.dot(axis)
        if offset.length < TOLERANCE:
            return None
        u = axis.orthogonal().normalized()
        return math.atan2(axis.cross(u).dot(offset), u.dot(offset))

    def _elevated(self, elevation):
        current = cplane.for_area(self.screen, self.area)
        return cplane.CPlane("Custom", current.origin + current.normal * elevation, current.x_axis, current.y_axis)

    def preview(self):
        if self.how == '3POINT' and self.points and self.current is not None:
            return [([self.points[0], self.current], 'guide')]
        if self.how == 'ROTATE' and self.points and self.current is not None:
            origin = cplane.for_area(self.screen, self.area).origin
            return [([self.points[0], origin, self.current], 'guide')]
        return []

    def snap_geometry(self):
        return self.points, []

    def on_point(self, context):
        current = cplane.for_area(self.screen, self.area)
        if self.how == '3POINT':
            return self._three_point()
        if self.how == 'ROTATE':
            if self._reference_angle(self.points[-1]) is None:
                self.points.pop()
                command_line.echo(self.window, "The reference point must not be on the rotation axis", 'ERROR')
                return None
            if len(self.points) < 2:
                return None
            first, second = (self._reference_angle(p) for p in self.points)
            self.result = self._rotated(second - first)
            return FINISH
        if self.how == 'ELEVATION':
            self.result = self._elevated(current.to_local(self.points[0]).z)
        elif self.how == 'VIEW':
            rotation = self.area.spaces.active.region_3d.view_rotation
            self.result = cplane.CPlane("View", self.points[0], rotation @ Vector((1, 0, 0)),
                                        rotation @ Vector((0, 1, 0)))
        else:
            self.result = cplane.CPlane("Custom", self.points[0], current.x_axis, current.y_axis)
        return FINISH

    def create(self, context):
        for area in self._areas():
            history = _history.setdefault(_viewport_key(self.screen, area), [])
            if self.result == 'UNDO':
                if not history:
                    continue
                plane = history.pop()
            else:
                history.append(cplane.custom(self.screen, area))
                plane = None if self.result == 'WORLD' else self.result
                if plane is not None and plane == cplane.for_view(area.spaces.active.region_3d):
                    plane = None  # The view's own world plane.
            if plane is None:
                cplane.clear_custom(self.screen, area)
            else:
                cplane.set_custom(self.screen, area, plane)
            area.tag_redraw()
        shown = cplane.for_area(self.screen, self.area)
        where = "all viewports" if self.all else "the active viewport"
        command_line.echo(self.window, f"CPlane of {where}: {shown.name}, origin {tuple(round(c, 4) for c in shown.origin)}")


# --- Grid of custom CPlanes --------------------------------------------------------

def _grid_lines(plane, spacing):
    """(minor, major, x axis, y axis) line segments as N x 3 arrays."""
    extent = GRID_LINES * spacing
    origin, x_axis, y_axis = (np.array(tuple(v)) for v in (plane.origin, plane.x_axis, plane.y_axis))
    minor, major = [], []
    for i in range(-GRID_LINES, GRID_LINES + 1):
        if i == 0:
            continue
        lines = major if i % MAJOR_EVERY == 0 else minor
        offset = i * spacing
        lines += [origin + x_axis * offset - y_axis * extent, origin + x_axis * offset + y_axis * extent,
                  origin + y_axis * offset - x_axis * extent, origin + y_axis * offset + x_axis * extent]
    x_line = [origin - x_axis * extent, origin + x_axis * extent]
    y_line = [origin - y_axis * extent, origin + y_axis * extent]
    return [np.array(lines, dtype=np.float32).reshape(-1, 3) for lines in (minor, major, x_line, y_line)]


def _draw_grid():
    context = bpy.context
    if context.area is None or context.screen is None:
        return
    plane = cplane.custom(context.screen, context.area)
    if plane is None:
        return
    region = context.region
    minor, major, x_line, y_line = _grid_lines(plane, settings.get(context).snap_spacing)
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (region.width, region.height))
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    scale = context.preferences.system.ui_scale
    for lines, color, width in ((minor, (0.5, 0.5, 0.5, 0.25), 1.0), (major, (0.6, 0.6, 0.6, 0.45), 1.0),
                                (x_line, (0.85, 0.25, 0.25, 0.9), 1.5), (y_line, (0.3, 0.75, 0.3, 0.9), 1.5)):
        if len(lines):
            shader.uniform_float("color", color)
            shader.uniform_float("lineWidth", width * scale)
            batch_for_shader(shader, 'LINES', {"pos": lines}).draw(shader)
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('NONE')


def register():
    global _draw_handle
    bpy.utils.register_class(R2B_OT_cplane)
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_grid, (), 'WINDOW', 'POST_VIEW')


def unregister():
    global _draw_handle
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _history.clear()
    bpy.utils.unregister_class(R2B_OT_cplane)
