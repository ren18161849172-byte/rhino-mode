# SPDX-License-Identifier: GPL-3.0-or-later
"""Debug log for interaction problems (CORE, off by default).

When "Debug Log" is enabled in the add-on preferences, every mouse button
event in a window is written to ``r2b_debug.log`` in the system temp folder,
with the area and region under the mouse and, in 3D viewports, the result of
the title hit test. R2B's operators add a line when they act on an event.
"""

import os
import tempfile
import time

import bpy
from bpy.app.handlers import persistent

LOG_PATH = os.path.join(tempfile.gettempdir(), "r2b_debug.log")

_MOUSE_BUTTONS = {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'}

# Each start() bumps this; older loggers stop at their next event, so there
# is never more than one logger writing.
_generation = 0


def enabled():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon is not None and addon.preferences.debug_log


def log(message):
    if not enabled():
        return
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {message}\n")


def _region_under(window, x, y):
    """(area, region) under window coordinates; overlapping regions such as
    headers and toolbars win over the main region they cover."""
    for area in window.screen.areas:
        if not (area.x <= x < area.x + area.width and area.y <= y < area.y + area.height):
            continue
        hits = [r for r in area.regions
                if r.width > 1 and r.height > 1 and r.x <= x < r.x + r.width and r.y <= y < r.y + r.height]
        hits.sort(key=lambda r: r.type == 'WINDOW')
        return area, (hits[0] if hits else None)
    return None, None


def describe_event(window, event):
    from . import viewport_maximize

    x, y = event.mouse_x, event.mouse_y
    area, region = _region_under(window, x, y)
    text = f"{event.type} {event.value} at ({x}, {y})"
    if event.shift or event.ctrl or event.alt:
        text += " mods=" + "+".join(m for m in ("shift", "ctrl", "alt") if getattr(event, m))
    if area is None:
        return text + " outside areas"
    text += f" area={area.type} region={region.type if region else None}"
    text += f" layout={window.screen.get(viewport_maximize.LAYOUT)}"
    main = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if area.type == 'VIEW_3D' and main is not None:
        # Headers overlap the main region and Blender may hand clicks on
        # their empty parts to it, so report the hit tests for the main region.
        local = (x - main.x, y - main.y)
        text += (f" main_local={local} visible_rect={viewport_maximize.visible_rect(area, main)}"
                 f" unit={viewport_maximize.widget_unit()}"
                 f" title_hit={viewport_maximize.title_hit(area, main, *local)}"
                 f" header_hit={viewport_maximize.header_hit(area, main, *local)}")
    return text


class R2B_OT_debug_event_log(bpy.types.Operator):
    """Log mouse button events while the debug log is enabled"""

    bl_idname = "r2b.debug_event_log"
    bl_label = "Rhino Mode Debug Event Log"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        self._generation = _generation
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if not enabled() or self._generation != _generation:
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type in _MOUSE_BUTTONS:
            try:
                log(describe_event(context.window, event))
            except Exception as ex:  # Never break input because of logging.
                log(f"logging failed: {ex!r}")
        return {'PASS_THROUGH'}


# Plain keys that type into the command line from a 3D viewport.
_TEXT_KEYS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") | {"ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN",
                                                "EIGHT", "NINE", "SPACE", "COMMA", "PERIOD", "MINUS"}


def foreign_typing_keys():
    """Other add-ons' shortcuts on plain letters and digits in the keymaps R2B
    types from (tools, modes, 3D View): Blender handles them first, so when
    their poll passes (e.g. Geo-Scatter's physics brushes) they take the key
    instead of the command line. Only reported, not switched off: add-ons
    switch such keys on and off for their own tools."""
    from . import command_line

    keyconfigs = bpy.context.window_manager.keyconfigs
    names = set(command_line._typing_keymaps(keyconfigs.active or keyconfigs.default))
    found = []
    for keymap in keyconfigs.addon.keymaps:
        if keymap.name not in names:
            continue
        for item in keymap.keymap_items:
            if item.idname.startswith("r2b.") or not item.active or item.type not in _TEXT_KEYS:
                continue
            if item.ctrl == 1 or item.alt == 1 or item.oskey == 1:
                continue
            found.append(f"{keymap.name}: {'Shift+' if item.shift == 1 else ''}{item.type} -> {item.idname}")
    return found


def _environment():
    prefs = bpy.context.preferences
    keyconfig = bpy.context.window_manager.keyconfigs.active
    return (f"Blender {bpy.app.version_string}, ui_scale={prefs.system.ui_scale},"
            f" region_overlap={prefs.system.use_region_overlap},"
            f" double_click_time={prefs.inputs.mouse_double_click_time} ms,"
            f" keyconfig={keyconfig.name if keyconfig else None}")


def start():
    global _generation
    windows = bpy.context.window_manager.windows
    if not windows:
        return
    _generation += 1
    log("=== debug log started: " + _environment())
    conflicts = foreign_typing_keys()
    if conflicts:
        log("other add-ons' letter keys where Rhino Mode types (they win when their poll passes): " + "; ".join(conflicts))
    with bpy.context.temp_override(window=windows[0]):
        bpy.ops.r2b.debug_event_log('INVOKE_DEFAULT')


def _start_if_enabled():
    if enabled():
        start()
    return None


@persistent
def _on_load_post(*_args):
    # Loading a file drops window handlers, including the logger.
    bpy.app.timers.register(_start_if_enabled, first_interval=0.0)


def register():
    bpy.utils.register_class(R2B_OT_debug_event_log)
    bpy.app.handlers.load_post.append(_on_load_post)
    bpy.app.timers.register(_start_if_enabled, first_interval=0.5)


def unregister():
    global _generation
    _generation += 1  # Stops a running logger.
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if bpy.app.timers.is_registered(_start_if_enabled):
        bpy.app.timers.unregister(_start_if_enabled)
    bpy.utils.unregister_class(R2B_OT_debug_event_log)
