# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's deformations (MODE feature): Twist, Taper and Bend with Blender's
Simple Deform modifier, Stretch as a scaling along the axis.

Each asks for an axis (two points), then the amount: Twist an angle, Taper
the start and end distances, Stretch the new end of the axis, Bend an angle
or a point to bend toward. An empty at the axis start gives the modifier its
axis. Without Record History the modifier is applied (and the empty
removed); with it, the modifier and the empty stay, so the deformation can
be changed in the modifier panel or by moving the empty.

The deformation runs over the object's extent along the axis (Blender's
Simple Deform), not only between the axis points as Rhino's does.
"""

import math

import bpy
from mathutils import Matrix

from . import command_line, settings
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-9


def _axis_empty(context, origin, z_axis, x_hint):
    """An empty at ``origin`` whose z is ``z_axis`` (x toward ``x_hint``)."""
    z = z_axis.normalized()
    x = x_hint - z * x_hint.dot(z)
    if x.length < TOLERANCE:
        x = z.orthogonal()
    x.normalize()
    y = z.cross(x)
    empty = bpy.data.objects.new("Deform Axis", None)
    empty.empty_display_type = 'SINGLE_ARROW'
    empty.matrix_world = Matrix.Translation(origin) @ Matrix((x, y, z)).transposed().to_4x4()
    context.collection.objects.link(empty)
    return empty


def deform(context, objects, method, empty, angle=None, factor=None, axis='Z'):
    """Add a Simple Deform modifier to each object; apply it unless Record
    History is on. Returns the objects that could not be deformed."""
    history = settings.get(context).record_history
    failed = []
    for obj in objects:
        if obj.type not in {'MESH', 'CURVE', 'LATTICE'}:
            failed.append(obj.name)
            continue
        modifier = obj.modifiers.new(method.title(), 'SIMPLE_DEFORM')
        modifier.deform_method = method
        modifier.deform_axis = axis
        modifier.origin = empty
        if angle is not None:
            modifier.angle = angle
        if factor is not None:
            modifier.factor = factor
        if history:
            continue
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        with context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
            bpy.ops.object.modifier_move_to_index(modifier=modifier.name, index=0)
            bpy.ops.object.modifier_apply(modifier=modifier.name)
    if not history:
        bpy.data.objects.remove(empty)
    return failed


class _Deform(GetPoints):
    """Select objects, then the axis, then the amount."""

    method = 'TWIST'

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.objects = []
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not context.selected_objects

    def selection_done(self, context):
        self.objects = [o for o in context.selected_objects if o.type in {'MESH', 'CURVE', 'LATTICE'}]
        if not self.objects:
            command_line.echo(self.window, "Select meshes or curves", 'ERROR')
            return False
        return True

    def axis_prompts(self):
        return ["Start of axis", "End of axis"]

    def prompt(self):
        if self.selecting:
            return "Select objects to deform. Press Enter when done"
        steps = self.axis_prompts() + self.value_prompts()
        return steps[min(len(self.points), len(steps) - 1)]

    def value_prompts(self):
        raise NotImplementedError

    def axis(self):
        return self.points[1] - self.points[0]

    def on_point(self, context):
        if len(self.points) == 2 and self.axis().length < TOLERANCE:
            self.points.pop()
            command_line.echo(self.window, "The axis needs two different points", 'ERROR')
            return None
        if len(self.points) >= 3:
            return self.with_point(context, self.points[-1])
        return None

    def on_number(self, context, value):
        if len(self.points) < 2:
            return None
        return self.with_number(context, value)

    def on_enter(self, context):
        return CANCEL

    def preview(self):
        lines = super().preview()
        return lines[:1] if len(self.points) < 2 else [(self.points[:2], 'guide')]

    def finish(self, context, empty, **values):
        failed = deform(context, self.objects, self.method, empty, **values)
        if failed:
            command_line.echo(self.window, f"Not deformed: {', '.join(failed)}", 'ERROR')

    def create(self, context):
        pass  # Done in with_number / with_point.


class R2B_OT_twist(_Deform, bpy.types.Operator):
    """Twist objects about an axis"""

    bl_idname = "r2b.twist"
    bl_label = "Twist"
    method = 'TWIST'

    def value_prompts(self):
        return ["Twist angle"]

    def with_number(self, context, value):
        # Simple Deform spreads the twist angle over the object's extent along the axis.
        empty = _axis_empty(context, self.points[0], self.axis(), self.plane.x_axis)
        self.finish(context, empty, angle=math.radians(value))
        return FINISH

    def with_point(self, context, point):
        self.points.pop()
        command_line.echo(self.window, "Type the twist angle", 'ERROR')
        return None


class R2B_OT_taper(_Deform, bpy.types.Operator):
    """Taper objects along an axis: from a start distance to an end distance"""

    bl_idname = "r2b.taper"
    bl_label = "Taper"
    method = 'TAPER'

    def invoke(self, context, event):
        self.start_distance = None
        return super().invoke(context, event)

    def value_prompts(self):
        return ["Start distance" if self.start_distance is None else "End distance"]

    def _distance_of(self, point):
        axis = self.axis().normalized()
        offset = point - self.points[0]
        return (offset - axis * offset.dot(axis)).length

    def with_number(self, context, value):
        if value <= 0:
            command_line.echo(self.window, "The distance must be more than zero", 'ERROR')
            return True
        if self.start_distance is None:
            self.start_distance = value
            return True
        # Simple Deform spreads the scale change over the object's extent along the axis.
        empty = _axis_empty(context, self.points[0], self.axis(), self.plane.x_axis)
        self.finish(context, empty, factor=value / self.start_distance - 1)
        return FINISH

    def with_point(self, context, point):
        self.points.pop()
        return self.with_number(context, self._distance_of(point))


class R2B_OT_stretch(_Deform, bpy.types.Operator):
    """Stretch objects along an axis: the axis end moves to a new end, the
    objects scale along the axis from its start (a one-direction scaling)"""

    bl_idname = "r2b.stretch"
    bl_label = "Stretch"
    method = 'STRETCH'

    def value_prompts(self):
        return ["New end of axis or stretch factor"]

    def with_number(self, context, value):
        from .transform import _about, set_world_matrix
        from mathutils import Matrix as _Matrix

        if value <= 0:
            command_line.echo(self.window, "The factor must be more than zero", 'ERROR')
            return True
        d = self.axis().normalized()
        stretch = _Matrix.Identity(3) + (value - 1) * _Matrix([[a * b for b in d] for a in d])
        matrix = _about(self.points[0], stretch)
        for obj in self.objects:
            set_world_matrix(obj, matrix @ obj.matrix_world)
        return FINISH

    def with_point(self, context, point):
        self.points.pop()
        axis = self.axis()
        return self.with_number(context, (point - self.points[0]).dot(axis.normalized()) / axis.length)


class R2B_OT_bend(_Deform, bpy.types.Operator):
    """Bend objects along a spine, by an angle or toward a point"""

    bl_idname = "r2b.bend"
    bl_label = "Bend"
    method = 'BEND'

    def axis_prompts(self):
        return ["Start of spine", "End of spine"]

    def value_prompts(self):
        return ["Point to bend toward or angle"]

    def _bend(self, context, angle, toward):
        spine = self.axis()
        normal = spine.cross(toward)
        if normal.length < TOLERANCE * spine.length:
            # Toward a direction along the spine: bend toward the CPlane's x
            # (or y if the spine is along x).
            side = self.plane.x_axis if abs(spine.normalized().dot(self.plane.x_axis)) < 0.9 else self.plane.y_axis
            normal = spine.cross(side)
        # Simple Deform bends the geometry along the empty's x about its z
        # (the angle spread over the object's extent).
        empty = _axis_empty(context, self.points[0], normal, spine)
        self.finish(context, empty, angle=angle, axis='Z')
        return FINISH

    def with_number(self, context, value):
        return self._bend(context, math.radians(value), self.plane.normal.cross(self.axis()))

    def with_point(self, context, point):
        self.points.pop()
        toward = point - self.points[0]
        spine = self.axis()
        angle = 2 * spine.angle(toward, 0.0)
        return self._bend(context, angle, toward)


_classes = (R2B_OT_twist, R2B_OT_taper, R2B_OT_stretch, R2B_OT_bend)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
