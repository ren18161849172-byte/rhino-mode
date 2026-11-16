# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact curve math for the curve editing commands (Trim, Split, ...).

A spline is handled as a *chain*: consecutive rational Bezier segments of one
degree, in homogeneous coordinates (x*w, y*w, z*w, w), each an array of
degree + 1 control points. Every Blender spline converts exactly:

* POLY: one degree 1 segment per edge
* BEZIER: one cubic segment per pair of points
* NURBS: Bezier extraction (knot insertion) of the knot vector Blender makes
  for its knot modes (uniform, Endpoint, Bezier), any order, cyclic or not

A position on a chain is a parameter t from 0 to the number of segments:
segment floor(t) at u = t - floor(t). Pieces of a chain are cut exactly (de
Casteljau), and Blender shows any chain exactly (``to_spline``): as a POLY
spline (degree 1), a BEZIER spline (cubic) or a NURBS spline in Bezier and
Endpoint knot mode with order = degree + 1. Blender's NURBS splines only
have uniform knots, so a cut free-form curve gets more control points, but
not a different shape.

Intersections are found by subdividing segment pairs whose control point
boxes overlap until both are nearly flat, then refined with Newton's method
on the exact curves. Straight segments lying along each other overlap; the
ends of the overlap count as intersections. Curved segments that overlap
(e.g. two arcs of one circle) are not reported.
"""

import math
from dataclasses import dataclass

import numpy as np

# Parameter differences below this are the same place.
PARAM_EPS = 1e-9
# Subdivide a segment until its control points are within this fraction of
# the segment's size from its chord; Newton's method does the rest.
FLAT_FRACTION = 1e-3
MAX_DEPTH = 50
# Distance tolerance relative to the size of the curves involved.
RELATIVE_TOLERANCE = 1e-5


@dataclass
class Chain:
    segments: np.ndarray  # (count, degree + 1, 4), homogeneous
    closed: bool = False  # Cyclic: the last segment ends where the first starts.

    @property
    def degree(self):
        return self.segments.shape[1] - 1

    @property
    def count(self):
        return len(self.segments)

    def start(self):
        return _cartesian(self.segments[0])[0]

    def end(self):
        return _cartesian(self.segments[-1])[-1]

    def transformed(self, matrix):
        """The chain moved by an affine 4x4 matrix (e.g. to world space)."""
        return Chain(self.segments @ np.array(matrix, dtype=np.float64).T, self.closed)

    def size(self):
        points = _cartesian(self.segments.reshape(-1, 4))
        return float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))


def tolerance(*chains):
    """Distance under which points of these chains are the same."""
    return RELATIVE_TOLERANCE * max(max(chain.size() for chain in chains), 1e-9)


def _cartesian(points):
    return points[..., :3] / points[..., 3:]


# --- Blender splines ------------------------------------------------------------

def blender_knots(count, order, cyclic=False, endpoint=False, bezier=False):
    """The knot vector Blender makes for a NURBS spline (``calculate_knots``
    in Blender's curve code), for ``count`` control points."""
    repeat_inner = order - 1 if bezier else 1
    if endpoint:
        head = order - (1 if cyclic else 0)
    else:
        head = min(2, repeat_inner) if bezier else 1
    tail = 2 * order - 1 if cyclic else (order if endpoint else 0)
    size = count + order + (order - 1 if cyclic else 0)
    knots = np.zeros(size)
    current = 0.0
    offset = 1 if endpoint and cyclic else 0
    if offset:
        knots[0] = current
        current += 1.0
    remaining = head
    for i in range(offset, size - tail):
        knots[i] = current
        remaining -= 1
        if remaining == 0:
            current += 1.0
            remaining = repeat_inner
    for i in range(tail):
        knots[size - tail + i] = current + (knots[i] - knots[0])
    return knots


def _insert_knot(knots, control, degree, value):
    """Boehm's knot insertion: ``value`` inserted once."""
    count = len(control)
    k = int(np.searchsorted(knots, value, side='right')) - 1
    k = min(max(k, degree), count - 1)
    new = np.empty((count + 1, control.shape[1]))
    new[:k - degree + 1] = control[:k - degree + 1]
    for i in range(k - degree + 1, k + 1):
        span = knots[i + degree] - knots[i]
        alpha = (value - knots[i]) / span if span > 0 else 0.0
        new[i] = alpha * control[i] + (1.0 - alpha) * control[i - 1]
    new[k + 1:] = control[k:]
    return np.insert(knots, k + 1, value), new


def nurbs_segments(control, order, knots, cyclic=False):
    """Bezier segments of a NURBS curve: homogeneous ``control`` points
    (count x 4) and Blender's ``knots`` for them. A cyclic curve wraps
    around its first order - 1 points, as Blender evaluates it."""
    control = np.asarray(control, dtype=np.float64)
    if cyclic:
        control = np.concatenate([control, control[:order - 1]])
    degree = order - 1
    knots = np.asarray(knots, dtype=np.float64)
    start, end = knots[degree], knots[len(control)]
    # Every knot of the evaluated range repeated `degree` times: the curve
    # between two of them is then a Bezier segment.
    for value in np.unique(knots[(knots >= start) & (knots <= end)]):
        for _ in range(degree - int(np.sum(knots == value))):
            knots, control = _insert_knot(knots, control, degree, value)
    segments = [control[k - degree:k + 1] for k in range(degree, len(control))
                if start <= knots[k] < knots[k + 1] <= end]
    return np.array(segments).reshape(-1, order, 4)


def _valid_nurbs(count, order, cyclic, endpoint, bezier):
    """Whether Blender evaluates a NURBS spline as a curve (its
    ``check_valid_num_and_order``)."""
    if count < order:
        return False
    if bezier:
        if not endpoint and count <= order:
            return False
        return not cyclic or count % (order - 1) == 0
    return True


def from_spline(spline):
    """The chain of a Blender spline in its object's local space, or None if
    it has fewer than two points."""
    cyclic = spline.use_cyclic_u
    if spline.type == 'BEZIER':
        count = len(spline.bezier_points)
        if count < 2:
            return None
        arrays = {}
        for name in ("co", "handle_left", "handle_right"):
            flat = np.empty(count * 3)
            spline.bezier_points.foreach_get(name, flat)
            arrays[name] = np.hstack([flat.reshape(-1, 3), np.ones((count, 1))])
        pairs = [(i, i + 1) for i in range(count - 1)] + ([(count - 1, 0)] if cyclic else [])
        segments = np.array([[arrays["co"][i], arrays["handle_right"][i], arrays["handle_left"][j], arrays["co"][j]]
                             for i, j in pairs])
        return Chain(segments, cyclic)
    count = len(spline.points)
    if count < 2:
        return None
    co = np.empty(count * 4)
    spline.points.foreach_get("co", co)
    co = co.reshape(-1, 4)
    order = spline.order_u
    if spline.type == 'POLY' or not _valid_nurbs(count, order, cyclic, spline.use_endpoint_u, spline.use_bezier_u):
        # Blender draws a NURBS spline whose knots do not fit as its control polygon.
        co[:, 3] = 1.0
        ends = np.concatenate([co[1:], co[:1]]) if cyclic else co[1:]
        return Chain(np.stack([co[:len(ends)], ends], axis=1), cyclic)
    homogeneous = np.hstack([co[:, :3] * co[:, 3:], co[:, 3:]])
    knots = blender_knots(count, order, cyclic, spline.use_endpoint_u, spline.use_bezier_u)
    segments = nurbs_segments(homogeneous, order, knots, cyclic)
    return Chain(segments, cyclic) if len(segments) else None


def _joined_points(chain):
    """Control points of consecutive segments with the shared ends once,
    each segment scaled so its first weight matches the previous one's last
    (a rational segment is unchanged by scaling all its points)."""
    points = [chain.segments[0] / chain.segments[0][0][3]]
    for segment in chain.segments[1:]:
        points.append(segment[1:] * (points[-1][-1][3] / segment[0][3]))
    return np.concatenate(points)


def to_spline(data, chain, like=None, kind=None):
    """A new spline in curve ``data`` that is exactly ``chain`` (local space):
    POLY for degree 1, BEZIER for cubic non-rational chains of ``kind``
    'BEZIER' (default: the type of ``like``), NURBS in Bezier and Endpoint
    knot mode otherwise. Spline settings (material, smooth, resolution) come
    from the spline ``like``."""
    kind = kind or (like.type if like is not None else 'NURBS')
    degree = chain.degree
    points = _joined_points(chain)
    weights = points[:, 3]
    if degree == 1:
        if chain.closed:
            points = points[:-1]  # The last point is the first again.
        spline = data.splines.new('POLY')
        spline.points.add(len(points) - 1)
        co = np.hstack([_cartesian(points), np.ones((len(points), 1))])
        spline.points.foreach_set("co", co.ravel())
        spline.use_cyclic_u = chain.closed
    elif kind == 'BEZIER' and degree == 3 and np.allclose(weights, weights[0]):
        corners = _cartesian(points)
        joints = corners[::3]
        right = list(corners[1::3])
        left = list(corners[2::3])
        if chain.closed:
            joints = joints[:-1]
            left = left[-1:] + left[:-1]  # The first point's left handle ends the last segment.
        else:
            right.append(2 * joints[-1] - left[-1])  # The free end handles mirror the other ones.
            left.insert(0, 2 * joints[0] - right[0])
        spline = data.splines.new('BEZIER')
        spline.bezier_points.add(len(joints) - 1)
        for point, co, handle_left, handle_right in zip(spline.bezier_points, joints, left, right):
            point.handle_left_type = point.handle_right_type = 'FREE'
            point.co, point.handle_left, point.handle_right = co, handle_left, handle_right
        spline.use_cyclic_u = chain.closed
    else:
        if chain.closed:
            raise ValueError("closed chains of higher degree are not written")
        spline = data.splines.new('NURBS')
        spline.points.add(len(points) - 1)
        co = np.hstack([_cartesian(points), weights[:, None]])
        spline.points.foreach_set("co", co.ravel())
        spline.order_u = degree + 1
        spline.use_bezier_u = True
        spline.use_endpoint_u = True
    if like is not None:
        spline.material_index = like.material_index
        spline.use_smooth = like.use_smooth
        resolution = like.resolution_u
        if like.type == 'NURBS' and degree > 1 and not like.use_bezier_u:
            # Blender evaluates `resolution` points per control point span:
            # keep about as many points per segment as before. (A spline
            # already written here has those spans: its resolution stays, or
            # every edit would halve it.)
            resolution = max(2, math.ceil(resolution / degree))
        spline.resolution_u = resolution
    return spline


# --- Degree ----------------------------------------------------------------------

def elevate_degree(segments, degree):
    """Segments (k x (p + 1) x 4) raised to ``degree``: the same curves with
    more control points (exact)."""
    segments = np.asarray(segments, dtype=np.float64)
    while segments.shape[1] - 1 < degree:
        p = segments.shape[1] - 1
        raised = np.empty((len(segments), p + 2, 4))
        raised[:, 0], raised[:, -1] = segments[:, 0], segments[:, -1]
        for i in range(1, p + 1):
            a = i / (p + 1)
            raised[:, i] = a * segments[:, i - 1] + (1.0 - a) * segments[:, i]
        segments = raised
    return segments


def reduce_degree(segments, degree):
    """The inverse of ``elevate_degree``, for segments that are really of ``degree``
    (exact for them; check with ``reducible``)."""
    segments = np.asarray(segments, dtype=np.float64)
    while segments.shape[1] - 1 > degree:
        q = segments.shape[1] - 1
        p = q - 1
        forward = np.empty((len(segments), p + 1, 4))
        backward = np.empty_like(forward)
        forward[:, 0], backward[:, p] = segments[:, 0], segments[:, q]
        for i in range(1, p + 1):
            a = i / q
            forward[:, i] = (segments[:, i] - a * forward[:, i - 1]) / (1.0 - a)
        for i in range(p, 0, -1):
            a = i / q
            backward[:, i - 1] = (segments[:, i] - (1.0 - a) * backward[:, i]) / a
        # Each direction is most accurate near the end it starts from.
        half = (p + 1) // 2
        segments = np.concatenate([forward[:, :half], backward[:, half:]], axis=1)
    return segments


def reducible(segments, degree, tol):
    """Whether segments are really curves of ``degree`` (within tol)."""
    if segments.shape[1] - 1 <= degree:
        return True
    again = elevate_degree(reduce_degree(segments, degree), segments.shape[1] - 1)
    return float(np.abs(_cartesian(again) - _cartesian(segments)).max()) <= tol


def reversed_chain(chain):
    """The same curve run the other way: t becomes count - t."""
    return Chain(chain.segments[::-1, ::-1].copy(), chain.closed)


# --- Evaluating and cutting -----------------------------------------------------

def _bernstein(degree, u):
    u = np.atleast_1d(np.asarray(u, dtype=np.float64))[:, None]
    i = np.arange(degree + 1)
    binomial = np.array([math.comb(degree, j) for j in i], dtype=np.float64)
    return binomial * u ** i * (1.0 - u) ** (degree - i)


def segment_points(segment, u):
    """Points (k x 3) of a segment at parameters u (0 to 1)."""
    homogeneous = _bernstein(len(segment) - 1, u) @ segment
    return homogeneous[:, :3] / homogeneous[:, 3:]


def segment_derivatives(segment, u):
    """Points and first derivatives (k x 3 each) of a segment at u."""
    degree = len(segment) - 1
    homogeneous = _bernstein(degree, u) @ segment
    points = homogeneous[:, :3] / homogeneous[:, 3:]
    derivative = degree * (_bernstein(degree - 1, u) @ (segment[1:] - segment[:-1]))
    return points, (derivative[:, :3] - points * derivative[:, 3:]) / homogeneous[:, 3:]


def locate(chain, t):
    """(segment index, u) of parameter t."""
    if chain.closed:
        t = t % chain.count
    index = min(max(int(math.floor(t)), 0), chain.count - 1)
    return index, t - index


def evaluate(chain, t):
    index, u = locate(chain, t)
    return segment_points(chain.segments[index], [u])[0]


def tangent(chain, t):
    index, u = locate(chain, t)
    return segment_derivatives(chain.segments[index], [u])[1][0]


def sample(chain, per_segment=16):
    """Points along the chain (N x 3) and their parameters; one segment per
    straight segment."""
    steps = 1 if chain.degree == 1 else per_segment
    u = np.linspace(0.0, 1.0, steps + 1)[:-1]
    points = [segment_points(segment, u) for segment in chain.segments]
    params = [i + u for i in range(chain.count)]
    points.append(_cartesian(chain.segments[-1])[-1:])
    params.append(np.array([float(chain.count)]))
    return np.concatenate(points), np.concatenate(params)


def split_segment(segment, u):
    """de Casteljau: the parts of a segment before and after u."""
    points = segment
    left, right = [points[0]], [points[-1]]
    for _ in range(len(segment) - 1):
        points = (1.0 - u) * points[:-1] + u * points[1:]
        left.append(points[0])
        right.append(points[-1])
    return np.array(left), np.array(right[::-1])


def sub_segment(segment, u0, u1):
    if u1 < 1.0:
        segment = split_segment(segment, u1)[0]
        u0 = u0 / u1
    if u0 > 0.0:
        segment = split_segment(segment, u0)[1]
    return segment


def extrapolate(segment, s):
    """The segment's own polynomial carried on to u = s: for s > 1 the
    segment over [0, s], for s < 0 over [s, 1] (exact, de Casteljau)."""
    if s > 1.0:
        return split_segment(segment, s)[0]
    if s < 0.0:
        return split_segment(segment, s)[1]
    return segment


_GAUSS = np.polynomial.legendre.leggauss(16)


def segment_length(segment, u0, u1, pieces=8):
    """Arc length of a segment from u0 to u1 (Gauss-Legendre)."""
    nodes, weights = _GAUSS
    bounds = np.linspace(u0, u1, pieces + 1)
    half = (bounds[1:] - bounds[:-1]) / 2
    u = (half[:, None] * nodes + (bounds[1:] + bounds[:-1])[:, None] / 2).ravel()
    speed = np.linalg.norm(segment_derivatives(segment, u)[1], axis=1).reshape(pieces, -1)
    return float((half[:, None] * speed * weights).sum())


def length(chain, t0=0.0, t1=None):
    """Arc length of an open chain from t0 to t1 (default: the end)."""
    t1 = float(chain.count) if t1 is None else t1
    total = 0.0
    for i in range(int(math.floor(t0)), int(math.ceil(t1))):
        u0, u1 = max(t0 - i, 0.0), min(t1 - i, 1.0)
        if u1 > u0 and 0 <= i < chain.count:
            total += segment_length(chain.segments[i], u0, u1)
    return total


def parameter_at_length(chain, distance):
    """The parameter of an open chain at arc length ``distance`` from its
    start (bisection); None if the chain is shorter."""
    total = length(chain)
    if distance >= total:
        # Its whole length, also when rounding made it a little more
        # (total * n / n can be one step above total).
        return float(chain.count) if distance <= total * (1 + 1e-9) else None
    low, high = 0.0, float(chain.count)
    for _ in range(60):
        middle = (low + high) / 2
        if length(chain, 0.0, middle) < distance:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def piece(chain, t0, t1):
    """The open chain from t0 to t1 (t0 < t1; on a closed chain t1 may pass
    the end, up to t0 + count)."""
    segments = []
    i = int(math.floor(t0))
    while i < t1 - PARAM_EPS:
        u0, u1 = max(t0 - i, 0.0), min(t1 - i, 1.0)
        if u1 - u0 > PARAM_EPS:
            segments.append(sub_segment(chain.segments[i % chain.count], u0, u1))
        i += 1
    return Chain(np.array(segments), closed=False)


def is_closed(chain, tol):
    """Cyclic, or ending where it starts."""
    return chain.closed or float(np.linalg.norm(chain.end() - chain.start())) <= tol


def closest_parameter(chain, point):
    """The parameter of the point of the chain closest to ``point``."""
    point = np.asarray(point, dtype=np.float64)
    if chain.degree == 1:
        return _closest_on_lines(chain, point)
    u = np.linspace(0.0, 1.0, 33)
    distance = np.array([np.linalg.norm(segment_points(segment, u) - point, axis=1) for segment in chain.segments])
    best_t, best = math.inf, math.inf
    # Refine from the few closest samples: wiggly curves have local minima.
    for k in np.argsort(distance.ravel())[:6]:
        index, j = divmod(int(k), len(u))
        t, gap = _refine_closest(chain.segments[index], point, float(u[j]))
        if gap < best:
            best_t, best = index + t, gap
    return best_t


def _closest_on_lines(chain, point):
    """closest_parameter for degree 1 chains: exact."""
    a, b = _cartesian(chain.segments[:, 0]), _cartesian(chain.segments[:, 1])
    ab = b - a
    length2 = np.maximum((ab * ab).sum(axis=1), 1e-300)
    s = np.clip(((point - a) * ab).sum(axis=1) / length2, 0.0, 1.0)
    index = int(np.argmin(np.linalg.norm(a + s[:, None] * ab - point, axis=1)))
    # A rational line runs along its chord at an uneven speed.
    w0, w1 = chain.segments[index, 0, 3], chain.segments[index, 1, 3]
    fraction = float(s[index])
    denominator = fraction * w0 + (1.0 - fraction) * w1
    return index + (fraction * w0 / denominator if denominator > 0 else fraction)


def _refine_closest(segment, point, u):
    """(u, distance) of the point of a segment closest to ``point``, by
    Newton's method from u, with the curve's curvature in the step: without
    it the step is too long from farther away than the radius of curvature
    (from three radii off a circle it lands farther from the answer than it
    started) and too short from inside. A step that does not bring the
    curve closer is halved."""
    def gap(at):
        offset = segment_points(segment, [at])[0] - point
        return float(offset @ offset)

    best = gap(u)
    h = 1e-5
    for _ in range(60):
        at, derivative = segment_derivatives(segment, [u])
        speed = float(derivative[0] @ derivative[0])
        if speed < 1e-300:
            break
        offset = point - at[0]
        slope = float(offset @ derivative[0])
        second = (segment_derivatives(segment, [u + h])[1][0] - segment_derivatives(segment, [u - h])[1][0]) / (2 * h)
        bend = speed - float(offset @ second)
        # Where the distance has no minimum nearby (bend <= 0), just downhill.
        step = slope / bend if bend > 1e-9 * speed else slope / speed
        for _ in range(40):
            new = min(max(u + step, 0.0), 1.0)
            if abs(new - u) < 1e-15:
                return u, math.sqrt(best)
            closer = gap(new)
            if closer < best:
                break
            step /= 2
        else:
            break
        u, best = new, closer
    return u, math.sqrt(best)


# --- Intersections ----------------------------------------------------------------

def _distance_to_segment(points, a, b):
    """Distances of points to the line segment ab."""
    ab = b - a
    length2 = float(ab @ ab)
    if length2 < 1e-300:
        return np.linalg.norm(points - a, axis=1)
    t = np.clip((points - a) @ ab / length2, 0.0, 1.0)
    return np.linalg.norm(points - (a + t[:, None] * ab), axis=1)


def _flatness(points):
    """How far a segment's control points are from its chord."""
    if len(points) <= 2:
        return 0.0
    return float(_distance_to_segment(points[1:-1], points[0], points[-1]).max())


def _closest_on_segments(p0, p1, q0, q1):
    """(s, t, distance, parallel) of the closest points of segments p0p1 and
    q0q1 (Ericson, Real-Time Collision Detection 5.1.9)."""
    d1, d2, r = p1 - p0, q1 - q0, p0 - q0
    a, e, f = float(d1 @ d1), float(d2 @ d2), float(d2 @ r)
    parallel = False
    if a < 1e-300 and e < 1e-300:
        s = t = 0.0
    elif a < 1e-300:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = float(d1 @ r)
        if e < 1e-300:
            s, t = min(max(-c / a, 0.0), 1.0), 0.0
        else:
            b = float(d1 @ d2)
            denominator = a * e - b * b
            parallel = denominator <= 1e-12 * a * e
            s = 0.0 if parallel else min(max((b * f - c * e) / denominator, 0.0), 1.0)
            t = (b * s + f) / e
            if t < 0.0:
                s, t = min(max(-c / a, 0.0), 1.0), 0.0
            elif t > 1.0:
                s, t = min(max((b - c) / a, 0.0), 1.0), 1.0
    distance = float(np.linalg.norm((p0 + d1 * s) - (q0 + d2 * t)))
    return s, t, distance, parallel


def _refine(a, b, u, v):
    """Newton's method for A(u) = B(v), from (u, v); returns (u, v, gap)."""
    for _ in range(40):
        pa, da = segment_derivatives(a, [u])
        pb, db = segment_derivatives(b, [v])
        jacobian = np.stack([da[0], -db[0]], axis=1)
        step = np.linalg.lstsq(jacobian, pb[0] - pa[0], rcond=None)[0]
        new_u, new_v = min(max(u + step[0], 0.0), 1.0), min(max(v + step[1], 0.0), 1.0)
        if abs(new_u - u) < 1e-15 and abs(new_v - v) < 1e-15:
            break
        u, v = new_u, new_v
    gap = float(np.linalg.norm(segment_points(a, [u])[0] - segment_points(b, [v])[0]))
    return u, v, gap


def _along_sides(a, t, b, tol):
    """For chain a before and after t: (whether it lies on chain b there,
    how far from t that was looked at; None past the end of an open chain).
    Looked at 2% of a segment from t, and farther on a chain of short
    segments: closer than a hundred tolerances a crossing has not left the
    other curve yet either."""
    here = evaluate(a, t)
    sides = []
    for direction in (-1.0, 1.0):
        step = 0.02
        while True:
            side = t + direction * step
            if not a.closed and not 0.0 <= side <= a.count:
                sides.append((False, None))
                break
            point = evaluate(a, side)
            reach = float(np.linalg.norm(point - here))
            if reach >= 100.0 * tol or step >= a.count / 2:
                along = np.linalg.norm(evaluate(b, closest_parameter(b, point)) - point) <= tol
                sides.append((bool(along), reach))
                break
            step *= 2
    return sides


def _overlap_end(a, t, b, tol, sides):
    """Where the curved overlap of chains a and b that t is in ends: an end
    of chain b lying on a, no farther from t than was looked at (else t)."""
    reaches = [reach for _, reach in sides if reach is not None]
    if b.closed or not reaches:
        return t
    here = evaluate(a, t)
    best, nearest = t, max(reaches)
    for end in (0.0, float(b.count)):
        point = evaluate(b, end)
        s = closest_parameter(a, point)
        gap = float(np.linalg.norm(point - here))
        if gap <= nearest and np.linalg.norm(evaluate(a, s) - point) <= tol:
            best, nearest = s, gap
    return best


def segment_intersections(a, b, tol):
    """Where segments a and b meet within ``tol``: points [(u, v, curved)]
    (curved: not both segments straight) and overlaps of straight segments
    [((u0, u1), (v0, v1))]."""
    points, overlaps = [], []
    ca, cb = _cartesian(a), _cartesian(b)
    # Each segment flat for its own size: a small arc is still an arc to a
    # long line that crosses it twice.
    limit_a = max(tol, FLAT_FRACTION * np.ptp(ca, axis=0).max())
    limit_b = max(tol, FLAT_FRACTION * np.ptp(cb, axis=0).max())
    straight_a, straight_b = _flatness(ca) <= tol * 1e-3, _flatness(cb) <= tol * 1e-3
    stack = [(a, 0.0, 1.0, b, 0.0, 1.0, 0)]
    while stack:
        sa, a0, a1, sb, b0, b1, depth = stack.pop()
        pa, pb = _cartesian(sa), _cartesian(sb)
        if np.any(pa.min(axis=0) > pb.max(axis=0) + tol) or np.any(pb.min(axis=0) > pa.max(axis=0) + tol):
            continue
        fa, fb = _flatness(pa), _flatness(pb)
        if (fa > limit_a or fb > limit_b) and depth < MAX_DEPTH:
            if fa / limit_a >= fb / limit_b:
                left, right = split_segment(sa, 0.5)
                middle = (a0 + a1) / 2
                stack += [(left, a0, middle, sb, b0, b1, depth + 1), (right, middle, a1, sb, b0, b1, depth + 1)]
            else:
                left, right = split_segment(sb, 0.5)
                middle = (b0 + b1) / 2
                stack += [(sa, a0, a1, left, b0, middle, depth + 1), (sa, a0, a1, right, middle, b1, depth + 1)]
            continue
        s, t, distance, parallel = _closest_on_segments(pa[0], pa[-1], pb[0], pb[-1])
        if distance > tol + fa + fb:
            continue
        if parallel and straight_a and straight_b:
            overlap = _line_overlap(ca[0], ca[-1], cb[0], cb[-1], tol)
            if overlap is not None:
                overlaps.append(overlap)
                continue
        u, v, gap = _refine(a, b, a0 + s * (a1 - a0), b0 + t * (b1 - b0))
        if gap > tol:
            continue
        points.append((u, v, not (straight_a and straight_b)))
    return points, overlaps


def _line_overlap(p0, p1, q0, q1, tol):
    """((u0, u1), (v0, v1)) where segments p0p1 and q0q1 lie along each
    other for more than a point, or None."""
    d = p1 - p0
    length2 = float(d @ d)
    if length2 < 1e-300:
        return None
    sq = sorted(float((q - p0) @ d) / length2 for q in (q0, q1))
    for q in (q0, q1):
        foot = p0 + d * (float((q - p0) @ d) / length2)
        if np.linalg.norm(q - foot) > tol:
            return None
    u0, u1 = max(sq[0], 0.0), min(sq[1], 1.0)
    if (u1 - u0) * math.sqrt(length2) <= tol:
        return None  # They only touch at a point: found as a point.
    e = q1 - q0
    e2 = float(e @ e)
    v0, v1 = (float((p0 + d * u - q0) @ e) / e2 for u in (u0, u1))
    return (u0, u1), (v0, v1)


def _boxes(chain):
    points = _cartesian(chain.segments)
    return points.min(axis=1), points.max(axis=1)


def chain_intersections(a, b, tol):
    """Parameters on chain a where chain b meets it, and the parameter
    ranges where straight parts of the two lie along each other."""
    low_a, high_a = _boxes(a)
    low_b, high_b = _boxes(b)
    touching = (np.all(low_a[:, None] <= high_b[None] + tol, axis=2)
                & np.all(low_b[None] <= high_a[:, None] + tol, axis=2))
    params, curved, overlaps = [], [], []
    for i, j in np.argwhere(touching):
        points, spans = segment_intersections(a.segments[i], b.segments[j], tol)
        params += [i + u for u, _, bent in points if not bent]
        curved += [i + u for u, _, bent in points if bent]
        overlaps += [(i + u0, i + u1) for (u0, u1), _ in spans]
    # Points where curved parts meet, without those inside a curved overlap
    # (Newton's method finds arbitrary points there; one found just before
    # the overlap's end is moved to the end).
    kept = []
    for t in sorted(curved):
        if kept and (t - kept[-1] <= PARAM_EPS
                     or np.linalg.norm(evaluate(a, t) - evaluate(a, kept[-1])) <= tol):
            continue
        kept.append(t)
    ends = []
    for t in kept:
        sides = _along_sides(a, t, b, tol)
        if sides[0][0] and sides[1][0]:
            continue
        if sides[0][0] or sides[1][0]:
            t = _overlap_end(a, t, b, tol, sides)
            if any(abs(t - other) <= PARAM_EPS for other in ends):
                continue
            ends.append(t)
        params.append(t)
    return params, overlaps


def cut_parameters(chain, params, overlaps, tol):
    """Sorted distinct parameters where a chain is cut: the intersections
    and the ends of overlaps, without the chain's ends (unless closed)."""
    found = list(params) + [t for span in overlaps for t in span]
    closed = is_closed(chain, tol)
    if closed:
        found = [t % chain.count for t in found]
    cuts = []
    for t in sorted(found):
        if not closed and (t <= PARAM_EPS or t >= chain.count - PARAM_EPS
                           or np.linalg.norm(evaluate(chain, t) - chain.start()) <= tol
                           or np.linalg.norm(evaluate(chain, t) - chain.end()) <= tol):
            continue  # At an end: nothing to cut off.
        if cuts and (t - cuts[-1] <= PARAM_EPS or np.linalg.norm(evaluate(chain, t) - evaluate(chain, cuts[-1])) <= tol):
            continue
        cuts.append(t)
    if closed and len(cuts) >= 2 and np.linalg.norm(evaluate(chain, cuts[-1]) - evaluate(chain, cuts[0])) <= tol:
        cuts.pop()  # The same point across the seam.
    return cuts


def removed_range(chain, cuts, pick, tol):
    """The parameter range (a, b) around ``pick`` between two cuts (or a cut
    and an end) that Trim removes; on a closed chain a may be below 0 or b
    past the end. None if nothing can be removed (no cuts, or fewer than
    two on a closed chain)."""
    n = chain.count
    if is_closed(chain, tol):
        if len(cuts) < 2:
            return None
        pick = pick % n
        after = [c for c in cuts if c > pick]
        before = [c for c in cuts if c < pick]
        return (before[-1] if before else cuts[-1] - n), (after[0] if after else cuts[0] + n)
    if not cuts:
        return None
    return (max((c for c in cuts if c < pick), default=0.0),
            min((c for c in cuts if c > pick), default=float(n)))


def trim_ranges(chain, cuts, pick, tol):
    """Parameter ranges kept when ``removed_range`` is removed, or None."""
    removed = removed_range(chain, cuts, pick, tol)
    if removed is None:
        return None
    a, b = removed
    n = chain.count
    if is_closed(chain, tol):
        start = b if b < n else b - n
        return [(start, start + n - (b - a))]
    return [(t0, t1) for t0, t1 in ((0.0, a), (b, float(n))) if t1 - t0 > PARAM_EPS]


def split_ranges(chain, cuts, tol):
    """Parameter ranges of the pieces between cuts; None if there are none
    (a closed chain cut once opens at the cut)."""
    if not cuts:
        return None
    n = chain.count
    if is_closed(chain, tol):
        return [(c, d) for c, d in zip(cuts, cuts[1:] + [cuts[0] + n])]
    bounds = [0.0] + list(cuts) + [float(n)]
    return list(zip(bounds[:-1], bounds[1:]))
