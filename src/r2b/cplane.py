# SPDX-License-Identifier: GPL-3.0-or-later
"""Construction planes (CPlanes): where picked and typed points go.

As in Rhino's default viewports, each 3D viewport uses a world plane that
matches its view: Top/Bottom and perspective views the world XY plane,
Front/Back the XZ plane, Right/Left the YZ plane. The CPlane command gives a
viewport its own plane, stored on the screen (layout) by area index so it is
saved with the file. Points under the mouse are where the view ray meets the
viewport's plane; typed coordinates are in that plane.
"""

import math
from dataclasses import dataclass

from bpy_extras.view3d_utils import region_2d_to_location_3d, region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Vector, geometry


@dataclass(frozen=True)
class CPlane:
    name: str
    origin: Vector
    x_axis: Vector
    y_axis: Vector

    @property
    def normal(self):
        return self.x_axis.cross(self.y_axis)

    def to_world(self, x, y, z=0.0):
        return self.origin + self.x_axis * x + self.y_axis * y + self.normal * z

    def to_local(self, point):
        offset = point - self.origin
        return Vector((offset.dot(self.x_axis), offset.dot(self.y_axis), offset.dot(self.normal)))


WORLD_TOP = CPlane("World Top", Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 1, 0)))
WORLD_FRONT = CPlane("World Front", Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 0, 1)))
WORLD_RIGHT = CPlane("World Right", Vector((0, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
# Rhino's six world CPlanes, as the CPlane command's World option offers them.
WORLD_PLANES = {
    "top": WORLD_TOP,
    "bottom": CPlane("World Bottom", Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, -1, 0))),
    "front": WORLD_FRONT,
    "back": CPlane("World Back", Vector((0, 0, 0)), Vector((-1, 0, 0)), Vector((0, 0, 1))),
    "right": WORLD_RIGHT,
    "left": CPlane("World Left", Vector((0, 0, 0)), Vector((0, -1, 0)), Vector((0, 0, 1))),
}


CUSTOM_PROPERTY = "r2b_cplanes"  # On screens: {area index: {"origin", "x_axis", "y_axis"}}


def _area_index(screen, area):
    return next((i for i, a in enumerate(screen.areas) if a.as_pointer() == area.as_pointer()), None)


def _entries(screen):
    """The screen's custom CPlanes as plain dicts (not ID property groups)."""
    return screen[CUSTOM_PROPERTY].to_dict() if CUSTOM_PROPERTY in screen else {}


def custom(screen, area):
    """The viewport's own CPlane (see the CPlane command), or None."""
    index = _area_index(screen, area)
    entry = _entries(screen).get(str(index)) if index is not None else None
    if not entry:
        return None
    return CPlane(entry.get("name", "Custom"), Vector(entry["origin"]), Vector(entry["x_axis"]),
                  Vector(entry["y_axis"]))


def for_area(screen, area):
    """The CPlane of a 3D viewport: its own, or the world plane of its view."""
    return custom(screen, area) or for_view(area.spaces.active.region_3d)


# Blender's grid and axes, hidden while a viewport has its own CPlane (R2B
# draws that plane's grid) and restored with the world plane.
_BLENDER_GRID = ("show_floor", "show_ortho_grid", "show_axis_x", "show_axis_y", "show_axis_z")


def set_custom(screen, area, plane):
    entries = _entries(screen)
    key = str(_area_index(screen, area))
    overlay = area.spaces.active.overlay
    previous = entries.get(key)
    saved = (previous["blender_grid"] if previous
             else {name: getattr(overlay, name) for name in _BLENDER_GRID})
    entries[key] = {"name": plane.name, "origin": tuple(plane.origin), "x_axis": tuple(plane.x_axis),
                    "y_axis": tuple(plane.y_axis), "blender_grid": saved}
    screen[CUSTOM_PROPERTY] = entries
    for name in _BLENDER_GRID:
        setattr(overlay, name, False)


def clear_custom(screen, area):
    entries = _entries(screen)
    entry = entries.pop(str(_area_index(screen, area)), None)
    screen[CUSTOM_PROPERTY] = entries
    if entry:
        for name, value in entry["blender_grid"].items():
            setattr(area.spaces.active.overlay, name, bool(value))


def copy_custom(source_screen, source_area, target_screen, target_area):
    """Give a viewport the CPlane of another one (maximize and restore). The
    viewports' overlays are copied separately, so only the entry moves."""
    entry = _entries(source_screen).get(str(_area_index(source_screen, source_area)))
    entries = _entries(target_screen)
    key = str(_area_index(target_screen, target_area))
    if entry:
        entries[key] = entry
    else:
        entries.pop(key, None)
    target_screen[CUSTOM_PROPERTY] = entries


def for_view(rv3d):
    """The CPlane of a viewport, from the direction it looks in."""
    if rv3d.view_perspective == 'PERSP' or not rv3d.is_orthographic_side_view:
        return WORLD_TOP
    view = rv3d.view_rotation @ Vector((0, 0, -1))
    axis = max(range(3), key=lambda i: abs(view[i]))
    return (WORLD_RIGHT, WORLD_FRONT, WORLD_TOP)[axis]


def point_under_mouse(region, rv3d, plane, mouse):
    """Where the view ray through region coordinates ``mouse`` meets the plane."""
    origin = region_2d_to_origin_3d(region, rv3d, mouse)
    direction = region_2d_to_vector_3d(region, rv3d, mouse)
    hit = geometry.intersect_line_plane(origin, origin + direction, plane.origin, plane.normal)
    if hit is None or (hit - origin).dot(direction) < 0:
        # Looking along the plane, or the plane is behind the view.
        return region_2d_to_location_3d(region, rv3d, mouse, plane.origin)
    return hit


def snap_to_grid(plane, point, spacing):
    local = plane.to_local(point)
    return plane.to_world(round(local.x / spacing) * spacing, round(local.y / spacing) * spacing, local.z)


def ortho(plane, previous, point):
    """Restrict ``point`` to a CPlane axis direction from ``previous``."""
    delta = plane.to_local(point) - plane.to_local(previous)
    if abs(delta.x) >= abs(delta.y):
        delta.y = 0.0
    else:
        delta.x = 0.0
    delta.z = 0.0
    return previous + plane.x_axis * delta.x + plane.y_axis * delta.y


def at_distance(previous, point, distance):
    """The point at ``distance`` from ``previous`` towards ``point``."""
    direction = point - previous
    if direction.length < 1e-12:
        return None
    return previous + direction.normalized() * distance


def at_angle(plane, previous, point, angle):
    """The point at ``angle`` degrees (in the CPlane) from ``previous``, as
    far along that direction as ``point``."""
    radians = math.radians(angle)
    direction = plane.x_axis * math.cos(radians) + plane.y_axis * math.sin(radians)
    return previous + direction * (point - previous).dot(direction)


def polar(plane, base, distance, angle):
    radians = math.radians(angle)
    return base + (plane.x_axis * math.cos(radians) + plane.y_axis * math.sin(radians)) * distance
