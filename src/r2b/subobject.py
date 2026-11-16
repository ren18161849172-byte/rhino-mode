# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's sub-object selection (MODE feature): Ctrl+Shift+click picks a
face, edge or point of a mesh without selecting the object.

Blender keeps the parts of a mesh selectable only in Edit Mode, so picking a
sub-object puts the mesh into Edit Mode with that part selected (the select
mode follows what was picked: a point or edge when the click is within a few
pixels of one, else the face). The Gumball and the commands that work on
selected points, edges or faces then act on it, as in Rhino.

* Ctrl+Shift+click: add the face, edge or point under the mouse (Ctrl+click
  removes, as for objects). A face brings along the faces joined to it in
  the same plane (Rhino's face of a polysurface). Clicks on other meshes add
  those meshes to Edit Mode.
* Ctrl+Shift+drag: select the points inside the rectangle (in the meshes
  under it).
* Ctrl+Shift+double-click: the loop through the edge or face under the mouse.
* When nothing is selected any more (click on empty space, Esc), the meshes
  go back to Object Mode and the objects selected before are selected again.
  Only Edit Mode entered this way ends by itself.
"""

import bmesh
import numpy as np

import bpy
from bpy_extras.view3d_utils import location_3d_to_region_2d, region_2d_to_origin_3d, region_2d_to_vector_3d
from mathutils import bvhtree

from . import keymaps
from .projection import project

# Within this many pixels (times the UI scale) of a point or an edge, the
# click picks that instead of the face.
POINT_PIXELS = 10
EDGE_PIXELS = 6
COPLANAR = 1e-4  # Tolerance for faces in the same plane (normal and distance).

_addon_items = keymaps.AddonItems()
# Edit Mode entered by picking a sub-object: (object names selected before, active object name), or None.
_entered = None
_check_scheduled = False


def entered():
    return _entered is not None


# --- Picking -------------------------------------------------------------------------

def _edit_mesh(obj):
    if obj.mode == 'EDIT':
        return bmesh.from_edit_mesh(obj.data), False
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    return mesh, True


def pick(context, mouse, faces_only=False):
    """(object, kind, index) of the mesh face, edge or point ('FACE', 'EDGE',
    'VERT') under ``mouse`` (region coordinates), or None. ``faces_only``:
    always the face, and (object, 'FACE', index, world location)."""
    region, rv3d = context.region, context.region_data
    origin = region_2d_to_origin_3d(region, rv3d, mouse)
    direction = region_2d_to_vector_3d(region, rv3d, mouse)
    depsgraph = context.evaluated_depsgraph_get()
    hit, location, _normal, _index, obj, _matrix = context.scene.ray_cast(depsgraph, origin, direction)
    if not hit or obj is None or obj.type != 'MESH':
        return None
    mesh, owned = _edit_mesh(obj)
    try:
        mesh.faces.ensure_lookup_table()
        tree = bvhtree.BVHTree.FromBMesh(mesh)
        inverse = obj.matrix_world.inverted()
        local_origin = inverse @ origin
        local_direction = (inverse.to_3x3() @ direction).normalized()
        found = tree.ray_cast(local_origin, local_direction)
        if found[2] is None:  # The evaluated surface (modifiers) differs: the nearest face.
            found = tree.find_nearest(inverse @ location)
        if found[2] is None:
            return None
        face = mesh.faces[found[2]]
        if faces_only:
            return obj, 'FACE', face.index, obj.matrix_world @ found[0]
        matrix = obj.matrix_world
        scale = context.preferences.system.ui_scale
        screen = {v.index: location_3d_to_region_2d(region, rv3d, matrix @ v.co) for v in face.verts}
        mouse_np = np.array(mouse, dtype=np.float64)
        best = min(((np.linalg.norm(np.array(p) - mouse_np), i) for i, p in screen.items() if p is not None),
                   default=None)
        if best is not None and best[0] <= POINT_PIXELS * scale:
            return obj, 'VERT', best[1]
        edges = []
        for edge in face.edges:
            a, b = screen.get(edge.verts[0].index), screen.get(edge.verts[1].index)
            if a is None or b is None:
                continue
            edges.append((_segment_distance(mouse_np, np.array(a), np.array(b)), edge.index))
        best = min(edges, default=None)
        if best is not None and best[0] <= EDGE_PIXELS * scale:
            return obj, 'EDGE', best[1]
        return obj, 'FACE', face.index
    finally:
        if owned:
            mesh.free()


def _segment_distance(p, a, b):
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip((p - a).dot(ab) / ab.dot(ab), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * ab)))


def coplanar_region(face, barriers=frozenset()):
    """The faces joined to ``face`` through edges (not through ``barriers``),
    in its plane."""
    normal, point = face.normal, face.calc_center_median()
    region, todo = {face}, [face]
    while todo:
        current = todo.pop()
        for edge in current.edges:
            if edge in barriers:
                continue
            for other in edge.link_faces:
                if other in region:
                    continue
                if (other.normal - normal).length < COPLANAR and abs((other.calc_center_median() - point).dot(normal)) < COPLANAR:
                    region.add(other)
                    todo.append(other)
    return region


# --- Entering and leaving Edit Mode ------------------------------------------------

def _enter(context, objects):
    """Put the meshes ``objects`` into Edit Mode (with the ones already
    there), remembering the object selection the first time."""
    global _entered
    in_mode = list(context.objects_in_mode) if context.mode == 'EDIT_MESH' else []
    if all(obj in in_mode for obj in objects):
        return
    if _entered is None and context.mode == 'OBJECT':
        active = context.view_layer.objects.active
        _entered = ([o.name for o in context.selected_objects], active.name if active else None)
    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.selected_objects:
        obj.select_set(False)
    chosen = in_mode + [obj for obj in objects if obj not in in_mode]
    for obj in chosen:
        obj.select_set(True)
    context.view_layer.objects.active = objects[0]
    bpy.ops.object.mode_set(mode='EDIT')
    for obj in objects:
        if obj in in_mode:
            continue
        # Start from nothing selected (the mesh keeps the selection of its last edit).
        mesh = bmesh.from_edit_mesh(obj.data)
        for element in (*mesh.verts, *mesh.edges, *mesh.faces):
            element.select = False
        bmesh.update_edit_mesh(obj.data)


def leave(context):
    """Back to Object Mode with the objects selected before."""
    global _entered
    if _entered is None:
        return
    names, active = _entered
    _entered = None
    if context.mode == 'EDIT_MESH':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.view_layer.objects:
        obj.select_set(obj.name in names)
    context.view_layer.objects.active = bpy.data.objects.get(active) if active else None


def _set_select_mode(context, kind, extend):
    tool = context.tool_settings
    wanted = {'VERT': (True, False, False), 'EDGE': (False, True, False), 'FACE': (False, False, True)}[kind]
    if extend:
        wanted = tuple(a or b for a, b in zip(tool.mesh_select_mode, wanted))
    if tuple(tool.mesh_select_mode) != wanted:
        tool.mesh_select_mode = wanted


def _nothing_selected(context):
    return not any(v.select for obj in context.objects_in_mode if obj.type == 'MESH'
                   for v in bmesh.from_edit_mesh(obj.data).verts)


# --- Operators -------------------------------------------------------------------------

class _SubObjectOperator:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area is not None and context.area.type == 'VIEW_3D' and context.region_data is not None
                and context.mode in {'OBJECT', 'EDIT_MESH'})


class R2B_OT_select_subobject(_SubObjectOperator, bpy.types.Operator):
    """Select the face, edge or point under the mouse (Rhino's Ctrl+Shift+click)"""

    bl_idname = "r2b.select_subobject"
    bl_label = "Select Sub-object"

    loop: bpy.props.BoolProperty(name="Loop", description="Select the loop through it",
                                 options={'SKIP_SAVE'})

    def invoke(self, context, event):
        mouse = (event.mouse_region_x, event.mouse_region_y)
        found = pick(context, mouse)
        if found is None:
            return {'CANCELLED'}
        obj, kind, index = found
        was_editing = context.mode == 'EDIT_MESH' and obj in context.objects_in_mode
        _enter(context, [obj])
        _set_select_mode(context, kind, extend=was_editing)
        if self.loop and kind != 'VERT':
            bpy.ops.mesh.loop_select('INVOKE_DEFAULT', extend=True, deselect=False, toggle=False)
            return {'FINISHED'}
        mesh = bmesh.from_edit_mesh(obj.data)
        elements = {'VERT': mesh.verts, 'EDGE': mesh.edges, 'FACE': mesh.faces}[kind]
        elements.ensure_lookup_table()
        element = elements[index]
        for item in coplanar_region(element) if kind == 'FACE' else (element,):
            item.select_set(True)
        mesh.select_flush_mode()
        bmesh.update_edit_mesh(obj.data)
        return {'FINISHED'}


class R2B_OT_select_subobject_box(_SubObjectOperator, bpy.types.Operator):
    """Select the points inside a rectangle (Rhino's Ctrl+Shift+drag)"""

    bl_idname = "r2b.select_subobject_box"
    bl_label = "Select Sub-objects in Rectangle"

    def invoke(self, context, event):
        region = context.region
        self._start = (event.mouse_prev_press_x - region.x, event.mouse_prev_press_y - region.y)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            (x0, y0), (x1, y1) = self._start, (event.mouse_region_x, event.mouse_region_y)
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            meshes = self._meshes_in(context, rect)
            if not meshes:
                return {'CANCELLED'}
            _enter(context, meshes)
            bpy.ops.view3d.select_box(xmin=int(rect[0]), ymin=int(rect[1]), xmax=int(rect[2]), ymax=int(rect[3]),
                                      wait_for_input=False, mode='ADD')
            return {'FINISHED'}
        return {'RUNNING_MODAL'}

    @staticmethod
    def _meshes_in(context, rect):
        """Visible meshes with points inside the rectangle."""
        region, rv3d = context.region, context.region_data
        found = []
        for obj in context.visible_objects:
            if obj.type != 'MESH':
                continue
            if obj.mode == 'EDIT':
                # First: the mesh may have gained points since it was last
                # written (an extrusion in Edit Mode), and the array below
                # would have the old size.
                obj.update_from_editmode()
            if not len(obj.data.vertices):
                continue
            local = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
            obj.data.vertices.foreach_get("co", local)
            points = np.hstack([local.reshape(-1, 3), np.ones((len(obj.data.vertices), 1))]) @ np.array(obj.matrix_world).T
            xy = project(points, region, rv3d)
            inside = ((xy[:, 0] >= rect[0]) & (xy[:, 0] <= rect[2]) & (xy[:, 1] >= rect[1]) & (xy[:, 1] <= rect[3]))
            if np.any(inside):
                found.append(obj)
        return found


_classes = (R2B_OT_select_subobject, R2B_OT_select_subobject_box)


# --- Leaving Edit Mode when the sub-object selection is empty ----------------------

def _check():
    global _check_scheduled
    _check_scheduled = False
    if _entered is None:
        return None
    context = bpy.context
    window = context.window_manager.windows[0] if context.window_manager.windows else None
    if window is None:
        return None
    with context.temp_override(window=window):
        if bpy.context.mode != 'EDIT_MESH':
            _forget()  # Left Edit Mode some other way.
        elif _nothing_selected(bpy.context):
            leave(bpy.context)
    return None


def _forget():
    global _entered
    _entered = None


def _on_depsgraph_update(scene, depsgraph):
    global _check_scheduled
    if _entered is not None and not _check_scheduled:
        _check_scheduled = True
        bpy.app.timers.register(_check, first_interval=0.0)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    add = _addon_items.add
    # In the mode keymaps, which come before the 3D View keymap (there,
    # Ctrl+Shift+click selects with "center"; in Mesh it picks a shortest path).
    for keymap in ("Object Mode", "Mesh"):
        add("r2b.select_subobject_box", 'LEFTMOUSE', 'CLICK_DRAG', keymap=keymap, space_type='EMPTY', ctrl=True,
            shift=True)
        add("r2b.select_subobject", 'LEFTMOUSE', 'CLICK', keymap=keymap, space_type='EMPTY', ctrl=True, shift=True)
        add("r2b.select_subobject", 'LEFTMOUSE', 'DOUBLE_CLICK', keymap=keymap, space_type='EMPTY', ctrl=True,
            shift=True, properties={"loop": True})
    keymaps.update_keyconfigs()
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    global _check_scheduled
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if bpy.app.timers.is_registered(_check):
        bpy.app.timers.unregister(_check)
    _check_scheduled = False
    _forget()
    _addon_items.remove()
    keymaps.update_keyconfigs()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
