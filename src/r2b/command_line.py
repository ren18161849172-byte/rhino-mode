# SPDX-License-Identifier: GPL-3.0-or-later
"""Rhino-style command line (MODE feature).

The command line is a Console area of the Rhino workspace whose language is
"rhino": Blender's console calls this module's ``execute``, ``autocomplete``
and ``banner`` for it, and provides line editing, Up/Down history and the
scrollback. Commands are looked up in ``commands.py``.

As in Rhino, typing in a 3D viewport goes to the command line without
clicking it first: the built-in single-key shortcuts (letters, digits and
punctuation without Ctrl/Alt) of the modeling keymaps are switched off in
memory while the mode is on. Enter or Space runs the line, and repeats the
last command when the line is empty; Esc clears the line, or the selection
when nothing is typed. The number pad keeps Blender's view shortcuts.

Outside the Rhino workspace there is no Console area: typing then goes to
a line of R2B's own, drawn at the bottom of the viewport typed in (prompt,
what is typed, the last message), with the same keys and Up/Down history.
"""

import string
import sys
import time
import types

import blf
import bpy

from . import commands, keymaps

LANGUAGE = "rhino"
PROMPT = "Command: "
SCROLLBACK_LIMIT = 500

# Keys that type text; their plain and Shift shortcuts are switched off.
TEXT_KEYS = frozenset(string.ascii_uppercase) | {
    "ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "COMMA", "PERIOD", "MINUS", "PLUS", "EQUAL", "SLASH", "BACK_SLASH", "SEMI_COLON",
    "QUOTE", "ACCENT_GRAVE", "LEFT_BRACKET", "RIGHT_BRACKET",
}

# Keymaps whose single-key shortcuts give way to typing. "Font" is not in the
# list: typing in a 3D text object in Edit Mode keeps working. In Sculpt
# Mode letters would pick brushes and start masks and hiding (B: box mask),
# so they type too; the paint modes keep their few keys (X swaps colors).
TYPING_KEYMAPS = (
    "3D View", "3D View Generic", "Object Mode", "Mesh", "Curve", "Curves",
    "Armature", "Metaball", "Lattice", "Pose", "Point Cloud", "Sculpt",
)
# Brush size and strength (F, Shift+F, [ and ]) stay on their keys.
KEPT_SHORTCUTS = {"wm.radial_control", "brush.scale_size"}

# Everything written to the command line, so that the console of another
# layout (maximized or four views) can show the same history.
_scrollback = []
_prompt = PROMPT
_last_command = None
# Commands that Enter on an empty line does not repeat, as in Rhino ("Never
# repeat these commands": Delete always; after Undo the command before it
# repeats).
NEVER_REPEAT = {"Delete", "Undo", "Redo"}
# Whether the note on Space (Enter in Rhino, play in Blender) was shown.
_space_note_shown = False
_last_viewport = None  # as_pointer() of the 3D viewport typed in last

# The command line outside the Rhino workspace (no Console area).
_buffer = ""
_history = []
_history_index = 0
_message = None  # (text, line type, time) of the last line written.
MESSAGE_SECONDS = 5.0

# Blender's interactive tools (G/R/S, extrude, slide, bevel, knife...) take a
# right click as Enter, as in Rhino, instead of cancelling (Esc still cancels).
# On release: some tools (bevel) eat the press, so no click would follow.
MODAL_RMB_KEYMAPS = ("Transform Modal Map", "Bevel Modal Map", "Knife Tool Modal Map")

_muted = keymaps.MutedItems()
_modal_rmb = keymaps.ChangedItems()
_addon_items = keymaps.AddonItems()
_from_viewport = False  # The line being run was typed in a viewport (not in the command line area).
_draw_handle = None
_viewport_draw_handle = None


# --- Console access -------------------------------------------------------

def find_console(screen):
    return next((a for a in screen.areas
                 if a.type == 'CONSOLE' and a.spaces.active.language == LANGUAGE), None)


def _override(window, area):
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return dict(window=window, screen=window.screen, area=area, region=region,
                space_data=area.spaces.active)


def current_line(area):
    history = area.spaces.active.history
    return history[-1].body if len(history) else ""


def _console_append(window, area, text, line_type):
    with bpy.context.temp_override(**_override(window, area)):
        bpy.ops.console.scrollback_append(text=text, type=line_type)


def _append(window, text, line_type='OUTPUT'):
    global _message
    _message = (text, line_type, time.monotonic())
    _redraw_viewports(window)
    _scrollback.append((text, line_type))
    del _scrollback[:-SCROLLBACK_LIMIT]
    area = find_console(window.screen)
    if area is not None:
        _console_append(window, area, text, line_type)


def print_lines(context, lines, line_type='OUTPUT'):
    for line in lines:
        _append(context.window, line, line_type)


def refresh(window, line=None):
    """Show the command line history (and ``line`` as input) in the window's
    command line, e.g. after switching between four views and maximized."""
    area = find_console(window.screen)
    if area is None:
        return
    with bpy.context.temp_override(**_override(window, area)):
        bpy.ops.console.clear(scrollback=True, history=False)
        for text, line_type in _scrollback:
            bpy.ops.console.scrollback_append(text=text, type=line_type)
        if line is not None:
            bpy.ops.console.clear_line()
            bpy.ops.console.insert(text=line)
        area.spaces.active.prompt = _prompt


def schedule_refresh(window, line):
    pointer = window.as_pointer()

    def run():
        for win in bpy.context.window_manager.windows:
            if win.as_pointer() == pointer:
                refresh(win, line)
        return None

    bpy.app.timers.register(run, first_interval=0.05)


# --- For commands that ask for input (see getpoint.py) ----------------------

def set_prompt(window, prompt=None):
    """Show ``prompt`` before the input line; the default is "Command: "."""
    global _prompt
    _prompt = prompt or PROMPT
    _redraw_viewports(window)
    area = find_console(window.screen)
    if area is not None:
        area.spaces.active.prompt = _prompt
        area.tag_redraw()


def _console_op(window, name, **properties):
    area = find_console(window.screen)
    if area is not None:
        with bpy.context.temp_override(**_override(window, area)):
            getattr(bpy.ops.console, name)(**properties)


def _redraw_viewports(window):
    for area in window.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def _set_buffer(window, value):
    global _buffer, _history_index
    _buffer = value
    _history_index = len(_history)
    _redraw_viewports(window)


def text(window):
    area = find_console(window.screen)
    return current_line(area) if area is not None else _buffer


def insert(window, typed):
    if find_console(window.screen) is None:
        _set_buffer(window, _buffer + typed)
    _console_op(window, "insert", text=typed)


def backspace(window):
    if find_console(window.screen) is None:
        _set_buffer(window, _buffer[:-1])
    _console_op(window, "delete", type='PREVIOUS_CHARACTER')


def clear(window):
    if find_console(window.screen) is None:
        _set_buffer(window, "")
    _console_op(window, "clear_line")


def take(window):
    """The typed line, which then moves into the history (Up/Down)."""
    typed = text(window)
    if find_console(window.screen) is None:
        if typed.strip() and (not _history or _history[-1] != typed):
            _history.append(typed)
        _set_buffer(window, "")
        return typed
    _console_op(window, "history_append", text="", current_character=0, remove_duplicates=True)
    return typed


def cycle_history(window, previous):
    """Up/Down in the line outside the Rhino workspace."""
    global _buffer, _history_index
    if not _history:
        return
    _history_index = max(0, _history_index - 1) if previous else min(len(_history), _history_index + 1)
    _buffer = _history[_history_index] if _history_index < len(_history) else ""
    _redraw_viewports(window)


def echo(window, line, line_type='OUTPUT'):
    _append(window, line, line_type)


# --- Running commands -------------------------------------------------------

def remember_viewport(area):
    global _last_viewport
    if area is not None and area.type == 'VIEW_3D':
        _last_viewport = area.as_pointer()


def _target_viewport(window):
    viewports = [a for a in window.screen.areas if a.type == 'VIEW_3D']
    for area in viewports:
        if area.as_pointer() == _last_viewport:
            return area
    return max(viewports, key=lambda a: a.width * a.height, default=None)


def _run(window, command, warp=False):
    area = _target_viewport(window)
    if area is None:
        _append(window, f"{command.name}: no 3D viewport to run in", 'ERROR')
        return
    if warp and command.modal:
        # Blender's interactive tools work from where the mouse is: bring it
        # into the viewport when the command was typed in the command line.
        region = next(r for r in area.regions if r.type == 'WINDOW')
        window.cursor_warp(region.x + region.width // 2, region.y + region.height // 2)
    commands.begin_undo()
    try:
        with bpy.context.temp_override(**_override(window, area)):
            result = command.run(bpy.context)
            if _needs_undo_step(result):
                bpy.ops.ed.undo_push(message=command.name)
    except commands.CommandError as ex:
        _append(window, f"{command.name}: {ex}", 'ERROR')
    except (RuntimeError, TypeError) as ex:
        # Usually an operator whose poll failed, e.g. Join with one object;
        # or one that has no such mode for the active object (TypeError:
        # EditMode with an empty active).
        _append(window, f"{command.name} cannot run here: {str(ex).strip().splitlines()[0]}", 'ERROR')


# Results after which the command pushes no undo step of its own: an
# interactive tool (pushes when it ends), a menu (its item pushes), nothing done.
_NO_UNDO_RESULTS = {'RUNNING_MODAL', 'INTERFACE', 'CANCELLED', 'PASS_THROUGH'}


def _needs_undo_step(result):
    """Whether a command that just ran changed data without an operator of
    its own (Python functions such as Lock or SelCrv); operators push their
    own step (see commands.begin_undo)."""
    if commands.undo_handled():
        return False
    return not (isinstance(result, set) and result & _NO_UNDO_RESULTS)


def run_line(window, text, from_viewport=True):
    """Run a command line entry. An empty entry repeats the last command.
    Returns False if there was nothing to run."""
    global _last_command
    text = text.strip()
    inputs = []
    if text:
        aliased = commands.alias(text)
        name = text
        if aliased is not None:
            name, inputs = aliased
        found = commands.lookup(name)
        command = found.command
        if command is None:
            _append(window, PROMPT + text, 'INPUT')
            _append(window, found.message or f"Unknown command: {name}", 'ERROR')
            return True
        shown = commands.typed_name(command, name)
    elif _last_command is not None:
        command, shown = _last_command
    else:
        return False
    _append(window, PROMPT + shown + (f"  (Blender: {command.label})" if command.label else ""), 'INPUT')
    if command.name not in NEVER_REPEAT:
        _last_command = command, shown
    commands.remember(command)
    from . import select_tools

    select_tools.before_command(bpy.context)  # For SelLast and SelPrev.
    _run(window, command, warp=not from_viewport)
    if inputs:
        _feed(window, command, inputs)
    return True


def _feed(window, command, inputs):
    """Give an alias's inputs to the command it started."""
    from . import getpoint

    if getpoint._active is None:
        _append(window, f"{command.name} takes no input: {' '.join(inputs)}", 'ERROR')
        return
    getpoint._active.queued_inputs.extend(inputs)


def _execute_console(window, area):
    """Run the command line from a key or click in a viewport."""
    global _from_viewport
    _from_viewport = True
    try:
        with bpy.context.temp_override(**_override(window, area)):
            _language_execute(bpy.context, True)
    finally:
        _from_viewport = False


def enter(context):
    """Rhino's Enter (also RMB click): run the line or repeat the last
    command. Returns False if there is nothing to run."""
    area = find_console(context.window.screen)
    if not text(context.window).strip() and _last_command is None:
        return False
    remember_viewport(context.area)
    if area is None:
        return run_line(context.window, take(context.window))
    _execute_console(context.window, area)
    return True


# --- The "rhino" console language ------------------------------------------

def _language_execute(context, is_interactive):
    line = current_line(context.area)
    # Keep the entry in the console history (Up/Down) and start a new line.
    bpy.ops.console.history_append(text="", current_character=0, remove_duplicates=True)
    run_line(context.window, line, from_viewport=_from_viewport)
    return {'FINISHED'}


def completion(text, mode=None):
    """What Tab puts on the line for ``text``: the first candidate's name,
    with b: when that is needed to get Blender's meaning."""
    found = commands.lookup(text, mode, operators=False)
    if not found.matches:
        return commands.typed_name(found.command, text) if found.command is not None else None
    name, command, dialect = found.matches[0]
    forced = text.strip().lower().startswith(("b:", "r:"))
    if dialect == commands.BLENDER and (forced or commands.lookup(name, mode, operators=False).command is not command):
        return "b:" + name
    return name


def _language_autocomplete(context):
    completed = completion(current_line(context.area))
    if completed:
        bpy.ops.console.clear_line()
        bpy.ops.console.insert(text=completed)
    return {'FINISHED'}


def _language_banner(context):
    context.space_data.prompt = _prompt
    if _scrollback:
        refresh(context.window)
    else:
        _append(context.window, "Rhino Mode command line: type a command and press Enter or Space. "
                "Enter on an empty line repeats the last command. Type Commands for the list.", 'INFO')
    return {'FINISHED'}


_language = types.ModuleType("_console_" + LANGUAGE)
_language.execute = _language_execute
_language.autocomplete = _language_autocomplete
_language.banner = _language_banner
# Blender 4.x imports "console_<language>", 5.x "_console_<language>".
_LANGUAGE_MODULE_NAMES = ("console_" + LANGUAGE, "_console_" + LANGUAGE)


# --- Keys in the 3D viewport and the command line ---------------------------

def _types_space(window, event):
    """Space after b: and the first words of a Blender name of several words
    types a space (commands.continues_with_space); otherwise it is Enter."""
    return event.type == 'SPACE' and commands.continues_with_space(text(window))


class _ViewportKeyOperator:
    """Base for operators that act on the command line from a 3D viewport."""

    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        # Some keys are bound in keymaps shared with other editors ("Frames").
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        area = find_console(context.window.screen)
        remember_viewport(context.area)
        if area is None:
            return self.act_here(context, event)  # Outside the Rhino workspace.
        return self.act(context, event, area)


class R2B_OT_command_type(_ViewportKeyOperator, bpy.types.Operator):
    """Type into the command line"""

    bl_idname = "r2b.command_type"
    bl_label = "Type Command"

    def invoke(self, context, event):
        if event.type.startswith("NUMPAD_") or not event.unicode:
            return {'PASS_THROUGH'}  # The number pad keeps Blender's view keys.
        return super().invoke(context, event)

    def act(self, context, event, area):
        if event.type == 'SPACE' and not _types_space(context.window, event):
            _execute_console(context.window, area)
        else:
            with context.temp_override(**_override(context.window, area)):
                bpy.ops.console.insert(text=event.unicode)
        return {'FINISHED'}

    def act_here(self, context, event):
        if event.type == 'SPACE' and not _types_space(context.window, event):
            run_line(context.window, take(context.window))
        else:
            insert(context.window, event.unicode)
        return {'FINISHED'}


def _space_note(window, event):
    """Once, when Space repeats a command in a scene with animation (a
    Blender user pressing Space to play): say how to play it here."""
    global _space_note_shown
    if _space_note_shown or event.type != 'SPACE' or text(window).strip() or not bpy.data.actions:
        return False
    _space_note_shown = True
    return True


class R2B_OT_command_enter(_ViewportKeyOperator, bpy.types.Operator):
    """Run the command line, or repeat the last command when it is empty"""

    bl_idname = "r2b.command_enter"
    bl_label = "Run Command"

    def act(self, context, event, area):
        if _types_space(context.window, event):
            insert(context.window, " ")
            return {'FINISHED'}
        note = _last_command is not None and _space_note(context.window, event)
        _execute_console(context.window, area)
        if note:
            echo(context.window, "Space repeats the last command, as Enter in Rhino. To play the animation: "
                 "Space over the timeline, or PlayAnimation.", 'INFO')
        return {'FINISHED'}

    def act_here(self, context, event):
        if _types_space(context.window, event):
            insert(context.window, " ")
            return {'FINISHED'}
        note = _last_command is not None and _space_note(context.window, event)
        run_line(context.window, take(context.window))
        if note:
            echo(context.window, "Space repeats the last command, as Enter in Rhino. To play the animation: "
                 "Space over the timeline, or PlayAnimation.", 'INFO')
        return {'FINISHED'}


class R2B_OT_command_edit(_ViewportKeyOperator, bpy.types.Operator):
    """Edit the command line from the 3D viewport"""

    bl_idname = "r2b.command_edit"
    bl_label = "Edit Command Line"

    action: bpy.props.EnumProperty(items=(
        ('BACKSPACE', "Backspace", ""),
        ('CLEAR', "Clear", ""),
        ('PREVIOUS', "Previous", ""),
        ('NEXT', "Next", ""),
    ))

    def act(self, context, event, area):
        if self.action == 'CLEAR' and not current_line(area):
            # Esc with nothing typed clears the selection, as in Rhino.
            try:
                commands.resolve("SelNone").run(context)
            except (commands.CommandError, RuntimeError):
                return {'PASS_THROUGH'}
            return {'FINISHED'}
        with context.temp_override(**_override(context.window, area)):
            if self.action == 'BACKSPACE':
                bpy.ops.console.delete(type='PREVIOUS_CHARACTER')
            elif self.action == 'CLEAR':
                bpy.ops.console.clear_line()
            else:
                bpy.ops.console.history_cycle(reverse=self.action == 'PREVIOUS')
        return {'FINISHED'}

    def act_here(self, context, event):
        window = context.window
        if self.action == 'CLEAR' and not _buffer:
            try:
                commands.resolve("SelNone").run(context)
            except (commands.CommandError, RuntimeError):
                return {'PASS_THROUGH'}
            return {'FINISHED'}
        if self.action == 'BACKSPACE':
            backspace(window)
        elif self.action == 'CLEAR':
            clear(window)
        else:
            cycle_history(window, self.action == 'PREVIOUS')
        return {'FINISHED'}


class R2B_OT_command_console_key(bpy.types.Operator):
    """Enter and Space run the line, Esc clears it, in the Rhino command line"""

    bl_idname = "r2b.command_console_key"
    bl_label = "Command Line Key"
    bl_options = {'INTERNAL'}

    action: bpy.props.EnumProperty(items=(('RUN', "Run", ""), ('CLEAR', "Clear", "")))

    @classmethod
    def poll(cls, context):
        # The Console keymap is shared with Blender's Python console.
        space = context.space_data
        return space is not None and space.type == 'CONSOLE' and space.language == LANGUAGE

    def invoke(self, context, event):
        if self.action == 'RUN' and event.type == 'SPACE' and commands.continues_with_space(current_line(context.area)):
            bpy.ops.console.insert(text=" ")
            return {'FINISHED'}
        return self.execute(context)

    def execute(self, context):
        if self.action == 'RUN':
            # Not Blender's console.execute: it would push an undo step of its
            # own ("Console Execute") after the command's step.
            _language_execute(context, True)
        else:
            bpy.ops.console.clear_line()
        return {'FINISHED'}


_classes = (R2B_OT_command_type, R2B_OT_command_enter, R2B_OT_command_edit, R2B_OT_command_console_key)


def _add_keymap_items():
    add = _addon_items.add
    add("r2b.command_type", 'TEXTINPUT', 'ANY')
    for key in ('RET', 'NUMPAD_ENTER'):
        add("r2b.command_enter", key, 'PRESS')
    for key, action in (('BACK_SPACE', 'BACKSPACE'), ('ESC', 'CLEAR')):
        add("r2b.command_edit", key, 'PRESS', properties={"action": action})
    # The "Frames" keymap (Space plays the animation, Up/Down jump between
    # keyframes) is handled before the 3D View keymap in viewports and shared
    # with the timeline and other editors; the operators' poll limits these
    # items to 3D viewports.
    add("r2b.command_enter", 'SPACE', 'PRESS', keymap="Frames", space_type='EMPTY')
    for key, action in (('UP_ARROW', 'PREVIOUS'), ('DOWN_ARROW', 'NEXT')):
        add("r2b.command_edit", key, 'PRESS', keymap="Frames", space_type='EMPTY',
            properties={"action": action})
    for key, action in (('SPACE', 'RUN'), ('RET', 'RUN'), ('NUMPAD_ENTER', 'RUN'), ('ESC', 'CLEAR')):
        add("r2b.command_console_key", key, 'PRESS', keymap="Console", space_type='CONSOLE',
            properties={"action": action})


def _typing_keymaps(keyconfig):
    return TYPING_KEYMAPS + tuple(km.name for km in keyconfig.keymaps if km.name.startswith("3D View Tool:"))


def _is_rmb_cancel(kmi):
    """Right button press that cancels (or starts a new cut in the knife)."""
    return (kmi.type == 'RIGHTMOUSE' and kmi.value == 'PRESS' and kmi.propvalue in ('CANCEL', 'NEW_CUT')
            and kmi.shift != 1 and kmi.ctrl != 1 and kmi.alt != 1)


def _is_typing_shortcut(kmi):
    return (kmi.type in TEXT_KEYS and kmi.ctrl != 1 and kmi.alt != 1 and kmi.oskey != 1
            and kmi.idname not in KEPT_SHORTCUTS)


# --- Candidates next to the input line ---------------------------------------

CANDIDATE_COLORS = {
    # (the one Enter runs, the others) per dialect: Blender's names in blue.
    commands.RHINO: ((0.95, 0.75, 0.3, 1.0), (0.6, 0.6, 0.6, 1.0)),
    commands.BLENDER: ((0.45, 0.75, 1.0, 1.0), (0.4, 0.55, 0.75, 1.0)),
}
NOTE_COLOR = (0.55, 0.55, 0.55, 1.0)


def candidate_row(text, mode=None):
    """[(text, color)] drawn right-aligned next to the input line."""
    found = commands.lookup(text, mode, operators=False)
    matches = found.matches[:6]
    if found.command is not None and all(m[1] is not found.command for m in matches):
        matches.insert(0, (commands.typed_name(found.command, text), found.command,
                           commands.RHINO if found.command.rhino_name else commands.BLENDER))
    if not matches:
        return [(found.message, NOTE_COLOR)] if found.message and text.strip() else []
    row = []
    for index, (name, command, dialect) in enumerate(matches):
        runs = command is found.command or (found.command is None and index == 0)
        row.append((name, CANDIDATE_COLORS[dialect][0 if runs else 1]))
        note = commands.detail(command, name) if index == 0 else ""
        if note:
            row.append((note, NOTE_COLOR))
    if len(found.matches) > len(matches):
        row.append(("...", NOTE_COLOR))
    return row


def _draw_candidates():
    context = bpy.context
    space = context.space_data
    if space.language != LANGUAGE or not len(space.history):
        return
    row = candidate_row(space.history[-1].body)
    if not row:
        return
    scale = context.preferences.system.ui_scale
    font = 0
    blf.size(font, 11 * scale)
    x = context.region.width - 12 * scale
    y = 6 * scale
    for index in reversed(range(len(row))):
        text, color = row[index]
        text += "" if index == len(row) - 1 else "  "
        x -= blf.dimensions(font, text)[0]
        blf.position(font, x, y, 0)
        blf.color(font, *color)
        blf.draw(font, text)


# --- The line outside the Rhino workspace ------------------------------------

_MESSAGE_COLORS = {'ERROR': (1.0, 0.45, 0.4, 1.0), 'INPUT': (0.75, 0.75, 0.75, 1.0)}


def _draw_viewport_line():
    """In the viewport typed in, outside the Rhino workspace: the prompt and
    what is typed, and the last message above it."""
    context = bpy.context
    window = context.window
    if window is None or find_console(window.screen) is not None:
        return
    target = _target_viewport(window)
    if target is None or context.area is None or context.area.as_pointer() != target.as_pointer():
        return
    recent = _message is not None and time.monotonic() - _message[2] < MESSAGE_SECONDS
    if not (_buffer or _prompt != PROMPT or recent):
        return
    scale = context.preferences.system.ui_scale
    font = 0
    blf.size(font, 12 * scale)
    x, y = 12 * scale, 10 * scale
    line = _prompt + _buffer + "_"
    blf.color(font, 1.0, 1.0, 1.0, 1.0)
    blf.position(font, x, y, 0)
    blf.draw(font, line)
    if recent:
        text, line_type, _ = _message
        blf.color(font, *_MESSAGE_COLORS.get(line_type, (0.85, 0.85, 0.85, 1.0)))
        blf.position(font, x, y + 18 * scale, 0)
        blf.draw(font, text)


# --- Registration -------------------------------------------------------------

def register():
    global _draw_handle
    for name in _LANGUAGE_MODULE_NAMES:
        sys.modules[name] = _language
    for cls in _classes:
        bpy.utils.register_class(cls)
    _add_keymap_items()
    _muted.mute(bpy.context.window_manager.keyconfigs.active, _typing_keymaps, _is_typing_shortcut)
    _modal_rmb.change(bpy.context.window_manager.keyconfigs.active, MODAL_RMB_KEYMAPS, _is_rmb_cancel,
                      propvalue='CONFIRM', value='RELEASE')
    keymaps.update_keyconfigs()
    _draw_handle = bpy.types.SpaceConsole.draw_handler_add(_draw_candidates, (), 'WINDOW', 'POST_PIXEL')
    global _viewport_draw_handle
    _viewport_draw_handle = bpy.types.SpaceView3D.draw_handler_add(_draw_viewport_line, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    global _draw_handle, _last_command, _last_viewport, _prompt, _viewport_draw_handle, _buffer, _message
    if _draw_handle is not None:
        bpy.types.SpaceConsole.draw_handler_remove(_draw_handle, 'WINDOW')
        _draw_handle = None
    if _viewport_draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_viewport_draw_handle, 'WINDOW')
        _viewport_draw_handle = None
    _buffer, _message = "", None
    _history.clear()
    _muted.restore()
    _modal_rmb.restore()
    _addon_items.remove()
    keymaps.update_keyconfigs()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    for name in _LANGUAGE_MODULE_NAMES:
        if sys.modules.get(name) is _language:
            del sys.modules[name]
    _scrollback.clear()
    _last_command = None
    _last_viewport = None
    _prompt = PROMPT
