# SPDX-License-Identifier: GPL-3.0-or-later
"""NURBS math matching Blender's NURBS splines, for previews and curve fitting.

Blender's NURBS splines have uniform knots; R2B uses two of its knot modes:

* Endpoint (``use_endpoint_u``): clamped uniform knots, so the curve starts
  and ends at the first and last control points, as Rhino's curves do.
* Bezier and Endpoint (``use_bezier_u``) with order 3: every inner knot is
  doubled, so each pair of segments is a rational quadratic Bezier; with the
  right weights these are exact circular arcs, as in Rhino.
"""

import math

import numpy as np
from mathutils import Vector


def endpoint_knots(count, order):
    """Blender's clamped uniform knot vector for ``count`` control points."""
    inner = count - order
    return np.array([0.0] * order + [float(i) for i in range(1, inner + 1)] + [float(inner + 1)] * order)


def basis(knots, order, t):
    """Values of all B-spline basis functions of ``order`` at parameter t."""
    knots = np.asarray(knots, dtype=np.float64)
    last = knots[-1]
    values = np.array([
        1.0 if knots[i] <= t < knots[i + 1] or (t == last and knots[i] < knots[i + 1] == last) else 0.0
        for i in range(len(knots) - 1)
    ])
    for degree in range(1, order):
        following = np.zeros(len(values) - 1)
        for i in range(len(following)):
            left = knots[i + degree] - knots[i]
            right = knots[i + degree + 1] - knots[i + 1]
            if left > 0:
                following[i] += (t - knots[i]) / left * values[i]
            if right > 0:
                following[i] += (knots[i + degree + 1] - t) / right * values[i + 1]
        values = following
    return values


def evaluate(points, order, weights=None, samples=64):
    """Points along a clamped uniform NURBS curve (Blender's Endpoint mode)."""
    points = np.asarray([tuple(p) for p in points], dtype=np.float64)
    order = min(order, len(points))
    weights = np.ones(len(points)) if weights is None else np.asarray(weights, dtype=np.float64)
    knots = endpoint_knots(len(points), order)
    result = []
    for t in np.linspace(knots[order - 1], knots[len(points)], samples):
        values = basis(knots, order, t) * weights
        result.append(values @ points / values.sum())
    return [Vector(p) for p in result]


def interpolate(data, order=4):
    """Control points of a clamped uniform B-spline (Blender's Endpoint mode)
    through ``data``: it passes through each point at the Greville parameter
    of the matching control point, which always has a solution."""
    data = np.asarray([tuple(p) for p in data], dtype=np.float64)
    count = len(data)
    order = min(order, count)
    if order <= 2:
        return [Vector(p) for p in data]
    knots = endpoint_knots(count, order)
    greville = [knots[i + 1:i + order].mean() for i in range(count)]
    matrix = np.array([basis(knots, order, t) for t in greville])
    control = np.linalg.solve(matrix, data)
    return [Vector(p) for p in control]


def arc_points(center, x_axis, y_axis, radius, start, sweep):
    """Control points and weights of an exact arc as a rational quadratic
    NURBS (Blender's Bezier and Endpoint modes, order 3): one segment per 90
    degrees or less. Angles in radians, counterclockwise from ``x_axis``."""
    segments = max(1, math.ceil(abs(sweep) / (math.pi / 2) - 1e-9))
    step = sweep / segments
    middle_weight = math.cos(step / 2)
    points, weights = [], []

    def at(angle, distance):
        return center + (x_axis * math.cos(angle) + y_axis * math.sin(angle)) * distance

    for i in range(segments):
        angle = start + i * step
        points.append(at(angle, radius))
        weights.append(1.0)
        points.append(at(angle + step / 2, radius / middle_weight))
        weights.append(middle_weight)
    points.append(at(start + sweep, radius))
    weights.append(1.0)
    return points, weights
