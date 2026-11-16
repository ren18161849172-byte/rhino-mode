# SPDX-License-Identifier: GPL-3.0-or-later
"""The viewport title menu (MODE feature), as in Rhino.

Clicking a viewport's title ("Top Orthographic", ...) opens a menu to set
the view (Top, Front, ..., Perspective), the display mode (Wireframe,
Shaded, Ghosted, Rendered) and the CPlane, zoom extents, and maximize or
restore the viewport. A left click opens it after the double-click time, so
that a double-click still maximizes (see viewport_maximize.py); a right
click on the title opens it at once.
"""

import math

import bpy
from mathutils import Euler

from . import debug

VIEWS = (
    ('TOP', "Top", "Look down the world Z axis"),
    ('BOTTOM', "Bottom", "Look up the world Z axis"),
    ('FRONT', "Front", "Look along the world Y axis"),
    ('BACK', "Back", "Look back along the world Y axis"),
    ('RIGHT', "Right", "Look along the world -X axis"),
    ('LEFT', "Left", "Look along the world X axis"),
    ('PERSPECTIVE', "Perspective", "Blender's default perspective view"),
)
DISPLAYS = (
    ('WIREFRAME', "Wireframe", "Edges only"),
    ('SHADED', "Shaded", "Solid shading"),
    ('GHOSTED', "Ghosted", "See-through solid shading"),
    ('RENDERED', "Rendered", "The render engine's shading"),
)

# Blender's default perspective view direction.
PERSPECTIVE_ROTATION = Euler((math.radians(63.6), 0.0, math.radians(46.7))).to_quaternion()

_pending = None  # (window, area, region) pointers of a title click waiting for the double-click time.
last_drawn = None  # Pointer of the area the menu was last drawn for (for tests).


def set_view(context, view):
    """Set the view of the viewport in ``context``."""
    if view == 'PERSPECTIVE':
        rv3d = context.region_data
        rv3d.view_perspective = 'PERSP'
        rv3d.view_rotation = PERSPECTIVE_ROTATION
    else:
        bpy.ops.view3d.view_axis(type=view)


def display_of(space):
    shading = space.shading
    if shading.type == 'WIREFRAME':
        return 'WIREFRAME'
    if shading.type == 'SOLID':
        return 'GHOSTED' if shading.show_xray else 'SHADED'
    return 'RENDERED' if shading.type == 'RENDERED' else None


def set_display(space, display):
    shading = space.shading
    if display == 'WIREFRAME':
        shading.type = 'WIREFRAME'
        shading.show_xray_wireframe = True
    elif display in {'SHADED', 'GHOSTED'}:
        shading.type = 'SOLID'
        shading.show_xray = display == 'GHOSTED'
    else:
        shading.type = 'RENDERED'


class _InViewport:
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and context.region_data is not None


class R2B_OT_viewport_view(_InViewport, bpy.types.Operator):
    """Set the viewport's view"""

    bl_idname = "r2b.viewport_view"
    bl_label = "Set View"
    bl_options = {'REGISTER'}

    view: bpy.props.EnumProperty(name="View", items=VIEWS)

    def execute(self, context):
        set_view(context, self.view)
        return {'FINISHED'}


class R2B_OT_viewport_display(_InViewport, bpy.types.Operator):
    """Set the viewport's display mode"""

    bl_idname = "r2b.viewport_display"
    bl_label = "Set Display Mode"
    bl_options = {'REGISTER'}

    display: bpy.props.EnumProperty(name="Display", items=DISPLAYS)

    def execute(self, context):
        set_display(context.space_data, self.display)
        return {'FINISHED'}


class R2B_OT_viewport_maximize_toggle(_InViewport, bpy.types.Operator):
    """Maximize the viewport, or restore the four viewports"""

    bl_idname = "r2b.viewport_maximize_toggle"
    bl_label = "Maximize or Restore"
    bl_options = {'REGISTER'}

    def execute(self, context):
        from . import viewport_maximize

        viewport_maximize.toggle_maximize(context)
        return {'FINISHED'}


class R2B_MT_viewport_title(bpy.types.Menu):
    bl_idname = "R2B_MT_viewport_title"
    bl_label = "Viewport"

    def draw(self, context):
        global last_drawn
        from . import viewport_maximize

        area = context.area
        last_drawn = area.as_pointer() if area is not None else None
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        for view, name, _ in VIEWS:
            layout.operator(R2B_OT_viewport_view.bl_idname, text=name).view = view
        layout.separator()
        current = display_of(context.space_data) if area is not None and area.type == 'VIEW_3D' else None
        for display, name, _ in DISPLAYS:
            layout.operator(R2B_OT_viewport_display.bl_idname, text=name,
                            icon='RADIOBUT_ON' if display == current else 'RADIOBUT_OFF').display = display
        layout.separator()
        layout.operator("r2b.cplane", text="Set CPlane...")
        layout.operator("view3d.view_all", text="Zoom Extents").center = False
        maximized = context.window.screen.get(viewport_maximize.LAYOUT) == "maximized"
        layout.operator(R2B_OT_viewport_maximize_toggle.bl_idname, text="Restore" if maximized else "Maximize")


def open_menu(context):
    debug.log("  viewport menu: open")
    bpy.ops.wm.call_menu(name=R2B_MT_viewport_title.bl_idname)


def schedule(context):
    """Open the menu for the viewport of ``context`` unless a double-click
    follows (``cancel``)."""
    global _pending
    _pending = (context.window.as_pointer(), context.area.as_pointer(), context.region.as_pointer())
    if bpy.app.timers.is_registered(_open_pending):
        bpy.app.timers.unregister(_open_pending)
    delay = context.preferences.inputs.mouse_double_click_time / 1000 + 0.05
    bpy.app.timers.register(_open_pending, first_interval=delay)


def cancel():
    global _pending
    _pending = None


def _open_pending():
    global _pending
    pending, _pending = _pending, None
    if pending is None:
        return None
    window_ptr, area_ptr, region_ptr = pending
    for window in bpy.context.window_manager.windows:
        if window.as_pointer() != window_ptr:
            continue
        for area in window.screen.areas:
            if area.as_pointer() != area_ptr:
                continue
            region = next((r for r in area.regions if r.as_pointer() == region_ptr), None)
            if region is not None:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    open_menu(bpy.context)
    return None


_classes = (R2B_OT_viewport_view, R2B_OT_viewport_display, R2B_OT_viewport_maximize_toggle, R2B_MT_viewport_title)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    cancel()
    if bpy.app.timers.is_registered(_open_pending):
        bpy.app.timers.unregister(_open_pending)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
