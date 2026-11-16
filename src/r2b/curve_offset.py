# SPDX-License-Identifier: GPL-3.0-or-later
"""Offset (MODE): a copy of a curve at a distance from it, as in Rhino.

The offset lies in the curve's plane (or, for a curve that is not planar, in
the CPlane: each point moves along the CPlane normal turned by the tangent).
Lines stay lines and arcs become concentric arcs, exactly; free-form curves
are approximated within a tolerance by cubic Bezier segments (each matches
the offset's points and derivatives at its ends, halved until close enough).

Where the curve has a corner, the offsets of the two sides either cross
(both are cut at the crossing) or leave a gap, closed per ``Corner``: Sharp
(carried on along their tangents until they meet), Round (an arc about the
corner) or Chamfer (a line). Loops the offset of a tight bend makes, or
pieces that vanish because the distance is larger than they are long, are
not removed: the distance is then reported as too large where it can be
told.
"""

import math
from dataclasses import dataclass

import numpy as np

import bpy

from . import command_line, cplane, curve_edit, curve_geom, curves, polycurve
from .getpoint import FINISH, GetPoints

CORNERS = ('SHARP', 'ROUND', 'CHAMFER')

# Free-form offsets: halve a piece at most this often.
MAX_HALVINGS = 12


class OffsetError(Exception):
    """The curve cannot be offset by that distance."""


@dataclass
class _Piece:
    """A smooth piece of a curve or of its offset, in world space."""

    kind: str  # 'LINE', 'ARC' or 'FREE'
    segments: np.ndarray  # Homogeneous Bezier segments
    arc: dict = None  # ARC: polycurve.ARC_PROPERTY format, world space

    def start(self):
        return self.segments[0, 0, :3] / self.segments[0, 0, 3]

    def end(self):
        return self.segments[-1, -1, :3] / self.segments[-1, -1, 3]

    def chain(self):
        return curve_geom.Chain(self.segments)

    def direction(self, at_end):
        """Unit tangent at the start or end."""
        chain = self.chain()
        t = float(chain.count) if at_end else 0.0
        d = curve_geom.tangent(chain, t)
        if np.linalg.norm(d) < 1e-12:
            d = curve_geom.tangent(chain, t + (-1e-4 if at_end else 1e-4))
        return d / max(np.linalg.norm(d), 1e-300)


def _line(a, b):
    return _Piece('LINE', np.array([[(*a, 1.0), (*b, 1.0)]], dtype=np.float64))


def _is_straight(segment, tol):
    points = segment[:, :3] / segment[:, 3:]
    a, b = points[0], points[-1]
    d = b - a
    length = np.linalg.norm(d)
    if length <= tol:
        return False
    offsets = points - a
    off = offsets - np.outer(offsets @ d / length, d / length)
    return bool(np.all(np.linalg.norm(off, axis=1) <= tol)) and np.all(offsets @ d >= -tol) \
        and np.all(offsets @ d <= length * length + tol * length)


def pieces_of(chain, parts, matrix, normal, tol):
    """Smooth pieces of a world chain made of ``parts`` (local arcs, moved
    by ``matrix``): each straight segment a line, each arc in the plane one
    arc, every other segment free-form."""
    pieces = []
    bounds = polycurve.edges(parts)
    for part, a, b in zip(parts, bounds[:-1], bounds[1:]):
        segments = chain.segments[int(a):int(b)]
        arc = polycurve.transform_arc(part.arc, matrix) if part.arc is not None else None
        if arc is not None:
            axis = np.cross(arc["x_axis"], arc["y_axis"])
            if abs(abs(axis @ normal) - 1.0) < 1e-6:
                pieces.append(_Piece('ARC', segments.copy(), arc))
                continue
        for segment in segments:
            points = segment[:, :3] / segment[:, 3:]
            if np.all(np.linalg.norm(points - points[0], axis=1) <= tol):
                continue  # A segment of no length.
            if _is_straight(segment, tol):
                pieces.append(_line(points[0], points[-1]))
            else:
                pieces.append(_Piece('FREE', segment[None].copy()))
    return pieces


# --- Offsetting pieces --------------------------------------------------------------

def _left(normal, direction):
    left = np.cross(normal, direction)
    length = np.linalg.norm(left)
    return left / length if length > 1e-300 else left


def _offset_point(segment, u, distance, normal):
    """The offset of a segment's point at u (a polynomial: u may be slightly
    outside 0..1)."""
    point, derivative = (values[0] for values in curve_geom.segment_derivatives(segment, [u]))
    if np.linalg.norm(derivative) < 1e-12:
        inside = u + (1e-5 if u < 0.5 else -1e-5)
        derivative = curve_geom.segment_derivatives(segment, [inside])[1][0]
    return point + _left(normal, derivative) * distance


def _offset_free(segment, distance, normal, tol):
    """Cubic Bezier segments (homogeneous, weight 1) within ``tol`` of the
    offset of a free-form segment."""
    h = 1e-6

    def at(u):
        return _offset_point(segment, u, distance, normal)

    def derivative(u):
        return (at(u + h) - at(u - h)) / (2 * h)

    result = []

    def fit(u0, u1, depth):
        span = u1 - u0
        p0, p3 = at(u0), at(u1)
        p1 = p0 + derivative(u0) * span / 3
        p2 = p3 - derivative(u1) * span / 3
        cubic = np.array([(*p, 1.0) for p in (p0, p1, p2, p3)])
        s = np.linspace(0.0, 1.0, 9)[1:-1]
        error = max(np.linalg.norm(curve_geom.segment_points(cubic, [v])[0] - at(u0 + v * span)) for v in s)
        if error > tol and depth < MAX_HALVINGS:
            middle = (u0 + u1) / 2
            fit(u0, middle, depth + 1)
            fit(middle, u1, depth + 1)
        else:
            result.append(cubic)

    fit(0.0, 1.0, 0)
    return np.array(result)


def offset_piece(piece, distance, normal, tol):
    """The offset of a piece (to the left of its direction for a positive
    distance)."""
    if piece.kind == 'LINE':
        a, b = piece.start(), piece.end()
        shift = _left(normal, b - a) * distance
        return _line(a + shift, b + shift)
    if piece.kind == 'ARC':
        arc = piece.arc
        center = np.array(arc["center"], dtype=np.float64)
        chain = piece.chain()
        middle = curve_geom.evaluate(chain, chain.count / 2)
        moved = middle + _left(normal, curve_geom.tangent(chain, chain.count / 2)) * distance
        radius = float(arc["radius"])
        new_radius = float(np.linalg.norm(moved - center))
        if (moved - center) @ (middle - center) <= 0 or new_radius <= tol:
            raise OffsetError("The distance is larger than the radius of an arc")
        scale = new_radius / radius
        segments = piece.segments.copy()
        weights = segments[..., 3:]
        points = segments[..., :3] / weights
        segments[..., :3] = (center + (points - center) * scale) * weights
        return _Piece('ARC', segments, dict(arc, center=list(center), radius=new_radius))
    return _Piece('FREE', np.concatenate([_offset_free(segment, distance, normal, tol)
                                          for segment in piece.segments]))


# --- Corners ------------------------------------------------------------------------

def _meet(point_a, direction_a, point_b, direction_b, normal):
    """(s, t) with point_a + s direction_a = point_b + t direction_b in the
    plane, or None if parallel."""
    x_axis = direction_a
    y_axis = np.cross(normal, x_axis)
    matrix = np.array([[direction_a @ x_axis, -(direction_b @ x_axis)],
                       [direction_a @ y_axis, -(direction_b @ y_axis)]])
    if abs(np.linalg.det(matrix)) < 1e-12:
        return None
    rhs = np.array([(point_b - point_a) @ x_axis, (point_b - point_a) @ y_axis])
    s, t = np.linalg.solve(matrix, rhs)
    return s, t


def _crossing(before, after, tol):
    """(parameter on before, parameter on after) where two offset pieces
    cross nearest the corner between them, or None."""
    a, b = before.chain(), after.chain()
    params, _ = curve_geom.chain_intersections(a, b, tol)
    best = None
    for ta in params:
        point = curve_geom.evaluate(a, ta)
        tb = curve_geom.closest_parameter(b, point)
        if np.linalg.norm(curve_geom.evaluate(b, tb) - point) > 10 * tol:
            continue
        # Nearest the corner: the least of each kept away.
        score = curve_geom.length(a, ta) + curve_geom.length(b, 0.0, tb)
        if best is None or score < best[0]:
            best = (score, ta, tb)
    return best[1:] if best else None


def arc_segments(center, x_axis, y_axis, radius, sweep):
    """Homogeneous rational quadratic Bezier segments of an exact arc from
    ``x_axis`` by ``sweep`` (radians, about x_axis × y_axis), 90 degrees or
    less each."""
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2) - 1e-9))
    step = sweep / count
    weight = math.cos(step / 2)

    def at(angle, distance):
        return center + (x_axis * math.cos(angle) + y_axis * math.sin(angle)) * distance

    segments = []
    for i in range(count):
        a0, a1 = i * step, (i + 1) * step
        middle = at((a0 + a1) / 2, radius / weight)
        segments.append([(*at(a0, radius), 1.0), (*(middle * weight), weight), (*at(a1, radius), 1.0)])
    return np.array(segments, dtype=np.float64)


def _round(corner, start, end, normal, radius):
    """An exact arc about ``corner`` from start to end, the short way."""
    x_axis = (start - corner) / np.linalg.norm(start - corner)
    y_axis = np.cross(normal, x_axis)
    offset = end - corner
    sweep = math.atan2(offset @ y_axis, offset @ x_axis)
    segments = arc_segments(corner, x_axis, y_axis, radius, sweep)
    # The arc's ends exactly at the pieces' ends.
    segments[0, 0, :3] = start
    segments[-1, -1, :3] = end
    first = 0.0 if sweep >= 0 else sweep % (2 * math.pi)
    tag = {"center": list(corner), "x_axis": list(x_axis), "y_axis": list(y_axis), "radius": float(radius),
           "start": first, "end": first + abs(sweep)}
    return _Piece('ARC', segments, tag)


def offset_curve(chain, parts, matrix, distance, normal, corner='SHARP', tol=None):
    """(world chain, parts with world arcs) of the offset of a world chain
    made of ``parts`` by ``distance`` (positive: to the left, ``normal`` ×
    tangent) in the plane of ``normal``."""
    normal = np.asarray(normal, dtype=np.float64)
    normal = normal / np.linalg.norm(normal)
    size = chain.size()
    if tol is None:
        tol = 1e-5 * max(size, abs(distance), 1e-9)
    closed = curve_geom.is_closed(chain, curve_geom.tolerance(chain))
    base = pieces_of(chain, parts, matrix, normal, curve_geom.tolerance(chain))
    if not base:
        raise OffsetError("The curve has no length")
    moved = [offset_piece(piece, distance, normal, tol) for piece in base]
    count = len(moved)
    # Per piece: parameter range kept, and points its ends are carried on to.
    keep = [[0.0, float(len(piece.segments))] for piece in moved]
    to_start, to_end = [None] * count, [None] * count
    connectors = [None] * count  # Piece after piece i (Round, Chamfer).
    joints = range(count if closed else count - 1)
    for i in joints:
        j = (i + 1) % count
        before, after = moved[i], moved[j]
        gap = np.linalg.norm(after.start() - before.end())
        if gap <= tol:
            continue
        turn = np.cross(base[i].direction(True), base[j].direction(False)) @ normal
        if turn * distance > 0:  # Inside the corner: the offsets cross.
            found = _crossing(before, after, tol)
            if found is None:
                raise OffsetError("The distance is too large for the curve's corners")
            keep[i][1], keep[j][0] = found
            continue
        corner_point = base[i].end()
        if corner == 'ROUND':
            connectors[i] = _round(corner_point, before.end(), after.start(), normal, abs(distance))
        elif corner == 'CHAMFER':
            connectors[i] = _line(before.end(), after.start())
        else:
            meet = _meet(before.end(), before.direction(True), after.start(), after.direction(False), normal)
            if meet is None or meet[0] < 0 or meet[1] > 0:
                connectors[i] = _round(corner_point, before.end(), after.start(), normal, abs(distance))
            else:
                point = before.end() + before.direction(True) * meet[0]
                to_end[i], to_start[j] = point, point
    result = []
    for i, piece in enumerate(moved):
        t0, t1 = keep[i]
        if t1 - t0 <= curve_geom.PARAM_EPS:
            raise OffsetError("The distance is too large for the curve's corners")
        if piece.kind == 'LINE':
            line = piece.chain()
            a = to_start[i] if to_start[i] is not None else curve_geom.evaluate(line, t0)
            b = to_end[i] if to_end[i] is not None else curve_geom.evaluate(line, t1)
            result.append(_line(a, b))
        else:
            if to_start[i] is not None:
                result.append(_line(to_start[i], piece.start()))
            cut = curve_geom.piece(piece.chain(), t0, t1)
            arc = piece.arc
            if arc is not None and (t0 > 0 or t1 < len(piece.segments)):
                arc = polycurve.sub_arc(arc, piece.chain(), t0, t1)
            result.append(_Piece(piece.kind, cut.segments, arc))
            if to_end[i] is not None:
                result.append(_line(piece.end(), to_end[i]))
        if connectors[i] is not None:
            result.append(connectors[i])
    return _assemble(result, closed, tol)


def _assemble(pieces, closed, tol):
    """(chain, parts) of pieces end to end; each starts exactly where the
    one before ends."""
    pieces = [piece for piece in pieces
              if not (piece.kind == 'LINE' and np.linalg.norm(piece.end() - piece.start()) <= tol)]
    if not pieces:
        raise OffsetError("The offset has no length")
    for before, after in zip(pieces, pieces[1:] + ([pieces[0]] if closed else [])):
        end = before.end()
        after.segments[0, 0, :3] = end * after.segments[0, 0, 3]
    degree = max(piece.segments.shape[1] - 1 for piece in pieces)
    segments = np.concatenate([curve_geom.elevate_degree(piece.segments, degree) for piece in pieces])
    parts = []
    for piece in pieces:
        if piece.kind == 'LINE':
            parts.append(polycurve.Part(1, 1, 'POLY'))
        elif piece.kind == 'ARC':
            parts.append(polycurve.Part(len(piece.segments), 2, 'NURBS', piece.arc))
        else:
            parts.append(polycurve.Part(len(piece.segments), 3, 'NURBS'))
    return curve_geom.Chain(segments, closed), parts


# --- The plane and the side ---------------------------------------------------------

def curve_normal(chain, fallback, tol):
    """Normal of the plane a curve lies in, facing like ``fallback`` (the
    CPlane normal); ``fallback`` for straight or non-planar curves."""
    fallback = np.asarray(fallback, dtype=np.float64)
    points, _ = curve_geom.sample(chain, 16)
    if len(points) < 3:
        return fallback
    centered = points - points.mean(axis=0)
    _, values, vectors = np.linalg.svd(centered, full_matrices=False)
    normal = vectors[2]
    if values[1] <= tol or np.max(np.abs(centered @ normal)) > tol:
        return fallback
    return -normal if normal @ fallback < 0 else normal


def side_of(chain, normal, point):
    """+1 if ``point`` is to the left of the curve (``normal`` × tangent),
    else -1. Closed curves: by whether it is inside, and the curve's turn."""
    normal = np.asarray(normal, dtype=np.float64)
    point = np.asarray(point, dtype=np.float64)
    tol = curve_geom.tolerance(chain)
    if curve_geom.is_closed(chain, tol):
        points, _ = curve_geom.sample(chain, 32)
        x_axis = points[1] - points[0] if np.linalg.norm(points[1] - points[0]) > 0 else np.array((1.0, 0, 0))
        x_axis = x_axis - normal * (x_axis @ normal)
        x_axis /= np.linalg.norm(x_axis)
        y_axis = np.cross(normal, x_axis)
        xy = np.stack([(points - point) @ x_axis, (points - point) @ y_axis], axis=1)
        a, b = xy[:-1], xy[1:]
        area = np.sum(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / 2  # About the point: same as about the origin.
        angles = np.arctan2(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0], (a * b).sum(axis=1))
        inside = abs(angles.sum()) > math.pi
        counterclockwise = area > 0
        return 1 if inside == counterclockwise else -1
    t = curve_geom.closest_parameter(chain, point)
    left = _left(normal, curve_geom.tangent(chain, t))
    return 1 if left @ (point - curve_geom.evaluate(chain, t)) >= 0 else -1


def distance_to(chain, normal, point):
    """Distance in the plane from ``point`` to the curve."""
    normal = np.asarray(normal, dtype=np.float64)
    t = curve_geom.closest_parameter(chain, point)
    offset = np.asarray(point, dtype=np.float64) - curve_geom.evaluate(chain, t)
    return float(np.linalg.norm(offset - normal * (offset @ normal)))


# --- The command -----------------------------------------------------------------------

class R2B_OT_offset(GetPoints, bpy.types.Operator):
    """Copy a curve at a distance from it, on the side picked"""

    bl_idname = "r2b.offset"
    bl_label = "Offset"

    # (Not "distance": GetPoints uses that for a typed distance constraint.)
    offset_distance: bpy.props.FloatProperty(name="Distance", default=1.0, min=0.0)
    corner: bpy.props.EnumProperty(name="Corner", items=[(c, c.title(), "") for c in CORNERS], default='SHARP')
    both_sides: bpy.props.BoolProperty(name="Both Sides", default=False)

    def invoke(self, context, event):
        self.curves = curve_edit.Curves()
        self.target = None  # (object, spline index, world chain, parts, plane normal)
        self.highlight = []
        self.through = False
        self.asking = False
        self._made = {}  # Offsets by (side, distance, corner, both sides).
        self._shown = []
        selected = [obj for obj in context.selected_objects if curve_edit.is_curve(obj)]
        area = context.area
        plane = (cplane.for_area(context.window.screen, area)
                 if area is not None and area.type == 'VIEW_3D' else cplane.WORLD_TOP)
        if len(selected) == 1 and len(selected[0].data.splines) == 1:
            found = self.curves.chains(selected[0])
            if found:
                self._choose(selected[0], *found[0], plane)
        return super().invoke(context, event)

    # --- Picking the curve ---

    def _choose(self, obj, index, chain, plane):
        local = curve_geom.from_spline(obj.data.splines[index])
        parts = polycurve.parts_of(obj, index, local) if local is not None else [
            polycurve.Part(chain.count, chain.degree, 'NURBS')]
        normal = curve_normal(chain, np.array(tuple(plane.normal)), curve_geom.tolerance(chain))
        self.target = (obj, index, chain, parts, normal)
        self.highlight = [curve_geom.sample(chain, 24)[0]]

    def picks_curves(self):
        return self.target is None and not self.asking

    def on_hover(self, context, area, region, mouse):
        self.highlight = []
        if area is None:
            return
        targets = [obj for obj in context.selectable_objects if curve_edit.is_curve(obj)]
        hit = self.curves.under_mouse(targets, region, mouse, curve_edit._pick_radius(context))
        if hit is not None:
            self.highlight = [curve_geom.sample(hit[2], 24)[0]]

    def on_pick(self, context, area, region, mouse):
        targets = [obj for obj in context.selectable_objects if curve_edit.is_curve(obj)]
        hit = self.curves.under_mouse(targets, region, mouse, curve_edit._pick_radius(context))
        if hit is None:
            return None
        obj, index, chain, _ = hit
        self._choose(obj, index, chain, cplane.for_area(self.window.screen, area))
        return None

    # --- Options ---

    def _options(self):
        both = "Yes" if self.both_sides else "No"
        through = " ThroughPoint" if self.target is not None else ""
        return f"Distance={self.offset_distance:g} Corner={self.corner.title()}{through} BothSides={both}"

    def prompt(self):
        if self.asking:
            return f"Offset distance <{self.offset_distance:g}>"
        if self.target is None:
            return f"Select curve to offset ( {self._options()} )"
        if self.through:
            return f"Through point ( {self._options()} )"
        return f"Side to offset ( {self._options()} )"

    def on_option(self, context, name):
        if "distance".startswith(name):
            self.asking = True
            self.through = False
        elif "corner".startswith(name):
            self.corner = CORNERS[(CORNERS.index(self.corner) + 1) % len(CORNERS)]
        elif any(c.lower().startswith(name) for c in CORNERS):
            self.corner = next(c for c in CORNERS if c.lower().startswith(name))
        elif "bothsides".startswith(name):
            self.both_sides = not self.both_sides
        elif self.target is not None and "throughpoint".startswith(name):
            self.through = not self.through
        else:
            return False
        self._made = {}
        return True

    def on_number(self, context, value):
        if value <= 0:
            command_line.echo(self.window, "The distance must be more than zero", 'ERROR')
            return True
        self.offset_distance = value
        self.asking = False
        self.through = False
        self._made = {}
        return True

    def on_enter(self, context):
        if self.asking:
            self.asking = False
            return None
        return FINISH if self.target is None else None

    # --- Offsetting ---

    def _offsets(self, point):
        """[(chain, parts)] for the side of ``point`` (and the other with
        BothSides), or an error message."""
        obj, index, chain, parts, normal = self.target
        side = side_of(chain, normal, point)
        distance = distance_to(chain, normal, point) if self.through else self.offset_distance
        if distance <= curve_geom.tolerance(chain):
            return "The point is on the curve"
        key = (side, round(distance, 12), self.corner, self.both_sides)
        if key not in self._made:
            try:
                sides = (side, -side) if self.both_sides else (side,)
                self._made[key] = [offset_curve(chain, parts, obj.matrix_world, s * distance, normal, self.corner)
                                   for s in sides]
            except OffsetError as error:
                self._made[key] = str(error)
        return self._made[key]

    def after_track(self):
        self._shown = []
        if self.target is None or self.current is None:
            return
        made = self._offsets(np.array(tuple(self.current)))
        if not isinstance(made, str):
            self._shown = [curve_geom.sample(chain, 24)[0] for chain, _ in made]

    def preview(self):
        return [(points, 'highlight') for points in self.highlight] + [(points, 'curve') for points in self._shown]

    def snap_geometry(self):
        return [], []

    def on_point(self, context):
        if self.target is None or self.asking:
            self.points.pop()
            return None
        made = self._offsets(np.array(tuple(self.points[-1])))
        if isinstance(made, str):
            command_line.echo(self.window, made, 'ERROR')
            self.points.pop()
            return None
        obj = self.target[0]
        for chain, parts in made:
            curves.new_chain_object(context, obj.name, chain, parts)
        return FINISH

    def create(self, context):
        pass


_classes = (R2B_OT_offset,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
