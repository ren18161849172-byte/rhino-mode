# SPDX-License-Identifier: GPL-3.0-or-later
"""More of Rhino's surfaces from curves, as meshes (MODE feature): Revolve,
Loft, Sweep1, Pipe, ExtrudeCrvTapered and Slab.

* Revolve: profile curves, the axis (two points), the angle (Enter: a full
  circle). Points on the axis are merged.
* Loft: two or more curves, ordered along the direction they are spread in;
  each is resampled to the same number of points (closed ones lined up at
  their seams). Style=Normal passes smoothly through the sections,
  Style=Straight joins them with straight faces.
* Sweep1: the rail, then the profile (placed anywhere along the rail); the
  profile follows the rail without twisting (rotation-minimizing frames).
* Pipe: curves and a radius; round, with flat caps on open curves (Cap).
* ExtrudeCrvTapered: closed planar curves, a distance and a draft angle
  (DraftAngle option); the top is the curve offset inward (sharp corners).
* Slab: a curve, the side, the thickness and the height: the curve offset to
  that side, closed and extruded.

Mesh Angle sets how finely curves and circles are divided (MeshAngle).
"""

import math

import bmesh
import numpy as np

import bpy
from mathutils import Matrix, Vector

from . import command_line, extrude, settings
from .extrude import _FromCurves
from .getpoint import CANCEL, FINISH

TOLERANCE = 1e-9


def _vec(p):
    return Vector(tuple(p))


def _resample(points, closed, count):
    """``count`` points evenly spaced (by length) along a polyline."""
    pts = np.vstack([points, points[:1]]) if closed else points
    lengths = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    total = lengths.sum()
    if total < TOLERANCE:
        return np.repeat(points[:1], count, axis=0)
    cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
    targets = np.linspace(0.0, total, count + (1 if closed else 0))[:count] if closed else np.linspace(0.0, total, count)
    result = []
    for d in targets:
        i = min(np.searchsorted(cumulative, d, side='right') - 1, len(lengths) - 1)
        t = 0.0 if lengths[i] < TOLERANCE else (d - cumulative[i]) / lengths[i]
        result.append(pts[i] + (pts[i + 1] - pts[i]) * t)
    return np.array(result)


def _grid_mesh(rows, closed_u, closed_v, smooth=True):
    """Quads between rows of points (each row a list of Vectors)."""
    mesh = bmesh.new()
    verts = [[mesh.verts.new(tuple(p)) for p in row] for row in rows]
    count_u = len(rows[0])
    row_pairs = list(zip(range(len(rows) - 1), range(1, len(rows))))
    if closed_v:
        row_pairs.append((len(rows) - 1, 0))
    for a, b in row_pairs:
        for i in range(count_u if closed_u else count_u - 1):
            j = (i + 1) % count_u
            try:
                face = mesh.faces.new((verts[a][i], verts[a][j], verts[b][j], verts[b][i]))
                face.smooth = smooth
            except ValueError:
                pass
    return mesh, verts


def _finish_mesh(mesh, merge=0.0):
    if merge > 0:
        bmesh.ops.remove_doubles(mesh, verts=mesh.verts, dist=merge)
        degenerate = [f for f in mesh.faces if f.calc_area() < 1e-12]
        if degenerate:
            bmesh.ops.delete(mesh, geom=degenerate, context='FACES_ONLY')
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    extrude.mark_sharp(mesh)
    return mesh


def rotation_minimizing_frames(points, closed=False):
    """(tangents, normals) along a polyline: normals turned as little as
    possible from one point to the next."""
    pts = [_vec(p) for p in points]
    count = len(pts)
    tangents = []
    for i in range(count):
        if closed:
            t = pts[(i + 1) % count] - pts[i - 1]
        else:
            t = pts[min(i + 1, count - 1)] - pts[max(i - 1, 0)]
        tangents.append(t.normalized() if t.length > TOLERANCE else Vector((0, 0, 1)))
    normal = tangents[0].orthogonal().normalized()
    normals = [normal]
    for i in range(1, count):
        turn = tangents[i - 1].rotation_difference(tangents[i])
        normal = (turn @ normal)
        normal = (normal - tangents[i] * normal.dot(tangents[i])).normalized()
        normals.append(normal)
    return tangents, normals


# --- Commands -----------------------------------------------------------------------

class R2B_OT_revolve(_FromCurves, bpy.types.Operator):
    """Revolve curves about an axis"""

    bl_idname = "r2b.revolve"
    bl_label = "Revolve"

    def invoke(self, context, event):
        self.angle = None
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return "Select curves to revolve. Press Enter when done"
        if len(self.points) < 2:
            return ("Start of revolve axis", "End of revolve axis")[len(self.points)] + \
                f" ( DeleteInput={'Yes' if self.delete_input else 'No'} )"
        return "Revolution angle <360> ( FullCircle )"

    def on_option(self, context, name):
        if len(self.points) >= 2 and "fullcircle".startswith(name):
            self.angle = 360.0
            return FINISH
        if "deleteinput".startswith(name):
            self.delete_input = not self.delete_input
            return True
        return False

    def on_point(self, context):
        if len(self.points) == 2 and (self.points[1] - self.points[0]).length < TOLERANCE:
            self.points.pop()
        if len(self.points) > 2:
            self.points.pop()
        return None

    def on_number(self, context, value):
        if len(self.points) < 2:
            return None
        if abs(value) < 1e-6:
            command_line.echo(self.window, "The angle must not be zero", 'ERROR')
            return True
        self.angle = max(-360.0, min(360.0, value))
        return FINISH

    def on_enter(self, context):
        if len(self.points) < 2:
            return CANCEL
        self.angle = 360.0
        return FINISH

    def preview(self):
        if len(self.points) == 1 and self.current is not None:
            return [([self.points[0], self.current], 'guide')]
        return [(self.points[:2], 'guide')] if len(self.points) >= 2 else []

    def create(self, context):
        origin, axis = self.points[0], (self.points[1] - self.points[0]).normalized()
        full = abs(abs(self.angle) - 360.0) < 1e-9
        step_limit = settings.get(context).mesh_angle
        steps = max(2, math.ceil(math.radians(abs(self.angle)) / step_limit))
        angle = math.radians(self.angle)
        mesh = bmesh.new()
        size = max(float(np.ptp(np.vstack([p for p, _, _ in self.polylines]), axis=0).max()), 1.0)
        for points, closed, smooth in self.polylines:
            rows = []
            for k in range(steps if full else steps + 1):
                rotation = Matrix.Rotation(angle * k / steps, 3, axis)
                rows.append([origin + rotation @ (_vec(p) - origin) for p in points])
            part, _ = _grid_mesh([[tuple(p) for p in r] for r in rows], closed, full)
            data = bpy.data.meshes.new("tmp")
            part.to_mesh(data)
            part.free()
            mesh.from_mesh(data)
            bpy.data.meshes.remove(data)
        self.finish_with(context, _finish_mesh(mesh, merge=1e-7 * size), "Revolve")


def _order_along(items):
    """Order (center, ...) items along the direction they spread in."""
    centers = np.array([item[0] for item in items])
    if len(items) < 3:
        return items
    _, _, vectors = np.linalg.svd(centers - centers.mean(axis=0))
    direction = vectors[0]
    return [items[i] for i in np.argsort(centers @ direction)]


def _align_loop(reference, loop):
    """``loop`` (closed, N x 3) turned and possibly reversed to start where
    ``reference`` does and run the same way."""
    best = None
    for candidate in (loop, loop[::-1]):
        for shift in range(len(candidate)):
            rolled = np.roll(candidate, -shift, axis=0)
            cost = float(np.sum(np.linalg.norm(rolled - reference, axis=1)))
            if best is None or cost < best[0]:
                best = (cost, rolled)
    return best[1]


def _catmull_rom(rows, between):
    """Rows smoothly interpolated through the given rows (``between`` new
    rows in each span)."""
    rows = [np.asarray(r) for r in rows]
    result = []
    for i in range(len(rows) - 1):
        p0 = rows[i - 1] if i > 0 else 2 * rows[i] - rows[i + 1]
        p1, p2 = rows[i], rows[i + 1]
        p3 = rows[i + 2] if i + 2 < len(rows) else 2 * rows[i + 1] - rows[i]
        for k in range(between + 1):
            t = k / (between + 1)
            result.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                                 + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    result.append(rows[-1])
    return result


class R2B_OT_loft(_FromCurves, bpy.types.Operator):
    """Fit a surface through curves (sections)"""

    bl_idname = "r2b.loft"
    bl_label = "Loft"

    def invoke(self, context, event):
        self.straight = False
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return "Select curves to loft. Press Enter when done"
        return f"Press Enter to loft ( Style={'Straight' if self.straight else 'Normal'} )"

    def after_selection(self, context):
        if len(self.polylines) < 2:
            command_line.echo(self.window, "Loft needs two or more curves", 'ERROR')
            return False
        if len({closed for _, closed, _ in self.polylines}) != 1:
            command_line.echo(self.window, "The curves must be all open or all closed", 'ERROR')
            return False
        return True

    def on_option(self, context, name):
        if "style".startswith(name):
            self.straight = not self.straight
            return True
        return super().toggle(name)

    def on_point(self, context):
        self.points.pop()
        return None

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        closed = self.polylines[0][1]
        count = max(max(len(p) for p, _, _ in self.polylines), 16)
        sections = _order_along([(p.mean(axis=0), p) for p, _, _ in self.polylines])
        rows = [_resample(sections[0][1], closed, count)]
        for _, points in sections[1:]:
            row = _resample(points, closed, count)
            if closed:
                row = _align_loop(rows[-1], row)
            elif np.linalg.norm(row[0] - rows[-1][0]) > np.linalg.norm(row[-1] - rows[-1][0]):
                row = row[::-1]
            rows.append(row)
        if not self.straight and len(rows) > 2:
            rows = _catmull_rom(rows, 6)
        mesh, _ = _grid_mesh([[tuple(p) for p in r] for r in rows], closed, False)
        self.finish_with(context, _finish_mesh(mesh), "Loft")


def _sweep(rail, profile_points, closed_profile, rail_closed=False):
    """Rows of a profile carried along a rail (N x 3) without twisting; the
    profile is taken where it sits relative to the nearest rail point. Round
    a closed rail (its first point not repeated) the last row meets the
    first: what twist is left after the round is spread along the rail."""
    tangents, normals = rotation_minimizing_frames(rail, rail_closed)
    if rail_closed and len(rail) > 2:
        back = tangents[-1].rotation_difference(tangents[0]) @ normals[-1]
        back = (back - tangents[0] * back.dot(tangents[0])).normalized()
        twist = math.atan2(tangents[0].dot(back.cross(normals[0])), back.dot(normals[0]))
        normals = [Matrix.Rotation(twist * i / len(rail), 3, tangents[i]) @ normal
                   for i, normal in enumerate(normals)]
    center = _vec(profile_points.mean(axis=0))
    nearest = min(range(len(rail)), key=lambda i: (_vec(rail[i]) - center).length)
    frame = Matrix((normals[nearest], tangents[nearest].cross(normals[nearest]), tangents[nearest])).transposed()
    local = [frame.transposed() @ (_vec(p) - _vec(rail[nearest])) for p in profile_points]
    rows = []
    for i, point in enumerate(rail):
        frame_i = Matrix((normals[i], tangents[i].cross(normals[i]), tangents[i])).transposed()
        rows.append([tuple(_vec(point) + frame_i @ q) for q in local])
    return rows


class R2B_OT_sweep1(_FromCurves, bpy.types.Operator):
    """Sweep a profile curve along a rail curve"""

    bl_idname = "r2b.sweep1"
    bl_label = "Sweep1"

    def invoke(self, context, event):
        self.rail = None
        return super().invoke(context, event)

    def needs_selection(self, context):
        return True

    def prompt(self):
        if self.rail is None:
            return "Select the rail. Press Enter when done"
        return "Select the cross section curve. Press Enter when done"

    def selection_done(self, context):
        curves = [o for o in context.selected_objects if o.type == 'CURVE']
        angle = settings.get(context).mesh_angle
        if self.rail is None:
            if len(curves) != 1:
                command_line.echo(self.window, "Select one rail curve", 'ERROR')
                return False
            self.rail = extrude.curve_polylines(curves[0], angle)[0]
            self.rail_object = curves[0]
            curves[0].select_set(False)
            command_line.echo(self.window, "Select the cross section curve", 'INFO')
            return False
        profiles = [o for o in curves if o is not self.rail_object]
        if len(profiles) != 1:
            # One shape along the rail (several were taken, and all but the first left out).
            command_line.echo(self.window, "Select one cross section curve", 'ERROR')
            return False
        self.curves = profiles
        self.polylines = [p for obj in profiles for p in extrude.curve_polylines(obj, angle)]
        return FINISH

    def create(self, context):
        rail_points, rail_closed = self.rail[0], self.rail[1]
        points, closed, _ = self.polylines[0]
        rows = _sweep(rail_points, points, closed, rail_closed)
        mesh, _ = _grid_mesh(rows, closed, rail_closed)
        self.finish_with(context, _finish_mesh(mesh, merge=1e-9), "Sweep")


class R2B_OT_pipe(_FromCurves, bpy.types.Operator):
    """Make round pipes around curves"""

    bl_idname = "r2b.pipe"
    bl_label = "Pipe"
    last_radius = 1.0

    def invoke(self, context, event):
        self.cap = True
        return super().invoke(context, event)

    def prompt(self):
        if self.selecting:
            return "Select curves for the pipe. Press Enter when done"
        return f"Radius <{type(self).last_radius:.3f}> ( Cap={'Flat' if self.cap else 'None'} )"

    def on_option(self, context, name):
        if "cap".startswith(name):
            self.cap = not self.cap
            return True
        return False

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if value <= 0:
            command_line.echo(self.window, "The radius must be more than zero", 'ERROR')
            return True
        type(self).last_radius = value
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        radius = type(self).last_radius
        sides = max(8, math.ceil(2 * math.pi / settings.get(context).mesh_angle))
        circle = [(radius * math.cos(2 * math.pi * k / sides), radius * math.sin(2 * math.pi * k / sides))
                  for k in range(sides)]
        mesh = bmesh.new()
        for points, closed, _ in self.polylines:
            tangents, normals = rotation_minimizing_frames(points, closed)
            rows = []
            for point, t, n in zip(points, tangents, normals):
                b = t.cross(n)
                rows.append([tuple(_vec(point) + n * x + b * y) for x, y in circle])
            part, verts = _grid_mesh(rows, True, closed)
            if self.cap and not closed:
                for ring in (verts[0], verts[-1]):
                    try:
                        part.faces.new(ring)
                    except ValueError:
                        pass
            data = bpy.data.meshes.new("tmp")
            part.to_mesh(data)
            part.free()
            mesh.from_mesh(data)
            bpy.data.meshes.remove(data)
        self.finish_with(context, _finish_mesh(mesh), "Pipe")


def _offset_loop(points, closed, distance, normal):
    """A polyline offset in its plane by ``distance`` (positive: to the left
    walking along it, seen from ``normal``), sharp (mitered) corners."""
    pts = [_vec(p) for p in points]
    count = len(pts)
    result = []
    for i in range(count):
        before = pts[i] - pts[i - 1] if (closed or i > 0) else None
        after = pts[(i + 1) % count] - pts[i] if (closed or i < count - 1) else None
        lefts = [normal.cross(d).normalized() for d in (before, after) if d is not None and d.length > TOLERANCE]
        if not lefts:
            result.append(pts[i].copy())
            continue
        if len(lefts) == 1:
            result.append(pts[i] + lefts[0] * distance)
            continue
        a, b = lefts
        bisector = a + b
        if bisector.length < TOLERANCE:
            result.append(pts[i] + a * distance)
            continue
        bisector.normalize()
        result.append(pts[i] + bisector * (distance / max(bisector.dot(a), 0.2)))
    return result


def _winding(points, normal):
    """+1 if the closed polyline turns counter-clockwise seen from ``normal``."""
    total = Vector()
    for a, b in zip(points, list(points[1:]) + [points[0]]):
        total += _vec(a).cross(_vec(b))
    return 1 if total.dot(normal) >= 0 else -1


class R2B_OT_extrude_crv_tapered(extrude._Straight, _FromCurves, bpy.types.Operator):
    """Extrude closed planar curves with a draft angle"""

    bl_idname = "r2b.extrude_crv_tapered"
    bl_label = "ExtrudeCrvTapered"
    draft_angle = 5.0

    def invoke(self, context, event):
        self.asking_angle = False
        return super().invoke(context, event)

    def after_selection(self, context):
        planar = [p for p in self.polylines if p[1] and extrude.plane_of(p[0]) is not None]
        if not planar:
            command_line.echo(self.window, "Select closed planar curves", 'ERROR')
            return False
        self.polylines = planar
        self.start_distance(extrude.default_direction(self.polylines, self.plane), _vec(self.polylines[0][0][0]))
        return True

    def prompt(self):
        if self.selecting:
            return "Select closed planar curves to extrude. Press Enter when done"
        if self.asking_angle:
            return f"Draft angle <{type(self).draft_angle:.1f}>"
        return self.distance_prompt()

    def extra_options(self):
        return f" DraftAngle={type(self).draft_angle:g}"

    def on_option(self, context, name):
        if "draftangle".startswith(name):
            self.asking_angle = True
            return True
        return super().on_option(context, name)

    def on_number(self, context, value):
        if self.asking_angle:
            if not -80 < value < 80:
                command_line.echo(self.window, "Between -80 and 80 degrees", 'ERROR')
                return True
            type(self).draft_angle = value
            self.asking_angle = False
            return True
        return super().on_number(context, value)

    def preview_outline(self):
        return self.polylines

    def build(self, context, start, sweep):
        inset = sweep.length * math.tan(math.radians(type(self).draft_angle))
        normal = sweep.normalized()
        mesh = bmesh.new()
        rings = []
        for points, closed, _ in self.polylines:
            side = _winding(points, normal)  # Inward is to the left of a counter-clockwise loop.
            bottom = [_vec(p) + start for p in points]
            top = [p + sweep for p in _offset_loop(bottom, True, inset * side, normal)]
            rows = [[tuple(p) for p in bottom], [tuple(p) for p in top]]
            part, verts = _grid_mesh(rows, True, False, smooth=False)
            if self.solid:
                for ring, flip in ((verts[0], True), (verts[1], False)):
                    try:
                        part.faces.new(ring[::-1] if flip else ring)
                    except ValueError:
                        pass
            data = bpy.data.meshes.new("tmp")
            part.to_mesh(data)
            part.free()
            mesh.from_mesh(data)
            bpy.data.meshes.remove(data)
        self.finish_with(context, _finish_mesh(mesh), "Extrusion")


class R2B_OT_slab(_FromCurves, bpy.types.Operator):
    """Offset a planar curve to a side and extrude the outline into a slab"""

    bl_idname = "r2b.slab"
    bl_label = "Slab"
    last_distance = 1.0
    last_height = 1.0

    def invoke(self, context, event):
        self.side = None
        self.thickness = None
        return super().invoke(context, event)

    def after_selection(self, context):
        found = extrude.plane_of(self.polylines[0][0])
        self.normal = found[1] if found is not None else self.plane.normal
        if self.normal.dot(self.plane.normal) < 0:
            self.normal = -self.normal
        return True

    def prompt(self):
        if self.selecting:
            return "Select a curve. Press Enter when done"
        if self.side is None:
            return "Side to offset"
        if self.thickness is None:
            return f"Offset distance <{type(self).last_distance:.3f}>"
        return f"Height <{type(self).last_height:.3f}>"

    def on_point(self, context):
        point = self.points.pop()
        if self.side is None:
            points = self.polylines[0][0]
            nearest = min(range(len(points) - 1), key=lambda i: (_vec(points[i]) - point).length)
            direction = _vec(points[nearest + 1]) - _vec(points[nearest])
            self.side = 1 if self.normal.cross(direction).dot(point - _vec(points[nearest])) >= 0 else -1
            self.points = []
        return None

    def on_number(self, context, value):
        if self.side is None:
            return None
        if value <= 0:
            command_line.echo(self.window, "Must be more than zero", 'ERROR')
            return True
        if self.thickness is None:
            self.thickness = type(self).last_distance = value
            return True
        type(self).last_height = value
        return FINISH

    def on_enter(self, context):
        if self.side is None:
            return CANCEL
        if self.thickness is None:
            self.thickness = type(self).last_distance
            self._show_prompt()
            return None
        return FINISH

    def create(self, context):
        height = type(self).last_height
        polylines = []
        for points, closed, smooth in self.polylines:
            offset = np.array([tuple(p) for p in _offset_loop(points, closed, self.thickness * self.side, self.normal)])
            if closed:
                polylines += [(points, True, smooth), (offset, True, smooth)]
            else:
                outline = np.vstack([points, offset[::-1]])
                polylines.append((outline, True, [False] * len(outline)))
        mesh = extrude.extrusion_mesh(polylines, Vector(), self.normal * height, solid=True)
        self.finish_with(context, mesh, "Slab")


_classes = (R2B_OT_revolve, R2B_OT_loft, R2B_OT_sweep1, R2B_OT_pipe, R2B_OT_extrude_crv_tapered, R2B_OT_slab)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
