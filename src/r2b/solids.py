# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style solid and surface commands (MODE feature): Box, Sphere,
Cylinder, Cone, Plane and Torus, made by picking points.

* Box: two corners of the base on the CPlane, then the height (picked, typed,
  or Enter for the width).
* Sphere: center and radius. Cylinder and Cone: base center, radius, then
  the height (Cone: the apex). Plane: two corners. Torus: center, radius,
  second radius.

Heights are picked as in Rhino: the point moves along the CPlane normal
through the base (an "elevator"), so the height can be picked in another
viewport; an object snap gives the height of the snapped point, and Grid
Snap rounds it. Blender has no NURBS surfaces, so the results are meshes
(smooth shaded where curved), aligned with the CPlane, with the origin at
their center, in the current layer and not selected.
"""

import math

import bmesh
import bpy
from bpy_extras.view3d_utils import region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import Matrix, Vector, geometry

from . import command_line, settings
from .getpoint import CANCEL, FINISH, GetPoints

TOLERANCE = 1e-6
SEGMENTS = 32  # Around circles.
SPHERE_RINGS = 16
TORUS_SEGMENTS = (48, 16)


def plane_matrix(plane, center):
    """Object matrix: at ``center``, axes along the CPlane's."""
    rotation = Matrix((plane.x_axis, plane.y_axis, plane.normal)).transposed()
    return Matrix.Translation(center) @ rotation.to_4x4()


def new_mesh_object(context, name, mesh, matrix, smooth_curved=None):
    """A mesh object from a bmesh (local coordinates), smooth shading on the
    faces for which ``smooth_curved(face)`` is true. Everything in it is
    selected for Edit Mode, as in a mesh from Blender's Add menu (Tab and
    then SubDivide acts on all of it)."""
    if smooth_curved is not None:
        for face in mesh.faces:
            face.smooth = smooth_curved(face)
    for elements in (mesh.verts, mesh.edges, mesh.faces):
        for element in elements:
            element.select = True
    data = bpy.data.meshes.new(name)
    mesh.to_mesh(data)
    mesh.free()
    obj = bpy.data.objects.new(name, data)
    obj.matrix_world = matrix
    context.collection.objects.link(obj)
    return obj


def elevator(context, plane, base, region, rv3d, mouse, point, snapped):
    """The point above ``base`` along the CPlane normal that the mouse (or
    an object snap) gives, and its signed height."""
    normal = plane.normal
    if snapped:
        height = (point - base).dot(normal)
    else:
        origin = region_2d_to_origin_3d(region, rv3d, mouse)
        direction = region_2d_to_vector_3d(region, rv3d, mouse)
        if abs(direction.normalized().dot(normal)) > 0.999:
            # Looking along the normal: the distance in the plane.
            offset = point - base
            height = (offset - normal * offset.dot(normal)).length
        else:
            found = geometry.intersect_line_line(base, base + normal, origin, origin + direction)
            height = (found[0] - base).dot(normal) if found else 0.0
        aids = settings.get(context)
        if aids.grid_snap and aids.snap_spacing > 0:
            height = round(height / aids.snap_spacing) * aids.snap_spacing
    return base + normal * height, height


def circle(center, x_axis, y_axis, radius, count=64):
    return [center + (x_axis * math.cos(a) + y_axis * math.sin(a)) * radius
            for a in (i * 2 * math.pi / count for i in range(count + 1))]


class _Solid(GetPoints):
    """Shared by the solid commands: the CPlane of the first point, typed
    numbers for sizes, and the elevator for heights."""

    # Which point (index into self.points) is asked as a height, if any.
    height_after = None

    def invoke(self, context, event):
        self.sizes = []  # Typed or picked sizes: radius, height, ...
        return super().invoke(context, event)

    def on_point(self, context):
        if len(self.points) == 1:
            self.solid_plane = self.plane  # The CPlane where the first point was picked.
        return self.next_size(context)

    def next_size(self, context):
        """A point was added: turn it into a size if needed. FINISH when done."""
        return None

    def on_enter(self, context):
        return CANCEL

    def asking_height(self):
        return self.height_after is not None and len(self.points) == self.height_after

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        if not self.asking_height():
            return point
        return elevator(context, self.solid_plane, self.height_base(), region, rv3d, mouse, point, snapped)[0]

    def height_base(self):
        return self.points[-1]

    def height_of(self, point):
        return (point - self.height_base()).dot(self.solid_plane.normal)

    def in_plane(self, point):
        """(x, y) of a point in the first point's CPlane, from the first point."""
        local = self.solid_plane.to_local(point) - self.solid_plane.to_local(self.points[0])
        return local.x, local.y

    def radius_to(self, point):
        return math.hypot(*self.in_plane(point))

    def snap_geometry(self):
        return self.points, []

    def error(self, message):
        command_line.echo(self.window, message, 'ERROR')


class R2B_OT_box(_Solid, bpy.types.Operator):
    """Box from two corners of its base and a height"""

    bl_idname = "r2b.box"
    bl_label = "Box"
    height_after = 2

    def prompt(self):
        return ("First corner of base", "Other corner of base",
                "Height. Press Enter to use width")[min(len(self.points), 2)]

    def _corners(self, other):
        plane = self.solid_plane
        a, b = plane.to_local(self.points[0]), plane.to_local(other)
        return [plane.to_world(x, y, a.z) for x, y in ((a.x, a.y), (b.x, a.y), (b.x, b.y), (a.x, b.y))]

    def height_base(self):
        return self.points[1]

    def next_size(self, context):
        if len(self.points) == 2:
            x, y = self.in_plane(self.points[1])
            if abs(x) < TOLERANCE or abs(y) < TOLERANCE:
                self.points.pop()
                self.error("The base needs a width and a depth")
            return None
        if len(self.points) == 3:
            return self._height(self.height_of(self.points.pop()))
        return None

    def _height(self, height):
        if abs(height) < TOLERANCE:
            self.error("The height must not be zero")
            return None
        self.height = height
        return FINISH

    def on_number(self, context, value):
        if len(self.points) == 2:
            return self._height(value) or True
        return None

    def on_enter(self, context):
        if len(self.points) == 2:
            return self._height(abs(self.in_plane(self.points[1])[0]))
        return CANCEL

    def _box_edges(self, height):
        base = self._corners(self.points[1])
        top = [p + self.solid_plane.normal * height for p in base]
        edges = []
        for ring in (base, top):
            edges += [(ring[i], ring[(i + 1) % 4]) for i in range(4)]
        edges += list(zip(base, top))
        return [tuple(p) for edge in edges for p in edge]

    def preview(self):
        if not self.points or self.current is None:
            return []
        if len(self.points) == 1:
            corners = self._corners(self.current)
            return [(corners + corners[:1], 'curve')]
        return [(self._box_edges(self.height_of(self.current)), 'shape')]

    def create(self, context):
        plane = self.solid_plane
        x, y = self.in_plane(self.points[1])
        start = plane.to_local(self.points[0])
        center = plane.to_world(start.x + x / 2, start.y + y / 2, start.z + self.height / 2)
        mesh = bmesh.new()
        bmesh.ops.create_cube(mesh, size=1.0)
        bmesh.ops.scale(mesh, vec=(abs(x), abs(y), abs(self.height)), verts=mesh.verts)
        new_mesh_object(context, "Box", mesh, plane_matrix(plane, center))


class _Round(_Solid):
    """Sphere, Cylinder, Cone, Torus: a center, then a radius."""

    def radius_prompt(self):
        return "Radius"

    def _radius(self, radius):
        if radius < TOLERANCE:
            self.error("The radius must be more than zero")
            return False
        self.sizes.append(radius)
        return True


class R2B_OT_sphere(_Round, bpy.types.Operator):
    """Sphere from its center and radius"""

    bl_idname = "r2b.sphere"
    bl_label = "Sphere"

    def prompt(self):
        return "Center of sphere" if not self.points else "Radius"

    def next_size(self, context):
        if len(self.points) == 2:
            point = self.points.pop()
            return FINISH if self._radius((point - self.points[0]).length) else None
        return None

    def on_number(self, context, value):
        if len(self.points) == 1:
            return FINISH if self._radius(value) else True
        return None

    def preview(self):
        if not self.points or self.current is None:
            return []
        radius = (self.current - self.points[0]).length
        plane, center = self.solid_plane, self.points[0]
        return [(circle(center, plane.x_axis, plane.y_axis, radius), 'curve'),
                (circle(center, plane.x_axis, plane.normal, radius), 'guide'),
                (circle(center, plane.y_axis, plane.normal, radius), 'guide')]

    def create(self, context):
        mesh = bmesh.new()
        bmesh.ops.create_uvsphere(mesh, u_segments=SEGMENTS, v_segments=SPHERE_RINGS, radius=self.sizes[0])
        new_mesh_object(context, "Sphere", mesh, plane_matrix(self.solid_plane, self.points[0]), lambda face: True)


class _Upright(_Round):
    """Cylinder and Cone: base center, radius, height."""

    height_after = 1
    top_radius_factor = 1.0
    noun = ""

    def prompt(self):
        if not self.points:
            return f"Base of {self.noun}"
        return "Radius" if not self.sizes else f"End of {self.noun}"

    def asking_height(self):
        return len(self.points) == 1 and len(self.sizes) == 1

    def height_base(self):
        return self.points[0]

    def next_size(self, context):
        if len(self.points) == 2 and not self.sizes:
            self._radius(self.radius_to(self.points.pop()))
            return None
        if len(self.points) == 2:
            return self._height(self.height_of(self.points.pop()))
        return None

    def _height(self, height):
        if abs(height) < TOLERANCE:
            self.error("The height must not be zero")
            return None
        self.sizes.append(height)
        return FINISH

    def on_number(self, context, value):
        if len(self.points) != 1:
            return None
        if not self.sizes:
            self._radius(value)
            return True
        return self._height(value) or True

    def preview(self):
        if not self.points or self.current is None:
            return []
        plane, base = self.solid_plane, self.points[0]
        if not self.sizes:
            return [(circle(base, plane.x_axis, plane.y_axis, self.radius_to(self.current)), 'curve')]
        radius, height = self.sizes[0], self.height_of(self.current)
        top = base + plane.normal * height
        lines = [(circle(base, plane.x_axis, plane.y_axis, radius), 'curve')]
        if self.top_radius_factor:
            lines.append((circle(top, plane.x_axis, plane.y_axis, radius * self.top_radius_factor), 'curve'))
        for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
            rim = base + (plane.x_axis * math.cos(angle) + plane.y_axis * math.sin(angle)) * radius
            top_rim = top + (rim - base) * self.top_radius_factor
            lines.append(([rim, top_rim], 'guide'))
        return lines

    def create(self, context):
        radius, height = self.sizes
        plane = self.solid_plane
        mesh = bmesh.new()
        bmesh.ops.create_cone(mesh, cap_ends=True, segments=SEGMENTS, radius1=radius,
                              radius2=radius * self.top_radius_factor, depth=abs(height))
        if height < 0:  # Upside down: mirror, then turn the faces out again.
            bmesh.ops.scale(mesh, vec=(1, 1, -1), verts=mesh.verts)
            bmesh.ops.reverse_faces(mesh, faces=mesh.faces)
        axis = Vector((0, 0, 1))
        new_mesh_object(context, self.bl_label, mesh, plane_matrix(plane, self.points[0] + plane.normal * height / 2),
                        lambda face: abs(face.normal.dot(axis)) < 0.999)


class R2B_OT_cylinder(_Upright, bpy.types.Operator):
    """Cylinder from its base center, radius and end"""

    bl_idname = "r2b.cylinder"
    bl_label = "Cylinder"
    noun = "cylinder"


class R2B_OT_cone(_Upright, bpy.types.Operator):
    """Cone from its base center, radius and apex"""

    bl_idname = "r2b.cone"
    bl_label = "Cone"
    noun = "cone"
    top_radius_factor = 0.0


class R2B_OT_plane(_Solid, bpy.types.Operator):
    """Plane (a flat rectangle) from two corners"""

    bl_idname = "r2b.plane"
    bl_label = "Plane"

    def prompt(self):
        return "First corner of plane" if not self.points else "Other corner"

    def next_size(self, context):
        if len(self.points) == 2:
            x, y = self.in_plane(self.points[1])
            if abs(x) < TOLERANCE or abs(y) < TOLERANCE:
                self.points.pop()
                self.error("The plane needs a width and a height")
                return None
            return FINISH
        return None

    def preview(self):
        if not self.points or self.current is None:
            return []
        plane = self.solid_plane
        a, b = plane.to_local(self.points[0]), plane.to_local(self.current)
        corners = [plane.to_world(x, y, a.z) for x, y in ((a.x, a.y), (b.x, a.y), (b.x, b.y), (a.x, b.y))]
        return [(corners + corners[:1], 'curve')]

    def create(self, context):
        plane = self.solid_plane
        x, y = self.in_plane(self.points[1])
        start = plane.to_local(self.points[0])
        center = plane.to_world(start.x + x / 2, start.y + y / 2, start.z)
        mesh = bmesh.new()
        bmesh.ops.create_grid(mesh, x_segments=1, y_segments=1, size=0.5)
        bmesh.ops.scale(mesh, vec=(abs(x), abs(y), 1), verts=mesh.verts)
        new_mesh_object(context, "Plane", mesh, plane_matrix(plane, center))


class R2B_OT_torus(_Round, bpy.types.Operator):
    """Torus from its center, radius and second radius"""

    bl_idname = "r2b.torus"
    bl_label = "Torus"

    def prompt(self):
        if not self.points:
            return "Center of torus"
        return "Radius" if not self.sizes else "Second radius"

    def _ring_distance(self, point):
        """Distance of a point from the torus's center circle."""
        x, y = self.in_plane(point)
        z = (point - self.points[0]).dot(self.solid_plane.normal)
        return math.hypot(math.hypot(x, y) - self.sizes[0], z)

    def next_size(self, context):
        if len(self.points) == 2 and not self.sizes:
            self._radius(self.radius_to(self.points.pop()))
            return None
        if len(self.points) == 2:
            return FINISH if self._second(self._ring_distance(self.points.pop())) else None
        return None

    def _second(self, radius):
        if radius < TOLERANCE:
            self.error("The second radius must be more than zero")
            return False
        self.sizes.append(radius)
        return True

    def on_number(self, context, value):
        if len(self.points) != 1:
            return None
        if not self.sizes:
            self._radius(value)
            return True
        return FINISH if self._second(value) else True

    def preview(self):
        if not self.points or self.current is None:
            return []
        plane, center = self.solid_plane, self.points[0]
        if not self.sizes:
            return [(circle(center, plane.x_axis, plane.y_axis, self.radius_to(self.current)), 'curve')]
        major, minor = self.sizes[0], self._ring_distance(self.current)
        return [(circle(center, plane.x_axis, plane.y_axis, major + minor), 'curve'),
                (circle(center, plane.x_axis, plane.y_axis, max(major - minor, 0.0)), 'curve'),
                (circle(center, plane.x_axis, plane.y_axis, major), 'guide')]

    def create(self, context):
        major, minor = self.sizes
        around, across = TORUS_SEGMENTS
        mesh = bmesh.new()
        rings = []
        for i in range(around):
            a = i * 2 * math.pi / around
            direction = Vector((math.cos(a), math.sin(a), 0))
            rings.append([mesh.verts.new(direction * (major + minor * math.cos(b)) + Vector((0, 0, minor * math.sin(b))))
                          for b in (j * 2 * math.pi / across for j in range(across))])
        for i in range(around):
            ring, following = rings[i], rings[(i + 1) % around]
            for j in range(across):
                k = (j + 1) % across
                mesh.faces.new((ring[j], following[j], following[k], ring[k]))
        bmesh.ops.recalc_face_normals(mesh, faces=mesh.faces)
        new_mesh_object(context, "Torus", mesh, plane_matrix(self.solid_plane, self.points[0]), lambda face: True)


_classes = (R2B_OT_box, R2B_OT_sphere, R2B_OT_cylinder, R2B_OT_cone, R2B_OT_plane, R2B_OT_torus)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
