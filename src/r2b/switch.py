# SPDX-License-Identifier: GPL-3.0-or-later
"""The Rhino Mode on/off switch (CORE layer): operator, top bar button, Edit
menu item and a shortcut that works everywhere (HOTKEY, free in Blender's
default keymaps from 4.2 on)."""

import bpy

from . import keymaps, mode, prefs

HOTKEY = ('R', {"ctrl": True, "shift": True, "alt": True})

_hotkey = keymaps.AddonItems()


class R2B_OT_toggle_mode(bpy.types.Operator):
    """Turn Rhino Mode on or off"""

    bl_idname = "r2b.toggle_mode"
    bl_label = "Toggle Rhino Mode"
    bl_options = {'REGISTER'}

    def execute(self, context):
        addon_prefs = prefs.get()
        if addon_prefs.mode_enabled:
            mode.disable()
            addon_prefs.mode_enabled = False
            # Blender saves the preferences on exit only if something marked
            # them changed (setting a property from Python does not): without
            # this the mode would be back on after a restart, or still off.
            context.preferences.is_dirty = True
            self.report({'INFO'}, "Rhino Mode off")
            return {'FINISHED'}

        if not mode.enable():
            self.report({'ERROR'}, "Rhino Mode failed to load, see the system console")
            return {'CANCELLED'}
        addon_prefs.mode_enabled = True
        context.preferences.is_dirty = True
        self.report({'INFO'}, "Rhino Mode on")
        return {'FINISHED'}


def draw_topbar_button(self, context):
    enabled = prefs.get().mode_enabled
    self.layout.operator(
        R2B_OT_toggle_mode.bl_idname,
        text="Rhino",
        depress=enabled,
    )


def draw_edit_menu_item(self, context):
    self.layout.separator()
    self.layout.operator(R2B_OT_toggle_mode.bl_idname)


def register():
    bpy.utils.register_class(R2B_OT_toggle_mode)
    bpy.types.TOPBAR_HT_upper_bar.append(draw_topbar_button)
    bpy.types.TOPBAR_MT_edit.append(draw_edit_menu_item)
    if bpy.context.window_manager.keyconfigs.addon is not None:
        key, modifiers = HOTKEY
        _hotkey.add(R2B_OT_toggle_mode.bl_idname, key, 'PRESS', keymap="Window", space_type='EMPTY',
                    **modifiers)
        keymaps.update_keyconfigs()  # Merged into the user keyconfig: works at once.


def unregister():
    _hotkey.remove()
    keymaps.update_keyconfigs()
    bpy.types.TOPBAR_MT_edit.remove(draw_edit_menu_item)
    bpy.types.TOPBAR_HT_upper_bar.remove(draw_topbar_button)
    bpy.utils.unregister_class(R2B_OT_toggle_mode)
