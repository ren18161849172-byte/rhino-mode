# SPDX-License-Identifier: GPL-3.0-or-later
"""Trim and Split of meshes (used by Trim and Split in curve_edit.py).

Blender's Intersect (Knife) with the exact solver cuts the mesh where the
cutting objects meet it:

* Cutting meshes cut where their faces go through it.
* Cutting curves are projected onto it as Rhino does: a band of faces swept
  from the curve both ways along the projection cuts it. The projection is
  the view direction in plan parallel views (looking straight at the
  viewport's CPlane, like Top, Front and Right); in other views a planar
  curve goes perpendicular to its plane (a line: along the CPlane's normal)
  and other curves along the view. Trim's ExtendLines and ExtendArcs make a
  line cut as an endless line (a plane cut, like Blender's Bisect) and an
  arc as its whole circle.

The cuts divide the mesh's faces into regions: Trim removes the region
under the mouse, Split makes each region its own object, and both leave
the mesh open as Rhino leaves surfaces.

With Cap=Yes a closed mesh is cut as a solid (Blender's Bisect calls this
Fill): the cutting faces inside the mesh divide its volume into pieces.
Trim removes the piece under the mouse and closes the cut with the cutting
faces (so a curved cut closes exactly; a cutting mesh gives the same as a
boolean difference or intersection); Split makes each piece a closed
object. Pieces are found by going round each cut edge: the faces meeting
there, in their order around the edge, bound the wedges of space between
them.
"""

import math

import bmesh
import numpy as np

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import extrude

CUTTER = "r2b_cutter"  # Face attribute while cutting: 1 on the cutting faces.

# Meshes with more faces are cut when clicked only, not to show what Trim
# would remove while the mouse moves over them.
HOVER_FACES = 20000

# Highlights draw the edges of the faces, or only their border above this.
OUTLINE_EDGES = 20000

# Inside tests count crossings along these rays (the majority wins).
_RAYS = tuple(Vector(d).normalized() for d in ((0.5773, 0.5774, 0.5776), (-0.6231, 0.3417, 0.7035),
                                                (0.2113, -0.8366, 0.5054)))


def is_mesh(obj):
    return obj.type == 'MESH' and len(obj.data.polygons) > 0


def world_box(obj):
    """(min corner, max corner) of the object's bounding box in world space."""
    corners = np.array([tuple(obj.matrix_world @ Vector(c)) for c in obj.bound_box])
    return corners.min(axis=0), corners.max(axis=0)


def mesh_under_ray(context, origin, direction):
    """(mesh object, world point hit) of the first selectable mesh along a
    world-space ray, or (None, None)."""
    depsgraph = context.evaluated_depsgraph_get()
    selectable = {obj.as_pointer() for obj in context.selectable_objects if is_mesh(obj)}
    start = origin
    for _ in range(64):
        hit, location, _, _, obj, _ = context.scene.ray_cast(depsgraph, start, direction)
        if not hit:
            break
        if obj.original.as_pointer() in selectable:
            return obj.original, location
        start = location + direction * max(1e-6, 1e-6 * (location - origin).length)
    return None, None


def _box_corners(box):
    lo, hi = box
    return np.array([(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])


# --- Projecting cutting curves -------------------------------------------------------

def _flatness(points):
    """('line' or 'plane', unit normal or None), or (None, None) for a curve
    that is not planar."""
    if len(points) < 2:
        return None, None
    center = points.mean(axis=0)
    _, values, vectors = np.linalg.svd(points - center, full_matrices=False)
    size = max(float(np.ptp(points, axis=0).max()), 1e-12)
    if len(values) < 2 or values[1] <= 1e-9 * size:
        return 'line', None
    found = extrude.plane_of(points)
    return ('plane', found[1]) if found is not None else (None, None)


def projection(points, rv3d, plane):
    """The world direction (unit) a cutting curve through ``points`` (world
    space) is projected along, in the view ``rv3d`` with CPlane ``plane``."""
    view = (rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))).normalized()
    normal = plane.normal.normalized()
    if not rv3d.is_perspective and abs(view.dot(normal)) > 1.0 - 1e-6:
        return view
    kind, plane_normal = _flatness(points)
    if kind == 'plane':
        return plane_normal
    if kind == 'line':
        along = Vector(tuple(points[-1] - points[0]))
        if along.length > 0.0 and abs(along.normalized().dot(normal)) < 1.0 - 1e-6:
            return normal
    return view


def curve_bands(chains, rv3d, plane, max_angle):
    """Bands for world-space chains: [(points N x 3, closed, direction)]."""
    bands = []
    for chain in chains:
        points, closed, _ = extrude.polyline(chain, max_angle)
        if len(points) >= 2:
            bands.append((points, closed, projection(points, rv3d, plane)))
    return bands


def _band_meets(band, box):
    """Whether a band (endless along its direction) can meet the box."""
    points, _, direction = band
    d = Vector(direction)
    u = np.array(d.orthogonal().normalized())
    w = np.cross(np.array(d), u)
    corners = _box_corners(box)
    a = np.stack([corners @ u, corners @ w], axis=1)
    b = np.stack([points @ u, points @ w], axis=1)
    tol = 1e-9 * max(float(np.ptp(corners, axis=0).max()), float(np.ptp(points, axis=0).max()), 1.0)
    return bool(np.all(a.min(axis=0) <= b.max(axis=0) + tol) and np.all(b.min(axis=0) <= a.max(axis=0) + tol))


def _boxes_meet(a, b, tol):
    return bool(np.all(a[0] <= b[1] + tol) and np.all(b[0] <= a[1] + tol))


# --- Cutting --------------------------------------------------------------------------

def _add_band(mesh, band, box, to_local, layer):
    """Cutting faces swept from a band's polyline both ways along its
    direction, past the box."""
    points, closed, direction = band
    d = np.array(direction)
    corners = _box_corners(box)
    along = corners @ d
    at = points @ d
    reach = max(float(along.max() - at.min()), float(at.max() - along.min()), 0.0) * 1.1
    reach += 0.1 * float(np.linalg.norm(corners.max(axis=0) - corners.min(axis=0))) + 1e-6
    count = len(points)
    pairs = [(i, i + 1) for i in range(count - 1)]
    if closed and count > 2:
        pairs.append((count - 1, 0))
    made = {}

    def vert(i, side):
        if (i, side) not in made:
            made[(i, side)] = mesh.verts.new(to_local @ Vector(tuple(points[i] + side * reach * d)))
        return made[(i, side)]

    for i, j in pairs:
        segment = points[j] - points[i]
        length = float(np.linalg.norm(segment))
        if length <= 0.0 or float(np.linalg.norm(np.cross(segment / length, d))) < 1e-9:
            continue  # No length, or along the projection: no face.
        face = mesh.faces.new((vert(i, -1), vert(j, -1), vert(j, 1), vert(i, 1)))
        face[layer] = 1


def _add_mesh(mesh, cutter, depsgraph, to_local, layer):
    """The faces of a cutting mesh (with its modifiers) as cutting faces."""
    evaluated = cutter.evaluated_get(depsgraph)
    data = evaluated.to_mesh()
    try:
        matrix = to_local @ evaluated.matrix_world
        verts = [None] * len(data.vertices)
        for polygon in data.polygons:
            corners = []
            for i in polygon.vertices:
                if verts[i] is None:
                    verts[i] = mesh.verts.new(matrix @ data.vertices[i].co)
                corners.append(verts[i])
            try:
                face = mesh.faces.new(corners)
            except ValueError:
                continue  # A face repeating a point, or twice the same face.
            face[layer] = 1
    finally:
        evaluated.to_mesh_clear()


def _select_cutters(mesh, layer):
    for elements in (mesh.verts, mesh.edges, mesh.faces):
        for element in elements:
            element.select = False
    for face in mesh.faces:
        if face[layer]:
            face.select_set(True)


def _knife(context, mesh, several):
    """Blender's Intersect (Knife), exact, on a copy of ``mesh``: the selected
    (cutting) faces cut the others, all joined where they meet (and first
    each other, when there are ``several`` cutters). Returns the result."""
    data = bpy.data.meshes.new("R2B Trim")
    mesh.to_mesh(data)
    temp = bpy.data.objects.new("R2B Trim", data)
    view_layer = context.view_layer
    active = view_layer.objects.active
    selected = [obj for obj in view_layer.objects if obj.select_get()]
    context.scene.collection.objects.link(temp)
    try:
        for obj in selected:
            obj.select_set(False)
        temp.select_set(True)
        view_layer.objects.active = temp
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            if several:
                bpy.ops.mesh.intersect(mode='SELECT', separate_mode='NONE', solver='EXACT')
                edit = bmesh.from_edit_mesh(data)
                _select_cutters(edit, edit.faces.layers.int[CUTTER])
                bmesh.update_edit_mesh(data)
            bpy.ops.mesh.intersect(mode='SELECT_UNSELECT', separate_mode='NONE', solver='EXACT')
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
        result = bmesh.new()
        result.from_mesh(data)
    finally:
        bpy.data.objects.remove(temp)
        bpy.data.meshes.remove(data)
        for obj in selected:
            obj.select_set(True)
        view_layer.objects.active = active
    return result


def _imprint(mesh, layer, size):
    """Split faces of the mesh along cutting edges that run straight across
    them from corner to corner: Blender's exact Intersect (Knife) puts such
    a face back together (e.g. a box cut through two opposite edges)."""
    mesh.normal_update()
    tol = 1e-6 * size
    for edge in list(mesh.edges):
        if not edge.is_valid or not edge.link_faces or not all(f[layer] for f in edge.link_faces):
            continue
        a, b = edge.verts
        middle = (a.co + b.co) / 2
        shared = {f for f in a.link_faces if not f[layer]} & {f for f in b.link_faces if not f[layer]}
        for face in shared:
            if edge in face.edges or abs((middle - face.verts[0].co).dot(face.normal)) > tol:
                continue
            if bmesh.geometry.intersect_face_point(face, middle):
                bmesh.utils.face_split(face, a, b, use_exist=True)
                break


def cut(context, obj, meshes, bands):
    """Cut the mesh object ``obj`` by cutting mesh objects and curve bands
    (see ``curve_bands``). Returns a Cut, or None when none of them comes
    near the mesh."""
    box = world_box(obj)
    size = float(np.linalg.norm(box[1] - box[0])) or 1.0
    meshes = [m for m in meshes if m != obj and is_mesh(m) and _boxes_meet(world_box(m), box, 1e-6 * size)]
    bands = [band for band in bands if _band_meets(band, box)]
    if not meshes and not bands:
        return None
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    closed = bool(mesh.faces) and all(len(e.link_faces) == 2 for e in mesh.edges if e.link_faces)
    layer = mesh.faces.layers.int.get(CUTTER) or mesh.faces.layers.int.new(CUTTER)
    for face in mesh.faces:
        face[layer] = 0
    to_local = obj.matrix_world.inverted()
    depsgraph = context.evaluated_depsgraph_get()
    for cutter in meshes:
        _add_mesh(mesh, cutter, depsgraph, to_local, layer)
    for band in bands:
        _add_band(mesh, band, box, to_local, layer)
    _select_cutters(mesh, layer)
    result = _knife(context, mesh, len(meshes) + len(bands) > 1)
    mesh.free()
    _imprint(result, result.faces.layers.int[CUTTER], float(np.linalg.norm(np.array(obj.dimensions))) or 1.0)
    return Cut(obj, result, closed)


# --- The cut mesh ---------------------------------------------------------------------

class _UnionFind:
    def __init__(self, count):
        self.parent = list(range(count))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[b] = a


def _flood(faces, member, crosses, region, flipped):
    """Group ``faces`` into regions connected across edges where
    ``crosses(edge)``; ``region``/``flipped`` (lists by face index) get each
    face's region and whether it is oriented against the region's first
    face. Returns the regions (lists of faces)."""
    regions = []
    for seed in faces:
        if region[seed.index] is not None:
            continue
        number = len(regions)
        region[seed.index] = number
        flipped[seed.index] = False
        members = [seed]
        stack = [seed]
        while stack:
            face = stack.pop()
            for loop in face.loops:
                if not crosses(loop.edge):
                    continue
                for other in loop.edge.link_loops:
                    neighbor = other.face
                    if neighbor is face or region[neighbor.index] is not None or not member(neighbor):
                        continue
                    region[neighbor.index] = number
                    # Both loops running the same way along the edge: the
                    # faces are oriented against each other.
                    flipped[neighbor.index] = flipped[face.index] ^ (other.vert is loop.vert)
                    members.append(neighbor)
                    stack.append(neighbor)
        regions.append(members)
    return regions


def _radial(edge, faces):
    """Pairs ((face, side), (face, side)) bounding the same wedge of space
    between the faces around ``edge``; side 1 is the face's normal side."""
    v0, v1 = edge.verts
    axis = v1.co - v0.co
    if axis.length == 0.0:
        return []
    axis.normalize()
    u = axis.orthogonal().normalized()
    w = axis.cross(u)
    entries = []
    for loop in edge.link_loops:
        if loop.face not in faces:
            continue
        forward = loop.vert is v0  # The face's loop runs along +axis.
        inward = loop.face.normal.cross(axis if forward else -axis)
        entries.append((math.atan2(inward.dot(w), inward.dot(u)), loop.face.index, forward))
    entries.sort()
    pairs = []
    for i, (_, face, forward) in enumerate(entries):
        _, following, following_forward = entries[(i + 1) % len(entries)]
        # The wedge turning on (counterclockwise about the axis) from a face
        # is on its normal side if its loop runs along the axis.
        pairs.append(((face, 1 if forward else 0), (following, 0 if following_forward else 1)))
    return pairs


class Cut:
    """A mesh with the cutting faces knifed into it (object space)."""

    def __init__(self, obj, mesh, closed):
        self.obj = obj
        self.mesh = mesh
        self.closed = closed
        mesh.faces.ensure_lookup_table()
        mesh.faces.index_update()
        mesh.normal_update()
        layer = mesh.faces.layers.int[CUTTER]
        count = len(mesh.faces)
        self.cutting = [bool(f[layer]) for f in mesh.faces]
        cutting = self.cutting
        self.region = [None] * count
        self.flipped = [False] * count

        def target_crosses(edge):
            return not any(cutting[f.index] for f in edge.link_faces)

        def cutter_crosses(edge):
            faces = edge.link_faces
            return len(faces) == 2 and cutting[faces[0].index] and cutting[faces[1].index]

        targets = [f for f in mesh.faces if not cutting[f.index]]
        self.regions = _flood(targets, lambda f: not cutting[f.index], target_crosses, self.region, self.flipped)
        self.cutter_regions = _flood([f for f in mesh.faces if cutting[f.index]], lambda f: cutting[f.index],
                                     cutter_crosses, self.region, self.flipped)
        # Regions next to each other at a cut make up a part (a loose part
        # of the mesh as it was).
        parts = _UnionFind(len(self.regions))
        self.cut_regions = set()
        self.cut_edges = []
        mesh.edges.index_update()
        for edge in mesh.edges:
            faces = edge.link_faces
            if not any(cutting[f.index] for f in faces):
                continue
            self.cut_edges.append(edge.index)
            touching = [self.region[f.index] for f in faces if not cutting[f.index]]
            self.cut_regions.update(touching)
            for other in touching[1:]:
                parts.union(touching[0], other)
        self.part = [parts.find(r) for r in range(len(self.regions))]
        mesh.verts.index_update()
        polygons = [[v.index for v in f.verts] for f in targets]
        self._tree_faces = [f.index for f in targets]
        self.tree = BVHTree.FromPolygons([v.co.copy() for v in mesh.verts], polygons, all_triangles=False)
        self._cells = None
        self._outlines = {}

    def free(self):
        self.mesh.free()

    # --- Picking ---

    def region_at(self, origin, direction, near=None):
        """The region hit by a world-space ray (or nearest to the world
        point ``near`` when the ray misses), or None."""
        to_local = self.obj.matrix_world.inverted()
        hit = self.tree.ray_cast(to_local @ origin, (to_local.to_3x3() @ direction).normalized())
        if hit[0] is None and near is not None:
            hit = self.tree.find_nearest(to_local @ near)
        if hit[0] is None:
            return None
        return self.region[self._tree_faces[hit[2]]]

    def divided(self, region):
        """Whether the cuts divide the part of the mesh ``region`` is in."""
        part = self.part[region]
        return region in self.cut_regions and sum(1 for p in self.part if p == part) > 1

    # --- Solid pieces ---

    def _inside(self, point):
        size = max(self.obj.dimensions.length, 1e-9)
        votes = 0
        for direction in _RAYS:
            crossings, start = 0, point
            for _ in range(1000):
                hit = self.tree.ray_cast(start, direction)
                if hit[0] is None:
                    break
                crossings += 1
                start = hit[0] + direction * (1e-7 * size)
            votes += crossings % 2
        return votes >= 2

    def _sample(self, faces):
        """A point inside the largest of ``faces`` and that face."""
        face = max(faces, key=lambda f: f.calc_area())
        corners = [v.co for v in face.verts]
        best, point = -1.0, face.calc_center_median()
        for i in range(1, len(corners) - 1):
            area = (corners[i] - corners[0]).cross(corners[i + 1] - corners[0]).length
            center = (corners[0] + corners[i] + corners[i + 1]) / 3.0
            # A triangle of a fan; in a concave face it may stick out.
            if area > best and self._on_face(face, center):
                best, point = area, center
        return point, face

    @staticmethod
    def _on_face(face, point):
        return bmesh.geometry.intersect_face_point(face, point)

    def cells(self):
        """The solid pieces, or None when the mesh is not closed: {"inner":
        piece inside each region, "outside": the pieces outside the mesh,
        "sides": (piece on the back, piece on the normal side) of each
        cutting region inside the mesh (None outside)}. The sides of a
        region are those of its first face; ``flipped`` faces have them the
        other way round."""
        if self._cells is not None or not self.closed:
            return self._cells or None
        mesh = self.mesh
        count = len(self.regions)
        size = max(self.obj.dimensions.length, 1e-9)
        inside = []
        for faces in self.cutter_regions:
            point, _ = self._sample(faces)
            inside.append(self._inside(point))
        nodes = _UnionFind(2 * (count + len(self.cutter_regions)))

        def node(face_index, side):
            number = self.region[face_index] + (count if self.cutting[face_index] else 0)
            return 2 * number + (side ^ self.flipped[face_index])

        kept = {f for f in mesh.faces if not self.cutting[f.index] or inside[self.region[f.index]]}
        edges = set(self.cut_edges)
        for number, faces in enumerate(self.cutter_regions):
            if inside[number]:
                edges.update(e.index for f in faces for e in f.edges)
        mesh.edges.ensure_lookup_table()
        for index in edges:
            edge = mesh.edges[index]
            around = [f for f in edge.link_faces if f in kept]
            if not around:
                continue
            if len(around) == 2 and self.region[around[0].index] == self.region[around[1].index] \
                    and self.cutting[around[0].index] == self.cutting[around[1].index]:
                continue  # Inside one region: its sides go on.
            if len(around) == 1:
                nodes.union(node(around[0].index, 0), node(around[0].index, 1))
                continue
            for (a, side_a), (b, side_b) in _radial(edge, set(around)):
                nodes.union(node(a, side_a), node(b, side_b))
        # The side of each region facing out of the mesh, and the one in.
        inner, outside = [], set()
        for number, faces in enumerate(self.regions):
            point, face = self._sample(faces)
            out = (0 if self._inside(point + face.normal * (1e-5 * size)) else 1) ^ self.flipped[face.index]
            outside.add(nodes.find(2 * number + out))
            inner.append(nodes.find(2 * number + 1 - out))
        if any(cell in outside for cell in inner):
            self._cells = False  # Open after all (e.g. faces only touching).
            return None
        sides = [None] * len(self.cutter_regions)
        for number in range(len(self.cutter_regions)):
            if inside[number]:
                sides[number] = (nodes.find(2 * (count + number)), nodes.find(2 * (count + number) + 1))
        self._cells = {"inner": inner, "outside": outside, "sides": sides}
        return self._cells

    # --- Results ---

    def _keep(self, keep, flip=()):
        """A copy of the mesh with only the faces ``keep`` (indices), the
        faces ``flip`` turned over."""
        mesh = self.mesh.copy()
        mesh.faces.index_update()  # A copy keeps the order of the faces.
        faces = list(mesh.faces)
        if flip:
            bmesh.ops.reverse_faces(mesh, faces=[faces[i] for i in flip])
        bmesh.ops.delete(mesh, geom=[f for f in faces if f.index not in keep], context='FACES')
        return mesh

    def _finish(self, mesh):
        layer = mesh.faces.layers.int.get(CUTTER)
        if layer is not None:
            mesh.faces.layers.int.remove(layer)
        return mesh

    def _faces_of(self, regions):
        return {f.index for r in regions for f in self.regions[r]}

    def _caps(self, cells, cell, toward):
        """(faces, faces to flip) of the cutting regions with one side on
        ``cell``: facing it if ``toward``, else facing away."""
        faces, flip = set(), set()
        for number, sides in enumerate(cells["sides"]):
            if sides is None or (sides[0] == cell) == (sides[1] == cell):
                continue
            other = sides[1] if sides[0] == cell else sides[0]
            if other in cells["outside"]:
                continue
            want = (0 if sides[0] == cell else 1) if toward else (1 if sides[0] == cell else 0)
            for face in self.cutter_regions[number]:
                faces.add(face.index)
                if (1 ^ self.flipped[face.index]) != want:
                    flip.add(face.index)
        return faces, flip

    def removed(self, region, cap):
        """(removed faces, cap faces, cap faces to flip) when Trim removes
        ``region``; cap faces only with ``cap`` on a closed mesh."""
        cells = self.cells() if cap else None
        if not cells:
            return self._faces_of([region]), set(), set()
        cell = cells["inner"][region]
        gone = [r for r, inner in enumerate(cells["inner"]) if inner == cell]
        caps, flip = self._caps(cells, cell, toward=True)
        return self._faces_of(gone), caps, flip

    def trim(self, region, cap):
        """(new mesh or None, message): the mesh without ``region`` (with
        ``cap``: without the solid piece behind it, the cut closed)."""
        if not self.divided(region):
            return None, "The cutting objects do not cut the mesh there"
        note = ""
        if cap and not self.cells():
            note = "Cap needs a closed mesh: trimmed as an open surface"
        gone, caps, flip = self.removed(region, cap)
        part = self.part[region]
        if all(f in gone for r, p in enumerate(self.part) if p == part for f in self._faces_of([r])):
            return None, "The cutting objects do not cut the mesh into pieces there"
        keep = {f.index for f in self.mesh.faces if not self.cutting[f.index] and f.index not in gone} | caps
        return self._finish(self._keep(keep, flip)), note

    def split(self, cap):
        """([new meshes], message): one mesh per region (with ``cap``: per
        solid piece, closed); [] when nothing is divided."""
        note = ""
        cells = self.cells() if cap else None
        if cap and not cells:
            note = "Cap needs a closed mesh: split as an open surface"
        if cells:
            groups = {}
            for r, inner in enumerate(cells["inner"]):
                groups.setdefault(inner, []).append(r)
            # Pieces the cuts do not reach stay with the first piece.
            cut = [cell for cell, regions in groups.items() if any(self.divided(r) for r in regions)]
            if len(cut) < 2:
                return [], note
            rest = [r for cell, regions in groups.items() if cell not in cut for r in regions]
            pieces = []
            for number, cell in enumerate(cut):
                caps, flip = self._caps(cells, cell, toward=False)
                regions = groups[cell] + (rest if number == 0 else [])
                pieces.append(self._keep(self._faces_of(regions) | caps, flip))
        else:
            divided = [r for r in range(len(self.regions)) if self.divided(r)]
            if not divided:
                return [], note
            rest = [r for r in range(len(self.regions)) if r not in divided]
            groups = [[r] for r in divided]
            groups[0] += rest
            pieces = [self._keep(self._faces_of(regions)) for regions in groups]
        for piece in pieces[1:]:
            loose = [v for v in piece.verts if not v.link_faces]
            if loose:
                bmesh.ops.delete(piece, geom=loose, context='VERTS')
        return [self._finish(piece) for piece in pieces], note

    # --- Highlight ---

    def outline(self, faces):
        """World-space segments (2N x 3, float32) along the edges of
        ``faces`` (indices), only their border when there are many."""
        key = frozenset(faces)
        if key in self._outlines:
            return self._outlines[key]
        mesh = self.mesh
        chosen = [mesh.faces[i] for i in faces]
        edges = {e for f in chosen for e in f.edges}
        if len(edges) > OUTLINE_EDGES:
            edges = {e for e in edges if sum(1 for f in e.link_faces if f.index in faces) == 1}
        matrix = self.obj.matrix_world
        segments = np.array([tuple(matrix @ v.co) for e in edges for v in e.verts], dtype=np.float32)
        self._outlines[key] = segments.reshape(-1, 3)
        return self._outlines[key]
