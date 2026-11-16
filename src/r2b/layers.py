# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style layers (MODE feature).

Blender collections are the layers: child collections are sublayers, the
active collection is the current layer (new objects go there, as in Rhino),
the eye hides a layer and the selectable toggle locks it. The Outliner of
the Rhino workspace is set up as the Layers panel (those two columns; see
tools/build_workspace.py), showing the objects under their layers as Blender
does (a header toggle lists layers only, as Rhino does), and its header gets
New Layer, New Sublayer and Delete Layer buttons and the current layer's
name.

Deleting a layer keeps its objects and sublayers: they move to the parent
layer. (Removing a Blender collection would leave its objects in no
collection of the scene.)

Layers made or colored in R2B have a color (custom property COLOR_PROPERTY,
edited through ``Collection.r2b_color``; new layers take the next color of
a palette, as in Rhino) and their objects are shown in it: R2B sets their
``Object.color``, and the Rhino workspace's viewports show object colors.
Objects in other collections keep their own color: materials (Object Info)
and add-ons use it (Geo-Scatter keeps each scatter system's display color
there). The color an object had before is kept (BEFORE_PROPERTY) and given
back when it leaves the colored layer, when Rhino Mode turns off and while
a file is saved, so files never keep the display colors. The collection's
color tag in the Outliner follows the nearest of Blender's eight tag colors;
the exact color is next to the current layer's name in the header. Move
Up/Down change a layer's place among its siblings.
"""

import bpy
from bpy.app.handlers import persistent

from . import workspace

DEFAULT_COLOR = (1.0, 1.0, 1.0)  # Blender's default object color: no layer color or tag.
PALETTE = (
    (0.90, 0.20, 0.20), (0.25, 0.45, 0.95), (0.20, 0.75, 0.30), (0.95, 0.80, 0.20),
    (0.75, 0.30, 0.85), (0.20, 0.80, 0.85), (0.95, 0.55, 0.15), (0.60, 0.45, 0.33),
)
# Blender's default theme colors of the collection color tags.
TAG_COLORS = {
    'COLOR_01': (0.88, 0.38, 0.38), 'COLOR_02': (0.93, 0.60, 0.28), 'COLOR_03': (0.93, 0.85, 0.35),
    'COLOR_04': (0.45, 0.80, 0.35), 'COLOR_05': (0.36, 0.55, 0.90), 'COLOR_06': (0.65, 0.42, 0.88),
    'COLOR_07': (0.88, 0.45, 0.70), 'COLOR_08': (0.60, 0.45, 0.33),
}
# A plain custom property: Blender 5 keeps properties defined with
# bpy.props apart from these (``collection.get`` does not see them), and
# the colors should stay in the file without R2B.
COLOR_PROPERTY = "r2b_layer_color"
# An object's color before R2B gave it its layer's color.
BEFORE_PROPERTY = "r2b_color_before"


def _layer_name():
    existing = {c.name for c in bpy.data.collections}
    number = 1
    while f"Layer {number:02d}" in existing:
        number += 1
    return f"Layer {number:02d}"


def _parent(root, collection):
    """The collection under ``root`` that has ``collection`` as a child."""
    if collection.name in root.children:
        return root
    for child in root.children:
        found = _parent(child, collection)
        if found is not None:
            return found
    return None


def _layer_collection(layer_collection, collection):
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _layer_collection(child, collection)
        if found is not None:
            return found
    return None


# --- Layer colors ---------------------------------------------------------------

def _all_layers(collection):
    yield collection
    for child in collection.children:
        yield from _all_layers(child)


def layer_color(collection, scene=None):
    if scene is not None and collection == scene.collection:
        return DEFAULT_COLOR
    stored = collection.get(COLOR_PROPERTY)
    if stored is not None:
        return tuple(float(c) for c in stored)
    return TAG_COLORS.get(collection.color_tag, DEFAULT_COLOR)


def _nearest_tag(color):
    return min(TAG_COLORS, key=lambda tag: sum((a - b) ** 2 for a, b in zip(TAG_COLORS[tag], color)))


def _next_palette_color(scene):
    used = {tuple(round(c, 3) for c in layer_color(layer)) for layer in _all_layers(scene.collection)
            if layer != scene.collection and layer.get(COLOR_PROPERTY) is not None}
    for color in PALETTE:
        if tuple(round(c, 3) for c in color) not in used:
            return color
    return PALETTE[len(used) % len(PALETTE)]


def set_layer_color(collection, color, scene=None):
    """Set a layer's color: its Outliner tag follows, its objects take it."""
    collection[COLOR_PROPERTY] = [float(c) for c in color[:3]]
    tag = _nearest_tag(color)
    if collection.color_tag != tag:
        collection.color_tag = tag
    scene = scene or bpy.context.scene
    if scene is not None:
        sync_colors(scene)


def _get_color(collection):
    return layer_color(collection)


def _set_color(collection, value):
    set_layer_color(collection, value)


def own_color(collection):
    """The color the layer was given in R2B, or None."""
    stored = collection.get(COLOR_PROPERTY)
    return tuple(float(c) for c in stored[:3]) if stored is not None else None


def _first_layers():
    """{object pointer: the first collection it is in}, what
    ``Object.users_collection[0]`` gives, read in one pass: that property
    looks through every collection of the file for each object (a scene of
    5000 objects took 0.9 s to color, at every change of the selection)."""
    first = {}
    for collection in bpy.data.collections:
        for obj in collection.objects:
            first.setdefault(obj.as_pointer(), collection)
    for scene in bpy.data.scenes:
        for obj in scene.collection.objects:
            first.setdefault(obj.as_pointer(), scene.collection)
    return first


def sync_colors(scene, objects=None):
    """Give objects in layers colored in R2B their layer's color, and the
    others back the color they had (only where it differs)."""
    objects = scene.objects if objects is None else objects
    if not len(objects):
        return
    first = _first_layers()
    colors = {}  # Collection pointer: its own color.
    for obj in objects:
        if not getattr(obj, "is_editable", True):
            continue  # Linked from a library.
        layer = first.get(obj.as_pointer())
        color = None
        if layer is not None:
            key = layer.as_pointer()
            if key not in colors:
                colors[key] = own_color(layer)
            color = colors[key]
        if color is None:
            if BEFORE_PROPERTY in obj:
                _give_back(obj)
            continue
        target = (*color, 1.0)
        if any(abs(a - b) > 1e-4 for a, b in zip(obj.color, target)):
            if BEFORE_PROPERTY not in obj:
                obj[BEFORE_PROPERTY] = [float(c) for c in obj.color]
            obj.color = target


def _give_back(obj):
    before = obj.get(BEFORE_PROPERTY)
    if before is not None and len(before) == 4:
        obj.color = tuple(before)
    del obj[BEFORE_PROPERTY]


def restore_colors():
    """Give every object back the color it had before R2B colored it."""
    for obj in bpy.data.objects:
        if BEFORE_PROPERTY in obj and getattr(obj, "is_editable", True):
            _give_back(obj)


# Between save_pre and save_post: the objects have their own colors back.
# Blender may evaluate the scene in between (a thumbnail rendered from the
# camera), and syncing then would color them again before the file is written.
_saving = False


@persistent
def _on_depsgraph_update(scene, depsgraph):
    if _saving:
        return
    objects = []
    for update in depsgraph.updates:
        if isinstance(update.id, (bpy.types.Collection, bpy.types.Scene)):
            sync_colors(scene)  # Layers added, objects moved between them.
            return
        if isinstance(update.id, bpy.types.Object):
            objects.append(update.id.original)
    if objects:
        sync_colors(scene, [obj for obj in objects if obj.name in scene.objects])


@persistent
def _on_load_post(*_args):
    if bpy.context.scene is not None:
        sync_colors(bpy.context.scene)


@persistent
def _on_save_pre(*_args):
    global _saving
    _saving = True
    restore_colors()  # Files keep the objects' own colors.


@persistent
def _on_save_post(*_args):
    global _saving
    _saving = False
    for scene in bpy.data.scenes:
        sync_colors(scene)


# save_post_fail (when the file could not be written) is not in every version.
_SAVE_HANDLERS = (("save_pre", _on_save_pre), ("save_post", _on_save_post), ("save_post_fail", _on_save_post))


# --- Operators --------------------------------------------------------------------

class R2B_OT_layer_new(bpy.types.Operator):
    """Add a layer (a collection); as a sublayer of the current layer if chosen"""

    bl_idname = "r2b.layer_new"
    bl_label = "New Layer"
    bl_options = {'REGISTER', 'UNDO'}

    sublayer: bpy.props.BoolProperty(name="Sublayer", description="Add inside the current layer")
    make_current: bpy.props.BoolProperty(
        name="Make Current", description="Make the new layer the current layer (what is drawn next goes there)")

    def execute(self, context):
        if self.sublayer:
            parent = context.view_layer.active_layer_collection.collection
        else:
            parent = context.scene.collection
        layer = bpy.data.collections.new(_layer_name())
        set_layer_color(layer, _next_palette_color(context.scene))
        parent.children.link(layer)
        if self.make_current:
            set_current_layer(context, layer)
            self.report({'INFO'}, f"Added {layer.name}, the current layer")
        else:
            self.report({'INFO'}, f"Added {layer.name}")
        return {'FINISHED'}


def set_current_layer(context, layer):
    """Make ``layer`` (a collection in the scene) the current layer: new
    objects go there (Blender's active collection)."""
    found = _layer_collection(context.view_layer.layer_collection, layer)
    if found is not None:
        context.view_layer.active_layer_collection = found
    return found is not None


def layer_of(context, obj):
    """The layer an object is on: its first collection in the scene."""
    in_scene = [c for c in obj.users_collection
                if c == context.scene.collection or _layer_collection(context.view_layer.layer_collection, c)]
    return in_scene[0] if in_scene else None


class R2B_OT_set_layer_to_object(bpy.types.Operator):
    """Make the layer of the selected object the current layer (Rhino's SetLayerToObject)"""

    bl_idname = "r2b.set_layer_to_object"
    bl_label = "Set Layer to Object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object if context.active_object in context.selected_objects else None
        obj = obj or next(iter(context.selected_objects), None)
        if obj is None:
            self.report({'ERROR'}, "Select an object on the layer to make current")
            return {'CANCELLED'}
        layer = layer_of(context, obj)
        if layer is None or not set_current_layer(context, layer):
            self.report({'ERROR'}, f"{obj.name} is not on a layer of this scene")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Current layer: {context.view_layer.active_layer_collection.name}")
        return {'FINISHED'}


class R2B_OT_set_current_layer(bpy.types.Operator):
    """Make this layer the current layer"""

    bl_idname = "r2b.set_current_layer"
    bl_label = "Set Current Layer"
    bl_options = {'REGISTER', 'UNDO'}

    name: bpy.props.StringProperty(name="Layer")

    def execute(self, context):
        layer = bpy.data.collections.get(self.name)
        if layer is None or not set_current_layer(context, layer):
            self.report({'ERROR'}, f"No layer {self.name} in this scene")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Current layer: {layer.name}")
        return {'FINISHED'}


class R2B_MT_layers(bpy.types.Menu):
    """The layers, to pick the current one (Rhino's Layer command; the
    Layers panel shows them all the time)"""

    bl_idname = "R2B_MT_layers"
    bl_label = "Current Layer"

    def draw(self, context):
        layout = self.layout
        current = context.view_layer.active_layer_collection.collection

        def add(parent, depth):
            for child in parent.children:
                item = layout.operator(R2B_OT_set_current_layer.bl_idname, text="   " * depth + child.name,
                                       icon='CHECKMARK' if child.collection == current else 'BLANK1')
                item.name = child.collection.name
                add(child, depth + 1)

        add(context.view_layer.layer_collection, 0)


class R2B_OT_change_to_current_layer(bpy.types.Operator):
    """Move the selected objects to the current layer (Rhino's ChangeToCurrentLayer)"""

    bl_idname = "r2b.change_to_current_layer"
    bl_label = "Change to Current Layer"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if not context.selected_objects:
            self.report({'ERROR'}, "Select objects to move to the current layer")
            return {'CANCELLED'}
        current = context.view_layer.active_layer_collection.collection
        moved = 0
        for obj in context.selected_objects:
            if list(obj.users_collection) == [current]:
                continue
            if obj.name not in current.objects:
                current.objects.link(obj)
            for collection in list(obj.users_collection):
                if collection != current:
                    collection.objects.unlink(obj)
            moved += 1
        self.report({'INFO'}, f"{moved} object(s) moved to {context.view_layer.active_layer_collection.name}")
        return {'FINISHED'}


class R2B_OT_layer_delete(bpy.types.Operator):
    """Delete the current layer, moving its objects and sublayers to the parent layer"""

    bl_idname = "r2b.layer_delete"
    bl_label = "Delete Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.view_layer.active_layer_collection.collection != context.scene.collection

    def execute(self, context):
        layer = context.view_layer.active_layer_collection.collection
        parent = _parent(context.scene.collection, layer) or context.scene.collection
        for obj in layer.objects:
            if obj.name not in parent.objects:
                parent.objects.link(obj)
        for child in layer.children:
            if child.name not in parent.children:
                parent.children.link(child)
        name = layer.name
        bpy.data.collections.remove(layer)
        context.view_layer.active_layer_collection = _layer_collection(
            context.view_layer.layer_collection, parent) or context.view_layer.layer_collection
        self.report({'INFO'}, f"Deleted {name}; its objects are now on {parent.name}")
        return {'FINISHED'}


def _layer_state(view_layer):
    """{path of collection names: (exclude, hide_viewport, holdout, indirect_only)}."""
    states = {}

    def walk(layer_collection, path):
        for child in layer_collection.children:
            key = path + (child.collection.name,)
            states[key] = (child.exclude, child.hide_viewport, child.holdout, child.indirect_only)
            walk(child, key)

    walk(view_layer.layer_collection, ())
    return states


def _restore_layer_state(view_layer, states):
    def walk(layer_collection, path):
        for child in layer_collection.children:
            key = path + (child.collection.name,)
            if key in states:
                child.exclude, child.hide_viewport, child.holdout, child.indirect_only = states[key]
            walk(child, key)

    walk(view_layer.layer_collection, ())


class R2B_OT_layer_move(bpy.types.Operator):
    """Move the current layer up or down among its sibling layers"""

    bl_idname = "r2b.layer_move"
    bl_label = "Move Layer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(items=(('UP', "Up", ""), ('DOWN', "Down", "")))

    @classmethod
    def poll(cls, context):
        return context.view_layer.active_layer_collection.collection != context.scene.collection

    def execute(self, context):
        scene = context.scene
        layer = context.view_layer.active_layer_collection.collection
        parent = _parent(scene.collection, layer) or scene.collection
        siblings = list(parent.children)
        index = siblings.index(layer)
        other = index - 1 if self.direction == 'UP' else index + 1
        if not 0 <= other < len(siblings):
            return {'CANCELLED'}
        siblings[index], siblings[other] = siblings[other], siblings[index]
        # Blender can only append children: unlink and link back the ones
        # from the change on. That resets their view layer settings (hidden,
        # excluded), so they are kept and put back.
        states = {view_layer.name: _layer_state(view_layer) for view_layer in scene.view_layers}
        moved = siblings[min(index, other):]
        for child in moved:
            parent.children.unlink(child)
        for child in moved:
            parent.children.link(child)
        for view_layer in scene.view_layers:
            _restore_layer_state(view_layer, states[view_layer.name])
        context.view_layer.active_layer_collection = _layer_collection(context.view_layer.layer_collection, layer)
        return {'FINISHED'}


def draw_layers_header(self, context):
    if not context.workspace.get(workspace.MARKER):
        return  # Only the Rhino workspace's Outliner is the Layers panel.
    layout = self.layout
    # Objects under their layers (Blender's way, the default) or layers only (Rhino's).
    layout.prop(context.space_data, "use_filter_object", text="", icon='OBJECT_DATA')
    # Blender's exclude checkboxes: an excluded layer is not even evaluated
    # (lighter than turning it off in a heavy scene).
    layout.prop(context.space_data, "show_restrict_column_enable", text="", icon='CHECKBOX_HLT')
    row = layout.row(align=True)
    # Not COLLECTION_NEW: the Outliner header already has Blender's own New
    # Collection button with that icon (it adds inside the selected collection).
    row.operator(R2B_OT_layer_new.bl_idname, text="", icon='ADD').sublayer = False
    row.operator(R2B_OT_layer_new.bl_idname, text="", icon='NEWFOLDER').sublayer = True
    row.operator(R2B_OT_layer_delete.bl_idname, text="", icon='TRASH')
    row.operator(R2B_OT_layer_move.bl_idname, text="", icon='TRIA_UP').direction = 'UP'
    row.operator(R2B_OT_layer_move.bl_idname, text="", icon='TRIA_DOWN').direction = 'DOWN'
    current = context.view_layer.active_layer_collection.collection
    if current != context.scene.collection:
        layout.prop(current, "r2b_color", text="")
    layout.label(text=context.view_layer.active_layer_collection.name, icon='CHECKMARK')


_classes = (R2B_OT_layer_new, R2B_OT_layer_delete, R2B_OT_layer_move, R2B_OT_set_layer_to_object,
            R2B_OT_set_current_layer, R2B_MT_layers, R2B_OT_change_to_current_layer)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Collection.r2b_color = bpy.props.FloatVectorProperty(
        name="Layer Color", description="The layer's color; its objects are shown in it",
        subtype='COLOR', size=3, min=0.0, max=1.0, get=_get_color, set=_set_color)
    bpy.types.OUTLINER_HT_header.append(draw_layers_header)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    bpy.app.handlers.load_post.append(_on_load_post)
    for name, function in _SAVE_HANDLERS:
        if hasattr(bpy.app.handlers, name):
            getattr(bpy.app.handlers, name).append(function)
    if bpy.context.scene is not None:
        sync_colors(bpy.context.scene)


def unregister():
    global _saving
    _saving = False
    for handlers, function in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
                               (bpy.app.handlers.load_post, _on_load_post),
                               *((getattr(bpy.app.handlers, name), function)
                                 for name, function in _SAVE_HANDLERS if hasattr(bpy.app.handlers, name))):
        if function in handlers:
            handlers.remove(function)
    restore_colors()
    bpy.types.OUTLINER_HT_header.remove(draw_layers_header)
    del bpy.types.Collection.r2b_color  # The colors stay in the file as custom properties.
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
