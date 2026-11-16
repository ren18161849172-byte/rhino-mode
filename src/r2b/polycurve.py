# SPDX-License-Identifier: GPL-3.0-or-later
"""Joined curves (Rhino's polycurves) and the arcs R2B tags curve objects with.

A Blender spline has one type and one degree, so a joined curve is one
spline: its parts (the curves that were joined) raised to the highest degree
among them (exact, see curve_geom.elevate_degree). The object keeps what
Explode needs to give the parts back, as JSON in PARTS_PROPERTY: for each
part, its number of Bezier segments in the spline's chain (curve_geom.py),
its own degree, the kind of spline it was ('POLY', 'NURBS' or 'BEZIER') and,
for an arc, the arc. A curve of one part that is an arc or circle keeps the
arc in ARC_PROPERTY instead (Osnap reads both: Cen, Quad, Tan, ...). Joined
lines and polylines are simply a polyline, as in Rhino.

Stored parts are only used while they still fit the spline (not after its
points were edited in Edit Mode, say); otherwise the curve is one part. The
same goes for arcs: they must still lie on their circle.
"""

import json
import math
from dataclasses import dataclass, replace

import numpy as np

from . import curve_geom

# Center, x_axis, y_axis (local space), radius, start and end angle (radians,
# counterclockwise about x_axis × y_axis).
ARC_PROPERTY = "r2b_arc"
PARTS_PROPERTY = "r2b_polycurve"
KINDS = ('POLY', 'NURBS', 'BEZIER')


@dataclass(frozen=True)
class Part:
    count: int  # Bezier segments of the chain
    degree: int  # Its own degree (the chain's may be higher)
    kind: str  # The kind of spline it was
    arc: dict = None  # ARC_PROPERTY format, local space


def _plain_arc(data):
    return {"center": [float(c) for c in data["center"]], "x_axis": [float(c) for c in data["x_axis"]],
            "y_axis": [float(c) for c in data["y_axis"]], "radius": float(data["radius"]),
            "start": float(data.get("start", 0.0)), "end": float(data.get("end", 2 * math.pi))}


# --- Arcs ---------------------------------------------------------------------------

def _frame(arc):
    return (np.array(arc["center"], dtype=np.float64), np.array(arc["x_axis"], dtype=np.float64),
            np.array(arc["y_axis"], dtype=np.float64), float(arc["radius"]))


def arc_fits(arc, chain, t0, t1, tol):
    """Whether the chain from t0 to t1 lies on the arc's circle."""
    center, x_axis, y_axis, radius = _frame(arc)
    normal = np.cross(x_axis, y_axis)
    for t in np.linspace(t0, t1, 9):
        offset = curve_geom.evaluate(chain, t) - center
        if abs(np.linalg.norm(offset) - radius) > tol or abs(offset @ normal) > tol:
            return False
    return True


def sub_arc(arc, chain, t0, t1):
    """The arc of the chain from t0 to t1, which lies on ``arc``'s circle."""
    center, x_axis, y_axis, radius = _frame(arc)

    def angle(t):
        # Past the end: the part goes on across the seam of a closed curve
        # (see cut; a circle is an open spline whose ends meet).
        offset = curve_geom.evaluate(chain, t - chain.count if t > chain.count else t) - center
        return math.atan2(offset @ y_axis, offset @ x_axis)

    a0, a1 = angle(t0), angle(t1)
    counterclockwise = curve_geom.tangent(chain, t0) @ (-math.sin(a0) * x_axis + math.cos(a0) * y_axis) >= 0
    start, sweep = (a0, (a1 - a0) % (2 * math.pi)) if counterclockwise else (a1, (a0 - a1) % (2 * math.pi))
    if sweep < 1e-9:
        sweep = 2 * math.pi  # A whole circle, opened at a point.
    start %= 2 * math.pi
    return {"center": list(center), "x_axis": list(x_axis), "y_axis": list(y_axis), "radius": radius,
            "start": start, "end": start + sweep}


def transform_arc(arc, matrix):
    """The arc moved by an affine 4x4 matrix, or None if the matrix does not
    keep circles circles (non-uniform scale)."""
    if arc is None:
        return None
    m = np.array(matrix, dtype=np.float64)
    center, x_axis, y_axis, radius = _frame(arc)
    x_axis, y_axis = m[:3, :3] @ x_axis, m[:3, :3] @ y_axis
    sx, sy = np.linalg.norm(x_axis), np.linalg.norm(y_axis)
    if abs(sx - sy) > 1e-6 * max(sx, sy) or abs(x_axis @ y_axis) > 1e-6 * sx * sy:
        return None
    return {"center": list(m[:3, :3] @ center + m[:3, 3]), "x_axis": list(x_axis / sx),
            "y_axis": list(y_axis / sy), "radius": radius * sx, "start": arc["start"], "end": arc["end"]}


# --- Parts of a curve object -------------------------------------------------------------

def edges(parts):
    """Chain parameters where the parts start, and the end."""
    return np.cumsum([0] + [part.count for part in parts]).astype(np.float64)


def _parse(stored):
    try:
        items = json.loads(stored)
        parts = [Part(int(item["count"]), int(item["degree"]), str(item["kind"]),
                      _plain_arc(item["arc"]) if item.get("arc") else None) for item in items]
    except (TypeError, ValueError, KeyError):
        return None
    if not parts or any(part.count < 1 or part.degree < 1 or part.kind not in KINDS for part in parts):
        return None
    return parts


def parts_of(obj, index, chain):
    """The parts of spline ``index`` of a curve object, whose chain (local
    space) is ``chain``."""
    spline = obj.data.splines[index]
    whole = Part(chain.count, chain.degree, spline.type if spline.type in KINDS else 'NURBS')
    if len(obj.data.splines) != 1:
        return [whole]
    tol = curve_geom.tolerance(chain)
    stored = obj.get(PARTS_PROPERTY)
    parts = _parse(stored) if isinstance(stored, str) else None
    if parts is not None and sum(part.count for part in parts) == chain.count:
        bounds = edges(parts)
        if all(part.degree <= chain.degree
               and curve_geom.reducible(chain.segments[int(a):int(b)], part.degree, tol)
               for part, a, b in zip(parts, bounds[:-1], bounds[1:])):
            return [part if part.arc is None or arc_fits(part.arc, chain, a, b, tol) else replace(part, arc=None)
                    for part, a, b in zip(parts, bounds[:-1], bounds[1:])]
    arc = obj.get(ARC_PROPERTY)
    if arc:
        arc = _plain_arc(arc)
        if arc_fits(arc, chain, 0.0, float(chain.count), tol):
            return [replace(whole, arc=arc)]
    return [whole]


def cut(chain, parts, t0, t1):
    """(chain, parts) of the open piece from t0 to t1 (as curve_geom.piece);
    parts cut short keep their degree and kind, arcs become shorter arcs."""
    n = chain.count
    bounds = edges(parts)
    spans = [(t0, min(t1, n))] + ([(0.0, t1 - n)] if t1 > n + curve_geom.PARAM_EPS else [])
    chunks = []  # [part index, start, end]
    for wrapped, (r0, r1) in enumerate(spans):
        for k in range(len(parts)):
            a, b = max(r0, bounds[k]), min(r1, bounds[k + 1])
            if b - a <= curve_geom.PARAM_EPS:
                continue
            if wrapped and chunks and chunks[-1][0] == k and chunks[-1][2] >= n - curve_geom.PARAM_EPS:
                chunks[-1][2] = n + b  # The same part across the seam of a closed curve.
            else:
                chunks.append([k, a, b])
    segments, result = [], []
    for k, a, b in chunks:
        part = parts[k]
        piece = curve_geom.piece(chain, a, b)
        if not len(piece.segments):
            continue
        whole = abs(a - bounds[k]) <= curve_geom.PARAM_EPS and abs(b - bounds[k + 1]) <= curve_geom.PARAM_EPS
        arc = part.arc if part.arc is None or whole else sub_arc(part.arc, chain, a, b)
        segments.append(piece.segments)
        result.append(Part(len(piece.segments), part.degree, part.kind, arc))
    return curve_geom.Chain(np.concatenate(segments)), result


def write(data, chain, parts, like=None):
    """A new spline in curve ``data`` for a chain made of ``parts``: one part
    in its own degree and kind, all straight parts as a polyline, otherwise
    the highest degree of the parts (Bezier if they all were, else NURBS)."""
    degree = max(part.degree for part in parts)
    segments = chain.segments
    if segments.shape[1] - 1 > degree:
        segments = curve_geom.reduce_degree(segments, degree)
    weights = segments[..., 3] / segments[:, :1, 3]
    rational = not np.allclose(weights, 1.0)
    if len(parts) == 1:
        kind = parts[0].kind
    elif degree == 1:
        kind = 'POLY'
    elif all(part.kind in ('BEZIER', 'POLY') for part in parts) and degree == 3 and not rational:
        kind = 'BEZIER'
    else:
        kind = 'NURBS'
    # A closed NURBS curve is written open, its ends at the same point (like
    # R2B's circles); Blender's cyclic NURBS have no Bezier knot mode.
    closed = bool(chain.closed) and (degree == 1 or (kind == 'BEZIER' and degree == 3 and not rational))
    return curve_geom.to_spline(data, curve_geom.Chain(segments, closed), like=like, kind=kind)


def tags_of(obj):
    """The object's arc and parts properties, to put back with ``restore``."""
    arc = obj.get(ARC_PROPERTY)
    return {ARC_PROPERTY: arc.to_dict() if hasattr(arc, "to_dict") else arc, PARTS_PROPERTY: obj.get(PARTS_PROPERTY)}


def restore(obj, tags):
    for name, value in tags.items():
        if value is not None:
            obj[name] = value
        elif name in obj:
            del obj[name]


def set_tags(obj, parts):
    """Tag a curve object whose only spline was written from ``parts``."""
    restore(obj, {ARC_PROPERTY: None, PARTS_PROPERTY: None})
    if len(parts) == 1:
        if parts[0].arc is not None:
            obj[ARC_PROPERTY] = parts[0].arc
    elif max(part.degree for part in parts) > 1:
        obj[PARTS_PROPERTY] = json.dumps([{"count": part.count, "degree": part.degree, "kind": part.kind,
                                           "arc": part.arc} for part in parts])


def clear_tags(obj):
    restore(obj, {ARC_PROPERTY: None, PARTS_PROPERTY: None})


def transform_tags(obj, matrix):
    """Move the arcs an object is tagged with by ``matrix`` (local space),
    after its curve data was transformed by it. Arcs that do not stay
    circles (non-uniform scale) are dropped."""
    arc = obj.get(ARC_PROPERTY)
    if arc:
        moved = transform_arc(_plain_arc(arc), matrix)
        restore(obj, {ARC_PROPERTY: moved})
    stored = obj.get(PARTS_PROPERTY)
    parts = _parse(stored) if isinstance(stored, str) else None
    if parts:
        parts = [replace(part, arc=transform_arc(part.arc, matrix)) for part in parts]
        obj[PARTS_PROPERTY] = json.dumps([{"count": part.count, "degree": part.degree, "kind": part.kind,
                                           "arc": part.arc} for part in parts])


# --- For Osnap ----------------------------------------------------------------------------

def arcs_of(obj):
    """The arcs (local space) a curve object is tagged with."""
    arc = obj.get(ARC_PROPERTY)
    if arc:
        return [_plain_arc(arc)]
    stored = obj.get(PARTS_PROPERTY)
    parts = _parse(stored) if isinstance(stored, str) else None
    return [part.arc for part in parts if part.arc is not None] if parts else []


def joints_of(obj):
    """Local points where the parts of a joined curve meet (N x 3)."""
    stored = obj.get(PARTS_PROPERTY)
    parts = _parse(stored) if isinstance(stored, str) else None
    if not parts or len(obj.data.splines) != 1:
        return np.empty((0, 3))
    chain = curve_geom.from_spline(obj.data.splines[0])
    if chain is None or sum(part.count for part in parts) != chain.count:
        return np.empty((0, 3))
    return np.array([curve_geom.evaluate(chain, t) for t in edges(parts)[1:-1]]).reshape(-1, 3)
