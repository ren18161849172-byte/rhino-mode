# SPDX-License-Identifier: GPL-3.0-or-later
"""Double-click a viewport's title or header to maximize or restore it
(MODE feature).

* Title: Blender's text info overlay ("Top Orthographic", ...) in the
  top-left corner of each 3D viewport, where Rhino shows the viewport name.
  A single click on the title never changes the selection (as in Rhino) and
  opens the viewport menu (viewport_menu.py) once the double-click time has
  passed without a second click; a right click opens it at once.
* Header: the empty part of the 3D viewport's header (the bar with the View,
  Select, Add and Object menus). Clicks on its buttons are handled by the
  buttons first. The header overlaps the viewport, and Blender gives clicks
  on most of its empty part to the viewport underneath, so those are
  recognized in the viewport too (and single clicks there are swallowed so
  they do not deselect everything).

In the Rhino workspace, maximizing switches to the workspace's "maximized"
layout (one viewport, with the Outliner and Properties still on the right as
in Rhino) after copying the viewport's view and display settings into it;
restoring copies them back, so navigation done while maximized is kept.
Elsewhere it falls back to Blender's own maximize area (Ctrl+Space), which
hides the other areas of the window.
"""

import bpy

from . import cplane, debug, keymaps, viewport_menu, workspace

# Size of the clickable title box, in Blender widget units (20 px at 1x).
# Wide enough for the longest view name, "Right Orthographic".
TITLE_WIDTH_UNITS = 9.0
TITLE_HEIGHT_UNITS = 1.3

_addon_items = keymaps.AddonItems()


def widget_unit():
    # Same formula as Blender's U.widget_unit.
    system = bpy.context.preferences.system
    return round(18 * system.ui_scale) + 2 * system.pixel_size


def visible_rect(area, region):
    """(xmin, ymax) of the part of ``region`` not covered by overlapping
    toolbars and headers, in region coordinates (like ED_region_visible_rect)."""
    xmin, ymax = 0, region.height
    for other in area.regions:
        if other == region or other.type == 'HUD' or other.width <= 1 or other.height <= 1:
            continue
        overlaps = (region.x <= other.x < region.x + region.width
                    and region.y <= other.y < region.y + region.height)
        if not overlaps:
            continue
        if other.alignment == 'LEFT':
            xmin = max(xmin, other.x + other.width - region.x)
        elif other.alignment == 'TOP':
            ymax = min(ymax, other.y - region.y)
    return xmin, ymax


def title_hit(area, region, x, y):
    """Whether region coordinates (x, y) are on the viewport title."""
    space = area.spaces.active
    if not (space.overlay.show_overlays and space.overlay.show_text):
        return False  # The title is not drawn.
    xmin, ymax = visible_rect(area, region)
    unit = widget_unit()
    return (xmin <= x <= xmin + TITLE_WIDTH_UNITS * unit
            and ymax - TITLE_HEIGHT_UNITS * unit <= y <= ymax)


def header_hit(area, region, x, y):
    """Whether region coordinates (x, y) of the viewport's main region are
    under the area's header, which overlaps the top of the main region."""
    window_x, window_y = region.x + x, region.y + y
    return any(
        header.type == 'HEADER' and header.width > 1 and header.height > 1
        and header.x <= window_x < header.x + header.width
        and header.y <= window_y < header.y + header.height
        for header in area.regions
    )


def _hits(context, event):
    """(over title, over header) for an event in a 3D viewport's main region."""
    area, region = context.area, context.region
    if area is None or region is None or area.type != 'VIEW_3D' or region.type != 'WINDOW':
        return False, False
    x, y = event.mouse_region_x, event.mouse_region_y
    return title_hit(area, region, x, y), header_hit(area, region, x, y)


# ID property telling the Rhino workspace's layouts apart (see
# tools/build_workspace.py), and the index of the four-view area that the
# maximized layout currently shows.
LAYOUT = "r2b_layout"
SOURCE_AREA = "r2b_source_area"

_REGION_3D_SKIP = {"view_matrix", "window_matrix", "perspective_matrix",
                   "is_perspective", "is_orthographic_side_view"}


def _copy_properties(source, target, skip=()):
    """Copy the writable, non-pointer RNA properties of ``source``."""
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if (name == "rna_type" or name in skip or prop.is_readonly
                or prop.type in {'POINTER', 'COLLECTION'}):
            continue
        try:
            setattr(target, name, getattr(source, name))
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass  # Not settable in this state (e.g. depends on another value).


def copy_view(source, target):
    """Copy view and display settings from one SpaceView3D to another.

    ``target`` is usually in a layout that is not shown yet. Region
    visibility (show_region_*) is not copied: changing it makes Blender
    initialize the area, which crashes for an area that is not shown.
    """
    # Properties shared by all editors (e.g. show_locked_time) do not apply
    # to the 3D View; Blender logs an error instead of raising when set.
    skip = {prop.identifier for prop in bpy.types.Space.bl_rna.properties}
    skip |= {prop.identifier for prop in source.bl_rna.properties if prop.identifier.startswith("show_region")}
    _copy_properties(source, target, skip=skip)
    target.camera = source.camera
    target.lock_object = source.lock_object
    shading_source, shading_target = source.shading, target.shading
    shading_target.type = shading_source.type
    # studio_light only has valid values for some shading types and lights;
    # reading it otherwise makes Blender log a warning.
    _copy_properties(shading_source, shading_target, skip={"studio_light"})
    if (shading_source.type == 'MATERIAL'
            or (shading_source.type == 'SOLID' and shading_source.light in {'STUDIO', 'MATCAP'})):
        shading_target.studio_light = shading_source.studio_light
    _copy_properties(source.overlay, target.overlay)
    rv3d_source, rv3d_target = source.region_3d, target.region_3d
    _copy_properties(rv3d_source, rv3d_target, skip=_REGION_3D_SKIP)
    rv3d_target.view_perspective = rv3d_source.view_perspective
    rv3d_target.view_rotation = rv3d_source.view_rotation
    rv3d_target.view_location = rv3d_source.view_location
    rv3d_target.view_distance = rv3d_source.view_distance
    if rv3d_source.is_orthographic_side_view:
        # Keeps it a named view ("Top Orthographic"), not "User Orthographic".
        rv3d_target.is_orthographic_side_view = True


def _first_area(screen, area_type):
    return next((a for a in screen.areas if a.type == area_type), None)


def _copy_panel_state(source_screen, target_screen):
    source = _first_area(source_screen, 'PROPERTIES')
    target = _first_area(target_screen, 'PROPERTIES')
    if source and target:
        try:
            target.spaces.active.context = source.spaces.active.context
        except (TypeError, ValueError):
            pass  # The tab does not exist for the active object.


def _rhino_layouts(window):
    if not window.workspace.get(workspace.MARKER):
        return None
    layouts = {screen.get(LAYOUT): screen for screen in window.workspace.screens}
    if "four_view" in layouts and "maximized" in layouts:
        return layouts
    return None


def toggle_maximize(context):
    window, area = context.window, context.area
    layouts = _rhino_layouts(window)
    current = window.screen.get(LAYOUT)
    if layouts is None or current not in {"four_view", "maximized"}:
        debug.log(f"  toggle_maximize: Blender maximize area (layout={current})")
        bpy.ops.screen.screen_full_area(use_hide_panels=False)
        return
    debug.log(f"  toggle_maximize: switch from layout {current}")

    if current == "four_view":
        target_screen = layouts["maximized"]
        target_area = _first_area(target_screen, 'VIEW_3D')
        source_index = next(i for i, a in enumerate(window.screen.areas)
                            if a.as_pointer() == area.as_pointer())
        copy_view(area.spaces.active, target_area.spaces.active)
        cplane.copy_custom(window.screen, area, target_screen, target_area)
        target_screen[SOURCE_AREA] = source_index
    else:
        target_screen = layouts["four_view"]
        index = window.screen.get(SOURCE_AREA, -1)
        areas = target_screen.areas
        target_area = (areas[index] if 0 <= index < len(areas) and areas[index].type == 'VIEW_3D'
                       else _first_area(target_screen, 'VIEW_3D'))
        copy_view(area.spaces.active, target_area.spaces.active)
        cplane.copy_custom(window.screen, area, target_screen, target_area)

    from . import command_line

    console = command_line.find_console(window.screen)
    line = command_line.current_line(console) if console else None
    _copy_panel_state(window.screen, target_screen)
    window.screen = target_screen
    command_line.schedule_refresh(window, line)


class R2B_OT_viewport_title_click(bpy.types.Operator):
    """Clicking a viewport title or header leaves the selection unchanged"""

    bl_idname = "r2b.viewport_title_click"
    bl_label = "Viewport Title Click"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        over_title, over_header = _hits(context, event)
        swallow = over_title or over_header
        debug.log(f"  title_click: over_title={over_title} over_header={over_header}"
                  f" -> {'swallowed' if swallow else 'pass through'}")
        if over_title:
            viewport_menu.schedule(context)
        return {'FINISHED'} if swallow else {'PASS_THROUGH'}


class R2B_OT_viewport_title_menu(bpy.types.Operator):
    """Open the viewport menu by right-clicking the viewport's title"""

    bl_idname = "r2b.viewport_title_menu"
    bl_label = "Viewport Menu"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        over_title, _ = _hits(context, event)
        if not over_title:
            return {'PASS_THROUGH'}  # RMB click elsewhere: repeat the last command.
        viewport_menu.open_menu(context)
        return {'FINISHED'}


class R2B_OT_viewport_title_maximize(bpy.types.Operator):
    """Maximize or restore the viewport by double-clicking its title or header"""

    bl_idname = "r2b.viewport_title_maximize"
    bl_label = "Maximize Viewport"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        over_title, over_header = _hits(context, event)
        debug.log(f"  title_maximize: over_title={over_title} over_header={over_header}"
                  f" at region ({event.mouse_region_x}, {event.mouse_region_y})")
        if not (over_title or over_header):
            return {'PASS_THROUGH'}
        viewport_menu.cancel()  # The first click's menu.
        toggle_maximize(context)
        return {'FINISHED'}


class R2B_OT_viewport_header_maximize(bpy.types.Operator):
    """Maximize or restore the viewport by double-clicking its header"""

    bl_idname = "r2b.viewport_header_maximize"
    bl_label = "Maximize Viewport"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        # Header keymaps are shared by all editors; other editors' headers
        # fail the poll and keep Blender's behavior.
        area, region = context.area, context.region
        return (area is not None and area.type == 'VIEW_3D'
                and region is not None and region.type == 'HEADER')

    def invoke(self, context, event):
        debug.log("  header_maximize: invoked")
        toggle_maximize(context)
        return {'FINISHED'}


_classes = (R2B_OT_viewport_title_click, R2B_OT_viewport_title_menu, R2B_OT_viewport_title_maximize,
            R2B_OT_viewport_header_maximize)

def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    _addon_items.add("r2b.viewport_title_menu", 'RIGHTMOUSE', 'CLICK')
    _addon_items.add("r2b.viewport_title_click", 'LEFTMOUSE', 'CLICK')
    _addon_items.add("r2b.viewport_title_maximize", 'LEFTMOUSE', 'DOUBLE_CLICK')
    # Blender handles events in every editor's header with this keymap.
    _addon_items.add("r2b.viewport_header_maximize", 'LEFTMOUSE', 'DOUBLE_CLICK',
                     keymap="Region Context Menu", space_type='EMPTY')


def unregister():
    _addon_items.remove()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
