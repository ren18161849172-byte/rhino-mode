# SPDX-License-Identifier: GPL-3.0-or-later
"""Surfaces from curves and solids from surfaces (MODE feature): ExtrudeCrv,
ExtrudeSrf, ExtrudeCrvToPoint, ExtrudeCrvAlongCrv, PlanarSrf and Cap, as in
Rhino 8.

Blender has no NURBS surfaces, so the results are meshes: the curves are
read exactly (curve_geom) and divided where they bend, by at most the Mesh
Angle setting (MeshAngle command); straight segments stay single edges, so
extruded polylines and rectangles are exact. Faces along curved segments
are smooth shaded, edges at corners sharp. Closed planar curves get caps
(Solid=Yes), curves inside them holes.

* ExtrudeCrv: select curves, then the distance (picked along the direction,
  in any viewport; or typed; 0 brings the end to the CPlane's elevation).
  The direction is the plane of a planar curve's normal, else the CPlane's
  normal. Options: Direction (two points), BothSides, Solid, DeleteInput,
  SetBasePoint (where picked distances are measured from), Output=Mesh/SubD.
  With Record History on, the result stays linked to the curve (a Geometry
  Nodes modifier reading it): edit the curve and the extrusion follows.
* ExtrudeSrf: the same for surfaces (open meshes, or faces picked with
  Ctrl+Shift): a separate solid, the input stays (DeleteInput removes it).
* ExtrudeCrvToPoint: curves to an apex point. ExtrudeCrvAlongCrv: the
  profile curves moved along a path curve (not turned).
* PlanarSrf: closed planar curves become flat faces (nested ones holes).
* Cap: planar holes of meshes are filled.
"""

import math
from types import SimpleNamespace

import bmesh
import numpy as np

import bpy
from mathutils import Matrix, Vector

from . import command_line, curve_geom, settings, solids
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-6
SHARP_ANGLE = math.radians(30)  # Edges where faces meet at more than this are sharp.


# --- Curves as polylines --------------------------------------------------------

def _turning(segment):
    """Total turning angle of a segment's tangent (radians)."""
    _, derivatives = curve_geom.segment_derivatives(segment, np.linspace(0.0, 1.0, 17))
    lengths = np.linalg.norm(derivatives, axis=1)
    if np.any(lengths < 1e-12):
        return math.pi
    unit = derivatives / lengths[:, None]
    return float(np.sum(np.arccos(np.clip(np.sum(unit[1:] * unit[:-1], axis=1), -1.0, 1.0))))


def polyline(chain, max_angle):
    """(points N x 3, closed, smooth): the chain divided where it bends by at
    most ``max_angle``; ``smooth[i]`` tells whether the edge from point i
    comes from a curved segment."""
    tol = curve_geom.tolerance(chain)
    points, smooth = [], []
    for segment in chain.segments:
        control = segment[:, :3] / segment[:, 3:]
        straight = chain.degree == 1 or curve_geom._flatness(control) <= tol
        steps = 1 if straight else max(2, math.ceil(_turning(segment) / max_angle))
        u = np.linspace(0.0, 1.0, steps + 1)[:-1]
        points.extend(curve_geom.segment_points(segment, u))
        smooth.extend([not straight] * steps)
    closed = curve_geom.is_closed(chain, tol)
    if not closed:
        points.append(chain.end())
    return np.array(points, dtype=np.float64), closed, smooth


def curve_polylines(obj, max_angle):
    """World-space polylines of a curve object's splines."""
    found = []
    for spline in obj.data.splines:
        chain = curve_geom.from_spline(spline)
        if chain is not None:
            found.append(polyline(chain.transformed(obj.matrix_world), max_angle))
    return found


def world_normal(matrix, normal):
    """A face normal of an object with world ``matrix``, in world space
    (unit). Not the matrix times the normal: under an uneven scale that is
    no longer square to the face."""
    return (matrix.to_3x3().inverted_safe().transposed() @ normal).normalized()


def plane_of(points):
    """(origin, unit normal) if the points lie in a plane, else None (fewer
    than three points not on a line count as not planar)."""
    if len(points) < 3:
        return None
    center = points.mean(axis=0)
    # Not the full SVD: its first factor is N x N (12.8 GB for the 40 000
    # face corners of a mesh of 10 000 quads).
    _, values, vectors = np.linalg.svd(points - center, full_matrices=False)
    normal = vectors[-1]
    size = max(float(np.ptp(points, axis=0).max()), 1e-9)
    if values[1] < 1e-9 * size or np.abs((points - center) @ normal).max() > 1e-6 * max(size, 1.0):
        return None
    return Vector(tuple(center)), Vector(tuple(normal)).normalized()


def default_direction(polylines, plane):
    """The extrusion direction: the normal of the first planar curve (to the
    CPlane normal's side), else the CPlane normal."""
    for points, closed, _ in polylines:
        found = plane_of(points)
        if found is not None:
            normal = found[1]
            return normal if normal.dot(plane.normal) >= 0 else -normal
    return plane.normal.normalized()


# --- Building meshes ------------------------------------------------------------------

def _cap(mesh, loops, flip):
    """Fill closed loops (lists of verts in one plane; loops inside others
    become holes)."""
    if len(loops) == 1:
        loop = loops[0][::-1] if flip else loops[0]
        try:
            mesh.faces.new(loop)
        except ValueError:
            pass
        return
    edges = {}
    for loop in loops:
        for a, b in zip(loop, loop[1:] + loop[:1]):
            if a is not b:
                edge = mesh.edges.get((a, b)) or mesh.edges.new((a, b))
                edges[edge] = None
    made = bmesh.ops.triangle_fill(mesh, use_beauty=True, use_dissolve=False, edges=list(edges))["geom"]
    faces = [f for f in made if isinstance(f, bmesh.types.BMFace)]
    if faces:
        inside = set(faces)
        between = {e for f in faces for e in f.edges if e.is_manifold and all(g in inside for g in e.link_faces)}
        bmesh.ops.dissolve_limit(mesh, angle_limit=0.0001, verts=[], edges=list(between))


def _plane_groups(polylines):
    """Closed planar polylines grouped by plane: {key: [index, ...]}."""
    groups = {}
    for index, (points, closed, _) in enumerate(polylines):
        if not closed:
            continue
        found = plane_of(points)
        if found is None:
            continue
        origin, normal = found
        if normal.z < 0 or (abs(normal.z) < 1e-9 and (normal.y < 0 or (abs(normal.y) < 1e-9 and normal.x < 0))):
            normal = -normal
        for key in groups:
            other_normal, other_origin = key
            if (Vector(other_normal) - normal).length < 1e-6 and abs((origin - Vector(other_origin)).dot(normal)) < 1e-6:
                groups[key].append(index)
                break
        else:
            groups[(tuple(normal), tuple(origin))] = [index]
    return groups


def mark_sharp(mesh, angle=SHARP_ANGLE):
    for edge in mesh.edges:
        if len(edge.link_faces) == 2 and edge.calc_face_angle(0.0) > angle:
            edge.smooth = False
        elif len(edge.link_faces) != 2:
            edge.smooth = False


def extrusion_mesh(polylines, start, vector, solid):
    """A bmesh (world space): the polylines moved by ``start`` and swept by
    ``vector``; closed planar ones capped when ``solid``."""
    mesh = bmesh.new()
    rings = []
    for points, closed, smooth in polylines:
        bottom = [mesh.verts.new(tuple(Vector(tuple(p)) + start)) for p in points]
        top = [mesh.verts.new(tuple(Vector(tuple(p)) + start + vector)) for p in points]
        count = len(points)
        for i in range(count if closed else count - 1):
            j = (i + 1) % count
            face = mesh.faces.new((bottom[i], bottom[j], top[j], top[i]))
            face.smooth = smooth[i]
        rings.append((bottom, top))
    if solid:
        for (normal, _), indices in _plane_groups(polylines).items():
            if abs(vector.normalized().dot(Vector(normal))) < 1e-6:
                continue  # Swept within its own plane: no volume.
            _cap(mesh, [rings[i][0] for i in indices], flip=False)
            _cap(mesh, [rings[i][1] for i in indices], flip=True)
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    mark_sharp(mesh)
    return mesh


def to_point_mesh(polylines, apex, solid):
    """Cones from the polylines to ``apex`` (world space)."""
    mesh = bmesh.new()
    tip = mesh.verts.new(tuple(apex))
    rings = []
    for points, closed, smooth in polylines:
        ring = [mesh.verts.new(tuple(p)) for p in points]
        count = len(points)
        for i in range(count if closed else count - 1):
            face = mesh.faces.new((ring[i], ring[(i + 1) % count], tip))
            face.smooth = smooth[i]
        rings.append(ring)
    if solid:
        for _, indices in _plane_groups(polylines).items():
            _cap(mesh, [rings[i] for i in indices], flip=False)
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    mark_sharp(mesh)
    return mesh


def along_path_mesh(polylines, path, solid):
    """The polylines moved along ``path`` (N x 3, world space), not turned."""
    mesh = bmesh.new()
    path_points, path_closed, path_smooth = path
    offsets = [Vector(tuple(p - path_points[0])) for p in path_points]
    if path_closed:
        offsets.append(offsets[0])
    first_rings, last_rings = [], []
    for points, closed, smooth in polylines:
        rows = [[mesh.verts.new(tuple(Vector(tuple(p)) + offset)) for p in points] for offset in offsets]
        if path_closed:
            rows[-1] = rows[0]
        count = len(points)
        for r in range(len(rows) - 1):
            for i in range(count if closed else count - 1):
                j = (i + 1) % count
                face = mesh.faces.new((rows[r][i], rows[r][j], rows[r + 1][j], rows[r + 1][i]))
                face.smooth = smooth[i] or path_smooth[r % len(path_smooth)]
        first_rings.append(rows[0])
        last_rings.append(rows[-1])
    if solid and not path_closed:
        for _, indices in _plane_groups(polylines).items():
            _cap(mesh, [first_rings[i] for i in indices], flip=False)
            _cap(mesh, [last_rings[i] for i in indices], flip=True)
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    mark_sharp(mesh)
    return mesh


def planar_mesh(polylines, indices):
    mesh = bmesh.new()
    loops = [[mesh.verts.new(tuple(p)) for p in polylines[i][0]] for i in indices]
    _cap(mesh, loops, flip=False)
    for vert in [v for v in mesh.verts if not v.link_faces]:
        mesh.verts.remove(vert)
    mark_sharp(mesh)
    return mesh


def new_object(context, name, mesh, subd=False):
    """A mesh object from a world-space bmesh, with its origin at the center
    of its bounding box, in the current layer."""
    if not mesh.verts:
        mesh.free()
        return None
    coords = np.array([tuple(v.co) for v in mesh.verts])
    center = Vector(tuple((coords.min(axis=0) + coords.max(axis=0)) / 2))
    bmesh.ops.translate(mesh, vec=-center, verts=mesh.verts)
    obj = solids.new_mesh_object(context, name, mesh, Matrix.Translation(center))
    if subd:
        modifier = obj.modifiers.new("Subdivision", 'SUBSURF')
        modifier.levels = modifier.render_levels = 2
    return obj


def curve_extrusion(context, obj, vector):
    """Extrude a curve object along ``vector`` (Gumball Ctrl+drag), capped
    where closed and planar. Returns the new object."""
    polylines = curve_polylines(obj, settings.get(context).mesh_angle)
    mesh = extrusion_mesh(polylines, Vector(), vector, solid=True)
    result = new_object(context, "Extrusion", mesh)
    if result is not None:
        for collection in result.users_collection:
            collection.objects.unlink(result)
        for collection in obj.users_collection:
            collection.objects.link(result)
    return result


# --- Commands ---------------------------------------------------------------------------

class _FromCurves(GetPoints):
    """Base: select curves first, then pick or type."""

    what = "curves"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.curves = []
        self.polylines = []
        self.delete_input = False
        self.solid = True
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not any(o.type == 'CURVE' for o in context.selected_objects)

    def selection_done(self, context):
        self.curves = [o for o in context.selected_objects if o.type == 'CURVE']
        if not self.curves:
            command_line.echo(self.window, f"Select {self.what}", 'ERROR')
            return False
        angle = settings.get(context).mesh_angle
        self.polylines = [p for obj in self.curves for p in curve_polylines(obj, angle)]
        if not self.polylines:
            command_line.echo(self.window, "The curves have no points", 'ERROR')
            return False
        return self.after_selection(context)

    def after_selection(self, context):
        return True

    def toggles(self):
        return f"Solid={'Yes' if self.solid else 'No'} DeleteInput={'Yes' if self.delete_input else 'No'}"

    def toggle(self, name):
        for option in ("solid", "delete_input"):
            if option.replace("_", "").startswith(name):
                setattr(self, option, not getattr(self, option))
                return True
        return False

    def finish_with(self, context, mesh, name, subd=False):
        made = new_object(context, name, mesh, subd)
        if made is None:
            command_line.echo(self.window, "Nothing to make", 'ERROR')
            return None
        if self.delete_input:
            for obj in self.curves:
                bpy.data.objects.remove(obj)
        return made



class _Straight(GetPoints):
    """ExtrudeCrv and ExtrudeSrf: the distance along a direction (picked in
    any viewport, typed, 0 for the CPlane's elevation, w0 for the world's),
    with Direction, BothSides, Solid, DeleteInput and SetBasePoint."""

    last_distance = 1.0

    def invoke(self, context, event):
        self.both_sides = False
        self.solid = True
        self.delete_input = False
        self.stage = 'DISTANCE'  # Or 'DIRECTION' (two points), 'BASE'.
        self.direction = None
        self.base = None
        self.extrude_distance = None  # (GetPoints' own self.distance is a typed constraint.)
        return super().invoke(context, event)

    def start_distance(self, direction, base):
        self.direction, self.base = direction, base
        self.points = [base.copy()]

    def extra_options(self):
        return ""

    def distance_prompt(self):
        if self.stage == 'DIRECTION':
            return "Start of direction" if not self.points else "End of direction"
        if self.stage == 'BASE':
            return "Base point"
        both = "Yes" if self.both_sides else "No"
        return (f"Extrusion distance <{type(self).last_distance:.3f}> ( Direction BothSides={both} "
                f"Solid={'Yes' if self.solid else 'No'} DeleteInput={'Yes' if self.delete_input else 'No'} "
                f"SetBasePoint{self.extra_options()} )")

    def on_option(self, context, name):
        if "bothsides".startswith(name):
            self.both_sides = not self.both_sides
        elif "solid".startswith(name):
            self.solid = not self.solid
        elif "deleteinput".startswith(name):
            self.delete_input = not self.delete_input
        elif "direction".startswith(name):
            self.stage, self.points = 'DIRECTION', []
        elif "setbasepoint".startswith(name):
            self.stage, self.points = 'BASE', []
        else:
            return False
        return True

    def on_text(self, context, text):
        if text.lower() == "w0" and self.stage == 'DISTANCE':  # Rhino 8: the end at the world origin's elevation.
            return self._finish_distance((Vector() - self.base).dot(self.direction))
        return None

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if self.stage != 'DISTANCE' or self.base is None:
            return point
        return solids.elevator(context, SimpleNamespace(normal=self.direction), self.base, region, rv3d, mouse,
                               point, snapped)[0]

    def on_point(self, context):
        if self.stage == 'DIRECTION':
            if len(self.points) == 2:
                vector = self.points[1] - self.points[0]
                if vector.length < TOLERANCE:
                    self.points.pop()
                    command_line.echo(self.window, "The direction needs two different points", 'ERROR')
                    return None
                self.direction = vector.normalized()
                self.stage, self.points = 'DISTANCE', [self.base.copy()]
            return None
        if self.stage == 'BASE':
            self.base = self.points[-1].copy()
            self.stage, self.points = 'DISTANCE', [self.base.copy()]
            return None
        return self._finish_distance((self.points[-1] - self.base).dot(self.direction))

    def _finish_distance(self, distance):
        if abs(distance) < TOLERANCE:
            command_line.echo(self.window, "The distance must not be zero", 'ERROR')
            if len(self.points) > 1:
                self.points.pop()
            return True if self.stage == 'DISTANCE' else None
        self.extrude_distance = distance
        return FINISH

    def on_number(self, context, value):
        if self.stage != 'DISTANCE':
            return None
        if value == 0:  # Rhino 8: the end at the CPlane origin's elevation.
            return self._finish_distance((self.plane.origin - self.base).dot(self.direction))
        return self._finish_distance(value)

    def on_enter(self, context):
        if self.stage == 'DISTANCE':
            return self._finish_distance(type(self).last_distance)
        self.stage, self.points = 'DISTANCE', [self.base.copy()]
        self._show_prompt()
        return None

    def sweep(self, distance):
        """(start offset, sweep vector) for a distance."""
        vector = self.direction * distance
        return (-vector, vector * 2) if self.both_sides else (Vector(), vector)

    def outline(self, polylines, offset):
        """Line segments (N x 3) of polylines moved by ``offset``."""
        segments = []
        for points, closed, _ in polylines:
            moved = points + np.array(tuple(offset))
            ends = np.vstack([moved, moved[:1]]) if closed else moved
            segments.append(np.stack([ends[:-1], ends[1:]], axis=1).reshape(-1, 3))
        return np.concatenate(segments) if segments else np.empty((0, 3))

    def preview_outline(self):
        """The polylines whose start and end positions are drawn."""
        return []

    def preview(self):
        lines = []
        if self.stage == 'DISTANCE' and self.current is not None and self.base is not None:
            start, sweep = self.sweep((self.current - self.base).dot(self.direction))
            outline = self.preview_outline()
            ends = np.concatenate([self.outline(outline, start), self.outline(outline, start + sweep)])
            lines.append((ends.astype(np.float32), 'shape'))
            lines.append(([self.base, self.current], 'guide'))
        elif self.points and self.current is not None:
            lines.append(([self.points[0], self.current], 'guide'))
        return lines

    def snap_geometry(self):
        return [], []

    def create(self, context):
        type(self).last_distance = self.extrude_distance
        self.build(context, *self.sweep(self.extrude_distance))

    def build(self, context, start, sweep):
        raise NotImplementedError


class R2B_OT_extrude_crv(_Straight, _FromCurves, bpy.types.Operator):
    """Extrude curves in a straight line into surfaces (meshes)"""

    bl_idname = "r2b.extrude_crv"
    bl_label = "ExtrudeCrv"

    def invoke(self, context, event):
        self.subd = False
        return super().invoke(context, event)

    def after_selection(self, context):
        self.start_distance(default_direction(self.polylines, self.plane), Vector(tuple(self.polylines[0][0][0])))
        return True

    def extra_options(self):
        return f" Output={'SubD' if self.subd else 'Mesh'}"

    def prompt(self):
        if self.selecting:
            return "Select curves to extrude. Press Enter when done"
        return self.distance_prompt()

    def on_option(self, context, name):
        if "output".startswith(name):
            self.subd = not self.subd
            return True
        return super().on_option(context, name)

    def preview_outline(self):
        return self.polylines

    def build(self, context, start, sweep):
        if settings.get(context).record_history and not self.subd and len(self.curves) == 1:
            from . import history

            if history.extrude_curve(context, self.curves[0], start, sweep, self.solid) is not None:
                return
        mesh = extrusion_mesh(self.polylines, start, sweep, self.solid)
        self.finish_with(context, mesh, "Extrusion", subd=self.subd)


def selected_faces(context):
    """World-space faces to extrude: [[points], ...] and the objects and
    faces they come from. In Object Mode all faces of the selected meshes,
    in Edit Mode the selected faces."""
    faces, sources = [], []
    if context.mode == 'EDIT_MESH':
        for obj in context.objects_in_mode_unique_data:
            if obj.type != 'MESH':
                continue
            mesh = bmesh.from_edit_mesh(obj.data)
            chosen = [f for f in mesh.faces if f.select]
            faces += [[obj.matrix_world @ v.co for v in f.verts] for f in chosen]
            if chosen:
                sources.append((obj, [f.index for f in chosen]))
        return faces, sources
    for obj in context.selected_objects:
        if obj.type == 'MESH' and len(obj.data.polygons):
            faces += [[obj.matrix_world @ obj.data.vertices[i].co for i in p.vertices] for p in obj.data.polygons]
            sources.append((obj, None))
    return faces, sources


def face_extrusion_mesh(faces, start, sweep, solid):
    """A solid from faces (world space): the faces, a copy moved by
    ``sweep`` and walls along their boundary; walls only without ``solid``."""
    mesh = bmesh.new()
    lookup = {}

    def vert(point, offset):
        key = (tuple(round(c, 9) for c in point), offset)
        if key not in lookup:
            lookup[key] = mesh.verts.new(tuple(point + (start if offset == 0 else start + sweep)))
        return lookup[key]

    edges = {}
    for face in faces:
        keys = [tuple(round(c, 9) for c in p) for p in face]
        for a, b in zip(keys, keys[1:] + keys[:1]):
            edge = tuple(sorted((a, b)))
            edges[edge] = edges.get(edge, 0) + 1
    by_key = {tuple(round(c, 9) for c in p): p for face in faces for p in face}
    if solid:
        for face in faces:
            for offset in (0, 1):
                try:
                    mesh.faces.new([vert(p, offset) for p in face])
                except ValueError:
                    pass
    for face in faces:
        keys = [tuple(round(c, 9) for c in p) for p in face]
        for a, b in zip(keys, keys[1:] + keys[:1]):
            if edges[tuple(sorted((a, b)))] == 1:  # A boundary edge: a wall.
                pa, pb = by_key[a], by_key[b]
                try:
                    mesh.faces.new((vert(pa, 0), vert(pb, 0), vert(pb, 1), vert(pa, 1)))
                except ValueError:
                    pass
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
    mark_sharp(mesh)
    return mesh


class R2B_OT_extrude_srf(_Straight, bpy.types.Operator):
    """Extrude surfaces (open meshes, or faces picked with Ctrl+Shift) in a
    straight line into solids"""

    bl_idname = "r2b.extrude_srf"
    bl_label = "ExtrudeSrf"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode in {'OBJECT', 'EDIT_MESH'}

    def needs_selection(self, context):
        return not selected_faces(context)[0]

    def selection_done(self, context):
        self.faces, self.sources = selected_faces(context)
        if not self.faces:
            command_line.echo(self.window, "Select surfaces (meshes) or faces (Ctrl+Shift+click)", 'ERROR')
            return False
        points = np.array([tuple(p) for face in self.faces for p in face])
        found = plane_of(points)
        direction = self.plane.normal.normalized()
        if found is not None:
            direction = found[1] if found[1].dot(self.plane.normal) >= 0 else -found[1]
        self.start_distance(direction, Vector(tuple(points.mean(axis=0))))
        return True

    def prompt(self):
        if self.selecting:
            return "Select surfaces to extrude. Press Enter when done"
        return self.distance_prompt()

    def preview_outline(self):
        return [(np.array([tuple(p) for p in face]), True, ()) for face in self.faces]

    def build(self, context, start, sweep):
        mesh = face_extrusion_mesh(self.faces, start, sweep, self.solid)
        if new_object(context, "Extrusion", mesh) is None:
            command_line.echo(self.window, "Nothing to make", 'ERROR')
            return
        if self.delete_input:
            for obj, indices in self.sources:
                if indices is None:
                    bpy.data.objects.remove(obj)
                else:
                    edit = bmesh.from_edit_mesh(obj.data)
                    edit.faces.ensure_lookup_table()
                    bmesh.ops.delete(edit, geom=[edit.faces[i] for i in indices], context='FACES')
                    bmesh.update_edit_mesh(obj.data)


class R2B_OT_extrude_crv_to_point(_FromCurves, bpy.types.Operator):
    """Extrude curves to a point (cones and pyramids)"""

    bl_idname = "r2b.extrude_crv_to_point"
    bl_label = "ExtrudeCrvToPoint"

    def prompt(self):
        if self.selecting:
            return "Select curves to extrude. Press Enter when done"
        return f"Point to extrude to ( {self.toggles()} )"

    def on_option(self, context, name):
        return self.toggle(name)

    def on_point(self, context):
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def preview(self):
        if self.current is None:
            return []
        apex = np.array(tuple(self.current))
        ends = [p for points, _, _ in self.polylines for p in points[::max(1, len(points) // 16)]]
        segments = np.array([q for p in ends for q in (p, apex)], dtype=np.float32).reshape(-1, 3)
        return [(segments, 'shape')]

    def create(self, context):
        self.finish_with(context, to_point_mesh(self.polylines, self.points[-1], self.solid), "Extrusion")


class R2B_OT_extrude_crv_along_crv(_FromCurves, bpy.types.Operator):
    """Extrude curves along a path curve (moved, not turned)"""

    bl_idname = "r2b.extrude_crv_along_crv"
    bl_label = "ExtrudeCrvAlongCrv"

    def invoke(self, context, event):
        self.profiles = None
        result = super().invoke(context, event)
        if 'RUNNING_MODAL' in result and any(o.type == 'CURVE' for o in context.selected_objects):
            self.selection_done(context)  # Preselected curves are the profiles; then the path.
            self._show_prompt()
        return result

    def needs_selection(self, context):
        return True  # Two rounds: the profiles, then the path.

    def prompt(self):
        if self.selecting and self.profiles is None:
            return "Select curves to extrude. Press Enter when done"
        if self.selecting:
            return f"Select the path curve ( {self.toggles()} )"
        return ""

    def on_option(self, context, name):
        return self.toggle(name)

    def selection_done(self, context):
        if self.profiles is None:
            result = super().selection_done(context)
            if not result:
                return False
            self.profiles = (self.curves, self.polylines)
            for obj in context.selected_objects:
                obj.select_set(False)
            command_line.echo(self.window, "Select the path curve", 'INFO')
            return False  # Second round: the path.
        paths = [o for o in context.selected_objects if o.type == 'CURVE' and o not in self.profiles[0]]
        if not paths:
            command_line.echo(self.window, "Select a path curve", 'ERROR')
            return False
        path = curve_polylines(paths[0], settings.get(context).mesh_angle)
        if not path:
            return False
        self.curves, self.polylines = self.profiles
        self.path = path[0]
        return FINISH

    def create(self, context):
        self.finish_with(context, along_path_mesh(self.polylines, self.path, self.solid), "Extrusion")


class R2B_OT_planar_srf(_FromCurves, bpy.types.Operator):
    """Fill closed planar curves with flat faces (curves inside others make holes)"""

    bl_idname = "r2b.planar_srf"
    bl_label = "PlanarSrf"
    what = "closed planar curves"

    def invoke(self, context, event):
        result = super().invoke(context, event)
        self.delete_input = False
        return result

    def prompt(self):
        return "Select closed planar curves. Press Enter when done"

    def after_selection(self, context):
        groups = _plane_groups(self.polylines)
        if not groups:
            command_line.echo(self.window, "No closed planar curves", 'ERROR')
            return False
        for indices in groups.values():
            made = new_object(context, "PlanarSrf", planar_mesh(self.polylines, indices))
            if made is None:
                command_line.echo(self.window, "Could not fill the curves", 'ERROR')
        return FINISH

    def create(self, context):
        pass


class R2B_OT_cap(bpy.types.Operator):
    """Fill the planar holes of the selected meshes"""

    bl_idname = "r2b.cap"
    bl_label = "Cap"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not meshes:
            command_line.echo(context.window, "Select meshes to cap", 'ERROR')
            return {'CANCELLED'}
        filled = 0
        for obj in meshes:
            mesh = bmesh.new()
            mesh.from_mesh(obj.data)
            boundary = [e for e in mesh.edges if e.is_boundary]
            made = bmesh.ops.holes_fill(mesh, edges=boundary, sides=0)["faces"]
            bent = [f for f in made if not _is_flat(f)]
            if bent:
                bmesh.ops.delete(mesh, geom=bent, context='FACES_ONLY')
            filled += len(made) - len(bent)
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
            mesh.to_mesh(obj.data)
            mesh.free()
            obj.data.update()
        command_line.echo(context.window, f"Capped {filled} hole{'s' if filled != 1 else ''}")
        return {'FINISHED'}


def _is_flat(face):
    points = np.array([tuple(v.co) for v in face.verts])
    return plane_of(points) is not None


class R2B_OT_mesh_angle(GetPoints, bpy.types.Operator):
    """Set the largest angle between mesh edges along curves (how finely
    curves become meshes)"""

    bl_idname = "r2b.mesh_angle"
    bl_label = "MeshAngle"

    def prompt(self):
        return f"Mesh angle in degrees <{math.degrees(settings.get(bpy.context).mesh_angle):.1f}>"

    def on_number(self, context, value):
        if not 1 <= value <= 45:
            command_line.echo(self.window, "Between 1 and 45 degrees", 'ERROR')
            return True
        settings.get(context).mesh_angle = math.radians(value)
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def create(self, context):
        command_line.echo(self.window, f"Mesh angle {math.degrees(settings.get(context).mesh_angle):.1f}°")


_classes = (R2B_OT_extrude_crv, R2B_OT_extrude_srf, R2B_OT_extrude_crv_to_point, R2B_OT_extrude_crv_along_crv, R2B_OT_planar_srf,
            R2B_OT_cap, R2B_OT_mesh_angle)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
