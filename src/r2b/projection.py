# SPDX-License-Identifier: GPL-3.0-or-later
"""Projecting many world-space points into a viewport at once (numpy)."""

import numpy as np


def project(points, region, rv3d):
    """Region coordinates (N x 2) of world-space points (N x 3 or N x 4);
    NaN for points behind the view."""
    points = np.asarray(points, dtype=np.float64)
    if points.shape[1] == 3:
        points = np.hstack([points, np.ones((len(points), 1))])
    clip = points @ np.array(rv3d.perspective_matrix).T
    w = clip[:, 3]
    with np.errstate(divide='ignore', invalid='ignore'):
        x = (clip[:, 0] / w + 1.0) * 0.5 * region.width
        y = (clip[:, 1] / w + 1.0) * 0.5 * region.height
    xy = np.stack([x, y], axis=1)
    xy[w <= 1e-6] = np.nan
    return xy


def to_world(local, matrix_world):
    """World coordinates (N x 3) of local coordinates (N x 3)."""
    matrix = np.array(matrix_world)
    return local @ matrix[:3, :3].T + matrix[:3, 3]
