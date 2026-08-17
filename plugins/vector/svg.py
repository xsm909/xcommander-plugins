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

    rules = _stylesheet(root)
    box = _view_box(root)
    notes: list[str] = []
    shapes: list[dict] = []
    gradients = _gradients(root)
    # A viewBox may start anywhere; the host is handed a picture whose corner
    # is the origin, so the offset is taken out here and nowhere else.
    start = _initial()
    start["transform"] = (1.0, 0.0, 0.0, 1.0, -box[0], -box[1])
    _walk(root, start, shapes, rules, gradients, notes, box)

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


def _walk(node, inherited, shapes, rules, gradients, notes, box):
    for child in node:
        tag = _tag(child)
        if tag in ("defs", "symbol", "clipPath", "mask", "marker", "style",
                   "title", "desc", "metadata", "linearGradient",
                   "radialGradient", "pattern", "filter"):
            if tag in ("clipPath", "mask") and "clipping" not in notes:
                notes.append("Clipping and masking are ignored.")
            continue
        if tag == "text" or tag == "tspan":
            if "text" not in notes:
                notes.append("Text is not drawn.")
            continue

        style = dict(inherited)
        for key, value in _style_of(child, rules).items():
            style[key] = value
        style["transform"] = _compose(
            inherited["transform"], _transform(child.get("transform", ""))
        )
        # Opacity is not inherited as a property — it multiplies down the tree.
        style["opacity"] = str(
            _number(inherited.get("opacity", "1"), 1)
            * _number(child.get("opacity", "1"), 1)
        )

        if tag in ("g", "svg", "a"):
            _walk(child, style, shapes, rules, gradients, notes, box)
            continue
        if tag == "use":
            if "use" not in notes:
                notes.append("Reused shapes are not followed.")
            continue

        path = _path_of(child, tag)
        if path is None:
            continue
        shape = _paint(path, style, gradients, notes)
        if shape is not None:
            shapes.append(shape)


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
    """Every gradient reduced to one colour — the mean of its stops.

    An honest lie: a gradient drawn as a flat colour is recognisably the shape
    it should be, where a gradient dropped altogether is a hole. The picture
    says that this happened.
    """
    out = {}
    for node in root.iter():
        if _tag(node) not in ("linearGradient", "radialGradient"):
            continue
        stops = []
        for stop in node.iter():
            if _tag(stop) != "stop":
                continue
            style = _declarations(stop.get("style", ""))
            colour = style.get("stop-color") or stop.get("stop-color") or "#000000"
            alpha = _number(
                style.get("stop-opacity") or stop.get("stop-opacity") or "1", 1
            )
            rgba = _colour(colour, 1)
            if rgba:
                stops.append((rgba, alpha))
        if not stops:
            continue
        count = len(stops)
        red = sum(int(s[0][1:3], 16) for s, _ in stops) // count
        green = sum(int(s[0][3:5], 16) for s, _ in stops) // count
        blue = sum(int(s[0][5:7], 16) for s, _ in stops) // count
        opacity = sum(a for _, a in stops) / count
        if node.get("id"):
            out[node.get("id")] = "#%02x%02x%02x%02x" % (
                red, green, blue, max(0, min(255, round(opacity * 255)))
            )
    return out


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


def _colour(value, opacity, gradients=None, notes=None):
    """One paint, as `#rrggbbaa`, or None where there is nothing to paint."""
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("none", "transparent", ""):
        return None
    if value.startswith("url("):
        name = value[4:-1].strip("'\"#) ")
        found = (gradients or {}).get(name)
        if found is None:
            return None
        if notes is not None and "gradient" not in notes:
            notes.append("Gradients are drawn in one flat colour.")
        return _with_opacity(found, opacity)
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


def _paint(path, style, gradients, notes):
    verbs, points = path
    if style.get("display") == "none" or style.get("visibility") == "hidden":
        return None
    matrix = style["transform"]
    points = _apply(matrix, points)

    whole = _number(style.get("opacity", "1"), 1)
    fill = _colour(
        style.get("fill", "#000000"),
        _number(style.get("fill-opacity", "1"), 1) * whole,
        gradients, notes,
    )
    stroke = _colour(
        style.get("stroke", "none"),
        _number(style.get("stroke-opacity", "1"), 1) * whole,
        gradients, notes,
    )
    if fill is None and stroke is None:
        return None

    # A stroke is drawn in the shape's own space, so it is scaled by whatever
    # the transform does to area. Exact for the uniform scales that make up
    # every transform anybody writes; an approximation for the rest.
    scale = math.sqrt(abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])) or 1.0
    width = _number(style.get("stroke-width", "1"), 1) * scale

    return {
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
