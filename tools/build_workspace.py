# SPDX-License-Identifier: GPL-3.0-or-later
"""Build src/r2b/assets/rhino_workspace.blend: the Rhino four-view workspace.

Needs the Blender UI (area splitting needs a window), and should be run with
the oldest supported Blender (4.2) so every newer version can read the file:

    blender --factory-startup --python tools/build_workspace.py -- OUTPUT.blend

The workspace has two layouts (screens), told apart by the "r2b_layout"
ID property:

"four_view", Rhino's default four viewports under the command line (a
Console with R2B's "rhino" language), with Blender's Outliner and Properties
kept on the right (the Outliner set up as the Layers panel), and Blender's
timeline as a strip of its header at the bottom (play, frames; drag it up
for the keys; Space over it plays, as in Blender):

    +---------------------------+-----------+
    | Command line              | Outliner  |
    +-------------+-------------+           |
    | Top         | Perspective |           |
    | wireframe   | solid       +-----------+
    +-------------+-------------+ Properties|
    | Front       | Right       |           |
    | wireframe   | wireframe   |           |
    +-------------+-------------+           |
    | Timeline                  |           |
    +---------------------------+-----------+

"maximized", one viewport under the command line with the same panels,
which a viewport switches to when its title or header is double-clicked:

    +---------------------------+-----------+
    | Command line              | Outliner  |
    +---------------------------+           |
    |                           +-----------+
    | (maximized viewport)      | Properties|
    |                           |           |
    +---------------------------+           |
    | Timeline                  |           |
    +---------------------------+-----------+

The four-view layout must come first: Blender shows a workspace's first
layout when it is appended.
"""

import os
import sys
import traceback
import types

import bpy

WORKSPACE_NAME = "Rhino"
# Lets R2B tell its own workspace apart from one the user named "Rhino".
MARKER = "r2b_workspace"
LAYOUT = "r2b_layout"

# (view axis, shading) per quadrant, keyed by (is_top_row, is_left_column).
QUADRANTS = {
    (True, True): ('TOP', 'WIREFRAME'),
    (True, False): (None, 'SOLID'),  # perspective
    (False, True): ('FRONT', 'WIREFRAME'),
    (False, False): ('RIGHT', 'WIREFRAME'),
}


def window_region(area):
    return next(region for region in area.regions if region.type == 'WINDOW')


def split(window, screen, area, direction, factor=0.5):
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=window_region(area)):
        bpy.ops.screen.area_split(direction=direction, factor=factor)


def viewports(screen):
    return [a for a in screen.areas if a.type == 'VIEW_3D']


def close_timeline(window, screen):
    # Layout's timeline is replaced by one just as tall as its header.
    for area in list(screen.areas):
        if area.type == 'DOPESHEET_EDITOR':
            with bpy.context.temp_override(window=window, screen=screen, area=area):
                bpy.ops.screen.area_close()


# Height of the timeline strip: its header (the user can drag it taller).
TIMELINE_HEIGHT = 27


def split_timeline(window, screen):
    area = max(viewports(screen), key=lambda a: a.width * a.height)
    height = TIMELINE_HEIGHT * bpy.context.preferences.system.ui_scale
    split(window, screen, area, 'HORIZONTAL', factor=height / area.height)


def make_timeline(window, screen):
    area = min(viewports(screen), key=lambda a: a.y)
    area.type = 'DOPESHEET_EDITOR'
    area.ui_type = 'TIMELINE'


# Height of the command line in console lines (about 4 visible lines).
COMMAND_LINE_LINES = 4.6


def split_command_line(window, screen):
    area = max(viewports(screen), key=lambda a: a.width * a.height)
    line_height = 18 * bpy.context.preferences.system.ui_scale
    split(window, screen, area, 'HORIZONTAL', factor=1 - COMMAND_LINE_LINES * line_height / area.height)


def make_command_line(window, screen):
    # The upper of the two viewports becomes the Rhino command line: a Console
    # whose "rhino" language is provided by R2B (see command_line.py).
    area = max(viewports(screen), key=lambda a: a.y)
    area.type = 'CONSOLE'
    space = area.spaces.active
    space.language = "rhino"
    space.show_region_header = False
    print(f"command line x={area.x} y={area.y} {area.width}x{area.height}")


_four_view_screen = []


def add_maximized_layout(window, screen):
    # Duplicate the current layout (one viewport and the panels) as a second
    # layout of the workspace; Blender switches the window to the copy.
    _four_view_screen.append(screen)
    with bpy.context.temp_override(window=window, screen=screen):
        bpy.ops.screen.new()


def back_to_four_view_layout(window, screen):
    screen.name = "Rhino Maximized"
    screen[LAYOUT] = "maximized"
    window.screen = _four_view_screen[0]


def split_left_right(window, screen):
    split(window, screen, max(viewports(screen), key=lambda a: a.width * a.height), 'VERTICAL')


def split_left(window, screen):
    split(window, screen, min(viewports(screen), key=lambda a: a.x), 'HORIZONTAL')


def split_right(window, screen):
    split(window, screen, max(viewports(screen), key=lambda a: a.x), 'HORIZONTAL')


def configure_layers_panel(screen):
    # The Outliner is the Rhino Layers panel: layers with their objects (as
    # Blender shows them; a header toggle lists layers only), with the eye
    # (layer on/off) and selectable (lock) columns.
    for area in screen.areas:
        if area.type != 'OUTLINER':
            continue
        space = area.spaces.active
        space.display_mode = 'VIEW_LAYER'
        space.use_filter_object = True
        space.show_restrict_column_hide = True
        space.show_restrict_column_select = True
        # Blender's render column (hide helpers from the camera); the exclude
        # checkboxes have a header toggle.
        space.show_restrict_column_render = True
        for column in ("enable", "viewport", "holdout", "indirect_only"):
            setattr(space, "show_restrict_column_" + column, False)


def configure_and_save(window, screen):
    views = viewports(screen)
    if len(views) != 4:
        raise RuntimeError(f"expected 4 viewports, got {len(views)}")
    mid_x = sum(a.x + a.width / 2 for a in views) / 4
    mid_y = sum(a.y + a.height / 2 for a in views) / 4

    for area in views:
        is_top = area.y + area.height / 2 > mid_y
        is_left = area.x + area.width / 2 < mid_x
        axis, shading = QUADRANTS[(is_top, is_left)]
        space = area.spaces.active
        space.shading.type = shading
        rv3d = space.region_3d
        if axis is not None:
            with bpy.context.temp_override(window=window, screen=screen, area=area, region=window_region(area)):
                bpy.ops.view3d.view_axis(type=axis)
            rv3d.view_perspective = 'ORTHO'
        else:
            rv3d.view_perspective = 'PERSP'
        print(f"{axis or 'PERSP':5s} {shading:9s} x={area.x} y={area.y} {area.width}x{area.height}")

    screen.name = "Rhino"
    screen[LAYOUT] = "four_view"
    for layout_screen in window.workspace.screens:
        configure_layers_panel(layout_screen)
    workspace = window.workspace
    workspace.name = WORKSPACE_NAME
    workspace[MARKER] = True
    layouts = [s.get(LAYOUT) for s in workspace.screens]
    if layouts != ["four_view", "maximized"]:
        raise RuntimeError(f"unexpected layouts {layouts}")
    output = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
    os.makedirs(os.path.dirname(output), exist_ok=True)
    bpy.data.libraries.write(output, {workspace}, compress=True)
    for layout_screen in workspace.screens:
        timeline = next(a for a in layout_screen.areas if a.type == 'DOPESHEET_EDITOR')
        print(f"{layout_screen.name}: timeline {timeline.width}x{timeline.height}")
    print("written:", output)


# R2B is not loaded while building: give the console's "rhino" language a
# placeholder so that drawing the command line does not raise errors.
_placeholder = types.ModuleType("_console_rhino")
_placeholder.banner = _placeholder.execute = _placeholder.autocomplete = lambda *args: {'FINISHED'}
sys.modules["console_rhino"] = sys.modules["_console_rhino"] = _placeholder


# Blender only updates area sizes when the screen is refreshed, so each step
# runs in its own timer tick.
STEPS = [close_timeline, split_timeline, make_timeline, split_command_line, make_command_line,
         add_maximized_layout, back_to_four_view_layout,
         split_left_right, split_left, split_right, configure_and_save]


def run_next_step():
    window = bpy.context.window_manager.windows[0]
    try:
        STEPS.pop(0)(window, window.screen)
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)
    if STEPS:
        return 0.3
    sys.stdout.flush()
    os._exit(0)


# Wait until the window is fully set up before touching the screen.
bpy.app.timers.register(run_next_step, first_interval=1.0)
