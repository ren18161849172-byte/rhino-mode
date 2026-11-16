# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino's solid and mesh editing commands on Blender meshes (MODE feature).

* BooleanUnion (objects), BooleanDifference, BooleanIntersection and
  BooleanSplit (objects, then the cutting objects): Blender's exact boolean,
  applied. DeleteInput (default Yes) removes the cutters. With Record
  History on, the Boolean modifiers stay instead (the result follows the
  cutters, which stay as wireframes and out of renders).
* FilletEdge (round, segments from Mesh Angle) and ChamferEdge (flat) on the
  edges picked with Ctrl+Shift: Blender's bevel.
* OffsetSrf: a copy of open meshes moved along their normals (Solid=Yes:
  thickened into a solid). Shell: remove the picked faces of a closed mesh
  and thicken the rest inward.
* ExtractSrf: the picked faces become a new object (Copy=Yes keeps them).
* MergeAllCoplanarFaces: faces in one plane joined into one.
* ToSubD: a subdivision surface modifier. Reflect: live symmetry across the
  CPlane's x or y axis (Mirror modifier). Mesh: a mesh copy of curves,
  surfaces or text. ReduceMesh (percent of faces), QuadRemesh (target face
  count, QuadriFlow), Weld and Unweld (normals across edges by angle).
"""

import math

import bmesh

import bpy
from mathutils import Matrix

from . import command_line, settings, subobject
from .getpoint import FINISH, GetPoints

TOLERANCE = 1e-9


def world_lengths(obj):
    """How to work in world lengths on an object's mesh: (factor, None) for
    an evenly scaled object, a world length times ``factor`` being the local
    one; (1.0, matrix) for an unevenly scaled one (Scale1D leaves objects
    so): its mesh times ``matrix`` has the world's proportions, to work on
    and bring back with the inverse. (Dividing by the largest scale, as
    before, made a fillet of 1 on an object scaled 5, 5, 2.5 only 0.5 along
    z; a mirrored object's scale is negative.)"""
    scale = [abs(s) for s in obj.matrix_world.to_scale()]
    largest = max(scale)
    if largest < 1e-12:
        return 1.0, None
    if largest - min(scale) <= 1e-6 * largest:
        return 1.0 / largest, None
    return 1.0, Matrix.Diagonal((*scale, 1.0))


def boolean(context, obj, cutter, operation):
    """Apply an exact boolean of ``cutter`` to ``obj`` (other modifiers of
    ``obj`` are left out and kept). False, with ``obj`` as it was, when
    nothing would be left of it (an intersection of meshes that do not
    overlap, a difference that takes everything)."""
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    # An object whose shape is made by its modifiers (an extrusion with
    # Record History has no mesh of its own): what they make is cut, and is
    # its mesh from then on. Left out, nothing was cut and the cutter deleted.
    generated = not len(obj.data.polygons) and any(m.show_viewport for m in obj.modifiers)
    others = [] if generated else [m for m in obj.modifiers if m.show_viewport]
    for modifier in others:
        modifier.show_viewport = False
    modifier = obj.modifiers.new("R2B Boolean", 'BOOLEAN')
    modifier.object = cutter
    modifier.operation = operation
    modifier.solver = 'EXACT'
    try:
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        result = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
    finally:
        obj.modifiers.remove(modifier)
        for other in others:
            other.show_viewport = True
    if not len(result.polygons):
        bpy.data.meshes.remove(result)
        return False
    mesh = bmesh.new()
    mesh.from_mesh(result)
    mesh.to_mesh(obj.data)
    mesh.free()
    bpy.data.meshes.remove(result)
    if generated:
        obj.modifiers.clear()
    obj.data.update()
    return True


def live_boolean(obj, cutter, operation):
    """Record History: a Boolean modifier on ``obj`` that stays; the
    cutter shows as a wireframe and is left out of renders."""
    modifier = obj.modifiers.new("Boolean", 'BOOLEAN')
    modifier.object = cutter
    modifier.operation = operation
    modifier.solver = 'EXACT'
    cutter.display_type = 'WIRE'
    cutter.hide_render = True
    return modifier


def _history_note(window):
    command_line.echo(window, "History: Boolean modifiers keep the result linked to the cutters (wireframes); "
                              "ApplyModifiers makes it final")


def _copy(context, obj, name=None):
    duplicate = obj.copy()
    duplicate.data = obj.data.copy()
    if name:
        duplicate.name = name
    for collection in obj.users_collection:
        collection.objects.link(duplicate)
    return duplicate


class _TwoSets(GetPoints):
    """Select objects, Enter, select the cutting objects, Enter."""

    first_prompt = "Select objects"
    second_prompt = "Select cutting objects"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.first = None
        self.delete_input = True
        self.failed = []  # Names of the objects a boolean would have left nothing of.
        self.history = settings.get(context).record_history
        return super().invoke(context, event)

    def needs_selection(self, context):
        return True

    def prompt(self):
        text = self.first_prompt if self.first is None else self.second_prompt
        if self.history:
            return f"{text}. Press Enter when done"  # The cutters stay.
        return f"{text}. Press Enter when done ( DeleteInput={'Yes' if self.delete_input else 'No'} )"

    def on_option(self, context, name):
        if "deleteinput".startswith(name):
            self.delete_input = not self.delete_input
            return True
        return False

    def selection_done(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if self.first is None:
            if not meshes:
                command_line.echo(self.window, "Select meshes", 'ERROR')
                return False
            self.first = meshes
            for obj in context.selected_objects:
                obj.select_set(False)
            return False
        cutters = [o for o in meshes if o not in self.first]
        if not cutters:
            command_line.echo(self.window, "Select other meshes", 'ERROR')
            return False
        self.cutters = cutters
        self.run(context)
        return FINISH

    def remove_cutters(self):
        if self.history:
            _history_note(self.window)
        elif self.failed:
            # A boolean that would have left nothing was not done: the
            # cutters stay too (deleted, both objects would be gone).
            command_line.echo(self.window, f"{self.nothing_left}: {', '.join(self.failed)} left as "
                              f"{'it was' if len(self.failed) == 1 else 'they were'}, the cutting meshes kept", 'ERROR')
        elif self.delete_input:
            for obj in self.cutters:
                bpy.data.objects.remove(obj)

    nothing_left = "Nothing would be left"

    def apply(self, context, obj, cutter, operation):
        """True if the boolean was done (always with History: it stays live)."""
        if self.history:
            live_boolean(obj, cutter, operation)
            return True
        done = boolean(context, obj, cutter, operation)
        if not done and obj.name not in self.failed:
            self.failed.append(obj.name)
        return done

    def create(self, context):
        pass


class R2B_OT_boolean_union(GetPoints, bpy.types.Operator):
    """Join the volumes of meshes into one"""

    bl_idname = "r2b.boolean_union"
    bl_label = "BooleanUnion"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def needs_selection(self, context):
        return len([o for o in context.selected_objects if o.type == 'MESH']) < 2

    def prompt(self):
        return "Select meshes to join. Press Enter when done"

    def selection_done(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if len(meshes) < 2:
            command_line.echo(self.window, "Select two or more meshes", 'ERROR')
            return False
        target = context.view_layer.objects.active if context.view_layer.objects.active in meshes else meshes[0]
        history = settings.get(context).record_history
        for other in meshes:
            if other is target:
                continue
            if history:
                live_boolean(target, other, 'UNION')
            else:
                boolean(context, target, other, 'UNION')
                bpy.data.objects.remove(other)
        if history:
            _history_note(self.window)
        return FINISH

    def create(self, context):
        pass


class R2B_OT_boolean_difference(_TwoSets, bpy.types.Operator):
    """Subtract the volumes of cutting meshes from meshes"""

    bl_idname = "r2b.boolean_difference"
    bl_label = "BooleanDifference"
    first_prompt = "Select meshes to subtract from"
    second_prompt = "Select meshes to subtract with"

    def run(self, context):
        for obj in self.first:
            for cutter in self.cutters:
                self.apply(context, obj, cutter, 'DIFFERENCE')
        self.remove_cutters()


class R2B_OT_boolean_intersection(_TwoSets, bpy.types.Operator):
    """Keep only where meshes overlap the second set"""

    bl_idname = "r2b.boolean_intersection"
    bl_label = "BooleanIntersection"
    first_prompt = "Select first set of meshes"
    second_prompt = "Select second set of meshes"
    nothing_left = "The meshes do not overlap"

    def run(self, context):
        for obj in self.first:
            for cutter in self.cutters:
                self.apply(context, obj, cutter, 'INTERSECT')
        self.remove_cutters()


class R2B_OT_boolean_split(_TwoSets, bpy.types.Operator):
    """Split meshes into the parts inside and outside cutting meshes"""

    bl_idname = "r2b.boolean_split"
    bl_label = "BooleanSplit"
    first_prompt = "Select meshes to split"
    second_prompt = "Select cutting meshes"

    def invoke(self, context, event):
        result = super().invoke(context, event)
        self.delete_input = False
        return result

    def run(self, context):
        for obj in self.first:
            for cutter in self.cutters:
                if self.history:
                    inside = _copy(context, obj, obj.name + " Inside")
                    live_boolean(inside, cutter, 'INTERSECT')
                    live_boolean(obj, cutter, 'DIFFERENCE')
                    continue
                inside = _copy(context, obj, obj.name + " Inside")
                # Nothing inside this cutter, or nothing outside it: there
                # is no second part (an empty object was left for it).
                if not boolean(context, inside, cutter, 'INTERSECT') or not boolean(context, obj, cutter, 'DIFFERENCE'):
                    bpy.data.objects.remove(inside)
        self.remove_cutters()


# --- Edges, faces, offsets -------------------------------------------------------------

class _EditMeshNumber(GetPoints):
    """In Edit Mode, on the picked edges or faces: a number, then done."""

    default = 1.0
    what = "edges"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'EDIT_MESH'

    def selection_done(self, context):
        # (One object for each mesh: a mesh shared by two would be filleted twice.)
        self.meshes = [obj for obj in context.objects_in_mode_unique_data if obj.type == 'MESH']
        if not any(self.chosen(bmesh.from_edit_mesh(obj.data)) for obj in self.meshes):
            command_line.echo(self.window, f"Pick {self.what} with Ctrl+Shift+click first", 'ERROR')
            return False
        return True

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if value <= 0:
            command_line.echo(self.window, "Must be more than zero", 'ERROR')
            return True
        type(self).default = value
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        for obj in self.meshes:
            mesh = bmesh.from_edit_mesh(obj.data)
            if self.chosen(mesh):
                self.edit(context, obj, mesh, type(self).default)
                bmesh.update_edit_mesh(obj.data)


class _Bevel(_EditMeshNumber):
    round_profile = True

    def prompt(self):
        return f"{'Radius' if self.round_profile else 'Distance'} <{type(self).default:.3f}>"

    def chosen(self, mesh):
        return [e for e in mesh.edges if e.select]

    def edit(self, context, obj, mesh, value):
        segments = max(2, math.ceil((math.pi / 2) / settings.get(context).mesh_angle)) if self.round_profile else 1
        factor, stretch = world_lengths(obj)
        if stretch is not None:
            bmesh.ops.transform(mesh, matrix=stretch, verts=mesh.verts)
            mesh.normal_update()
        bmesh.ops.bevel(mesh, geom=self.chosen(mesh), offset=value * factor, offset_type='OFFSET', segments=segments,
                        profile=0.5, affect='EDGES', clamp_overlap=True)
        if stretch is not None:
            bmesh.ops.transform(mesh, matrix=stretch.inverted(), verts=mesh.verts)
            mesh.normal_update()


class R2B_OT_fillet_edge(_Bevel, bpy.types.Operator):
    """Round the edges picked with Ctrl+Shift (Blender's bevel)"""

    bl_idname = "r2b.fillet_edge"
    bl_label = "FilletEdge"


class R2B_OT_chamfer_edge(_Bevel, bpy.types.Operator):
    """Cut the edges picked with Ctrl+Shift flat (Blender's bevel, one segment)"""

    bl_idname = "r2b.chamfer_edge"
    bl_label = "ChamferEdge"
    round_profile = False


class R2B_OT_shell(_EditMeshNumber, bpy.types.Operator):
    """Remove the faces picked with Ctrl+Shift and thicken the rest inward"""

    bl_idname = "r2b.shell"
    bl_label = "Shell"
    what = "faces to remove"

    def prompt(self):
        return f"Thickness <{type(self).default:.3f}>"

    def chosen(self, mesh):
        return [f for f in mesh.faces if f.select]

    def create(self, context):
        targets = []
        for obj in self.meshes:
            mesh = bmesh.from_edit_mesh(obj.data)  # Kept: its faces die with it.
            targets.append((obj, [f.index for f in mesh.faces if f.select]))
        bpy.ops.object.mode_set(mode='OBJECT')
        thickness = type(self).default
        for obj, indices in targets:
            if not indices:
                continue
            mesh = bmesh.new()
            mesh.from_mesh(obj.data)
            mesh.faces.ensure_lookup_table()
            bmesh.ops.delete(mesh, geom=[mesh.faces[i] for i in indices], context='FACES')
            mesh.to_mesh(obj.data)
            mesh.free()
            _solidify_apply(context, obj, thickness, offset=-1.0)
        if subobject.entered():
            subobject.leave(context)


def _solidify_apply(context, obj, thickness, offset):
    """Thicken the object's mesh by ``thickness`` (a world length)."""
    factor, stretch = world_lengths(obj)
    others = [m for m in obj.modifiers if m.show_viewport]
    for modifier in others:
        modifier.show_viewport = False
    if stretch is not None:
        obj.data.transform(stretch)  # World proportions while it is thickened.
        obj.data.update()
    modifier = obj.modifiers.new("R2B Solidify", 'SOLIDIFY')
    modifier.thickness = thickness * factor
    modifier.offset = offset
    modifier.use_even_offset = True
    try:
        context.view_layer.update()
        result = bpy.data.meshes.new_from_object(obj.evaluated_get(context.evaluated_depsgraph_get()))
    except Exception:
        if stretch is not None:
            obj.data.transform(stretch.inverted())
        raise
    finally:
        obj.modifiers.remove(modifier)
        for other in others:
            other.show_viewport = True
    mesh = bmesh.new()
    mesh.from_mesh(result)
    if stretch is not None:
        bmesh.ops.transform(mesh, matrix=stretch.inverted(), verts=mesh.verts)
    mesh.to_mesh(obj.data)
    mesh.free()
    bpy.data.meshes.remove(result)
    obj.data.update()


class R2B_OT_offset_srf(GetPoints, bpy.types.Operator):
    """Copy meshes moved along their normals (Solid: thicken into a solid)"""

    bl_idname = "r2b.offset_srf"
    bl_label = "OffsetSrf"
    default = 1.0

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        self.solid = False
        self.delete_input = False
        return super().invoke(context, event)

    def needs_selection(self, context):
        return not any(o.type == 'MESH' for o in context.selected_objects)

    def selection_done(self, context):
        self.meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not self.meshes:
            command_line.echo(self.window, "Select meshes", 'ERROR')
            return False
        return True

    def prompt(self):
        if self.selecting:
            return "Select surfaces (meshes) to offset. Press Enter when done"
        return (f"Offset distance <{type(self).default:.3f}> ( Solid={'Yes' if self.solid else 'No'} "
                f"DeleteInput={'Yes' if self.delete_input else 'No'} )")

    def on_option(self, context, name):
        if "solid".startswith(name):
            self.solid = not self.solid
            return True
        if "deleteinput".startswith(name):
            self.delete_input = not self.delete_input
            return True
        return False

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if abs(value) < TOLERANCE:
            command_line.echo(self.window, "The distance must not be zero", 'ERROR')
            return True
        type(self).default = value
        return FINISH

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        distance = type(self).default
        for obj in self.meshes:
            copy = _copy(context, obj, obj.name + " Offset")
            if self.solid:
                _solidify_apply(context, copy, abs(distance), offset=1.0 if distance > 0 else -1.0)
            else:
                factor, stretch = world_lengths(copy)
                mesh = bmesh.new()
                mesh.from_mesh(copy.data)
                if stretch is not None:
                    bmesh.ops.transform(mesh, matrix=stretch, verts=mesh.verts)
                    mesh.normal_update()
                # Along the point's normal, farther at an edge between
                # faces at an angle: every face ends up at the distance (as
                # Solid does), not just the points.
                moves = [v.normal * (distance * factor * v.calc_shell_factor()) for v in mesh.verts]
                for vertex, move in zip(mesh.verts, moves):
                    vertex.co += move
                if stretch is not None:
                    bmesh.ops.transform(mesh, matrix=stretch.inverted(), verts=mesh.verts)
                mesh.to_mesh(copy.data)
                mesh.free()
                copy.data.update()
            copy.select_set(False)
            if self.delete_input:
                bpy.data.objects.remove(obj)


class R2B_OT_extract_srf(bpy.types.Operator):
    """Make the faces picked with Ctrl+Shift a new object"""

    bl_idname = "r2b.extract_srf"
    bl_label = "ExtractSrf"
    bl_options = {'REGISTER', 'UNDO'}

    copy: bpy.props.BoolProperty(name="Copy", description="Keep the faces in the original too")

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        found = False
        for obj in list(context.objects_in_mode_unique_data):
            mesh = bmesh.from_edit_mesh(obj.data)
            if not any(f.select for f in mesh.faces):
                continue
            found = True
            if self.copy:
                bmesh.ops.duplicate(mesh, geom=[f for f in mesh.faces if f.select])
                bmesh.update_edit_mesh(obj.data)
        if not found:
            command_line.echo(context.window, "Pick faces with Ctrl+Shift+click first", 'ERROR')
            return {'CANCELLED'}
        bpy.ops.mesh.separate(type='SELECTED')
        if subobject.entered():
            subobject.leave(context)
        return {'FINISHED'}


def _for_meshes(function):
    def run(context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not meshes:
            from .commands import CommandError

            raise CommandError("select meshes")
        for obj in meshes:
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            mesh = bmesh.new()
            mesh.from_mesh(obj.data)
            function(context, obj, mesh)
            mesh.to_mesh(obj.data)
            mesh.free()
            obj.data.update()
    run.operators = ()
    return run


def _merge_coplanar(context, obj, mesh):
    # 0.1 degree: the normals of faces in one plane differ by float noise (1e-4 rad is too strict).
    bmesh.ops.dissolve_limit(mesh, angle_limit=math.radians(0.1), verts=list(mesh.verts), edges=list(mesh.edges))


class _ObjectNumber(GetPoints):
    """In Object Mode, on the selected meshes: a number."""

    default = 50.0
    question = "Value"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def needs_selection(self, context):
        return not any(o.type == 'MESH' for o in context.selected_objects)

    def selection_done(self, context):
        self.meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not self.meshes:
            command_line.echo(self.window, "Select meshes", 'ERROR')
            return False
        return True

    def prompt(self):
        if self.selecting:
            return "Select meshes. Press Enter when done"
        return f"{self.question} <{type(self).default:g}>"

    def on_point(self, context):
        self.points.pop()
        return None

    def on_number(self, context, value):
        if not self.valid(value):
            return True
        type(self).default = value
        return FINISH

    def valid(self, value):
        return value > 0

    def on_enter(self, context):
        return FINISH

    def create(self, context):
        for obj in self.meshes:
            self.edit(context, obj, type(self).default)


class R2B_OT_reduce_mesh(_ObjectNumber, bpy.types.Operator):
    """Reduce the number of faces of meshes (percent to keep)"""

    bl_idname = "r2b.reduce_mesh"
    bl_label = "ReduceMesh"
    question = "Percent of faces to keep"

    def valid(self, value):
        if not 0 < value < 100:
            command_line.echo(self.window, "Between 0 and 100", 'ERROR')
            return False
        return True

    def edit(self, context, obj, percent):
        modifier = obj.modifiers.new("R2B Decimate", 'DECIMATE')
        modifier.ratio = percent / 100
        with context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
            bpy.ops.object.modifier_move_to_index(modifier=modifier.name, index=0)
            bpy.ops.object.modifier_apply(modifier=modifier.name)


class R2B_OT_quad_remesh(_ObjectNumber, bpy.types.Operator):
    """Rebuild meshes with quads (target number of faces)"""

    bl_idname = "r2b.quad_remesh"
    bl_label = "QuadRemesh"
    default = 2000.0
    question = "Target number of quads"

    def edit(self, context, obj, count):
        with context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
            bpy.ops.object.quadriflow_remesh(target_faces=int(count), mode='FACES')


class R2B_OT_weld(_ObjectNumber, bpy.types.Operator):
    """Merge points in the same place where the faces meet at less than an angle"""

    bl_idname = "r2b.weld"
    bl_label = "Weld"
    default = 180.0
    question = "Angle tolerance"

    def valid(self, value):
        return 0 <= value <= 180

    def edit(self, context, obj, angle):
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        bmesh.ops.remove_doubles(mesh, verts=mesh.verts, dist=1e-6)
        limit = math.radians(angle)
        for face in mesh.faces:
            face.smooth = True
        for edge in mesh.edges:
            edge.smooth = not (len(edge.link_faces) == 2 and edge.calc_face_angle(0.0) > limit - 1e-9) or angle >= 180
        mesh.to_mesh(obj.data)
        mesh.free()
        obj.data.update()


class R2B_OT_unweld(_ObjectNumber, bpy.types.Operator):
    """Split meshes along edges where the faces meet at more than an angle"""

    bl_idname = "r2b.unweld"
    bl_label = "Unweld"
    default = 0.0
    question = "Angle tolerance"

    def valid(self, value):
        return 0 <= value <= 180

    def edit(self, context, obj, angle):
        mesh = bmesh.new()
        mesh.from_mesh(obj.data)
        limit = math.radians(angle)
        sharp = [e for e in mesh.edges if len(e.link_faces) == 2 and e.calc_face_angle(0.0) > limit + 1e-9]
        bmesh.ops.split_edges(mesh, edges=sharp)
        mesh.to_mesh(obj.data)
        mesh.free()
        obj.data.update()


def to_subd(context):
    """Rhino's ToSubD: meshes get a subdivision surface modifier, smooth shaded."""
    meshes = [o for o in context.selected_objects if o.type == 'MESH']
    if not meshes:
        from .commands import CommandError

        raise CommandError("select meshes")
    for obj in meshes:
        if not any(m.type == 'SUBSURF' for m in obj.modifiers):
            modifier = obj.modifiers.new("SubD", 'SUBSURF')
            modifier.levels = modifier.render_levels = 2
        for polygon in obj.data.polygons:
            polygon.use_smooth = True


to_subd.operators = ()


def mesh_copies(context):
    """Mesh copies of the selected curves, surfaces and text (Rhino's Mesh)."""
    made = []
    for obj in [o for o in context.selected_objects if o.type in {'CURVE', 'SURFACE', 'FONT', 'META'}]:
        depsgraph = context.evaluated_depsgraph_get()
        data = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
        if not len(data.polygons) and not len(data.edges):
            bpy.data.meshes.remove(data)
            continue
        copy = bpy.data.objects.new(obj.name + " Mesh", data)
        copy.matrix_world = obj.matrix_world
        context.collection.objects.link(copy)
        made.append(copy)
    if not made:
        from .commands import CommandError

        raise CommandError("select curves, surfaces or text with faces")
    return made


mesh_copies.operators = ()
merge_all_coplanar_faces = _for_meshes(_merge_coplanar)


class R2B_OT_reflect(GetPoints, bpy.types.Operator):
    """Make meshes symmetric across the CPlane's x or y axis (live, Mirror modifier)"""

    bl_idname = "r2b.reflect"
    bl_label = "Reflect"

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def needs_selection(self, context):
        return not any(o.type == 'MESH' for o in context.selected_objects)

    def selection_done(self, context):
        self.meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not self.meshes:
            command_line.echo(self.window, "Select meshes", 'ERROR')
            return False
        return True

    def prompt(self):
        if self.selecting:
            return "Select meshes to make symmetric. Press Enter when done"
        return "Reflection axis ( XAxis YAxis )"

    def on_option(self, context, name):
        for option, axis in (("xaxis", 0), ("yaxis", 1)):
            if option.startswith(name) and len(name) > 1:
                self.axis = axis
                return FINISH
        return False

    def on_point(self, context):
        self.points.pop()
        return None

    def on_enter(self, context):
        self.axis = 0
        return FINISH

    def create(self, context):
        plane = self.plane
        rotation = Matrix((plane.x_axis, plane.y_axis, plane.normal)).transposed().to_4x4()
        center = bpy.data.objects.new("Reflect Plane", None)
        center.empty_display_type = 'ARROWS'
        center.matrix_world = Matrix.Translation(plane.origin) @ rotation
        context.collection.objects.link(center)
        for obj in self.meshes:
            modifier = obj.modifiers.new("Reflect", 'MIRROR')
            # Across the CPlane's x axis: mirror the y coordinate (and the other way round).
            modifier.use_axis = (self.axis == 1, self.axis == 0, False)
            modifier.mirror_object = center
            modifier.use_clip = True


_classes = (R2B_OT_boolean_union, R2B_OT_boolean_difference, R2B_OT_boolean_intersection, R2B_OT_boolean_split,
            R2B_OT_fillet_edge, R2B_OT_chamfer_edge, R2B_OT_shell, R2B_OT_offset_srf, R2B_OT_extract_srf,
            R2B_OT_reduce_mesh, R2B_OT_quad_remesh, R2B_OT_weld, R2B_OT_unweld, R2B_OT_reflect)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
