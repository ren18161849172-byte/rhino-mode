# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's Gumball (MODE feature): handles on the selection, aligned to the
viewport's CPlane, to move along an axis (arrows), rotate about one (arcs),
scale along one (boxes; Shift while dragging scales in all directions), move
in one of the planes of two axes (squares; Shift+drag scales in that plane,
Rhino's Scale2D) and move freely in the CPlane (the center).

Dragging a handle picks points like the transform commands (getpoint.py):
the selection follows the mouse along the axis, and when Osnap finds a
point, the gumball's center goes to that point (projected onto the axis),
as Rhino's snappy dragging. Clicking a handle without dragging asks for a
distance, angle or scale factor in the command line instead.

Shown with the Gumball toggle in the Rhino workspace, on the selected
objects (or the selected points in mesh or curve Edit Mode), at the center of
their bounding box. Its axes follow the CPlane, the active object's axes or
the world (Gumball alignment in the status bar, GumballAlignment command).

* Ctrl+click the center: relocate the gumball (pick a new origin, then
  optionally an x direction) without moving anything; it stays there until
  the selection changes, and moves along with what it transforms.
* Ctrl+drag an arrow (or Ctrl+click it and type a distance): extrude. In
  mesh Edit Mode the selected faces (else edges, else points) are extruded;
  in Object Mode a curve gives a new extrusion mesh (capped when it is closed
  and planar) and an open mesh becomes a solid.
"""

import math

import bmesh
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

import bpy
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Matrix, Vector, geometry

from . import command_line, cplane, getpoint, settings, transform, workspace
from .getpoint import CANCEL, FINISH
from .transform import TOLERANCE, _about, _TransformObjects

ARROW_PIXELS = 90  # Length of the arrows on screen, times the UI scale.
AXIS_NAMES = ("X", "Y", "Z")
AXIS_COLORS = ((0.9, 0.25, 0.25), (0.35, 0.8, 0.3), (0.3, 0.5, 1.0))
HIGHLIGHT = (1.0, 0.85, 0.3)

# Handle geometry in units of the arrow length, along the handle's axis (z)
# or, for the rotation arcs, in the plane of the other two axes.
SHAFT = (0.18, 0.9)
CONE = (0.9, 1.12, 0.05)  # Start, tip, radius.
BOX = (0.55, 0.045)  # Center, half size.
ARC = (0.75, math.radians(20), math.radians(70))  # Radius, start and end angle.
BALL = 0.07
PLANE = (0.3, 0.42)  # The planar handle's square, from and to, along both axes of its plane.


# Where the gumball was relocated to: (selection key, center, axes).
_relocation = None


def _selection_key(context):
    return context.mode, tuple(sorted(obj.name for obj in context.selected_objects))


def relocated(context):
    """(center, axes) of a relocated gumball for this selection, or None."""
    if _relocation is not None and _relocation[0] == _selection_key(context):
        return _relocation[1], _relocation[2]
    return None


def relocate(context, center, axes):
    global _relocation
    _relocation = (_selection_key(context), center.copy(), tuple(a.copy() for a in axes))


def reset_relocation():
    global _relocation
    _relocation = None


def _axes(context, area):
    """The gumball's axes for its alignment setting."""
    alignment = settings.get(context).gumball_alignment
    if alignment == 'WORLD':
        return Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1))
    if alignment == 'OBJECT':
        obj = context.edit_object or context.active_object
        if obj is None and context.selected_objects:
            obj = context.selected_objects[0]
        if obj is not None:
            rotation = obj.matrix_world.to_quaternion().to_matrix()
            return tuple(Vector(rotation.col[i]).normalized() for i in range(3))
    plane = cplane.for_area(context.window.screen, area)
    return plane.x_axis.normalized(), plane.y_axis.normalized(), plane.normal.normalized()


def frame(context, area, region):
    """(center, axes, length) of the gumball in a viewport: the center of the
    selection's bounding box along the axes (CPlane, object or world, see
    _axes; or where it was relocated) and the arrow length in world units.
    None without a selection."""
    points = _selection_points(context)
    if points is None or not len(points):
        return None
    moved = relocated(context)
    if moved is not None:
        center, axes = moved
    else:
        axes = _axes(context, area)
        basis = np.array([tuple(a) for a in axes])
        local = points @ basis.T
        middle = (local.min(axis=0) + local.max(axis=0)) / 2
        center = Vector(tuple(middle @ basis))
    length = _pixel_size(region, area.spaces.active.region_3d, center)
    if length is None:
        return None
    return center, axes, length * ARROW_PIXELS * context.preferences.system.ui_scale


def _selection_points(context):
    """World positions spanning the selection: corners of the selected
    objects' bounding boxes, or the selected mesh or curve points in Edit
    Mode."""
    if context.mode == 'EDIT_CURVE':
        found = [tuple(position) for obj, chosen in transform.selected_curve_points(context)
                 for spline, point, names in chosen
                 for position in transform.curve_point_positions(obj, spline, point, names)]
        return np.array(found, dtype=np.float64).reshape(-1, 3)
    if context.mode == 'EDIT_MESH':
        found = [np.empty((0, 3))]
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue  # (The mesh's selection counts lag behind the edit mesh.)
            # Read at every redraw of every viewport: the points as they
            # are, turned to world space all at once (one by one, a dense
            # mesh with everything selected took 42 ms for 100 000 points).
            local = [v.co[:] for v in bmesh.from_edit_mesh(obj.data).verts if v.select]
            if local:
                matrix = np.array(obj.matrix_world, dtype=np.float64)
                found.append(np.array(local, dtype=np.float64) @ matrix[:3, :3].T + matrix[:3, 3])
        return np.concatenate(found)
    if context.mode != 'OBJECT':
        return None
    found = []
    for obj in context.selected_objects:
        matrix = obj.matrix_world
        found += [tuple(matrix @ Vector(corner)) for corner in obj.bound_box]
    return np.array(found, dtype=np.float64).reshape(-1, 3)


def _pixel_size(region, rv3d, point):
    """World length of one pixel at ``point``, from the projection (as
    Blender sizes its gizmos). Unprojecting two neighbouring pixels instead
    is too imprecise in perspective views: the gumball would jitter."""
    depth = (rv3d.perspective_matrix @ point.to_4d()).w
    scale = region.width * rv3d.window_matrix[0][0]
    if depth <= 0 or scale == 0:
        return None  # Behind the view.
    return 2 * depth / scale


def _handle_frame(axes, axis):
    """The handle's axis as z, the other two (in cyclic order) as x and y."""
    return axes[(axis + 1) % 3], axes[(axis + 2) % 3], axes[axis]


def grab_point(found, handle, axis=0):
    """Where a handle is, in world space: a point to press to grab it."""
    center, axes, length = found
    u, v, w = _handle_frame(axes, axis)
    if handle == 'MOVE':
        return center + w * (CONE[0] + CONE[1]) / 2 * length
    if handle == 'SCALE':
        return center + w * BOX[0] * length
    if handle == 'ROTATE':
        angle = (ARC[1] + ARC[2]) / 2
        return center + (u * math.cos(angle) + v * math.sin(angle)) * ARC[0] * length
    if handle == 'PLANE':
        return center + (u + v) * (PLANE[0] + PLANE[1]) / 2 * length
    return center.copy()


# --- Handle shapes ------------------------------------------------------------

def _cone():
    start, tip, radius = CONE
    ring = [(radius * math.cos(a), radius * math.sin(a), start)
            for a in (2 * math.pi * i / 12 for i in range(13))]
    tris = []
    for a, b in zip(ring, ring[1:]):
        tris += [a, b, (0, 0, tip), a, (0, 0, start), b]
    return tris


def _box():
    middle, half = BOX
    corners = [(x * half, y * half, middle + z * half) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    faces = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))
    tris = []
    for a, b, c, d in faces:
        tris += [corners[a], corners[b], corners[c], corners[a], corners[c], corners[d]]
    return tris


def _ball():
    tips = [(BALL, 0, 0), (-BALL, 0, 0), (0, BALL, 0), (0, -BALL, 0), (0, 0, BALL), (0, 0, -BALL)]
    tris = []
    for x in (0, 1):
        for y in (2, 3):
            for z in (4, 5):
                tris += [tips[x], tips[y], tips[z]]
    return tris


def _square():
    low, high = PLANE
    corners = [(low, low, 0), (high, low, 0), (high, high, 0), (low, high, 0)]
    return [corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]]


def _arc_lines():
    radius, start, end = ARC
    steps = 16
    points = [(radius * math.cos(a), radius * math.sin(a), 0)
              for a in (start + (end - start) * i / steps for i in range(steps + 1))]
    return [p for pair in zip(points, points[1:]) for p in pair]


_SHAPES = {
    'MOVE': ([(0, 0, SHAFT[0]), (0, 0, SHAFT[1])], _cone()),
    'SCALE': ([], _box()),
    'ROTATE': (_arc_lines(), []),
    'FREE': ([], _ball()),
    'PLANE': ([], _square()),
}


class R2B_GT_gumball_handle(bpy.types.Gizmo):
    """One Gumball handle: lines (drawn wide) and solid parts."""

    bl_idname = "R2B_GT_gumball_handle"
    __slots__ = ("lines", "solid", "select_shapes")

    def build(self, handle):
        self.lines, solid = _SHAPES[handle]
        self.solid = self.new_custom_shape('TRIS', solid) if solid else None
        # Picking: the solid parts, and the lines, except most of the arrow
        # shaft (the scale box sits on it).
        lines = self.lines if handle != 'MOVE' else [(0, 0, 0.72), (0, 0, SHAFT[1])]
        self.select_shapes = [self.new_custom_shape('LINES', lines)] if lines else []
        if self.solid is not None:
            self.select_shapes.append(self.solid)

    def draw(self, context):
        color = (*(self.color_highlight if self.is_highlight else self.color), self.alpha)
        if self.lines:
            region = context.region
            shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            shader.uniform_float("viewportSize", (region.width, region.height))
            shader.uniform_float("lineWidth", 2.0 * context.preferences.system.ui_scale)
            shader.uniform_float("color", color)
            gpu.state.blend_set('ALPHA')
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(self.matrix_world)
                batch_for_shader(shader, 'LINES', {"pos": self.lines}).draw(shader)
            gpu.state.blend_set('NONE')
        if self.solid is not None:
            self.draw_custom_shape(self.solid)

    def draw_select(self, context, select_id):
        for shape in self.select_shapes:
            self.draw_custom_shape(shape, select_id=select_id)


class R2B_GGT_gumball(bpy.types.GizmoGroup):
    bl_idname = "R2B_GGT_gumball"
    bl_label = "Gumball"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'3D', 'PERSISTENT', 'SHOW_MODAL_ALL'}

    @classmethod
    def poll(cls, context):
        return (context.workspace is not None and context.workspace.get(workspace.MARKER)
                and settings.get(context).gumball and context.mode in {'OBJECT'} | transform.EDIT_MODES)

    def setup(self, context):
        self.handles = []
        for handle, axes in (('FREE', (0,)), ('MOVE', (0, 1, 2)), ('ROTATE', (0, 1, 2)), ('SCALE', (0, 1, 2)),
                             ('PLANE', (0, 1, 2))):
            for axis in axes:
                gizmo = self.gizmos.new(R2B_GT_gumball_handle.bl_idname)
                gizmo.build(handle)
                gizmo.use_draw_scale = False  # Sized in draw_prepare.
                gizmo.color = (0.9, 0.9, 0.9) if handle == 'FREE' else AXIS_COLORS[axis]
                gizmo.color_highlight = HIGHLIGHT
                gizmo.alpha = 0.6 if handle == 'PLANE' else 0.9
                gizmo.alpha_highlight = 1.0
                properties = gizmo.target_set_operator("r2b.gumball_drag")
                properties.handle = handle
                properties.axis = axis
                self.handles.append((gizmo, handle, axis))

    def draw_prepare(self, context):
        active = getpoint._active
        found = None
        if active is None or isinstance(active, R2B_OT_gumball_drag):
            found = frame(context, context.area, context.region)
        if found is not None and active is not None and active.preview_matrix is not None:
            # Follow the selection while a handle is dragged.
            matrix = active.preview_matrix
            center, axes, length = found
            rotation = matrix.to_3x3()
            found = (matrix @ center, tuple((rotation @ a).normalized() for a in axes), length)
        rv3d = context.region_data
        for gizmo, handle, axis in self.handles:
            gizmo.hide = found is None
            if found is None:
                continue
            center, axes, length = found
            if handle in {'MOVE', 'SCALE', 'PLANE'}:
                toward = (rv3d.view_rotation @ Vector((0, 0, 1)) if not rv3d.is_perspective
                          else (rv3d.view_matrix.inverted().translation - center).normalized())
                facing = abs(axes[axis].dot(toward))
                # Arrows and boxes pointing at the viewer would cover the
                # center, squares seen edge-on are lines: hidden, as in Rhino.
                gizmo.hide = facing > 0.995 if handle != 'PLANE' else facing < 0.1
            u, v, w = _handle_frame(axes, axis)
            gizmo.matrix_basis = _matrix(u * length, v * length, w * length, center)


def _matrix(x, y, z, origin):
    """4x4 matrix with the given columns."""
    return Matrix(((x[0], y[0], z[0], origin[0]), (x[1], y[1], z[1], origin[1]),
                   (x[2], y[2], z[2], origin[2]), (0, 0, 0, 1)))


# --- Dragging and clicking handles ----------------------------------------------

def _closest_on_line(region, rv3d, mouse, origin, direction):
    """The point on a line nearest to the view ray through ``mouse``."""
    ray = region_2d_to_origin_3d(region, rv3d, mouse)
    along = region_2d_to_vector_3d(region, rv3d, mouse)
    found = geometry.intersect_line_line(origin, origin + direction, ray, ray + along)
    return found[0] if found else None


class R2B_OT_gumball_drag(_TransformObjects, bpy.types.Operator):
    """Drag to move, rotate or scale the selection with Osnap, or click to
    type a distance, angle or scale factor"""

    bl_idname = "r2b.gumball_drag"
    bl_label = "Gumball"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    handle: bpy.props.EnumProperty(
        items=(('MOVE', "Move", "Move along the axis"), ('ROTATE', "Rotate", "Rotate about the axis"),
               ('SCALE', "Scale", "Scale along the axis"), ('FREE', "Free", "Move in the CPlane"),
               ('PLANE', "Plane", "Move in the plane across the axis (Shift: scale in it)")),
        options={'SKIP_SAVE'},
    )
    axis: bpy.props.IntProperty(min=0, max=2, options={'SKIP_SAVE'})
    extrude: bpy.props.BoolProperty(options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        found = frame(context, context.area, context.region)
        if found is None:
            return {'CANCELLED'}
        self.center, self.axes, _length = found
        if event.ctrl and self.handle == 'FREE':
            bpy.ops.r2b.gumball_relocate('INVOKE_DEFAULT')
            return {'CANCELLED'}
        self.extruding = self.handle == 'MOVE' and (self.extrude or event.ctrl)
        self.plane_scale = self.handle == 'PLANE' and event.shift  # Shift+drag a square: Scale2D.
        self.direction = self.axes[self.axis]
        self.pick_on_release = True
        self.dragging = False
        self.typing = False
        self.result = None
        self.press = (event.mouse_x, event.mouse_y)
        self.mouse = self.press
        region = context.region
        mouse = (self.press[0] - region.x, self.press[1] - region.y)
        self.grabbed = self._under_mouse(region, context.region_data, mouse)
        if self.grabbed is None:
            return {'CANCELLED'}
        self.base = self.grabbed
        self.uniform = False
        result = super().invoke(context, event)
        if 'RUNNING_MODAL' in result:
            self.points = [self.center.copy()]
            self._show_prompt()
        return result

    def needs_selection(self, context):
        return False  # The gumball is only there with a selection.

    # --- Where the mouse is, on the handle's axis or plane --------------------------

    def _plane(self):
        """The plane the handle works in: the rotation plane, or the plane of
        the gumball's x and y through the center for the free move."""
        u, v, _w = _handle_frame(self.axes, self.axis if self.handle in {'ROTATE', 'PLANE'} else 2)
        return cplane.CPlane("Gumball", self.center, u, v)

    def _under_mouse(self, region, rv3d, mouse):
        if self.handle in {'MOVE', 'SCALE'}:
            return _closest_on_line(region, rv3d, mouse, self.center, self.direction)
        return cplane.point_under_mouse(region, rv3d, self._plane(), mouse)

    def _project(self, point):
        """A (snapped or typed) point moved onto the handle's axis or plane."""
        if self.handle in {'MOVE', 'SCALE'}:
            return self.center + self.direction * (point - self.center).dot(self.direction)
        plane = self._plane()
        return point - plane.normal * (point - plane.origin).dot(plane.normal)

    def _track(self, context, event):
        self.mouse = (event.mouse_x, event.mouse_y)
        self.uniform = event.shift
        return super()._track(context, event)

    def picks_point(self):
        """Whether typing (after a click) asks for a point to move to."""
        return self.handle == 'FREE' or (self.handle == 'PLANE' and not self.plane_scale)

    def after_track(self):
        if self.typing:
            if self.picks_point():  # Picks the point the center goes to.
                self.base = self.center
                super().after_track()
            else:
                self.preview_matrix = None
            return
        if self.snap_mode is not None or self.track_lines:
            # Snappy dragging: the center goes to the snapped point.
            self.current = self._project(self.current)
            self.base = self.center
        else:
            area, region = getpoint._viewport_under(self.window, *self.mouse)
            if area is not None:
                mouse = (self.mouse[0] - region.x, self.mouse[1] - region.y)
                self.current = self._under_mouse(region, area.spaces.active.region_3d, mouse) or self.current
            self.base = self.grabbed
        super().after_track()

    # --- The transform ---------------------------------------------------------------

    def matrix_to(self, point):
        if point is None:
            return None
        if self.handle in {'MOVE', 'FREE'} or (self.handle == 'PLANE' and not self.plane_scale):
            return Matrix.Translation(point - self.base)
        if self.handle == 'PLANE':
            reference = self._in_plane(self.grabbed - self.center).length
            if reference < TOLERANCE:
                return None
            return self._scaling_2d(self._in_plane(point - self.center).length / reference)
        if self.handle == 'ROTATE':
            plane = self._plane()
            start, end = plane.to_local(self.grabbed), plane.to_local(point)
            if math.hypot(start.x, start.y) < TOLERANCE or math.hypot(end.x, end.y) < TOLERANCE:
                return None
            return self._rotation(math.atan2(end.y, end.x) - math.atan2(start.y, start.x))
        reference = (self.grabbed - self.center).dot(self.direction)
        if abs(reference) < TOLERANCE:
            return None
        return self._scaling((point - self.center).dot(self.direction) / reference)

    def _rotation(self, angle):
        return _about(self.center, Matrix.Rotation(angle, 4, self.direction))

    def _in_plane(self, vector):
        return vector - self.direction * vector.dot(self.direction)

    def _scaling_2d(self, factor):
        """Scaling in the plane across the handle's axis (Scale2D)."""
        n = self.direction
        flat = Matrix([[a * b for b in n] for a in n])
        return _about(self.center, factor * Matrix.Identity(3) + (1 - factor) * flat)

    def _scaling(self, factor):
        if self.uniform:
            return _about(self.center, Matrix.Diagonal((factor, factor, factor)))
        d = self.direction
        stretch = Matrix.Identity(3) + (factor - 1) * Matrix([[a * b for b in d] for a in d])
        return _about(self.center, stretch)

    # --- Picking ---------------------------------------------------------------------

    def prompt(self):
        name = AXIS_NAMES[self.axis]
        if self.handle == 'PLANE':
            plane = AXIS_NAMES[(self.axis + 1) % 3] + AXIS_NAMES[(self.axis + 2) % 3]
            if self.plane_scale:
                return f"Scale factor in {plane}" if self.typing else f"Drag to scale in {plane}"
            return "Point to move to" if self.typing else f"Drag to move in {plane} (Shift: scale in it)"
        if self.extruding:
            return f"Distance to extrude along {name}" if self.typing else f"Drag to extrude along {name}"
        if self.typing:
            return {'MOVE': f"Distance to move along {name}", 'ROTATE': f"Angle to rotate about {name}",
                    'SCALE': f"Scale factor along {name}", 'FREE': "Point to move to"}[self.handle]
        return {'MOVE': f"Drag to move along {name}", 'ROTATE': f"Drag to rotate about {name}",
                'SCALE': f"Drag to scale along {name} (Shift: in all directions)",
                'FREE': "Drag to move"}[self.handle]

    def modal(self, context, event):
        if event.type == 'LEFTMOUSE' and event.value in {'CLICK', 'DOUBLE_CLICK'}:
            return {'RUNNING_MODAL'}  # The handle's own click: not a selection click.
        if self.pick_on_release and event.type == 'MOUSEMOVE' and not self.dragging:
            threshold = context.preferences.inputs.drag_threshold_mouse * context.preferences.system.ui_scale
            if max(abs(event.mouse_x - self.press[0]), abs(event.mouse_y - self.press[1])) >= threshold:
                self.dragging = True
        if event.type == 'LEFTMOUSE':
            if self.pick_on_release and event.value == 'RELEASE' and not self.dragging:
                # A click: type the value instead.
                self.pick_on_release = False
                self.typing = True
                self.preview_matrix = None
                self._show_prompt()
                getpoint._tag_viewports(self.window)
                return {'RUNNING_MODAL'}
            if self.typing and event.value == 'PRESS' and not self.picks_point():
                return self._end(context, finished=False)  # Clicked away.
        return super().modal(context, event)

    def on_point(self, context):
        point = self.points[-1]
        if self.current is None or (point - self.current).length > 1e-9:
            # A typed point: the center goes there.
            self.base = self.center
            point = self._project(point)
        self.result = self.matrix_to(point)
        if self.result is None:
            self.points.pop()
            command_line.echo(self.window, "That point does not give a transform", 'ERROR')
            return None
        return FINISH

    def on_number(self, context, value):
        if self.handle == 'MOVE':
            self.result = Matrix.Translation(self.direction * value)
        elif self.handle == 'ROTATE':
            self.result = self._rotation(math.radians(value))
        elif self.handle == 'SCALE' or (self.handle == 'PLANE' and self.plane_scale):
            if abs(value) < TOLERANCE:
                command_line.echo(self.window, "The scale factor must not be zero", 'ERROR')
                return True
            self.result = self._scaling(value) if self.handle == 'SCALE' else self._scaling_2d(value)
        else:
            return None  # Free or planar move: a distance from the center.
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def create(self, context):
        if self.extruding:
            message = extrude(context, self.result.translation)
            if message:
                command_line.echo(self.window, message, 'ERROR')
            return
        moved = relocated(context)
        self.apply(self.result)
        if moved is not None:  # A relocated gumball goes along.
            center, axes = moved
            rotation = self.result.to_3x3()
            relocate(context, self.result @ center, tuple((rotation @ a).normalized() for a in axes))


class R2B_OT_gumball_relocate(getpoint.GetPoints, bpy.types.Operator):
    """Move the gumball (not the selection): a new origin, then optionally
    the direction of its x axis"""

    bl_idname = "r2b.gumball_relocate"
    bl_label = "Relocate Gumball"
    bl_options = {'REGISTER', 'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode in {'OBJECT'} | transform.EDIT_MODES

    def invoke(self, context, event):
        area, region = context.area, context.region
        found = frame(context, area, region) if area is not None and area.type == 'VIEW_3D' else None
        if found is None:
            return {'CANCELLED'}
        self.axes = found[1]
        return super().invoke(context, event)

    def prompt(self):
        if not self.points:
            return "New gumball origin ( Reset )"
        return "X axis direction. Press Enter to keep the axes"

    def on_option(self, context, name):
        if "reset".startswith(name):
            reset_relocation()
            return FINISH
        return False

    def modal(self, context, event):
        if event.type == 'LEFTMOUSE' and event.value in {'RELEASE', 'CLICK'} and not self.points:
            return {'RUNNING_MODAL'}  # The Ctrl+click that started it.
        return super().modal(context, event)

    def on_point(self, context):
        if len(self.points) == 2:
            z_axis = self.axes[2]
            x_axis = self.points[1] - self.points[0]
            x_axis -= z_axis * x_axis.dot(z_axis)
            if x_axis.length < TOLERANCE:
                self.points.pop()
                command_line.echo(self.window, "Pick a direction across the z axis", 'ERROR')
                return None
            x_axis.normalize()
            self.axes = (x_axis, z_axis.cross(x_axis), z_axis)
            return FINISH
        return None

    def on_enter(self, context):
        return FINISH if self.points else CANCEL

    def create(self, context):
        if self.points:
            relocate(context, self.points[0], self.axes)


# --- Extruding ------------------------------------------------------------------

def _extrude_edit_mesh(context, vector):
    for obj in context.objects_in_mode_unique_data:  # A shared mesh once.
        if obj.type != 'MESH':
            continue
        mesh = bmesh.from_edit_mesh(obj.data)
        local = obj.matrix_world.inverted().to_3x3() @ vector
        faces = [f for f in mesh.faces if f.select]
        edges = [e for e in mesh.edges if e.select]
        verts = [v for v in mesh.verts if v.select]
        if faces:
            made = bmesh.ops.extrude_face_region(mesh, geom=faces)["geom"]
            bmesh.ops.delete(mesh, geom=faces, context='FACES')
        elif edges:
            made = bmesh.ops.extrude_edge_only(mesh, edges=edges)["geom"]
        elif verts:
            made = bmesh.ops.extrude_vert_indiv(mesh, verts=verts)["verts"]
        else:
            continue
        new_verts = [v for v in made if isinstance(v, bmesh.types.BMVert)]
        bmesh.ops.translate(mesh, vec=local, verts=new_verts)
        for element in (*mesh.verts, *mesh.edges, *mesh.faces):
            element.select = False
        for element in made:
            element.select = True
        mesh.select_flush(True)
        bmesh.update_edit_mesh(obj.data)


def _solid_from_open_mesh(obj, vector):
    """Extrude all the faces of an open mesh into a solid (in place)."""
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    if all(edge.is_manifold for edge in mesh.edges):
        mesh.free()
        return False
    faces = list(mesh.faces)
    made = bmesh.ops.extrude_face_region(mesh, geom=faces, use_keep_orig=True)["geom"]
    local = obj.matrix_world.inverted().to_3x3() @ vector
    bmesh.ops.translate(mesh, vec=local, verts=[v for v in made if isinstance(v, bmesh.types.BMVert)])
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    mesh.to_mesh(obj.data)
    mesh.free()
    obj.data.update()
    return True


def extrude(context, vector):
    """Extrude the selection along ``vector``; an error message or None."""
    if vector.length < TOLERANCE:
        return "Nothing to extrude: the distance is zero"
    if context.mode == 'EDIT_MESH':
        _extrude_edit_mesh(context, vector)
        return None
    if context.mode != 'OBJECT':
        return "Extrude works on curves and meshes, and on mesh points in Edit Mode"
    closed = []
    for obj in list(context.selected_objects):
        if obj.type == 'CURVE':
            from . import extrude as extrusions

            extrusions.curve_extrusion(context, obj, vector)
        elif obj.type == 'MESH' and not _solid_from_open_mesh(obj, vector):
            closed.append(obj.name)
    if closed:
        return f"Closed meshes are extruded face by face in Edit Mode: {', '.join(closed)}"
    return None


_classes = (R2B_GT_gumball_handle, R2B_OT_gumball_drag, R2B_OT_gumball_relocate, R2B_GGT_gumball)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    reset_relocation()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
