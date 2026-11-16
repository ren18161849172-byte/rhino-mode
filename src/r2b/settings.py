# SPDX-License-Identifier: GPL-3.0-or-later
"""Modeling aids stored in the scene, as Rhino stores them in the document
(MODE feature): Grid Snap, Ortho, Planar, Osnap, SmartTrack and Gumball, with toggles
in the status bar
and Rhino's F9 (Snap) and F8 (Ortho) keys in the 3D View.

Their labels have their own translation context: in Blender's dictionary
"Int" is an integer and "End" means finish. R2B's translations use Rhino's
own Chinese terms.
"""

import bpy

from . import keymaps, osnap

TRANSLATION_CONTEXT = "R2B"
_RHINO_TERMS = {
    # English label: (Simplified Chinese, Traditional Chinese), as in Rhino.
    "Grid Snap": ("锁定格点", "鎖定格點"),
    "Ortho": ("正交", "正交"),
    "Planar": ("平面模式", "平面模式"),
    "Osnap": ("物件锁点", "物件鎖點"),
    "SmartTrack": ("智慧轨迹", "智慧軌跡"),
    "Gumball": ("操作轴", "操作軸"),
    "History": ("记录建构历史", "記錄建構歷史"),
    "Record History": ("记录建构历史", "記錄建構歷史"),
    "Gumball Alignment": ("操作轴对齐", "操作軸對齊"),
    "CPlane": ("工作平面", "工作平面"),
    "Object": ("物件", "物件"),
    "World": ("世界", "世界"),
    "Osnap Modes": ("物件锁点", "物件鎖點"),
    "End": ("端点", "端點"),
    "Near": ("最近点", "最近點"),
    "Point": ("点", "點"),
    "Mid": ("中点", "中點"),
    "Cen": ("中心点", "中心點"),
    "Int": ("交点", "交點"),
    "Perp": ("垂直点", "垂直點"),
    "Tan": ("切点", "切點"),
    "Quad": ("四分点", "四分點"),
}
TRANSLATIONS = {
    locale: {(TRANSLATION_CONTEXT, english): terms[i] for english, terms in _RHINO_TERMS.items()}
    for i, locale in enumerate(("zh_HANS", "zh_HANT"))
}



class R2B_Settings(bpy.types.PropertyGroup):
    grid_snap: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Grid Snap",
        description="Snap picked points to the construction plane grid (F9)",
    )
    ortho: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Ortho",
        description="Restrict picked points to 90° directions from the previous point "
                    "(F8; hold Shift to toggle while picking)",
    )
    planar: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Planar",
        description="Pick points at the same CPlane elevation as the previous point",
    )
    osnap: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Osnap",
        description="Snap picked points to objects (hold Alt to pick without snapping)",
        default=True,
    )
    gumball: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Gumball",
        description="Show move, rotate and scale handles on the selection in the Rhino workspace: "
                    "drag one (with Osnap), or click it to type a value",
    )
    smart_track: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="SmartTrack",
        description="Line picked points up with points snapped to before in the same command "
                    "(hold Alt to pick without it)",
        default=True,
    )
    gumball_alignment: bpy.props.EnumProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Gumball Alignment",
        description="What the Gumball's axes follow",
        items=(('CPLANE', "CPlane", "The viewport's construction plane"),
               ('OBJECT', "Object", "The active object's own axes"),
               ('WORLD', "World", "The world axes")),
        default='CPLANE',
    )
    osnap_modes: bpy.props.EnumProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Osnap Modes",
        description="What to snap to",
        items=osnap.MODES,
        options={'ENUM_FLAG'},
        default=osnap.DEFAULT_MODES,
    )
    osnap_radius: bpy.props.IntProperty(
        name="Osnap Radius",
        description="How close to the mouse an object snap must be, in pixels",
        default=12,
        min=2,
        max=100,
        subtype='PIXEL',
    )
    mesh_angle: bpy.props.FloatProperty(
        name="Mesh Angle",
        description="Largest angle between neighboring mesh edges along a curve when curves become meshes "
                    "(extrusions, planar surfaces); straight segments are not divided",
        default=0.17453292519943295,  # 10 degrees
        min=0.017453292519943295,
        max=0.7853981633974483,
        subtype='ANGLE',
    )
    record_history: bpy.props.BoolProperty(
        translation_context=TRANSLATION_CONTEXT,
        name="Record History",
        description="Commands that support it (ExtrudeCrv, booleans, arrays, Twist/Taper/Bend) keep their result "
                    "linked to the input: change the input and the result follows (Blender modifiers / "
                    "Geometry Nodes)",
    )
    snap_spacing: bpy.props.FloatProperty(
        name="Snap Spacing",
        description="Grid Snap spacing",
        default=1.0,
        min=1e-6,
        subtype='DISTANCE',
    )


def get(context):
    return context.scene.r2b


def draw_status_bar(self, context):
    settings = get(context)
    row = self.layout.row(align=True)
    row.prop(settings, "grid_snap", toggle=True)
    row.prop(settings, "ortho", toggle=True)
    row.prop(settings, "planar", toggle=True)
    row.prop(settings, "osnap", toggle=True)
    row.prop(settings, "smart_track", toggle=True)
    row.prop(settings, "gumball", toggle=True)
    row.prop(settings, "record_history", text="History", text_ctxt=TRANSLATION_CONTEXT, toggle=True)
    if settings.gumball:
        row.prop(settings, "gumball_alignment", text="")
    modes = self.layout.row(align=True)
    modes.active = settings.osnap
    modes.prop(settings, "osnap_modes", expand=True)


_addon_items = keymaps.AddonItems()


def _keep_through_status_text():
    """``WorkSpace.status_text_set`` remembers the status bar's draw function
    the first time anything uses it, and puts that one back every time after.
    If that was before the toggles were appended (installing an extension
    shows status text), the next status text would take them away: make it
    remember the function that has them."""
    header = bpy.types.STATUSBAR_HT_header
    remembered = getattr(header, "_draw_orig", None)
    functions = getattr(header.draw, "_draw_funcs", None)
    if remembered is not None and functions and functions[0] is remembered:
        header._draw_orig = header.draw


def register():
    bpy.app.translations.register(__name__, TRANSLATIONS)
    bpy.utils.register_class(R2B_Settings)
    bpy.types.Scene.r2b = bpy.props.PointerProperty(type=R2B_Settings)
    bpy.types.STATUSBAR_HT_header.append(draw_status_bar)
    _keep_through_status_text()
    for key, path in (('F8', "scene.r2b.ortho"), ('F9', "scene.r2b.grid_snap")):
        _addon_items.add("wm.context_toggle", key, 'PRESS', properties={"data_path": path})
    keymaps.update_keyconfigs()


def unregister():
    _addon_items.remove()
    keymaps.update_keyconfigs()
    bpy.types.STATUSBAR_HT_header.remove(draw_status_bar)
    del bpy.types.Scene.r2b
    bpy.utils.unregister_class(R2B_Settings)
    bpy.app.translations.unregister(__name__)
