# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's Record History, done with Blender's Geometry Nodes: results that
stay linked to their input curve and follow it when it changes.

ExtrudeCrv with Record History on (status bar) makes a mesh object whose
Geometry Nodes modifier reads the curve (Object Info) and extrudes it; its
Start and Sweep inputs can be changed in the modifier panel. Applying the
modifier (ApplyModifiers) breaks the link, as editing a history child does
in Rhino. The object sits in the curve's plane (local XY), where Geometry
Nodes' Fill Curve makes the caps.
"""

import bpy
from mathutils import Matrix, Vector

from . import extrude, settings


def set_input(modifier, name, value):
    """Set a Geometry Nodes modifier input by its name (Blender 4.2 keeps the
    values as ID properties of the modifier, 5.x under ``properties.inputs``)."""
    tree = modifier.node_group
    identifier = next(item.identifier for item in tree.interface.items_tree
                      if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name)
    properties = getattr(modifier, "properties", None)
    inputs = getattr(properties, "inputs", None) if properties is not None else None
    if inputs is not None:
        getattr(inputs, identifier).value = value
    else:
        modifier[identifier] = value


def get_input(modifier, name):
    tree = modifier.node_group
    identifier = next(item.identifier for item in tree.interface.items_tree
                      if item.item_type == 'SOCKET' and item.in_out == 'INPUT' and item.name == name)
    properties = getattr(modifier, "properties", None)
    inputs = getattr(properties, "inputs", None) if properties is not None else None
    if inputs is not None:
        return getattr(inputs, identifier).value
    return modifier[identifier]


def _extrusion_tree(name, curve, solid, flip_bottom, flip_sides):
    tree = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    interface = tree.interface
    interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    interface.new_socket("Start", in_out='INPUT', socket_type='NodeSocketVector')
    interface.new_socket("Sweep", in_out='INPUT', socket_type='NodeSocketVector')
    nodes, links = tree.nodes, tree.links
    group_in, group_out = nodes.new('NodeGroupInput'), nodes.new('NodeGroupOutput')
    info = nodes.new('GeometryNodeObjectInfo')
    info.transform_space = 'RELATIVE'
    info.inputs["Object"].default_value = curve
    start = nodes.new('GeometryNodeTransform')
    links.new(info.outputs["Geometry"], start.inputs["Geometry"])
    links.new(group_in.outputs["Start"], start.inputs["Translation"])
    edges = nodes.new('GeometryNodeCurveToMesh')
    links.new(start.outputs["Geometry"], edges.inputs["Curve"])
    sides = nodes.new('GeometryNodeExtrudeMesh')
    sides.mode = 'EDGES'
    links.new(edges.outputs["Mesh"], sides.inputs["Mesh"])
    links.new(group_in.outputs["Sweep"], sides.inputs["Offset"])
    # Extrude Mesh turns the faces of a counter-clockwise curve swept up
    # inward (Blender 4.2 and 5.2): flip them to face out.
    outward = nodes.new('GeometryNodeFlipFaces')
    links.new(sides.outputs["Mesh"], outward.inputs["Mesh"])
    outward.inputs["Selection"].default_value = flip_sides
    join = nodes.new('GeometryNodeJoinGeometry')
    links.new(outward.outputs["Mesh"], join.inputs[0])
    if solid:
        # Fill Curve makes its mesh in the plane z = 0, wherever the curve
        # is: the curve is filled where it is drawn (in that plane, see
        # _frame) and the cap moved to the start. Filled after the move, the
        # caps of an extrusion to both sides were at the curve and a whole
        # sweep above it, not at the ends of the walls.
        fill = nodes.new('GeometryNodeFillCurve')
        links.new(info.outputs["Geometry"], fill.inputs["Curve"])
        cap = nodes.new('GeometryNodeTransform')
        links.new(fill.outputs["Mesh"], cap.inputs["Geometry"])
        links.new(group_in.outputs["Start"], cap.inputs["Translation"])
        bottom = nodes.new('GeometryNodeFlipFaces')
        links.new(cap.outputs["Geometry"], bottom.inputs["Mesh"])
        bottom.inputs["Selection"].default_value = flip_bottom
        top_flip = nodes.new('GeometryNodeFlipFaces')
        links.new(cap.outputs["Geometry"], top_flip.inputs["Mesh"])
        top_flip.inputs["Selection"].default_value = not flip_bottom
        top = nodes.new('GeometryNodeTransform')
        links.new(top_flip.outputs["Mesh"], top.inputs["Geometry"])
        links.new(group_in.outputs["Sweep"], top.inputs["Translation"])
        links.new(bottom.outputs["Mesh"], join.inputs[0])
        links.new(top.outputs["Geometry"], join.inputs[0])
    merge = nodes.new('GeometryNodeMergeByDistance')
    links.new(join.outputs["Geometry"], merge.inputs["Geometry"])
    links.new(merge.outputs["Geometry"], group_out.inputs["Geometry"])
    return tree


def _winding(polylines, matrix):
    """+1 if the first curve turns counter-clockwise in the local XY plane of
    ``matrix``, -1 if clockwise."""
    inverse = matrix.inverted()
    points = [inverse @ Vector(tuple(p)) for p in polylines[0][0]]
    area = sum(a.x * b.y - b.x * a.y for a, b in zip(points, points[1:] + points[:1]))
    return 1 if area >= 0 else -1


def _frame(curve, context):
    """Matrix of the curve's plane (z its normal), at its center; or at its
    center with the world axes if it is not planar."""
    polylines = extrude.curve_polylines(curve, settings.get(context).mesh_angle)
    points = [p for points, _, _ in polylines for p in points]
    center = sum((Vector(tuple(p)) for p in points), Vector()) / max(len(points), 1)
    closed = [is_closed for _, is_closed, _ in polylines]
    found = extrude.plane_of(polylines[0][0]) if polylines else None
    if found is None:
        return Matrix.Translation(center), False, 1, closed
    z = found[1]
    x = z.orthogonal().normalized()
    y = z.cross(x)
    matrix = Matrix.Translation(center) @ Matrix((x, y, z)).transposed().to_4x4()
    return matrix, True, _winding(polylines, matrix), closed


def extrude_curve(context, curve, start, sweep, solid):
    """A linked extrusion of ``curve`` (world vectors ``start`` and ``sweep``),
    or None if it cannot be made (the caller makes a plain mesh)."""
    if curve.type != 'CURVE':
        return None
    matrix, planar, winding, closed = _frame(curve, context)
    # Caps for closed curves only, as without History (Fill Curve would
    # close an open curve with a face). Closed and open curves in one
    # object: the plain mesh caps just the closed ones.
    caps = solid and planar and bool(closed) and all(closed)
    if solid and planar and any(closed) and not caps:
        return None
    to_local = matrix.to_3x3().inverted()
    local_sweep = to_local @ sweep
    data = bpy.data.meshes.new("Extrusion")
    obj = bpy.data.objects.new("Extrusion", data)
    context.collection.objects.link(obj)
    obj.matrix_world = matrix
    up = 1 if local_sweep.z >= 0 else -1
    tree = _extrusion_tree("Rhino Mode ExtrudeCrv", curve, caps, flip_bottom=up > 0, flip_sides=winding * up > 0)
    modifier = obj.modifiers.new("ExtrudeCrv", 'NODES')
    modifier.node_group = tree
    set_input(modifier, "Start", tuple(to_local @ start))
    set_input(modifier, "Sweep", tuple(local_sweep))
    return obj
