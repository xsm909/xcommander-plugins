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

"""Running one page, to find out what is written on it and where.

A content stream is a little program, and the only honest way to know where a
word sits is to execute it. This is that interpreter — and it is **one**
interpreter, feeding all three of the routes above it: the structure tags want
text filed by `/MCID`, the drawn grid wants text and rectangles in the same
coordinates, and the last-resort reading wants baselines. Running the page three
times to answer three questions would be three chances to disagree.

**Nothing is drawn.** Colours are followed only as far as telling a filled
heading band from the paper, glyphs are never rasterised, and an image is
stepped over.

Two things this does that the prototypes did not, and both of them show:

- **Real advance widths.** Where a word ends is arithmetic on the font's own
  `/Widths`, not a guess of half an em per letter, which is what puts the
  boundary between two columns in the right place.
- **Reading coordinates.** The page's own space has y running up and may be
  turned on its side by `/Rotate`; everything leaves here with y running down
  the page as it is read, so "the next line" is simply a larger y.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pdfdoc
import pdffont

Matrix = Tuple[float, float, float, float, float, float]

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


class Run:
    """A piece of text with the place it was drawn."""

    __slots__ = ("x0", "x1", "y", "size", "text", "font", "mcid", "artifact",
                 "bold", "italic", "gray", "vertical")

    def __init__(self, x0: float, x1: float, y: float, size: float, text: str,
                 font: Optional[pdffont.Font], mcid: Optional[int], artifact: bool,
                 gray: float, vertical: bool):
        self.x0 = x0
        self.x1 = x1
        self.y = y
        self.size = size
        self.text = text
        self.font = font
        self.mcid = mcid
        self.artifact = artifact
        self.bold = bool(font.bold) if font is not None else False
        self.italic = bool(font.italic) if font is not None else False
        self.gray = gray
        self.vertical = vertical

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<run %.1f,%.1f %r>" % (self.x0, self.y, self.text[:24])


class Shape:
    """A rectangle the page drew: a table cell, a rule, or a coloured band."""

    __slots__ = ("x0", "y0", "x1", "y1", "filled", "stroked", "gray")

    def __init__(self, x0: float, y0: float, x1: float, y1: float,
                 filled: bool, stroked: bool, gray: float):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.filled = filled
        self.stroked = stroked
        self.gray = gray

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<shape %.0f,%.0f %.0fx%.0f%s>" % (
            self.x0, self.y0, self.width, self.height, " filled" if self.filled else "")


class PageContent:
    """Everything one page says, in reading coordinates."""

    def __init__(self, number: int, width: float, height: float):
        self.number = number
        self.width = width
        self.height = height
        self.runs: List[Run] = []
        self.shapes: List[Shape] = []
        #: Horizontal and vertical rules, as `(x0, y0, x1, y1)`.
        self.rules: List[Tuple[float, float, float, float]] = []
        #: `/MCID` → the text under it, for the structure-tag route. A line
        #: break inside one is kept as a newline: it is the only evidence that
        #: a hyphen at the end of a piece is a hyphenation and not a word.
        self.by_mcid: Dict[int, List[str]] = {}
        #: `/MCID` → where it starts and ends down the page, and how big it is.
        #: The structure tags say a paragraph is one thing but not where its
        #: lines were, and only the lines say whether a trailing hyphen is a
        #: word or a broken one.
        self.mcid_place: Dict[int, Tuple[float, float, float]] = {}
        self._mcid_y: Dict[int, float] = {}
        #: True when something on the page used a font whose codes mean nothing
        #: without the font program itself.
        self.unreadable_font = False

    def text(self) -> str:
        return "".join(run.text for run in self.runs)


# -- the interpreter -------------------------------------------------------

_MAX_OPERANDS = 32
_MAX_FORM_DEPTH = 8

#: Points of one path worth keeping. Only the box round a path is ever asked
#: for, and a map of a coastline has settled that box long before this.
_MAX_PATH = 2000


class _State:
    __slots__ = ("ctm", "gray", "stroke_gray")

    def __init__(self, ctm: Matrix, gray: float, stroke_gray: float):
        self.ctm = ctm
        self.gray = gray
        self.stroke_gray = stroke_gray

    def copy(self) -> "_State":
        return _State(self.ctm, self.gray, self.stroke_gray)


def read_page(doc: pdfdoc.Document, page: Dict[str, Any], number: int,
              font_cache: Optional[Dict[int, pdffont.Font]] = None,
              max_operators: int = 400000) -> PageContent:
    """Runs one page and hands back what is on it."""
    box = (pdfdoc.rectangle(doc, page.get("CropBox"))
           or pdfdoc.rectangle(doc, page.get("MediaBox"))
           or (0.0, 0.0, 612.0, 792.0))
    x0, y0, x1, y1 = box
    page_width, page_height = x1 - x0, y1 - y0
    rotate = doc.resolve(page.get("Rotate"))
    rotate = int(rotate) % 360 if isinstance(rotate, (int, float)) else 0
    rotate -= rotate % 90

    # From page space (y up, origin at the box's corner) to reading space
    # (y down, origin top left), with /Rotate folded in.
    if rotate == 90:
        base: Matrix = (0.0, 1.0, -1.0, 0.0, y1, -x0)
        width, height = page_height, page_width
    elif rotate == 180:
        base = (-1.0, 0.0, 0.0, 1.0, x1, -y0)
        width, height = page_width, page_height
    elif rotate == 270:
        base = (0.0, -1.0, 1.0, 0.0, -y0, x1)
        width, height = page_height, page_width
    else:
        base = (1.0, 0.0, 0.0, -1.0, -x0, y1)
        width, height = page_width, page_height

    content = PageContent(number, width, height)
    try:
        data = doc.content_of(page)
    except Exception:  # noqa: BLE001 - an unreadable stream is an empty page
        return content
    if not data:
        return content
    _run(doc, data, doc.resolve(page.get("Resources")), base, content,
         font_cache if font_cache is not None else {}, [max_operators], 0)
    content.runs.sort(key=lambda r: (round(r.y, 1), r.x0))
    return content


def _run(doc: pdfdoc.Document, data: bytes, resources: Any, base: Matrix,
         out: PageContent, font_cache: Dict[int, pdffont.Font],
         budget: List[int], depth: int) -> None:
    fonts = pdffont.fonts_of(doc, resources, font_cache)
    lexer = pdfdoc.Lexer(data)
    operands: List[Any] = []

    state = _State(base, 0.0, 0.0)
    stack: List[_State] = []

    text_matrix: Optional[Matrix] = None
    line_matrix: Optional[Matrix] = None
    font: Optional[pdffont.Font] = None
    size = 0.0
    leading = 0.0
    char_space = 0.0
    word_space = 0.0
    horizontal = 1.0
    rise = 0.0
    render_mode = 0

    # The path being built, as points; only its bounding box is of interest.
    path: List[Tuple[float, float]] = []
    pending_rects: List[Tuple[float, float, float, float]] = []
    subpath_start = 0

    # Marked content: every BDC and BMC opens a level, whether or not it names
    # an /MCID. A /Span carrying /ActualText around a single letter closes with
    # its own EMC, and treating that as the cell's truncated whole cells.
    # (mcid, artifact, actual): `actual` is None where the span carries no
    # /ActualText, the replacement text while it is still to be shown, and the
    # empty string once it has been.
    marks: List[Tuple[Optional[int], bool, Optional[str]]] = []

    def current_mcid() -> Optional[int]:
        for mcid, _artifact, _suppressed in reversed(marks):
            if mcid is not None:
                return mcid
        return None

    def in_artifact() -> bool:
        return any(artifact for _mcid, artifact, _s in marks)

    def suppressed() -> bool:
        """Whether the glyphs here are being replaced by an `/ActualText`."""
        return any(actual is not None for _m, _a, actual in marks)

    def emit(text: str, x0: float, x1: float, y: float, run_size: float,
             vertical: bool) -> None:
        # `/ActualText` replaces the glyphs, it does not accompany them: the
        # first thing shown inside such a span becomes that text and the rest
        # of the span shows nothing. Emitting both is how "action" came out as
        # "aaction" in the prototype this replaces.
        if marks and marks[-1][2] is not None:
            mcid, artifact, actual = marks[-1]
            text = actual
            marks[-1] = (mcid, artifact, "")
            if not text:
                return
        elif suppressed():
            return
        if not text:
            return
        mcid = current_mcid()
        if mcid is not None:
            pieces = out.by_mcid.setdefault(mcid, [])
            last = out._mcid_y.get(mcid)
            if last is not None and abs(last - y) > max(1.0, run_size * 0.5):
                pieces.append("\n")
            out._mcid_y[mcid] = y
            place = out.mcid_place.get(mcid)
            out.mcid_place[mcid] = (
                place[0] if place else y, y, max(place[2] if place else 0.0, run_size)
            )
            pieces.append(text)
        if not text.strip():
            return
        out.runs.append(Run(x0, x1, y, run_size, text, font, mcid,
                            in_artifact(), state.gray, vertical))

    def show(raw: bytes, adjustments: Optional[Sequence[Any]] = None) -> None:
        nonlocal text_matrix
        if text_matrix is None or font is None:
            return
        if render_mode == 3 or render_mode == 7:
            # Invisible text — the layer under a scanned page. Advanced over
            # but never shown: it is not what the reader is looking at, and on
            # a searchable scan it would double every word.
            _advance_only(raw)
            return
        pieces = adjustments if adjustments is not None else [raw]
        text: List[str] = []
        start = None
        last_x = None
        matrix = multiply((size * horizontal, 0.0, 0.0, size, 0.0, rise), text_matrix)
        full = multiply(matrix, state.ctm)
        vertical = abs(full[1]) > abs(full[0]) and abs(full[3]) < abs(full[2])
        origin = apply(state.ctm, *apply(text_matrix, 0.0, rise))
        scale = _scale_of(multiply(text_matrix, state.ctm))
        for piece in pieces:
            if isinstance(piece, (int, float)) and not isinstance(piece, bool):
                shift = -float(piece) / 1000.0 * size * horizontal
                _shift(shift)
                # A kern this wide is a word space the file never spells out.
                if -float(piece) > 100 and text and not text[-1].endswith(" "):
                    text.append(" ")
                continue
            if not isinstance(piece, bytes):
                continue
            for code in font.codes(piece):
                char = font.char(code)
                if char == "" and not font.readable:
                    out.unreadable_font = True
                text.append(char)
                width = font.width(code) / 1000.0 * size
                advance = (width + char_space) * horizontal
                if code == 32 and not font.two_byte:
                    advance += word_space * horizontal
                _shift(advance)
        joined = "".join(text)
        end = apply(state.ctm, *apply(text_matrix, 0.0, rise))
        x_start, y_start = origin
        x_end, _ = end
        if x_end < x_start:
            x_start, x_end = x_end, x_start
        emit(joined, x_start, x_end, y_start, abs(size * scale), vertical)

    def _shift(amount: float) -> None:
        nonlocal text_matrix
        if text_matrix is not None:
            text_matrix = multiply((1.0, 0.0, 0.0, 1.0, amount, 0.0), text_matrix)

    def _advance_only(raw: bytes) -> None:
        if font is None:
            return
        total = 0.0
        for code in font.codes(raw):
            total += (font.width(code) / 1000.0 * size + char_space) * horizontal
            if code == 32 and not font.two_byte:
                total += word_space * horizontal
        _shift(total)

    def next_line(dx: float, dy: float) -> None:
        nonlocal text_matrix, line_matrix
        line_matrix = multiply((1.0, 0.0, 0.0, 1.0, dx, dy), line_matrix or IDENTITY)
        text_matrix = line_matrix

    def flush_path(filled: bool, stroked: bool) -> None:
        for rx0, ry0, rx1, ry1 in pending_rects:
            corners = [apply(state.ctm, rx0, ry0), apply(state.ctm, rx1, ry0),
                       apply(state.ctm, rx1, ry1), apply(state.ctm, rx0, ry1)]
            xs = [p[0] for p in corners]
            ys = [p[1] for p in corners]
            _record(min(xs), min(ys), max(xs), max(ys), filled, stroked, state.gray
                    if filled else state.stroke_gray)
        if path and (filled or stroked):
            xs = [p[0] for p in path]
            ys = [p[1] for p in path]
            _record(min(xs), min(ys), max(xs), max(ys), filled, stroked,
                    state.gray if filled else state.stroke_gray)
        pending_rects.clear()
        path.clear()

    def _record(bx0: float, by0: float, bx1: float, by1: float,
                filled: bool, stroked: bool, gray: float) -> None:
        w, h = bx1 - bx0, by1 - by0
        if w < 0.2 and h < 0.2:
            return
        if len(out.shapes) < 20000:
            out.shapes.append(Shape(bx0, by0, bx1, by1, filled, stroked, gray))
        # A rectangle thin in one direction is a rule, and a table is often
        # drawn out of nothing else.
        if len(out.rules) < 20000:
            if h <= 2.5 and w > 4:
                middle = (by0 + by1) / 2
                out.rules.append((bx0, middle, bx1, middle))
            elif w <= 2.5 and h > 4:
                middle = (bx0 + bx1) / 2
                out.rules.append((middle, by0, middle, by1))

    end = len(data)
    while True:
        if budget[0] <= 0:
            return
        lexer.skip_space()
        if lexer.pos >= end:
            return
        before = lexer.pos
        token = lexer.read_here()
        if lexer.pos == before:
            lexer.pos += 1
            continue
        if not isinstance(token, pdfdoc.Keyword):
            if len(operands) < _MAX_OPERANDS:
                operands.append(token)
            continue

        budget[0] -= 1
        op = str(token)

        try:
            if op == "q":
                stack.append(state.copy())
                if len(stack) > 64:
                    stack.pop(0)
            elif op == "Q":
                if stack:
                    state = stack.pop()
            elif op == "cm" and len(operands) >= 6:
                state.ctm = multiply(_matrix(operands[-6:]), state.ctm)
            elif op == "gs":
                pass  # nothing in a graphics state dictionary changes the text
            elif op == "BT":
                text_matrix = line_matrix = IDENTITY
            elif op == "ET":
                text_matrix = line_matrix = None
            elif op == "Tf" and len(operands) >= 2:
                name = operands[-2]
                font = fonts.get(str(name)) if isinstance(name, pdfdoc.Name) else font
                size = _float(operands[-1])
            elif op == "Td" and len(operands) >= 2:
                next_line(_float(operands[-2]), _float(operands[-1]))
            elif op == "TD" and len(operands) >= 2:
                leading = -_float(operands[-1])
                next_line(_float(operands[-2]), _float(operands[-1]))
            elif op == "Tm" and len(operands) >= 6:
                text_matrix = line_matrix = _matrix(operands[-6:])
            elif op == "T*":
                next_line(0.0, -leading)
            elif op == "TL" and operands:
                leading = _float(operands[-1])
            elif op == "Tc" and operands:
                char_space = _float(operands[-1])
            elif op == "Tw" and operands:
                word_space = _float(operands[-1])
            elif op == "Tz" and operands:
                horizontal = _float(operands[-1]) / 100.0
            elif op == "Ts" and operands:
                rise = _float(operands[-1])
            elif op == "Tr" and operands:
                render_mode = int(_float(operands[-1]))
            elif op == "Tj" and operands:
                if isinstance(operands[-1], bytes):
                    show(operands[-1])
            elif op == "'" and operands:
                next_line(0.0, -leading)
                if isinstance(operands[-1], bytes):
                    show(operands[-1])
            elif op == '"' and len(operands) >= 3:
                word_space = _float(operands[-3])
                char_space = _float(operands[-2])
                next_line(0.0, -leading)
                if isinstance(operands[-1], bytes):
                    show(operands[-1])
            elif op == "TJ" and operands:
                if isinstance(operands[-1], list):
                    show(b"", operands[-1])
            elif op in ("BDC", "BMC", "DP", "MP"):
                if op in ("BDC", "BMC"):
                    marks.append(_open_mark(doc, operands, resources, out))
            elif op == "EMC":
                if marks:
                    marks.pop()
            elif op == "re" and len(operands) >= 4:
                rx, ry = _float(operands[-4]), _float(operands[-3])
                rw, rh = _float(operands[-2]), _float(operands[-1])
                pending_rects.append((rx, ry, rx + rw, ry + rh))
            elif op == "m" and len(operands) >= 2:
                subpath_start = len(path)
                if len(path) < _MAX_PATH:
                    path.append(apply(state.ctm, _float(operands[-2]), _float(operands[-1])))
            elif op == "l" and len(operands) >= 2:
                if len(path) < _MAX_PATH:
                    path.append(apply(state.ctm, _float(operands[-2]), _float(operands[-1])))
            elif op in ("c", "v", "y") and len(operands) >= 4:
                if len(path) < _MAX_PATH:
                    for i in range(0, len(operands) - 1, 2):
                        path.append(apply(state.ctm, _float(operands[i]), _float(operands[i + 1])))
            elif op == "h":
                if path and subpath_start < len(path):
                    path.append(path[subpath_start])
            elif op in ("f", "F", "f*"):
                flush_path(True, False)
            elif op in ("B", "B*", "b", "b*"):
                flush_path(True, True)
            elif op in ("S", "s"):
                flush_path(False, True)
            elif op == "n":
                pending_rects.clear()
                path.clear()
            elif op in ("W", "W*"):
                pass  # a clip does not paint; the path is settled by the next op
            elif op == "g" and operands:
                state.gray = _clamp(_float(operands[-1]))
            elif op == "G" and operands:
                state.stroke_gray = _clamp(_float(operands[-1]))
            elif op == "rg" and len(operands) >= 3:
                state.gray = _luma(operands[-3:])
            elif op == "RG" and len(operands) >= 3:
                state.stroke_gray = _luma(operands[-3:])
            elif op == "k" and len(operands) >= 4:
                state.gray = _cmyk(operands[-4:])
            elif op == "K" and len(operands) >= 4:
                state.stroke_gray = _cmyk(operands[-4:])
            elif op in ("sc", "scn") and operands:
                state.gray = _components(operands)
            elif op in ("SC", "SCN") and operands:
                state.stroke_gray = _components(operands)
            elif op == "Do" and operands:
                _do_xobject(doc, operands[-1], resources, state.ctm, out,
                            font_cache, budget, depth)
            elif op == "BI":
                lexer.pos = _skip_inline_image(data, lexer.pos)
        except Exception:  # noqa: BLE001 - one bad operator is not a bad page
            pass
        operands = []


def _open_mark(doc: pdfdoc.Document, operands: List[Any], resources: Any,
               out: PageContent) -> Tuple[Optional[int], bool, Optional[str]]:
    """What a `BDC` opens: an id, whether it is furniture, what it really says.

    **Every `BDC` and `BMC` opens a level, not only the ones carrying an
    `/MCID`.** A `/Span << /ActualText >>` around a single letter closes with
    its own `EMC`, and treating that as the enclosing cell's silently truncated
    cells — `PlayStation, Xbox, Steam` came out as `Pla`.
    """
    tag = operands[0] if operands else None
    properties = operands[-1] if len(operands) >= 2 else None
    if isinstance(properties, pdfdoc.Name):
        table = doc.resolve(doc.dict_of(resources).get("Properties"))
        properties = doc.dict_of(table.get(str(properties))) if isinstance(table, dict) else {}
    if not isinstance(properties, dict):
        properties = {}
    mcid = doc.resolve(properties.get("MCID"))
    mcid = int(mcid) if isinstance(mcid, (int, float)) and not isinstance(mcid, bool) else None
    artifact = isinstance(tag, pdfdoc.Name) and str(tag) == "Artifact"

    # /ActualText replaces whatever the glyphs would decode to, which is how a
    # subset font can spell "a" as "!" and still read correctly.
    actual = doc.resolve(properties.get("ActualText"))
    text = pdfdoc.text_of(actual) if isinstance(actual, bytes) else None
    if mcid is not None:
        out.by_mcid.setdefault(mcid, [])
    return (mcid, artifact, text)


def _do_xobject(doc: pdfdoc.Document, name: Any, resources: Any, ctm: Matrix,
                out: PageContent, font_cache: Dict[int, pdffont.Font],
                budget: List[int], depth: int) -> None:
    if depth >= _MAX_FORM_DEPTH or not isinstance(name, pdfdoc.Name):
        return
    table = doc.resolve(doc.dict_of(resources).get("XObject"))
    if not isinstance(table, dict):
        return
    stream = doc.resolve(table.get(str(name)))
    if not isinstance(stream, pdfdoc.Stream):
        return
    if str(doc.resolve(stream.dict.get("Subtype")) or "") != "Form":
        return  # an image: this viewer has nothing to do with it
    matrix = pdfdoc.numbers(doc, stream.dict.get("Matrix"))
    inner = multiply(_matrix(matrix), ctm) if len(matrix) == 6 else ctm
    child = doc.resolve(stream.dict.get("Resources")) or resources
    try:
        data = stream.data
    except Exception:  # noqa: BLE001
        return
    _run(doc, data, child, inner, out, font_cache, budget, depth + 1)


def _skip_inline_image(data: bytes, pos: int) -> int:
    """Steps over `BI … ID <binary> EI`, which is not PDF syntax in the middle."""
    marker = data.find(b"ID", pos)
    if marker == -1:
        return len(data)
    start = marker + 2
    if start < len(data) and data[start] in pdfdoc.WHITESPACE:
        start += 1
    i = start
    while True:
        end = data.find(b"EI", i)
        if end == -1:
            return len(data)
        before_ok = end == 0 or data[end - 1] in pdfdoc.WHITESPACE
        after = data[end + 2:end + 3]
        after_ok = not after or after in b"\x00\t\n\x0c\r /[<("
        if before_ok and after_ok:
            return end + 2
        i = end + 2


# -- little helpers --------------------------------------------------------


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _matrix(values: Sequence[Any]) -> Matrix:
    numbers = [_float(v) for v in values[:6]]
    while len(numbers) < 6:
        numbers.append(0.0)
    return (numbers[0], numbers[1], numbers[2], numbers[3], numbers[4], numbers[5])


def _scale_of(m: Matrix) -> float:
    """How much this matrix magnifies — what a text size means on the page."""
    a, b, c, d = m[0], m[1], m[2], m[3]
    return ((abs(a * d - b * c)) ** 0.5) or 1.0


def _clamp(value: float) -> float:
    return 0.0 if value < 0 else (1.0 if value > 1 else value)


def _luma(values: Sequence[Any]) -> float:
    r, g, b = (_clamp(_float(v)) for v in values[:3])
    return 0.299 * r + 0.587 * g + 0.114 * b


def _cmyk(values: Sequence[Any]) -> float:
    c, m, y, k = (_clamp(_float(v)) for v in values[:4])
    return _clamp((1 - c) * (1 - k) * 0.299 + (1 - m) * (1 - k) * 0.587
                  + (1 - y) * (1 - k) * 0.114)


def _components(operands: List[Any]) -> float:
    """A colour in whatever space is current, read as lightness.

    Only the number of components is known here, and that is enough: one is
    grey, three are RGB, four are CMYK, and a pattern name means a fill this
    viewer treats as dark. Colour is used for one thing only — telling a
    coloured band from the paper — so an approximation is the right size of
    answer.
    """
    numbers = [v for v in operands if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if len(numbers) >= 4:
        return _cmyk(numbers[-4:])
    if len(numbers) == 3:
        return _luma(numbers)
    if len(numbers) == 1:
        return _clamp(_float(numbers[0]))
    return 0.5


__all__ = ["PageContent", "Run", "Shape", "read_page"]
