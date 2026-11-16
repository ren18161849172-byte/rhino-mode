# SPDX-License-Identifier: GPL-3.0-or-later
"""Curve editing commands (MODE feature): Trim, Split, Join, Explode, Extend,
Fillet and Chamfer. Trim and Split also cut meshes (see mesh_trim.py, with
the Cap option to cut closed meshes as solids).

They work on the exact geometry of curve objects (curve_geom.py), in world
space, so object transforms do not matter:

* Trim: select cutting objects (Enter with nothing selected: all curves cut
  each other), then click the parts of curves to remove. The part under the
  mouse is highlighted; each click trims at once (Undo option). A curve does
  not cut itself, and the pieces of a trimmed cutter still cut.
* Split: select curves, then cutting objects; each curve is cut into pieces
  where they meet it. The Point option splits at picked points instead (the
  point slides along the curves; object snaps on them are used as they are).
* Join: curves whose ends meet become one curve (Rhino's polycurve, see
  polycurve.py), reversed where needed and closed if the ends meet; meshes
  join into one mesh (Blender's Join).
* Explode: a joined curve falls apart into its parts again, a polyline into
  lines, a curve object with several splines into one object per spline.
* Extend: select boundary objects, then click near the ends of curves to
  extend them to the first boundary they reach (preview while hovering).
  A typed length extends by that length instead; Enter without boundaries
  asks for the end point of each extension (dynamic). Type=Natural carries
  the curve on in its own shape (lines straight, arcs round the circle,
  free-form curves by their own polynomial); Type=Line adds a tangent line,
  Type=Arc a tangent arc (with the curve's curvature at the end, or through
  the picked point) and Type=Smooth a curvature-continuous cubic that
  straightens out; these make a curve a joined curve.
* Fillet: pick two curves; an exact arc of the radius touches both, and
  the curves are cut back (or extended in their own shape) to it, keeping
  the picked sides. Radius 0 meets them in a corner. Join joins the three.
* Chamfer: a line between the points at the two distances from where the
  curves meet, along each curve.

The first piece stays in the curve object (name, layer, material, ...);
other pieces become copies of it. Splines of a curve object with several
splines stay in it. Pieces are exact: lines and polylines stay poly
splines, Bezier splines stay Bezier, NURBS curves keep their degree (as
Bezier-mode NURBS), arcs and circles made by R2B stay arcs for Osnap, and
the parts of joined curves are kept.

Cutting objects and boundaries may be curves or meshes (where a curve goes
through a mesh's faces). Trim's options: ExtendLines and ExtendArcs cut
with cutting lines made endless and cutting arcs made whole circles, and
ApparentIntersections cuts where curves cross in the view (an exact
projective map of the curves, so the 3D curve is cut there).
"""

import math
from dataclasses import dataclass, replace

import numpy as np

import bpy
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import command_line, cplane, curve_geom, getpoint, mesh_trim, polycurve, selection, settings
from .getpoint import CANCEL, FINISH, GetPoints
from .projection import project


def is_curve(obj):
    return obj.type == 'CURVE' and len(obj.data.splines) > 0


def is_cutter(obj):
    """Curves and meshes can cut and be boundaries."""
    return is_curve(obj) or (obj.type == 'MESH' and len(obj.data.polygons) > 0)


def mesh_parameters(chain, obj, depsgraph):
    """Parameters where a world-space chain goes through a mesh object's
    faces: ray casts along the curve, then Newton's method onto the plane
    of the face hit (exact for the mesh's flat faces)."""
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        matrix = evaluated.matrix_world
        vertices = [matrix @ v.co for v in mesh.vertices]
        polygons = [tuple(p.vertices) for p in mesh.polygons]
    finally:
        evaluated.to_mesh_clear()
    if not polygons:
        return []
    tree = BVHTree.FromPolygons(vertices, polygons)
    steps = 1 if chain.degree == 1 else 32
    u = np.linspace(0.0, 1.0, steps + 1)
    found = []
    for i, segment in enumerate(chain.segments):
        points = curve_geom.segment_points(segment, u)
        for k in range(steps):
            a, b = Vector(points[k]), Vector(points[k + 1])
            length = (b - a).length
            if length < 1e-12:
                continue
            direction = (b - a) / length
            travelled = 0.0
            while travelled < length:
                location, normal, _, distance = tree.ray_cast(a + direction * travelled, direction, length - travelled)
                if location is None:
                    break
                travelled += distance
                t = u[k] + travelled / length * (u[k + 1] - u[k])
                for _ in range(20):  # Onto the face's plane, on the exact curve.
                    at, slope = curve_geom.segment_derivatives(segment, [t])
                    rate = float(np.dot(slope[0], tuple(normal)))
                    if abs(rate) < 1e-300:
                        break
                    step = float(np.dot(at[0] - np.array(location), tuple(normal))) / rate
                    t = min(max(t - step, 0.0), 1.0)
                    if abs(step) < 1e-14:
                        break
                found.append(i + t)
                travelled += 1e-7 * length + 1e-12
    return found


def projected(chain, rv3d):
    """The chain as the view shows it: its points mapped by the view's
    perspective matrix, flat (exact: the parameters stay the same)."""
    clip = chain.segments @ np.array(rv3d.perspective_matrix).T
    clip[..., 2] = 0.0
    return curve_geom.Chain(clip, chain.closed)


class Curves:
    """World-space chains of curve objects, cached during a command (clear
    after changing curves)."""

    def __init__(self):
        self._chains = {}
        self._samples = {}
        self._cuts = {}

    def clear(self):
        self._chains.clear()
        self._samples.clear()
        self._cuts.clear()

    def chains(self, obj):
        """[(spline index, chain)] of a curve object, in world space."""
        key = obj.as_pointer()
        if key not in self._chains:
            matrix = obj.matrix_world
            found = []
            for index, spline in enumerate(obj.data.splines):
                chain = curve_geom.from_spline(spline)
                if chain is not None:
                    found.append((index, chain.transformed(matrix)))
            self._chains[key] = found
        return self._chains[key]

    def samples(self, obj):
        """[(spline index, chain, points, parameters)] for picking."""
        key = obj.as_pointer()
        if key not in self._samples:
            self._samples[key] = [(index, chain, *curve_geom.sample(chain, 16)) for index, chain in self.chains(obj)]
        return self._samples[key]

    def under_mouse(self, objects, region, mouse, radius):
        """(object, spline index, chain, parameter) of the curve closest to
        ``mouse`` (region coordinates) within ``radius`` pixels, or None."""
        rv3d = region.data
        mouse = np.asarray(mouse, dtype=np.float64)
        best = None
        for obj in objects:
            for index, chain, points, params in self.samples(obj):
                xy = project(points, region, rv3d)
                a, b = xy[:-1], xy[1:]
                d = b - a
                with np.errstate(divide='ignore', invalid='ignore'):
                    s = np.clip(((mouse - a) * d).sum(axis=1) / (d * d).sum(axis=1), 0.0, 1.0)
                s[np.isnan(s)] = 0.0
                closest = a + d * s[:, None]
                distance = np.hypot(closest[:, 0] - mouse[0], closest[:, 1] - mouse[1])
                distance[np.isnan(distance)] = np.inf
                if not len(distance):
                    continue
                k = int(np.argmin(distance))
                if distance[k] <= radius and (best is None or distance[k] < best[0]):
                    t = params[k] + s[k] * (params[k + 1] - params[k])
                    best = (distance[k], obj, index, chain, float(t))
        return best[1:] if best else None

    def cuts(self, obj, index, chain, cutters, extend_lines=False, extend_arcs=False, view=None):
        """Where the cutters (curves, meshes) cut spline ``index`` of ``obj``
        (its ``chain``): sorted parameters. ``view``: a RegionView3D for
        apparent intersections. Cached: the cutters must stay the same until
        ``clear``."""
        key = (obj.as_pointer(), index, extend_lines, extend_arcs, view.as_pointer() if view is not None else None)
        if key not in self._cuts:
            self._cuts[key] = self._find_cuts(obj, index, chain, cutters, extend_lines, extend_arcs, view)
        return self._cuts[key]

    def _cutter_chains(self, cutter, extend_lines, extend_arcs, reach):
        """[(spline index, chain)] a cutting curve cuts with: endless lines
        and whole circles if asked."""
        found = self.chains(cutter)
        if len(found) == 1 and (extend_lines or extend_arcs):
            index, chain = found[0]
            if extend_lines and chain.degree == 1 and chain.count == 1 and not chain.closed:
                start, end = chain.start(), chain.end()
                along = (end - start) / max(np.linalg.norm(end - start), 1e-300)
                ends = np.array([(*(start - along * reach), 1.0), (*(end + along * reach), 1.0)])
                return [(index, curve_geom.Chain(ends[None]))]
            if extend_arcs:
                parts = polycurve.parts_of(cutter, index, curve_geom.from_spline(cutter.data.splines[index]))
                if len(parts) == 1 and parts[0].arc is not None:
                    arc = polycurve.transform_arc(parts[0].arc, cutter.matrix_world)
                    if arc is not None:
                        from . import curves

                        segments, _ = curves.arc_pieces_chain(Vector(arc["center"]), Vector(arc["x_axis"]),
                                                              Vector(arc["y_axis"]), arc["radius"], 0.0, 2 * math.pi)
                        return [(index, curve_geom.Chain(segments))]
        return found

    def _find_cuts(self, obj, index, chain, cutters, extend_lines, extend_arcs, view):
        params, overlaps = [], []
        reach = 10 * max([chain.size()] + [other.size() for cutter in cutters if is_curve(cutter)
                                           for _, other in self.chains(cutter)])
        flat = projected(chain, view) if view is not None else None
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for cutter in cutters:
            if cutter.type == 'MESH':
                params += mesh_parameters(chain, cutter, depsgraph)
                continue
            for other_index, other in self._cutter_chains(cutter, extend_lines, extend_arcs, reach):
                if cutter == obj and other_index == index:
                    continue  # A curve does not cut itself.
                if flat is not None:
                    other_flat = projected(other, view)
                    found, spans = curve_geom.chain_intersections(flat, other_flat,
                                                                  curve_geom.tolerance(flat, other_flat))
                else:
                    found, spans = curve_geom.chain_intersections(chain, other, curve_geom.tolerance(chain, other))
                params += found
                overlaps += spans
        return curve_geom.cut_parameters(chain, params, overlaps, curve_geom.tolerance(chain))


# --- Changing curves ----------------------------------------------------------------

def _blocks(data):
    """The collection of ``data`` in bpy.data (curves or meshes)."""
    return bpy.data.meshes if isinstance(data, bpy.types.Mesh) else bpy.data.curves


class Edits:
    """Changes made by a command: one record per step, for its Undo option.
    Changed objects get a copy of their curve (or mesh) data, so undoing a
    step puts the old data back; ``finish`` drops the old data no longer
    used."""

    def __init__(self):
        self.records = []

    def begin(self):
        self.records.append({"changed": [], "created": []})

    def _change(self, obj):
        """Give ``obj`` a copy of its curve data to change; returns it."""
        self.records[-1]["changed"].append((obj, obj.data, polycurve.tags_of(obj)))
        obj.data = obj.data.copy()
        return obj.data

    def _copy(self, obj, data):
        """A new object like ``obj`` (layer, material, ...) with ``data``."""
        copy = obj.copy()
        copy.data = data
        for collection in obj.users_collection:
            collection.objects.link(copy)
        copy.select_set(obj.select_get())
        self.records[-1]["created"].append(copy)
        return copy

    def replace(self, obj, index, ranges):
        """Replace spline ``index`` of ``obj`` by its pieces over parameter
        ``ranges``. Returns the new objects (pieces after the first, for
        objects with one spline)."""
        data = self._change(obj)
        spline = data.splines[index]
        chain = curve_geom.from_spline(spline)
        parts = polycurve.parts_of(obj, index, chain)
        pieces = [polycurve.cut(chain, parts, t0, t1) for t0, t1 in ranges]
        if len(data.splines) > 1:
            for piece, piece_parts in pieces:
                polycurve.write(data, piece, piece_parts, like=spline)
            data.splines.remove(spline)
            return []
        created = []
        for piece, piece_parts in pieces[1:]:
            piece_data = data.copy()
            piece_data.splines.clear()
            polycurve.write(piece_data, piece, piece_parts, like=spline)
            piece_obj = self._copy(obj, piece_data)
            polycurve.set_tags(piece_obj, piece_parts)
            created.append(piece_obj)
        piece, piece_parts = pieces[0]
        polycurve.write(data, piece, piece_parts, like=spline)
        data.splines.remove(spline)
        polycurve.set_tags(obj, piece_parts)
        return created

    def set_spline(self, obj, index, chain, parts):
        """Replace spline ``index`` of ``obj`` by ``chain`` made of ``parts``
        (local space)."""
        data = self._change(obj)
        spline = data.splines[index]
        polycurve.write(data, chain, parts, like=spline)
        data.splines.remove(spline)
        if len(data.splines) == 1:
            polycurve.set_tags(obj, parts)

    def separate(self, obj):
        """One object per spline of ``obj``; returns the new objects."""
        data = self._change(obj)
        created = []
        for index in range(1, len(data.splines)):
            piece_data = data.copy()
            for other, spline in reversed(list(enumerate(piece_data.splines))):
                if other != index:
                    piece_data.splines.remove(spline)
            created.append(self._copy(obj, piece_data))
        for spline in list(data.splines)[1:]:
            data.splines.remove(spline)
        for each in [obj] + created:
            polycurve.clear_tags(each)
        return created

    def set_mesh(self, obj, mesh):
        """Give the mesh object ``obj`` the bmesh ``mesh`` (object space,
        freed here) as its geometry."""
        data = self._change(obj)
        mesh.to_mesh(data)
        mesh.free()
        data.update()

    def mesh_piece(self, obj, mesh):
        """A new object like the mesh object ``obj`` with the bmesh ``mesh``
        (object space, freed here) as its geometry."""
        data = obj.data.copy()
        mesh.to_mesh(data)
        mesh.free()
        data.update()
        return self._copy(obj, data)

    def undo(self):
        """Undo the last step; returns the objects it had created."""
        record = self.records.pop()
        created = record["created"]
        for obj in created:
            data = obj.data
            bpy.data.objects.remove(obj)
            if data.users == 0:
                _blocks(data).remove(data)
        for obj, original, tags in reversed(record["changed"]):
            modified = obj.data
            obj.data = original
            if modified.users == 0:
                _blocks(modified).remove(modified)
            polycurve.restore(obj, tags)
        return created

    def finish(self):
        """Drop the old data; changed objects keep their data's name."""
        names = {}
        for record in self.records:
            for obj, original, _ in record["changed"]:
                blocks = _blocks(original)
                names.setdefault(obj, (original.name, blocks))
                if original.users == 0:
                    blocks.remove(original)
        for obj, (name, blocks) in names.items():
            if obj.data.name != name and name not in blocks:
                obj.data.name = name
        self.records = []


# --- Join and Explode --------------------------------------------------------------------

@dataclass
class _Piece:
    """A spline to join: its chain and parts in world space."""
    obj: object
    spline: object
    chain: curve_geom.Chain
    parts: list


def _join_groups(pieces, tol):
    """Lists of (piece index, reversed) whose ends meet one after another,
    as Rhino's Join: from each piece on, the closest end within ``tol``."""
    ends = [(piece.chain.start(), piece.chain.end()) for piece in pieces]
    unused = list(range(len(pieces)))
    groups = []
    while unused:
        group = [(unused.pop(0), False)]

        def head():
            i, flipped = group[0]
            return ends[i][1] if flipped else ends[i][0]

        def tail():
            i, flipped = group[-1]
            return ends[i][0] if flipped else ends[i][1]

        for at_tail in (True, False):
            while unused and not (len(group) > 1 and np.linalg.norm(head() - tail()) <= tol):
                target = tail() if at_tail else head()
                found = min(((np.linalg.norm(ends[j][which] - target), j, which) for j in unused for which in (0, 1)),
                            key=lambda item: item[0])
                if found[0] > tol:
                    break
                _, j, which = found
                unused.remove(j)
                if at_tail:
                    group.append((j, which == 1))  # Its end meets the tail: run it backwards.
                else:
                    group.insert(0, (j, which == 0))  # Its start meets the head: run it backwards.
        groups.append(group)
    return groups


def _joined(pieces, group, tol):
    """The world-space chain and parts of a group of pieces, end to end."""
    degree = max(part.degree for i, _ in group for part in pieces[i].parts)
    segments, parts = [], []
    for i, flipped in group:
        piece = pieces[i]
        own = max(part.degree for part in piece.parts)
        piece_segments = piece.chain.segments
        if piece_segments.shape[1] - 1 > own:
            piece_segments = curve_geom.reduce_degree(piece_segments, own)
        piece_segments = curve_geom.elevate_degree(piece_segments, degree).copy()
        piece_parts = list(piece.parts)
        if flipped:
            piece_segments = piece_segments[::-1, ::-1].copy()
            piece_parts.reverse()
        if segments:
            # Make the ends meet exactly (they are within tol).
            end = segments[-1][-1, -1]
            piece_segments[0, 0, :3] = end[:3] / end[3] * piece_segments[0, 0, 3]
        segments.append(piece_segments)
        parts += piece_parts
    joined = np.concatenate(segments)
    first, last = joined[0, 0], joined[-1, -1]
    closed = len(group) > 1 and bool(np.linalg.norm(first[:3] / first[3] - last[:3] / last[3]) <= tol)
    if closed:
        joined[-1, -1, :3] = first[:3] / first[3] * last[3]
    return curve_geom.Chain(joined, closed), parts


def join_curves(objects):
    """Join the curves of ``objects`` whose ends meet. Returns (curves
    joined, curves made)."""
    curves = [obj for obj in objects if obj.type == 'CURVE' and len(obj.data.splines)]
    for obj in curves:
        if obj.data.users > 1:
            obj.data = obj.data.copy()  # Do not change other objects using it.
    pieces = []
    for obj in curves:
        matrix = obj.matrix_world
        for index, spline in enumerate(obj.data.splines):
            chain = curve_geom.from_spline(spline)
            if chain is None:
                continue
            world = chain.transformed(matrix)
            if curve_geom.is_closed(world, curve_geom.tolerance(world)):
                continue  # Closed curves have no free ends.
            parts = [replace(part, arc=polycurve.transform_arc(part.arc, matrix))
                     for part in polycurve.parts_of(obj, index, chain)]
            pieces.append(_Piece(obj, spline, world, parts))
    if len(pieces) < 2:
        return 0, 0
    tol = curve_geom.tolerance(*[piece.chain for piece in pieces])
    groups = [group for group in _join_groups(pieces, tol) if len(group) > 1]
    written = {}  # Owner: parts of its joined spline.
    for group in groups:
        chain, parts = _joined(pieces, group, tol)
        owner = pieces[group[0][0]]
        inverse = owner.obj.matrix_world.inverted()
        parts = [replace(part, arc=polycurve.transform_arc(part.arc, inverse)) for part in parts]
        polycurve.write(owner.obj.data, chain.transformed(inverse), parts, like=owner.spline)
        written[owner.obj] = parts
    for group in groups:
        for i, _ in group:
            pieces[i].obj.data.splines.remove(pieces[i].spline)
    for obj in curves:
        if not len(obj.data.splines):
            data = obj.data
            bpy.data.objects.remove(obj)
            if data.users == 0:
                bpy.data.curves.remove(data)
        elif obj in written:
            if len(obj.data.splines) == 1:
                polycurve.set_tags(obj, written[obj])
            else:
                polycurve.clear_tags(obj)
    return sum(len(group) for group in groups), len(groups)


def explode(objects, edits):
    """Explode curves into their parts (see the module description) as one
    step of ``edits``. Returns (curves exploded, pieces)."""
    edits.begin()
    exploded = pieces = 0
    for obj in objects:
        if obj.type != 'CURVE' or not len(obj.data.splines):
            continue
        if len(obj.data.splines) > 1:
            pieces += 1 + len(edits.separate(obj))
            exploded += 1
            continue
        chain = curve_geom.from_spline(obj.data.splines[0])
        if chain is None:
            continue
        parts = polycurve.parts_of(obj, 0, chain)
        if len(parts) > 1:
            bounds = polycurve.edges(parts)
            ranges = list(zip(bounds[:-1], bounds[1:]))
        elif chain.degree == 1 and chain.count > 1:
            ranges = [(float(i), float(i + 1)) for i in range(chain.count)]
        else:
            continue  # One curve: nothing to explode.
        pieces += 1 + len(edits.replace(obj, 0, ranges))
        exploded += 1
    return exploded, pieces


# --- Extend ----------------------------------------------------------------------------------

class Extender:
    """Extending one end of a spline. The possible extension is a world-space
    chain (``candidate``) starting at that end; a parameter on it says how
    far to extend, and ``result`` gives the extended spline (local space)."""

    TYPES = ('NATURAL', 'LINE', 'ARC', 'SMOOTH')

    def __init__(self, obj, index, at_start, kind='NATURAL'):
        self.obj, self.index, self.at_start = obj, index, at_start
        chain = curve_geom.from_spline(obj.data.splines[index])
        parts = polycurve.parts_of(obj, index, chain)
        if at_start:  # Work on the end of the chain; turn it back in `result`.
            chain, parts = curve_geom.reversed_chain(chain), parts[::-1]
        self.chain, self.parts = chain, parts
        self.matrix = obj.matrix_world.copy()
        self.world = chain.transformed(self.matrix)
        last = parts[-1]
        self.end = self.world.end()
        direction = curve_geom.tangent(self.world, float(self.world.count))
        self.direction = direction / max(np.linalg.norm(direction), 1e-300)
        self.curvature, self.bend = self._end_curvature()
        straight = self.curvature * max(self.world.size(), 1e-12) < 1e-9
        if kind == 'NATURAL' or last.degree == 1 and kind != 'LINE':
            kind = 'ARC_NATURAL' if last.arc is not None else 'NATURAL'
        if kind == 'ARC' and last.arc is not None:
            kind = 'ARC_NATURAL'  # An arc's tangent arc of its curvature: the same circle.
        if kind in {'ARC', 'SMOOTH'} and straight:
            kind = 'LINE'
        if kind == 'LINE' and last.degree == 1:
            kind = 'NATURAL'  # A line's tangent line is the line.
        self.mode = {'ARC_NATURAL': 'ARC', 'ARC': 'TANGENT_ARC'}.get(kind, kind)
        if self.mode == 'ARC':
            self._arc_setup(last.arc)
        self._candidate = None

    def _end_curvature(self):
        """(curvature, unit normal toward the center of curvature) at the end
        of the world chain."""
        segment = self.world.segments[-1]
        step = 1e-4
        _, (first, before) = curve_geom.segment_derivatives(segment, [1.0, 1.0 - step])
        second = (first - before) / step
        speed = np.linalg.norm(first)
        if speed < 1e-300:
            return 0.0, np.zeros(3)
        normal = second - first * (second @ first) / (speed * speed)
        size = np.linalg.norm(normal)
        if size < 1e-300:
            return 0.0, np.zeros(3)
        return float(np.linalg.norm(np.cross(first, second)) / speed ** 3), normal / size

    def _arc_setup(self, arc):
        center, x_axis, y_axis, radius = (np.array(arc["center"]), np.array(arc["x_axis"]), np.array(arc["y_axis"]),
                                          float(arc["radius"]))
        self.frame = center, x_axis, y_axis, radius
        bounds = polycurve.edges(self.parts)
        self.arc_start = self._angle(curve_geom.evaluate(self.chain, bounds[-2]))
        self.arc_end = self._angle(self.chain.end())
        along = -math.sin(self.arc_end) * x_axis + math.cos(self.arc_end) * y_axis
        self.ccw = curve_geom.tangent(self.chain, float(self.chain.count)) @ along >= 0
        sweep = (self.arc_end - self.arc_start) * (1 if self.ccw else -1) % (2 * math.pi)
        self.sweep = sweep if sweep > 1e-9 else 2 * math.pi
        self.room = 2 * math.pi - self.sweep - 1e-3  # It must not close on itself.

    def _angle(self, local_point):
        center, x_axis, y_axis, _ = self.frame
        offset = local_point - center
        return math.atan2(offset @ y_axis, offset @ x_axis)

    def _arc_chain(self, start, sweep):
        """Local chain of an arc of the circle from ``start`` (radians)."""
        from . import nurbs

        center, x_axis, y_axis, radius = self.frame
        points, weights = nurbs.arc_points(Vector(center), Vector(x_axis), Vector(y_axis), radius, start,
                                           sweep if self.ccw else -sweep)
        control = np.array([(*(p * w), w) for p, w in zip(points, weights)])
        knots = curve_geom.blender_knots(len(control), 3, endpoint=True, bezier=True)
        return curve_geom.Chain(curve_geom.nurbs_segments(control, 3, knots))

    def _tangent_arc_chain(self, radius, normal, sweep):
        """World chain of an arc leaving the end tangentially, bending toward
        ``normal``, with its Osnap tag."""
        from . import curves

        center = Vector(self.end) + Vector(normal) * radius
        x_axis = (Vector(self.end) - center).normalized()
        segments, tag = curves.arc_pieces_chain(center, x_axis, Vector(self.direction), radius, 0.0, sweep)
        chain = curve_geom.Chain(segments)
        chain.arc_tag = tag
        return chain

    def _smooth_chain(self, length, end=None):
        """World chain of a cubic leaving the end with its tangent and
        curvature, straightening out over about ``length`` (to ``end``)."""
        h = length / 3
        start = np.asarray(self.end)
        first = start + self.direction * h
        second = first + self.direction * h + self.bend * (1.5 * self.curvature * h * h)
        last = second + self.direction * h if end is None else np.asarray(end)
        control = np.array([(*p, 1.0) for p in (start, first, second, last)])
        return curve_geom.Chain(control[None])

    def candidate(self, reach):
        """World chain of the extension, at least ``reach`` long where possible."""
        if self.mode == 'TANGENT_ARC':
            if self._candidate is None:
                self._candidate = self._tangent_arc_chain(1 / self.curvature, self.bend, 2 * math.pi - 1e-3)
            return self._candidate
        if self.mode == 'SMOOTH':
            return self._smooth_chain(reach)
        if self.mode == 'ARC':
            if self._candidate is None:
                if self.room <= 0:
                    return None
                self._candidate = self._arc_chain(self.arc_end, self.room).transformed(self.matrix)
            return self._candidate
        if self.mode == 'LINE':
            ends = np.array([(*self.end, 1.0), (*(self.end + self.direction * reach), 1.0)])
            return curve_geom.Chain(ends[None])
        segment = self.world.segments[-1]
        speed = float(np.linalg.norm(curve_geom.segment_derivatives(segment, [1.0])[1][0]))
        s_max = min(1.0 + reach / max(speed, 1e-300), 1e3)
        far = curve_geom.extrapolate(segment, s_max)
        candidate = curve_geom.Chain(curve_geom.sub_segment(far, 1.0 / s_max, 1.0)[None])
        candidate.s_max = s_max  # The end segment's parameter at the candidate's end.
        return candidate

    def to_boundaries(self, curves, boundaries):
        """(world candidate, parameter) where the extension first meets a
        boundary, or None."""
        corners = [chain.segments[..., :3].reshape(-1, 3) / chain.segments[..., 3:].reshape(-1, 1)
                   for obj in boundaries if is_curve(obj) for _, chain in curves.chains(obj)]
        corners += [np.array([tuple(obj.matrix_world @ Vector(c)) for c in obj.bound_box])
                    for obj in boundaries if obj.type == 'MESH']
        if not corners:
            return None
        reach = 2.0 * float(np.linalg.norm(np.concatenate(corners) - self.end, axis=1).max()) + self.world.size()
        candidate = self.candidate(reach)
        if candidate is None:
            return None
        tol = curve_geom.tolerance(self.world)
        found = []
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for obj in boundaries:
            if obj.type == 'MESH':
                found += mesh_parameters(candidate, obj, depsgraph)
                continue
            for index, chain in curves.chains(obj):
                if obj == self.obj and index == self.index:
                    continue
                params, overlaps = curve_geom.chain_intersections(candidate, chain, max(tol, curve_geom.tolerance(chain)))
                found += params + [span[0] for span in overlaps]
        found = [t for t in found if curve_geom.length(candidate, 0.0, t) > tol]
        return (candidate, min(found)) if found else None

    def by_length(self, distance):
        """(world candidate, parameter) to extend by ``distance``, or None."""
        reach = distance * 1.5
        for _ in range(12):
            candidate = self.candidate(reach)
            if candidate is None:
                return None
            t = curve_geom.parameter_at_length(candidate, distance)
            if t is not None:
                return candidate, t
            if self.mode in {'ARC', 'TANGENT_ARC'}:
                return None  # Longer than the rest of the circle.
            reach *= 2
        return None

    def to_point(self, point):
        """(world candidate, parameter) of the extension point closest to a
        world-space point."""
        point = np.asarray(point, dtype=np.float64)
        if self.mode == 'TANGENT_ARC':
            # The tangent arc through the point.
            from . import curves

            normal = Vector(np.cross(self.direction, point - self.end))
            if normal.length < 1e-12:
                return None
            piece, _ = curves.tangent_arc(Vector(self.end), Vector(self.direction), Vector(point), normal.normalized())
            if piece[0] != 'ARC':
                return None
            segments, tag = curves.arc_pieces_chain(*piece[1:])
            candidate = curve_geom.Chain(segments)
            candidate.arc_tag = tag
            return candidate, float(candidate.count)
        if self.mode == 'SMOOTH':
            candidate = self._smooth_chain(float(np.linalg.norm(point - self.end)), end=point)
            return candidate, 1.0
        reach = 2.0 * float(np.linalg.norm(point - self.end)) + self.world.size()
        candidate = self.candidate(reach)
        if candidate is None:
            return None
        return candidate, curve_geom.closest_parameter(candidate, point)

    def result(self, candidate, t):
        """(local chain, parts) of the spline extended to parameter t of the
        candidate, in the spline's own direction."""
        chain, parts = self.chain, list(self.parts)
        degree = chain.degree
        if self.mode == 'NATURAL':
            segments = chain.segments.copy()
            segments[-1] = curve_geom.extrapolate(segments[-1], 1.0 + t * (candidate.s_max - 1.0))
            if np.any(segments[-1][:, 3] <= 0):
                raise ValueError("This curve cannot be extended that far in its own shape: use Type=Line")
            if parts[-1].arc is not None:
                parts[-1] = replace(parts[-1], arc=None)
            result = curve_geom.Chain(segments)
        else:
            inverse = np.array(self.matrix.inverted())
            point = inverse[:3, :3] @ curve_geom.evaluate(candidate, t) + inverse[:3, 3]
            bounds = polycurve.edges(parts)
            if self.mode == 'ARC':
                if self.ccw:
                    more = (self._angle(point) - self.arc_end) % (2 * math.pi)
                else:
                    more = (self.arc_end - self._angle(point)) % (2 * math.pi)
                sweep = self.sweep + min(more, self.room)
                arc = self._arc_chain(self.arc_start, sweep)
                head = chain.segments[:int(bounds[-2])]
                segments = np.concatenate([head, curve_geom.elevate_degree(arc.segments, degree)])
                start = self.arc_start if self.ccw else self.arc_start - sweep
                tag = dict(parts[-1].arc, start=start % (2 * math.pi), end=start % (2 * math.pi) + sweep)
                parts[-1] = replace(parts[-1], count=arc.count, arc=tag)
            elif self.mode == 'LINE':
                end = chain.end()
                line = np.array([[(*end, 1.0), (*point, 1.0)]])
                segments = np.concatenate([chain.segments, curve_geom.elevate_degree(line, degree)])
                parts.append(polycurve.Part(1, 1, 'POLY'))
            else:  # A tangent arc or a smooth cubic: a new part, from the world candidate.
                added = curve_geom.piece(candidate, 0.0, t).transformed(self.matrix.inverted())
                top = max(degree, added.degree)
                tail = curve_geom.elevate_degree(added.segments, top).copy()
                end = chain.segments[-1, -1]
                tail[0, 0] = end / end[3] * tail[0, 0, 3]  # Exactly at the end.
                segments = np.concatenate([curve_geom.elevate_degree(chain.segments, top), tail])
                if self.mode == 'TANGENT_ARC':
                    tag = polycurve.sub_arc(candidate.arc_tag, candidate, 0.0, t)
                    tag = polycurve.transform_arc(tag, self.matrix.inverted())
                    parts.append(polycurve.Part(added.count, 2, 'NURBS', tag))
                else:
                    parts.append(polycurve.Part(added.count, 3, 'NURBS'))
            result = curve_geom.Chain(segments)
        if self.at_start:
            result, parts = curve_geom.reversed_chain(result), parts[::-1]
        return result, parts


# --- Corners: Fillet and Chamfer --------------------------------------------------

class Lengthened:
    """A curve made longer at both ends in its own shape (Extend's Natural),
    so that a corner may lie beyond its ends. Parameter v: [0, a] the start
    extension (run backwards), [a, a + n] the curve, then the end extension."""

    def __init__(self, obj, index, reach):
        self.obj, self.index = obj, index
        self.world = dict(Curves().chains(obj))[index]
        self.n = self.world.count
        self.start = Extender(obj, index, True, 'NATURAL')
        self.end = Extender(obj, index, False, 'NATURAL')
        self.start_candidate = self.start.candidate(reach)
        self.end_candidate = self.end.candidate(reach)
        pieces = [self.world.segments]
        self.a = 0
        if self.start_candidate is not None:
            pieces.insert(0, curve_geom.reversed_chain(self.start_candidate).segments)
            self.a = self.start_candidate.count
        if self.end_candidate is not None:
            pieces.append(self.end_candidate.segments)
        degree = max(piece.shape[1] - 1 for piece in pieces)
        self.chain = curve_geom.Chain(np.concatenate([curve_geom.elevate_degree(p, degree) for p in pieces]))

    def kept(self, v, pick):
        """(local chain, parts) of the curve cut back or extended to v,
        keeping the side of the picked parameter ``pick`` (both virtual)."""
        a, n, eps = self.a, self.n, curve_geom.PARAM_EPS
        spline = self.obj.data.splines[self.index]
        chain = curve_geom.from_spline(spline)
        parts = polycurve.parts_of(self.obj, self.index, chain)
        if pick < v:  # Keep the start side, up to v.
            if v - a <= eps:
                return None
            if v <= a + n + eps:
                return polycurve.cut(chain, parts, 0.0, min(v - a, n))
            return self.end.result(self.end_candidate, v - (a + n))
        if v >= a + n - eps:
            return None
        if v >= a - eps:
            return polycurve.cut(chain, parts, max(v - a, 0.0), float(n))
        return self.start.result(self.start_candidate, a - v)


def corner_between(first, second, tol):
    """Virtual parameters (v1, v2) where two lengthened curves meet, nearest
    the picks, or None. ``first``, ``second``: (Lengthened, picked point,
    virtual parameter of the pick). A lengthened arc may pass a point twice
    (round its circle from both ends): the pass nearest the pick counts."""
    (one, pick1, at1), (two, pick2, at2) = first, second
    params, overlaps = curve_geom.chain_intersections(one.chain, two.chain, tol)
    params += [span[0] for span in overlaps]
    if not params:
        return None

    def score(v):
        point = curve_geom.evaluate(one.chain, v)
        return np.linalg.norm(point - pick1) + np.linalg.norm(point - pick2)

    least = min(score(v) for v in params)
    best = min((v for v in params if score(v) <= least + 10 * tol), key=lambda v: abs(v - at1))
    point = curve_geom.evaluate(one.chain, best)
    others, overlaps = curve_geom.chain_intersections(two.chain, one.chain, tol)
    others = [v for v in others + [span[0] for span in overlaps]
              if np.linalg.norm(curve_geom.evaluate(two.chain, v) - point) <= 10 * tol]
    if not others:
        others = [curve_geom.closest_parameter(two.chain, point)]
    return best, min(others, key=lambda v: abs(v - at2))


def _corner_plane(first_chain, t1, second_chain, t2, fallback):
    """A plane through both curves (from their directions at the picks), or
    the viewport's CPlane if they run parallel."""
    from . import cplane

    a = Vector(curve_geom.tangent(first_chain, t1)).normalized()
    b = Vector(curve_geom.tangent(second_chain, t2)).normalized()
    normal = a.cross(b)
    if normal.length < 1e-6:
        return fallback
    normal.normalize()
    return cplane.CPlane("Corner", Vector(curve_geom.evaluate(first_chain, t1)), a, normal.cross(a))


class _CornerCommand:
    """Fillet and Chamfer: pick two curves; their picked sides are kept."""

    noun = ""

    def invoke(self, context, event):
        self.first = None
        self.join = False
        self.trim = True
        self.asking = None  # An option waiting for its number.
        return super().invoke(context, event)

    def picks_curves(self):
        return self.asking is None

    def _yes(self, value):
        return "Yes" if value else "No"

    def prompt(self):
        if self.asking:
            return self.asking_prompt()
        which = "first" if self.first is None else "second"
        return f"Select {which} curve to {self.noun} ( {self.size_options()} Join={self._yes(self.join)} " \
               f"Trim={self._yes(self.trim)} )"

    def on_option(self, context, name):
        if "join".startswith(name):
            self.join = not self.join
            return True
        if "trim".startswith(name):
            self.trim = not self.trim
            return True
        return self.size_option(name)

    def on_enter(self, context):
        if self.asking:
            self.asking = None
            return None
        return CANCEL

    def on_pick(self, context, area, region, mouse):
        from . import cplane

        targets = [obj for obj in context.selectable_objects if is_curve(obj)]
        hit = self.curves.under_mouse(targets, region, mouse, _pick_radius(context))
        if hit is None:
            return None
        obj, index, chain, t = hit
        if curve_geom.is_closed(chain, curve_geom.tolerance(chain)):
            command_line.echo(self.window, f"Pick open curves to {self.noun}", 'ERROR')
            return None
        picked = (obj, index, chain, t, curve_geom.evaluate(chain, t))
        if self.first is None:
            self.first = picked
            self.highlight = [curve_geom.sample(chain, 24)[0]]
            return None
        if obj == self.first[0] and index == self.first[1]:
            command_line.echo(self.window, "Pick a second, different curve", 'ERROR')
            return None
        plane = _corner_plane(self.first[2], self.first[3], chain, t, cplane.for_area(self.window.screen, area))
        message = self.make(context, self.first, picked, plane)
        if message:
            command_line.echo(self.window, message, 'ERROR')
            self.first = None
            self.highlight = []
            return None
        return FINISH

    def preview(self):
        return [(points, 'highlight') for points in self.highlight]

    def lengthened(self, first, second):
        reach = 2.0 * (first[2].size() + second[2].size())
        return Lengthened(first[0], first[1], reach), Lengthened(second[0], second[1], reach)

    @staticmethod
    def kept_side(v, pick, corner):
        """A parameter on the side of v that is kept: the picked side of the
        corner, away from it. The pick itself does not tell when it is
        between the corner and v (nearer the corner than the fillet
        touches): the stub at the corner would be kept. Without a corner
        (``corner`` None), or with v at it or across it, the pick's side."""
        if corner is None or (v - corner) * (pick - corner) <= 0:
            return pick
        return v + (v - corner)

    def finish_corner(self, context, first, second, one, two, v1, v2, made, corner=None):
        """Cut back or extend both curves to v1, v2 (keeping the picked
        sides of the ``corner``, the parameters where they meet), add
        ``made`` (the fillet or chamfer object, or None), join."""
        corner = corner or (None, None)
        kept1 = one.kept(v1, self.kept_side(v1, one.a + first[3], corner[0]))
        kept2 = two.kept(v2, self.kept_side(v2, two.a + second[3], corner[1]))
        if self.trim and (kept1 is None or kept2 is None):
            if made is not None:
                bpy.data.objects.remove(made)
            return "The corner is not on the picked sides of the curves"
        self.edits.begin()
        if self.trim:
            self.edits.set_spline(first[0], first[1], *kept1)
            self.edits.set_spline(second[0], second[1], *kept2)
        self.edits.finish()
        if self.join:
            context.view_layer.update()
            join_curves([first[0]] + ([made] if made is not None else []) + [second[0]])
        return None

    def create(self, context):
        pass


# --- Commands -------------------------------------------------------------------------

def _pick_radius(context):
    return selection.PICK_RADIUS * context.preferences.system.ui_scale


class _CurveCommand:
    """Shared by the curve editing commands (mix in before GetPoints)."""

    def invoke(self, context, event):
        self.curves = Curves()
        self.edits = Edits()
        self.highlight = []
        self.mesh_highlight = []  # (segments, kind) of a mesh's faces.
        self.mesh_cuts = {}
        return super().invoke(context, event)

    def preview(self):
        return [(points, 'highlight') for points in self.highlight] + self.mesh_highlight

    def cut_mesh(self, context, obj, cutters, rv3d, plane, extend_lines=False, extend_arcs=False):
        """A mesh_trim.Cut of the mesh object ``obj`` by the cutting objects
        (curves projected in the view ``rv3d`` with CPlane ``plane``), or
        None when none of them comes near it."""
        cutters = [c for c in cutters if c != obj]
        lo, hi = mesh_trim.world_box(obj)
        center = (lo + hi) / 2
        curves = [c for c in cutters if is_curve(c)]
        reach = 10 * max([float(np.linalg.norm(hi - lo))] + [chain.size() + float(np.linalg.norm(chain.start() - center))
                                                             for c in curves for _, chain in self.curves.chains(c)])
        chains = [chain for c in curves for _, chain in self.curves._cutter_chains(c, extend_lines, extend_arcs, reach)]
        bands = mesh_trim.curve_bands(chains, rv3d, plane, settings.get(context).mesh_angle)
        return mesh_trim.cut(context, obj, [c for c in cutters if mesh_trim.is_mesh(c)], bands)

    def free_mesh_cuts(self):
        for found in self.mesh_cuts.values():
            if found is not None:
                found.free()
        self.mesh_cuts.clear()
        self.mesh_highlight = []

    def snap_geometry(self):
        return [], []

    def _replace_all(self, context, cuts):
        """Split the splines in ``cuts`` ({(object, spline index): (chain,
        ranges)}) in one step; returns the number of pieces."""
        self.edits.begin()
        pieces = 0
        by_object = {}
        for (obj, index), (chain, ranges) in cuts.items():
            by_object.setdefault(obj, []).append((index, ranges))
        for obj, splines in by_object.items():
            # Highest index first: new splines go to the end, so the lower
            # indices stay valid.
            for index, ranges in sorted(splines, key=lambda item: item[0], reverse=True):
                self.edits.replace(obj, index, ranges)
                pieces += len(ranges)
        self.curves.clear()
        return pieces


class R2B_OT_trim(_CurveCommand, GetPoints, bpy.types.Operator):
    """Trim curves and meshes: select cutting objects, then click the parts to remove"""

    bl_idname = "r2b.trim"
    bl_label = "Trim"

    cap = False  # Meshes: cut closed meshes as solids and close the cut.

    def invoke(self, context, event):
        self.cutters = None  # None: all visible curves (and meshes, for curves).
        self.extend_lines = self.extend_arcs = self.apparent = False
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not context.selected_objects

    def selection_done(self, context):
        selected = list(context.selected_objects)
        cutters = [obj for obj in selected if is_cutter(obj)]
        if selected and not cutters:
            command_line.echo(self.window, "Curves and meshes can cut", 'ERROR')
            return False
        self.cutters = cutters or None
        if not cutters:
            command_line.echo(self.window, "No cutting objects selected: all curves cut (and meshes cut curves)")
        return True

    def picks_curves(self):
        return not self.selecting

    def _options(self):
        yes = {True: "Yes", False: "No"}
        options = (f"ExtendLines={yes[self.extend_lines]} ExtendArcs={yes[self.extend_arcs]} "
                   f"ApparentIntersections={yes[self.apparent]} Cap={yes[type(self).cap]}")
        return options + (" Undo" if self.edits.records and not self.selecting else "")

    def prompt(self):
        if self.selecting:
            return f"Select cutting objects. Press Enter to use all curves ( {self._options()} )"
        return f"Select object to trim ( {self._options()} )"

    def _cutters(self, context):
        if self.cutters is None:
            return [obj for obj in context.visible_objects if is_cutter(obj)]
        return self.cutters

    def _mesh_cutters(self, context):
        """Meshes are cut by the cutting objects, or by all curves."""
        if self.cutters is None:
            return [obj for obj in context.visible_objects if is_curve(obj)]
        return self.cutters

    def _under_mouse(self, context, region, mouse):
        """(hit, (removed parameter range or None, cuts)) of the curve under
        the mouse, or (None, None)."""
        targets = [obj for obj in context.selectable_objects if is_curve(obj)]
        hit = self.curves.under_mouse(targets, region, mouse, _pick_radius(context))
        if hit is None:
            return None, None
        obj, index, chain, t = hit
        cuts = self.curves.cuts(obj, index, chain, self._cutters(context), self.extend_lines, self.extend_arcs,
                                region.data if self.apparent else None)
        return hit, (curve_geom.removed_range(chain, cuts, t, curve_geom.tolerance(chain)), cuts)

    def _mesh_under_mouse(self, context, area, region, mouse, hovering):
        """(mesh object, its Cut or None, region under the mouse or None) of
        the mesh under the mouse, or None. While ``hovering``, heavy meshes
        not cut yet give no Cut."""
        rv3d = region.data
        origin = region_2d_to_origin_3d(region, rv3d, mouse)
        direction = region_2d_to_vector_3d(region, rv3d, mouse)
        obj, location = mesh_trim.mesh_under_ray(context, origin, direction)
        if obj is None:
            return None
        plane = cplane.for_area(self.window.screen, area)
        view = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
        key = (obj.as_pointer(), tuple(round(c, 5) for c in view), rv3d.is_perspective,
               tuple(round(c, 5) for c in plane.normal), self.extend_lines, self.extend_arcs)
        if key not in self.mesh_cuts:
            if hovering and len(obj.data.polygons) > mesh_trim.HOVER_FACES:
                return obj, None, None
            self.mesh_cuts[key] = self.cut_mesh(context, obj, self._mesh_cutters(context), rv3d, plane,
                                                self.extend_lines, self.extend_arcs)
        found = self.mesh_cuts[key]
        if found is None:
            return obj, None, None
        return obj, found, found.region_at(origin, direction, near=location)

    def on_hover(self, context, area, region, mouse):
        self.highlight = []
        self.mesh_highlight = []
        if area is None:
            return
        hit, found = self._under_mouse(context, region, mouse)
        if hit is not None:
            if found[0] is not None:
                a, b = found[0]
                self.highlight = [curve_geom.sample(curve_geom.piece(hit[2], a, b), 24)[0]]
            return
        picked = self._mesh_under_mouse(context, area, region, mouse, hovering=True)
        if picked is None or picked[2] is None or not picked[1].divided(picked[2]):
            return
        cut, number = picked[1], picked[2]
        gone, caps, _ = cut.removed(number, type(self).cap)
        self.mesh_highlight = [(cut.outline(gone), 'removal')]
        if caps:
            self.mesh_highlight.append((cut.outline(caps), 'growth'))

    def _changed(self):
        self.curves.clear()
        self.free_mesh_cuts()
        self.highlight = []

    def on_pick(self, context, area, region, mouse):
        hit, found = self._under_mouse(context, region, mouse)
        if hit is None:
            self._trim_mesh(context, area, region, mouse)
            return None
        obj, index, chain, t = hit
        tol = curve_geom.tolerance(chain)
        ranges = curve_geom.trim_ranges(chain, found[1], t, tol)
        if ranges is None:
            if curve_geom.is_closed(chain, tol) and found[1]:
                message = "A closed curve must be cut at two points to trim it"
            else:
                message = "The curve does not meet a cutting object"
            command_line.echo(self.window, message, 'ERROR')
            return None
        self._replace_all(context, {(obj, index): (chain, ranges)})
        if self.cutters is not None and obj in self.cutters:
            self.cutters += self.edits.records[-1]["created"]  # Pieces of a cutter still cut.
        self._changed()
        return None

    def _trim_mesh(self, context, area, region, mouse):
        picked = self._mesh_under_mouse(context, area, region, mouse, hovering=False)
        if picked is None:
            return
        obj, cut, number = picked
        if cut is None or number is None:
            command_line.echo(self.window, "The cutting objects do not meet the mesh", 'ERROR')
            return
        result, message = cut.trim(number, type(self).cap)
        if result is None:
            command_line.echo(self.window, message, 'ERROR')
            return
        if message:
            command_line.echo(self.window, message)
        self.edits.begin()
        self.edits.set_mesh(obj, result)
        self._changed()

    def on_option(self, context, name):
        for option, attribute in (("extendlines", "extend_lines"), ("extendarcs", "extend_arcs"),
                                  ("apparentintersections", "apparent")):
            if option.startswith(name) and len(name) >= (7 if option.startswith("extend") else 1):
                setattr(self, attribute, not getattr(self, attribute))
                self.highlight = []
                self.mesh_highlight = []
                return True
        if "cap".startswith(name):
            type(self).cap = not type(self).cap
            self.mesh_highlight = []
            return True
        if self.picks_curves() and "undo".startswith(name):
            if not self.edits.records:
                command_line.echo(self.window, "Nothing to undo", 'ERROR')
                return True
            created = self.edits.undo()
            if self.cutters is not None:
                gone = {obj.as_pointer() for obj in created}
                self.cutters = [obj for obj in self.cutters if obj.as_pointer() not in gone]
            self._changed()
            return True
        return False

    def on_enter(self, context):
        return FINISH if self.edits.records else CANCEL

    def on_cancel(self, context):
        return self.on_enter(context)

    def create(self, context):
        self.free_mesh_cuts()
        self.edits.finish()


class R2B_OT_split(_CurveCommand, GetPoints, bpy.types.Operator):
    """Split curves and meshes where cutting objects meet them, or curves at picked points"""

    bl_idname = "r2b.split"
    bl_label = "Split"

    cap = False  # Meshes: cut closed meshes into closed pieces.

    def invoke(self, context, event):
        self.stage = 'TARGETS'
        self.to_split = []
        self.cutters = []
        self.split_points = []  # (object, spline index, parameter)
        # Curves are projected in the view where the cutting objects are
        # chosen: the one under the mouse then, or the command's own.
        self.mouse = None
        self.view_area = context.area if context.area is not None and context.area.type == 'VIEW_3D' else None
        chosen = [obj for obj in context.selected_objects if is_curve(obj) or mesh_trim.is_mesh(obj)]
        if chosen:
            self._choose_targets(chosen)
        return super().invoke(context, event)

    def modal(self, context, event):
        self.mouse = (event.mouse_x, event.mouse_y)
        return super().modal(context, event)

    def _choose_targets(self, chosen):
        self.to_split = chosen
        for obj in chosen:
            obj.select_set(False)
        self.stage = 'CUTTERS'

    def _curve_targets(self):
        return [obj for obj in self.to_split if is_curve(obj)]

    def _mesh_targets(self):
        return [obj for obj in self.to_split if mesh_trim.is_mesh(obj)]

    def needs_selection(self, context):
        return True

    def prompt(self):
        if self.stage == 'TARGETS':
            return "Select curves or meshes to split. Press Enter when done"
        if self.stage == 'CUTTERS':
            options = (["Point"] if self._curve_targets() else []) + \
                (["Cap=" + ("Yes" if type(self).cap else "No")] if self._mesh_targets() else [])
            return "Select cutting objects. Press Enter when done" + (f" ( {' '.join(options)} )" if options else "")
        return "Point to split at. Press Enter when done ( Undo )" if self.split_points else "Point to split at"

    def selection_done(self, context):
        if self.stage == 'TARGETS':
            chosen = [obj for obj in context.selected_objects if is_curve(obj) or mesh_trim.is_mesh(obj)]
            if not chosen:
                command_line.echo(self.window, "Select curves or meshes to split", 'ERROR')
            else:
                self._choose_targets(chosen)
            return False
        chosen = [obj for obj in context.selected_objects if is_cutter(obj)]
        if not chosen:
            command_line.echo(self.window, "Select cutting curves or meshes" +
                              (", or type Point" if self._curve_targets() else ""), 'ERROR')
            return False
        self.cutters = chosen
        return FINISH

    def on_option(self, context, name):
        if self.stage == 'CUTTERS' and self._curve_targets() and "point".startswith(name):
            self.stage = 'POINTS'
            self.selecting = False
            return True
        if self.stage == 'CUTTERS' and self._mesh_targets() and "cap".startswith(name):
            type(self).cap = not type(self).cap
            return True
        if self.stage == 'POINTS' and "undo".startswith(name):
            if self.split_points:
                self.split_points.pop()
            return True
        return False

    def _nearest(self, point):
        """(distance, object, spline index, chain, parameter) of the curve
        to split nearest to a world-space point."""
        best = None
        for obj in self._curve_targets():
            for index, chain in self.curves.chains(obj):
                t = curve_geom.closest_parameter(chain, point)
                distance = float(np.linalg.norm(curve_geom.evaluate(chain, t) - point))
                if best is None or distance < best[0]:
                    best = (distance, obj, index, chain, t)
        return best

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if self.stage != 'POINTS':
            return point
        if snapped:
            nearest = self._nearest(np.array(point))
            if nearest is not None and nearest[0] <= curve_geom.tolerance(nearest[3]):
                return point  # An object snap on a curve: exactly there.
        hit = self.curves.under_mouse(self._curve_targets(), region, mouse, math.inf)
        return Vector(curve_geom.evaluate(hit[2], hit[3])) if hit is not None else point

    def on_point(self, context):
        point = np.array(self.points.pop())
        nearest = self._nearest(point)
        if nearest is None or nearest[0] > curve_geom.tolerance(nearest[3]):
            command_line.echo(self.window, "Pick a point on a curve to split", 'ERROR')
            return None
        _, obj, index, _, t = nearest
        self.split_points.append((obj, index, t))
        return None

    def on_enter(self, context):
        if self.stage == 'POINTS':
            return FINISH if self.split_points else CANCEL
        return CANCEL

    def preview(self):
        return []

    def _view(self):
        """(area, main region) of the viewport curves are projected in."""
        if self.mouse is not None:
            area, region = getpoint._viewport_under(self.window, *self.mouse)
            if area is not None:
                return area, region
        areas = [a for a in self.window.screen.areas if a.type == 'VIEW_3D']
        area = self.view_area if self.view_area in areas else (areas[0] if areas else None)
        if area is None:
            return None, None
        return area, next(r for r in area.regions if r.type == 'WINDOW')

    def _split_curves(self, context):
        """The number of curves split and of pieces."""
        cuts = {}
        if self.stage == 'POINTS':
            for obj, index, t in self.split_points:
                cuts.setdefault((obj, index), []).append(t)
            found = {}
            for (obj, index), params in cuts.items():
                chain = dict(self.curves.chains(obj))[index]
                found[(obj, index)] = (chain, curve_geom.cut_parameters(chain, params, [], curve_geom.tolerance(chain)))
        else:
            found = {}
            for obj in self._curve_targets():
                for index, chain in self.curves.chains(obj):
                    found[(obj, index)] = (chain, self.curves.cuts(obj, index, chain, self.cutters))
        ranges = {}
        for key, (chain, params) in found.items():
            pieces = curve_geom.split_ranges(chain, params, curve_geom.tolerance(chain))
            if pieces:
                ranges[key] = (chain, pieces)
        if not ranges:
            return 0, 0
        return len(ranges), self._replace_all(context, ranges)

    def _split_meshes(self, context):
        """The number of meshes split and of pieces, and notes."""
        meshes = self._mesh_targets() if self.stage != 'POINTS' else []
        if not meshes:
            return 0, 0, set()
        area, region = self._view()
        plane = cplane.for_area(self.window.screen, area) if area is not None else cplane.WORLD_TOP
        split = pieces = 0
        notes = set()
        for obj in meshes:
            found = self.cut_mesh(context, obj, self.cutters, region.data, plane)
            if found is None:
                continue
            results, note = found.split(type(self).cap)
            found.free()
            if note:
                notes.add(note)
            if not results:
                continue
            self.edits.begin()
            self.edits.set_mesh(obj, results[0])
            for result in results[1:]:
                self.edits.mesh_piece(obj, result)
            split += 1
            pieces += len(results)
        return split, pieces, notes

    def create(self, context):
        curves, curve_pieces = self._split_curves(context)
        meshes, mesh_pieces, notes = self._split_meshes(context)
        self.edits.finish()
        for note in sorted(notes):
            command_line.echo(self.window, note)
        if not curves and not meshes:
            what = " or ".join(["curves"] * bool(self._curve_targets()) + ["meshes"] * bool(self._mesh_targets()))
            command_line.echo(self.window, f"Nothing was split: the cutting objects do not cut the {what}", 'ERROR')
            return
        done = []
        if curves:
            done.append(f"{curves} curve{'s' if curves > 1 else ''} into {curve_pieces} pieces")
        if meshes:
            done.append(f"{meshes} mesh{'es' if meshes > 1 else ''} into {mesh_pieces} pieces")
        command_line.echo(self.window, "Split " + " and ".join(done))


class R2B_OT_extend(_CurveCommand, GetPoints, bpy.types.Operator):
    """Extend curves to boundary objects, by a length, or to picked points"""

    bl_idname = "r2b.extend"
    bl_label = "Extend"

    def invoke(self, context, event):
        self.stage = 'BOUNDARIES'
        self.boundaries = None  # None: dynamic (or by length).
        self.distance_to_extend = None  # Extend by this length.
        self.extend_type = 'NATURAL'  # See Extender.TYPES.
        self.extender = None  # Dynamic: the curve end picked.
        self._dynamic = None  # Dynamic: (candidate, parameter) under the mouse.
        self._preview = {}  # Hover previews, by curve end.
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not context.selected_objects

    def _options(self):
        options = [f"Type={self.extend_type.title()}"]
        if self.stage == 'CURVES' and self.edits.records:
            options.append("Undo")
        return " ".join(options)

    def prompt(self):
        if self.stage == 'BOUNDARIES':
            return (f"Select boundary objects, or type the length to extend by. Press Enter for dynamic extend "
                    f"( {self._options()} )")
        if self.stage == 'POINT':
            return "End of extension"
        length = f" by {self.distance_to_extend:g}" if self.distance_to_extend is not None else ""
        return f"Select curve to extend{length} ( {self._options()} )"

    def selection_done(self, context):
        selected = list(context.selected_objects)
        boundaries = [obj for obj in selected if is_cutter(obj)]
        if selected and not boundaries:
            command_line.echo(self.window, "Curves and meshes can be boundaries", 'ERROR')
            return False
        self.boundaries = boundaries or None
        self.stage = 'CURVES'
        return True

    def on_number(self, context, value):
        if value <= 0:
            command_line.echo(self.window, "The length must be more than zero", 'ERROR')
            return True
        if self.stage == 'POINT':
            self._extend(self.extender, self.extender.by_length(value))
            self.stage = 'CURVES'
            return True
        self.distance_to_extend = value
        if self.stage == 'BOUNDARIES':
            self.stage = 'CURVES'
            self.selecting = False
        return True

    def on_option(self, context, name):
        types = Extender.TYPES
        if "type".startswith(name):
            self.extend_type = types[(types.index(self.extend_type) + 1) % len(types)]
        elif any(kind.lower().startswith(name) for kind in types):
            self.extend_type = next(kind for kind in types if kind.lower().startswith(name))
        elif self.stage == 'CURVES' and "undo".startswith(name):
            if self.edits.records:
                self.edits.undo()
                self.curves.clear()
            else:
                command_line.echo(self.window, "Nothing to undo", 'ERROR')
        else:
            return False
        self.highlight = []
        self._preview = {}
        return True

    def picks_curves(self):
        return self.stage == 'CURVES'

    def _end_under_mouse(self, context, region, mouse):
        """An Extender for the curve end nearest the mouse, or None."""
        targets = [obj for obj in context.selectable_objects if is_curve(obj)]
        hit = self.curves.under_mouse(targets, region, mouse, _pick_radius(context))
        if hit is None:
            return None
        obj, index, chain, t = hit
        if curve_geom.is_closed(chain, curve_geom.tolerance(chain)):
            return 'CLOSED'
        at_start = curve_geom.length(chain, 0.0, t) < curve_geom.length(chain, t)
        return Extender(obj, index, at_start, self.extend_type)

    def _target(self, extender):
        if self.distance_to_extend is not None:
            return extender.by_length(self.distance_to_extend)
        if self.boundaries is not None:
            return extender.to_boundaries(self.curves, self.boundaries)
        return None

    def on_hover(self, context, area, region, mouse):
        self.highlight = []
        if area is None:
            return
        extender = self._end_under_mouse(context, region, mouse)
        if not isinstance(extender, Extender):
            return
        key = (extender.obj.as_pointer(), extender.index, extender.at_start, self.extend_type)
        if key not in self._preview:
            found = self._target(extender)
            self._preview[key] = (curve_geom.sample(curve_geom.piece(found[0], 0.0, found[1]), 24)[0]
                                  if found is not None and found[1] > curve_geom.PARAM_EPS else None)
        if self._preview[key] is not None:
            self.highlight = [self._preview[key]]

    def on_pick(self, context, area, region, mouse):
        extender = self._end_under_mouse(context, region, mouse)
        if extender is None:
            return None
        if extender == 'CLOSED':
            command_line.echo(self.window, "Closed curves cannot be extended", 'ERROR')
            return None
        if self.boundaries is None and self.distance_to_extend is None:
            self.extender = extender  # Dynamic: ask for the end point.
            self.stage = 'POINT'
            self.points = []
            return None
        found = self._target(extender)
        if found is None:
            message = ("The extension does not reach a boundary" if self.distance_to_extend is None
                       else "The arc would close on itself")
            command_line.echo(self.window, message, 'ERROR')
            return None
        self._extend(extender, found)
        return None

    def _extend(self, extender, found):
        if found is None or found[1] <= curve_geom.PARAM_EPS:
            command_line.echo(self.window, "Nothing to extend", 'ERROR')
            return
        try:
            chain, parts = extender.result(*found)
        except ValueError as ex:
            command_line.echo(self.window, str(ex), 'ERROR')
            return
        self.edits.begin()
        self.edits.set_spline(extender.obj, extender.index, chain, parts)
        self.curves.clear()
        self._preview = {}
        self.highlight = []

    # Dynamic extend: picking the end of the extension.

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if self.stage != 'POINT':
            return point
        found = self.extender.to_point(np.array(point))
        if found is None:
            return point
        self._dynamic = found
        return Vector(curve_geom.evaluate(*found))

    def preview(self):
        if self.stage == 'POINT' and self.current is not None and getattr(self, "_dynamic", None) is not None:
            candidate, t = self._dynamic
            if t > curve_geom.PARAM_EPS:
                return [(curve_geom.sample(curve_geom.piece(candidate, 0.0, t), 24)[0], 'addition')]
            return []
        return [(points, 'addition') for points in self.highlight]

    def on_point(self, context):
        point = np.array(self.points.pop())
        self._extend(self.extender, self.extender.to_point(point))
        self.stage = 'CURVES'
        self._dynamic = None
        return None

    def on_enter(self, context):
        if self.stage == 'POINT':
            self.stage = 'CURVES'
            return None
        return FINISH if self.edits.records else CANCEL

    def on_cancel(self, context):
        return FINISH if self.edits.records else CANCEL

    def create(self, context):
        self.edits.finish()


class R2B_OT_fillet(_CornerCommand, _CurveCommand, GetPoints, bpy.types.Operator):
    """Round the corner between two curves with an arc of a radius"""

    bl_idname = "r2b.fillet"
    bl_label = "Fillet"
    noun = "fillet"

    radius: bpy.props.FloatProperty(name="Radius", default=1.0, min=0.0)

    def size_options(self):
        return f"Radius={self.radius:g}"

    def asking_prompt(self):
        return f"Fillet radius <{self.radius:g}>"

    def size_option(self, name):
        if "radius".startswith(name):
            self.asking = 'RADIUS'
            return True
        return False

    def on_number(self, context, value):
        if value < 0:
            command_line.echo(self.window, "The radius must not be negative", 'ERROR')
            return True
        self.radius = value
        self.asking = None
        return True

    def make(self, context, first, second, plane):
        from . import curves

        one, two = self.lengthened(first, second)
        tolerance = curve_geom.tolerance(one.chain, two.chain)
        if self.radius <= 0:
            corner = corner_between((one, first[4], one.a + first[3]), (two, second[4], two.a + second[3]),
                                    tolerance)
            if corner is None:
                return "The curves do not meet, even extended"
            return self.finish_corner(context, first, second, one, two, *corner, None)
        # Where the curves meet (if they do): the picks tell the sides of it.
        corner = corner_between((one, first[4], one.a + first[3]), (two, second[4], two.a + second[3]), tolerance)
        found = curves.circle_tangent_to((one.chain, one.a + first[3], Vector(first[4])),
                                         (two.chain, two.a + second[3], Vector(second[4])), self.radius, plane,
                                         corner)
        if found is None:
            return f"No fillet of radius {self.radius:g} fits there"
        center, touch1, touch2, v1, v2 = found
        x_axis = (touch1 - center).normalized()
        y_axis = plane.normal.cross(x_axis)
        # The arc leaves the first tangent point away from the part of the
        # first curve that is kept, and goes round to the second one.
        side = self.kept_side(v1, one.a + first[3], corner[0] if corner else None)
        kept = Vector(curve_geom.tangent(one.chain, v1)) * (1 if side > v1 else -1)
        turn = 1 if y_axis.dot(kept) < 0 else -1
        offset = touch2 - center
        angle = (turn * math.atan2(offset.dot(y_axis), offset.dot(x_axis))) % (2 * math.pi)
        arc = curves.new_arc_object(context, "Fillet", center, x_axis, y_axis, self.radius, 0.0, turn * angle)
        return self.finish_corner(context, first, second, one, two, v1, v2, arc, corner)


class R2B_OT_chamfer(_CornerCommand, _CurveCommand, GetPoints, bpy.types.Operator):
    """Cut the corner between two curves with a line"""

    bl_idname = "r2b.chamfer"
    bl_label = "Chamfer"
    noun = "chamfer"

    first_distance: bpy.props.FloatProperty(name="First Distance", default=1.0, min=0.0)
    second_distance: bpy.props.FloatProperty(name="Second Distance", default=1.0, min=0.0)

    def size_options(self):
        return f"Distances={self.first_distance:g},{self.second_distance:g}"

    def asking_prompt(self):
        if self.asking == 'FIRST':
            return f"First chamfer distance <{self.first_distance:g}>"
        return f"Second chamfer distance <{self.second_distance:g}>"

    def size_option(self, name):
        if "distances".startswith(name):
            self.asking = 'FIRST'
            return True
        return False

    def on_enter(self, context):
        if self.asking == 'FIRST':
            self.asking = 'SECOND'
            return None
        return super().on_enter(context)

    def on_number(self, context, value):
        if value < 0:
            command_line.echo(self.window, "The distance must not be negative", 'ERROR')
            return True
        if self.asking == 'FIRST':
            self.first_distance = value
            self.asking = 'SECOND'
        elif self.asking == 'SECOND':
            self.second_distance = value
            self.asking = None
        else:  # A number at the curve prompts: both distances.
            self.first_distance = self.second_distance = value
        return True

    @staticmethod
    def _along(lengthened, corner, pick, distance):
        """Virtual parameter at ``distance`` from the corner toward the pick."""
        # Measured on the chain itself: a lengthened arc may go round its
        # circle more than once, so a point does not tell its parameter.
        chain = lengthened.chain
        end = float(chain.count) if pick > corner else 0.0

        def along(t):
            return curve_geom.length(chain, min(corner, t), max(corner, t))

        if along(end) < distance:
            return None
        low, high = corner, end
        for _ in range(60):
            middle = (low + high) / 2
            if along(middle) < distance:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    def make(self, context, first, second, plane):
        from . import curves

        one, two = self.lengthened(first, second)
        tolerance = curve_geom.tolerance(one.chain, two.chain)
        corner = corner_between((one, first[4], one.a + first[3]), (two, second[4], two.a + second[3]),
                                tolerance)
        if corner is None:
            return "The curves do not meet, even extended"
        v1 = self._along(one, corner[0], one.a + first[3], self.first_distance)
        v2 = self._along(two, corner[1], two.a + second[3], self.second_distance)
        if v1 is None or v2 is None:
            return "The chamfer distances are longer than the curves"
        start, end = curve_geom.evaluate(one.chain, v1), curve_geom.evaluate(two.chain, v2)
        line = None
        if np.linalg.norm(end - start) > curve_geom.tolerance(one.chain, two.chain):
            line = curves.new_curve_object(context, "Chamfer", [Vector(start), Vector(end)])
        return self.finish_corner(context, first, second, one, two, v1, v2, line, corner)


class _SelectThenRun:
    """Join and Explode: work on the selected objects at once, or ask for
    objects first (Enter runs)."""

    verb = ""

    def needs_selection(self, context):
        return not context.selected_objects

    def prompt(self):
        return f"Select objects to {self.verb}. Press Enter when done"

    def selection_done(self, context):
        if not context.selected_objects:
            command_line.echo(self.window, f"Select objects to {self.verb}", 'ERROR')
            return False
        active = context.view_layer.objects.active
        # The active object first: a joined curve keeps its name and layer.
        self.chosen = sorted(context.selected_objects, key=lambda obj: obj != active)
        return FINISH


class R2B_OT_join(_SelectThenRun, GetPoints, bpy.types.Operator):
    """Join curves whose ends meet into one curve, and meshes into one mesh"""

    bl_idname = "r2b.join"
    bl_label = "Join"
    verb = "join"

    def create(self, context):
        meshes = [obj for obj in self.chosen if obj.type == 'MESH']
        joined, made = join_curves(self.chosen)  # Removes the curves joined into others.
        if len(meshes) >= 2:
            with context.temp_override(active_object=meshes[0], selected_editable_objects=meshes):
                bpy.ops.object.join()
            command_line.echo(self.window, f"Joined {len(meshes)} meshes into one")
        if joined:
            command_line.echo(self.window, f"Joined {joined} curves into {made} curve{'s' if made > 1 else ''}")
        elif len(meshes) < 2:
            command_line.echo(self.window, "Nothing to join: no curve ends meet", 'ERROR')


class R2B_OT_explode(_SelectThenRun, GetPoints, bpy.types.Operator):
    """Explode joined curves into their parts, polylines into lines"""

    bl_idname = "r2b.explode"
    bl_label = "Explode"
    verb = "explode"

    def create(self, context):
        edits = Edits()
        exploded, pieces = explode(self.chosen, edits)
        edits.finish()
        if exploded:
            command_line.echo(self.window, f"Exploded {exploded} curve{'s' if exploded > 1 else ''} "
                                           f"into {pieces} pieces")
        else:
            command_line.echo(self.window, "Nothing to explode", 'ERROR')


_classes = (R2B_OT_trim, R2B_OT_split, R2B_OT_join, R2B_OT_explode, R2B_OT_extend, R2B_OT_fillet, R2B_OT_chamfer)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
