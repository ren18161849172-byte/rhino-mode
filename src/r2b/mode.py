# SPDX-License-Identifier: GPL-3.0-or-later
"""MODE layer: everything Rhino-specific, registered all-or-nothing.

Each feature lives in its own module with ``register()`` / ``unregister()``
and is listed in ``_feature_modules`` below, e.g. navigation keymap,
four-view workspace, command line, layer panel.
"""

import traceback

import bpy

from . import keymaps
from . import (analysis, array, command_line, commands, cplane_tools, curve_edit, curve_offset, curves, curves_more,
               deform, extrude, getpoint, gumball, layers, navigation, pushpull, select_tools, selection, settings,
               solid_tools, solids, subobject, surfaces, transform, transform_more, viewport_maximize, viewport_menu,
               workspace)

_feature_modules = (settings, navigation, selection, subobject, workspace, viewport_menu, viewport_maximize, commands,
                    command_line, layers, getpoint, curves, curves_more, curve_edit, curve_offset, extrude, surfaces,
                    pushpull, solid_tools, solids, transform, transform_more, deform, array, select_tools, analysis,
                    cplane_tools, gumball)

_registered = []
_enabled = False

# How often to check that the muted shortcuts are still muted (the user may
# switch keymap presets while the mode is on).
KEYCONFIG_CHECK_INTERVAL = 1.0


def _check_keyconfig():
    if not _enabled:
        return None
    try:
        keymaps.follow_active_keyconfig()
    except Exception:
        traceback.print_exc()
    return KEYCONFIG_CHECK_INTERVAL


def is_enabled():
    return _enabled


def _take_back(module):
    """Undo what a module registered before its ``register()`` failed: left
    in place, its classes would make every later attempt to turn the mode
    on fail ("already registered"), and its handlers would go on running."""
    try:
        module.unregister()
    except Exception:
        pass  # It stops at the first thing that was never registered.
    for value in list(vars(module).values()):
        if (isinstance(value, type) and value.__module__ == module.__name__
                and getattr(value, "is_registered", False)):
            try:
                bpy.utils.unregister_class(value)
            except Exception:
                traceback.print_exc()


def enable():
    """Register all feature modules. On any failure, roll back and return False."""
    global _enabled
    if _enabled:
        return True
    for module in _feature_modules:
        try:
            module.register()
        except Exception:
            traceback.print_exc()
            _take_back(module)
            disable()
            return False
        _registered.append(module)
    _enabled = True
    bpy.app.timers.register(_check_keyconfig, first_interval=KEYCONFIG_CHECK_INTERVAL, persistent=True)
    return True


def disable():
    """Unregister feature modules in reverse order. Safe to call when already off."""
    global _enabled
    if bpy.app.timers.is_registered(_check_keyconfig):
        bpy.app.timers.unregister(_check_keyconfig)
    while _registered:
        module = _registered.pop()
        try:
            module.unregister()
        except Exception:
            traceback.print_exc()
    _enabled = False
