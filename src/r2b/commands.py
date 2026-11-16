# SPDX-License-Identifier: GPL-3.0-or-later
"""Command names and what they run, in two dialects: Rhino and Blender.

Every command has Rhino names, Blender names, or both: Rhino users type
Rhino's names (Line, Trim, InsertEdge), Blender users Blender's (Extrude,
LoopCut, Solidify), and one capability can be reached by either. Names are
matched case-insensitively and without spaces or punctuation, since a space
runs the command line ("Loop Cut and Slide" is typed LoopCut...).

How a typed word is looked up (``lookup``):

1. ``b:`` / ``r:`` in front forces Blender's / Rhino's meaning. After ``b:``
   any Blender operator can also be named by its Python name
   (``b:mesh.bevel``) or by its label.
2. An exact name of the preferred dialect (Rhino unless changed in the
   preferences), then of the other one. A Blender name that is also a Rhino
   command name (PushPull, Array, Split...) keeps Rhino's meaning: if R2B
   does not have that Rhino command yet, it says so instead of running
   Blender's namesake, which then needs ``b:``.
3. Other Blender operators by their label (Smart UV Project -> SmartUVProject),
   so Blender tools without an entry here, and other add-ons' operators, can
   be run by name.
4. A prefix runs the first matching command: Rhino names first, in table
   order (as Rhino's alphabetical list), then Blender names.

Running a command calls registered Blender operators by name (no
eval/exec), in the context of a 3D viewport, usually with 'INVOKE_DEFAULT'
so interactive operators behave as when used from Blender's own UI.
"""

import os
import re
import time
from dataclasses import dataclass, field

import bpy

RHINO = 'RHINO'
BLENDER = 'BLENDER'

_MODE_NAMES = {
    'OBJECT': "Object Mode", 'EDIT_MESH': "Edit Mode (mesh)", 'EDIT_CURVE': "Edit Mode (curve)",
    'EDIT_ARMATURE': "Edit Mode (armature)", 'POSE': "Pose Mode", 'SCULPT': "Sculpt Mode",
}


class CommandError(Exception):
    """A command cannot run in the current state (shown in the command line)."""


@dataclass(frozen=True)
class Command:
    name: str
    run: object  # callable(context)
    description: str
    aliases: tuple = ()  # More Rhino names.
    operators: tuple = field(default=())  # Blender operators used, for tests.
    blender: tuple = ()  # Blender names.
    rhino_name: bool = True  # Whether ``name`` is a Rhino name (else a Blender one).
    label: str = ""  # Blender's own name for the tool, e.g. "Loop Cut and Slide".
    key: str = ""  # Blender's shortcut for it (muted in Rhino Mode, shown as a hint).
    like: tuple = ()  # Names on the other side for something similar ("FilletEdge").
    modes: tuple = ()  # context.mode values it works in; () = any.
    modal: bool = False  # Starts one of Blender's interactive tools (needs the mouse in the viewport).

    def names(self, dialect):
        if dialect == RHINO:
            return ((self.name,) if self.rhino_name else ()) + self.aliases
        return (() if self.rhino_name else (self.name,)) + self.blender

    def available(self, mode):
        return not self.modes or mode in self.modes


def key(name):
    """A name as it is matched: lower case, letters and digits only."""
    return re.sub(r"[^0-9a-z]", "", name.lower())


# --- Undo ---------------------------------------------------------------------
#
# Commands run inside R2B's own key operators, where Blender's bookkeeping for
# a key or menu item does not happen by itself: an operator called from
# Python with the default undo=False pushes no undo step and is not
# registered (no Adjust Last Operation panel, not in the history). So
# runners call operators with undo=True, and the command line pushes a step
# for runners that change data without an operator (``undo_handled`` is
# False after them; see command_line._run).

_undo = {"handled": False}


def begin_undo():
    _undo["handled"] = False


def undo_handled():
    return _undo["handled"]


def _handled():
    """The runner pushed its own undo step, or changed nothing undoable."""
    _undo["handled"] = True


# --- Runners ------------------------------------------------------------------

def _call(idname, context_props, invoke=True):
    module, name = idname.split(".")
    operator = getattr(getattr(bpy.ops, module), name)
    _handled()
    # undo=True: Blender pushes the operator's undo step (if it has one) and
    # registers it, as when it runs from a key or a menu.
    if invoke:
        return operator('INVOKE_DEFAULT', True, **context_props)
    return operator('EXEC_DEFAULT', True, **context_props)


def op(idname, invoke=True, modal=False, **props):
    """Runner calling one operator."""
    def run(context):
        return _call(idname, props, invoke)
    run.operators = (idname,)
    run.modal = modal
    return run


def first_op(idnames, **props):
    """Runner calling the first of ``idnames`` this Blender version has
    (operators renamed between versions)."""
    for idname in idnames:
        module, name = idname.split(".")
        if name in dir(getattr(bpy.ops, module)):
            return op(idname, **props)
    return None


def menu(name, pie=False):
    """Runner opening one of Blender's menus (e.g. the Merge menu, M)."""
    def run(context):
        if not hasattr(bpy.types, name):
            raise CommandError(f"Blender {bpy.app.version_string} has no menu {name}")
        _handled()  # The menu item runs (and pushes) on its own.
        if pie:
            return bpy.ops.wm.call_menu_pie(name=name)
        return bpy.ops.wm.call_menu(name=name)
    run.operators = ("wm.call_menu_pie" if pie else "wm.call_menu",)
    run.menus = (name,)
    return run


def _mode_list(modes):
    return ", ".join(_MODE_NAMES.get(m, m.replace("_", " ").title()) for m in modes)


def by_mode(default=None, **runners):
    """Runner choosing by ``context.mode`` ('OBJECT', 'EDIT_MESH', ...), with
    ``default`` for the other modes."""
    def run(context):
        runner = runners.get(context.mode, default)
        if runner is None:
            raise CommandError(f"works in {_mode_list(runners)}, not in "
                               f"{_MODE_NAMES.get(context.mode, context.mode.replace('_', ' ').title())}")
        return runner(context)
    every = (*runners.values(), *([default] if default else []))
    run.operators = tuple(dict.fromkeys(i for r in every for i in getattr(r, "operators", ())))
    run.menus = tuple(dict.fromkeys(m for r in every for m in getattr(r, "menus", ())))
    run.modal = any(getattr(r, "modal", False) for r in every)
    run.modes = () if default else tuple(runners)
    return run


def in_modes(modes, runner):
    return by_mode(**{mode: runner for mode in modes})


def _select_all(action):
    return by_mode(
        OBJECT=op("object.select_all", action=action),
        EDIT_MESH=op("mesh.select_all", action=action),
        EDIT_CURVE=op("curve.select_all", action=action),
        EDIT_ARMATURE=op("armature.select_all", action=action),
        POSE=op("pose.select_all", action=action),
    )


def _all_viewports(runner):
    """Run ``runner`` in every 3D viewport of the window."""
    def run(context):
        for area in context.window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with context.temp_override(area=area, region=region, space_data=area.spaces.active):
                runner(bpy.context)
    run.operators = runner.operators
    return run


def _display(display):
    def run(context):
        from . import viewport_menu

        _handled()  # View only.
        viewport_menu.set_display(context.space_data, display)
    run.operators = ()
    return run


def _perspective(context):
    from . import viewport_menu

    _handled()  # View only.
    viewport_menu.set_view(context, 'PERSPECTIVE')


_perspective.operators = ()


def _maximize(want_maximized):
    def run(context):
        from . import viewport_maximize

        _handled()  # Layout only.
        maximized = context.window.screen.get(viewport_maximize.LAYOUT) == "maximized"
        if maximized != want_maximized:
            viewport_maximize.toggle_maximize(context)
    run.operators = ()
    return run


def _toggle_region(attribute, label):
    """Runner showing or hiding a region of the 3D viewport it runs in (the
    one typed in): its shortcut letter types into the command line."""
    def run(context):
        from . import command_line

        _handled()  # View only.
        space = context.space_data
        if space is None or space.type != 'VIEW_3D':
            raise CommandError("needs a 3D viewport")
        setattr(space, attribute, not getattr(space, attribute))
        command_line.echo(context.window, f"{label} {'shown' if getattr(space, attribute) else 'hidden'}")
    run.operators = ()
    return run


def _toggle_setting(name, label):
    def run(context):
        from . import command_line

        _handled()  # Drawing aids are not undone (as in Rhino).
        aids = context.scene.r2b
        setattr(aids, name, not getattr(aids, name))
        command_line.echo(context.window, f"{label} {'on' if getattr(aids, name) else 'off'}")
    run.operators = ()
    return run


def _toggle(data_path, label):
    """Runner switching a boolean like Blender's own toggle shortcuts (O, Alt+Z)."""
    def run(context):
        from . import command_line

        _handled()
        bpy.ops.wm.context_toggle('EXEC_DEFAULT', True, data_path=data_path)
        command_line.echo(context.window, f"{label} {'on' if _follow(context, data_path) else 'off'}")
    run.operators = ("wm.context_toggle",)
    return run


def _follow(context, path):
    """The value at a dotted attribute path from ``context``."""
    value = context
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _cycle_gumball_alignment(context):
    from . import command_line

    _handled()
    aids = context.scene.r2b
    order = ('CPLANE', 'OBJECT', 'WORLD')
    aids.gumball_alignment = order[(order.index(aids.gumball_alignment) + 1) % len(order)]
    command_line.echo(context.window, f"Gumball aligned to {aids.gumball_alignment.title()}")


_cycle_gumball_alignment.operators = ()


def _all_layers_on(context):
    def show(layer_collection):
        layer_collection.hide_viewport = False
        for child in layer_collection.children:
            show(child)
    show(context.view_layer.layer_collection)


_all_layers_on.operators = ()


def _list_commands(context):
    from . import command_line

    _handled()
    lines = []
    for command in COMMANDS:
        names = [*command.names(RHINO), *command.names(BLENDER)]
        source = "Rhino Mode" if any(o.startswith("r2b.") for o in command.operators) else "Blender"
        lines.append(f"{' / '.join(names):<34} {command.description}  [{source}]")
    command_line.print_lines(context, lines, 'INFO')


_list_commands.operators = ()


def _show_aliases(context):
    _handled()
    bpy.ops.preferences.addon_show(module=__package__)


_show_aliases.operators = ("preferences.addon_show",)


def _rhino_transform(idname, fallback):
    """R2B's transform for objects and mesh or curve points, Blender's elsewhere."""
    return by_mode(OBJECT=op(idname), EDIT_MESH=op(idname), EDIT_CURVE=op(idname), default=op(fallback))


def _crease(value):
    """Rhino's Crease / RemoveCrease: full crease on the selected edges."""
    def run(context):
        _handled()
        bpy.ops.transform.edge_crease('EXEC_DEFAULT', True, value=value)
    run.operators = ("transform.edge_crease",)
    return in_modes(('EDIT_MESH',), run)


def _extrude_objects(context):
    """Blender's Extrude name in Object Mode: ExtrudeSrf for selected meshes,
    else ExtrudeCrv."""
    _handled()
    if any(o.type == 'MESH' for o in context.selected_objects) and not any(
            o.type == 'CURVE' for o in context.selected_objects):
        return bpy.ops.r2b.extrude_srf('INVOKE_DEFAULT', True)
    return bpy.ops.r2b.extrude_crv('INVOKE_DEFAULT', True)


_extrude_objects.operators = ("r2b.extrude_srf", "r2b.extrude_crv")


def _solid_tool(name):
    """Runner calling a function of solid_tools.py (imported when run)."""
    def run(context):
        from . import solid_tools

        getattr(solid_tools, name)(context)
    run.operators = ()
    return run


def _module_function(module, name, undo=True):
    """Runner calling a function of an R2B module (imported when run).
    ``undo``: it changes data (the command line pushes an undo step)."""
    def run(context):
        import importlib

        if not undo:
            _handled()
        getattr(importlib.import_module(f"{__package__}.{module}"), name)(context)
    run.operators = ()
    return run


def _flip_objects(context):
    """Flip in Object Mode: reverse the faces of the selected meshes."""
    import bmesh

    meshes = [o for o in context.selected_objects if o.type == 'MESH']
    if not meshes:
        raise CommandError("select meshes, or points, edges or faces in Edit Mode")
    for data in {o.data for o in meshes}:
        mesh = bmesh.new()
        mesh.from_mesh(data)
        bmesh.ops.reverse_faces(mesh, faces=mesh.faces)
        mesh.to_mesh(data)
        mesh.free()
        data.update()


_flip_objects.operators = ()


def _apply_modifiers(context):
    """Apply all modifiers of the selected objects (Blender's "Apply" for each)."""
    from . import command_line

    done = 0
    for obj in list(context.selected_objects):
        for modifier in list(obj.modifiers):
            with context.temp_override(object=obj, active_object=obj):
                bpy.ops.object.modifier_apply(modifier=modifier.name)
            done += 1
    command_line.echo(context.window, f"Applied {done} modifier{'s' if done != 1 else ''}")


_apply_modifiers.operators = ("object.modifier_apply",)


def _add_modifier(modifier_type):
    def run(context):
        if not any(o.type in ('MESH', 'CURVE', 'SURFACE', 'FONT', 'LATTICE') for o in context.selected_objects):
            raise CommandError("select the objects to add it to")
        _handled()
        bpy.ops.object.modifier_add('EXEC_DEFAULT', True, type=modifier_type, use_selected_objects=True)
    run.operators = ("object.modifier_add",)
    return run


# Brush modes: Blender shows a brush shelf in each of the four viewports.
_BRUSH_MODES = {'SCULPT', 'TEXTURE_PAINT', 'VERTEX_PAINT', 'WEIGHT_PAINT'}
_maximize_tip_shown = False


def _mode_set(mode):
    def run(context):
        global _maximize_tip_shown
        if context.active_object is None:
            raise CommandError("no active object")
        _handled()
        bpy.ops.object.mode_set('EXEC_DEFAULT', True, mode=mode)
        window = context.window
        if (mode in _BRUSH_MODES and not _maximize_tip_shown and window is not None
                and window.screen.get("r2b_layout") == "four_view"):
            # Found reproducing a tutorial: each viewport's brush shelf left little room.
            _maximize_tip_shown = True
            from . import command_line

            command_line.echo(window, "Tip: MaxViewport (or a double-click on a viewport's title) gives one "
                              "viewport all the room for sculpting and painting; 4View goes back.", 'INFO')
    run.operators = ("object.mode_set",)
    return run


# --- The table ------------------------------------------------------------------

def _command(name, run, description, aliases=(), blender=(), like=(), label="", key=""):
    """A command with Rhino names: R2B's own, or one of Blender's tools
    (``label`` and ``key``: its Blender name and shortcut)."""
    return Command(name, run, description, tuple(aliases), getattr(run, "operators", ()),
                   blender=tuple(blender), like=tuple(like), modes=getattr(run, "modes", ()),
                   modal=getattr(run, "modal", False), label=label, key=key)


def _tool(name, run, label, key="", also=(), rhino=(), like=(), modes=None):
    """One of Blender's tools under its Blender name(s). ``rhino`` are Rhino
    names for the same thing; ``like`` Rhino names for something similar."""
    if modes is not None and not hasattr(run, "modes"):
        run = in_modes(modes, run)
    return Command(name, run, label, tuple(rhino), getattr(run, "operators", ()), blender=tuple(also),
                   rhino_name=False, label=label, key=key, like=tuple(like),
                   modes=getattr(run, "modes", ()), modal=getattr(run, "modal", False))


MESH = ('EDIT_MESH',)
EDIT = ('EDIT_MESH', 'EDIT_CURVE')

COMMANDS = [
    # Transform: Rhino-style point picking for objects and mesh points, Blender's own otherwise
    _command("Move", _rhino_transform("r2b.move", "transform.translate"),
             "Move objects or points from a base point to a target point"),
    _command("SoftMove", in_modes(MESH, op("r2b.soft_move")),
             "Move mesh points with the points around them following (Proportional Editing, within Radius)"),
    _command("Copy", by_mode(
        OBJECT=op("r2b.copy"),
        EDIT_MESH=op("mesh.duplicate_move"),
        EDIT_CURVE=op("curve.duplicate_move"),
    ), "Copy objects from a base point to target points"),
    _command("Rotate", _rhino_transform("r2b.rotate", "transform.rotate"),
             "Rotate objects or points about a center"),
    _command("Scale", _rhino_transform("r2b.scale", "transform.resize"),
             "Scale objects or points about an origin point, the same in all directions"),
    _command("Scale1D", _rhino_transform("r2b.scale1d", "transform.resize"),
             "Scale objects or points in one direction", like=("b:Scale X",)),
    _command("Scale2D", _rhino_transform("r2b.scale2d", "transform.resize"),
             "Scale objects or points in the construction plane", like=("b:Scale Shift Z",)),
    _command("ScaleNU", _rhino_transform("r2b.scale_nu", "transform.resize"),
             "Scale objects or points by different factors along x, y and z"),
    _command("Mirror", _rhino_transform("r2b.mirror", "transform.mirror"),
             "Mirror objects or points across a line"),
    _command("Orient", _rhino_transform("r2b.orient", "transform.transform"),
             "Move and turn objects from two reference points to two target points"),
    _command("Orient3Pt", _rhino_transform("r2b.orient3pt", "transform.transform"),
             "Move and turn objects from three reference points to three target points"),
    _command("Shear", _rhino_transform("r2b.shear", "transform.shear"),
             "Slide objects along a direction in proportion to their distance across it"),
    _command("ProjectToCPlane", _rhino_transform("r2b.project_to_cplane", "transform.resize"),
             "Flatten objects or points onto the construction plane"),
    _command("SetPt", _rhino_transform("r2b.set_pt", "transform.translate"),
             "Set the x, y or z of points or objects to a picked point's"),
    _command("Align", in_modes(('OBJECT',), op("r2b.align")), "Align objects by their bounding boxes",
             like=("AlignObjects",)),
    _command("Distribute", in_modes(('OBJECT',), op("r2b.distribute")), "Space objects evenly"),
    # Deformations (Simple Deform) and arrays
    _command("Twist", in_modes(('OBJECT',), op("r2b.twist")), "Twist objects about an axis",
             like=("SimpleDeformModifier",)),
    _command("Taper", in_modes(('OBJECT',), op("r2b.taper")), "Taper objects along an axis",
             like=("SimpleDeformModifier",)),
    _command("Stretch", in_modes(('OBJECT',), op("r2b.stretch")), "Stretch objects along an axis",
             like=("SimpleDeformModifier",)),
    _command("Bend", in_modes(('OBJECT',), op("r2b.bend")), "Bend objects along a spine",
             like=("SimpleDeformModifier", "b:Bend")),
    _command("Array", in_modes(('OBJECT',), op("r2b.array")), "Copy objects in rows, columns and levels",
             like=("ArrayModifier",)),
    _command("ArrayLinear", in_modes(('OBJECT',), op("r2b.array_linear")), "Copy objects in a row",
             like=("ArrayModifier",)),
    _command("ArrayPolar", in_modes(('OBJECT',), op("r2b.array_polar")), "Copy objects around a center"),
    _command("ArrayCrv", in_modes(('OBJECT',), op("r2b.array_crv")), "Copy objects along a curve"),
    # Edit
    _command("Delete", by_mode(
        OBJECT=op("object.delete", confirm=False),
        EDIT_MESH=op("mesh.delete", type='VERT'),
        EDIT_CURVE=op("curve.delete", type='VERT'),
    ), "Delete the selection"),
    _command("Join", op("r2b.join"), "Join curves whose ends meet, or meshes"),
    _command("Explode", op("r2b.explode"), "Explode joined curves into their parts, polylines into lines"),
    _command("Hide", by_mode(
        OBJECT=op("object.hide_view_set"),
        EDIT_MESH=op("mesh.hide"),
        EDIT_CURVE=op("curve.hide"),
    ), "Hide the selection"),
    _command("Show", by_mode(
        OBJECT=op("object.hide_view_clear"),
        EDIT_MESH=op("mesh.reveal"),
        EDIT_CURVE=op("curve.reveal"),
    ), "Show hidden objects or points"),
    _command("SelAll", _select_all('SELECT'), "Select everything"),
    _command("SelNone", _select_all('DESELECT'), "Deselect everything"),
    _command("Invert", _select_all('INVERT'), "Invert the selection"),
    _command("Undo", op("ed.undo", invoke=False), "Undo"),
    _command("Redo", op("ed.redo", invoke=False), "Redo"),
    # F9 is Rhino's Snap in Rhino Mode; Blender's F9 panel by its name.
    _tool("AdjustLastOperation", op("screen.redo_last"), "Adjust Last Operation", "F9"),
    _command("PointsOn", op("object.mode_set", invoke=False, mode='EDIT'), "Edit points (Blender Edit Mode)",
             like=("EditMode",)),
    _command("PointsOff", op("object.mode_set", invoke=False, mode='OBJECT'), "Stop editing points",
             like=("ObjectMode",)),
    # Views
    _command("ZE", op("view3d.view_all", center=False), "Zoom extents in the active viewport", like=("FrameAll",)),
    _command("ZEA", _all_viewports(op("view3d.view_all", center=False)), "Zoom extents in all viewports"),
    _command("ZS", op("view3d.view_selected"), "Zoom to the selection in the active viewport",
             like=("FrameSelected",)),
    _command("ZSA", _all_viewports(op("view3d.view_selected")), "Zoom to the selection in all viewports"),
    _command("Top", op("view3d.view_axis", type='TOP'), "Top view"),
    _command("Bottom", op("view3d.view_axis", type='BOTTOM'), "Bottom view"),
    _command("Front", op("view3d.view_axis", type='FRONT'), "Front view"),
    _command("Back", op("view3d.view_axis", type='BACK'), "Back view"),
    _command("Right", op("view3d.view_axis", type='RIGHT'), "Right view"),
    _command("Left", op("view3d.view_axis", type='LEFT'), "Left view"),
    _command("Perspective", _perspective, "Perspective view"),
    _command("SetView", menu("VIEW3D_MT_view_viewpoint"), "Set the viewport's view (Blender's View > Viewpoint menu)",
             label="Viewpoint"),
    _command("Turntable", op("r2b.turntable", modal=True), "Turn the view around until a key or a click"),
    _command("Wireframe", _display('WIREFRAME'), "Wireframe display"),
    _command("Shaded", _display('SHADED'), "Shaded display"),
    _command("Ghosted", _display('GHOSTED'), "See-through shaded display", like=("XRay",)),
    _command("Rendered", _display('RENDERED'), "Rendered display"),
    _command("MaxViewport", _maximize(True), "Maximize the active viewport"),
    _command("4View", _maximize(False), "Show the four viewports"),
    # Selection and organizing (select_tools.py)
    _command("Lasso", in_modes(('OBJECT', 'EDIT_MESH', 'EDIT_CURVE'), op("r2b.lasso", modal=True)),
             "Select inside a freehand loop (Blender's Lasso Select)"),
    _command("SelCrv", in_modes(('OBJECT',), _module_function("select_tools", "sel_crv")), "Select all curves"),
    _command("SelMesh", in_modes(('OBJECT',), _module_function("select_tools", "sel_mesh")), "Select all meshes"),
    _command("SelPt", in_modes(('OBJECT',), _module_function("select_tools", "sel_pt")), "Select all point objects"),
    _command("SelOpen", in_modes(('OBJECT',), _module_function("select_tools", "sel_open")),
             "Select open curves and meshes"),
    _command("SelClosed", in_modes(('OBJECT',), _module_function("select_tools", "sel_closed")),
             "Select closed curves and meshes"),
    _command("SelSmall", in_modes(('OBJECT',), op("r2b.sel_small")), "Select objects smaller than a size"),
    _command("SelDup", in_modes(('OBJECT',), _module_function("select_tools", "sel_dup")),
             "Select copies lying on top of other objects"),
    _command("SelLayer", in_modes(('OBJECT',), _module_function("select_tools", "sel_layer")),
             "Select the objects on the layers of the selection (or the current layer)", like=("SelectGrouped",)),
    _command("SelColor", in_modes(('OBJECT',), _module_function("select_tools", "sel_color")),
             "Select objects of the colors of the selection"),
    _command("SelLast", in_modes(('OBJECT',), _module_function("select_tools", "sel_last")),
             "Select what the last command made"),
    _command("SelPrev", in_modes(('OBJECT',), _module_function("select_tools", "sel_prev")),
             "Select the selection before the last command"),
    _command("Lock", in_modes(('OBJECT',), _module_function("select_tools", "lock")),
             "Make the selected objects unselectable"),
    _command("Unlock", in_modes(('OBJECT',), _module_function("select_tools", "unlock")),
             "Make locked objects selectable again"),
    _command("Isolate", in_modes(('OBJECT',), _module_function("select_tools", "isolate")),
             "Hide all but the selection", like=("LocalView",)),
    _command("Unisolate", in_modes(('OBJECT',), _module_function("select_tools", "unisolate")),
             "Show the objects Isolate hid"),
    _command("HideSwap", in_modes(('OBJECT',), _module_function("select_tools", "hide_swap")),
             "Hide the shown objects, show the hidden ones"),
    _command("ShowSelected", in_modes(('OBJECT',), _module_function("select_tools", "show_selected")),
             "Show hidden objects, selected"),
    _command("Properties", _module_function("select_tools", "properties", undo=False), "Object properties",
             like=("ObjectProperties",)),
    # Measuring (analysis.py)
    _command("Distance", op("r2b.distance"), "Distance between two points", like=("MeasureTool",)),
    _command("Angle", op("r2b.angle"), "Angle at a vertex between two points"),
    _command("Length", _module_function("analysis", "length", undo=False), "Length of curves or selected mesh edges"),
    _command("Area", _module_function("analysis", "area", undo=False), "Area of meshes or closed planar curves"),
    _command("Volume", _module_function("analysis", "volume", undo=False), "Volume of closed meshes"),
    _command("Radius", _module_function("analysis", "radius", undo=False), "Radius in the middle of curves"),
    _command("What", _module_function("analysis", "what", undo=False), "Details of the selected objects"),
    _command("BoundingBox", in_modes(('OBJECT',), op("r2b.bounding_box")), "A box around the selection"),
    # Layers (Blender collections)
    # (Before ChangeLayer: "cha" runs Chamfer, as in Rhino's alphabetical list.)
    _command("Chamfer", op("r2b.chamfer"), "Cut the corner between two curves with a line"),
    _command("ChangeLayer", op("object.move_to_collection"), "Move selected objects to another layer",
             like=("MoveToCollection",)),
    _command("ChangeToCurrentLayer", in_modes(('OBJECT',), op("r2b.change_to_current_layer")),
             "Move the selected objects to the current layer"),
    # Typed, a new layer becomes current: what is drawn next goes there (the
    # Layers panel's button leaves the current layer as Rhino's does).
    _command("NewLayer", op("r2b.layer_new", make_current=True), "Add a layer and make it the current layer"),
    _command("SetLayerToObject", in_modes(('OBJECT',), op("r2b.set_layer_to_object")),
             "Make the layer of the selected object the current layer"),
    _command("Layer", menu("R2B_MT_layers"), "Pick the current layer (all layers are in the Layers panel)"),
    _command("AllLayersOn", _all_layers_on, "Turn all layers on"),
    # Curves
    _command("Line", op("r2b.line"), "Line from two points"),
    _command("Polyline", op("r2b.polyline"), "Polyline through points"),
    _command("Curve", op("r2b.curve"), "Curve from control points"),
    _command("InterpCrv", op("r2b.interp_curve"), "Curve through points"),
    _command("Circle", op("r2b.circle"), "Circle from center and radius"),
    _command("Arc", op("r2b.arc"), "Arc from center, start and end"),
    _command("Rectangle", op("r2b.rectangle"), "Rectangle from two corners"),
    _command("CPlane", op("r2b.cplane"), "Set the construction plane of the active viewport"),
    # Modeling aids
    _command("Snap", _toggle_setting("grid_snap", "Grid Snap"), "Toggle Grid Snap (F9)"),
    _command("Ortho", _toggle_setting("ortho", "Ortho"), "Toggle Ortho (F8)"),
    _command("Planar", _toggle_setting("planar", "Planar"), "Toggle Planar mode"),
    _command("Osnap", _toggle_setting("osnap", "Osnap"), "Toggle object snaps"),
    _command("SmartTrack", _toggle_setting("smart_track", "SmartTrack"), "Toggle SmartTrack"),
    _command("Gumball", _toggle_setting("gumball", "Gumball"), "Toggle the Gumball handles on the selection"),
    _command("GumballAlignment", _cycle_gumball_alignment, "Align the Gumball to the CPlane, the object or the world"),
    # Create (meshes, see solids.py)
    _command("Box", op("r2b.box"), "Box from two corners of its base and a height", like=("Cube",)),
    _command("Sphere", op("r2b.sphere"), "Sphere from its center and radius", like=("UVSphere",)),
    _command("Cylinder", op("r2b.cylinder"), "Cylinder from its base center, radius and end"),
    _command("Cone", op("r2b.cone"), "Cone from its base center, radius and apex"),
    _command("Plane", op("r2b.plane"), "Plane from two corners"),
    _command("Torus", op("r2b.torus"), "Torus from its center and two radii"),
    # Curve editing (after Create, so "sp" still runs Sphere, as in Rhino)
    _command("Trim", op("r2b.trim"), "Trim curves and meshes: click the parts to remove (Cap: close cut solids)",
             like=("Bisect", "KnifeProject", "IntersectKnife")),
    _command("Split", op("r2b.split"), "Split curves and meshes where cutting objects meet them, or curves at points",
             like=("Bisect", "KnifeProject", "IntersectKnife")),
    _command("Extend", op("r2b.extend"), "Extend curves to boundaries, by a length, or to points"),
    _command("Fillet", op("r2b.fillet"), "Round the corner between two curves with an arc"),
    _command("Offset", op("r2b.offset"), "Copy a curve at a distance from it"),
    # More curves and points (curves_more.py)
    _command("Ellipse", op("r2b.ellipse"), "Ellipse from its center and the ends of its axes"),
    _command("Polygon", op("r2b.polygon"), "Regular polygon from its center and a corner"),
    _command("Point", op("r2b.point"), "Place a point object", like=("AddEmpty",)),
    _command("Points", op("r2b.points"), "Place point objects"),
    _command("Divide", op("r2b.divide"), "Points along curves at equal lengths"),
    _command("DupEdge", in_modes(MESH, _module_function("curves_more", "dup_edge")),
             "Curves along the selected mesh edges"),
    _command("DupBorder", in_modes(('OBJECT',), _module_function("curves_more", "dup_border")),
             "Curves along the boundaries of open meshes"),
    # Surfaces from curves (meshes, see extrude.py)
    _command("ExtrudeCrv", op("r2b.extrude_crv"), "Extrude curves in a straight line", like=("Extrude",)),
    _command("ExtrudeSrf", op("r2b.extrude_srf"), "Extrude surfaces (meshes, or faces) into solids",
             like=("Extrude",)),
    _command("ExtrudeCrvToPoint", op("r2b.extrude_crv_to_point"), "Extrude curves to a point"),
    _command("ExtrudeCrvAlongCrv", op("r2b.extrude_crv_along_crv"), "Extrude curves along a path curve"),
    _command("Revolve", op("r2b.revolve"), "Revolve curves about an axis", like=("Spin", "ScrewModifier")),
    _command("Loft", op("r2b.loft"), "Fit a surface through curves", like=("BridgeEdgeLoops",)),
    _command("Sweep1", op("r2b.sweep1"), "Sweep a profile curve along a rail"),
    _command("Pipe", op("r2b.pipe"), "Round pipes around curves"),
    _command("ExtrudeCrvTapered", op("r2b.extrude_crv_tapered"), "Extrude closed planar curves with a draft angle"),
    _command("Slab", op("r2b.slab"), "Offset a curve to a side and extrude the outline"),
    # Solids and meshes (Blender's exact boolean, bevel, solidify, modifiers; see solid_tools.py)
    _command("BooleanUnion", in_modes(('OBJECT',), op("r2b.boolean_union")), "Join the volumes of meshes",
             like=("BooleanModifier",)),
    _command("BooleanDifference", in_modes(('OBJECT',), op("r2b.boolean_difference")),
             "Subtract the volumes of meshes from others", like=("BooleanModifier",)),
    _command("BooleanIntersection", in_modes(('OBJECT',), op("r2b.boolean_intersection")),
             "Keep where meshes overlap", like=("BooleanModifier",)),
    _command("BooleanSplit", in_modes(('OBJECT',), op("r2b.boolean_split")),
             "Split meshes into the parts inside and outside others"),
    _command("FilletEdge", in_modes(MESH, op("r2b.fillet_edge")), "Round the picked edges", like=("Bevel",)),
    _command("ChamferEdge", in_modes(MESH, op("r2b.chamfer_edge")), "Cut the picked edges flat", like=("Bevel",)),
    _command("OffsetSrf", in_modes(('OBJECT',), op("r2b.offset_srf")), "Copy meshes moved along their normals",
             like=("SolidifyModifier",)),
    _command("Shell", in_modes(MESH, op("r2b.shell")), "Remove the picked faces and thicken the rest inward",
             like=("Solidify",)),
    _command("ExtractSrf", in_modes(MESH, op("r2b.extract_srf", invoke=False)),
             "Make the picked faces a new object", like=("Separate",)),
    _command("MergeAllCoplanarFaces", in_modes(('OBJECT',), _solid_tool("merge_all_coplanar_faces")),
             "Join faces that lie in one plane", like=("LimitedDissolve",)),
    _command("ToSubD", in_modes(('OBJECT',), _solid_tool("to_subd")), "Make meshes subdivision surfaces",
             like=("SubdivisionSurfaceModifier",)),
    _command("Reflect", in_modes(('OBJECT',), op("r2b.reflect")), "Make meshes symmetric across a CPlane axis",
             like=("MirrorModifier", "Symmetrize")),
    _command("Mesh", in_modes(('OBJECT',), _solid_tool("mesh_copies")), "Mesh copies of curves, surfaces and text",
             like=("Convert",)),
    _command("ReduceMesh", in_modes(('OBJECT',), op("r2b.reduce_mesh")), "Reduce the faces of meshes",
             like=("Decimate",)),
    _command("QuadRemesh", in_modes(('OBJECT',), op("r2b.quad_remesh")), "Rebuild meshes with quads",
             like=("QuadriflowRemesh", "Remesh")),
    _command("Weld", in_modes(('OBJECT',), op("r2b.weld")), "Merge coincident mesh points, smooth across edges",
             like=("MergeByDistance",)),
    _command("Unweld", in_modes(('OBJECT',), op("r2b.unweld")), "Split meshes along sharp edges",
             like=("EdgeSplitModifier",)),
    _command("PushPull", op("r2b.push_pull"), "Push a face into a solid or pull it out",
             like=("ExtrudeManifold",)),
    _command("MoveFace", in_modes(MESH, op("r2b.move_face")), "Move faces picked with Ctrl+Shift (neighbors stretch)"),
    _command("DeleteFaces", in_modes(MESH, op("mesh.delete", type='FACE')),
             "Delete the faces picked with Ctrl+Shift, leaving a hole", blender=("DeleteFaces",)),
    _command("PlanarSrf", op("r2b.planar_srf"), "Fill closed planar curves with flat faces", like=("FillCurve",)),
    _command("Cap", op("r2b.cap", invoke=False), "Fill the planar holes of meshes", like=("FillHoles",)),
    _command("MeshAngle", op("r2b.mesh_angle"), "How finely curves become meshes (angle between mesh edges)"),
    _command("History", _toggle_setting("record_history", "Record History"),
             "Toggle Record History (results follow their input curves)"),
    # Rhino's mesh and SubD editing, done with Blender's tools (select points,
    # edges or faces in Edit Mode first).
    _command("Bevel", in_modes(MESH, op("mesh.bevel", modal=True, affect='EDGES')),
             "Bevel the selected edges", blender=("BevelEdges",), like=("FilletEdge", "ChamferEdge"),
             label="Bevel", key="Ctrl B"),
    _command("Inset", in_modes(MESH, op("mesh.inset", modal=True)), "Inset the selected faces",
             blender=("InsetFaces",), label="Inset Faces", key="I"),
    _command("InsertEdge", in_modes(MESH, op("mesh.loopcut_slide", modal=True)),
             "Insert an edge loop: hover an edge and click", blender=("LoopCut", "LoopCutAndSlide"),
             label="Loop Cut and Slide", key="Ctrl R"),
    _command("Bridge", in_modes(MESH, op("mesh.bridge_edge_loops")), "Connect two edge loops with faces",
             blender=("BridgeEdgeLoops",), label="Bridge Edge Loops"),
    _command("Fill", in_modes(MESH, op("mesh.fill")), "Fill the selected boundary edges with faces",
             label="Fill", key="Alt F"),
    _command("SubDivide", by_mode(EDIT_MESH=op("mesh.subdivide"), EDIT_CURVE=op("curve.subdivide")),
             "Subdivide the selected edges or curve segments", label="Subdivide"),
    _command("Smooth", by_mode(EDIT_MESH=op("mesh.vertices_smooth"), EDIT_CURVE=op("curve.smooth")),
             "Smooth the selected points", blender=("SmoothVertices",), label="Smooth Vertices"),
    _command("Crease", _crease(1.0), "Crease the selected edges fully", label="Edge Crease", key="Shift E"),
    _command("RemoveCrease", _crease(-1.0), "Remove the crease from the selected edges", label="Edge Crease"),
    _command("Slide", in_modes(MESH, op("transform.edge_slide", modal=True)),
             "Slide the selected edges along their neighbors", blender=("EdgeSlide",), label="Edge Slide"),
    _command("Flip", by_mode(EDIT_MESH=op("mesh.flip_normals"), EDIT_CURVE=op("curve.switch_direction"),
                             OBJECT=_flip_objects),
             "Reverse the direction of faces or curves", blender=("FlipNormals",), label="Flip"),
    _command("CopyToClipboard", op("view3d.copybuffer"), "Copy the selected objects to the clipboard",
             like=("CopyObjects",), label="Copy Objects", key="Ctrl C"),
    _command("Paste", op("view3d.pastebuffer"), "Paste objects from the clipboard", blender=("PasteObjects",),
             label="Paste Objects", key="Ctrl V"),
    # File and application
    _command("New", op("wm.read_homefile"), "New file"),
    _command("Open", op("wm.open_mainfile"), "Open a file"),
    _command("Save", op("wm.save_mainfile"), "Save"),
    _command("SaveAs", op("wm.save_as_mainfile"), "Save as"),
    _command("Render", op("render.render"), "Render", like=("RenderWindow",)),
    _command("ViewCaptureToFile", op("render.opengl"),
             "Render the viewport as an image (save it from the image window: Image > Save As)",
             label="Viewport Render Image"),
    # Rhino names for Blender's file, light and animation tools (a scene project needs them)
    _command("IncrementalSave", op("wm.save_mainfile", incremental=True), "Save under the next numbered name",
             label="Save Incremental"),
    _command("Import", menu("TOPBAR_MT_file_import"), "Import a file (Blender's File > Import menu)",
             label="Import"),
    _command("Export", menu("TOPBAR_MT_file_export"), "Export (Blender's File > Export menu)", label="Export"),
    _command("DirectionalLight", in_modes(('OBJECT',), op("object.light_add", type='SUN')),
             "Add a directional light (Blender's Sun) at the 3D cursor", label="Add Sun Light", like=("Sun",)),
    _command("PointLight", in_modes(('OBJECT',), op("object.light_add", type='POINT')),
             "Add a point light at the 3D cursor", label="Add Point Light"),
    _command("Spotlight", in_modes(('OBJECT',), op("object.light_add", type='SPOT')),
             "Add a spotlight at the 3D cursor", label="Add Spot Light"),
    _command("RectangularLight", in_modes(('OBJECT',), op("object.light_add", type='AREA')),
             "Add a rectangular light (Blender's Area light) at the 3D cursor", label="Add Area Light",
             like=("LinearLight",)),
    # Rhino's blocks are Blender's collection instances.
    _command("Insert", in_modes(('OBJECT',), op("object.collection_instance_add")),
             "Insert an instance of a collection (Rhino's block instances are Blender's collection instances)",
             label="Add Collection Instance", like=("Block",)),
    _command("PlayAnimation", op("screen.animation_play"), "Play the animation (Space in Blender; "
             "in Rhino Mode Space is Enter)", label="Play Animation", key="Space"),
    _command("Options", op("screen.userpref_show"), "Blender preferences", blender=("Preferences",)),
    _command("Commands", _list_commands, "List the available commands"),
    _command("Aliases", _show_aliases, "Edit command aliases (Rhino Mode's preferences)"),

    # --- Blender's tools under Blender's names --------------------------------
    # Mesh Edit Mode
    _tool("Extrude", by_mode(
        EDIT_MESH=op("view3d.edit_mesh_extrude_move_normal", modal=True),
        EDIT_CURVE=op("curve.extrude_move", modal=True),
        OBJECT=_extrude_objects,
    ), "Extrude (Edit Mode: E; curves: ExtrudeCrv; meshes: ExtrudeSrf)", "E", also=("ExtrudeFaces",),
        like=("ExtrudeSrf", "ExtrudeCrv")),
    _tool("ExtrudeManifold", op("view3d.edit_mesh_extrude_manifold_normal", modal=True), "Extrude Manifold",
          like=("PushPull",), modes=MESH),
    _tool("ExtrudeIndividual", op("mesh.extrude_faces_move", modal=True), "Extrude Individual Faces",
          also=("ExtrudeIndividualFaces",), modes=MESH),
    _tool("ExtrudeAlongNormals", op("view3d.edit_mesh_extrude_move_shrink_fatten", modal=True),
          "Extrude Faces Along Normals", also=("ExtrudeFacesAlongNormals",), modes=MESH),
    _tool("ExtrudeEdges", op("mesh.extrude_edges_move", modal=True), "Extrude Edges", modes=MESH),
    _tool("ExtrudeVertices", op("mesh.extrude_vertices_move", modal=True), "Extrude Vertices", modes=MESH),
    _tool("BevelVertices", op("mesh.bevel", modal=True, affect='VERTICES'), "Bevel Vertices", "Shift Ctrl B",
          modes=MESH),
    _tool("OffsetEdgeSlide", op("mesh.offset_edge_loops_slide", modal=True), "Offset Edge Slide", "Shift Ctrl R",
          modes=MESH),
    _tool("Knife", op("mesh.knife_tool", modal=True), "Knife Topology Tool", "K", modes=MESH,
          like=("Split",)),
    _tool("KnifeProject", op("mesh.knife_project"), "Knife Project", modes=MESH, like=("Project", "Split", "Trim")),
    _tool("Bisect", op("mesh.bisect", modal=True), "Bisect", modes=MESH, like=("Section", "Split", "Trim")),
    _tool("IntersectKnife", op("mesh.intersect"), "Intersect (Knife)", modes=MESH, like=("Split", "Trim")),
    _tool("IntersectBoolean", op("mesh.intersect_boolean"), "Intersect (Boolean)", modes=MESH,
          like=("BooleanUnion", "BooleanDifference", "BooleanIntersection")),
    _tool("Spin", op("mesh.spin", modal=True), "Spin", modes=MESH, like=("Revolve",)),
    _tool("Screw", op("mesh.screw"), "Screw", modes=MESH, like=("Revolve", "Helix")),
    _tool("Unsubdivide", op("mesh.unsubdivide"), "Un-Subdivide", modes=MESH),
    _tool("Dissolve", op("mesh.dissolve_mode"), "Dissolve Selection", "Ctrl X", modes=MESH),
    _tool("DissolveVertices", op("mesh.dissolve_verts"), "Dissolve Vertices", modes=MESH),
    _tool("DissolveEdges", op("mesh.dissolve_edges"), "Dissolve Edges", modes=MESH),
    _tool("DissolveFaces", op("mesh.dissolve_faces"), "Dissolve Faces", modes=MESH, like=("MergeFaces",)),
    _tool("LimitedDissolve", op("mesh.dissolve_limited"), "Limited Dissolve", modes=MESH,
          like=("MergeAllCoplanarFaces",)),
    _tool("Collapse", op("mesh.edge_collapse"), "Collapse Edges & Faces", modes=MESH,
          like=("CollapseMeshEdge", "CollapseMeshFace")),
    _tool("Merge", menu("VIEW3D_MT_edit_mesh_merge"), "Merge", "M", modes=MESH, like=("Weld", "Stitch")),
    _tool("MergeByDistance", op("mesh.remove_doubles"), "Merge by Distance", also=("RemoveDoubles",),
          modes=MESH, like=("Weld",)),
    _tool("Rip", op("mesh.rip_move", modal=True), "Rip", "V", modes=MESH, like=("Unweld",)),
    _tool("Split", op("mesh.split"), "Split", "Y", modes=MESH, like=("ExtractSrf",)),
    _tool("Separate", by_mode(EDIT_MESH=op("mesh.separate"), EDIT_CURVE=op("curve.separate")), "Separate", "P",
          like=("ExtractSrf", "SplitDisjointMesh")),
    _tool("Duplicate", by_mode(
        OBJECT=op("object.duplicate_move", modal=True),
        EDIT_MESH=op("mesh.duplicate_move", modal=True),
        EDIT_CURVE=op("curve.duplicate_move", modal=True),
    ), "Duplicate", "Shift D", also=("DuplicateObjects",), like=("Copy",)),
    _tool("DuplicateLinked", op("object.duplicate_move_linked", modal=True), "Duplicate Linked", "Alt D",
          modes=('OBJECT',), like=("Block", "Insert")),
    _tool("GridFill", op("mesh.fill_grid"), "Grid Fill", modes=MESH, like=("EdgeSrf", "Patch")),
    _tool("FillHoles", op("mesh.fill_holes"), "Fill Holes", modes=MESH, like=("FillMeshHoles", "Cap")),
    _tool("MakeFace", op("mesh.edge_face_add"), "New Edge/Face from Vertices", "F", also=("NewEdgeFace",),
          modes=MESH, like=("3DFace",)),
    _tool("Poke", op("mesh.poke"), "Poke Faces", also=("PokeFaces",), modes=MESH),
    _tool("Triangulate", op("mesh.quads_convert_to_tris"), "Triangulate Faces", "Ctrl T",
          also=("TriangulateFaces",), modes=MESH, like=("TriangulateMesh",)),
    _tool("TrisToQuads", op("mesh.tris_convert_to_quads"), "Triangles to Quads", "Alt J",
          also=("TrianglesToQuads",), modes=MESH, like=("QuadrangulateMesh",)),
    _tool("Solidify", by_mode(EDIT_MESH=op("mesh.solidify"), OBJECT=_add_modifier('SOLIDIFY')),
          "Solidify (faces in Edit Mode, a modifier in Object Mode)", also=("SolidifyFaces",),
          like=("OffsetSrf", "Shell")),
    _tool("Wireframe", op("mesh.wireframe"), "Wireframe (faces to a frame of edges)", modes=MESH),
    _tool("Symmetrize", op("mesh.symmetrize"), "Symmetrize", modes=MESH, like=("Reflect", "Symmetry")),
    _tool("ConvexHull", op("mesh.convex_hull"), "Convex Hull", modes=MESH),
    _tool("VertexSlide", op("transform.vert_slide", modal=True), "Vertex Slide", "Shift V", modes=MESH,
          like=("Slide",)),
    _tool("ShrinkFatten", op("transform.shrink_fatten", modal=True), "Shrink/Fatten", "Alt S", modes=MESH),
    _tool("ToSphere", op("transform.tosphere", modal=True), "To Sphere", "Shift Alt S", modes=MESH),
    _tool("Randomize", op("transform.vertex_random"), "Randomize", modes=MESH),
    _tool("EdgeCrease", op("transform.edge_crease", modal=True), "Edge Crease", "Shift E", modes=MESH,
          like=("Crease",)),
    _tool("MarkSeam", op("mesh.mark_seam"), "Mark Seam", modes=MESH),
    _tool("ClearSeam", op("mesh.mark_seam", clear=True), "Clear Seam", modes=MESH),
    _tool("MarkSharp", op("mesh.mark_sharp"), "Mark Sharp", modes=MESH),
    _tool("ClearSharp", op("mesh.mark_sharp", clear=True), "Clear Sharp", modes=MESH),
    _tool("RecalculateNormals", op("mesh.normals_make_consistent", inside=False), "Recalculate Outside",
          "Shift N", also=("RecalculateOutside",), modes=MESH, like=("UnifyMeshNormals",)),
    _tool("CircleSelect", in_modes(('OBJECT', 'EDIT_MESH', 'EDIT_CURVE'), op("view3d.select_circle", modal=True)),
          "Circle Select", "C"),
    _tool("SelectLinked", by_mode(EDIT_MESH=op("mesh.select_linked"), EDIT_CURVE=op("curve.select_linked")),
          "Select Linked All", "Ctrl L"),
    _tool("SelectMore", by_mode(EDIT_MESH=op("mesh.select_more"), EDIT_CURVE=op("curve.select_more")),
          "Select More", "Ctrl Numpad +"),
    _tool("SelectLess", by_mode(EDIT_MESH=op("mesh.select_less"), EDIT_CURVE=op("curve.select_less")),
          "Select Less", "Ctrl Numpad -"),
    _tool("SelectSimilar", by_mode(EDIT_MESH=menu("VIEW3D_MT_edit_mesh_select_similar"),
                                   EDIT_CURVE=op("curve.select_similar")), "Select Similar", "Shift G"),
    _tool("SelectLoops", first_op(("mesh.select_edge_loop_multi", "mesh.loop_multi_select")),
          "Select Edge Loops", also=("EdgeLoops",), modes=MESH),
    _tool("VertexMode", op("mesh.select_mode", type='VERT'), "Vertex select mode", "1", modes=MESH,
          like=("SelectionFilterVertices",)),
    _tool("EdgeMode", op("mesh.select_mode", type='EDGE'), "Edge select mode", "2", modes=MESH,
          like=("SelectionFilterEdges",)),
    _tool("FaceMode", op("mesh.select_mode", type='FACE'), "Face select mode", "3", modes=MESH,
          like=("SelectionFilterFaces",)),
    _tool("Unwrap", op("uv.unwrap"), "UV Unwrap", "U", also=("UVUnwrap",), modes=MESH, like=("UnwrapMesh",)),
    _tool("SmartUVProject", op("uv.smart_project"), "Smart UV Project", modes=MESH),
    # Objects
    _tool("ShadeSmooth", by_mode(OBJECT=op("object.shade_smooth"), EDIT_MESH=op("mesh.faces_shade_smooth")),
          "Shade Smooth"),
    _tool("ShadeFlat", by_mode(OBJECT=op("object.shade_flat"), EDIT_MESH=op("mesh.faces_shade_flat")),
          "Shade Flat"),
    _tool("ShadeAutoSmooth", op("object.shade_auto_smooth"), "Shade Auto Smooth", modes=('OBJECT',)),
    _tool("SetOrigin", op("wm.call_menu", name="R2B_MT_set_origin"), "Set Origin", modes=('OBJECT',)),
    _tool("Apply", menu("VIEW3D_MT_object_apply"), "Apply", "Ctrl A", modes=('OBJECT',)),
    _tool("ApplyScale", op("object.transform_apply", location=False, rotation=False, scale=True),
          "Apply Scale", modes=('OBJECT',)),
    _tool("ApplyRotation", op("object.transform_apply", location=False, rotation=True, scale=False),
          "Apply Rotation", modes=('OBJECT',)),
    _tool("ApplyAllTransforms", op("object.transform_apply", location=True, rotation=True, scale=True),
          "Apply All Transforms", modes=('OBJECT',)),
    _tool("ApplyModifiers", _apply_modifiers, "Apply all modifiers of the selected objects",
          modes=('OBJECT',), like=("History",)),
    _tool("ClearLocation", op("object.location_clear"), "Clear Location", "Alt G", modes=('OBJECT',)),
    _tool("ClearRotation", op("object.rotation_clear"), "Clear Rotation", "Alt R", modes=('OBJECT',)),
    _tool("ClearScale", op("object.scale_clear"), "Clear Scale", "Alt S", modes=('OBJECT',)),
    _tool("Parent", op("object.parent_set"), "Make Parent", "Ctrl P", also=("MakeParent",),
          modes=('OBJECT',)),
    _tool("ClearParent", op("object.parent_clear"), "Clear Parent", "Alt P", modes=('OBJECT',)),
    _tool("Convert", menu("VIEW3D_MT_object_convert"), "Convert", modes=('OBJECT',), like=("Mesh",)),
    _tool("Add", by_mode(OBJECT=menu("VIEW3D_MT_add"), EDIT_MESH=menu("VIEW3D_MT_mesh_add")), "Add", "Shift A"),
    _tool("Cube", op("mesh.primitive_cube_add"), "Add Cube", modes=('OBJECT', 'EDIT_MESH'), like=("Box",)),
    _tool("UVSphere", op("mesh.primitive_uv_sphere_add"), "Add UV Sphere", modes=('OBJECT', 'EDIT_MESH'),
          like=("Sphere",)),
    _tool("IcoSphere", op("mesh.primitive_ico_sphere_add"), "Add Ico Sphere", modes=('OBJECT', 'EDIT_MESH')),
    _tool("Monkey", op("mesh.primitive_monkey_add"), "Add Monkey", also=("Suzanne",), modes=('OBJECT', 'EDIT_MESH')),
    _tool("CursorToSelected", op("view3d.snap_cursor_to_selected"), "Cursor to Selected"),
    _tool("CursorToWorldOrigin", op("view3d.snap_cursor_to_center"), "Cursor to World Origin"),
    _tool("SelectionToCursor", op("view3d.snap_selected_to_cursor"), "Selection to Cursor"),
    _tool("LocalView", op("view3d.localview"), "Local View", "/", like=("Isolate",)),
    _tool("Sidebar", _toggle_region("show_region_ui", "Sidebar"), "Sidebar", "N", like=("Properties",)),
    _command("Toolbar", _toggle_region("show_region_toolbar", "Toolbar"),
             "Show or hide the viewport's toolbar", label="Toolbar", key="T"),
    _tool("XRay", op("view3d.toggle_xray"), "Toggle X-Ray", "Alt Z", like=("Ghosted",)),
    _tool("ProportionalEditing", by_mode(OBJECT=_toggle("tool_settings.use_proportional_edit_objects",
                                                        "Proportional Editing"),
                                         default=_toggle("tool_settings.use_proportional_edit",
                                                         "Proportional Editing")),
          "Proportional Editing", "O", like=("SoftMove", "SoftTransform")),
    _tool("EditMode", _mode_set('EDIT'), "Edit Mode", "Tab", like=("PointsOn",)),
    _tool("ObjectMode", _mode_set('OBJECT'), "Object Mode", "Tab", like=("PointsOff",)),
    _tool("SculptMode", _mode_set('SCULPT'), "Sculpt Mode"),
    _tool("WeightPaint", _mode_set('WEIGHT_PAINT'), "Weight Paint"),
    _tool("TexturePaint", _mode_set('TEXTURE_PAINT'), "Texture Paint"),
    _tool("VertexPaint", _mode_set('VERTEX_PAINT'), "Vertex Paint"),
    _tool("AddCamera", op("object.camera_add"), "Add Camera", modes=('OBJECT',), like=("Camera", "NamedView")),
    _tool("CameraToView", op("view3d.camera_to_view"), "Align Active Camera to View", "Ctrl Alt Numpad 0"),
    _tool("ViewCamera", op("view3d.view_camera"), "View Camera", "Numpad 0"),
    _tool("AddLight", menu("VIEW3D_MT_light_add"), "Add Light", modes=('OBJECT',),
          like=("PointLight", "Spotlight", "DirectionalLight")),
    _tool("AddModifier", menu("OBJECT_MT_modifier_add"), "Add Modifier", modes=('OBJECT', 'EDIT_MESH')),
    _tool("Search", op("wm.search_menu"), "Menu Search", "F3"),
]

# Blender's interactive versions of commands whose bare names are Rhino's
# (reached with b:, e.g. b:Scale is Blender's S).
COMMANDS += [
    _tool("Move", op("transform.translate", modal=True), "Move", "G", also=("Grab",)),
    _tool("Rotate", op("transform.rotate", modal=True), "Rotate", "R"),
    _tool("Scale", op("transform.resize", modal=True), "Scale", "S", also=("Resize",)),
    _tool("Mirror", op("transform.mirror", modal=True), "Interactive Mirror", "Ctrl M"),
    _tool("Delete", by_mode(OBJECT=op("object.delete"), EDIT_MESH=menu("VIEW3D_MT_edit_mesh_delete"),
                            EDIT_CURVE=menu("VIEW3D_MT_edit_curve_delete")), "Delete", "X"),
    _tool("Copy", op("view3d.copybuffer"), "Copy Objects", "Ctrl C", also=("CopyObjects",)),
    _tool("PushPull", op("transform.push_pull", modal=True), "Push/Pull (points toward or away from their center)",
          modes=EDIT),
    _tool("Circle", op("mesh.primitive_circle_add"), "Add Mesh Circle", modes=('OBJECT', 'EDIT_MESH')),
    _tool("Grid", op("mesh.primitive_grid_add"), "Add Grid", modes=('OBJECT', 'EDIT_MESH')),
    _tool("Snap", menu("VIEW3D_MT_snap_pie", pie=True), "Snap", "Shift S"),
]

# Modifiers, as "<Name>Modifier" and, where the name is free, plain "<Name>".
_MODIFIERS = (
    ('ARRAY', ("Array",)), ('BEVEL', ("FilletEdge",)), ('BOOLEAN', ("BooleanUnion", "BooleanDifference")),
    ('BUILD', ()), ('DECIMATE', ("ReduceMesh",)), ('EDGE_SPLIT', ("Unweld",)), ('NODES', ()), ('MASK', ()),
    ('MIRROR', ("Reflect", "Mirror")), ('MULTIRES', ()), ('REMESH', ("QuadRemesh", "ShrinkWrap")),
    ('SCREW', ("Revolve",)), ('SKIN', ("MultiPipe",)), ('SOLIDIFY', ("OffsetSrf", "Shell")),
    ('SUBSURF', ("ToSubD",)), ('TRIANGULATE', ("TriangulateMesh",)), ('WELD', ("Weld",)), ('WIREFRAME', ()),
    ('CAST', ()), ('CURVE', ("Flow",)), ('DISPLACE', ()), ('LATTICE', ("CageEdit",)), ('MESH_DEFORM', ("CageEdit",)),
    ('SHRINKWRAP', ("Pull", "Project")), ('SIMPLE_DEFORM', ("Bend", "Twist", "Taper", "Stretch")),
    ('SMOOTH', ("Smooth",)), ('CORRECTIVE_SMOOTH', ()), ('LAPLACIANSMOOTH', ()), ('SURFACE_DEFORM', ("FlowAlongSrf",)),
    ('WARP', ()), ('WAVE', ()), ('WEIGHTED_NORMAL', ()),
)


def _modifier_commands():
    names = {item.identifier: item.name for item in bpy.types.Modifier.bl_rna.properties['type'].enum_items}
    taken = {key(n) for c in COMMANDS for d in (RHINO, BLENDER) for n in c.names(d)}
    found = []
    for identifier, like in _MODIFIERS:
        if identifier not in names:
            continue
        label = names[identifier]
        base = re.sub(r"[^0-9A-Za-z]", "", label.title() if label.islower() else label)
        also = [] if key(base) in taken or key(base) in rhino_names() else [base]
        if identifier == 'SUBSURF':
            also.append("Subsurf")
        found.append(_tool(base + "Modifier", _add_modifier(identifier), f"Add a {label} modifier",
                           also=also, like=like, modes=('OBJECT', 'EDIT_MESH', 'EDIT_CURVE')))
    return found


# --- Rhino's command names ------------------------------------------------------

_rhino_names = None


def rhino_names():
    """{key: name} of Rhino 8's commands (assets/rhino_commands.txt)."""
    global _rhino_names
    if _rhino_names is None:
        path = os.path.join(os.path.dirname(__file__), "assets", "rhino_commands.txt")
        with open(path, encoding="utf-8") as f:
            _rhino_names = {key(line): line.strip() for line in f if line.strip()}
    return _rhino_names


COMMANDS += _modifier_commands()

_INDEX = {RHINO: {}, BLENDER: {}}
for _c in COMMANDS:
    for _dialect in (RHINO, BLENDER):
        for _name in _c.names(_dialect):
            _INDEX[_dialect].setdefault(key(_name), _c)


# --- Other Blender operators, by label ----------------------------------------

_operator_index = None
_operator_index_time = 0.0
_operator_labels = set()  # Blender's operator names of several words, lower case.
OPERATOR_INDEX_SECONDS = 10.0  # Rebuild after this long on a miss (add-ons may add operators).


def _operators(rebuild=False):
    """{key of label: [idname, ...]} of Blender's operators (not internal ones)."""
    global _operator_index, _operator_index_time, _operator_labels
    if _operator_index is None or (rebuild and time.monotonic() - _operator_index_time > OPERATOR_INDEX_SECONDS):
        index = {}
        labels = set()
        for module_name in dir(bpy.ops):
            if module_name.startswith("_") or module_name == "r2b":  # R2B's own are in the table.
                continue
            module = getattr(bpy.ops, module_name)
            for name in dir(module):
                if name.startswith("_"):
                    continue
                operator = getattr(module, name)
                try:
                    if 'INTERNAL' in operator.bl_options:
                        continue
                    label = operator.get_rna_type().name
                except (AttributeError, KeyError, RuntimeError):
                    continue
                index.setdefault(key(label), []).append(f"{module_name}.{name}")
                if " " in label:
                    labels.add(" ".join(label.lower().split()))
        _operator_index, _operator_index_time, _operator_labels = index, time.monotonic(), labels
    return _operator_index



def continues_with_space(text):
    """Whether Space after ``text`` types a space instead of running the
    line: on a b: line whose words are not a whole Blender name but begin
    one of several words, as Blender shows them (b:insert keyframe, b:play
    animation). Space stays Enter everywhere else, as in Rhino."""
    match = re.match(r"^\s*[bB]:(.*)$", text)
    if not match:
        return False
    words = " ".join(match.group(1).lower().split())
    k = key(words)
    # A whole name in the table runs (b:move); other operators' names are
    # only a fallback ("Insert" is the Text Editor's and the console's).
    if not k or "." in words or k in _INDEX[BLENDER]:
        return False
    _operators()
    start = words + " "
    labels = {" ".join(c.label.lower().split()) for c in COMMANDS if " " in c.label}
    return any(label.startswith(start) for label in labels | _operator_labels)


# Which operator modules to try first when several share a label.
_MODULE_ORDER = {
    'EDIT_MESH': ("mesh", "transform", "view3d", "uv", "object"),
    'EDIT_CURVE': ("curve", "transform", "view3d", "object"),
    'OBJECT': ("object", "view3d", "transform", "mesh", "curve"),
}


def _camel(label):
    return "".join(w[:1].upper() + w[1:] for w in re.split(r"[^0-9A-Za-z]+", label) if w)


def _operator_command(idnames, mode):
    """A command running the first of ``idnames`` whose poll passes."""
    order = _MODULE_ORDER.get(mode, ())
    idnames = sorted(idnames, key=lambda i: order.index(i.split(".")[0]) if i.split(".")[0] in order else len(order))
    module, name = idnames[0].split(".")
    rna = getattr(getattr(bpy.ops, module), name).get_rna_type()

    def run(context):
        for idname in idnames:
            m, n = idname.split(".")
            operator = getattr(getattr(bpy.ops, m), n)
            if operator.poll():
                # As _call: with undo=True Blender pushes the operator's undo
                # step and registers it (Adjust Last Operation); without, an
                # operator typed by its label had no options panel, and a
                # step was pushed for ones that change nothing (a view).
                _handled()
                return operator('INVOKE_DEFAULT', True)
        raise CommandError("cannot run here (it needs another mode or editor)")
    run.operators = tuple(idnames)
    return Command(_camel(rna.name), run, rna.description or rna.name, (), tuple(idnames),
                   rhino_name=False, label=f"{rna.name} ({idnames[0]})")


def _idname_command(idname):
    module, _, name = idname.partition(".")
    if not module.isidentifier() or not name.isidentifier() or module not in dir(bpy.ops):
        return None
    if name not in dir(getattr(bpy.ops, module)):
        return None
    return _operator_command([idname], None)


# --- Lookup -----------------------------------------------------------------------

@dataclass
class Found:
    command: Command = None  # What Enter runs, or None.
    message: str = ""  # Why nothing runs, or a note.
    matches: list = field(default_factory=list)  # [(name as typed, Command, dialect)] for the candidate list.


def preferred_dialect():
    from . import prefs

    try:
        return prefs.get().name_clash
    except (KeyError, AttributeError):
        return RHINO


def _strip_rhino_prefixes(text):
    """Rhino's _ (English name), - (no dialog), ! (cancel) and ' (nested) prefixes."""
    return text.lstrip("_-!'")


def _other(dialect):
    return BLENDER if dialect == RHINO else RHINO


def _like_hint(name):
    """Commands that do something like Rhino's ``name``."""
    k = key(name)
    return [c for c in COMMANDS if any(key(n) == k for n in c.like)]


def _missing_rhino(k, matches):
    name = rhino_names()[k]
    message = f"Rhino's {name} is not in Rhino Mode yet"
    blender = _INDEX[BLENDER].get(k)
    if blender is not None:
        message += f"; Blender's {blender.label or blender.name} is b:{blender.name}"
    similar = [c.name for c in _like_hint(name) if c is not blender][:3]
    if similar:
        message += f" (similar: {', '.join(similar)})"
    return Found(None, message, matches)


def lookup(text, mode=None, operators=True):
    """What the command line runs for ``text`` (see the module docstring),
    with candidates for ``mode`` (default: the current one). ``operators``:
    also look for other Blender operators by label (slower; not while
    drawing)."""
    mode = mode or bpy.context.mode
    text = _strip_rhino_prefixes(text.strip())
    forced = None
    match = re.match(r"^([bBrR]):(.*)$", text)
    if match:
        forced = BLENDER if match.group(1).lower() == "b" else RHINO
        text = _strip_rhino_prefixes(match.group(2).strip())
        if forced == BLENDER and "." in text:
            command = _idname_command(text)
            return Found(command, "" if command else f"No Blender operator {text}")
    k = key(text)
    if not k:
        return Found(None)
    preferred = preferred_dialect()
    order = (forced,) if forced else (preferred, _other(preferred))
    reserved = k in rhino_names() and not forced and preferred == RHINO

    matches = _prefix_matches(k, order, mode, reserved_check=not forced and preferred == RHINO)
    for dialect in order:
        command = _INDEX[dialect].get(k)
        if command is None:
            continue
        if dialect == BLENDER and reserved and k not in {key(n) for n in command.names(RHINO)}:
            return _missing_rhino(k, matches)
        return Found(command, "", matches)
    if reserved:
        return _missing_rhino(k, matches)
    if forced != RHINO and operators:
        idnames = _operators().get(k) or _operators(rebuild=True).get(k)
        if idnames:
            return Found(_operator_command(idnames, mode), "", matches)
    if matches:
        return Found(matches[0][1], "", matches)
    elsewhere = _prefix_matches(k, order, None, reserved_check=not forced and preferred == RHINO)
    if elsewhere:
        return Found(elsewhere[0][1], "", [])  # Running it says which mode it needs.
    if forced == BLENDER:
        return Found(None, f"No Blender command {text}")
    started = sorted(n for nk, n in rhino_names().items() if nk.startswith(k))
    if started and forced != BLENDER:
        shown = ", ".join(started[:4]) + (", ..." if len(started) > 4 else "")
        return Found(None, f"Not in Rhino Mode yet: Rhino's {shown}")
    return Found(None, f"Unknown command: {text}")


# Recently run commands, most recent first (for the "recent first" preference).
_recent = []
RECENT_LIMIT = 50


def remember(command):
    if command in _recent:
        _recent.remove(command)
    _recent.insert(0, command)
    del _recent[RECENT_LIMIT:]


def _recent_first():
    from . import prefs

    try:
        return prefs.get().recent_first
    except (KeyError, AttributeError):
        return False


def _prefix_matches(k, order, mode, reserved_check):
    found, seen = [], set()
    for dialect in order:
        for command in COMMANDS:
            if command in seen or (mode is not None and not command.available(mode)):
                continue
            for name in command.names(dialect):
                nk = key(name)
                if not nk.startswith(k):
                    continue
                if (dialect == BLENDER and reserved_check and nk in rhino_names()
                        and nk not in {key(n) for n in command.names(RHINO)}):
                    continue  # Rhino's meaning; reached with b:
                found.append((name, command, dialect))
                seen.add(command)
                break
    if _recent_first():
        # Within each dialect, recently run commands first (Rhino's way).
        rank = {command: index for index, command in enumerate(_recent)}
        found.sort(key=lambda m: (order.index(m[2]), rank.get(m[1], len(rank))))
    # An exact name first.
    found.sort(key=lambda m: key(m[0]) != k)
    return found


def typed_name(command, text):
    """The name of ``command`` that ``text`` spells (e.g. LoopCut for the
    InsertEdge command), else its main name."""
    k = key(_strip_rhino_prefixes(text.strip()).split(":")[-1])
    for dialect in (RHINO, BLENDER):
        for name in command.names(dialect):
            if key(name) == k:
                return name
    return command.name


def detail(command, name):
    """A short note on a command shown as ``name`` in the candidate list: the
    Blender tool it runs and its shortcut, and similar commands."""
    parts = []
    if command.label:
        tool = "" if key(command.label) == key(name) else f": {command.label}"
        parts.append(f"Blender{tool}" + (f" ({command.key})" if command.key else ""))
    if command.like:
        parts.append("≈ " + ", ".join(command.like[:2]))
    return "  ".join(parts)


def candidates(text, mode=None):
    """Commands the text could run, in order (the first is what Enter runs)."""
    found = lookup(text, mode)
    result = [m[1] for m in found.matches]
    if found.command is not None and found.command not in result:
        result.insert(0, found.command)
    elif found.command is not None:
        result.remove(found.command)
        result.insert(0, found.command)
    return result


def resolve(text, mode=None):
    """The command a command line entry runs, or None."""
    return lookup(text, mode).command


def alias(text):
    """(command text, inputs) of a user alias typed as ``text``, or None."""
    from . import prefs

    k = text.strip().lower()
    for entry in prefs.get().aliases:
        if entry.name.strip().lower() == k and entry.macro.strip():
            words = entry.macro.split()
            return _strip_rhino_prefixes(words[0]), [word.lstrip("_") for word in words[1:]]
    return None


# --- A menu for Set Origin (Blender's is an enum on the Object menu) ----------

class R2B_MT_set_origin(bpy.types.Menu):
    bl_label = "Set Origin"

    def draw(self, context):
        self.layout.operator_enum("object.origin_set", "type")


def register():
    bpy.utils.register_class(R2B_MT_set_origin)


def unregister():
    _recent.clear()
    bpy.utils.unregister_class(R2B_MT_set_origin)
