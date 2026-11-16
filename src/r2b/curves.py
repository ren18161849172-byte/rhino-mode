# SPDX-License-Identifier: GPL-3.0-or-later
"""Curve drawing commands (MODE feature): Line, Polyline, Curve, InterpCrv,
Circle, Arc and Rectangle.

The results are Blender curve objects in the current layer (the active
collection), with the object origin at the center of the bounding box of
their points so Blender's own transforms pivot sensibly. As in Rhino, new
curves are not selected. Lines, polylines and rectangles are poly splines;
the others are NURBS splines (see nurbs.py), circles and arcs exact rational
ones, tagged for Osnap (Cen, Quad, Tan, ...). Shapes made of lines and arcs
(Polyline with Mode=Arc, rounded rectangles) are joined curves (see
polycurve.py), exact and with their arcs tagged.

Line options: BothSides (the first point is the middle), Vertical (along
the CPlane normal), Tangent (starting tangent to a curve, the start sliding
along it) and Perpendicular (starting on a curve, perpendicular to it).
"""

import math
from dataclasses import replace

import numpy as np

import bpy
from mathutils import Matrix, Vector

from . import command_line, curve_geom, nurbs, osnap, polycurve
from .getpoint import CANCEL, FINISH, GetPoints

# Points closer than this are the same point.
TOLERANCE = 1e-6


def _bounding_center(points):
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    return (low + high) / 2


def _new_object(context, name, spline_type, points, weights=None):
    """A curve object with one spline through ``points`` (world space)."""
    center = _bounding_center(points)
    data = bpy.data.curves.new(name, 'CURVE')
    data.dimensions = '3D'
    spline = data.splines.new(spline_type)
    spline.points.add(len(points) - 1)
    for spline_point, point, weight in zip(spline.points, points, weights or [1.0] * len(points)):
        spline_point.co = (*(point - center), weight)
    obj = bpy.data.objects.new(name, data)
    obj.location = center
    context.collection.objects.link(obj)
    return obj, spline


def new_curve_object(context, name, points, closed=False):
    """A poly spline through ``points``."""
    obj, spline = _new_object(context, name, 'POLY', points)
    spline.use_cyclic_u = closed
    return obj


def new_nurbs_object(context, name, points, order, closed=False):
    """A NURBS spline with control ``points``, clamped like Rhino's curves
    (or periodic when closed)."""
    obj, spline = _new_object(context, name, 'NURBS', points)
    spline.order_u = min(order, len(points))
    spline.use_cyclic_u = closed
    spline.use_endpoint_u = not closed
    spline.resolution_u = 24
    return obj


def new_arc_object(context, name, center, x_axis, y_axis, radius, start, sweep):
    """An exact circular arc (or circle, sweep 2π) as a rational NURBS,
    tagged for Osnap."""
    points, weights = nurbs.arc_points(center, x_axis, y_axis, radius, start, sweep)
    obj, spline = _new_object(context, name, 'NURBS', points, weights)
    spline.order_u = 3
    spline.use_bezier_u = True
    spline.use_endpoint_u = True
    spline.resolution_u = 16
    if sweep < 0:
        start, sweep = start + sweep, -sweep
    obj[osnap.ARC_PROPERTY] = {
        "center": tuple(center - obj.location), "x_axis": tuple(x_axis), "y_axis": tuple(y_axis),
        "radius": radius, "start": start % (2 * math.pi), "end": start % (2 * math.pi) + sweep,
    }
    return obj


def arc_pieces_chain(center, x_axis, y_axis, radius, start, sweep):
    """Homogeneous Bezier segments of an exact arc, and its Osnap tag."""
    points, weights = nurbs.arc_points(center, x_axis, y_axis, radius, start, sweep)
    control = np.array([(*(p * w), w) for p, w in zip(points, weights)])
    knots = curve_geom.blender_knots(len(control), 3, endpoint=True, bezier=True)
    if sweep < 0:
        start, sweep = start + sweep, -sweep
    start %= 2 * math.pi
    tag = {"center": list(center), "x_axis": list(x_axis), "y_axis": list(y_axis), "radius": radius,
           "start": start, "end": start + sweep}
    return curve_geom.nurbs_segments(control, 3, knots), tag


def new_chain_object(context, name, chain, parts):
    """A curve object for a world chain made of ``parts`` (polycurve.Part,
    arcs in world space), its origin at the middle of its bounding box."""
    points = (chain.segments[..., :3] / chain.segments[..., 3:]).reshape(-1, 3)
    center = Vector(tuple((points.min(axis=0) + points.max(axis=0)) / 2))
    to_local = Matrix.Translation(-center)
    parts = [replace(part, arc=polycurve.transform_arc(part.arc, to_local)) for part in parts]
    data = bpy.data.curves.new(name, 'CURVE')
    data.dimensions = '3D'
    polycurve.write(data, chain.transformed(to_local), parts)
    obj = bpy.data.objects.new(name, data)
    obj.location = center
    context.collection.objects.link(obj)
    polycurve.set_tags(obj, parts)
    return obj


def new_joined_object(context, name, pieces, closed=False):
    """A joined curve object of lines ('LINE', a, b) and exact arcs ('ARC',
    center, x_axis, y_axis, radius, start, sweep) end to end, world space."""
    chunks = []
    for piece in pieces:
        if piece[0] == 'LINE':
            line = np.array([[(*piece[1], 1.0), (*piece[2], 1.0)]])
            chunks.append((line, polycurve.Part(1, 1, 'POLY')))
        else:
            segments, tag = arc_pieces_chain(*piece[1:])
            chunks.append((segments, polycurve.Part(len(segments), 2, 'NURBS', tag)))
    degree = max(part.degree for _, part in chunks)
    segments = np.concatenate([curve_geom.elevate_degree(chunk, degree) for chunk, _ in chunks])
    return new_chain_object(context, name, curve_geom.Chain(segments, closed), [part for _, part in chunks])


def _curve_hit(context, region, mouse):
    """(world chain, parameter) of the visible curve under the mouse, or None."""
    from . import curve_edit

    curves = curve_edit.Curves()
    objects = [obj for obj in context.visible_objects if curve_edit.is_curve(obj)]
    hit = curves.under_mouse(objects, region, mouse, curve_edit._pick_radius(context))
    return (hit[2], hit[3]) if hit is not None else None


def tangent_parameter(chain, t, point, normal):
    """The parameter near t where the line from ``point`` touches the chain
    (in the plane with ``normal``), or None."""
    n = chain.count

    def f(u):
        return float(np.cross(curve_geom.evaluate(chain, u) - point, curve_geom.tangent(chain, u)) @ normal)

    step = 1e-5 * n
    for _ in range(60):
        value = f(t)
        slope = (f(t + step) - f(t - step)) / (2 * step)
        if abs(slope) < 1e-300:
            return None
        new = t - value / slope
        if not chain.closed:
            new = min(max(new, 0.0), float(n))
        if abs(new - t) < 1e-12:
            break
        t = new
    size = float(np.linalg.norm(curve_geom.evaluate(chain, t) - point)) * float(np.linalg.norm(curve_geom.tangent(chain, t)))
    return t if abs(f(t)) <= 1e-7 * max(size, 1e-12) else None


class R2B_OT_line(GetPoints, bpy.types.Operator):
    """Draw a line from two points"""

    bl_idname = "r2b.line"
    bl_label = "Line"

    MODES = ("bothsides", "vertical", "tangent", "perpendicular")

    def invoke(self, context, event):
        self.mode = None
        self.anchor = None  # Tangent, Perpendicular: (world chain, parameter) of the curve.
        self._mouse = None
        return super().invoke(context, event)

    def prompt(self):
        if self.points:
            return "End of line"
        return {None: "Start of line ( BothSides Vertical Tangent Perpendicular )",
                "bothsides": "Middle of line", "vertical": "Start of vertical line",
                "tangent": "Start of line tangent to curve",
                "perpendicular": "Start of line perpendicular to curve"}[self.mode]

    def on_option(self, context, name):
        if self.points:
            return False
        for mode in self.MODES:
            if mode.startswith(name):
                self.mode = mode
                return True
        return False

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        self._mouse = (region, mouse)
        if len(self.points) == 1 and self.mode == "vertical":
            from . import solids

            return solids.elevator(context, self.line_plane, self.points[0], region, rv3d, mouse, point, snapped)[0]
        if len(self.points) == 1 and self.mode == "perpendicular":
            start = self.points[0]
            return start + self.direction * (point - start).dot(self.direction)
        return point

    def _ends(self, end):
        """(start, end) of the line for a second point."""
        start = self.points[0]
        if self.mode == "bothsides":
            return start * 2 - end, end
        if self.mode == "tangent":
            chain, t = self.anchor
            found = tangent_parameter(chain, t, np.array(end), np.array(self.line_plane.normal))
            if found is None:
                return None
            return Vector(curve_geom.evaluate(chain, found)), end
        return start, end

    def on_point(self, context):
        if len(self.points) == 1:
            self.line_plane = self.plane
            if self.mode in ("tangent", "perpendicular"):
                hit = _curve_hit(context, *self._mouse) if self._mouse else None
                if hit is None:
                    self.points.pop()
                    command_line.echo(self.window, "Pick a point on a curve", 'ERROR')
                    return None
                self.anchor = hit
                chain, t = hit
                self.points[0] = Vector(curve_geom.evaluate(chain, t))
                if self.mode == "perpendicular":
                    along = Vector(curve_geom.tangent(chain, t))
                    self.direction = self.line_plane.normal.cross(along).normalized()
            return None
        ends = self._ends(self.points[1])
        if ends is None or (ends[1] - ends[0]).length < TOLERANCE:
            self.points.pop()
            message = ("No line from there touches the curve" if ends is None
                       else "The end of the line must differ from its start")
            command_line.echo(self.window, message, 'ERROR')
            return None
        self.result = ends
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def preview(self):
        if not self.points or self.current is None:
            return []
        ends = self._ends(self.current)
        return [(list(ends), 'curve')] if ends else []

    def snap_geometry(self):
        return self.points, []

    def create(self, context):
        new_curve_object(context, "Line", list(self.result))


def tangent_arc(start, direction, end, normal):
    """('ARC', ...) piece from ``start`` leaving in ``direction`` to ``end``
    (in the plane with ``normal``), with its end direction; a line if the end
    is straight ahead."""
    chord = end - start
    chord -= normal * chord.dot(normal)
    side = normal.cross(direction).normalized()
    toward = chord.dot(side)
    if abs(toward) < 1e-9 * max(chord.length, 1e-12) or chord.length < TOLERANCE:
        return ('LINE', start, end), direction
    radius = chord.length_squared / (2 * toward)  # Signed: negative turns the other way.
    center = start + side * radius
    x_axis = (start - center).normalized()
    y_axis = normal.cross(x_axis)
    sweep = 2 * math.atan2(chord.dot(side) * math.copysign(1, radius), chord.dot(direction))
    if direction.dot(y_axis) < 0:
        sweep = -sweep
    end_angle = sweep
    out = (-x_axis * math.sin(end_angle) + y_axis * math.cos(end_angle)) * math.copysign(1, sweep)
    return ('ARC', center, x_axis, y_axis, abs(radius), 0.0, sweep), out.normalized()


class R2B_OT_polyline(GetPoints, bpy.types.Operator):
    """Draw a polyline through points; Enter ends it. Mode=Arc adds arcs
    tangent to the previous segment"""

    bl_idname = "r2b.polyline"
    bl_label = "Polyline"

    closed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        self.arc_mode = False
        self.kinds = []  # Per segment: 'LINE' or 'ARC'.
        return super().invoke(context, event)

    def prompt(self):
        mode = f"Mode={'Arc' if self.arc_mode else 'Line'}"
        if not self.points:
            return f"Start of polyline ( {mode} )"
        options = f"{mode} Undo Close" if len(self.points) >= 3 else f"{mode} Undo"
        return f"Next point of polyline. Press Enter when done ( {options} )"

    def on_point(self, context):
        if len(self.points) >= 2 and (self.points[-1] - self.points[-2]).length < TOLERANCE:
            self.points.pop()  # Same point twice.
        elif len(self.points) >= 2:
            # The first segment has no direction to be tangent to: a line.
            self.kinds.append('ARC' if self.arc_mode and len(self.points) > 2 else 'LINE')
        return None

    def on_enter(self, context):
        return FINISH if len(self.points) >= 2 else CANCEL

    def on_option(self, context, name):
        if "mode".startswith(name):
            self.arc_mode = not self.arc_mode
            return True
        if "undo".startswith(name):
            if self.points:
                self.points.pop()
                del self.kinds[len(self.points) - 1:]
            return True
        if "close".startswith(name) and len(self.points) >= 3:
            self.closed = True
            return FINISH
        return False

    def _pieces(self, points, kinds):
        normal = self.plane.normal
        pieces, direction = [], None
        for (a, b), kind in zip(zip(points[:-1], points[1:]), kinds):
            if kind == 'ARC' and direction is not None:
                piece, direction = tangent_arc(a, direction, b, normal)
            else:
                piece, direction = ('LINE', a, b), (b - a).normalized()
            pieces.append(piece)
        return pieces

    def preview(self):
        if not self.points or self.current is None:
            return []
        points = self.points + [self.current]
        kinds = self.kinds + ['ARC' if self.arc_mode and len(self.points) >= 2 else 'LINE']
        lines = []
        for piece in self._pieces(points, kinds):
            if piece[0] == 'LINE':
                lines.append(([piece[1], piece[2]], 'curve'))
            else:
                _, center, x_axis, y_axis, radius, start, sweep = piece
                lines.append(([center + (x_axis * math.cos(start + sweep * i / 32) + y_axis * math.sin(start + sweep * i / 32)) * radius
                               for i in range(33)], 'curve'))
        return lines

    def create(self, context):
        if 'ARC' not in self.kinds:
            new_curve_object(context, "Polyline", self.points, closed=self.closed)
            return
        points, kinds = list(self.points), list(self.kinds)
        if self.closed:
            points.append(points[0])
            kinds.append('LINE')
        new_joined_object(context, "Polyline", self._pieces(points, kinds), closed=self.closed)


class R2B_OT_curve(GetPoints, bpy.types.Operator):
    """Draw a curve from control points; Enter ends it"""

    bl_idname = "r2b.curve"
    bl_label = "Curve"

    degree: bpy.props.IntProperty(name="Degree", default=3, min=1, max=5, options={'SKIP_SAVE'})
    closed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        self.asking_degree = False
        return super().invoke(context, event)

    def prompt(self):
        if self.asking_degree:
            return f"Degree <{self.degree}>"
        if not self.points:
            return f"Start of curve ( Degree={self.degree} )"
        options = "Degree Undo Close" if len(self.points) >= 3 else "Degree Undo"
        return f"Next point. Press Enter when done ( {options} )"

    def on_number(self, context, value):
        if not self.asking_degree:
            return None
        if value != int(value) or not 1 <= value <= 5:
            command_line.echo(self.window, "The degree must be a whole number from 1 to 5", 'ERROR')
            return True
        self.degree = int(value)
        self.asking_degree = False
        return True

    def on_point(self, context):
        if len(self.points) >= 2 and (self.points[-1] - self.points[-2]).length < TOLERANCE:
            self.points.pop()
        return None

    def on_enter(self, context):
        if self.asking_degree:
            self.asking_degree = False
            return None
        return FINISH if len(self.points) >= 2 else CANCEL

    def on_option(self, context, name):
        if "degree".startswith(name):
            self.asking_degree = True
            return True
        if "undo".startswith(name):
            if self.points:
                self.points.pop()
            return True
        if "close".startswith(name) and len(self.points) >= 3:
            self.closed = True
            return FINISH
        return False

    def preview(self):
        control = self.points + ([self.current] if self.current is not None else [])
        lines = [(control, 'guide')]
        if len(control) >= 3:
            lines.append((nurbs.evaluate(control, self.degree + 1, samples=96), 'curve'))
        return lines

    def snap_geometry(self):
        return self.points, []

    def create(self, context):
        new_nurbs_object(context, "Curve", self.points, self.degree + 1, closed=self.closed)


def periodic_interpolation(data):
    """(control points, order) of a closed (cyclic, uniform) NURBS curve
    through ``data``, as Blender evaluates cyclic splines."""
    data = np.asarray([tuple(p) for p in data], dtype=np.float64)
    count = len(data)
    order = min(4, count)
    knots = curve_geom.blender_knots(count, order, cyclic=True)
    matrix = np.zeros((count, count))
    for row in range(count):
        values = nurbs.basis(knots, order, knots[order - 1] + row)
        for column, value in enumerate(values[:count + order - 1]):
            matrix[row, column % count] += value
    return [Vector(p) for p in np.linalg.solve(matrix, data)], order


class R2B_OT_interp_curve(GetPoints, bpy.types.Operator):
    """Draw a curve through points; Enter ends it, Close closes it"""

    bl_idname = "r2b.interp_curve"
    bl_label = "Interpolate Points"

    closed: bpy.props.BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def prompt(self):
        if not self.points:
            return "Start of curve"
        options = "Undo Close" if len(self.points) >= 3 else "Undo"
        return f"Next point. Press Enter when done ( {options} )"

    def on_point(self, context):
        if len(self.points) >= 2 and (self.points[-1] - self.points[-2]).length < TOLERANCE:
            self.points.pop()
        return None

    def on_enter(self, context):
        return FINISH if len(self.points) >= 2 else CANCEL

    def on_option(self, context, name):
        if "undo".startswith(name):
            if self.points:
                self.points.pop()
            return True
        if "close".startswith(name) and len(self.points) >= 3:
            self.closed = True
            return FINISH
        return False

    def preview(self):
        through = self.points + ([self.current] if self.current is not None else [])
        if len(through) < 3:
            return [(through, 'curve')]
        return [(nurbs.evaluate(nurbs.interpolate(through), 4, samples=96), 'curve')]

    def create(self, context):
        if self.closed:
            control, order = periodic_interpolation(self.points)
            new_nurbs_object(context, "Curve", control, order, closed=True)
        else:
            new_nurbs_object(context, "Curve", nurbs.interpolate(self.points), 4)


def circle_through(a, b, c):
    """(center, normal, radius) of the circle through three points, or None
    if they are in line. The normal makes a -> b -> c counterclockwise."""
    ab, ac = b - a, c - a
    normal = ab.cross(ac)
    if normal.length < 1e-12 * max(ab.length, ac.length, 1e-12) ** 2:
        return None
    center = a + (normal.cross(ab) * ac.length_squared + ac.cross(normal) * ab.length_squared) / (2 * normal.length_squared)
    return center, normal.normalized(), (a - center).length


def circle_tangent_to(first, second, radius, plane, corner=None):
    """(center, tangent point 1, tangent point 2, parameter 1, parameter 2)
    of a circle of ``radius`` touching two curves near where they were
    picked, or None. ``first`` and ``second``: (world chain, parameter,
    picked point). ``corner``: the parameters where the curves meet, if
    they do: a circle touching both on the picked sides of it comes first
    (a pick nearer the corner than the circle touches is nearer to the
    circle in the next wedge)."""
    (chain1, guess1, pick1), (chain2, guess2, pick2) = first, second
    # In float64 throughout: mathutils vectors are single precision, too
    # coarse for the derivatives and the gap far from the origin.
    axes = np.array((tuple(plane.x_axis), tuple(plane.y_axis)), dtype=float)
    up = np.array(tuple(plane.normal), dtype=float)

    def center_of(chain, t, side):
        normal = np.cross(up, curve_geom.tangent(chain, t))
        length = np.linalg.norm(normal)
        if length > 1e-300:
            normal = normal / length
        return np.asarray(curve_geom.evaluate(chain, t), dtype=float) + normal * (side * radius)

    def clamp(chain, t):
        return t if chain.closed else min(max(t, 0.0), float(chain.count))

    best = None
    for side1 in (1, -1):
        for side2 in (1, -1):
            def gap_at(t1, t2):
                return axes @ (center_of(chain1, t1, side1) - center_of(chain2, t2, side2))

            # Damped Gauss-Newton (Levenberg-Marquardt): the centers' paths
            # (the offset curves) may only touch, e.g. a circle filling a
            # channel exactly as wide as it, where the Jacobian is singular.
            t1, t2 = guess1, guess2
            residual = gap_at(t1, t2)
            damping = 1e-6
            for _ in range(200):
                h1, h2 = 1e-8 * max(chain1.count, 1), 1e-8 * max(chain2.count, 1)
                d1 = (gap_at(t1 + h1, t2) - residual) / h1
                d2 = (gap_at(t1, t2 + h2) - residual) / h2
                jacobian = np.stack([d1, d2], axis=1)
                normal = jacobian.T @ jacobian
                gradient = jacobian.T @ residual
                scale = max(np.trace(normal), 1e-300)
                while True:
                    step = np.linalg.solve(normal + damping * scale * np.eye(2), -gradient)
                    n1, n2 = clamp(chain1, t1 + step[0]), clamp(chain2, t2 + step[1])
                    trial = gap_at(n1, n2)
                    if trial @ trial < residual @ residual or damping > 1e12:
                        break
                    damping *= 10
                if trial @ trial >= residual @ residual:
                    break
                t1, t2, residual = n1, n2, trial
                damping = max(damping / 10, 1e-12)
                if abs(step).max() < 1e-14 or residual @ residual < 1e-30:
                    break
            gap = np.linalg.norm(residual)
            if gap > 1e-7 * max(radius, 1.0):
                continue
            touch1, touch2 = Vector(curve_geom.evaluate(chain1, t1)), Vector(curve_geom.evaluate(chain2, t2))
            other_side = corner is not None and ((t1 - corner[0]) * (guess1 - corner[0]) < 0
                                                 or (t2 - corner[1]) * (guess2 - corner[1]) < 0)
            score = (other_side, (touch1 - pick1).length + (touch2 - pick2).length)
            if best is None or score < best[0]:
                best = (score, Vector(center_of(chain1, t1, side1)), touch1, touch2, t1, t2)
    return best[1:] if best else None


class R2B_OT_circle(GetPoints, bpy.types.Operator):
    """Draw a circle: center and radius, two or three points, or tangent to two curves"""

    bl_idname = "r2b.circle"
    bl_label = "Circle"

    MODES = ("2point", "3point", "tangent")

    def invoke(self, context, event):
        self.diameter = False
        self.mode = None
        self.tangents = []  # Tangent: (world chain, parameter, picked point) per curve.
        self.result = None  # (center, x axis, y axis, radius)
        self._mouse = None
        return super().invoke(context, event)

    def prompt(self):
        if self.mode is None:
            if not self.points:
                return "Center of circle ( 2Point 3Point Tangent )"
            return "Diameter ( Radius )" if self.diameter else "Radius ( Diameter )"
        if self.mode == "2point":
            return ("Start of diameter", "End of diameter")[len(self.points)]
        if self.mode == "3point":
            return ("First point on circle", "Second point on circle", "Third point on circle")[len(self.points)]
        return ("First tangent curve", "Second tangent curve", "Radius")[len(self.tangents)]

    def on_option(self, context, name):
        if not self.points and not self.tangents and self.mode is None:
            for mode in self.MODES:
                if mode.startswith(name):
                    self.mode = mode
                    return True
        if self.mode is None and self.points and "diameter".startswith(name):
            self.diameter = True
            return True
        if self.mode is None and self.points and "radius".startswith(name):
            self.diameter = False
            return True
        return False

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        self._mouse = (region, mouse)
        return point

    def _radius_to(self, point):
        plane = self.circle_plane
        local = plane.to_local(point) - plane.to_local(self.points[0])
        return math.hypot(local.x, local.y)

    def _from_points(self, points):
        """(center, x axis, y axis, radius) for the points of 2Point/3Point."""
        plane = self.circle_plane
        if self.mode == "2point":
            a, b = points
            local = plane.to_local(b) - plane.to_local(a)
            center = (a + b) / 2
            return center, plane.x_axis, plane.y_axis, math.hypot(local.x, local.y) / 2
        found = circle_through(*points)
        if found is None:
            return None
        center, normal, radius = found
        x_axis = (points[0] - center).normalized()
        return center, x_axis, normal.cross(x_axis), radius

    def on_point(self, context):
        if len(self.points) == 1:
            self.circle_plane = self.plane
        if self.mode == "tangent":
            point = self.points.pop()
            hit = _curve_hit(context, *self._mouse) if self._mouse else None
            if hit is None:
                return self._error_none("Pick a point on a curve")
            if len(self.tangents) < 2:
                self.circle_plane = self.plane
                self.tangents.append((hit[0], hit[1], point))
            return None
        if self.mode is None and len(self.points) == 2:
            return self._finish_radius(self._radius_to(self.points.pop()))
        needed = {"2point": 2, "3point": 3}.get(self.mode)
        if needed and len(self.points) == needed:
            found = self._from_points(self.points)
            if found is None or found[3] < TOLERANCE:
                self.points.pop()
                return self._error_none("The points must not be in one line or at one place")
            self.result = found
            return FINISH
        return None

    def _error_none(self, message):
        command_line.echo(self.window, message, 'ERROR')
        return None

    def _finish_radius(self, radius):
        if radius < TOLERANCE:
            return self._error_none("The radius must be more than zero")
        plane = self.circle_plane
        self.result = (self.points[0], plane.x_axis, plane.y_axis, radius)
        return FINISH

    def on_number(self, context, value):
        if self.mode is None and self.points:
            return self._finish_radius(value / 2 if self.diameter else value) or True
        if self.mode == "tangent" and len(self.tangents) == 2:
            if value <= 0:
                return self._error_none("The radius must be more than zero") or True
            found = circle_tangent_to(*self.tangents, value, self.circle_plane)
            if found is None:
                return self._error_none("No circle of that size touches both curves there") or True
            plane = self.circle_plane
            self.result = (found[0], plane.x_axis, plane.y_axis, value)
            return FINISH
        return None

    def on_enter(self, context):
        return CANCEL

    def _circle_points(self, center, x_axis, y_axis, radius):
        return [center + (x_axis * math.cos(a) + y_axis * math.sin(a)) * radius
                for a in (i * 2 * math.pi / 96 for i in range(97))]

    def preview(self):
        if self.current is None or not self.points or self.mode == "tangent":
            return []
        if self.mode is None:
            plane = self.circle_plane
            circle = self._circle_points(self.points[0], plane.x_axis, plane.y_axis, self._radius_to(self.current))
            return [([self.points[0], self.current], 'guide'), (circle, 'curve')]
        points = self.points + [self.current]
        if (self.mode == "2point" and len(points) == 2) or (self.mode == "3point" and len(points) == 3):
            found = self._from_points(points)
            if found is not None:
                return [(self._circle_points(*found), 'curve')]
        return [(points, 'guide')]

    def snap_geometry(self):
        return self.points, []

    def create(self, context):
        center, x_axis, y_axis, radius = self.result
        new_arc_object(context, "Circle", center, x_axis, y_axis, radius, 0.0, 2 * math.pi)


class _CenterRadius:
    """Arc from its center: a center, then points in the center's CPlane."""

    def _local(self, point):
        """(x, y) of ``point`` in the plane of the center's CPlane."""
        local = self.circle_plane.to_local(point) - self.circle_plane.to_local(self.points[0])
        return local.x, local.y

    def _radius_to(self, point):
        x, y = self._local(point)
        return math.hypot(x, y)

    def snap_geometry(self):
        return self.points, []


class R2B_OT_arc(_CenterRadius, GetPoints, bpy.types.Operator):
    """Draw an arc from its center, start point and end point or angle; or
    (StartPoint) from its start, end and a point on it or the start direction"""

    bl_idname = "r2b.arc"
    bl_label = "Arc"

    def invoke(self, context, event):
        self.sweep = 0.0
        self.tracked_angle = None
        self.start_point = False  # StartPoint: start, end, then a point on the arc.
        self.direction = False  # StartPoint: the third point gives the start direction.
        return super().invoke(context, event)

    def prompt(self):
        if self.start_point:
            third = "Direction at start ( Point )" if self.direction else "Point on arc ( Direction )"
            return ("Start of arc", "End of arc", third)[len(self.points)]
        if not self.points:
            return "Center of arc ( StartPoint )"
        return ("Center of arc", "Start of arc", "End point or angle")[len(self.points)]

    def on_option(self, context, name):
        if not self.points and "startpoint".startswith(name):
            self.start_point = True
            return True
        if self.start_point and len(self.points) == 2:
            if "direction".startswith(name):
                self.direction = True
                return True
            if "point".startswith(name):
                self.direction = False
                return True
        return False

    def after_track(self):
        # Follow the mouse around the center, so the arc goes the way it moves.
        if self.start_point or len(self.points) != 2 or self.current is None:
            return
        x, y = self._local(self.current)
        if math.hypot(x, y) < TOLERANCE:
            return
        angle = math.atan2(y, x)
        if self.tracked_angle is not None:
            step = (angle - self.tracked_angle + math.pi) % (2 * math.pi) - math.pi
            self.sweep += step
        self.tracked_angle = angle

    def _three_point(self, third):
        """('ARC', center, x, y, radius, 0, sweep) of the StartPoint arc for a
        third point, or None."""
        start, end = self.points
        if self.direction:
            piece, _ = tangent_arc(start, (third - start).normalized(), end, self.circle_plane.normal)
            return piece if piece[0] == 'ARC' else None
        found = circle_through(start, third, end)
        if found is None:
            return None
        center, normal, radius = found
        x_axis = (start - center).normalized()
        y_axis = normal.cross(x_axis)
        offset = end - center
        sweep = math.atan2(offset.dot(y_axis), offset.dot(x_axis)) % (2 * math.pi)
        return ('ARC', center, x_axis, y_axis, radius, 0.0, sweep)

    def on_point(self, context):
        if len(self.points) == 1:
            self.circle_plane = self.plane  # The CPlane where the first point was picked.
        if self.start_point:
            if len(self.points) == 2 and (self.points[1] - self.points[0]).length < TOLERANCE:
                self.points.pop()
                command_line.echo(self.window, "The end must differ from the start", 'ERROR')
            if len(self.points) == 3:
                self.result = self._three_point(self.points.pop())
                if self.result is None:
                    command_line.echo(self.window, "No arc goes that way", 'ERROR')
                    return None
                return FINISH
            return None
        if len(self.points) == 2:
            if self._radius_to(self.points[1]) < TOLERANCE:
                self.points.pop()
                command_line.echo(self.window, "The start must differ from the center", 'ERROR')
                return None
            self.sweep = 0.0
            self.tracked_angle = self._start_angle()
            return None
        if len(self.points) == 3:
            end = self.points.pop()
            x, y = self._local(end)
            angle = math.atan2(y, x)
            start = self._start_angle()
            # The end point's angle, on the side the mouse went round.
            span = (angle - start) % (2 * math.pi)
            sweep = span if self.sweep >= 0 else span - 2 * math.pi
            if abs(sweep) < 1e-9:
                command_line.echo(self.window, "The end must differ from the start", 'ERROR')
                return None
            self.sweep = sweep
            return FINISH
        return None

    def on_number(self, context, value):
        if self.start_point or len(self.points) != 2:
            return None
        if abs(value) < 1e-9 or abs(value) > 360:
            command_line.echo(self.window, "The angle must be between -360 and 360 degrees, not zero", 'ERROR')
            return True
        self.sweep = math.radians(value)
        return FINISH

    def on_enter(self, context):
        return CANCEL

    def _start_angle(self):
        x, y = self._local(self.points[1])
        return math.atan2(y, x)

    def _arc_points(self, center, x_axis, y_axis, radius, start, sweep):
        return [center + (x_axis * math.cos(start + sweep * t) + y_axis * math.sin(start + sweep * t)) * radius
                for t in (i / 64 for i in range(65))]

    def preview(self):
        if self.start_point:
            if self.current is None or not self.points:
                return []
            if len(self.points) == 1:
                return [([self.points[0], self.current], 'guide')]
            piece = self._three_point(self.current)
            lines = [([self.points[0], self.current], 'guide')] if self.direction else []
            return lines + ([(self._arc_points(*piece[1:]), 'curve')] if piece else [])
        if len(self.points) < 2:
            return super().preview()
        plane = self.circle_plane
        arc = self._arc_points(self.points[0], plane.x_axis, plane.y_axis, self._radius_to(self.points[1]),
                               self._start_angle(), self.sweep)
        guide = [self.points[1], self.points[0]] + ([self.current] if self.current is not None else [])
        return [(guide, 'guide'), (arc, 'curve')]

    def create(self, context):
        if self.start_point:
            new_arc_object(context, "Arc", *self.result[1:])
            return
        plane = self.circle_plane
        new_arc_object(context, "Arc", self.points[0], plane.x_axis, plane.y_axis,
                       self._radius_to(self.points[1]), self._start_angle(), self.sweep)


def rounded_pieces(corners, radius, normal):
    """Pieces (see new_joined_object) of a polygon with corners rounded by
    ``radius``."""
    count = len(corners)
    edges = [(corners[(i + 1) % count] - corners[i]).normalized() for i in range(count)]
    pieces = []
    for i in range(count):
        corner = corners[(i + 1) % count]
        before, after = edges[i], edges[(i + 1) % count]
        pieces.append(('LINE', corners[i] + before * radius, corner - before * radius))
        start, end = corner - before * radius, corner + after * radius
        center = start + after * radius
        x_axis = (start - center).normalized()
        y_axis = normal.cross(x_axis)
        sweep = math.pi / 2 if (end - center).dot(y_axis) > 0 else -math.pi / 2
        pieces.append(('ARC', center, x_axis, y_axis, radius, 0.0, sweep))
    return pieces


class R2B_OT_rectangle(GetPoints, bpy.types.Operator):
    """Draw a rectangle from two opposite corners, its center and a corner, or
    an edge and its width (3Point); Rounded rounds the corners"""

    bl_idname = "r2b.rectangle"
    bl_label = "Rectangle"

    def invoke(self, context, event):
        self.method = None  # None: two corners, "center", "3point".
        self.rounded = False
        self.radius = None
        self.corners = None
        return super().invoke(context, event)

    def _options(self):
        return f"( Center 3Point Rounded={'Yes' if self.rounded else 'No'} )"

    def prompt(self):
        if self.corners is not None:
            return "Radius of the corners"
        if not self.points:
            first = {None: "First corner of rectangle", "center": "Center of rectangle",
                     "3point": "Start of edge"}[self.method]
            return f"{first} {self._options()}"
        if self.method == "3point":
            return ("Start of edge", "End of edge", "Width")[len(self.points)]
        return "Other corner"

    def on_option(self, context, name):
        if self.points or self.corners is not None:
            return False
        if "center".startswith(name):
            self.method = "center"
        elif "3point".startswith(name):
            self.method = "3point"
        elif "rounded".startswith(name):
            self.rounded = not self.rounded
        else:
            return False
        return True

    def _corners(self, points):
        plane = self.rectangle_plane
        if self.method == "3point":
            a, b, c = points
            along = b - a
            along -= plane.normal * along.dot(plane.normal)
            if along.length < TOLERANCE:
                return None
            across = plane.normal.cross(along).normalized()
            width = (c - a).dot(across)
            return [a, a + along, a + along + across * width, a + across * width]
        a, b = plane.to_local(points[0]), plane.to_local(points[1])
        if self.method == "center":
            a, b = a * 2 - b, b
        return [plane.to_world(x, y, a.z) for x, y in ((a.x, a.y), (b.x, a.y), (b.x, b.y), (a.x, b.y))]

    def _valid(self, corners):
        return (corners is not None and (corners[1] - corners[0]).length >= TOLERANCE
                and (corners[3] - corners[0]).length >= TOLERANCE)

    def on_point(self, context):
        if len(self.points) == 1:
            self.rectangle_plane = self.plane
            return None
        if self.corners is not None:  # A point for the radius: its distance to the nearest corner.
            point = self.points.pop()
            return self._finish(min((point - corner).length for corner in self.corners))
        needed = 3 if self.method == "3point" else 2
        if len(self.points) < needed:
            return None
        corners = self._corners(self.points)
        if not self._valid(corners):
            self.points.pop()
            command_line.echo(self.window, "The rectangle needs a width and a height", 'ERROR')
            return None
        self.corners = corners
        if self.rounded:
            self.points = [self.points[0]]
            return None
        return FINISH

    def _finish(self, radius):
        sides = [(self.corners[1] - self.corners[0]).length, (self.corners[3] - self.corners[0]).length]
        if radius < 0 or radius > min(sides) / 2 + TOLERANCE:
            command_line.echo(self.window, f"The radius must be between 0 and {min(sides) / 2:g}", 'ERROR')
            return None
        self.radius = min(radius, min(sides) / 2)
        return FINISH

    def on_number(self, context, value):
        if self.corners is not None:
            return self._finish(value) or True
        return None

    def on_enter(self, context):
        return CANCEL

    def _outline(self, corners, radius):
        if not radius:
            return [(corners + corners[:1], 'curve')]
        lines = []
        for piece in rounded_pieces(corners, radius, self.rectangle_plane.normal):
            if piece[0] == 'LINE':
                lines.append(([piece[1], piece[2]], 'curve'))
            else:
                _, center, x_axis, y_axis, r, start, sweep = piece
                lines.append(([center + (x_axis * math.cos(start + sweep * i / 16) +
                                         y_axis * math.sin(start + sweep * i / 16)) * r for i in range(17)], 'curve'))
        return lines

    def preview(self):
        if not self.points or self.current is None:
            return []
        if self.corners is not None:
            radius = min((self.current - corner).length for corner in self.corners)
            sides = [(self.corners[1] - self.corners[0]).length, (self.corners[3] - self.corners[0]).length]
            return self._outline(self.corners, min(radius, min(sides) / 2))
        points = self.points + [self.current]
        if self.method == "3point" and len(points) < 3:
            return [(points, 'guide')]
        corners = self._corners(points)
        return self._outline(corners, None) if self._valid(corners) else []

    def snap_geometry(self):
        return self.points, []

    def create(self, context):
        if self.radius:
            new_joined_object(context, "Rectangle", rounded_pieces(self.corners, self.radius,
                                                                    self.rectangle_plane.normal), closed=True)
        else:
            new_curve_object(context, "Rectangle", self.corners, closed=True)


_classes = (R2B_OT_line, R2B_OT_polyline, R2B_OT_curve, R2B_OT_interp_curve, R2B_OT_circle, R2B_OT_arc,
            R2B_OT_rectangle)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
