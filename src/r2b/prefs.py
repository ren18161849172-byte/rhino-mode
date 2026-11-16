# SPDX-License-Identifier: GPL-3.0-or-later
"""Add-on preferences (CORE layer)."""

import bpy


def _debug_log_update(self, context):
    if self.debug_log:
        from . import debug
        debug.start()


class R2B_Alias(bpy.types.PropertyGroup):
    """A command alias, as in Rhino: typing ``name`` runs ``macro``."""

    name: bpy.props.StringProperty(name="Alias", description="What to type")
    macro: bpy.props.StringProperty(
        name="Command",
        description="The command to run, optionally followed by its input, separated by spaces "
                    "(e.g. \"Circle 0,0 5\"); Rhino's _ and ! prefixes are ignored",
    )


class R2B_OT_alias_add(bpy.types.Operator):
    """Add a command alias"""

    bl_idname = "r2b.alias_add"
    bl_label = "Add Alias"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        get().aliases.add()
        context.preferences.is_dirty = True
        return {'FINISHED'}


class R2B_OT_alias_remove(bpy.types.Operator):
    """Remove this command alias"""

    bl_idname = "r2b.alias_remove"
    bl_label = "Remove Alias"
    bl_options = {'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        aliases = get().aliases
        if 0 <= self.index < len(aliases):
            aliases.remove(self.index)
            context.preferences.is_dirty = True
        return {'FINISHED'}


# Alias presets: Rhino's default aliases (those of commands R2B has), and
# Blender's single-key shortcuts as aliases (type the letter and Space).
ALIAS_PRESETS = {
    'RHINO': (("M", "Move"), ("U", "Undo"), ("O", "Ortho"), ("P", "Planar"), ("S", "Snap"),
              ("POn", "PointsOn"), ("POff", "PointsOff")),
    'BLENDER': (("g", "b:Move"), ("r", "b:Rotate"), ("s", "b:Scale"), ("e", "Extrude"), ("i", "Inset"),
                ("k", "Knife"), ("x", "b:Delete"), ("f", "MakeFace"), ("p", "Separate"), ("y", "b:Split"),
                ("v", "Rip"), ("m", "Merge"), ("h", "Hide"), ("d", "Duplicate")),
}


class R2B_OT_alias_preset(bpy.types.Operator):
    """Add a set of aliases (existing aliases with the same name are kept)"""

    bl_idname = "r2b.alias_preset"
    bl_label = "Add Alias Preset"
    bl_options = {'INTERNAL'}

    preset: bpy.props.EnumProperty(items=(
        ('RHINO', "Rhino's Default Aliases", "M Move, U Undo, O Ortho, P Planar, S Snap, POn, POff"),
        ('BLENDER', "Blender Single-Key Aliases",
         "Blender's shortcut letters as aliases: g b:Move, r b:Rotate, s b:Scale, e Extrude, i Inset, k Knife..."),
    ))

    def execute(self, context):
        aliases = get().aliases
        existing = {entry.name.strip().lower() for entry in aliases}
        added = 0
        for name, macro in ALIAS_PRESETS[self.preset]:
            if name.lower() in existing:
                continue
            entry = aliases.add()
            entry.name, entry.macro = name, macro
            added += 1
        context.preferences.is_dirty = True
        self.report({'INFO'}, f"Added {added} aliases")
        return {'FINISHED'}


class R2B_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    # Remembered between sessions so the mode comes back on after a restart.
    # Only changed through the toggle operator, never directly by the UI.
    mode_enabled: bpy.props.BoolProperty(
        name="Rhino Mode",
        default=False,
    )

    debug_log: bpy.props.BoolProperty(
        name="Debug Log",
        description="Write mouse button events in Blender's windows to r2b_debug.log "
                    "in the system temp folder, to help diagnose interaction problems",
        default=False,
        update=_debug_log_update,
    )

    aliases: bpy.props.CollectionProperty(type=R2B_Alias)

    name_clash: bpy.props.EnumProperty(
        name="Same name, different meaning",
        description="Which meaning a command name has when Rhino and Blender use it for different things "
                    "(PushPull, Array, Split, Copy...); the other one is typed with b: or r: in front",
        items=(('RHINO', "Rhino", "Rhino's meaning (Blender's with b:, e.g. b:PushPull)"),
               ('BLENDER', "Blender", "Blender's meaning (Rhino's with r:, e.g. r:Scale)")),
        default='RHINO',
    )

    solid_heavy_scenes: bpy.props.BoolProperty(
        name="Solid viewports in heavy scenes",
        description="In a scene of millions of faces (scattered plants, hair), show the wireframe viewports "
                    "of the Rhino workspace as Solid: wireframes of that much geometry make every redraw "
                    "slow. Type Wireframe in a viewport to switch it back",
        default=True,
    )

    recent_first: bpy.props.BoolProperty(
        name="Recently used commands first",
        description="When a typed prefix matches several commands, run the one used most recently "
                    "(as Rhino's command line does) instead of the first in the list",
        default=False,
    )

    def draw(self, context):
        from . import debug

        layout = self.layout
        row = layout.row()
        row.label(text="Rhino Mode is " + ("on" if self.mode_enabled else "off"))
        row.operator("r2b.toggle_mode", text="Turn Off" if self.mode_enabled else "Turn On")
        layout.prop(self, "debug_log")
        if self.debug_log:
            layout.label(text=debug.LOG_PATH)
        layout.prop(self, "name_clash")
        layout.prop(self, "recent_first")
        layout.prop(self, "solid_heavy_scenes")
        box = layout.box()
        box.label(text="Command aliases (type the alias in the command line to run the command)")
        for index, alias in enumerate(self.aliases):
            row = box.row(align=True)
            row.prop(alias, "name", text="")
            row.prop(alias, "macro", text="")
            row.operator(R2B_OT_alias_remove.bl_idname, text="", icon='X').index = index
        row = box.row()
        row.operator(R2B_OT_alias_add.bl_idname, icon='ADD')
        for preset in ('RHINO', 'BLENDER'):
            row.operator(R2B_OT_alias_preset.bl_idname,
                         text={'RHINO': "Rhino's Defaults", 'BLENDER': "Blender Keys"}[preset]).preset = preset


def get():
    return bpy.context.preferences.addons[__package__].preferences


_classes = (R2B_Alias, R2B_OT_alias_add, R2B_OT_alias_remove, R2B_OT_alias_preset, R2B_AddonPreferences)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
