# SPDX-License-Identifier: GPL-3.0-or-later
"""Picking points for drawing commands, like Rhino's GetPoint.

``GetPoints`` is a base for modal operators that ask for a series of points:

* The point under the mouse snaps to objects (Osnap, see osnap.py; Alt held
  skips it, and typing a mode name such as "end" snaps the next point with
  that mode only). Otherwise it is where the view ray meets the viewport's
  construction plane (see cplane.py; with Planar on, the plane parallel to
  it through the previous point), lined up with points snapped to
  earlier in the command (SmartTrack) or else with Grid Snap and Ortho
  (Shift toggles it while held). A typed angle or distance constraint
  applies to all of these.
* LMB picks the point. RMB click, Enter and Space are Enter: they read what
  is typed in the command line (coordinates, a distance, an angle or an
  option, see coords.py) or, with nothing typed, end the command. Esc clears
  what is typed, or cancels the command.
* Typing goes to the command line (also from the number pad); other events
  pass through, so the views can still be navigated while picking.
* Commands that work on objects can ask for them first (``needs_selection``):
  until Enter, the mouse and keys select objects as usual; typed options
  still go to the command.
* Commands can pick curves instead of points (``picks_curves``, e.g. Trim):
  the mouse hovers and clicks on curves, see ``on_hover`` and ``on_pick``.
* A rubber band from the points to the mouse is drawn in all viewports, and
  the status bar shows the point in CPlane coordinates next to the modeling
  aid toggles. The options of the prompt ("( Copy=Yes Undo )") are buttons
  there too: clicking one is typing it and pressing Enter.

Subclasses implement ``prompt()``, ``on_point()``, ``on_enter()``,
``on_option()`` and ``create()``, and may override ``on_number()``,
``after_track()``, ``constrain_point()``, ``preview()``, ``snap_geometry()``
and ``on_cancel()``.
"""

import re
import traceback

import blf
import gpu
from gpu_extras.batch import batch_for_shader

import bpy
from bpy_extras.view3d_utils import location_3d_to_region_2d

from . import command_line, coords, cplane, osnap, settings

FINISH = 'FINISH'
CANCEL = 'CANCEL'

_active = None  # The running GetPoints operator, for the draw callbacks.
_draw_handles = []

# Regions over the 3D View main region; clicks there belong to the UI.
_UI_REGIONS = {'HEADER', 'TOOL_HEADER', 'TOOLS', 'UI', 'HUD', 'ASSET_SHELF', 'ASSET_SHELF_HEADER'}

# SmartTrack remembers this many snapped points per command.
TRACKING_POINTS = 4


def _viewport_under(window, x, y):
    """(area, main region) of the 3D viewport at window coordinates, or
    (None, None) over other editors and over a viewport's own UI."""
    for area in window.screen.areas:
        if area.type != 'VIEW_3D' or not (area.x <= x < area.x + area.width and area.y <= y < area.y + area.height):
            continue
        for region in area.regions:
            if (region.type in _UI_REGIONS and region.width > 1 and region.height > 1
                    and region.x <= x < region.x + region.width and region.y <= y < region.y + region.height):
                return None, None
        main = next(r for r in area.regions if r.type == 'WINDOW')
        return area, main
    return None, None


def prompt_options(prompt):
    """The options at the end of a prompt, e.g. ["Copy=Yes", "Undo"] for
    "Start of mirror plane ( Copy=Yes Undo )"."""
    found = re.search(r"\(\s*([^()]*?)\s*\)\s*:?\s*$", prompt)
    return found.group(1).split() if found else []


def _tag_viewports(window):
    for area in window.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


class GetPoints:
    """Base for modal operators that pick points (mix in before Operator)."""

    bl_options = {'REGISTER', 'UNDO'}

    # Take the point when the left button is released (for drags) instead
    # of when it is pressed.
    pick_on_release = False

    # --- To implement -------------------------------------------------------

    def prompt(self):
        """Prompt for the next point, e.g. "Start of line"."""
        raise NotImplementedError

    def on_point(self, context):
        """A point was added to ``self.points``. Return FINISH to end."""
        return None

    def on_enter(self, context):
        """Enter with nothing typed: FINISH, CANCEL or None (keep going)."""
        return FINISH

    def on_option(self, context, name):
        """An option was typed (lowercase). Return FINISH, True if it was
        handled, or False if it is not an option of this command."""
        return False

    def create(self, context):
        """Make the result from ``self.points``."""
        raise NotImplementedError

    def on_number(self, context, value):
        """A number was typed: FINISH, True if handled, or None to use it as
        a distance constraint (the default)."""
        return None

    def on_text(self, context, text):
        """Anything typed, before it is read as coordinates or an option (e.g.
        ExtrudeCrv's "w0"): FINISH, True if handled, or None."""
        return None

    def after_track(self):
        """``self.current`` has just been updated."""

    def preview(self):
        """Lines to draw: (points, 'curve' or 'guide') polylines, or
        (N x 3 array of segment end points, 'shape')."""
        return [(self.points + ([self.current] if self.current is not None else []), 'curve')]

    def snap_geometry(self):
        """(points, segments) of the command in progress that can be snapped to."""
        return self.points, list(zip(self.points[:-1], self.points[1:]))

    def needs_selection(self, context):
        """Whether to ask for objects (with the normal selection keys) first."""
        return False

    def selection_done(self, context):
        """Enter while selecting: True to go on to picking points, False to
        keep selecting (e.g. the next set of objects), or FINISH."""
        return True

    def constrain_point(self, context, region, rv3d, mouse, point, snapped):
        """The point to use for ``point`` under the mouse (``snapped``: it is
        an object snap), e.g. moved onto a curve."""
        return point

    def on_cancel(self, context):
        """Esc: CANCEL, or FINISH to keep what the command has already done."""
        return CANCEL

    # --- Picking curves instead of points ------------------------------------

    def picks_curves(self):
        """Whether the mouse picks curves (hover and click) now."""
        return False

    def on_hover(self, context, area, region, mouse):
        """The mouse moved to ``mouse`` (region coordinates) in a viewport, or
        left the viewports (all None)."""

    def on_pick(self, context, area, region, mouse):
        """Left click at ``mouse`` while picking curves. FINISH to end."""
        return None

    # --- Operator -----------------------------------------------------------

    @classmethod
    def poll(cls, context):
        return context.window is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        global _active
        if _active is not None:
            self.report({'WARNING'}, "Another command is picking points")
            return {'CANCELLED'}
        self.window = context.window
        self.status = ""
        self.queued_inputs = []  # Options clicked in the status bar, an alias's input.
        self.points = []
        self.current = None
        self.distance = None
        self.angle = None
        self.one_shot = None  # Osnap mode typed for the next point only.
        self.snap_mode = None  # Osnap mode of the current point.
        self.tracking = []  # SmartTrack: points snapped to in this command.
        self.track_lines = []  # SmartTrack: (tracking point, current point) lines in use.
        self.targets = osnap.SnapTargets(context)
        area = context.area
        self.plane = (cplane.for_area(context.window.screen, area)
                      if area is not None and area.type == 'VIEW_3D' else cplane.WORLD_TOP)
        self.selecting = self.needs_selection(context)
        if not self.selecting:
            done = self.selection_done(context)
            if not done:
                return {'CANCELLED'}
            if done == FINISH:  # Works on the selection at once (e.g. Join).
                self.create(context)
                return {'FINISHED'}
        _active = self
        _add_draw_handlers()
        try:
            self._show_prompt()
            if not self.selecting:
                self._start_picking(context, event)
        except Exception:
            # Do not leave a dead operator as the running command.
            self._release()
            raise
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _start_picking(self, context, event):
        if self.picks_curves():
            self._hover(context, event)
        else:
            self._track(context, event)

    def _select_modal(self, context, event):
        """Selecting objects: the mouse and keys select as usual; Enter, Space
        or RMB click goes on to picking points (or runs a typed option)."""
        kind, value = event.type, event.value
        if kind == 'ESC' and value == 'PRESS':
            if command_line.text(self.window):
                command_line.clear(self.window)
                return {'RUNNING_MODAL'}
            return self._cancel(context)
        if (kind in {'RET', 'NUMPAD_ENTER', 'SPACE'} and value == 'PRESS') or (kind == 'RIGHTMOUSE' and value == 'CLICK'):
            return self._selection_enter(context, event, command_line.take(self.window))
        return {'PASS_THROUGH'}

    def _selection_enter(self, context, event, typed):
        if typed.strip():
            command_line.echo(self.window, f"{self.prompt()}: {typed}", 'INPUT')
            result = self._selection_option(context, typed)
        else:
            result = self.selection_done(context)
            if result and result != FINISH:
                self.selecting = False
        if result == FINISH:
            return self._end(context, finished=True)
        self._show_prompt()
        if not self.selecting:
            self._start_picking(context, event)
        return {'RUNNING_MODAL'}

    def _selection_option(self, context, typed):
        try:
            parsed = coords.parse(typed)
        except coords.InputError as ex:
            parsed = ex
        if isinstance(parsed, coords.DistanceInput):
            result = self.on_number(context, parsed.distance)
            if result:
                return result
        if not isinstance(parsed, coords.OptionInput):
            self._error("Select objects, then press Enter")
            return False
        result = self.on_option(context, parsed.name.lower())
        if not result:
            self._error(f"Unknown option: {parsed.name}")
        return result

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:
            # An error in a command must not leave it as the running command
            # (no other command could start) with its prompt, status bar and
            # previews: end it here, and say so in the command line.
            traceback.print_exc()
            self._release()
            try:
                command_line.echo(self.window, f"{self.bl_label} stopped: an error occurred "
                                  "(the details are in the system console)", 'ERROR')
            except Exception:
                traceback.print_exc()
            return {'CANCELLED'}

    def cancel(self, context):
        # Blender ends the command itself (a file is loaded, the window closes).
        self._release()

    def _modal(self, context, event):
        while self.queued_inputs:
            typed = self.queued_inputs.pop(0)
            if self.selecting:
                result = self._selection_enter(context, event, typed)
            else:
                command_line.echo(self.window, f"{self.prompt()}: {typed}", 'INPUT')
                result = self._typed(context, typed)
                if 'RUNNING_MODAL' in result:
                    self._show_prompt()
            if 'RUNNING_MODAL' not in result:
                self.queued_inputs = []
                return result
        if self.selecting:
            return self._select_modal(context, event)
        if self.picks_curves():
            return self._pick_modal(context, event)
        kind, value = event.type, event.value
        if kind in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self._track(context, event)
            return {'PASS_THROUGH'}
        if kind == 'LEFTMOUSE' and self.pick_on_release:
            if value != 'RELEASE':
                return {'RUNNING_MODAL'}
            if self._track(context, event):
                return self._accept(context, self.current)
            return self._end(context, finished=False)  # Released over the UI.
        if kind == 'LEFTMOUSE' and value == 'PRESS':
            if self._track(context, event):
                return self._accept(context, self.current)
            return {'PASS_THROUGH'}  # Over the UI: buttons still work.
        return self._keys(context, event)

    def _pick_modal(self, context, event):
        """Picking curves: hover highlights, a left click picks."""
        kind, value = event.type, event.value
        if kind in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            self._hover(context, event)
            return {'PASS_THROUGH'}
        if kind == 'LEFTMOUSE' and value == 'PRESS':
            area, region = _viewport_under(self.window, event.mouse_x, event.mouse_y)
            if area is None:
                return {'PASS_THROUGH'}  # Over the UI: buttons still work.
            mouse = (event.mouse_x - region.x, event.mouse_y - region.y)
            if self.on_pick(context, area, region, mouse) == FINISH:
                return self._end(context, finished=True)
            self._show_prompt()
            if self.picks_curves():
                self._hover(context, event)
            else:
                self._track(context, event)
            return {'RUNNING_MODAL'}
        return self._keys(context, event)

    def _hover(self, context, event):
        area, region = _viewport_under(self.window, event.mouse_x, event.mouse_y)
        if area is None:
            self.on_hover(context, None, None, None)
        else:
            self.on_hover(context, area, region, (event.mouse_x - region.x, event.mouse_y - region.y))
        self.status = ""
        context.workspace.status_text_set(_draw_status_bar)
        _tag_viewports(self.window)

    def _keys(self, context, event):
        """Keys and RMB, the same when picking points or curves."""
        kind, value = event.type, event.value
        if kind == 'RIGHTMOUSE':
            # RMB drag navigates; RMB click is Enter, as in Rhino.
            return self._enter(context) if value == 'CLICK' else {'PASS_THROUGH'}
        if value != 'PRESS':
            return {'PASS_THROUGH'}
        if kind in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            return self._enter(context)
        if kind == 'ESC':
            if command_line.text(self.window):
                command_line.clear(self.window)
                return {'RUNNING_MODAL'}
            return self._cancel(context)
        if kind == 'BACK_SPACE':
            command_line.backspace(self.window)
            return {'RUNNING_MODAL'}
        if event.unicode and not (event.ctrl or event.alt or event.oskey):
            command_line.insert(self.window, event.unicode)
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    # --- Points -------------------------------------------------------------

    def _track(self, context, event):
        """Update the point under the mouse; False if not over a viewport."""
        area, region = _viewport_under(self.window, event.mouse_x, event.mouse_y)
        if area is None:
            return False
        rv3d = area.spaces.active.region_3d
        self.plane = cplane.for_area(self.window.screen, area)
        mouse = (event.mouse_x - region.x, event.mouse_y - region.y)
        snapped = self._osnap(context, region, rv3d, mouse, event.alt)
        self.track_lines = []
        if snapped is not None:
            point, self.snap_mode = snapped
            self._remember_tracking(point)
            self.current = self._constrain_direction(point)
        else:
            self.snap_mode = None
            point = cplane.point_under_mouse(region, rv3d, self._picking_plane(context), mouse)
            tracked = self._smart_track(context, region, rv3d, point, event.alt)
            if tracked is not None:
                self.current = self._constrain_direction(tracked)
            else:
                self.current = self._constrain(context, point, event.shift)
        if self.current is not None:
            self.current = self.constrain_point(context, region, rv3d, mouse, self.current, snapped is not None)
        self.after_track()
        self._show_status(context)
        _tag_viewports(self.window)
        return True

    def _osnap(self, context, region, rv3d, mouse, alt):
        aids = settings.get(context)
        if self.one_shot is not None:
            modes = {self.one_shot}
        elif aids.osnap and not alt:
            modes = set(aids.osnap_modes)
        else:
            return None
        if not modes:
            return None
        radius = aids.osnap_radius * context.preferences.system.ui_scale
        points, segments = self.snap_geometry()
        return self.targets.snap(region, rv3d, mouse, modes, radius,
                                 previous=self.points[-1] if self.points else None,
                                 extra_points=points, extra_segments=segments)

    def _picking_plane(self, context):
        """The CPlane, or with Planar on the plane parallel to it through the
        previous point."""
        if not (settings.get(context).planar and self.points):
            return self.plane
        elevation = self.plane.to_local(self.points[-1]).z
        return cplane.CPlane(self.plane.name, self.plane.origin + self.plane.normal * elevation,
                             self.plane.x_axis, self.plane.y_axis)

    def _remember_tracking(self, point):
        if all((point - known).length > 1e-9 for known in self.tracking):
            self.tracking = (self.tracking + [point.copy()])[-TRACKING_POINTS:]

    def _smart_track(self, context, region, rv3d, point, alt):
        """``point`` lined up, in CPlane x and/or y, with a tracking point
        whose line passes within the snap radius of it on screen."""
        aids = settings.get(context)
        if not aids.smart_track or alt or not self.tracking:
            return None
        radius = aids.osnap_radius * context.preferences.system.ui_scale
        mouse = location_3d_to_region_2d(region, rv3d, point)
        if mouse is None:
            return None
        local = self.plane.to_local(point)
        best = [None, None]  # Per CPlane axis: (pixels, coordinate, tracking point).
        for tracked in self.tracking:
            tracked_local = self.plane.to_local(tracked)
            for axis in (0, 1):
                lined_up = local.copy()
                lined_up[axis] = tracked_local[axis]
                on_screen = location_3d_to_region_2d(region, rv3d, self.plane.to_world(*lined_up))
                if on_screen is None:
                    continue
                pixels = (on_screen - mouse).length
                if pixels <= radius and (best[axis] is None or pixels < best[axis][0]):
                    best[axis] = (pixels, tracked_local[axis], tracked)
        if best == [None, None]:
            return None
        for axis in (0, 1):
            if best[axis] is not None:
                local[axis] = best[axis][1]
        result = self.plane.to_world(*local)
        self.track_lines = [(found[2], result) for found in best if found is not None]
        return result

    def _constrain(self, context, point, shift):
        aids = settings.get(context)
        if aids.grid_snap:
            point = cplane.snap_to_grid(self.plane, point, aids.snap_spacing)
        if self.points and aids.ortho != shift:
            point = cplane.ortho(self.plane, self.points[-1], point)
        return self._constrain_direction(point)

    def _constrain_direction(self, point):
        """Typed angle and distance constraints from the previous point."""
        if self.points:
            previous = self.points[-1]
            if self.angle is not None:
                point = cplane.at_angle(self.plane, previous, point, self.angle)
            if self.distance is not None:
                point = cplane.at_distance(previous, point, self.distance) or point
        return point

    def _accept(self, context, point):
        if point is None:
            return {'RUNNING_MODAL'}
        self.points.append(point.copy())
        self.distance = self.angle = self.one_shot = None
        if self.on_point(context) == FINISH:
            return self._end(context, finished=True)
        self._show_prompt()
        return {'RUNNING_MODAL'}

    # --- Typed input ----------------------------------------------------------

    def _enter(self, context):
        typed = command_line.take(self.window)
        if typed.strip():
            command_line.echo(self.window, f"{self.prompt()}: {typed}", 'INPUT')
            return self._typed(context, typed)
        result = self.on_enter(context)
        if result in {FINISH, CANCEL}:
            return self._end(context, finished=result == FINISH)
        return {'RUNNING_MODAL'}

    def _error(self, message):
        command_line.echo(self.window, message, 'ERROR')
        return {'RUNNING_MODAL'}

    def _typed(self, context, typed):
        special = self.on_text(context, typed.strip())
        if special == FINISH:
            return self._end(context, finished=True)
        if special:
            self._show_prompt()
            return {'RUNNING_MODAL'}
        try:
            parsed = coords.parse(typed)
        except coords.InputError as ex:
            return self._error(str(ex))
        previous = self.points[-1] if self.points else None
        if isinstance(parsed, coords.OptionInput):
            # An object snap for the next point only, unless the prompt has
            # an option of that very name (Arc's "Point": its button types it).
            own = {option.split("=")[0].lower() for option in prompt_options(self.prompt())}
            if parsed.name.lower() in osnap.ONE_SHOT and parsed.name.lower() not in own:
                self.one_shot = osnap.ONE_SHOT[parsed.name.lower()]
                return {'RUNNING_MODAL'}
            result = self.on_option(context, parsed.name.lower())
            if result == FINISH:
                return self._end(context, finished=True)
            if not result:
                return self._error(f"Unknown option: {parsed.name}")
            self._show_prompt()
            return {'RUNNING_MODAL'}
        if isinstance(parsed, coords.DistanceInput) and previous is None:
            result = self.on_number(context, parsed.distance)
            if result == FINISH:
                return self._end(context, finished=True)
            if result:
                self._show_prompt()
                return {'RUNNING_MODAL'}
        if self.picks_curves():
            return self._error("Pick a curve, or type an option")
        needs_previous = isinstance(parsed, (coords.DistanceInput, coords.AngleInput)) or parsed.relative
        if needs_previous and previous is None:
            return self._error("Pick or type a first point")
        if isinstance(parsed, coords.DistanceInput):
            result = self.on_number(context, parsed.distance)
            if result == FINISH:
                return self._end(context, finished=True)
            if not result:
                self.distance = parsed.distance
            self._show_prompt()
            return {'RUNNING_MODAL'}
        if isinstance(parsed, coords.AngleInput):
            self.angle = parsed.angle
            return {'RUNNING_MODAL'}
        plane = cplane.WORLD_TOP if parsed.world else self.plane
        if isinstance(parsed, coords.PointInput):
            if parsed.relative:
                point = previous + plane.to_world(parsed.x, parsed.y, parsed.z) - plane.origin
            else:
                point = plane.to_world(parsed.x, parsed.y, parsed.z)
        else:
            base = previous if parsed.relative else plane.origin
            point = cplane.polar(plane, base, parsed.distance, parsed.angle)
        return self._accept(context, point)

    # --- Feedback and ending ----------------------------------------------------

    def _show_prompt(self):
        command_line.set_prompt(self.window, self.prompt() + ": ")
        # The status bar: the prompt's options as buttons (also while selecting).
        self.window.workspace.status_text_set(_draw_status_bar)

    def _show_status(self, context):
        local = self.plane.to_local(self.current)
        text = f"{self.plane.name}   x {local.x:.3f}   y {local.y:.3f}   z {local.z:.3f}"
        if self.snap_mode is not None:
            text += f"   {osnap.LABELS[self.snap_mode]}"
        elif self.track_lines:
            text += "   SmartTrack"
        if self.one_shot is not None:
            text += f"   (next point: {osnap.LABELS[self.one_shot]} only)"
        if self.distance is not None:
            text += f"   Distance {self.distance:g}"
        if self.angle is not None:
            text += f"   Angle {self.angle:g}°"
        self.status = text
        # A draw function rather than text keeps the modeling aid toggles
        # (Grid Snap, Ortho, Osnap) clickable while picking.
        context.workspace.status_text_set(_draw_status_bar)

    def _cancel(self, context):
        return self._end(context, finished=self.on_cancel(context) == FINISH)

    def _end(self, context, finished):
        try:
            if finished:
                self.create(context)
        finally:
            self._release()
        return {'FINISHED'} if finished else {'CANCELLED'}

    def _release(self):
        """Stop being the running command, however it ends: the previews, the
        prompt and the status bar go back."""
        global _active
        if _active is not self:
            return
        _active = None
        _remove_draw_handlers()
        _clear_status_bar()
        try:
            command_line.set_prompt(self.window)
            _tag_viewports(self.window)
        except ReferenceError:
            pass  # The window is gone.


# --- Drawing -------------------------------------------------------------------

def _clear_status_bar():
    """Blender's status bar back in place of ``_draw_status_bar`` (one draw
    function serves every workspace)."""
    if bpy.types.STATUSBAR_HT_header.draw is not _draw_status_bar:
        return
    workspace = bpy.context.workspace or next(iter(bpy.data.workspaces), None)
    if workspace is not None:
        workspace.status_text_set(None)


def _draw_status_bar(header, context):
    if _active is not None:
        options = prompt_options(_active.prompt())
        if options:
            row = header.layout.row(align=True)
            for option in options:
                row.operator(R2B_OT_command_option.bl_idname, text=option).option = option.split("=")[0]
        header.layout.label(text=_active.status)
    settings.draw_status_bar(header, context)


class R2B_OT_command_option(bpy.types.Operator):
    """Choose this option of the running command (as typing it and pressing Enter)"""

    bl_idname = "r2b.command_option"
    bl_label = "Command Option"
    bl_options = {'INTERNAL'}

    option: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return _active is not None

    def execute(self, context):
        # The running command takes it with its next event (this click's
        # release, at the latest the next mouse move).
        _active.queued_inputs.append(self.option)
        return {'FINISHED'}


# 'curve', 'guide', 'highlight' (e.g. what Trim would remove) and 'addition'
# (e.g. what Extend would add) are polylines; 'shape' is pairs of points (segments), e.g. the outline of
# objects being moved, and so are 'removal' and 'growth' (e.g. PushPull taking volume away or adding it).
# Kind: (color, line width).
_PREVIEW_STYLES = {
    'curve': ((1.0, 1.0, 1.0, 1.0), 1.5),
    'guide': ((0.7, 0.7, 0.7, 0.6), 1.0),
    'shape': ((1.0, 0.85, 0.3, 0.9), 1.0),
    'highlight': ((1.0, 0.35, 0.3, 0.95), 3.0),
    'addition': ((0.35, 1.0, 0.5, 0.95), 3.0),
    'removal': ((1.0, 0.3, 0.25, 0.95), 2.0),
    'growth': ((0.35, 1.0, 0.5, 0.95), 2.0),
}
_SEGMENT_KINDS = {'shape', 'removal', 'growth'}


def _draw_rubber_band():
    if _active is None or _active.selecting:
        return
    region = bpy.context.region
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
    shader.uniform_float("viewportSize", (region.width, region.height))
    gpu.state.blend_set('ALPHA')
    for points, kind in _active.preview():
        if len(points) < 2:
            continue
        color, width = _PREVIEW_STYLES[kind]
        shader.uniform_float("lineWidth", width * bpy.context.preferences.system.ui_scale)
        shader.uniform_float("color", color)
        primitive = 'LINES' if kind in _SEGMENT_KINDS else 'LINE_STRIP'
        positions = points if kind in _SEGMENT_KINDS else [tuple(p) for p in points]
        batch_for_shader(shader, primitive, {"pos": positions}).draw(shader)
    if _active.track_lines:
        shader.uniform_float("lineWidth", bpy.context.preferences.system.ui_scale)
        shader.uniform_float("color", (0.45, 0.75, 1.0, 0.9))
        positions = [tuple(p) for line in _active.track_lines for p in line]
        batch_for_shader(shader, 'LINES', {"pos": positions}).draw(shader)
    gpu.state.blend_set('NONE')


def _draw_marker():
    if _active is None or _active.selecting or _active.current is None:
        return
    context = bpy.context
    position = location_3d_to_region_2d(context.region, context.region_data, _active.current)
    if position is None:
        return
    size = 4 * context.preferences.system.ui_scale
    x, y = position
    corners = [(x - size, y - size), (x + size, y - size), (x + size, y + size), (x - size, y + size)]
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.uniform_float("color", (1.0, 0.85, 0.2, 1.0))
    batch_for_shader(shader, 'LINE_LOOP', {"pos": corners}).draw(shader)
    # SmartTrack's points: small crosses.
    crosses = []
    for tracked in _active.tracking:
        at = location_3d_to_region_2d(context.region, context.region_data, tracked)
        if at is not None:
            crosses += [(at.x - size, at.y - size), (at.x + size, at.y + size),
                        (at.x - size, at.y + size), (at.x + size, at.y - size)]
    if crosses:
        shader.uniform_float("color", (0.45, 0.75, 1.0, 0.9))
        batch_for_shader(shader, 'LINES', {"pos": crosses}).draw(shader)
    if _active.snap_mode is not None:
        # The osnap's name next to the point, as in Rhino.
        font = 0
        blf.size(font, 11 * context.preferences.system.ui_scale)
        blf.color(font, 1.0, 0.85, 0.2, 1.0)
        blf.position(font, x + 2 * size, y + 2 * size, 0)
        blf.draw(font, osnap.LABELS[_active.snap_mode])


def _add_draw_handlers():
    _draw_handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_rubber_band, (), 'WINDOW', 'POST_VIEW'))
    _draw_handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_marker, (), 'WINDOW', 'POST_PIXEL'))


def _remove_draw_handlers():
    while _draw_handles:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handles.pop(), 'WINDOW')


def register():
    bpy.utils.register_class(R2B_OT_command_option)


def unregister():
    # A command still picking points when the mode turns off: Blender ends
    # it without a call when its class is unregistered.
    global _active
    running, _active = _active, None
    _remove_draw_handlers()
    _clear_status_bar()
    if running is not None:
        try:
            command_line.set_prompt(running.window)
        except ReferenceError:
            pass
    bpy.utils.unregister_class(R2B_OT_command_option)
