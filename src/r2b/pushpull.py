# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino 8's PushPull for meshes (MODE feature): push a face into a solid to
take volume away, or pull it out to add volume.

The face is the one picked with Ctrl+Shift (Edit Mode: the selected faces of
one mesh), or clicked when the command asks; a face brings along the faces
joined to it in its plane. The distance is picked along the face's normal
(in any viewport, Osnap gives the height of the snapped point) or typed: a
typed distance counts toward the side of the face the mouse is on, as in
Rhino. The preview is green when volume is added, red when it is removed.

* Mode=Extend (default): the faces around follow (Blender's Extrude
  Manifold): pulling a box's top makes it taller, pushing a face in makes a
  pocket.
* Mode=Extrude: new side faces, the faces around stay as they were.

When the simple way cannot give a valid solid (pushed through the mesh or
past other faces), the volume is cut away or added with an exact boolean
instead (a prism over the face).
"""

from types import SimpleNamespace

import bmesh
import numpy as np

import bpy
from mathutils import Vector, bvhtree

from . import command_line, extrude, settings, solids, subobject
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-6


def _closed(mesh):
    return len(mesh.edges) > 0 and all(e.is_manifold for e in mesh.edges)


def _region_normal(faces):
    total = sum((f.normal * f.calc_area() for f in faces), Vector())
    return total.normalized() if total.length > 0 else Vector((0, 0, 1))


def _prism_faces(faces):
    return [[v.co.copy() for v in f.verts] for f in faces]


def _extend_or_extrude(mesh, faces, local, extend):
    """Move the faces by ``local`` with new sides (dissolved where they line
    up with the faces around when ``extend``)."""
    geom = faces
    if extend:
        # With the region's edges and points given too, the sides that line up
        # with the faces around are merged (Extrude Manifold).
        geom = list(dict.fromkeys([*faces, *(e for f in faces for e in f.edges), *(v for f in faces for v in f.verts)]))
    made = bmesh.ops.extrude_face_region(mesh, geom=geom, use_dissolve_ortho_edges=extend)["geom"]
    bmesh.ops.delete(mesh, geom=[f for f in faces if f.is_valid], context='FACES')
    bmesh.ops.translate(mesh, vec=local, verts=[v for v in made if isinstance(v, bmesh.types.BMVert)])
    bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)


def _reaches_through(mesh, faces, normal, depth):
    """Whether pushing the faces ``depth`` into the mesh meets other faces
    (rays from points just inside the region, against the normal)."""
    tree = bvhtree.BVHTree.FromBMesh(mesh)
    center = sum((f.calc_center_median() for f in faces), Vector()) / len(faces)
    starts = [center] + [v.co + (center - v.co) * 0.01 for f in faces for v in f.verts]
    epsilon = 1e-6 * max(depth, 1.0)
    for start in starts:
        hit = tree.ray_cast(start - normal * epsilon, -normal)
        if hit[0] is not None and hit[3] + epsilon <= depth + 1e-9:
            return True
    return False


def _boolean(context, obj, prism, operation):
    """Cut away or add a prism (bmesh in the object's space) exactly."""
    data = bpy.data.meshes.new("R2B PushPull")
    prism.to_mesh(data)
    prism.free()
    cutter = bpy.data.objects.new("R2B PushPull", data)
    cutter.matrix_world = obj.matrix_world
    context.scene.collection.objects.link(cutter)
    others = [m for m in obj.modifiers if m.show_viewport]
    for modifier in others:
        modifier.show_viewport = False
    boolean = obj.modifiers.new("R2B PushPull", 'BOOLEAN')
    boolean.object = cutter
    boolean.operation = operation
    boolean.solver = 'EXACT'
    try:
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        result = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
    finally:
        obj.modifiers.remove(boolean)
        for modifier in others:
            modifier.show_viewport = True
        bpy.data.objects.remove(cutter)
        bpy.data.meshes.remove(data)
    mesh = bmesh.new()
    mesh.from_mesh(result)
    mesh.to_mesh(obj.data)
    mesh.free()
    bpy.data.meshes.remove(result)
    obj.data.update()


# --- Regions cut by curves drawn on the face ------------------------------------

def _plane_coords(face):
    """A function giving 2D coordinates in the face's plane."""
    origin = face.verts[0].co.copy()
    x_axis = (face.verts[1].co - origin).normalized()
    y_axis = face.normal.cross(x_axis)
    return lambda p: ((p - origin).dot(x_axis), (p - origin).dot(y_axis))


def _inside(polygon, point):
    """Whether a 2D point is inside a polygon (even-odd rule)."""
    x, y = point
    inside = False
    for (x0, y0), (x1, y1) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def curve_loops_on_face(context, obj, face):
    """Closed curves lying on a face (bmesh, object space), inside it: lists
    of points in the object's space."""
    inverse = obj.matrix_world.inverted()
    to_2d = _plane_coords(face)
    outline = [to_2d(v.co) for v in face.verts]
    size = max(e.calc_length() for e in face.edges)
    loops = []
    for curve in context.visible_objects:
        if curve.type != 'CURVE':
            continue
        for points, closed, _ in extrude.curve_polylines(curve, settings.get(context).mesh_angle):
            if not closed or len(points) < 3:
                continue
            local = [inverse @ Vector(tuple(p)) for p in points]
            if (all(abs((p - face.verts[0].co).dot(face.normal)) <= 1e-5 * size for p in local)
                    and all(_inside(outline, to_2d(p)) for p in local)):
                loops.append(local)
    return loops


def split_by_loops(mesh, face, loops):
    """Cut ``face`` along closed loops lying inside it (each joined to the
    face's corners by two bridge edges, which stay as coplanar edges).
    Returns the loops' edges."""
    to_2d = _plane_coords(face)
    pieces = [face]
    barriers = set()
    for loop in loops:
        target = next((f for f in pieces if f.is_valid and _inside([to_2d(v.co) for v in f.verts], to_2d(loop[0]))),
                      None)
        if target is None:
            continue
        verts = [mesh.verts.new(tuple(p)) for p in loop]
        count = len(verts)
        edges = [mesh.edges.new((verts[i], verts[(i + 1) % count])) for i in range(count)]
        bridges, used = [], set()
        for i in (0, count // 2):
            corner = min((v for v in target.verts if v not in used), key=lambda v: (v.co - verts[i].co).length)
            used.add(corner)
            bridges.append(mesh.edges.new((verts[i], corner)))
        made = bmesh.utils.face_split_edgenet(target, edges + bridges)
        barriers.update(edges)
        pieces = [f for f in pieces if f.is_valid] + list(made)
    return barriers


def region_by_curves(mesh, face, loops, click, invert):
    """Split ``face`` by ``loops`` and return the faces of the region around
    ``click`` (object space), or of the rest of the face with ``invert``."""
    normal, origin = face.normal.copy(), face.verts[0].co.copy()
    size = max(e.calc_length() for e in face.edges)
    to_2d = _plane_coords(face)
    barriers = split_by_loops(mesh, face, loops)
    in_plane = [f for f in mesh.faces if (f.normal - normal).length < 1e-4
                and abs((f.verts[0].co - origin).dot(normal)) < 1e-6 * size]
    picked = next((f for f in in_plane if _inside([to_2d(v.co) for v in f.verts], to_2d(click))), None)
    if picked is None:
        return None
    region = subobject.coplanar_region(picked, barriers)
    if invert:
        region = subobject.coplanar_region(picked) - region
    return region


def push_pull(context, obj, face_indices, distance, extend):
    """Push (negative ``distance``) or pull the faces of ``obj`` along their
    normal; the object must be in Object Mode. Returns a message on failure."""
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.faces.ensure_lookup_table()
    faces = [mesh.faces[i] for i in face_indices]
    normal = _region_normal(faces)  # Local.
    world_normal = extrude.world_normal(obj.matrix_world, normal)
    local = obj.matrix_world.inverted().to_3x3() @ (world_normal * distance)
    depth = local.dot(normal)
    area = sum(f.calc_area() for f in faces)
    closed = _closed(mesh)
    before = mesh.calc_volume(signed=False) if closed else None
    prism_faces = _prism_faces(faces)
    coords = np.array([tuple(v.co) for v in mesh.verts]) if mesh.verts else np.zeros((1, 3))
    size = max(float(np.ptp(coords, axis=0).max()), 1e-3)
    removing = depth < 0
    if removing and _reaches_through(mesh, faces, normal, -depth):
        extend = None  # Past the other side or other faces: only the boolean is right.
    if extend is not None and (not removing or extend):
        _extend_or_extrude(mesh, faces, local, extend)
        expected = before + area * depth if closed else None
        if not closed or (_closed(mesh) and abs(mesh.calc_volume(signed=False) - expected) <= 1e-4 * max(before, 1e-9)):
            mesh.to_mesh(obj.data)
            mesh.free()
            obj.data.update()
            return None
    mesh.free()
    if not closed:
        return "PushPull needs a closed mesh to cut into"
    # A prism over the faces, reaching a little past them so it does not
    # share their plane.
    margin = normal * 1e-4 * size
    start, sweep = (margin, local - margin) if removing else (-margin, local + margin)
    prism = extrude.face_extrusion_mesh(prism_faces, start, sweep, solid=True)
    _boolean(context, obj, prism, 'DIFFERENCE' if removing else 'UNION')
    return None


class R2B_OT_push_pull(GetPoints, bpy.types.Operator):
    """Push a face into a solid or pull it out (Rhino's PushPull, for meshes)"""

    bl_idname = "r2b.push_pull"
    bl_label = "PushPull"
    last_distance = 1.0

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode in {'OBJECT', 'EDIT_MESH'}

    def invoke(self, context, event):
        self.extend = True
        self.invert = False
        self.loops = []  # Closed curves on the clicked face (object space).
        self.click = None  # Where the face was clicked (object space).
        self.target = None
        self.face_indices = []
        self.picking_face = False
        self.boundary = np.empty((0, 3))
        self.push_distance = None
        return super().invoke(context, event)

    # --- The face ---------------------------------------------------------------

    def selection_done(self, context):
        if context.mode == 'EDIT_MESH':
            for obj in context.objects_in_mode:
                if obj.type != 'MESH':
                    continue
                mesh = bmesh.from_edit_mesh(obj.data)
                chosen = [f for f in mesh.faces if f.select]
                if chosen:
                    self._use_faces(obj, chosen)
                    return True
        self.picking_face = True
        return True

    def picks_curves(self):
        return self.picking_face

    def on_pick(self, context, area, region, mouse):
        with context.temp_override(area=area, region=region):
            found = subobject.pick(bpy.context, mouse, faces_only=True)
        if found is None:
            command_line.echo(self.window, "Click a face of a mesh", 'ERROR')
            return None
        obj, _, index, location = found
        mesh, owned = subobject._edit_mesh(obj)
        mesh.faces.ensure_lookup_table()
        face = mesh.faces[index]
        self._use_faces(obj, list(subobject.coplanar_region(face)))
        if context.mode == 'OBJECT':
            self.loops = curve_loops_on_face(context, obj, face)
            self.click = obj.matrix_world.inverted() @ location
            self._loop_boundary(face)
        if owned:
            mesh.free()
        self.picking_face = False
        return None

    def _loop_boundary(self, face):
        """With curves on the face: the outline of the region that will move."""
        if not self.loops:
            return
        self.region_boundary = self.boundary
        to_2d = _plane_coords(face)
        containing = [loop for loop in self.loops if _inside([to_2d(p) for p in loop], to_2d(self.click))]
        matrix = self.target.matrix_world

        def segments(loop):
            return [tuple(matrix @ p) for a, b in zip(loop, loop[1:] + loop[:1]) for p in (a, b)]

        inner = [p for loop in containing[-1:] for p in segments(loop)]
        outer = [tuple(p) for p in self.region_boundary] + [p for loop in self.loops for p in segments(loop)]
        self.inside_boundary = np.array(inner, dtype=np.float64).reshape(-1, 3)
        self.outside_boundary = np.array(outer, dtype=np.float64).reshape(-1, 3)
        self.inside_click = bool(containing)
        self._update_boundary()

    def _update_boundary(self):
        if self.loops:
            self.boundary = self.inside_boundary if self.inside_click != self.invert else self.outside_boundary

    def _use_faces(self, obj, faces):
        self.target = obj
        self.face_indices = [f.index for f in faces]
        matrix = obj.matrix_world
        normal = _region_normal(faces)
        self.normal = extrude.world_normal(matrix, normal)
        chosen = set(faces)
        boundary = [(matrix @ e.verts[0].co, matrix @ e.verts[1].co) for f in faces for e in f.edges
                    if sum(g in chosen for g in e.link_faces) == 1]
        self.boundary = np.array([tuple(p) for pair in boundary for p in pair], dtype=np.float64).reshape(-1, 3)
        center = sum((matrix @ f.calc_center_median() for f in faces), Vector()) / len(faces)
        self.base = center
        self.points = [center.copy()]

    # --- The distance ------------------------------------------------------------

    def prompt(self):
        mode = "Extend" if self.extend else "Extrude"
        if self.picking_face or self.target is None:
            return f"Select a face to push or pull ( Mode={mode} )"
        invert = " Invert" if self.loops else ""
        return f"Distance to push or pull <{type(self).last_distance:.3f}> ( Mode={mode}{invert} )"

    def on_option(self, context, name):
        if "mode".startswith(name):
            self.extend = not self.extend
            return True
        if self.loops and "invert".startswith(name):
            self.invert = not self.invert
            self._update_boundary()
            return True
        return False

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if self.target is None:
            return point
        return solids.elevator(context, SimpleNamespace(normal=self.normal), self.base, region, rv3d, mouse,
                               point, snapped)[0]

    def _distance_to(self, point):
        return (point - self.base).dot(self.normal)

    def on_point(self, context):
        if self.target is None:
            self.points.pop()
            return None
        return self._finish(self._distance_to(self.points[-1]))

    def on_number(self, context, value):
        if self.target is None:
            return None
        side = -1 if self.current is not None and self._distance_to(self.current) < 0 else 1
        return self._finish(side * value)

    def on_enter(self, context):
        if self.target is None:
            return CANCEL
        return self._finish(type(self).last_distance)

    def _finish(self, distance):
        if abs(distance) < TOLERANCE:
            command_line.echo(self.window, "The distance must not be zero", 'ERROR')
            if len(self.points) > 1:
                self.points.pop()
            return True
        self.push_distance = distance
        return FINISH

    def preview(self):
        if self.target is None or self.current is None or not len(self.boundary):
            return []
        distance = self._distance_to(self.current)
        offset = np.array(tuple(self.normal * distance))
        moved = self.boundary + offset
        sides = np.stack([self.boundary[::2], moved[::2]], axis=1).reshape(-1, 3)
        kind = 'removal' if distance < 0 else 'growth'
        return [(np.concatenate([moved, sides]).astype(np.float32), kind), ([self.base, self.current], 'guide')]

    def snap_geometry(self):
        return [], []

    def create(self, context):
        type(self).last_distance = self.push_distance
        editing = context.mode == 'EDIT_MESH'
        if editing:
            bpy.ops.object.mode_set(mode='OBJECT')
        indices = self.face_indices
        if self.loops:
            indices = self._split(context)
        message = push_pull(context, self.target, indices, self.push_distance, self.extend) if indices else \
            "The curves on the face could not cut it"
        if message:
            command_line.echo(self.window, message, 'ERROR')
        if not editing:
            return
        if subobject.entered():
            subobject.leave(context)  # Back to the objects selected before, as in Rhino.
            return
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')


    def _split(self, context):
        """Cut the face along the curves on it; the faces of the chosen region."""
        obj = self.target
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        mesh.faces.ensure_lookup_table()
        face = next((mesh.faces[i] for i in self.face_indices
                     if _inside([_plane_coords(mesh.faces[i])(v.co) for v in mesh.faces[i].verts],
                                _plane_coords(mesh.faces[i])(self.loops[0][0]))), None)
        if face is None:
            mesh.free()
            return None
        region = region_by_curves(mesh, face, self.loops, self.click, self.invert)
        if not region:
            mesh.free()
            return None
        mesh.faces.index_update()
        indices = [f.index for f in region]
        mesh.to_mesh(obj.data)
        mesh.free()
        obj.data.update()
        return indices


_classes = (R2B_OT_push_pull,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
