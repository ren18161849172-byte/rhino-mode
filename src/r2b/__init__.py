# SPDX-License-Identifier: GPL-3.0-or-later
"""R2B: Rhino-style workflow for Blender.

Two layers, following the Maya Workflow Kit pattern:

* CORE (prefs, switch) is always registered, so the user can always turn
  the mode off again, even if part of the mode failed to load.
* MODE (see ``mode.py``) holds everything Rhino-specific and is registered
  all-or-nothing only while the switch is on.

The add-on never writes Blender preferences, themes or the active keyconfig.
"""

import sys

import bpy

from . import debug, mode, prefs, switch

_core_modules = (prefs, switch, debug)


_RESTORE_RETRIES = 50
_restore_attempts = 0


def _keyconfig_ready():
    keyconfig = bpy.context.window_manager.keyconfigs.active
    keymap = keyconfig.keymaps.get("3D View") if keyconfig else None
    return keymap is not None and len(keymap.keymap_items) > 0


def _restore_mode():
    # Deferred: preferences, the window manager and the keymaps are not fully
    # available while add-ons are being registered at startup.
    global _restore_attempts
    if not prefs.get().mode_enabled:
        return None
    if not _keyconfig_ready():
        _restore_attempts += 1
        return 0.1 if _restore_attempts < _RESTORE_RETRIES else None
    if not mode.enable():
        prefs.get().mode_enabled = False
    return None


def register():
    for module in _core_modules:
        module.register()
    bpy.app.timers.register(_restore_mode, first_interval=0.1)


def _forget_modules():
    # Blender only re-imports an add-on's __init__.py (and only if it changed)
    # when the add-on is enabled again, so edited submodules would stay stale
    # until Blender restarts. Dropping all of R2B's modules makes the next
    # enable load every file fresh.
    for name in [n for n in sys.modules if n == __name__ or n.startswith(__name__ + ".")]:
        del sys.modules[name]


def unregister():
    if bpy.app.timers.is_registered(_restore_mode):
        bpy.app.timers.unregister(_restore_mode)
    mode.disable()
    for module in reversed(_core_modules):
        module.unregister()
    _forget_modules()
