# Copyright (C) 2026 xsm909
#
# This file is part of xcommander-plugins.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""EPS — and the reason this is an interpreter rather than a reader.

**A PostScript file is a program.** Nothing in it says "here is a rectangle";
what it says is "put 4 on the stack, put 5 on the stack, call the thing called
`rectfill`". And nearly every EPS in the world begins with a *prolog* that
defines its own shorthand — Illustrator's files draw with `m`, `l`, `c` and `f`,
which exist only because the file itself defined them a hundred lines earlier in
terms of the real operators. A reader that pattern-matched on operator names
would work on one exporter and on nothing else.

So this is a small stack machine: a tokeniser, an operand stack, a dictionary
stack, procedures that can be defined and called, and the drawing operators at
the bottom of it. A file's own definitions then work by construction, because
they are written in the language this executes.

**What it is not.** It is not a PostScript implementation: no fonts and no text,
no images, no clipping, no patterns or shading dictionaries, no `for`/`loop`
beyond the simple forms, no error handler. What it cannot do it counts and says
out loud. And it is bounded — a program with a runaway loop must not take the
application with it.
"""

from __future__ import annotations

import math
import re

#: How many operators may run before this gives up on a file. A drawing is
#: tens of thousands; a runaway loop is unbounded, and this is a preview.
MAX_STEPS = 4_000_000

#: How deep procedures may call each other.
MAX_DEPTH = 100


class PostScriptError(Exception):
    """The file is not one, or is one this cannot run."""


class Underflow(PostScriptError):
    """An operator wanted more than the stack had.

    **Survivable, and deliberately so.** A prolog this machine only half
    understands leaves the stack in a state its own operators did not expect,
    and the choice is between drawing nothing and drawing everything that did
    make sense. A preview draws what it can and says what it could not.
    """ 


class Name:
    """A bare name — `moveto` — as against a literal one, `/moveto`."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "Name(%s)" % self.text


class Literal:
    """`/name`: pushed, not executed."""

    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __repr__(self):
        return "/%s" % self.text


class Procedure:
    """`{ … }`: a list of tokens, run when something calls it."""

    __slots__ = ("body",)

    def __init__(self, body):
        self.body = body


NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")

#: `16#020` — a number in a radix, which PostScript allows and which turns up
#: in the middle of otherwise ordinary files.
RADIX = re.compile(r"(\d+)#([0-9A-Za-z]+)$")


def tokenise(text: str):
    """The program as tokens, with `{}` already nested.

    Comments go, `()` strings are kept whole because a name inside one is not a
    name, and `<…>` hex strings are kept as strings for the same reason.
    """
    out = []
    stack = [out]
    at = 0
    length = len(text)
    while at < length:
        char = text[at]
        if char in " \t\r\n\f\0":
            at += 1
        elif char == "%":
            end = text.find("\n", at)
            at = length if end < 0 else end + 1
        elif char == "{":
            new = []
            stack[-1].append(new)
            stack.append(new)
            at += 1
        elif char == "}":
            if len(stack) > 1:
                body = stack.pop()
                stack[-1][-1] = Procedure(body)
            at += 1
        elif char == "(":
            depth = 1
            start = at + 1
            at += 1
            while at < length and depth:
                if text[at] == "\\":
                    at += 2
                    continue
                if text[at] == "(":
                    depth += 1
                elif text[at] == ")":
                    depth -= 1
                at += 1
            stack[-1].append(text[start:at - 1])
        elif char == "<":
            end = text.find(">", at)
            at = length if end < 0 else end + 1
            stack[-1].append("")
        elif char == "[":
            stack[-1].append(Name("["))
            at += 1
        elif char == "]":
            stack[-1].append(Name("]"))
            at += 1
        elif char == "/":
            end = at + 1
            while end < length and text[end] not in " \t\r\n\f/{}()[]<>%":
                end += 1
            stack[-1].append(Literal(text[at + 1:end]))
            at = end
        else:
            end = at
            while end < length and text[end] not in " \t\r\n\f/{}()[]<>%":
                end += 1
            word = text[at:end] if end > at else text[at]
            at = end if end > at else at + 1
            radix = RADIX.match(word)
            if NUMBER.match(word):
                stack[-1].append(float(word))
            elif radix:
                stack[-1].append(float(int(radix.group(2), int(radix.group(1)))))
            else:
                stack[-1].append(Name(word))
    return out


class Machine:
    """The interpreter, and the drawing it is building."""

    def __init__(self, notes):
        self.stack = []
        self.dicts = [{}]
        self.notes = notes
        self.steps = 0
        self.unknown = set()
        self.starved = 0

        # The graphics state: a transform, a colour, a line width, and the path
        # being built. `gsave`/`grestore` push and pop all of it but the path,
        # which is what the specification says.
        self.matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.fill = (0.0, 0.0, 0.0)
        self.line_width = 1.0
        self.saved = []
        self.path = []                 # [(verb, [numbers in user space])]
        self.here = (0.0, 0.0)
        self.start = (0.0, 0.0)
        self.shapes = []

    # ------------------------------------------------------------ execution

    def run(self, tokens, depth=0):
        if depth > MAX_DEPTH:
            raise PostScriptError("This file calls itself too deeply.")
        for token in tokens:
            self.steps += 1
            if self.steps > MAX_STEPS:
                raise PostScriptError(
                    "This file is doing more work than a preview can wait for."
                )
            if isinstance(token, Name):
                self.execute(token.text, depth)
            elif isinstance(token, list):
                self.stack.append(Procedure(token))
            else:
                self.stack.append(token)

    def execute(self, word, depth):
        found = self.lookup(word)
        if found is not None:
            if isinstance(found, Procedure):
                self.run(found.body, depth + 1)
            else:
                self.stack.append(found)
            return
        handler = OPERATORS.get(word)
        if handler is not None:
            try:
                handler(self)
            except Underflow:
                # The one error worth surviving — see [Underflow].
                self.starved += 1
            return
        self.unknown.add(word)

    def lookup(self, word):
        for table in reversed(self.dicts):
            if word in table:
                return table[word]
        return None

    # ------------------------------------------------------------- the stack

    def pop(self, count=1):
        if len(self.stack) < count:
            raise Underflow("an operator wanted more values than there were")
        out = self.stack[-count:]
        del self.stack[-count:]
        return out

    def numbers(self, count):
        values = self.pop(count)
        out = []
        for value in values:
            if isinstance(value, (int, float)):
                out.append(float(value))
            else:
                out.append(0.0)
        return out

    # ------------------------------------------------------------- the paint

    def point(self, x, y):
        a, b, c, d, e, f = self.matrix
        return (a * x + c * y + e, b * x + d * y + f)

    def keep(self, stroke):
        """The path so far, as one shape, and then forgotten."""
        if not self.path:
            return
        verbs = []
        points = []
        for verb, values in self.path:
            verbs.append(verb)
            points.extend(values)
        colour = "#%02x%02x%02x" % tuple(
            max(0, min(255, round(channel * 255))) for channel in self.fill
        )
        scale = math.sqrt(
            abs(self.matrix[0] * self.matrix[3] - self.matrix[1] * self.matrix[2])
        ) or 1.0
        self.shapes.append({
            "verbs": verbs,
            "points": points,
            "fill": None if stroke else colour + "ff",
            "stroke": colour + "ff" if stroke else None,
            "strokeWidth": self.line_width * scale,
        })
        self.path = []


# ------------------------------------------------------------- the operators


def _def(machine):
    value, key = machine.pop(2)[::-1]
    if isinstance(key, Literal):
        machine.dicts[-1][key.text] = value


def _moveto(machine):
    x, y = machine.numbers(2)
    machine.here = (x, y)
    machine.start = (x, y)
    machine.path.append((0, list(machine.point(x, y))))


def _rmoveto(machine):
    dx, dy = machine.numbers(2)
    x, y = machine.here[0] + dx, machine.here[1] + dy
    machine.here = (x, y)
    machine.start = (x, y)
    machine.path.append((0, list(machine.point(x, y))))


def _lineto(machine):
    x, y = machine.numbers(2)
    machine.here = (x, y)
    machine.path.append((1, list(machine.point(x, y))))


def _rlineto(machine):
    dx, dy = machine.numbers(2)
    x, y = machine.here[0] + dx, machine.here[1] + dy
    machine.here = (x, y)
    machine.path.append((1, list(machine.point(x, y))))


def _curveto(machine):
    x1, y1, x2, y2, x3, y3 = machine.numbers(6)
    machine.here = (x3, y3)
    machine.path.append((2, [
        *machine.point(x1, y1), *machine.point(x2, y2), *machine.point(x3, y3),
    ]))


def _rcurveto(machine):
    dx1, dy1, dx2, dy2, dx3, dy3 = machine.numbers(6)
    x, y = machine.here
    x1, y1 = x + dx1, y + dy1
    x2, y2 = x + dx2, y + dy2
    x3, y3 = x + dx3, y + dy3
    machine.here = (x3, y3)
    machine.path.append((2, [
        *machine.point(x1, y1), *machine.point(x2, y2), *machine.point(x3, y3),
    ]))


def _closepath(machine):
    if machine.path:
        machine.path.append((3, []))
        machine.here = machine.start


def _newpath(machine):
    machine.path = []


def _fill(machine):
    machine.keep(stroke=False)


def _stroke(machine):
    machine.keep(stroke=True)


def _rect(machine, fill=True):
    x, y, width, height = machine.numbers(4)
    machine.path = [
        (0, list(machine.point(x, y))),
        (1, list(machine.point(x + width, y))),
        (1, list(machine.point(x + width, y + height))),
        (1, list(machine.point(x, y + height))),
        (3, []),
    ]
    machine.keep(stroke=not fill)


def _setgray(machine):
    grey, = machine.numbers(1)
    machine.fill = (grey, grey, grey)


def _setrgb(machine):
    red, green, blue = machine.numbers(3)
    machine.fill = (red, green, blue)


def _setcmyk(machine):
    cyan, magenta, yellow, black = machine.numbers(4)
    machine.fill = (
        (1 - cyan) * (1 - black),
        (1 - magenta) * (1 - black),
        (1 - yellow) * (1 - black),
    )


def _sethsb(machine):
    import colorsys
    hue, saturation, brightness = machine.numbers(3)
    machine.fill = colorsys.hsv_to_rgb(hue, saturation, brightness)


def _setlinewidth(machine):
    machine.line_width, = machine.numbers(1)


def _gsave(machine):
    machine.saved.append((machine.matrix, machine.fill, machine.line_width))


def _grestore(machine):
    if machine.saved:
        machine.matrix, machine.fill, machine.line_width = machine.saved.pop()


def _translate(machine):
    x, y = machine.numbers(2)
    machine.matrix = _compose(machine.matrix, (1.0, 0.0, 0.0, 1.0, x, y))


def _scale(machine):
    x, y = machine.numbers(2)
    machine.matrix = _compose(machine.matrix, (x, 0.0, 0.0, y, 0.0, 0.0))


def _rotate(machine):
    angle, = machine.numbers(1)
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    machine.matrix = _compose(machine.matrix, (cos, sin, -sin, cos, 0.0, 0.0))


def _concat(machine):
    values = machine.pop(1)[0]
    if isinstance(values, list) and len(values) == 6:
        try:
            machine.matrix = _compose(
                machine.matrix, tuple(float(value) for value in values)
            )
        except (TypeError, ValueError):
            pass


def _compose(a, b):
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def _mark(machine):
    machine.stack.append(Name("["))


def _array_end(machine):
    """`]` — everything back to the mark, as a list."""
    out = []
    while machine.stack:
        value = machine.stack.pop()
        if isinstance(value, Name) and value.text == "[":
            break
        out.append(value)
    machine.stack.append(out[::-1])


def _dup(machine):
    value = machine.pop(1)[0]
    machine.stack += [value, value]


def _exch(machine):
    a, b = machine.pop(2)
    machine.stack += [b, a]


def _index(machine):
    count, = machine.numbers(1)
    at = int(count)
    if 0 <= at < len(machine.stack):
        machine.stack.append(machine.stack[-1 - at])


def _roll(machine):
    count, shift = machine.numbers(2)
    count, shift = int(count), int(shift)
    if 0 < count <= len(machine.stack):
        part = machine.stack[-count:]
        del machine.stack[-count:]
        shift %= count
        machine.stack += part[-shift:] + part[:-shift]


def _if(machine):
    procedure, condition = machine.pop(2)[::-1]
    if condition is True and isinstance(procedure, Procedure):
        machine.run(procedure.body, 1)


def _ifelse(machine):
    otherwise, procedure, condition = machine.pop(3)[::-1]
    chosen = procedure if condition is True else otherwise
    if isinstance(chosen, Procedure):
        machine.run(chosen.body, 1)


def _for(machine):
    procedure, limit, step, start = machine.pop(4)[::-1]
    if not isinstance(procedure, Procedure):
        return
    try:
        value, step, limit = float(start), float(step), float(limit)
    except (TypeError, ValueError):
        return
    if step == 0:
        return
    while (step > 0 and value <= limit) or (step < 0 and value >= limit):
        machine.stack.append(value)
        machine.run(procedure.body, 1)
        value += step


def _repeat(machine):
    procedure, count = machine.pop(2)[::-1]
    if isinstance(procedure, Procedure) and isinstance(count, (int, float)):
        for _ in range(min(int(count), 100000)):
            machine.run(procedure.body, 1)


def _same(value):
    """A value in a form two of them can be compared in.

    Names and literal names are compared by what they say — `/fill eq` is how
    every Illustrator file asks what state it is in, and comparing the objects
    instead makes every one of those answers false. That is what left a whole
    drawing black: the file never got as far as setting a colour.
    """
    if isinstance(value, (Literal, Name)):
        return ("name", value.text)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, str):
        return ("string", value)
    return ("thing", id(value))


def _equality(want_same):
    def run(machine):
        b, a = machine.pop(2)[::-1]
        machine.stack.append((_same(a) == _same(b)) is want_same)
    return run


def _order(operation):
    def run(machine):
        b, a = machine.pop(2)[::-1]
        first, second = _same(a), _same(b)
        if first[0] == second[0] == "number" or (
            first[0] == second[0] == "string"
        ):
            machine.stack.append(bool(operation(first[1], second[1])))
        else:
            machine.stack.append(False)
    return run


def _binary(operation):
    def run(machine):
        b, a = machine.pop(2)[::-1]
        try:
            machine.stack.append(operation(float(a), float(b)))
        except (TypeError, ValueError, ZeroDivisionError):
            machine.stack.append(0.0)
    return run


def _unary(operation):
    def run(machine):
        value, = machine.pop(1)
        try:
            machine.stack.append(operation(float(value)))
        except (TypeError, ValueError):
            machine.stack.append(0.0)
    return run


def _begin(machine):
    value = machine.pop(1)[0]
    machine.dicts.append(value if isinstance(value, dict) else {})


def _end(machine):
    if len(machine.dicts) > 1:
        machine.dicts.pop()


def _dict(machine):
    machine.pop(1)
    machine.stack.append({})


def _load(machine):
    key = machine.pop(1)[0]
    if isinstance(key, Literal):
        found = machine.lookup(key.text)
        machine.stack.append(found if found is not None else Procedure([]))


def _exec(machine):
    value = machine.pop(1)[0]
    if isinstance(value, Procedure):
        machine.run(value.body, 1)


def _put(machine):
    """`dict key value put`, and the array form beside it.

    **This is the operator Illustrator files stand on.** Their prolog builds a
    dictionary of its own shorthand and stores it in `userdict` with `put`;
    without it, every one of those definitions is written into a dictionary
    nobody can find again, and the file draws nothing at all while looking
    perfectly well-formed.
    """
    target, key, value = machine.pop(3)
    if isinstance(target, dict):
        name = key.text if isinstance(key, Literal) else key
        try:
            target[name] = value
        except TypeError:
            pass
    elif isinstance(target, list) and isinstance(key, (int, float)):
        at = int(key)
        if 0 <= at < len(target):
            target[at] = value


def _get(machine):
    target, key = machine.pop(2)
    if isinstance(target, dict):
        name = key.text if isinstance(key, Literal) else key
        machine.stack.append(target.get(name))
    elif isinstance(target, (list, str)) and isinstance(key, (int, float)):
        at = int(key)
        machine.stack.append(target[at] if 0 <= at < len(target) else None)
    else:
        machine.stack.append(None)


def _known(machine):
    target, key = machine.pop(2)
    name = key.text if isinstance(key, Literal) else key
    machine.stack.append(isinstance(target, dict) and name in target)


def _where(machine):
    """Which dictionary a name lives in — and **operators count**.

    A prolog asks `/setcmykcolor where not { … define a fallback … } if`
    because on an old printer that operator did not exist. Answering "not
    found" for an operator this machine *does* implement makes every file
    install its own emulation and hand us colours through a conversion nobody
    wanted: the Tcl logo came out grey. Saying yes is both truer and better.
    """
    key = machine.pop(1)[0]
    name = key.text if isinstance(key, Literal) else key
    for table in reversed(machine.dicts):
        if name in table:
            machine.stack += [table, True]
            return
    if name in OPERATORS:
        machine.stack += [{}, True]
        return
    machine.stack.append(False)


def _length(machine):
    value = machine.pop(1)[0]
    try:
        machine.stack.append(float(len(value)))
    except TypeError:
        machine.stack.append(0.0)


def _array(machine):
    count, = machine.numbers(1)
    machine.stack.append([None] * max(0, min(int(count), 100000)))


def _aload(machine):
    value = machine.pop(1)[0]
    if isinstance(value, list):
        machine.stack += value
    machine.stack.append(value)


def _astore(machine):
    value = machine.pop(1)[0]
    if isinstance(value, list) and value:
        taken = machine.pop(len(value))
        machine.stack.append(list(taken))
    else:
        machine.stack.append(value)


def _cleartomark(machine):
    while machine.stack:
        value = machine.stack.pop()
        if isinstance(value, Name) and value.text == "[":
            return


def _counttomark(machine):
    for depth, value in enumerate(reversed(machine.stack)):
        if isinstance(value, Name) and value.text == "[":
            machine.stack.append(float(depth))
            return
    machine.stack.append(float(len(machine.stack)))


def _copy(machine):
    count, = machine.numbers(1)
    at = int(count)
    if 0 < at <= len(machine.stack):
        machine.stack += machine.stack[-at:]


def _logical(operation):
    def run(machine):
        b, a = machine.pop(2)[::-1]
        if isinstance(a, bool) or isinstance(b, bool):
            machine.stack.append(operation(bool(a), bool(b)))
        else:
            try:
                machine.stack.append(float(operation(int(a), int(b))))
            except (TypeError, ValueError):
                machine.stack.append(0.0)
    return run


def _not(machine):
    value = machine.pop(1)[0]
    if isinstance(value, bool):
        machine.stack.append(not value)
    else:
        try:
            machine.stack.append(float(~int(value)))
        except (TypeError, ValueError):
            machine.stack.append(0.0)


def _currentpoint(machine):
    """Where the path is, which several files ask for in the middle of a curve.

    Illustrator's own shorthand for a curve with one control point is defined
    as `currentpoint 6 2 roll curveto` — so without this the stack is two
    numbers short and the drawing comes out as a spray of lines from nowhere.
    That is exactly what it did.
    """
    machine.stack += [machine.here[0], machine.here[1]]


def _matrix(machine):
    machine.stack.append([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])


def _currentmatrix(machine):
    machine.pop(1)
    machine.stack.append(list(machine.matrix))


def _concatmatrix(machine):
    out, b, a = machine.pop(3)[::-1]
    try:
        product = list(_compose(tuple(a), tuple(b)))
    except (TypeError, ValueError):
        product = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    machine.stack.append(product)


def _transform(machine):
    """`x y transform` — a point through the current transform."""
    value = machine.pop(1)[0]
    if isinstance(value, list):
        matrix = tuple(value)
        x, y = machine.numbers(2)
    else:
        matrix = machine.matrix
        x, y = machine.numbers(1)[0], value
    a, b, c, d, e, f = matrix
    machine.stack += [a * x + c * y + e, b * x + d * y + f]


def _dtransform(machine):
    """The same without the move, which is what a distance wants."""
    value = machine.pop(1)[0]
    if isinstance(value, list):
        matrix = tuple(value)
        x, y = machine.numbers(2)
    else:
        matrix = machine.matrix
        x, y = machine.numbers(1)[0], value
    a, b, c, d = matrix[:4]
    machine.stack += [a * x + c * y, b * x + d * y]


def _invert(matrix):
    a, b, c, d, e, f = matrix
    det = a * d - b * c
    if not det:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (
        d / det, -b / det, -c / det, a / det,
        (c * f - d * e) / det, (b * e - a * f) / det,
    )


def _itransform(machine):
    """The **inverse** of [_transform], and it has to really be the inverse.

    Illustrator's own files round a point to the pixel grid with
    `transform … itransform`, so the pair must come back where it started.
    Aliasing this to the forward transform applies it twice: every coordinate
    in the file lands somewhere else, and the drawing comes out as a spray of
    lines. That is what it did.
    """
    value = machine.pop(1)[0]
    if isinstance(value, list):
        matrix = _invert(tuple(value))
        x, y = machine.numbers(2)
    else:
        matrix = _invert(machine.matrix)
        x, y = machine.numbers(1)[0], value
    a, b, c, d, e, f = matrix
    machine.stack += [a * x + c * y + e, b * x + d * y + f]


def _idtransform(machine):
    value = machine.pop(1)[0]
    if isinstance(value, list):
        matrix = _invert(tuple(value))
        x, y = machine.numbers(2)
    else:
        matrix = _invert(machine.matrix)
        x, y = machine.numbers(1)[0], value
    a, b, c, d = matrix[:4]
    machine.stack += [a * x + c * y, b * x + d * y]


def _forall(machine):
    procedure, value = machine.pop(2)[::-1]
    if not isinstance(procedure, Procedure):
        return
    if isinstance(value, list):
        for item in list(value)[:100000]:
            machine.stack.append(item)
            machine.run(procedure.body, 1)
    elif isinstance(value, dict):
        for key, item in list(value.items()):
            machine.stack += [Literal(str(key)), item]
            machine.run(procedure.body, 1)


def _stopped(machine):
    value = machine.pop(1)[0]
    if isinstance(value, Procedure):
        try:
            machine.run(value.body, 1)
        except Underflow:
            machine.starved += 1
    machine.stack.append(False)


def _nothing(machine):
    pass


def _drop(count):
    def run(machine):
        machine.pop(count)
    return run


OPERATORS = {
    "def": _def,
    "moveto": _moveto, "rmoveto": _rmoveto,
    "lineto": _lineto, "rlineto": _rlineto,
    "curveto": _curveto, "rcurveto": _rcurveto,
    "closepath": _closepath, "newpath": _newpath,
    "fill": _fill, "eofill": _fill, "stroke": _stroke,
    "rectfill": lambda m: _rect(m, True),
    "rectstroke": lambda m: _rect(m, False),
    "setgray": _setgray, "setrgbcolor": _setrgb, "setcmykcolor": _setcmyk,
    "sethsbcolor": _sethsb,
    "setlinewidth": _setlinewidth,
    "gsave": _gsave, "grestore": _grestore, "save": _gsave, "restore": _grestore,
    "translate": _translate, "scale": _scale, "rotate": _rotate,
    "concat": _concat,
    "[": _mark, "]": _array_end, "mark": _mark,
    "dup": _dup, "exch": _exch, "index": _index, "roll": _roll,
    "pop": _drop(1),
    "if": _if, "ifelse": _ifelse, "for": _for, "repeat": _repeat,
    "add": _binary(lambda a, b: a + b),
    "sub": _binary(lambda a, b: a - b),
    "mul": _binary(lambda a, b: a * b),
    "div": _binary(lambda a, b: a / b if b else 0.0),
    "idiv": _binary(lambda a, b: float(int(a) // int(b)) if b else 0.0),
    "mod": _binary(lambda a, b: float(int(a) % int(b)) if b else 0.0),
    "atan": _binary(lambda a, b: math.degrees(math.atan2(a, b)) % 360),
    "exp": _binary(lambda a, b: a ** b),
    "neg": _unary(lambda a: -a),
    "abs": _unary(abs),
    "sqrt": _unary(lambda a: math.sqrt(abs(a))),
    "sin": _unary(lambda a: math.sin(math.radians(a))),
    "cos": _unary(lambda a: math.cos(math.radians(a))),
    "ln": _unary(lambda a: math.log(a) if a > 0 else 0.0),
    "log": _unary(lambda a: math.log10(a) if a > 0 else 0.0),
    "truncate": _unary(lambda a: float(int(a))),
    "round": _unary(lambda a: float(round(a))),
    "floor": _unary(math.floor), "ceiling": _unary(math.ceil),
    "eq": _equality(True), "ne": _equality(False),
    "lt": _order(lambda a, b: a < b), "le": _order(lambda a, b: a <= b),
    "gt": _order(lambda a, b: a > b), "ge": _order(lambda a, b: a >= b),
    "true": lambda m: m.stack.append(True),
    "false": lambda m: m.stack.append(False),
    "null": lambda m: m.stack.append(None),
    "begin": _begin, "end": _end, "dict": _dict,
    "put": _put, "get": _get, "known": _known, "where": _where,
    "length": _length, "array": _array, "aload": _aload, "astore": _astore,
    "cleartomark": _cleartomark, "counttomark": _counttomark, "copy": _copy,
    "and": _logical(lambda a, b: a and b if isinstance(a, bool) else a & b),
    "or": _logical(lambda a, b: a or b if isinstance(a, bool) else a | b),
    "xor": _logical(lambda a, b: a != b if isinstance(a, bool) else a ^ b),
    "not": _not,
    "cvi": _unary(lambda a: float(int(a))), "cvr": _unary(float),
    "string": lambda m: (m.pop(1), m.stack.append(""))[1],
    "type": lambda m: (m.pop(1), m.stack.append(Literal("nametype")))[1],
    "load": _load, "exec": _exec, "cvx": _nothing, "bind": _nothing,
    "readonly": _nothing, "executeonly": _nothing, "noaccess": _nothing,
    "currentdict": lambda m: m.stack.append(m.dicts[-1]),
    "userdict": lambda m: m.stack.append(m.dicts[0]),
    "systemdict": lambda m: m.stack.append({}),
    "currentpoint": _currentpoint,
    "matrix": _matrix, "defaultmatrix": _currentmatrix,
    "currentmatrix": _currentmatrix, "concatmatrix": _concatmatrix,
    "transform": _transform, "itransform": _itransform,
    "dtransform": _dtransform, "idtransform": _idtransform,
    "forall": _forall, "stopped": _stopped, "store": _def,
    "currentgray": lambda m: m.stack.append(0.0),
    "currentflat": lambda m: m.stack.append(1.0),
    "currentpacking": lambda m: m.stack.append(False),
    "setpacking": _drop(1), "setcolorspace": _drop(1),
    "showpage": _nothing, "setlinecap": _drop(1), "setlinejoin": _drop(1),
    "setmiterlimit": _drop(1), "setdash": _drop(2), "setflat": _drop(1),
    "setoverprint": _drop(1), "setstrokeadjust": _drop(1), "count": lambda m: m.stack.append(
        float(len(m.stack))
    ),
}
