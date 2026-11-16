# SPDX-License-Identifier: GPL-3.0-or-later
"""Keymap helpers shared by MODE features.

R2B's own shortcuts go into the add-on keyconfig. Conflicting built-in items
are only switched off in memory (``KeyMapItem.active``) and switched back on
when the feature is unregistered, so nothing is written to the user's
preferences.
"""

import bpy


def signature(kmi):
    return (kmi.idname, kmi.type, kmi.value, kmi.shift, kmi.ctrl, kmi.alt,
            kmi.oskey, kmi.any, kmi.key_modifier)


def update_keyconfigs():
    keyconfigs = bpy.context.window_manager.keyconfigs
    if hasattr(keyconfigs, "update"):
        keyconfigs.update()


# MutedItems in use, so they can follow the active keyconfig (see ``follow_active_keyconfig``).
_in_use = []


class MutedItems:
    """Built-in keymap items switched off while a feature is on. They are
    muted in the active keyconfig; when the user switches to another keymap
    preset, or Blender rebuilds it (e.g. after changing Select with Left/Right
    in the preferences), ``follow_active_keyconfig`` mutes them there again."""

    def __init__(self):
        # (keyconfig name, keymap name, item index, signature)
        self._items = []
        # (keymap names or a function of the keyconfig giving them, predicate)
        self._rules = []
        self._keyconfig = None  # Pointer of the keyconfig muted in.

    def mute(self, keyconfig, keymap_names, predicate):
        self._rules.append((keymap_names, predicate))
        self._apply(keyconfig, keymap_names, predicate)
        self._keyconfig = keyconfig.as_pointer()
        if self not in _in_use:
            _in_use.append(self)

    def _apply(self, keyconfig, keymap_names, predicate):
        names = keymap_names(keyconfig) if callable(keymap_names) else keymap_names
        for keymap_name in names:
            keymap = keyconfig.keymaps.get(keymap_name)
            if keymap is None or keymap.is_modal:
                continue
            for index, kmi in enumerate(keymap.keymap_items):
                if kmi.active and predicate(kmi):
                    kmi.active = False
                    self._items.append((keyconfig.name, keymap_name, index, signature(kmi)))

    def _restore_items(self):
        keyconfigs = bpy.context.window_manager.keyconfigs
        for keyconfig_name, keymap_name, index, sig in reversed(self._items):
            keyconfig = keyconfigs.get(keyconfig_name)
            keymap = keyconfig.keymaps.get(keymap_name) if keyconfig else None
            if keymap is None:
                continue
            items = keymap.keymap_items
            if index < len(items) and signature(items[index]) == sig:
                items[index].active = True
                continue
            # The keymap changed since muting; fall back to matching by signature.
            for kmi in items:
                if not kmi.active and signature(kmi) == sig:
                    kmi.active = True
                    break
        self._items.clear()

    def restore(self):
        self._restore_items()
        self._rules.clear()
        self._keyconfig = None
        if self in _in_use:
            _in_use.remove(self)

    def still_muted(self, keyconfig):
        """Whether the items are still muted in ``keyconfig``."""
        if keyconfig.as_pointer() != self._keyconfig:
            return False
        for keyconfig_name, keymap_name, index, sig in self._items:
            keymap = keyconfig.keymaps.get(keymap_name) if keyconfig.name == keyconfig_name else None
            items = keymap.keymap_items if keymap is not None else ()
            if keymap is None or index >= len(items) or signature(items[index]) != sig or items[index].active:
                return False
        return True

    def follow(self, keyconfig):
        """Mute the same kinds of items in ``keyconfig`` instead."""
        self._restore_items()
        for keymap_names, predicate in self._rules:
            self._apply(keyconfig, keymap_names, predicate)
        self._keyconfig = keyconfig.as_pointer()


class ChangedItems:
    """Built-in items (e.g. of modal keymaps) changed in memory while a
    feature is on, and changed back when it is turned off. Follows the
    active keyconfig like ``MutedItems``."""

    def __init__(self):
        # (keyconfig name, keymap name, item index, signature after the change, {attribute: old value})
        self._items = []
        self._rules = []  # (keymap names, predicate, {attribute: new value})
        self._keyconfig = None

    def change(self, keyconfig, keymap_names, predicate, **values):
        self._rules.append((keymap_names, predicate, values))
        self._apply(keyconfig, keymap_names, predicate, values)
        self._keyconfig = keyconfig.as_pointer()
        if self not in _in_use:
            _in_use.append(self)

    def _apply(self, keyconfig, keymap_names, predicate, values):
        for keymap_name in keymap_names:
            keymap = keyconfig.keymaps.get(keymap_name)
            if keymap is None:
                continue
            for index, kmi in enumerate(keymap.keymap_items):
                if not predicate(kmi):
                    continue
                old = {name: getattr(kmi, name) for name in values}
                for name, value in values.items():
                    setattr(kmi, name, value)
                self._items.append((keyconfig.name, keymap_name, index, signature(kmi), old))

    def _find(self, keyconfig_name, keymap_name, index, sig):
        keyconfig = bpy.context.window_manager.keyconfigs.get(keyconfig_name)
        keymap = keyconfig.keymaps.get(keymap_name) if keyconfig else None
        if keymap is None:
            return None
        items = keymap.keymap_items
        if index < len(items) and signature(items[index]) == sig:
            return items[index]
        return next((kmi for kmi in items if signature(kmi) == sig), None)

    def _restore_items(self):
        for keyconfig_name, keymap_name, index, sig, old in reversed(self._items):
            kmi = self._find(keyconfig_name, keymap_name, index, sig)
            if kmi is not None:
                for name, value in old.items():
                    setattr(kmi, name, value)
        self._items.clear()

    def restore(self):
        self._restore_items()
        self._rules.clear()
        self._keyconfig = None
        if self in _in_use:
            _in_use.remove(self)

    def still_muted(self, keyconfig):
        """Whether the items are still changed in ``keyconfig`` (same name as
        ``MutedItems`` for ``follow_active_keyconfig``)."""
        if keyconfig.as_pointer() != self._keyconfig:
            return False
        for keyconfig_name, keymap_name, index, sig, old in self._items:
            keymap = keyconfig.keymaps.get(keymap_name) if keyconfig.name == keyconfig_name else None
            items = keymap.keymap_items if keymap is not None else ()
            if keymap is None or index >= len(items) or signature(items[index]) != sig:
                return False
        return True

    def follow(self, keyconfig):
        self._restore_items()
        for keymap_names, predicate, values in self._rules:
            self._apply(keyconfig, keymap_names, predicate, values)
        self._keyconfig = keyconfig.as_pointer()


def follow_active_keyconfig():
    """Re-mute where the active keyconfig changed or was rebuilt. Returns
    True if anything was muted again."""
    keyconfig = bpy.context.window_manager.keyconfigs.active
    if keyconfig is None:
        return False
    changed = [muted for muted in _in_use if not muted.still_muted(keyconfig)]
    for muted in changed:
        muted.follow(keyconfig)
    if changed:
        update_keyconfigs()
    return bool(changed)


class AddonItems:
    """R2B's own items in the add-on keyconfig.

    Blender inserts add-on items at the head of a keymap one by one, so items
    of the same keymap end up in reverse order of adding: add the item that
    must be checked first last.
    """

    def __init__(self):
        self._items = []

    def add(self, idname, event_type, value, keymap="3D View", space_type='VIEW_3D',
            region_type='WINDOW', properties=None, **modifiers):
        keyconfig = bpy.context.window_manager.keyconfigs.addon
        km = keyconfig.keymaps.new(name=keymap, space_type=space_type, region_type=region_type)
        kmi = km.keymap_items.new(idname, event_type, value, **modifiers)
        for name, value in (properties or {}).items():
            setattr(kmi.properties, name, value)
        self._items.append((km, kmi))
        return kmi

    def remove(self):
        for km, kmi in self._items:
            km.keymap_items.remove(kmi)
        self._items.clear()
