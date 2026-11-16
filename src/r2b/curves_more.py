# SPDX-License-Identifier: GPL-3.0-or-later
"""More of Rhino's curve and point commands (MODE feature).

* Ellipse: center, end of the first axis, then the second axis's length (a
  point or a number): an exact rational NURBS (an affine image of a circle).
* Polygon: center and a corner (or a radius); NumSides option.
* Point / Points: point objects, which are small plain-axes empties (Osnap's
  Point snaps to them).
* Divide: points along curves at equal lengths, by a number of segments.
* DupEdge: polylines along the mesh edges selected in Edit Mode (one per
  chain). DupBorder: the boundaries of open meshes as closed polylines.
"""

import math

import bmesh
import numpy as np

import bpy
from mathutils import Matrix, Vector

from . import command_line, curve_geom, curves, nurbs
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-9
POINT_SIZE = 0.25  # Display size of point objects (empties).


def new_point(context, location):
    empty = bpy.data.objects.new("Point", None)
    empty.empty_display_type = 'PLAIN_AXES'
    empty.empty_display_size = POINT_SIZE
    empty.location = location
    context.collection.objects.link(empty)
    return empty


class R2B_OT_ellipse(GetPoints, bpy.types.Operator):
    """Draw an ellipse from its center and the ends of its axes"""

    bl_idname = "r2b.ellipse"
    bl_label = "Ellipse"

    def prompt(self):
        return ("Ellipse center", "End of first axis", "End of second axis")[min(len(self.points), 2)]

    def _axes(self, point):
        center, first = self.points[0], self.points[1]
        x_axis = first - center
        normal = self.plane.normal
        y_direction = normal.cross(x_axis).normalized()
        if point is None:
            return None
        b = abs((point - center).dot(y_direction))
        return x_axis, y_direction * b

    def on_point(self, context):
        if len(self.points) == 2 and (self.points[1] - self.points[0]).length < TOLERANCE:
            self.points.pop()
            return None
        if len(self.points) == 3:
            x_axis, y_axis = self._axes(self.points.pop())
            if y_axis.length < TOLERANCE:
                command_line.echo(self.window, "The second axis must not be zero", 'ERROR')
                return None
            self.result = (x_axis, y_axis)
            return FINISH
        return None

    def on_number(self, context, value):
        if len(self.points) != 2:
            return None
        if value <= 0:
            return True
        x_axis = self.points[1] - self.points[0]
        self.result = (x_axis, self.plane.normal.cross(x_axis).normalized() * value)
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def preview(self):
        if len(self.points) == 2 and self.current is not None:
            x_axis, y_axis = self._axes(self.current)
            center = self.points[0]
            return [([center + x_axis * math.cos(a) + y_axis * math.sin(a)
                      for a in np.linspace(0, 2 * math.pi, 65)], 'curve')]
        return super().preview()

    def create(self, context):
        x_axis, y_axis = self.result
        points, weights = nurbs.arc_points(self.points[0], x_axis, y_axis, 1.0, 0.0, 2 * math.pi)
        obj, spline = curves._new_object(context, "Ellipse", 'NURBS', points, weights)
        spline.order_u = 3
        spline.use_bezier_u = True
        spline.use_endpoint_u = True
        spline.resolution_u = 16


class R2B_OT_polygon(GetPoints, bpy.types.Operator):
    """Draw a regular polygon from its center and a corner"""

    bl_idname = "r2b.polygon"
    bl_label = "Polygon"
    sides = 5

    def prompt(self):
        if not self.points:
            return f"Center of polygon ( NumSides={type(self).sides} )"
        return f"Corner of polygon or radius ( NumSides={type(self).sides} )"

    def on_option(self, context, name):
        if "numsides".startswith(name):
            self.asking_sides = True
            return True
        return False

    def invoke(self, context, event):
        self.asking_sides = False
        return super().invoke(context, event)

    def _corners(self, corner):
        center, normal = self.points[0], self.plane.normal
        first = corner - center
        first -= normal * first.dot(normal)
        count = type(self).sides
        return [center + Matrix.Rotation(2 * math.pi * i / count, 3, normal) @ first for i in range(count)]

    def on_number(self, context, value):
        if self.asking_sides:
            if value >= 3 and abs(value - round(value)) < 1e-9:
                type(self).sides = int(round(value))
                self.asking_sides = False
            return True
        if len(self.points) != 1 or value <= 0:
            return None
        self.result = self._corners(self.points[0] + self.plane.x_axis * value)
        return FINISH

    def on_point(self, context):
        if len(self.points) == 2:
            corner = self.points.pop()
            if (corner - self.points[0]).length < TOLERANCE:
                return None
            self.result = self._corners(corner)
            return FINISH
        return None

    def on_enter(self, context):
        return CANCEL

    def preview(self):
        if len(self.points) == 1 and self.current is not None and (self.current - self.points[0]).length > TOLERANCE:
            corners = self._corners(self.current)
            return [(corners + corners[:1], 'curve')]
        return super().preview()

    def create(self, context):
        curves.new_curve_object(context, "Polygon", self.result, closed=True)


class _Point(GetPoints):
    many = False

    def prompt(self):
        return "Location of point" + (". Press Enter when done" if self.many else "")

    def on_point(self, context):
        new_point(context, self.points[-1])
        return None if self.many else FINISH

    def on_enter(self, context):
        return FINISH if self.points else CANCEL

    def preview(self):
        return []

    def create(self, context):
        pass


class R2B_OT_point(_Point, bpy.types.Operator):
    """Place a point object"""

    bl_idname = "r2b.point"
    bl_label = "Point"


class R2B_OT_points(_Point, bpy.types.Operator):
    """Place point objects until Enter"""

    bl_idname = "r2b.points"
    bl_label = "Points"
    many = True


class R2B_OT_divide(GetPoints, bpy.types.Operator):
    """Place points along curves at equal lengths"""

    bl_idname = "r2b.divide"
    bl_label = "Divide"
    count = 10

    def needs_selection(self, context):
        return not any(o.type == 'CURVE' for o in context.selected_objects)

    def selection_done(self, context):
        self.curves = [o for o in context.selected_objects if o.type == 'CURVE']
        if not self.curves:
            command_line.echo(self.window, "Select curves", 'ERROR')
            return False
        return True

    def prompt(self):
        if self.selecting:
            return "Select curves to divide. Press Enter when done"
        return f"Number of segments <{type(self).count}>"

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if value < 2 or abs(value - round(value)) > 1e-9:
            command_line.echo(self.window, "Type a whole number, 2 or more", 'ERROR')
            return True
        type(self).count = int(round(value))
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        segments = type(self).count
        made = 0
        for obj in self.curves:
            for spline in obj.data.splines:
                chain = curve_geom.from_spline(spline)
                if chain is None:
                    continue
                chain = chain.transformed(obj.matrix_world)
                total = curve_geom.length(chain)
                closed = curve_geom.is_closed(chain, curve_geom.tolerance(chain))
                for i in range(segments if closed else segments + 1):
                    t = curve_geom.parameter_at_length(chain, total * i / segments)
                    new_point(context, Vector(tuple(curve_geom.evaluate(chain, t))))
                    made += 1
        command_line.echo(self.window, f"{made} points")


def _chains_of_edges(edges):
    """Ordered vertex chains through a set of edges: [(verts, closed)]."""
    remaining = set(edges)
    chains = []
    while remaining:
        edge = remaining.pop()
        chain = [edge.verts[0], edge.verts[1]]
        for forward in (True, False):
            while True:
                end = chain[-1] if forward else chain[0]
                next_edge = next((e for e in end.link_edges if e in remaining), None)
                if next_edge is None:
                    break
                remaining.discard(next_edge)
                other = next_edge.other_vert(end)
                if forward:
                    chain.append(other)
                else:
                    chain.insert(0, other)
        closed = len(chain) > 2 and chain[0] is chain[-1]
        if closed:
            chain.pop()
        chains.append((chain, closed))
    return chains


def dup_edge(context):
    """Polylines along the mesh edges selected in Edit Mode."""
    made = 0
    for obj in context.objects_in_mode:
        if obj.type != 'MESH':
            continue
        mesh = bmesh.from_edit_mesh(obj.data)
        for verts, closed in _chains_of_edges([e for e in mesh.edges if e.select]):
            curves.new_curve_object(context, "DupEdge", [obj.matrix_world @ v.co for v in verts], closed)
            made += 1
    command_line.echo(context.window, f"{made} curves" if made else "Select mesh edges", 'OUTPUT' if made else 'ERROR')


dup_edge.operators = ()


def dup_border(context):
    """The boundaries of the selected open meshes as closed polylines."""
    made = 0
    for obj in [o for o in context.selected_objects if o.type == 'MESH']:
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        for verts, closed in _chains_of_edges([e for e in mesh.edges if e.is_boundary]):
            curves.new_curve_object(context, "DupBorder", [obj.matrix_world @ v.co for v in verts], closed)
            made += 1
        mesh.free()
    command_line.echo(context.window, f"{made} curves" if made else "No open edges", 'OUTPUT' if made else 'ERROR')


dup_border.operators = ()


_classes = (R2B_OT_ellipse, R2B_OT_polygon, R2B_OT_point, R2B_OT_points, R2B_OT_divide)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
