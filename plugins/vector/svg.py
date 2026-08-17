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

"""SVG, flattened into the shapes the host knows how to draw.

**The division of labour, and it is the same one the 3D viewer settled.** The
plugin does the arcana and the host draws: every rectangle, circle and polygon
becomes a path; every arc becomes curves; every transform on the way down the
tree is composed and applied to the coordinates; every style — inherited, from
an attribute, from a `style=`, from a CSS rule in a `<style>` block — is worked
out here. What crosses the pipe is a flat list of paths with their colours
already decided, so the host has no notion of what SVG is.

**Why not simply rasterise it here.** Because then it would be a picture, and a
picture magnified is mush. A vector file is worth having precisely because it
stays sharp at any magnification, and that is only true if the shapes reach the
canvas as shapes.

**What is not read, and is said out loud rather than drawn wrong:** gradients
(drawn in their average colour), text, filters, clipping and masking.
"""

from __future__ import annotations

import base64
import math
import re
import struct
import xml.etree.ElementTree as ElementTree

#: Verbs, as the host reads them.
MOVE, LINE, CUBIC, CLOSE = 0, 1, 2, 3

#: Properties a child takes from its parent unless it says otherwise.
INHERITED = (
    "fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity",
    "fill-rule", "stroke-linecap", "stroke-linejoin", "color",
    "font-size", "font-family", "font-weight", "font-style", "text-anchor",
)

#: The colours SVG names. The whole list is 147 long and nobody uses most of
#: it; these are the ones that turn up, plus every one used by the icon sets.
NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000",
    "green": "#008000", "lime": "#00ff00", "blue": "#0000ff",
    "yellow": "#ffff00", "cyan": "#00ffff", "aqua": "#00ffff",
    "magenta": "#ff00ff", "fuchsia": "#ff00ff", "silver": "#c0c0c0",
    "gray": "#808080", "grey": "#808080", "maroon": "#800000",
    "olive": "#808000", "purple": "#800080", "teal": "#008080",
    "navy": "#000080", "orange": "#ffa500", "pink": "#ffc0cb",
    "brown": "#a52a2a", "gold": "#ffd700", "indigo": "#4b0082",
    "violet": "#ee82ee", "transparent": None, "none": None,
}

MAX_SHAPES = 20000


class SvgError(Exception):
    """The file is not one, or is one this cannot read."""


def is_svg(head: bytes) -> bool:
    text = head[:4096].lstrip()
    return text.startswith(b"<svg") or (b"<svg" in text and text.startswith(b"<?xml"))


def read(data: bytes) -> dict:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as failure:
        raise SvgError("This is not XML: %s" % failure) from failure
    if _tag(root) != "svg":
        raise SvgError("This XML is not an SVG.")

    box = _view_box(root)
    world = _World(
        rules=_stylesheet(root),
        gradients=_gradients(root),
        # Everything with an id, so `<use>` can find what it points at
        # wherever it was declared — which is usually inside `<defs>`, where
        # nothing is drawn from.
        named={node.get("id"): node for node in root.iter() if node.get("id")},
        notes=[],
        shapes=[],
    )
    # A viewBox may start anywhere; the host is handed a picture whose corner
    # is the origin, so the offset is taken out here and nowhere else.
    start = _initial()
    start["transform"] = (1.0, 0.0, 0.0, 1.0, -box[0], -box[1])
    _walk(root, start, world, 0)
    notes = world.notes
    shapes = world.shapes

    if len(shapes) > MAX_SHAPES:
        notes.append(
            "%d shapes of %d shown." % (MAX_SHAPES, len(shapes))
        )
        shapes = shapes[:MAX_SHAPES]
    if not shapes:
        notes.append("There is nothing in this file that can be drawn.")
    return {
        "width": box[2],
        "height": box[3],
        "shapes": shapes,
        "notes": notes,
    }


# ------------------------------------------------------------------ the tree


def _tag(node) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _initial() -> dict:
    return {
        "fill": "#000000", "stroke": "none", "stroke-width": "1",
        "fill-opacity": "1", "stroke-opacity": "1", "opacity": "1",
        "fill-rule": "nonzero", "stroke-linecap": "butt",
        "stroke-linejoin": "miter", "transform": (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    }


class _World:
    """Everything the walk needs that is not the element it is standing on."""

    __slots__ = ("rules", "gradients", "named", "notes", "shapes")

    def __init__(self, rules, gradients, named, notes, shapes):
        self.rules = rules
        self.gradients = gradients
        self.named = named
        self.notes = notes
        self.shapes = shapes

    def say(self, note):
        if note not in self.notes:
            self.notes.append(note)


#: How far a `<use>` may point at something that points at something. Files
#: that refer to themselves exist, and without this they are a stack overflow
#: rather than a drawing.
MAX_DEPTH = 8

#: Declared and drawn are different things: these are read for what they say
#: and never drawn where they stand.
DECLARATIONS = (
    "defs", "symbol", "clipPath", "mask", "marker", "style", "title", "desc",
    "metadata", "linearGradient", "radialGradient", "pattern", "filter",
)


def _walk(node, inherited, world, depth):
    for child in node:
        _draw(child, inherited, world, depth)


def _draw(child, inherited, world, depth):
    """One element: a group to walk into, a shape to keep, or nothing."""
    tag = _tag(child)
    if tag in DECLARATIONS:
        if tag in ("clipPath", "mask"):
            world.say("Clipping and masking are ignored.")
        return
    style = dict(inherited)
    for key, value in _style_of(child, world.rules).items():
        style[key] = value
    style["transform"] = _compose(
        inherited["transform"], _transform(child.get("transform", ""))
    )
    # Opacity is not inherited as a property — it multiplies down the tree.
    style["opacity"] = str(
        _number(inherited.get("opacity", "1"), 1)
        * _number(child.get("opacity", "1"), 1)
    )

    if tag in ("text", "tspan"):
        _text(child, style, world, depth)
        return
    if tag in ("g", "svg", "a"):
        _walk(child, style, world, depth)
        return
    if tag == "use":
        _use(child, style, world, depth)
        return

    path = _path_of(child, tag)
    if path is None:
        return
    shape = _paint(path, style, world.gradients, world.notes)
    if shape is not None:
        world.shapes.append(shape)


#: What a font family in a file becomes here. A drawing may name any font on
#: the machine that made it, and this one has the application's own two — so
#: the choice is between the ordinary face and a typewriter face, and the
#: drawing says that a substitution happened.
def _family(names):
    for name in (names or "").lower().replace('"', "").split(","):
        name = name.strip()
        if name in ("monospace", "courier", "courier new", "menlo", "consolas") \
                or "mono" in name:
            return "mono"
        if name in ("serif", "times", "times new roman", "georgia"):
            return "serif"
    return "sans"


def _text(node, style, world, depth):
    """`<text>` and the runs inside it.

    **Drawn as text, not as outlines.** Turning letters into paths would need
    the font the file names, and a drawing is not allowed to bring one; so what
    crosses is the words, where they sit and how big they are, and the host
    sets them in a face it has. That is a substitution and the drawing says so
    — a diagram whose labels are in the wrong face is still a diagram, where a
    diagram with no labels is a puzzle.
    """
    world.say("Text is set in this application's own faces, not the file's.")
    x = _number(node.get("x", 0))
    y = _number(node.get("y", 0))
    x += _number(node.get("dx", 0))
    y += _number(node.get("dy", 0))

    body = (node.text or "").strip()
    if body:
        world.shapes.append(_word(body, x, y, style, world))
    # A `<tspan>` inside places itself, and what follows it belongs to the
    # parent again — which is why this walks children rather than joining the
    # text of the whole element.
    for child in node:
        if _tag(child) == "tspan":
            inherited = dict(style)
            for key, value in _style_of(child, world.rules).items():
                inherited[key] = value
            inherited.setdefault("x", node.get("x", "0"))
            if child.get("x") is None:
                child.set("x", str(x))
            if child.get("y") is None:
                child.set("y", str(y))
            _text(child, inherited, world, depth + 1)
        tail = (child.tail or "").strip()
        if tail:
            world.shapes.append(_word(tail, x, y, style, world))


def _word(body, x, y, style, world):
    """One run of text, with its paint and its place."""
    whole = _number(style.get("opacity", "1"), 1)
    fill = _colour(
        style.get("fill", "#000000"),
        _number(style.get("fill-opacity", "1"), 1) * whole,
    )
    if fill is None and _named_paint(style.get("fill")) is not None:
        # A gradient on text is not something the contract can carry; the
        # average is better than nothing at all.
        name = _named_paint(style.get("fill"))
        found = (world.gradients or {}).get(name)
        fill = _average(found["stops"]) if found else "#000000ff"
    return {
        "text": body,
        "at": [x, y],
        "size": _number(style.get("font-size", "16"), 16),
        "family": _family(style.get("font-family")),
        "weight": int(_number(style.get("font-weight", "400"), 400)) or 400,
        "italic": style.get("font-style", "") in ("italic", "oblique"),
        "anchor": style.get("text-anchor", "start"),
        "matrix": list(style["transform"]),
        "fill": fill or "#000000ff",
    }


def _use(node, style, world, depth):
    """`<use>` — the same shape drawn again somewhere else.

    Icon sets lean on it heavily: one path in `<defs>` and a dozen `<use>`
    pointing at it. A reader that skips them draws an empty file and looks like
    it worked.

    The instance takes the style it is used *in*, not the style at the
    declaration — which is what makes one shape in `<defs>` come out in a
    different colour at every place it is used.
    """
    if depth >= MAX_DEPTH:
        world.say("A reused shape points at itself; part of the file is not drawn.")
        return
    href = node.get("href") or node.get("{http://www.w3.org/1999/xlink}href") or ""
    target = world.named.get(href.lstrip("#"))
    if target is None:
        world.say("A reused shape points at something that is not in the file.")
        return

    # `x` and `y` on the `<use>` are a move, and they come after its own
    # transform.
    x, y = _number(node.get("x", 0)), _number(node.get("y", 0))
    moved = dict(style)
    if x or y:
        moved["transform"] = _compose(
            style["transform"], (1.0, 0.0, 0.0, 1.0, x, y)
        )

    # A `<symbol>` or an `<svg>` is a box of children; anything else is one
    # element, and it is drawn even though its own tag would be skipped where
    # it stands — that is the whole point of `<defs>`.
    if _tag(target) in ("symbol", "svg", "defs", "g"):
        _walk(target, moved, world, depth + 1)
    else:
        _draw(target, moved, world, depth + 1)


def _style_of(node, rules) -> dict:
    """One element's own declarations, in the order they take effect.

    A CSS rule beats a presentation attribute — which is what Illustrator's own
    exports rely on, and what makes a reader that only looks at `fill=` show a
    black silhouette.
    """
    out = {}
    for key in INHERITED + ("opacity", "display", "visibility"):
        value = node.get(key)
        if value is not None:
            out[key] = value.strip()
    for selector in _selectors(node):
        out.update(rules.get(selector, {}))
    out.update(_declarations(node.get("style", "")))
    return out


def _selectors(node):
    """The selectors that could name this element, weakest first."""
    out = [_tag(node)]
    for name in (node.get("class") or "").split():
        out.append("." + name)
    if node.get("id"):
        out.append("#" + node.get("id"))
    return out


def _stylesheet(root) -> dict:
    """Every rule in every `<style>`, by selector.

    Deliberately small: a selector is a tag, a class or an id, and anything
    with a space, a comma at the wrong place or a pseudo-class in it is skipped
    rather than half-understood.
    """
    rules: dict[str, dict] = {}
    for node in root.iter():
        if _tag(node) != "style":
            continue
        body = re.sub(r"/\*.*?\*/", "", node.text or "", flags=re.S)
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
            parsed = _declarations(declarations)
            for selector in selectors.split(","):
                selector = selector.strip()
                if selector and not re.search(r"[\s>+~:\[]", selector):
                    rules.setdefault(selector, {}).update(parsed)
    return rules


def _declarations(body: str) -> dict:
    out = {}
    for piece in body.split(";"):
        if ":" not in piece:
            continue
        key, value = piece.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def _gradients(root) -> dict:
    """Every gradient, read whole: its stops, its geometry and its units.

    **Stops are often somewhere else.** A gradient may carry none of its own and
    point at another with `href` — which is how every editor writes a family of
    gradients that differ only in where they run — so the reference is followed
    for whatever this one does not say.
    """
    found = {}
    for node in root.iter():
        tag = _tag(node)
        if tag not in ("linearGradient", "radialGradient"):
            continue
        name = node.get("id")
        if not name:
            continue
        found[name] = node

    out = {}
    for name, node in found.items():
        stops = _stops(node, found)
        if not stops:
            continue
        out[name] = {
            "kind": "radial" if _tag(node) == "radialGradient" else "linear",
            "stops": stops,
            "units": _inherited_attribute(node, found, "gradientUnits")
                     or "objectBoundingBox",
            "spread": _inherited_attribute(node, found, "spreadMethod") or "pad",
            "transform": _transform(
                _inherited_attribute(node, found, "gradientTransform") or ""
            ),
            "numbers": {
                key: _inherited_attribute(node, found, key)
                for key in ("x1", "y1", "x2", "y2", "cx", "cy", "r", "fx", "fy")
            },
        }
    return out


def _reference(node, found):
    """What this gradient inherits from, if it points at one."""
    href = (
        node.get("href")
        or node.get("{http://www.w3.org/1999/xlink}href")
        or ""
    ).lstrip("#")
    return found.get(href)


def _inherited_attribute(node, found, key, depth=0):
    value = node.get(key)
    if value is not None or depth >= MAX_DEPTH:
        return value
    other = _reference(node, found)
    return None if other is None else _inherited_attribute(other, found, key, depth + 1)


def _stops(node, found, depth=0):
    """The colour stops, in order, following a reference where there are none."""
    out = []
    for stop in node:
        if _tag(stop) != "stop":
            continue
        style = _declarations(stop.get("style", ""))
        colour = style.get("stop-color") or stop.get("stop-color") or "#000000"
        alpha = _number(
            style.get("stop-opacity") or stop.get("stop-opacity") or "1", 1
        )
        offset = stop.get("offset", "0")
        at = _number(offset, 0)
        if str(offset).strip().endswith("%"):
            at /= 100.0
        rgba = _colour(colour, alpha)
        if rgba is None:
            rgba = "#00000000"
        out.append({"at": max(0.0, min(1.0, at)), "colour": rgba})
    if out or depth >= MAX_DEPTH:
        return out
    other = _reference(node, found)
    return [] if other is None else _stops(other, found, depth + 1)


def _average(stops):
    """A gradient as one colour, for where it cannot be drawn as a gradient."""
    if not stops:
        return None
    count = len(stops)
    parts = [0, 0, 0, 0]
    for stop in stops:
        for i in range(4):
            parts[i] += int(stop["colour"][1 + i * 2:3 + i * 2], 16)
    return "#%02x%02x%02x%02x" % tuple(value // count for value in parts)


def _view_box(root):
    box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(box) == 4:
        try:
            return [float(value) for value in box]
        except ValueError:
            pass
    width = _length(root.get("width"), 300)
    height = _length(root.get("height"), 150)
    return [0.0, 0.0, width, height]


def _length(value, fallback):
    if not value:
        return fallback
    match = re.match(r"\s*(-?[\d.]+(?:e-?\d+)?)", value)
    return float(match.group(1)) if match else fallback


def _number(value, fallback=0.0):
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return fallback


# ------------------------------------------------------------------- shapes


def _path_of(node, tag):
    """One element as verbs and points, in its own coordinates."""
    if tag == "path":
        return _parse_path(node.get("d") or "")
    if tag == "rect":
        x, y = _number(node.get("x", 0)), _number(node.get("y", 0))
        w, h = _number(node.get("width", 0)), _number(node.get("height", 0))
        if w <= 0 or h <= 0:
            return None
        rx = _number(node.get("rx", node.get("ry", 0)))
        ry = _number(node.get("ry", node.get("rx", 0)))
        if rx > 0 or ry > 0:
            rx = min(rx or ry, w / 2)
            ry = min(ry or rx, h / 2)
            return _rounded(x, y, w, h, rx, ry)
        return ([MOVE, LINE, LINE, LINE, CLOSE],
                [x, y, x + w, y, x + w, y + h, x, y + h])
    if tag == "circle":
        cx, cy = _number(node.get("cx", 0)), _number(node.get("cy", 0))
        r = _number(node.get("r", 0))
        return _ellipse(cx, cy, r, r) if r > 0 else None
    if tag == "ellipse":
        cx, cy = _number(node.get("cx", 0)), _number(node.get("cy", 0))
        rx, ry = _number(node.get("rx", 0)), _number(node.get("ry", 0))
        return _ellipse(cx, cy, rx, ry) if rx > 0 and ry > 0 else None
    if tag == "line":
        return ([MOVE, LINE], [
            _number(node.get("x1", 0)), _number(node.get("y1", 0)),
            _number(node.get("x2", 0)), _number(node.get("y2", 0)),
        ])
    if tag in ("polyline", "polygon"):
        numbers = [float(n) for n in re.findall(r"-?[\d.]+(?:e-?\d+)?", node.get("points") or "")]
        if len(numbers) < 4:
            return None
        verbs = [MOVE] + [LINE] * (len(numbers) // 2 - 1)
        if tag == "polygon":
            verbs.append(CLOSE)
        return (verbs, numbers[:len(verbs) * 2])
    return None


#: How far a Bézier control point sits along the tangent to make a quarter of
#: a circle. Four of them is a circle to within a thousandth of its radius.
KAPPA = 0.5522847498307936


def _ellipse(cx, cy, rx, ry):
    ox, oy = rx * KAPPA, ry * KAPPA
    return (
        [MOVE, CUBIC, CUBIC, CUBIC, CUBIC, CLOSE],
        [
            cx + rx, cy,
            cx + rx, cy + oy, cx + ox, cy + ry, cx, cy + ry,
            cx - ox, cy + ry, cx - rx, cy + oy, cx - rx, cy,
            cx - rx, cy - oy, cx - ox, cy - ry, cx, cy - ry,
            cx + ox, cy - ry, cx + rx, cy - oy, cx + rx, cy,
        ],
    )


def _rounded(x, y, w, h, rx, ry):
    ox, oy = rx * KAPPA, ry * KAPPA
    return (
        [MOVE, LINE, CUBIC, LINE, CUBIC, LINE, CUBIC, LINE, CUBIC, CLOSE],
        [
            x + rx, y,
            x + w - rx, y,
            x + w - rx + ox, y, x + w, y + ry - oy, x + w, y + ry,
            x + w, y + h - ry,
            x + w, y + h - ry + oy, x + w - rx + ox, y + h, x + w - rx, y + h,
            x + rx, y + h,
            x + rx - ox, y + h, x, y + h - ry + oy, x, y + h - ry,
            x, y + ry,
            x, y + ry - oy, x + rx - ox, y, x + rx, y,
        ],
    )


NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
COMMAND = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]")


def _parse_path(d: str):
    """Path data, as verbs and absolute points.

    Every curve comes out cubic: quadratics are raised, arcs are cut into up to
    four cubics. The host then knows four verbs and no geometry at all.
    """
    tokens = []
    at = 0
    while at < len(d):
        char = d[at]
        if COMMAND.match(char):
            tokens.append(char)
            at += 1
            continue
        match = NUMBER.match(d, at)
        if match:
            tokens.append(float(match.group()))
            at = match.end()
            continue
        at += 1

    verbs: list[int] = []
    points: list[float] = []
    x = y = 0.0
    start_x = start_y = 0.0
    last_control = None
    command = None
    index = 0

    def take(count):
        nonlocal index
        values = tokens[index:index + count]
        index += count
        if len(values) < count or any(isinstance(v, str) for v in values):
            return None
        return values

    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, str):
            command = token
            index += 1
            if command in "Zz":
                verbs.append(CLOSE)
                x, y = start_x, start_y
                last_control = None
                continue
        elif command is None:
            index += 1
            continue
        elif command in "Mm":
            # A second pair after a move is a line, which is the rule people
            # forget and which turns a polygon into a scatter of dots.
            command = "L" if command == "M" else "l"

        relative = command.islower()
        letter = command.upper()

        if letter == "M":
            values = take(2)
            if values is None:
                break
            x = values[0] + (x if relative else 0)
            y = values[1] + (y if relative else 0)
            start_x, start_y = x, y
            verbs.append(MOVE)
            points += [x, y]
            last_control = None
        elif letter == "L":
            values = take(2)
            if values is None:
                break
            x = values[0] + (x if relative else 0)
            y = values[1] + (y if relative else 0)
            verbs.append(LINE)
            points += [x, y]
            last_control = None
        elif letter == "H":
            values = take(1)
            if values is None:
                break
            x = values[0] + (x if relative else 0)
            verbs.append(LINE)
            points += [x, y]
            last_control = None
        elif letter == "V":
            values = take(1)
            if values is None:
                break
            y = values[0] + (y if relative else 0)
            verbs.append(LINE)
            points += [x, y]
            last_control = None
        elif letter == "C":
            values = take(6)
            if values is None:
                break
            ox, oy = (x, y) if relative else (0, 0)
            x1, y1 = values[0] + ox, values[1] + oy
            x2, y2 = values[2] + ox, values[3] + oy
            x, y = values[4] + ox, values[5] + oy
            verbs.append(CUBIC)
            points += [x1, y1, x2, y2, x, y]
            last_control = (x2, y2)
        elif letter == "S":
            values = take(4)
            if values is None:
                break
            ox, oy = (x, y) if relative else (0, 0)
            x1, y1 = _mirror(last_control, x, y)
            x2, y2 = values[0] + ox, values[1] + oy
            x, y = values[2] + ox, values[3] + oy
            verbs.append(CUBIC)
            points += [x1, y1, x2, y2, x, y]
            last_control = (x2, y2)
        elif letter in ("Q", "T"):
            if letter == "Q":
                values = take(4)
                if values is None:
                    break
                ox, oy = (x, y) if relative else (0, 0)
                cx, cy = values[0] + ox, values[1] + oy
                nx, ny = values[2] + ox, values[3] + oy
            else:
                values = take(2)
                if values is None:
                    break
                ox, oy = (x, y) if relative else (0, 0)
                cx, cy = _mirror(last_control, x, y)
                nx, ny = values[0] + ox, values[1] + oy
            # A quadratic raised to a cubic, which is exact.
            verbs.append(CUBIC)
            points += [
                x + 2.0 / 3.0 * (cx - x), y + 2.0 / 3.0 * (cy - y),
                nx + 2.0 / 3.0 * (cx - nx), ny + 2.0 / 3.0 * (cy - ny),
                nx, ny,
            ]
            last_control = (cx, cy)
            x, y = nx, ny
        elif letter == "A":
            values = take(7)
            if values is None:
                break
            ox, oy = (x, y) if relative else (0, 0)
            rx, ry, rotation, large, sweep = values[:5]
            nx, ny = values[5] + ox, values[6] + oy
            for curve in _arc(x, y, rx, ry, rotation, large, sweep, nx, ny):
                verbs.append(CUBIC)
                points += curve
            x, y = nx, ny
            last_control = None
        else:
            index += 1
    return (verbs, points) if verbs else None


def _mirror(control, x, y):
    if control is None:
        return x, y
    return 2 * x - control[0], 2 * y - control[1]


def _arc(x1, y1, rx, ry, rotation, large, sweep, x2, y2):
    """An elliptical arc as cubic curves — the endpoint parameterisation.

    Straight out of the specification's own appendix, because this is the one
    piece of SVG geometry nobody derives correctly from memory.
    """
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return []
    rx, ry = abs(rx), abs(ry)
    angle = math.radians(rotation % 360)
    cos, sin = math.cos(angle), math.sin(angle)

    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos * dx2 + sin * dy2
    y1p = -sin * dx2 + cos * dy2

    # An ellipse too small to reach is grown until it just does.
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        root = math.sqrt(lam)
        rx, ry = rx * root, ry * root

    sign = -1 if bool(large) == bool(sweep) else 1
    numerator = max(
        0.0,
        rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p,
    )
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coefficient = sign * math.sqrt(numerator / denominator) if denominator else 0.0
    cxp = coefficient * rx * y1p / ry
    cyp = -coefficient * ry * x1p / rx
    cx = cos * cxp - sin * cyp + (x1 + x2) / 2.0
    cy = sin * cxp + cos * cyp + (y1 + y2) / 2.0

    def angle_of(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        length = math.hypot(ux, uy) * math.hypot(vx, vy)
        if length == 0:
            return 0.0
        value = max(-1.0, min(1.0, dot / length))
        found = math.acos(value)
        return -found if ux * vy - uy * vx < 0 else found

    start = angle_of(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    sweep_angle = angle_of(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry,
    )
    if not sweep and sweep_angle > 0:
        sweep_angle -= 2 * math.pi
    elif sweep and sweep_angle < 0:
        sweep_angle += 2 * math.pi

    pieces = max(1, int(math.ceil(abs(sweep_angle) / (math.pi / 2))))
    step = sweep_angle / pieces
    alpha = 4.0 / 3.0 * math.tan(step / 4.0)
    out = []
    theta = start
    px = cx + rx * cos * math.cos(theta) - ry * sin * math.sin(theta)
    py = cy + rx * sin * math.cos(theta) + ry * cos * math.sin(theta)
    for _ in range(pieces):
        next_theta = theta + step
        dpx = -rx * cos * math.sin(theta) - ry * sin * math.cos(theta)
        dpy = -rx * sin * math.sin(theta) + ry * cos * math.cos(theta)
        nx = cx + rx * cos * math.cos(next_theta) - ry * sin * math.sin(next_theta)
        ny = cy + rx * sin * math.cos(next_theta) + ry * cos * math.sin(next_theta)
        dnx = -rx * cos * math.sin(next_theta) - ry * sin * math.cos(next_theta)
        dny = -rx * sin * math.sin(next_theta) + ry * cos * math.cos(next_theta)
        out.append([
            px + alpha * dpx, py + alpha * dpy,
            nx - alpha * dnx, ny - alpha * dny,
            nx, ny,
        ])
        theta, px, py = next_theta, nx, ny
    return out


# -------------------------------------------------------------------- paint


def _transform(body: str):
    """Every transform on one element, composed left to right."""
    out = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, arguments in re.findall(r"(\w+)\s*\(([^)]*)\)", body or ""):
        values = [float(n) for n in NUMBER.findall(arguments)]
        if name == "matrix" and len(values) == 6:
            piece = tuple(values)
        elif name == "translate" and values:
            piece = (1.0, 0.0, 0.0, 1.0, values[0], values[1] if len(values) > 1 else 0.0)
        elif name == "scale" and values:
            sx = values[0]
            sy = values[1] if len(values) > 1 else sx
            piece = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate" and values:
            angle = math.radians(values[0])
            cos, sin = math.cos(angle), math.sin(angle)
            piece = (cos, sin, -sin, cos, 0.0, 0.0)
            if len(values) >= 3:
                cx, cy = values[1], values[2]
                piece = _compose(
                    _compose((1.0, 0.0, 0.0, 1.0, cx, cy), piece),
                    (1.0, 0.0, 0.0, 1.0, -cx, -cy),
                )
        elif name == "skewX" and values:
            piece = (1.0, 0.0, math.tan(math.radians(values[0])), 1.0, 0.0, 0.0)
        elif name == "skewY" and values:
            piece = (1.0, math.tan(math.radians(values[0])), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        out = _compose(out, piece)
    return out


def _compose(a, b):
    """`a` after `b` — the ordinary two-by-three product."""
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4],
        a[1] * b[4] + a[3] * b[5] + a[5],
    )


def _apply(matrix, points):
    a, b, c, d, e, f = matrix
    out = []
    for i in range(0, len(points), 2):
        x, y = points[i], points[i + 1]
        out.append(a * x + c * y + e)
        out.append(b * x + d * y + f)
    return out


def _named_paint(value):
    """The id a paint points at, or None when it is an ordinary colour."""
    if value and value.strip().lower().startswith("url("):
        return value.strip()[4:-1].strip("'\"#) ")
    return None


def _colour(value, opacity):
    """One paint, as `#rrggbbaa`, or None where there is nothing to paint."""
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("none", "transparent", ""):
        return None
    if value.startswith("url("):
        return None
    if value in NAMED:
        found = NAMED[value]
        return None if found is None else _with_opacity(found + "ff", opacity)
    match = re.match(r"rgba?\(([^)]*)\)", value)
    if match:
        parts = [p.strip() for p in match.group(1).replace("/", ",").split(",")]
        numbers = []
        for i, part in enumerate(parts[:4]):
            number = _number(part.rstrip("%"), 0)
            if part.endswith("%"):
                number = number * (255 if i < 3 else 1) / 100.0
            numbers.append(number)
        while len(numbers) < 3:
            numbers.append(0)
        alpha = numbers[3] if len(numbers) > 3 else 1.0
        return "#%02x%02x%02x%02x" % (
            _byte(numbers[0]), _byte(numbers[1]), _byte(numbers[2]),
            _byte(alpha * opacity * 255),
        )
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        if len(digits) == 4:
            digits = "".join(c * 2 for c in digits)
        if len(digits) == 6:
            digits += "ff"
        if len(digits) != 8:
            return None
        return _with_opacity("#" + digits, opacity)
    return None


def _with_opacity(rgba, opacity):
    alpha = int(rgba[7:9], 16) if len(rgba) >= 9 else 255
    return rgba[:7] + "%02x" % _byte(alpha * opacity)


def _byte(value):
    return max(0, min(255, int(round(value))))


def _bounds(points):
    """The box a shape occupies, in its own coordinates."""
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs = points[0::2]
    ys = points[1::2]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _is_similarity(matrix):
    """Whether a transform keeps circles circular.

    A gradient that runs along a line survives any transform — a line maps to a
    line. A round one does not: squash it unevenly and the circle becomes an
    ellipse, which is a shape the host is not handed a way to describe.
    """
    a, b, c, d = matrix[:4]
    return abs(a - d) < 1e-6 and abs(b + c) < 1e-6


def _gradient_paint(name, gradients, opacity, matrix, bounds, notes):
    """One gradient in the drawing's own coordinates, or a flat colour.

    Everything a format knows is undone here: fractions of the shape's own box
    turned into real coordinates, the gradient's own transform composed with the
    shape's, the stops sorted and their colours resolved. What the host receives
    is two points (or a centre and a radius) and a list of stops.
    """
    found = (gradients or {}).get(name)
    if found is None:
        return None, None
    stops = [
        {"at": stop["at"], "colour": _with_opacity(stop["colour"], opacity)}
        for stop in found["stops"]
    ]
    if not stops:
        return None, None

    x, y, width, height = bounds
    fractional = found["units"] != "userSpaceOnUse"
    if fractional:
        if width <= 0 or height <= 0:
            return _with_opacity(_average(stops), 1), None
        place = (width, 0.0, 0.0, height, x, y)
    else:
        place = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    whole = _compose(_compose(matrix, place), found["transform"])

    def number(key, fallback):
        value = found["numbers"].get(key)
        if value is None:
            return fallback
        got = _number(value, fallback)
        if str(value).strip().endswith("%"):
            got /= 100.0
            if not fractional:
                got *= width if key in ("x1", "x2", "cx", "fx", "r") else height
        return got

    if found["kind"] == "linear":
        x1, y1 = number("x1", 0.0), number("y1", 0.0)
        x2, y2 = number("x2", 1.0), number("y2", 0.0)
        moved = _apply(whole, [x1, y1, x2, y2])
        return None, {
            "kind": "linear",
            "from": [moved[0], moved[1]],
            "to": [moved[2], moved[3]],
            "stops": stops,
            "spread": found["spread"],
        }

    # Round, and only where the shape has not been squashed out of shape.
    if not _is_similarity(whole):
        if notes is not None and "round gradient" not in " ".join(notes):
            notes.append(
                "A round gradient on a stretched shape is drawn in one colour."
            )
        return _with_opacity(_average(stops), 1), None
    cx, cy = number("cx", 0.5), number("cy", 0.5)
    radius = number("r", 0.5)
    fx, fy = number("fx", cx), number("fy", cy)
    moved = _apply(whole, [cx, cy, fx, fy])
    scale = math.sqrt(abs(whole[0] * whole[3] - whole[1] * whole[2])) or 1.0
    return None, {
        "kind": "radial",
        "centre": [moved[0], moved[1]],
        "focus": [moved[2], moved[3]],
        "radius": radius * scale,
        "stops": stops,
        "spread": found["spread"],
    }


def _paint(path, style, gradients, notes):
    verbs, points = path
    if style.get("display") == "none" or style.get("visibility") == "hidden":
        return None
    matrix = style["transform"]
    bounds = _bounds(points)
    points = _apply(matrix, points)

    whole = _number(style.get("opacity", "1"), 1)

    def paint(key, fallback):
        value = style.get(key, fallback)
        alpha = _number(style.get("%s-opacity" % key, "1"), 1) * whole
        name = _named_paint(value)
        if name is None:
            return _colour(value, alpha), None
        return _gradient_paint(name, gradients, alpha, matrix, bounds, notes)

    fill, fill_gradient = paint("fill", "#000000")
    stroke, stroke_gradient = paint("stroke", "none")
    if fill is None and stroke is None and fill_gradient is None \
            and stroke_gradient is None:
        return None

    # A stroke is drawn in the shape's own space, so it is scaled by whatever
    # the transform does to area. Exact for the uniform scales that make up
    # every transform anybody writes; an approximation for the rest.
    scale = math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])) or 1.0
    width = _number(style.get("stroke-width", "1"), 1) * scale

    out = {
        "verbs": base64.b64encode(bytes(verbs)).decode("ascii"),
        "points": base64.b64encode(
            struct.pack("<%df" % len(points), *points)
        ).decode("ascii"),
        "fill": fill,
        "stroke": stroke,
        "strokeWidth": width,
        "evenOdd": style.get("fill-rule", "nonzero") == "evenodd",
        "cap": style.get("stroke-linecap", "butt"),
        "join": style.get("stroke-linejoin", "miter"),
    }
    if fill_gradient is not None:
        out["fillGradient"] = fill_gradient
    if stroke_gradient is not None:
        out["strokeGradient"] = stroke_gradient
    return out
