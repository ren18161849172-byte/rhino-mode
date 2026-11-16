# SPDX-License-Identifier: GPL-3.0-or-later
"""More of Rhino's transform commands (MODE feature): Orient, Orient3Pt,
Shear, ProjectToCPlane, SetPt, Align and Distribute. They work like Move and
Rotate (transform.py): on objects, or on the selected points in Edit Mode,
picking points with Osnap, with a preview and one undo step.

* Orient: two reference points, then two target points; the selection is
  moved, turned and (Scale=1D/3D) scaled so the first pair lands on the
  second. Orient3Pt: three points each (a plane and a direction).
* Shear: origin, reference point (the direction that slides), then an angle
  or a point; points slide along the reference direction in proportion to
  their distance across it, in the CPlane.
* ProjectToCPlane: flatten onto the CPlane (DeleteInput=No keeps the
  original). SetPt: set x, y and/or z (CPlane coordinates) of the selected
  points or objects to those of a picked point.
* Align: Left, Right, Top, Bottom (CPlane y), HorizCenter, VertCenter, then a
  point to align to (Enter: the selection's own extreme). Distribute: three or
  more objects evenly spaced between the outer ones, along CPlane x, y or z.
"""

import math

import bmesh

import bpy
from mathutils import Matrix, Vector

from . import command_line
from .getpoint import CANCEL, FINISH
from .transform import TOLERANCE, _about, _TransformObjects, object_center, set_world_matrix


def _frame(origin, x_point, plane_point, normal_hint):
    """A 4x4 matrix whose columns are an orthonormal frame at ``origin``:
    x toward ``x_point``, y toward ``plane_point`` (or the hint's plane)."""
    x = x_point - origin
    if x.length < TOLERANCE:
        return None
    x.normalize()
    if plane_point is not None:
        y = plane_point - origin
        y -= x * y.dot(x)
        if y.length < TOLERANCE:
            return None
        y.normalize()
    else:
        y = normal_hint.cross(x)
        if y.length < TOLERANCE:
            y = x.orthogonal()
        y.normalize()
    z = x.cross(y)
    return Matrix(((x[0], y[0], z[0], origin[0]), (x[1], y[1], z[1], origin[1]),
                   (x[2], y[2], z[2], origin[2]), (0, 0, 0, 1)))


class _WithCopy(_TransformObjects):
    """A Copy option (objects only)."""

    copy_default = False

    def invoke(self, context, event):
        self.copy = self.copy_default
        return super().invoke(context, event)

    def copy_option(self):
        return "" if self.edit_mode else f"Copy={'Yes' if self.copy else 'No'}"

    def on_option(self, context, name):
        if not self.edit_mode and "copy".startswith(name):
            self.copy = not self.copy
            return True
        return False

    def prompt(self):
        if self.selecting:
            return super().prompt()
        text = self.step_prompt()
        options = " ".join(o for o in self.options_list() if o)
        return f"{text} ( {options} )" if options else text

    def options_list(self):
        return [self.copy_option()]

    def create(self, context):
        self.apply(self.result, copy=self.copy and not self.edit_mode)


class R2B_OT_orient(_WithCopy, bpy.types.Operator):
    """Move, turn and optionally scale objects from two reference points to two target points"""

    bl_idname = "r2b.orient"
    bl_label = "Orient"
    verb = "orient"
    _NAMES = ("First reference point", "Second reference point", "First target point", "Second target point")

    def invoke(self, context, event):
        self.scale = 'NO'
        return super().invoke(context, event)

    def step_prompt(self):
        return self._NAMES[min(len(self.points), 3)]

    def options_list(self):
        scale = {'NO': "No", '1D': "1D", '3D': "3D"}[self.scale]
        return [self.copy_option(), f"Scale={scale}"]

    def on_option(self, context, name):
        if "scale".startswith(name):
            order = ('NO', '1D', '3D')
            self.scale = order[(order.index(self.scale) + 1) % 3]
            return True
        return super().on_option(context, name)

    def _matrix(self, r1, r2, t1, t2):
        source, target = r2 - r1, t2 - t1
        if source.length < TOLERANCE or target.length < TOLERANCE:
            return None
        rotation = source.normalized().rotation_difference(target.normalized()).to_matrix().to_4x4()
        factor = target.length / source.length
        if self.scale == '3D':
            stretch = Matrix.Diagonal((factor, factor, factor, 1))
        elif self.scale == '1D':
            d = source.normalized()
            stretch = (Matrix.Identity(3) + (factor - 1) * Matrix([[a * b for b in d] for a in d])).to_4x4()
        else:
            stretch = Matrix.Identity(4)
        return Matrix.Translation(t1) @ rotation @ stretch @ Matrix.Translation(-r1)

    def matrix_to(self, point):
        if len(self.points) == 2:
            return Matrix.Translation(point - self.points[0])
        if len(self.points) == 3:
            return self._matrix(*self.points[:3], point)
        return None

    def on_point(self, context):
        if len(self.points) == 4:
            self.result = self._matrix(*self.points)
            if self.result is None:
                self.points.pop()
                command_line.echo(self.window, "The points of a pair must differ", 'ERROR')
                return None
            return FINISH
        return None


class R2B_OT_orient3pt(_WithCopy, bpy.types.Operator):
    """Move and turn objects from three reference points to three target points"""

    bl_idname = "r2b.orient3pt"
    bl_label = "Orient3Pt"
    verb = "orient"
    _NAMES = ("First reference point", "Second reference point", "Third reference point",
              "First target point", "Second target point", "Third target point")

    def step_prompt(self):
        return self._NAMES[min(len(self.points), 5)]

    def _frames(self, points):
        normal = self.plane.normal
        source = _frame(points[0], points[1], points[2] if len(points) > 2 else None, normal)
        target = _frame(points[3], points[4] if len(points) > 4 else points[3] + (points[1] - points[0]),
                        points[5] if len(points) > 5 else None, normal) if len(points) > 3 else None
        return source, target

    def matrix_to(self, point):
        if len(self.points) < 3:
            return None
        source, target = self._frames(self.points + [point])
        if source is None or target is None:
            return None
        return target @ source.inverted()

    def on_point(self, context):
        if len(self.points) == 6:
            self.result = self.matrix_to(self.points.pop())
            if self.result is None:
                command_line.echo(self.window, "The points must not be on one line", 'ERROR')
                return None
            return FINISH
        return None


class R2B_OT_shear(_WithCopy, bpy.types.Operator):
    """Slide objects along a direction in proportion to their distance across it"""

    bl_idname = "r2b.shear"
    bl_label = "Shear"
    verb = "shear"

    def step_prompt(self):
        return ("Origin point", "Reference point", "Shear angle or point")[min(len(self.points), 2)]

    def _shear(self, angle):
        origin = self.points[0]
        direction = self.points[1] - origin
        direction -= self.plane.normal * direction.dot(self.plane.normal)
        if direction.length < TOLERANCE or abs(math.cos(angle)) < 1e-6:
            return None
        direction.normalize()
        across = self.plane.normal.cross(direction)
        matrix = Matrix.Identity(3) + math.tan(angle) * Matrix([[a * b for b in across] for a in direction])
        return _about(origin, matrix)

    def _angle_to(self, point):
        origin, reference = self.points[0], self.points[1]
        a, b = reference - origin, point - origin
        normal = self.plane.normal
        return math.atan2(a.cross(b).dot(normal), a.dot(b))

    def matrix_to(self, point):
        if len(self.points) != 2:
            return None
        return self._shear(-self._angle_to(point))

    def on_number(self, context, value):
        if len(self.points) != 2:
            return None
        self.result = self._shear(math.radians(value))
        if self.result is None:
            command_line.echo(self.window, "The angle must be less than 90 degrees", 'ERROR')
            return True
        return FINISH

    def on_point(self, context):
        if len(self.points) == 3:
            self.result = self.matrix_to(self.points.pop())
            if self.result is None:
                command_line.echo(self.window, "Pick a point off the reference direction", 'ERROR')
                return None
            return FINISH
        if len(self.points) == 2 and (self.points[1] - self.points[0]).length < TOLERANCE:
            self.points.pop()
        return None


class R2B_OT_project_to_cplane(_TransformObjects, bpy.types.Operator):
    """Flatten objects or points onto the construction plane"""

    bl_idname = "r2b.project_to_cplane"
    bl_label = "ProjectToCPlane"
    verb = "project"

    def invoke(self, context, event):
        self.delete_input = True
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        if self.edit_mode:
            return "Press Enter to flatten the points onto the CPlane"
        return f"Press Enter to flatten onto the CPlane ( DeleteInput={'Yes' if self.delete_input else 'No'} )"

    def on_option(self, context, name):
        if not self.edit_mode and "deleteinput".startswith(name):
            self.delete_input = not self.delete_input
            return True
        return False

    def _matrix(self):
        n = self.plane.normal.normalized()
        return _about(self.plane.origin, Matrix.Identity(3) - Matrix([[a * b for b in n] for a in n]))

    def after_track(self):
        self.preview_matrix = self._matrix()

    def on_point(self, context):
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        self.apply(self._matrix(), copy=not self.delete_input and not self.edit_mode)


class R2B_OT_set_pt(_TransformObjects, bpy.types.Operator):
    """Set the x, y and/or z coordinate (CPlane) of points or objects to a picked point's"""

    bl_idname = "r2b.set_pt"
    bl_label = "SetPt"
    verb = "set"
    proportional = False  # Each point is set on its own.

    def invoke(self, context, event):
        self.axes = [True, True, True]
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        flags = " ".join(f"Set{n}={'Yes' if on else 'No'}" for n, on in zip("XYZ", self.axes))
        return f"Location for points ( {flags} )"

    def on_option(self, context, name):
        for index, axis in enumerate("xyz"):
            if f"set{axis}".startswith(name) and len(name) == 4:
                self.axes[index] = not self.axes[index]
                return True
        return False

    def _set(self, position, target):
        local, wanted = self.plane.to_local(position), self.plane.to_local(target)
        values = [wanted[i] if self.axes[i] else local[i] for i in range(3)]
        return self.plane.to_world(*values)

    def on_point(self, context):
        return FINISH

    def create(self, context):
        target = self.points[-1]
        if self.edit_kind == 'EDIT_CURVE':
            for obj, chosen in self.curve_points:
                inverse = obj.matrix_world.inverted()
                for spline, point, names in chosen:
                    for name in names if spline.type == 'BEZIER' else ("co",):
                        world = obj.matrix_world @ (getattr(point, name) if spline.type == 'BEZIER' else point.co.xyz)
                        local = inverse @ self._set(world, target)
                        if spline.type == 'BEZIER':
                            setattr(point, name, local)
                        else:
                            point.co = (*local, point.co.w)
                obj.data.update_tag()
            return
        if self.edit_mode:
            for obj, indices in self.points_to_move:
                mesh = bmesh.from_edit_mesh(obj.data)
                mesh.verts.ensure_lookup_table()
                inverse = obj.matrix_world.inverted()
                for index in indices:
                    vertex = mesh.verts[index]
                    vertex.co = inverse @ self._set(obj.matrix_world @ vertex.co, target)
                bmesh.update_edit_mesh(obj.data)
            return
        for obj in self.objects:
            center = object_center(obj)
            set_world_matrix(obj, Matrix.Translation(self._set(center, target) - center) @ obj.matrix_world)


_ALIGN = {
    'LEFT': (0, min), 'RIGHT': (0, max), 'BOTTOM': (1, min), 'TOP': (1, max),
    'HORIZCENTER': (0, None), 'VERTCENTER': (1, None),
}


def _extent(obj, plane, axis):
    """(low, high) of an object's bounding box along a CPlane axis."""
    values = [plane.to_local(obj.matrix_world @ Vector(c))[axis] for c in obj.bound_box]
    return min(values), max(values)


class R2B_OT_align(_TransformObjects, bpy.types.Operator):
    """Align objects by their bounding boxes in the construction plane"""

    bl_idname = "r2b.align"
    bl_label = "Align"
    verb = "align"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.how = None
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        if self.how is None:
            return "Alignment ( Bottom HorizCenter Left Right Top VertCenter )"
        return f"Point to align {self.how.lower()} to. Press Enter for the selection's own"

    def on_option(self, context, name):
        for how in _ALIGN:
            if how.lower().startswith(name):
                self.how = how
                return True
        return False

    def _value(self, obj, axis, side):
        low, high = _extent(obj, self.plane, axis)
        return (low + high) / 2 if side is None else side(low, high)

    def _matrices(self, target):
        axis, side = _ALIGN[self.how]
        result = {}
        for obj in self.objects:
            shift = [0.0, 0.0, 0.0]
            shift[axis] = target - self._value(obj, axis, side)
            delta = self.plane.to_world(*shift) - self.plane.origin
            result[obj] = Matrix.Translation(delta)
        return result

    def _default(self):
        axis, side = _ALIGN[self.how]
        values = [self._value(obj, axis, side) for obj in self.objects]
        return (min if side is min else max)(values) if side is not None else sum(values) / len(values)

    def on_point(self, context):
        if self.how is None:
            self.points.pop()
            return None
        self.target = self.plane.to_local(self.points[-1])[_ALIGN[self.how][0]]
        return FINISH

    def on_enter(self, context):
        if self.how is None:
            return CANCEL
        self.target = self._default()
        return FINISH

    def create(self, context):
        for obj, matrix in self._matrices(self.target).items():
            set_world_matrix(obj, matrix @ obj.matrix_world)


class R2B_OT_distribute(_TransformObjects, bpy.types.Operator):
    """Space three or more objects evenly between the outer ones"""

    bl_idname = "r2b.distribute"
    bl_label = "Distribute"
    verb = "distribute"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.axis = 0
        self.gap = False
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        return (f"Press Enter to distribute ( Direction={'XYZ'[self.axis]}Axis "
                f"Mode={'Gap' if self.gap else 'Center'} )")

    def on_option(self, context, name):
        if "direction".startswith(name):
            self.axis = (self.axis + 1) % 3
            return True
        if "mode".startswith(name):
            self.gap = not self.gap
            return True
        return False

    def on_point(self, context):
        self.points.pop()
        return None

    def on_enter(self, context):
        if len(self.objects) < 3:
            command_line.echo(self.window, "Distribute needs three or more objects", 'ERROR')
            return CANCEL
        return FINISH

    def create(self, context):
        plane, axis = self.plane, self.axis
        spans = {obj: _extent(obj, plane, axis) for obj in self.objects}
        ordered = sorted(self.objects, key=lambda o: sum(spans[o]) / 2)
        if self.gap:
            total = sum(high - low for low, high in spans.values())
            first_low, last_high = spans[ordered[0]][0], spans[ordered[-1]][1]
            gap = (last_high - first_low - total) / (len(ordered) - 1)
            position = first_low
            targets = {}
            for obj in ordered:
                low, high = spans[obj]
                targets[obj] = position - low
                position += (high - low) + gap
        else:
            first = sum(spans[ordered[0]]) / 2
            last = sum(spans[ordered[-1]]) / 2
            step = (last - first) / (len(ordered) - 1)
            targets = {obj: first + step * i - sum(spans[obj]) / 2 for i, obj in enumerate(ordered)}
        for obj, shift in targets.items():
            offset = [0.0, 0.0, 0.0]
            offset[axis] = shift
            set_world_matrix(obj, Matrix.Translation(plane.to_world(*offset) - plane.origin) @ obj.matrix_world)


_classes = (R2B_OT_orient, R2B_OT_orient3pt, R2B_OT_shear, R2B_OT_project_to_cplane, R2B_OT_set_pt, R2B_OT_align,
            R2B_OT_distribute)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
