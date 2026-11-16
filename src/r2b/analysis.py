# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's measuring commands (MODE feature); results go to the command line.

* Distance: two points (also in CPlane x, y, z). Angle: a vertex and two
  points.
* Length: selected curves (exact), or mesh edges selected in Edit Mode.
* Area: selected meshes (world space), closed planar curves. Volume: closed
  meshes.
* Radius: the radius of curvature in the middle of selected curves (exact
  for circles and arcs).
* What: name, type, layer, location, size and counts of the selected objects.
* BoundingBox: a box mesh around the selection, along the CPlane.
"""

import math

import bmesh
import numpy as np

import bpy
from mathutils import Vector

from . import command_line, curve_geom, extrude, solids
from .getpoint import CANCEL, FINISH, GetPoints


def _echo(context, text, kind='INFO'):
    command_line.echo(context.window, text, kind)


def _command(function):
    function.operators = ()
    return function


def _world_mesh(obj, context):
    depsgraph = context.evaluated_depsgraph_get()
    mesh = bmesh.new()
    mesh.from_object(obj, depsgraph)
    mesh.transform(obj.matrix_world)
    return mesh


def _chains(obj):
    for spline in obj.data.splines:
        chain = curve_geom.from_spline(spline)
        if chain is not None:
            yield chain.transformed(obj.matrix_world)


class R2B_OT_distance(GetPoints, bpy.types.Operator):
    """Measure the distance between two points"""

    bl_idname = "r2b.distance"
    bl_label = "Distance"

    def prompt(self):
        return ("First point for distance", "Second point for distance")[min(len(self.points), 1)]

    def on_point(self, context):
        return FINISH if len(self.points) == 2 else None

    def on_enter(self, context):
        return CANCEL

    def create(self, context):
        a, b = self.points
        local = self.plane.to_local(b) - self.plane.to_local(a)
        _echo(context, f"Distance = {(b - a).length:.6g}   CPlane dx = {local.x:.6g}  dy = {local.y:.6g}  "
                       f"dz = {local.z:.6g}")


class R2B_OT_angle(GetPoints, bpy.types.Operator):
    """Measure the angle at a vertex between two points"""

    bl_idname = "r2b.angle"
    bl_label = "Angle"

    def prompt(self):
        return ("Vertex of the angle", "End of first line", "End of second line")[min(len(self.points), 2)]

    def on_point(self, context):
        return FINISH if len(self.points) == 3 else None

    def on_enter(self, context):
        return CANCEL

    def create(self, context):
        vertex, a, b = self.points
        if (a - vertex).length < 1e-12 or (b - vertex).length < 1e-12:
            _echo(context, "The lines need two different points each", 'ERROR')
            return
        _echo(context, f"Angle = {math.degrees((a - vertex).angle(b - vertex)):.6g} degrees")


@_command
def length(context):
    if context.mode == 'EDIT_MESH':
        total = 0.0
        for obj in context.objects_in_mode:
            mesh = bmesh.from_edit_mesh(obj.data)
            total += sum((obj.matrix_world @ e.verts[0].co - obj.matrix_world @ e.verts[1].co).length
                         for e in mesh.edges if e.select)
        _echo(context, f"Length = {total:.6g}")
        return
    curves = [o for o in context.selected_objects if o.type == 'CURVE']
    if not curves:
        _echo(context, "Select curves (or mesh edges in Edit Mode)", 'ERROR')
        return
    total = sum(curve_geom.length(chain) for obj in curves for chain in _chains(obj))
    _echo(context, f"Length = {total:.6g}" + (f" ({len(curves)} curves)" if len(curves) > 1 else ""))


def _polygon_area(points):
    found = extrude.plane_of(points)
    if found is None:
        return None
    normal = found[1]
    total = Vector()
    for a, b in zip(points, np.vstack([points[1:], points[:1]])):
        total += Vector(tuple(a)).cross(Vector(tuple(b)))
    return abs(total.dot(normal)) / 2


@_command
def area(context):
    total, counted = 0.0, 0
    for obj in context.selected_objects:
        if obj.type == 'MESH':
            mesh = _world_mesh(obj, context)
            total += sum(f.calc_area() for f in mesh.faces)
            mesh.free()
            counted += 1
        elif obj.type == 'CURVE':
            for points, closed, _ in extrude.curve_polylines(obj, math.radians(1)):
                value = _polygon_area(points) if closed else None
                if value is not None:
                    total += value
                    counted += 1
    if not counted:
        _echo(context, "Select meshes or closed planar curves", 'ERROR')
        return
    _echo(context, f"Area = {total:.6g}")


@_command
def volume(context):
    total, open_ones = 0.0, []
    meshes = [o for o in context.selected_objects if o.type == 'MESH']
    if not meshes:
        _echo(context, "Select closed meshes", 'ERROR')
        return
    for obj in meshes:
        mesh = _world_mesh(obj, context)
        if all(e.is_manifold for e in mesh.edges):
            total += mesh.calc_volume(signed=False)
        else:
            open_ones.append(obj.name)
        mesh.free()
    text = f"Volume = {total:.6g}"
    if open_ones:
        text += f" (not closed, left out: {', '.join(open_ones)})"
    _echo(context, text)


@_command
def radius(context):
    curves = [o for o in context.selected_objects if o.type == 'CURVE']
    if not curves:
        _echo(context, "Select curves", 'ERROR')
        return
    for obj in curves:
        for chain in _chains(obj):
            segment, u = chain.segments[chain.count // 2], 0.5  # Inside a segment, away from its ends.
            h = 1e-3  # In float64 throughout (mathutils.Vector is single precision).
            a, b, c = curve_geom.segment_points(segment, [max(u - h, 0.0), u, min(u + h, 1.0)])
            cross = np.linalg.norm(np.cross(b - a, c - b))
            if cross < 1e-14 * max(np.linalg.norm(c - a) ** 2, 1e-300):
                _echo(context, f"{obj.name}: straight (no radius)")
                continue
            r = np.linalg.norm(b - a) * np.linalg.norm(c - b) * np.linalg.norm(c - a) / (2 * cross)
            _echo(context, f"{obj.name}: radius = {r:.6g}  diameter = {2 * r:.6g}")


@_command
def what(context):
    objects = list(context.selected_objects)
    if not objects:
        _echo(context, "Select objects", 'ERROR')
        return
    for obj in objects:
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        size = [max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3)]
        layer = ", ".join(c.name for c in obj.users_collection)
        text = (f"{obj.name}: {obj.type.lower()} on layer {layer}, at {tuple(round(v, 4) for v in obj.location)}, "
                f"size {size[0]:.4g} x {size[1]:.4g} x {size[2]:.4g}")
        if obj.type == 'MESH':
            text += f", {len(obj.data.vertices)} points, {len(obj.data.polygons)} faces"
        elif obj.type == 'CURVE':
            total = sum(curve_geom.length(chain) for chain in _chains(obj))
            text += f", {len(obj.data.splines)} splines, length {total:.6g}"
        if obj.modifiers:
            text += f", modifiers: {', '.join(m.name for m in obj.modifiers)}"
        _echo(context, text)


class R2B_OT_bounding_box(GetPoints, bpy.types.Operator):
    """Make a box around the selected objects, along the construction plane"""

    bl_idname = "r2b.bounding_box"
    bl_label = "BoundingBox"

    def needs_selection(self, context):
        return not context.selected_objects

    def selection_done(self, context):
        objects = list(context.selected_objects)
        if not objects:
            return False
        depsgraph = context.evaluated_depsgraph_get()
        local = []
        for obj in objects:
            evaluated = obj.evaluated_get(depsgraph)
            if obj.type == 'MESH':
                points = [evaluated.matrix_world @ v.co for v in evaluated.data.vertices]
            else:
                points = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            local += [self.plane.to_local(p) for p in points]
        low = Vector([min(p[i] for p in local) for i in range(3)])
        high = Vector([max(p[i] for p in local) for i in range(3)])
        size = high - low
        center = self.plane.to_world(*((low + high) / 2))
        mesh = bmesh.new()
        bmesh.ops.create_cube(mesh, size=1.0)
        bmesh.ops.scale(mesh, vec=tuple(max(s, 1e-9) for s in size), verts=mesh.verts)
        solids.new_mesh_object(context, "BoundingBox", mesh, solids.plane_matrix(self.plane, center))
        _echo(context, f"Bounding box {size.x:.6g} x {size.y:.6g} x {size.z:.6g} (CPlane)")
        return FINISH

    def prompt(self):
        return "Select objects for the bounding box. Press Enter when done"

    def create(self, context):
        pass


_classes = (R2B_OT_distance, R2B_OT_angle, R2B_OT_bounding_box)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
