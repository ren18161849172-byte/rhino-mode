# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino four-view workspace (MODE feature).

Four real 3D View areas (Top, Perspective, Front, Right), each with its own
shading, instead of Blender's Quad View which shares one shading setting.
The layout lives in ``assets/rhino_workspace.blend`` (built by
``tools/build_workspace.py``) and is appended when Rhino Mode turns on.

Workspaces are stored per .blend file, so while the mode is on the workspace
is added again to every newly loaded file that does not have it yet. Turning
the mode off switches each window back to its previous workspace and deletes
R2B's workspace from the current file.

A file saved while the mode is on keeps the workspace, but its command lines
are saved as Python consoles: where R2B is not enabled, Blender would fail
to load the "rhino" console language (ModuleNotFoundError _console_rhino)
whenever the file is opened. They become command lines again when the file
is opened with the mode on.
"""

import os

import bpy
from bpy.app.handlers import persistent

from . import command_line

WORKSPACE_NAME = "Rhino"
MARKER = "r2b_workspace"
ASSET_PATH = os.path.join(os.path.dirname(__file__), "assets", "rhino_workspace.blend")

# Name of the workspace each window showed before, by window index.
_previous_workspaces = {}

# Scenes with more faces than this (all evaluated meshes and instances) get
# Solid instead of Wireframe viewports: in a scene of 4 M faces (mostly
# scattered plants) the four views took 690 ms to redraw with three
# wireframes and 76 ms all Solid.
HEAVY_FACES = 2_000_000
# Workspaces already looked at in this file (not stored: a property write
# would change the file).
_weighed = set()

# Language of the command line consoles, and the one they have in saved files.
COMMAND_LINE_LANGUAGE = command_line.LANGUAGE
SAVED_LANGUAGE = "python"
# Consoles switched to SAVED_LANGUAGE while a file is being written.
_saving = []


def _windows():
    return list(bpy.context.window_manager.windows)


def rhino_workspaces():
    return [ws for ws in bpy.data.workspaces if ws.get(MARKER)]


# Layouts whose Layers panel was switched to showing objects (once: after
# that the header toggle is the user's).
OBJECTS_MARKER = "r2b_layer_objects"
# Layouts whose Layers panel got Blender's render column (once).
RENDER_COLUMN_MARKER = "r2b_layer_render_column"


def show_objects_in_layers(workspace):
    """Show objects under their layers in the workspace's Outliner (Blender's
    way; Rhino's Layers panel lists layers only, the header toggle switches),
    once per layout: files saved before kept layers only."""
    for screen in workspace.screens:
        if screen.get(OBJECTS_MARKER):
            continue
        for area in screen.areas:
            if area.type == 'OUTLINER':
                area.spaces.active.use_filter_object = True
        screen[OBJECTS_MARKER] = 1
    show_render_column(workspace)


def show_render_column(workspace):
    """Blender's "Disable in Renders" column (the camera) in the Layers
    panel: Rhino renders what is on, but a scene hides helpers and light
    blockers from the camera. Once per layout; the exclude checkboxes have a
    header toggle (layers.py)."""
    for screen in workspace.screens:
        if screen.get(RENDER_COLUMN_MARKER):
            continue
        for area in screen.areas:
            if area.type == 'OUTLINER':
                area.spaces.active.show_restrict_column_render = True
        screen[RENDER_COLUMN_MARKER] = 1


def _consoles(workspace):
    return [area.spaces.active for screen in workspace.screens for area in screen.areas if area.type == 'CONSOLE']


def own_command_lines(workspace):
    """Make the workspace's consoles R2B command lines again (saved files
    have Python consoles there)."""
    for space in _consoles(workspace):
        if space.language == SAVED_LANGUAGE:
            space.language = COMMAND_LINE_LANGUAGE


def _ensure_workspace(window):
    existing = rhino_workspaces()
    if existing:
        show_objects_in_layers(existing[0])
        own_command_lines(existing[0])
        return existing[0], False
    with bpy.context.temp_override(window=window, screen=window.screen):
        bpy.ops.workspace.append_activate(idname=WORKSPACE_NAME, filepath=ASSET_PATH)
    appended = rhino_workspaces()
    if not appended:
        raise RuntimeError(f"could not append the {WORKSPACE_NAME} workspace from {ASSET_PATH}")
    for screen in appended[0].screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                # Objects show their layer's color, as in Rhino (see layers.py).
                shading = area.spaces.active.shading
                shading.color_type = 'OBJECT'
                shading.wireframe_color_type = 'OBJECT'
    show_objects_in_layers(appended[0])
    return appended[0], True


def scene_faces(context, limit=None):
    """Faces of all visible evaluated meshes and their instances (counting
    stops past ``limit``)."""
    depsgraph = context.evaluated_depsgraph_get()
    total = 0
    for instance in depsgraph.object_instances:
        if instance.object.type == 'MESH':
            total += len(instance.object.data.polygons)
            if limit is not None and total > limit:
                break
    return total


def lighten_heavy_scene(window, workspace):
    """Solid instead of Wireframe viewports in the workspace when the scene
    is heavy (once per file and workspace), with a note in the command line."""
    from . import prefs

    if workspace.as_pointer() in _weighed or not prefs.get().solid_heavy_scenes:
        return False
    _weighed.add(workspace.as_pointer())
    if scene_faces(bpy.context, HEAVY_FACES) <= HEAVY_FACES:
        return False
    changed = [area.spaces.active for screen in workspace.screens for area in screen.areas
               if area.type == 'VIEW_3D' and area.spaces.active.shading.type == 'WIREFRAME']
    for space in changed:
        space.shading.type = 'SOLID'
    if changed:
        command_line.echo(window, f"This scene has over {HEAVY_FACES // 1_000_000} million faces: the wireframe "
                          "viewports show Solid, since wireframes of that much make every redraw slow. Type "
                          "Wireframe in a viewport to switch it back (or turn this off in Rhino Mode's preferences).",
                          'INFO')
    return bool(changed)


def _activate(only_if_new=False):
    windows = _windows()
    if not windows:
        return  # Background mode: there is no UI to change.
    workspace, appended = _ensure_workspace(windows[0])
    lighten_heavy_scene(windows[0], workspace)
    if only_if_new and not appended:
        # A file saved with the Rhino workspace keeps the workspace it was
        # saved with; Layout is the way back when the mode turns off.
        _previous_workspaces.clear()
        return
    for index, window in enumerate(windows):
        if window.workspace.get(MARKER):
            continue
        _previous_workspaces[index] = window.workspace.name
        # append_activate already switches the first window; switching it a
        # second time before the first switch is applied corrupts the
        # screen's user count.
        if not (appended and index == 0):
            window.workspace = workspace


def _fallback_workspace(index):
    candidates = [_previous_workspaces.get(index), "Layout"]
    for name in candidates:
        workspace = bpy.data.workspaces.get(name) if name else None
        if workspace is not None and not workspace.get(MARKER):
            return workspace
    return next((ws for ws in bpy.data.workspaces if not ws.get(MARKER)), None)


def _restore_maximized(window):
    # A maximized viewport lives on a temporary screen; go back to the
    # four-view screen before leaving the workspace.
    screen = window.screen
    if not screen.show_fullscreen:
        return
    area = next((a for a in screen.areas if a.type == 'VIEW_3D'), screen.areas[0])
    with bpy.context.temp_override(window=window, screen=screen, area=area):
        bpy.ops.screen.back_to_previous()


def _deactivate():
    windows = _windows()
    if not windows or not rhino_workspaces():
        _previous_workspaces.clear()
        return
    for index, window in enumerate(windows):
        if window.workspace.get(MARKER):
            fallback = _fallback_workspace(index)
            if fallback is None:
                return  # Nothing to switch to, keep the Rhino workspace.
            _restore_maximized(window)
            window.workspace = fallback
    _previous_workspaces.clear()
    # Blender applies workspace switches on the next UI update; delete only
    # once no window shows the Rhino workspace any more.
    global _delete_attempts
    _delete_attempts = 0
    if not bpy.app.timers.is_registered(_delete_unused_workspaces):
        bpy.app.timers.register(_delete_unused_workspaces, first_interval=0.0)


_DELETE_RETRIES = 20
_delete_attempts = 0


def _delete_unused_workspaces():
    global _delete_attempts
    windows = _windows()
    in_use = {window.workspace.as_pointer() for window in windows}
    unused = [ws for ws in rhino_workspaces() if ws.as_pointer() not in in_use]
    pending = len(unused) < len(rhino_workspaces())
    # Not bpy.ops.workspace.delete: it ignores a workspace context override
    # and always deletes the window's active workspace.
    if unused:
        bpy.data.batch_remove(unused)
    _delete_attempts += 1
    if pending and _delete_attempts < _DELETE_RETRIES:
        return 0.1
    return None


def _activate_after_load():
    _activate(only_if_new=True)
    return None


@persistent
def _on_load_post(*_args):
    _weighed.clear()  # Another file.
    # UI operators are not safe inside the load handler itself.
    bpy.app.timers.register(_activate_after_load, first_interval=0.0)


@persistent
def _on_save_pre(*_args):
    _saving.clear()
    for workspace in rhino_workspaces():
        for space in _consoles(workspace):
            if space.language == COMMAND_LINE_LANGUAGE:
                space.language = SAVED_LANGUAGE
                _saving.append(space)


@persistent
def _on_save_post(*_args):
    for space in _saving:
        space.language = COMMAND_LINE_LANGUAGE
    _saving.clear()


# save_post_fail: the file could not be written (save_post is not called
# then); the command lines must come back all the same.
_HANDLERS = (("load_post", _on_load_post), ("save_pre", _on_save_pre), ("save_post", _on_save_post),
             ("save_post_fail", _on_save_post))


def register():
    for name, function in _HANDLERS:
        if hasattr(bpy.app.handlers, name):
            getattr(bpy.app.handlers, name).append(function)
    _activate()


def unregister():
    for name, function in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name, ())
        if function in handlers:
            handlers.remove(function)
    if bpy.app.timers.is_registered(_activate_after_load):
        bpy.app.timers.unregister(_activate_after_load)
    _deactivate()
