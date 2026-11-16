# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's arrays (MODE feature): Array, ArrayLinear, ArrayPolar and
ArrayCrv make copies of the selected objects.

* Array: numbers along the CPlane's x, y and z, then the spacings (typed, or
  two points).
* ArrayLinear: the number, then two points: copies step by their distance.
* ArrayPolar: a center, the number, the angle to fill (360: all around),
  about the CPlane's normal.
* ArrayCrv: a path curve and the number: copies spread evenly along it,
  turned with the curve.

The copies are independent objects. With Record History on, ArrayLinear and
ArrayPolar add Blender's Array modifier instead (live: change the count or
the offset in the modifier panel).
"""

import math

import bpy
from mathutils import Matrix, Vector

from . import command_line, curve_geom, settings
from .getpoint import CANCEL, FINISH
from .transform import TOLERANCE, _about, _TransformObjects


class _Array(_TransformObjects):
    verb = "array"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def matrices(self):
        """The transforms of the copies (not the original)."""
        return []

    def preview(self):
        lines = []
        if len(self.outline):
            for matrix in self.matrices()[:200]:
                lines.append((self._outline(matrix), 'shape'))
        if self.points and self.current is not None:
            lines.append(([self.points[-1], self.current], 'guide'))
        return lines

    def make(self, context):
        for matrix in self.matrices():
            self.apply(matrix, copy=True)

    def create(self, context):
        self.make(context)


def _count(value, minimum=1):
    count = int(round(value))
    return count if count >= minimum and abs(value - count) < 1e-9 else None


class R2B_OT_array(_Array, bpy.types.Operator):
    """Copy objects in rows, columns and levels along the construction plane"""

    bl_idname = "r2b.array"
    bl_label = "Array"

    def invoke(self, context, event):
        self.counts = []
        self.spacings = []
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        if len(self.counts) < 3:
            return f"Number in {'xyz'[len(self.counts)]} direction <{1 if self.counts else 2}>"
        axis = 'xyz'[len(self.spacings)]
        return f"Spacing in {axis} direction (number, or two points)" if not self.points else "Second point"

    def _next_spacing_axis(self):
        while len(self.spacings) < 3 and self.counts[len(self.spacings)] == 1:
            self.spacings.append(0.0)

    def _number(self, value):
        if len(self.counts) < 3:
            count = _count(value)
            if count is None:
                command_line.echo(self.window, "Type a whole number, 1 or more", 'ERROR')
                return True
            self.counts.append(count)
            if len(self.counts) == 3:
                self._next_spacing_axis()
                if len(self.spacings) == 3:
                    return FINISH
            return True
        self.spacings.append(value)
        self._next_spacing_axis()
        return FINISH if len(self.spacings) == 3 else True

    def on_number(self, context, value):
        if self.points:
            return None
        return self._number(value)

    def on_enter(self, context):
        if len(self.counts) < 3:
            if self._number(1 if self.counts else 2) == FINISH:
                return FINISH
            self._show_prompt()
            return None
        return CANCEL

    def on_point(self, context):
        if len(self.counts) < 3:
            self.points.pop()
            return None
        if len(self.points) == 2:
            first, second = self.points
            self.points.clear()
            return self._number((second - first).length)
        return None

    def matrices(self):
        if len(self.counts) < 3 or len(self.spacings) < 3:
            return []
        axes = (self.plane.x_axis, self.plane.y_axis, self.plane.normal)
        found = []
        for i in range(self.counts[0]):
            for j in range(self.counts[1]):
                for k in range(self.counts[2]):
                    if i == j == k == 0:
                        continue
                    offset = sum((a * s * n for a, s, n in zip(axes, self.spacings, (i, j, k))), Vector())
                    found.append(Matrix.Translation(offset))
        return found


class R2B_OT_array_linear(_Array, bpy.types.Operator):
    """Copy objects in a row: the number, then two points for the step"""

    bl_idname = "r2b.array_linear"
    bl_label = "ArrayLinear"

    def invoke(self, context, event):
        self.count = None
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        if self.count is None:
            return "Number of elements <2>"
        return ("First reference point", "Second reference point")[min(len(self.points), 1)]

    def on_number(self, context, value):
        if self.count is None:
            self.count = _count(value, 2)
            if self.count is None:
                command_line.echo(self.window, "Type a whole number, 2 or more", 'ERROR')
            return True
        return None

    def on_enter(self, context):
        if self.count is None:
            self.count = 2
            self._show_prompt()
            return None
        return CANCEL

    def on_point(self, context):
        if self.count is None:
            self.points.pop()
            return None
        if len(self.points) == 2:
            if (self.points[1] - self.points[0]).length < TOLERANCE:
                self.points.pop()
                return None
            return FINISH
        return None

    def _step(self, point=None):
        if not self.points or self.count is None:
            return None
        end = self.points[1] if len(self.points) > 1 else point
        return end - self.points[0] if end is not None else None

    def matrices(self):
        step = self._step(self.current)
        if step is None:
            return []
        return [Matrix.Translation(step * i) for i in range(1, self.count)]

    def make(self, context):
        step = self._step()
        if settings.get(context).record_history:
            for obj in self.objects:
                modifier = obj.modifiers.new("ArrayLinear", 'ARRAY')
                modifier.count = self.count
                modifier.use_relative_offset = False
                modifier.use_constant_offset = True
                modifier.constant_offset_displace = obj.matrix_world.inverted().to_3x3() @ step
            return
        super().make(context)


class R2B_OT_array_polar(_Array, bpy.types.Operator):
    """Copy objects around a center"""

    bl_idname = "r2b.array_polar"
    bl_label = "ArrayPolar"

    def invoke(self, context, event):
        self.count = None
        self.fill = None
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return super().prompt()
        if not self.points:
            return "Center of polar array"
        if self.count is None:
            return "Number of items <4>"
        return "Angle to fill <360>"

    def on_point(self, context):
        if len(self.points) > 1:
            self.points.pop()
        return None

    def on_number(self, context, value):
        if not self.points:
            return None
        if self.count is None:
            self.count = _count(value, 2)
            if self.count is None:
                command_line.echo(self.window, "Type a whole number, 2 or more", 'ERROR')
            return True
        self.fill = value
        return FINISH

    def on_enter(self, context):
        if not self.points:
            return CANCEL
        if self.count is None:
            self.count = 4
            self._show_prompt()
            return None
        self.fill = 360.0
        return FINISH

    def _step_angle(self):
        fill = self.fill if self.fill is not None else 360.0
        full = abs(abs(fill) - 360) < 1e-9
        return math.radians(fill) / (self.count if full else self.count - 1)

    def matrices(self):
        if not self.points or self.count is None:
            return []
        step = self._step_angle()
        return [_about(self.points[0], Matrix.Rotation(step * i, 4, self.plane.normal)) for i in range(1, self.count)]

    def make(self, context):
        if not settings.get(context).record_history:
            super().make(context)
            return
        step = self._step_angle()
        for obj in self.objects:
            pivot = bpy.data.objects.new("ArrayPolar Center", None)
            context.collection.objects.link(pivot)
            pivot.matrix_world = _about(self.points[0], Matrix.Rotation(step, 4, self.plane.normal)) @ obj.matrix_world
            modifier = obj.modifiers.new("ArrayPolar", 'ARRAY')
            modifier.count = self.count
            modifier.use_relative_offset = False
            modifier.use_object_offset = True
            modifier.offset_object = pivot
            pivot.parent = obj
            pivot.matrix_parent_inverse = obj.matrix_world.inverted()


class R2B_OT_array_crv(_Array, bpy.types.Operator):
    """Copy objects along a path curve"""

    bl_idname = "r2b.array_crv"
    bl_label = "ArrayCrv"

    def invoke(self, context, event):
        self.path = None
        self.count = None
        self.first_round = True
        return super().invoke(context, event)

    def needs_selection(self, context):
        return True  # The objects, then the path curve.

    def selection_done(self, context):
        if self.first_round:
            if not super().selection_done(context):
                return False
            self.first_round = False
            self.chosen = list(self.objects)
            for obj in context.selected_objects:
                obj.select_set(False)
            command_line.echo(self.window, "Select the path curve", 'INFO')
            return False
        paths = [o for o in context.selected_objects if o.type == 'CURVE' and o not in self.chosen]
        if not paths:
            command_line.echo(self.window, "Select a path curve", 'ERROR')
            return False
        chain = curve_geom.from_spline(paths[0].data.splines[0]) if paths[0].data.splines else None
        if chain is None:
            return False
        self.path = chain.transformed(paths[0].matrix_world)
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in self.chosen:
            obj.select_set(True)
        self.objects = self.chosen
        return True

    def prompt(self):
        if self.selecting:
            return "Select objects to array. Press Enter when done" if self.first_round else \
                "Select the path curve. Press Enter when done"
        return "Number of items <5>"

    def on_number(self, context, value):
        self.count = _count(value, 2)
        if self.count is None:
            command_line.echo(self.window, "Type a whole number, 2 or more", 'ERROR')
            return True
        return FINISH

    def on_enter(self, context):
        self.count = 5
        return FINISH

    def on_point(self, context):
        self.points.pop()
        return None

    def matrices(self):
        if self.path is None or self.count is None:
            return []
        total = curve_geom.length(self.path)
        start = Vector(tuple(curve_geom.evaluate(self.path, 0.0)))
        start_tangent = Vector(tuple(curve_geom.tangent(self.path, 0.0))).normalized()
        closed = curve_geom.is_closed(self.path, curve_geom.tolerance(self.path))
        steps = self.count if closed else self.count - 1
        found = []
        for i in range(1, self.count):
            t = curve_geom.parameter_at_length(self.path, total * i / steps)
            point = Vector(tuple(curve_geom.evaluate(self.path, t)))
            tangent = Vector(tuple(curve_geom.tangent(self.path, t))).normalized()
            turn = start_tangent.rotation_difference(tangent).to_matrix().to_4x4()
            found.append(Matrix.Translation(point) @ turn @ Matrix.Translation(-start))
        return found


_classes = (R2B_OT_array, R2B_OT_array_linear, R2B_OT_array_polar, R2B_OT_array_crv)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
