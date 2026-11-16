# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse what is typed in the command line while a command asks for a point.

Rhino's point input, in construction plane (CPlane) coordinates:

* ``3,4`` or ``3,4,5``: absolute point (z defaults to 0)
* ``@3,4`` or ``r3,4``: relative to the previous point
* ``5<30``: polar, 5 units at 30 degrees from the CPlane origin
* ``@5<30`` or ``r5<30``: polar, relative to the previous point
* ``w`` prefix (``w3,4,5``, ``w@1,0``): world coordinates instead of CPlane
* ``5``: distance constraint, the next point is 5 units from the previous one
  in the direction of the mouse
* ``<30``: angle constraint, the next point is at 30 degrees from the
  previous one, at the distance of the mouse
* anything else is a command option (``Close``, ``Undo``, ``3Point``, ...)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PointInput:
    x: float
    y: float
    z: float = 0.0
    relative: bool = False
    world: bool = False


@dataclass(frozen=True)
class PolarInput:
    distance: float
    angle: float  # degrees, counterclockwise from the x axis
    relative: bool = False
    world: bool = False


@dataclass(frozen=True)
class DistanceInput:
    distance: float


@dataclass(frozen=True)
class AngleInput:
    angle: float


@dataclass(frozen=True)
class OptionInput:
    name: str


class InputError(ValueError):
    """The text looks like coordinates but cannot be read."""


def _number(text):
    try:
        return float(text)
    except ValueError:
        raise InputError(f"'{text}' is not a number") from None


def _is_numeric_start(text):
    return bool(text) and (text[0].isdigit() or text[0] in "+-.@<")


def parse(text):
    """Parse a point input; raises InputError for malformed coordinates."""
    text = text.strip().replace(" ", "")
    if not text:
        raise InputError("nothing typed")
    lowered = text.lower()

    world = False
    if lowered[0] == "w" and _is_numeric_start(lowered[1:]):
        world = True
        lowered = lowered[1:]
    relative = False
    if lowered[0] == "@" or (lowered[0] == "r" and _is_numeric_start(lowered[1:])):
        relative = True
        lowered = lowered[1:]

    if not _is_numeric_start(lowered):
        if world or relative:
            raise InputError(f"'{text}' is not a point")
        return OptionInput(text)

    if lowered.startswith("<"):
        if world or relative:
            raise InputError(f"'{text}' is not a point")
        return AngleInput(_number(lowered[1:]))
    if "<" in lowered:
        distance, angle = lowered.split("<", 1)
        return PolarInput(_number(distance), _number(angle), relative, world)
    if "," in lowered:
        parts = lowered.split(",")
        if len(parts) not in (2, 3):
            raise InputError(f"'{text}' needs 2 or 3 coordinates")
        values = [_number(p) for p in parts]
        return PointInput(*values, relative=relative, world=world)
    if world or relative:
        raise InputError(f"'{text}' is not a point")
    try:
        return DistanceInput(float(lowered))
    except ValueError:
        if any(c.isalpha() for c in lowered):
            return OptionInput(text)  # An option starting with a digit, e.g. 3Point.
        raise InputError(f"'{text}' is not a number") from None
