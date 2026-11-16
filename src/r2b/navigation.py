# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style right mouse button navigation (MODE feature).

* RMB drag: rotate in perspective views, pan in parallel (orthographic) views
* Shift+RMB drag: pan, Ctrl+RMB drag: zoom, Ctrl+Shift+RMB drag: rotate
* RMB click: Enter in the command line (run what is typed or repeat the last
  command); Blender's last operation before any command was run

Blender's own right mouse items in the modeling keymaps (context menus,
3D cursor, lasso, extrude to cursor) are switched off in memory while the
mode is on and switched back on when it is turned off. Sculpt and paint
modes keep Blender's right button items, but their brush panel (RMB) and
pivot (Shift+RMB) open on click, so that RMB and Shift+RMB drags navigate
there too; Ctrl+RMB stays Blender's lasso mask.
"""

import math

import bpy
from bpy_extras.view3d_utils import region_2d_to_location_3d
from mathutils import Quaternion

from . import keymaps

# Keymaps used in Object, Edit and Pose modes of the 3D viewport.
MODELING_KEYMAPS = (
    "3D View",
    "Object Mode",
    "Mesh",
    "Curve",
    "Curves",
    "Armature",
    "Metaball",
    "Lattice",
    "Font",
    "Pose",
    "Point Cloud",
)

# Sculpt and paint modes keep Blender's right button, but on click: their
# brush panel (RMB) and pivot (Shift+RMB) open when pressed, which would
# leave no drag for R2B's navigation. With CLICK, RMB drag orbits and
# Shift+RMB drag pans as in the other modes. Ctrl+RMB stays the lasso mask.
PAINT_KEYMAPS = ("Sculpt", "Image Paint", "Vertex Paint", "Weight Paint")
PAINT_RMB_ON_CLICK = {"wm.call_panel", "sculpt.set_pivot_position"}

# Operators that RMB click never repeats.
_NOT_REPEATED_MODULES = {"ed", "r2b", "screen", "wm"}


def _split_idname(bl_idname):
    """'OBJECT_OT_select_all' or 'object.select_all' -> ('object', 'select_all')."""
    if "_OT_" in bl_idname:
        module, name = bl_idname.split("_OT_", 1)
    else:
        module, _, name = bl_idname.partition(".")
    return module.lower(), name.lower()


def is_repeatable(bl_idname):
    module, name = _split_idname(bl_idname)
    return module not in _NOT_REPEATED_MODULES and "select" not in name


def last_repeatable_index(bl_idnames):
    for index in range(len(bl_idnames) - 1, -1, -1):
        if is_repeatable(bl_idnames[index]):
            return index
    return None


class R2B_OT_repeat_last_command(bpy.types.Operator):
    """Repeat the last command, skipping selection"""

    bl_idname = "r2b.repeat_last_command"
    bl_label = "Repeat Last Command"
    # Not registered itself: the Adjust Last Operation panel keeps showing the
    # repeated command's options.
    bl_options = {'INTERNAL'}

    def execute(self, context):
        from . import command_line

        # In Rhino, RMB click is Enter: run what is typed or repeat the last
        # command. Before any command was run, repeat Blender's last operation.
        if command_line.enter(context):
            return {'FINISHED'}
        history = [op.bl_idname for op in context.window_manager.operators]
        index = last_repeatable_index(history)
        if index is None:
            return {'CANCELLED'}
        # undo=True: the repeated operator pushes its undo step.
        return bpy.ops.screen.repeat_history('EXEC_DEFAULT', True, index=index)


class R2B_OT_view_pan_parallel(bpy.types.Operator):
    """Pan the view with the right mouse button (parallel views only)"""

    bl_idname = "r2b.view_pan_parallel"
    bl_label = "Pan Parallel View"

    @classmethod
    def poll(cls, context):
        # In perspective views the poll fails, so the keymap falls through
        # to the next RMB drag item, which is view3d.rotate.
        rv3d = context.region_data
        return rv3d is not None and rv3d.view_perspective == 'ORTHO'

    def invoke(self, context, event):
        self._region = context.region
        self._rv3d = context.region_data
        self._start_location = self._rv3d.view_location.copy()
        self._last_mouse = (event.mouse_region_x, event.mouse_region_y)
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('SCROLL_XY')
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'MOUSEMOVE':
            mouse = (event.mouse_region_x, event.mouse_region_y)
            depth = self._rv3d.view_location
            before = region_2d_to_location_3d(self._region, self._rv3d, self._last_mouse, depth)
            after = region_2d_to_location_3d(self._region, self._rv3d, mouse, depth)
            self._rv3d.view_location = depth + (before - after)
            self._last_mouse = mouse
            self._region.tag_redraw()
        elif event.type == 'RIGHTMOUSE' and event.value == 'RELEASE':
            context.window.cursor_modal_restore()
            return {'FINISHED'}
        elif event.type == 'ESC':
            self._rv3d.view_location = self._start_location
            self._region.tag_redraw()
            context.window.cursor_modal_restore()
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}


class R2B_OT_turntable(bpy.types.Operator):
    """Turn the view around the world's vertical axis until a key or a click
    (Rhino's Turntable)"""

    bl_idname = "r2b.turntable"
    bl_label = "Turntable"

    # Degrees per second.
    SPEED = 30.0

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        from . import command_line

        self._area = context.area
        self._interval = 1 / 30
        self._timer = context.window_manager.event_timer_add(self._interval, window=context.window)
        context.window_manager.modal_handler_add(self)
        command_line.set_prompt(context.window, "Turntable: press any key or click to stop")
        return {'RUNNING_MODAL'}

    def _stop(self, context):
        from . import command_line

        context.window_manager.event_timer_remove(self._timer)
        command_line.set_prompt(context.window)

    def modal(self, context, event):
        if event.type == 'TIMER' and event.value != 'RELEASE':
            space = self._area.spaces.active if self._area.spaces else None
            if space is None or space.type != 'VIEW_3D':
                self._stop(context)
                return {'CANCELLED'}
            rv3d = space.region_3d
            if rv3d.view_perspective == 'CAMERA':
                rv3d.view_perspective = 'PERSP'
            turn = Quaternion((0.0, 0.0, 1.0), math.radians(self.SPEED * self._interval))
            rv3d.view_rotation = turn @ rv3d.view_rotation
            self._area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.value == 'PRESS' and event.type not in {'TIMER', 'TIMER_REPORT', 'NONE'}:
            self._stop(context)
            return {'FINISHED'}  # The key or click only stops it.
        return {'PASS_THROUGH'}


_classes = (R2B_OT_repeat_last_command, R2B_OT_view_pan_parallel, R2B_OT_turntable)


_muted = keymaps.MutedItems()
_paint_rmb = keymaps.ChangedItems()
_addon_items = keymaps.AddonItems()


def _is_paint_rmb_press(kmi):
    return (kmi.type == 'RIGHTMOUSE' and kmi.value == 'PRESS' and kmi.idname in PAINT_RMB_ON_CLICK
            and kmi.ctrl != 1 and kmi.alt != 1)


def _add_rmb_items():
    # Added in reverse order of priority (see keymaps.AddonItems): the
    # parallel-view pan, added last, is checked before view3d.rotate.
    _addon_items.add("r2b.repeat_last_command", 'RIGHTMOUSE', 'CLICK')
    _addon_items.add("view3d.rotate", 'RIGHTMOUSE', 'CLICK_DRAG', ctrl=True, shift=True)
    _addon_items.add("view3d.zoom", 'RIGHTMOUSE', 'CLICK_DRAG', ctrl=True)
    _addon_items.add("view3d.move", 'RIGHTMOUSE', 'CLICK_DRAG', shift=True)
    _addon_items.add("view3d.rotate", 'RIGHTMOUSE', 'CLICK_DRAG')
    _addon_items.add("r2b.view_pan_parallel", 'RIGHTMOUSE', 'CLICK_DRAG')


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    _add_rmb_items()
    _muted.mute(bpy.context.window_manager.keyconfigs.active, MODELING_KEYMAPS,
                lambda kmi: kmi.type == 'RIGHTMOUSE')
    _paint_rmb.change(bpy.context.window_manager.keyconfigs.active, PAINT_KEYMAPS, _is_paint_rmb_press,
                      value='CLICK')
    keymaps.update_keyconfigs()


def unregister():
    _muted.restore()
    _paint_rmb.restore()
    _addon_items.remove()
    keymaps.update_keyconfigs()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
