# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style selection with the left mouse button (MODE feature).

* Drag left to right: window selection, objects completely inside the
  rectangle (solid outline). Drag right to left: crossing selection,
  objects inside or touching it (dashed outline).
* Shift adds to the selection, Ctrl removes from it (click or drag).
* Dragging on an object (or on a mesh or curve point, in Edit Mode) moves it,
  selecting it first if needed; releasing the button ends the move. Objects
  and points move from the grabbed point to the released one, both snapped
  (Osnap). In Edit
  Mode a point counts as under the mouse within a small pick radius (Blender's
  own click select reaches much further, which would turn most drags near a
  mesh into moves). On a dense mesh (several points within the radius) only
  a press on the selection grabs; elsewhere the drag selects.
* Click selects, on empty space deselects everything (Blender's click
  select, also with the preferences set to Select with Right).

Blender's object box select already picks objects with any visible part in
the box, which is Rhino's crossing selection. The window test is done here:
an object counts as inside when all its points project into the rectangle
(the evaluated mesh vertices, or the bounding box for other types). In Edit
Mode points are either inside or not, so both directions use box select.

The drag and click items of Blender's selection tools and the modeling
keymaps are switched off in memory while the mode is on; other tools (Move,
Extrude, ...) keep their drags.
"""

import math

import bpy
import gpu
import numpy as np
from gpu_extras.batch import batch_for_shader

from . import keymaps
from .projection import project

# Keymaps of Blender's selection tools: their LMB items (without Alt) give
# way to R2B's click and drag selection whichever selection tool is active.
SELECT_TOOL_KEYMAPS = tuple(
    f"3D View Tool: {name}{suffix}"
    for name in ("Tweak", "Select Box", "Select Circle", "Select Lasso")
    for suffix in ("", " (fallback)")
)
# Keymaps whose LMB drag (tweak to move) and Ctrl+click (shortest path)
# give way to R2B's selection.
MODE_KEYMAPS = ("Object Mode", "Mesh", "Curve", "Curves", "Armature", "Metaball",
                "Lattice", "Pose", "Point Cloud")

# Pick radius for points in Edit Mode, in pixels at UI scale 1.
PICK_RADIUS = 8
# Points closer than this on screen (pixels at UI scale 1) count as one.
TOUCHING = 1.5

_muted = keymaps.MutedItems()
_addon_items = keymaps.AddonItems()
_draw_handle = None
# The rectangle being dragged: (region pointer, start, end), for drawing.
_rectangle = None
# The loop of the Lasso command: (region pointer, [points]), for drawing.
_lasso = None


# Select tools that keep their own drag when picked in the toolbar (Lasso is
# also Rhino's Lasso command, below). Not Circle Select: it selects on the
# press, before a click on a viewport title can be told apart; it is typed
# instead (CircleSelect, Blender's C).
_OWN_DRAG_TOOLS = {"view3d.select_lasso"}


def _is_select_tool_item(kmi):
    return kmi.type == 'LEFTMOUSE' and kmi.alt != 1 and kmi.idname not in _OWN_DRAG_TOOLS


def _is_mode_item(kmi):
    if kmi.type != 'LEFTMOUSE' or kmi.alt == 1:
        return False
    return kmi.value == 'CLICK_DRAG' or (kmi.value == 'CLICK' and kmi.ctrl == 1)


# --- Window selection -----------------------------------------------------------

def _world_points(obj, depsgraph):
    """Points of an object in world space: the evaluated mesh vertices for
    meshes, the bounding box corners or the origin otherwise."""
    evaluated = obj.evaluated_get(depsgraph)
    matrix = np.array(evaluated.matrix_world)
    if obj.type == 'MESH' and len(evaluated.data.vertices):
        local = np.empty(len(evaluated.data.vertices) * 3, dtype=np.float32)
        evaluated.data.vertices.foreach_get("co", local)
        local = local.reshape(-1, 3)
    elif obj.type in {'EMPTY', 'LIGHT', 'CAMERA', 'SPEAKER', 'LIGHT_PROBE'}:
        local = np.zeros((1, 3))
    else:
        local = np.array([tuple(corner) for corner in evaluated.bound_box])
    homogeneous = np.hstack([local, np.ones((len(local), 1))])
    return homogeneous @ matrix.T


def edit_mesh_grab_point(context, location):
    """Region coordinates of the mesh point a drag from ``location`` grabs
    (Edit Mode), or None to make the drag a window or crossing selection.

    A point is grabbed when the press is on the selection (a selected point
    within the pick radius: the selection moves) or on one point alone. On a
    dense mesh, where several points are within the radius, an unselected
    point is not grabbed: there almost every press is near some point, and
    grabbing it would make window selection impossible (and move a point
    by accident).
    """
    radius = PICK_RADIUS * context.preferences.system.ui_scale
    near = []  # (distance, selected, xy) of the points within the radius
    for obj in context.objects_in_mode:
        if obj.type != 'MESH':
            continue
        obj.update_from_editmode()
        vertices = obj.data.vertices
        if not len(vertices):
            continue
        local = np.empty(len(vertices) * 3, dtype=np.float32)
        vertices.foreach_get("co", local)
        hidden = np.empty(len(vertices), dtype=bool)
        vertices.foreach_get("hide", hidden)
        selected = np.empty(len(vertices), dtype=bool)
        vertices.foreach_get("select", selected)
        homogeneous = np.hstack([local.reshape(-1, 3), np.ones((len(vertices), 1))])
        xy = project(homogeneous @ np.array(obj.matrix_world).T, context.region, context.region_data)
        distance = np.hypot(xy[:, 0] - location[0], xy[:, 1] - location[1])
        distance[hidden | np.isnan(distance)] = np.inf
        for index in np.flatnonzero(distance <= radius):
            near.append((float(distance[index]), bool(selected[index]), tuple(xy[index])))
    if not near:
        return None
    on_selection = [n for n in near if n[1]]
    if on_selection:
        return min(on_selection)[2]
    # Points on top of each other (a box's top and bottom corners seen from
    # Top) are one target.
    _, _, (x, y) = min(near)
    touching = TOUCHING * context.preferences.system.ui_scale
    if all(math.hypot(p[0] - x, p[1] - y) <= touching for _, _, p in near):
        return min(near)[2]
    return None


def edit_curve_point_near(context, location):
    """Region coordinates of the visible curve control point or Bezier
    handle nearest to ``location`` within the pick radius, or None (curve
    Edit Mode)."""
    radius = PICK_RADIUS * context.preferences.system.ui_scale
    world = []
    for obj in context.objects_in_mode:
        if obj.type != 'CURVE':
            continue
        local = []
        for spline in obj.data.splines:
            if spline.type == 'BEZIER':
                local += [tuple(getattr(p, name)) for p in spline.bezier_points if not p.hide
                          for name in ("co", "handle_left", "handle_right")]
            else:
                local += [tuple(p.co.xyz) for p in spline.points if not p.hide]
        if local:
            homogeneous = np.hstack([np.array(local, dtype=np.float64), np.ones((len(local), 1))])
            world.append(homogeneous @ np.array(obj.matrix_world).T)
    if not world:
        return None
    xy = project(np.concatenate(world), context.region, context.region_data)
    distance = np.hypot(xy[:, 0] - location[0], xy[:, 1] - location[1])
    distance[np.isnan(distance)] = np.inf
    index = int(np.argmin(distance))
    return tuple(xy[index]) if distance[index] <= radius else None


def inside_rectangle(points, region, rv3d, rect):
    """Whether all world-space points (N x 4) project into ``rect``
    (xmin, ymin, xmax, ymax in region coordinates)."""
    xy = project(points, region, rv3d)
    if np.isnan(xy).any():
        return False  # Behind the view.
    xmin, ymin, xmax, ymax = rect
    x, y = xy[:, 0], xy[:, 1]
    return bool(np.all((x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax)))


def window_select_objects(context, rect, mode):
    region, rv3d = context.region, context.region_data
    depsgraph = context.evaluated_depsgraph_get()
    inside = [obj for obj in context.selectable_objects
              if inside_rectangle(_world_points(obj, depsgraph), region, rv3d, rect)]
    if mode == 'SET':
        for obj in context.selected_objects:
            obj.select_set(False)
    for obj in inside:
        obj.select_set(mode != 'SUB')
    view_layer = context.view_layer
    active = view_layer.objects.active
    if mode != 'SUB' and inside and (active is None or not active.select_get()):
        view_layer.objects.active = inside[0]


# --- Drawing the rectangle ------------------------------------------------------

def _dashes(a, b, length):
    """Line segments of a dashed line from a to b."""
    (x0, y0), (x1, y1) = a, b
    total = max(abs(x1 - x0), abs(y1 - y0))
    segments = []
    t = 0.0
    while t < total:
        t1 = min(t + length, total)
        for s in (t, t1):
            f = s / total if total else 0.0
            segments.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
        t += 2 * length
    return segments


def _draw_lasso():
    if _lasso is None or bpy.context.region.as_pointer() != _lasso[0] or len(_lasso[1]) < 2:
        return
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    loop = batch_for_shader(shader, 'LINE_LOOP', {"pos": _lasso[1]})
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.9))
    loop.draw(shader)
    gpu.state.blend_set('NONE')


def _draw_rectangle():
    _draw_lasso()
    if _rectangle is None or bpy.context.region.as_pointer() != _rectangle[0]:
        return
    _, (x0, y0), (x1, y1) = _rectangle
    crossing = x1 < x0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    fill = batch_for_shader(shader, 'TRIS', {"pos": corners}, indices=((0, 1, 2), (0, 2, 3)))
    shader.uniform_float("color", (0.3, 0.6, 1.0, 0.08) if not crossing else (0.3, 1.0, 0.5, 0.08))
    fill.draw(shader)
    if crossing:
        dash = 6 * bpy.context.preferences.system.ui_scale
        points = []
        for i in range(4):
            points += _dashes(corners[i], corners[(i + 1) % 4], dash)
        outline = batch_for_shader(shader, 'LINES', {"pos": points})
    else:
        outline = batch_for_shader(shader, 'LINE_LOOP', {"pos": corners})
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.9))
    outline.draw(shader)
    gpu.state.blend_set('NONE')


# --- Operator -------------------------------------------------------------------

class R2B_OT_select_drag(bpy.types.Operator):
    """Drag on empty space to select (left to right: window, right to left:
    crossing), or on an object to move it"""

    bl_idname = "r2b.select_drag"
    bl_label = "Select"
    bl_options = {'REGISTER', 'UNDO'}

    mode: bpy.props.EnumProperty(items=(
        ('SET', "Set", "Select only what is in the rectangle"),
        ('ADD', "Add", "Add to the selection"),
        ('SUB', "Subtract", "Remove from the selection"),
    ))

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.region_data is not None

    def invoke(self, context, event):
        global _rectangle
        region = context.region
        start = (event.mouse_prev_press_x - region.x, event.mouse_prev_press_y - region.y)
        if self.mode == 'SET' and self._drag_move(context, start):
            return {'FINISHED'}
        self._region = region
        self._start = start
        _rectangle = (region.as_pointer(), start, (event.mouse_region_x, event.mouse_region_y))
        context.window_manager.modal_handler_add(self)
        region.tag_redraw()
        return {'RUNNING_MODAL'}

    def _drag_move(self, context, start):
        """Something under the press point: select it (keeping an existing
        selection that contains it) and move it, as dragging in Rhino.

        view3d.select cannot tell "already selected" from "nothing there"
        (both return CANCELLED with select_passthrough), so objects are
        picked without passthrough and the selection restored afterwards.
        """
        if context.mode == 'OBJECT':
            before = set(context.selected_objects)
            result = bpy.ops.view3d.select(location=start, deselect_all=False)
            if 'FINISHED' not in result:
                return False
            hit = context.view_layer.objects.active
            if hit in before:
                for obj in before:
                    obj.select_set(True)
        elif context.mode in {'EDIT_MESH', 'EDIT_CURVE'}:
            near = edit_mesh_grab_point if context.mode == 'EDIT_MESH' else edit_curve_point_near
            point = near(context, start)
            if point is None:
                return False
            location = (int(round(point[0])), int(round(point[1])))
            bpy.ops.view3d.select(location=location, select_passthrough=True, deselect_all=False)
        else:
            return False
        # Rhino-style drag: grabbed and target points snap (transform.py).
        bpy.ops.r2b.drag_move('INVOKE_DEFAULT')
        return True

    def modal(self, context, event):
        global _rectangle
        if event.type == 'MOUSEMOVE':
            _rectangle = (self._region.as_pointer(), self._start, (event.mouse_region_x, event.mouse_region_y))
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            _rectangle = None
            self._region.tag_redraw()
            return {'CANCELLED'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            (x0, y0), (x1, y1) = self._start, (event.mouse_region_x, event.mouse_region_y)
            _rectangle = None
            self._region.tag_redraw()
            rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            window = x1 >= x0
            if context.mode == 'OBJECT' and window:
                window_select_objects(context, rect, self.mode)
            else:
                bpy.ops.view3d.select_box(xmin=int(rect[0]), ymin=int(rect[1]), xmax=int(rect[2]),
                                          ymax=int(rect[3]), wait_for_input=False, mode=self.mode)
            return {'FINISHED'}
        return {'RUNNING_MODAL'}


class R2B_OT_lasso(bpy.types.Operator):
    """Select inside a freehand loop (Rhino's Lasso): drag around the objects,
    or the points in Edit Mode. Shift adds, Ctrl removes"""

    bl_idname = "r2b.lasso"
    bl_label = "Lasso"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area is not None and context.area.type == 'VIEW_3D'
                and context.mode in {'OBJECT', 'EDIT_MESH', 'EDIT_CURVE'})

    def invoke(self, context, event):
        from . import command_line

        self._region = context.region
        self._points = []
        command_line.set_prompt(context.window, "Drag around what to select (Shift adds, Ctrl removes)")
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _end(self, context, result):
        global _lasso
        from . import command_line

        _lasso = None
        command_line.set_prompt(context.window)
        self._region.tag_redraw()
        return result

    def modal(self, context, event):
        global _lasso
        point = (event.mouse_x - self._region.x, event.mouse_y - self._region.y)
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return self._end(context, {'CANCELLED'})
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self._points = [point]
        elif event.type == 'MOUSEMOVE' and self._points:
            last = self._points[-1]
            if math.hypot(point[0] - last[0], point[1] - last[1]) >= 2:
                self._points.append(point)
                _lasso = (self._region.as_pointer(), list(self._points))
                self._region.tag_redraw()
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE' and self._points:
            if len(self._points) < 3:
                return self._end(context, {'CANCELLED'})
            mode = 'ADD' if event.shift else 'SUB' if event.ctrl else 'SET'
            path = [{"name": "", "loc": p, "time": 0.0} for p in self._points]
            bpy.ops.view3d.select_lasso(path=path, mode=mode)
            return self._end(context, {'FINISHED'})
        elif event.type in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}


_classes = (R2B_OT_select_drag, R2B_OT_lasso)


def register():
    global _draw_handle
    for cls in _classes:
        bpy.utils.register_class(cls)
    add = _addon_items.add
    for modifiers, mode in (({}, 'SET'), ({"shift": True}, 'ADD'), ({"ctrl": True}, 'SUB')):
        add("r2b.select_drag", 'LEFTMOUSE', 'CLICK_DRAG', properties={"mode": mode}, **modifiers)
    # Click selects (also when the preferences say Select with Right, where
    # Blender has no left click select); Shift+click adds, Ctrl+click
    # removes (Blender toggles).
    add("view3d.select", 'LEFTMOUSE', 'CLICK', properties={"deselect_all": True})
    add("view3d.select", 'LEFTMOUSE', 'CLICK', shift=True, properties={"extend": True, "deselect_all": False})
    add("view3d.select", 'LEFTMOUSE', 'CLICK', ctrl=True, properties={"deselect": True, "deselect_all": False})
    keyconfig = bpy.context.window_manager.keyconfigs.active
    _muted.mute(keyconfig, SELECT_TOOL_KEYMAPS, _is_select_tool_item)
    _muted.mute(keyconfig, MODE_KEYMAPS, _is_mode_item)
    keymaps.update_keyconfigs()
    _draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_rectangle, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _draw_handle, _rectangle, _lasso
    if _draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    _rectangle = None
    _lasso = None
    _muted.restore()
    _addon_items.remove()
    keymaps.update_keyconfigs()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
