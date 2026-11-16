# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's selection and organizing commands (MODE feature).

Selection (they add to the selection, as in Rhino):

* SelCrv, SelMesh, SelPt (point objects: empties), SelOpen, SelClosed
  (curves whose ends meet, meshes without boundary edges), SelSmall (smaller
  than a typed size), SelDup (copies lying on top of others), SelLayer (the
  layers of the selected objects, else the current layer), SelColor (the
  colors of the selected objects).
* SelLast: what the last command made (or, if it made nothing, what it
  worked on). SelPrev: the selection before the last command.

Organizing: Lock / Unlock (not selectable), Isolate / Unisolate (hide all but
the selection, and back), HideSwap (hidden and shown swap), ShowSelected
(show hidden objects, selected), Properties (the object tab of the
properties editor).
"""

import bmesh
import numpy as np

import bpy
from mathutils import Vector

from . import command_line, curve_geom
from .getpoint import FINISH, GetPoints

# Before each command: (object names, selected object names), for SelLast and SelPrev.
_history = []
HISTORY_LIMIT = 4
_isolated = []  # Names of the objects Isolate hid.


def before_command(context):
    """Called by the command line before running a command."""
    if context.mode != 'OBJECT':
        return
    _history.append(({o.name for o in bpy.data.objects}, [o.name for o in context.selected_objects]))
    del _history[:-HISTORY_LIMIT]


def _echo(context, text, kind='OUTPUT'):
    command_line.echo(context.window, text, kind)


def _select(context, objects, what):
    count = 0
    for obj in objects:
        if obj.visible_get() and not obj.hide_select:
            obj.select_set(True)
            count += 1
    _echo(context, f"{count} {what} selected" if count else f"No {what}")


def _visible(context):
    return [o for o in context.view_layer.objects if o.visible_get()]


def _command(function):
    function.operators = ()
    return function


@_command
def sel_crv(context):
    _select(context, [o for o in _visible(context) if o.type == 'CURVE'], "curves")


@_command
def sel_mesh(context):
    _select(context, [o for o in _visible(context) if o.type == 'MESH'], "meshes")


@_command
def sel_pt(context):
    _select(context, [o for o in _visible(context) if o.type == 'EMPTY'], "points")


def is_closed(obj):
    if obj.type == 'CURVE':
        chains = [curve_geom.from_spline(s) for s in obj.data.splines]
        chains = [c for c in chains if c is not None]
        return bool(chains) and all(curve_geom.is_closed(c, curve_geom.tolerance(c)) for c in chains)
    if obj.type == 'MESH':
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        closed = len(mesh.edges) > 0 and all(e.is_manifold for e in mesh.edges)
        mesh.free()
        return closed
    return False


@_command
def sel_open(context):
    _select(context, [o for o in _visible(context) if o.type in {'CURVE', 'MESH'} and not is_closed(o)], "open objects")


@_command
def sel_closed(context):
    _select(context, [o for o in _visible(context) if o.type in {'CURVE', 'MESH'} and is_closed(o)], "closed objects")


def _signature(obj):
    """What two copies on top of each other share."""
    matrix = tuple(round(v, 6) for row in obj.matrix_world for v in row)
    if obj.type == 'MESH':
        coords = np.empty(len(obj.data.vertices) * 3)
        obj.data.vertices.foreach_get("co", coords)
        return obj.type, matrix, tuple(np.round(coords, 6))
    if obj.type == 'CURVE':
        points = tuple(round(c, 6) for s in obj.data.splines
                       for p in (s.bezier_points if s.type == 'BEZIER' else s.points)
                       for c in (p.co[:] if s.type != 'BEZIER' else (*p.co, *p.handle_left, *p.handle_right)))
        return obj.type, matrix, points
    return obj.type, matrix, ()


@_command
def sel_dup(context):
    seen = {}
    duplicates = []
    for obj in sorted(_visible(context), key=lambda o: o.name):
        key = _signature(obj)
        if key in seen:
            duplicates.append(obj)
        else:
            seen[key] = obj
    _select(context, duplicates, "duplicates")


@_command
def sel_layer(context):
    layers = {c for o in context.selected_objects for c in o.users_collection} or {context.collection}
    _select(context, [o for o in _visible(context) if set(o.users_collection) & layers], "objects on the layers")


@_command
def sel_color(context):
    colors = {tuple(round(c, 4) for c in o.color) for o in context.selected_objects}
    if not colors:
        _echo(context, "Select objects with the colors to look for", 'ERROR')
        return
    _select(context, [o for o in _visible(context) if tuple(round(c, 4) for c in o.color) in colors],
            "objects of the colors")


@_command
def sel_last(context):
    if len(_history) < 2:
        _echo(context, "No earlier command", 'ERROR')
        return
    names, selected = _history[-2]
    made = [o for o in bpy.data.objects if o.name not in names]
    for obj in context.selected_objects:
        obj.select_set(False)
    _select(context, made or [bpy.data.objects[n] for n in selected if n in bpy.data.objects], "objects")


@_command
def sel_prev(context):
    if len(_history) < 2:
        _echo(context, "No earlier selection", 'ERROR')
        return
    _, selected = _history[-2]
    for obj in context.selected_objects:
        obj.select_set(False)
    _select(context, [bpy.data.objects[n] for n in selected if n in bpy.data.objects], "objects")


class R2B_OT_sel_small(GetPoints, bpy.types.Operator):
    """Select objects smaller than a size (their bounding box diagonal)"""

    bl_idname = "r2b.sel_small"
    bl_label = "SelSmall"
    size = 1.0

    def prompt(self):
        return f"Size <{type(self).size:g}>"

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if value <= 0:
            return True
        type(self).size = value
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        small = []
        for obj in _visible(context):
            corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
            low = Vector([min(c[i] for c in corners) for i in range(3)])
            high = Vector([max(c[i] for c in corners) for i in range(3)])
            if (high - low).length < type(self).size:
                small.append(obj)
        _select(context, small, "small objects")


# --- Organizing ---------------------------------------------------------------------------

@_command
def lock(context):
    objects = list(context.selected_objects)
    for obj in objects:
        obj.select_set(False)
        obj.hide_select = True
    _echo(context, f"{len(objects)} objects locked")


@_command
def unlock(context):
    locked = [o for o in context.view_layer.objects if o.hide_select]
    for obj in locked:
        obj.hide_select = False
    _echo(context, f"{len(locked)} objects unlocked")


@_command
def isolate(context):
    if not context.selected_objects:
        _echo(context, "Select objects to keep", 'ERROR')
        return
    hidden = [o for o in context.view_layer.objects if o.visible_get() and not o.select_get()]
    for obj in hidden:
        obj.hide_set(True)
    _isolated[:] = [o.name for o in hidden]
    _echo(context, f"{len(hidden)} objects hidden")


@_command
def unisolate(context):
    shown = [bpy.data.objects[n] for n in _isolated if n in bpy.data.objects]
    for obj in shown:
        obj.hide_set(False)
    _isolated.clear()
    _echo(context, f"{len(shown)} objects shown")


@_command
def hide_swap(context):
    objects = list(context.view_layer.objects)
    hidden = [o for o in objects if o.hide_get()]
    shown = [o for o in objects if not o.hide_get()]
    for obj in shown:
        obj.select_set(False)
        obj.hide_set(True)
    for obj in hidden:
        obj.hide_set(False)


@_command
def show_selected(context):
    hidden = [o for o in context.view_layer.objects if o.hide_get()]
    for obj in hidden:
        obj.hide_set(False)
        obj.select_set(True)
    _echo(context, f"{len(hidden)} objects shown and selected")


@_command
def properties(context):
    found = False
    for area in context.window.screen.areas:
        if area.type == 'PROPERTIES':
            space = area.spaces.active
            try:
                space.context = 'OBJECT'
            except TypeError:
                pass
            area.tag_redraw()
            found = True
    if not found:
        _echo(context, "No properties editor in this layout", 'ERROR')


def unregister_state():
    _history.clear()
    _isolated.clear()


_classes = (R2B_OT_sel_small,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    unregister_state()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
