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

"""The document as markdown: headings, lists and tables, and nothing else.

This is the whole point of the plugin. It is *not* a PDF viewer — a page as a
page is one Enter away, in whatever the machine opens PDFs with. What this
produces is the document with the page thrown away: no running heads, no page
numbers, no line breaks left over from a column measure nobody is reading in.

**Structure is looked for three ways, in this order of trust.**

1. **The tags the file carries.** `/StructTreeRoot` says which text is a
   heading, which is a table cell and where the rows end, and where it exists it
   is simply right. Roughly two documents in five have it.
2. **The grid the page draws.** With no tags, the rectangles and rules a table
   is drawn out of *are* the table: text is filed into whichever cell contains
   it, and a cell with three lines in it stays one cell. This was proved on a
   form with no tags at all and it rebuilt all fourteen rows.
3. **The baselines.** Last of all, lines from shared baselines and columns from
   the gaps between words. It is a guess, and a reading that rests on it says so.

**A scan says so and stops.** Half the pages in a real collection carry no text
at all, and a viewer that showed them as a blank screen would be lying about the
file. One sentence, and Enter opens the page itself.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pdfdoc
import pdfpage

#: Text this much larger than the body is a heading, when nothing better says so.
HEADING_RATIO = 1.18

#: How far apart two baselines may be and still be one line, as a fraction of
#: the text size.
LINE_TOLERANCE = 0.45

#: A gap this wide, in ems, is the space between two columns rather than
#: between two words.
COLUMN_GAP = 2.2

#: How many pages with not one word on them settle it that a file is a scan.
#: A cover, a plate and a blank verso are three; two dozen are a photocopier.
SCAN_VERDICT = 24

#: Seconds to spend reading pages before saying what has been read so far. The
#: host allows a call sixty; this leaves room for the answer to travel.
TIME_BUDGET = 25.0


class Result:
    """What came out, and by which road."""

    def __init__(self) -> None:
        self.markdown: str = ""
        self.route: str = ""
        self.pages: int = 0
        self.pages_read: int = 0
        self.characters: int = 0
        self.tables: int = 0
        self.headings: int = 0
        self.notes: List[str] = []
        self.empty: bool = False
        self.truncated: bool = False


# -- the outermost layer ---------------------------------------------------


def convert(doc: pdfdoc.Document, max_pages: int = 0, max_characters: int = 0,
            seconds: float = TIME_BUDGET) -> Result:
    """A whole document as markdown."""
    result = Result()
    pages = doc.pages()
    result.pages = len(pages)
    if not pages:
        result.empty = True
        result.notes.append("This file has no pages.")
        return result

    wanted = pages if max_pages <= 0 else pages[:max_pages]
    result.pages_read = len(wanted)
    result.truncated = len(wanted) < len(pages)

    reader = _Reader(doc, wanted, time.monotonic() + seconds if seconds > 0 else None)
    blocks = _tagged_document(doc, reader)
    if blocks is not None:
        result.route = "tags"
    else:
        blocks = _laid_out_document(reader)
        result.route = reader.route or "flow"
    if reader.stopped_early:
        result.truncated = True
        result.pages_read = reader.read_count
    blocks = _drop_repeats(blocks, len(wanted))
    _normalise_headings(blocks)

    if not any(block.speaks for block in blocks):
        result.empty = True
        if reader.unreadable_font:
            result.notes.append(
                "The text in this file is drawn with fonts that carry no "
                "table of what their codes mean, so the words cannot be read "
                "out of it."
            )
        else:
            result.notes.append(
                "There is no text in this file — it is a scan, a picture of a "
                "document rather than a document."
            )
        return result

    text = _render(blocks)
    if max_characters and len(text) > max_characters:
        cut = text.rfind("\n", 0, max_characters)
        text = text[: cut if cut > 0 else max_characters]
        result.truncated = True
    result.markdown = text
    result.characters = len(text)
    result.tables = sum(1 for block in blocks if isinstance(block, _Table))
    result.headings = sum(1 for block in blocks if isinstance(block, _Heading))
    if reader.unreadable_font:
        result.notes.append(
            "Some of this document is drawn with a font that does not say what "
            "its codes mean; that part is missing from the reading."
        )
    return result


# -- blocks ----------------------------------------------------------------


class _Block:
    speaks = True

    def lines(self) -> List[str]:  # pragma: no cover - overridden everywhere
        return []


class _Paragraph(_Block):
    """A run of prose, with the two things that decide what it turns out to be.

    [bold] and [size] are carried because whether a line is a heading cannot be
    settled while reading it: a bold sentence that runs over two lines looks
    exactly like a heading followed by a heading until the second line is
    there. So the decision is made once the lines have been put back together.
    """

    def __init__(self, text: str, bold: bool = False, size: float = 0.0):
        self.text = text
        self.bold = bold
        self.size = size

    @property
    def speaks(self) -> bool:  # noqa: D401 - a property standing in for a flag
        return bool(self.text.strip())

    def lines(self) -> List[str]:
        return [self.text]


class _Heading(_Block):
    """A heading, at the depth the document put it and not at a fixed one.

    [level] is what the file said — `/Title` is nought, `/H3` is three, and a
    heading found by its size is ranked among the sizes on the page. What comes
    out is worked out afterwards by [_normalise_headings], because a document
    that starts at `/H2` should still open with a `#` and one that has both a
    title and an `/H1` cannot have them both be one.
    """

    def __init__(self, level: int, text: str):
        self.level = level
        self.shown = max(1, min(6, level or 1))
        self.text = text

    @property
    def speaks(self) -> bool:
        return bool(self.text.strip())

    def lines(self) -> List[str]:
        return ["#" * self.shown + " " + self.text]


class _Item(_Block):
    def __init__(self, marker: str, text: str, depth: int):
        self.marker = marker
        self.text = text
        self.depth = max(0, min(6, depth))

    @property
    def speaks(self) -> bool:
        return bool(self.text.strip())

    def lines(self) -> List[str]:
        return ["  " * self.depth + self.marker + " " + self.text]


class _Rule(_Block):
    speaks = False

    def lines(self) -> List[str]:
        return ["---"]


class _Table(_Block):
    """A grid of cells, with a head only when the document really has one.

    An empty head row is deliberate and is not a gap: the host's markdown
    reader draws no head bar for one, which is the honest way to show a table
    whose first row is data like every other row. Guessing a head would put a
    line of the document in a place it does not belong.
    """

    def __init__(self, head: List[str], rows: List[List[str]]):
        self.head = head
        self.rows = rows

    @property
    def speaks(self) -> bool:
        return any(cell.strip() for row in self.rows for cell in row) or any(
            cell.strip() for cell in self.head
        )

    def lines(self) -> List[str]:
        width = max([len(self.head)] + [len(row) for row in self.rows] or [0])
        if width <= 0:
            return []
        head = (self.head + [""] * width)[:width]
        out = ["| " + " | ".join(_cell(c) for c in head) + " |",
               "|" + "---|" * width]
        for row in self.rows:
            padded = (row + [""] * width)[:width]
            out.append("| " + " | ".join(_cell(c) for c in padded) + " |")
        return out


def _cell(text: str) -> str:
    """A cell's text, safe inside a pipe table.

    A line break inside a cell becomes `<br>`: GFM has no other way to say it,
    and a multi-line cell is exactly what the grid route exists to keep.
    """
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return re.sub(r"\s*\n\s*", "<br>", text).strip()


def _render(blocks: List[_Block]) -> str:
    out: List[str] = []
    previous: Optional[_Block] = None
    for block in blocks:
        if not block.speaks and not isinstance(block, _Rule):
            continue
        rendered = block.lines()
        if not rendered:
            continue
        if out:
            tight = (
                isinstance(block, _Item)
                and isinstance(previous, _Item)
            )
            if not tight:
                out.append("")
        out.extend(rendered)
        previous = block
    text = "\n".join(out).rstrip()
    return text + "\n" if text else ""


# -- reading the pages -----------------------------------------------------


class _Reader:
    """Runs pages once and keeps them, however many routes ask."""

    def __init__(self, doc: pdfdoc.Document, pages: List[Dict[str, Any]],
                 deadline: Optional[float] = None):
        self.doc = doc
        self.pages = pages
        self.deadline = deadline
        self.stopped_early = False
        self.read_count = 0
        self.route = ""
        self._content: Dict[int, pdfpage.PageContent] = {}
        self._by_ref: Dict[int, int] = {}
        self._fonts: Dict[int, Any] = {}
        for i, page in enumerate(pages):
            ref = page.get("__ref__")
            if isinstance(ref, pdfdoc.Ref):
                self._by_ref[ref.num] = i

    @property
    def unreadable_font(self) -> bool:
        return any(page.unreadable_font for page in self._content.values())

    @property
    def out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def content(self, index: int) -> pdfpage.PageContent:
        if index not in self._content:
            self._content[index] = pdfpage.read_page(
                self.doc, self.pages[index], index + 1, self._fonts
            )
            self.read_count = max(self.read_count, index + 1)
        return self._content[index]

    def content_of_ref(self, ref: Any) -> Optional[pdfpage.PageContent]:
        if isinstance(ref, pdfdoc.Ref) and ref.num in self._by_ref:
            return self.content(self._by_ref[ref.num])
        return None

    def read_all(self) -> List[pdfpage.PageContent]:
        """Every page, and two reasons to stop before the end of a long one.

        **A scan is settled early.** Two dozen pages without one word on them
        is not a document this viewer can show, and running the remaining six
        hundred to say so again would be the slowest way to reach the same
        sentence.
        """
        out: List[pdfpage.PageContent] = []
        found_text = False
        for i in range(len(self.pages)):
            page = self.content(i)
            out.append(page)
            if not found_text and any(
                run.text.strip() and not run.artifact for run in page.runs
            ):
                found_text = True
            if not found_text and len(out) >= SCAN_VERDICT:
                break
            if self.out_of_time and i + 1 < len(self.pages):
                self.stopped_early = True
                break
        return out


# -- route one: the tags the file carries ----------------------------------

_HEADINGS = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}
_SKIP = {"Artifact", "Private"}


def _tagged_document(doc: pdfdoc.Document, reader: _Reader) -> Optional[List[_Block]]:
    """The document through `/StructTreeRoot`, or `None` when it has none."""
    root_ref = doc.catalog.get("StructTreeRoot")
    root = doc.dict_of(root_ref)
    if not root or "K" not in root:
        return None
    role_map = doc.resolve(root.get("RoleMap"))
    roles = {}
    if isinstance(role_map, dict):
        roles = {str(k): str(doc.resolve(v) or "") for k, v in role_map.items()}

    walker = _TagWalker(doc, reader, roles)
    blocks: List[_Block] = []
    walker.walk(root_ref if isinstance(root_ref, pdfdoc.Ref) else root, None, blocks, 0)
    if not any(block.speaks for block in blocks):
        return None
    return blocks


class _TagWalker:
    def __init__(self, doc: pdfdoc.Document, reader: _Reader, roles: Dict[str, str]):
        self.doc = doc
        self.reader = reader
        self.roles = roles
        self.seen: Set[int] = set()
        self.has_title = False

    # -- the shape of a node ----------------------------------------------

    def role(self, node: Dict[str, Any]) -> str:
        name = self.doc.resolve(node.get("S"))
        name = str(name) if isinstance(name, pdfdoc.Name) else ""
        seen: Set[str] = set()
        while name in self.roles and name not in seen:
            seen.add(name)
            name = self.roles[name]
        return name

    def kids(self, node: Dict[str, Any]) -> List[Any]:
        k = self.doc.resolve(node.get("K"))
        if k is None:
            return []
        return k if isinstance(k, list) else [k]

    def page_of(self, node: Dict[str, Any], fallback: Any) -> Any:
        pg = node.get("Pg")
        return pg if pg is not None else fallback

    # -- text ---------------------------------------------------------------

    def text_of(self, node: Any, page: Any) -> str:
        """Every piece of text under one element, in the order it is written.

        **With the line breaks put back.** The tags say this is one paragraph
        and say nothing about how it was set, and a paragraph is usually a
        dozen separate marked pieces, one per line. Joining them with nothing
        turns every hyphenated word into a hyphenated word — `Ве-ликобритания`
        — so the pieces are separated by where they sat on the page, and
        [_tidy] then decides which hyphens were the measure's doing.
        """
        pieces: List[str] = []
        self._gather(node, page, pieces, [None, 0.0], 0)
        return "".join(pieces)

    def _gather(self, node: Any, page: Any, pieces: List[str],
                cursor: List[Any], depth: int) -> None:
        if depth > 32:
            return
        node = self.doc.resolve(node)
        if isinstance(node, int) and not isinstance(node, bool):
            self._leaf(int(node), page, pieces, cursor)
            return
        if not isinstance(node, dict):
            return
        kind = str(self.doc.resolve(node.get("Type")) or "")
        if kind == "OBJR":
            return
        if kind == "MCR" or ("MCID" in node and "S" not in node):
            mcid = self.doc.resolve(node.get("MCID"))
            if isinstance(mcid, (int, float)) and not isinstance(mcid, bool):
                self._leaf(int(mcid), self.page_of(node, page), pieces, cursor)
            return
        if self.role(node) in _SKIP:
            return
        here = self.page_of(node, page)
        for kid in self.kids(node):
            self._gather(kid, here, pieces, cursor, depth + 1)

    def _leaf(self, mcid: int, page: Any, pieces: List[str], cursor: List[Any]) -> None:
        content = self.reader.content_of_ref(page)
        if content is None:
            return
        text = "".join(content.by_mcid.get(mcid, []))
        if not text:
            return
        place = content.mcid_place.get(mcid)
        if place is not None:
            first, last, size = place
            previous, previous_size = cursor[0], cursor[1]
            reference = max(1.0, min(size, previous_size or size) * 0.5)
            if previous is not None and abs(first - previous) > reference:
                # Unless what came before was a drop capital: one big letter
                # on its own baseline, which is the first letter of the very
                # word that follows and not a line of its own.
                if not _is_drop_cap(pieces, previous_size, size):
                    pieces.append("\n")
            cursor[0], cursor[1] = last, size
        pieces.append(text)

    # -- the walk -----------------------------------------------------------

    def walk(self, node: Any, page: Any, out: List[_Block], depth: int,
             list_depth: int = 0) -> None:
        if depth > 64:
            return
        if isinstance(node, pdfdoc.Ref):
            if node.num in self.seen:
                return
            self.seen.add(node.num)
        d = self.doc.dict_of(node)
        if not d:
            return
        role = self.role(d)
        here = self.page_of(d, page)

        if role in _SKIP:
            return
        if role == "Title":
            text = _tidy(self.text_of(node, here))
            if text:
                out.append(_Heading(0, text))
            return
        if role in _HEADINGS or role == "H":
            text = _tidy(self.text_of(node, here))
            if text:
                # A bare `/H` is a heading whose depth is where it stands: the
                # specification lets a document nest sections and use `/H`
                # throughout, and the nesting is then the only thing that says
                # which is under which.
                out.append(_Heading(_HEADINGS.get(role, max(1, depth // 2)), text))
            return
        if role in ("P", "Note", "Caption", "Quote", "BlockQuote", "Code",
                    "Reference", "Index", "Form"):
            text = _tidy(self.text_of(node, here))
            if text:
                out.append(_Paragraph(text))
            return
        if role == "Table":
            table = self._table(node, here)
            if table is not None:
                out.append(table)
            return
        if role == "L":
            for kid in self.kids(d):
                self.walk(kid, here, out, depth + 1, list_depth + 1)
            return
        if role == "LI":
            self._item(node, here, out, depth, list_depth)
            return
        if role == "TOCI":
            text = _tidy(self.text_of(node, here))
            if text:
                out.append(_Item("-", text, max(0, list_depth)))
            return
        if role == "Figure":
            # A picture is not shown, and saying "a picture" over and over is
            # exactly the noise this viewer exists to remove. Only its
            # alternate text, which is a sentence somebody wrote.
            alt = self.doc.resolve(d.get("Alt"))
            text = _tidy(pdfdoc.text_of(alt)) if isinstance(alt, bytes) else ""
            if text:
                out.append(_Paragraph("*%s*" % text))
            return
        for kid in self.kids(d):
            self.walk(kid, here, out, depth + 1, list_depth)

    def _item(self, node: Any, page: Any, out: List[_Block], depth: int,
              list_depth: int) -> None:
        d = self.doc.dict_of(node)
        label = ""
        body_blocks: List[_Block] = []
        body_text: List[str] = []
        for kid in self.kids(d):
            kd = self.doc.dict_of(kid)
            role = self.role(kd) if kd else ""
            if role == "Lbl":
                label = _tidy(self.text_of(kid, page))
            elif role == "LBody":
                # A list item may hold a nested list, a table, or plain text;
                # its own text is the item, and anything structural under it
                # comes out after.
                for inner in self.kids(kd):
                    inner_dict = self.doc.dict_of(inner)
                    inner_role = self.role(inner_dict) if inner_dict else ""
                    if inner_role in ("L", "Table"):
                        self.walk(inner, page, body_blocks, depth + 1, list_depth)
                    else:
                        body_text.append(self.text_of(inner, page))
                if not self.kids(kd):
                    body_text.append(self.text_of(kid, page))
            else:
                body_text.append(self.text_of(kid, page))
        text = _tidy("".join(body_text))
        marker = label if re.match(r"^\(?\d+[.)]?$", label) else "-"
        if text:
            out.append(_Item(marker, text, max(0, list_depth - 1)))
        out.extend(body_blocks)

    # -- tables -------------------------------------------------------------

    def _table(self, node: Any, page: Any) -> Optional[_Table]:
        d = self.doc.dict_of(node)
        head_rows: List[List[str]] = []
        body_rows: List[List[str]] = []
        header_flags: List[List[bool]] = []

        def rows_under(target: Any, into: List[List[str]], flags: List[List[bool]],
                       inside_head: bool, depth: int = 0) -> None:
            if depth > 16:
                return
            td = self.doc.dict_of(target)
            if not td:
                return
            role = self.role(td)
            here = self.page_of(td, page)
            if role == "TR":
                cells: List[str] = []
                heads: List[bool] = []
                for kid in self.kids(td):
                    kd = self.doc.dict_of(kid)
                    if not kd:
                        continue
                    cell_role = self.role(kd)
                    if cell_role not in ("TD", "TH"):
                        continue
                    text = _tidy(self.text_of(kid, self.page_of(kd, here)))
                    span = self.doc.resolve(kd.get("ColSpan"))
                    span = int(span) if isinstance(span, (int, float)) and span else 1
                    cells.append(text)
                    heads.append(cell_role == "TH")
                    # A spanned cell is spread over the columns it covers, so
                    # every row has the same width and the table lines up.
                    for _ in range(max(0, min(span, 32) - 1)):
                        cells.append("")
                        heads.append(cell_role == "TH")
                if cells:
                    into.append(cells)
                    flags.append(heads)
                return
            if role == "THead":
                for kid in self.kids(td):
                    rows_under(kid, head_rows, header_flags, True, depth + 1)
                return
            for kid in self.kids(td):
                rows_under(kid, into, flags, inside_head, depth + 1)

        for kid in self.kids(d):
            rows_under(kid, body_rows, header_flags, False)

        rows = head_rows + body_rows
        if not rows:
            return None
        head: List[str] = []
        if head_rows:
            head = head_rows[0]
            rows = head_rows[1:] + body_rows
        elif header_flags and header_flags[0] and all(header_flags[0]):
            head = rows[0]
            rows = rows[1:]
        return _Table(head, rows)


# -- routes two and three: the page as it is drawn -------------------------


def _laid_out_document(reader: _Reader) -> List[_Block]:
    """Every page read from its own layout, with the furniture thrown away."""
    pages = reader.read_all()
    furniture = _repeating_lines(pages)
    body_size = _body_size(pages)
    used_grid = False
    blocks: List[_Block] = []
    for page in pages:
        page_blocks, grid = _laid_out_page(page, body_size, furniture)
        used_grid = used_grid or grid
        blocks.extend(page_blocks)
    blocks = _gather_rows(blocks)
    blocks = _join_paragraphs(blocks)
    blocks = _bold_headings(blocks)
    reader.route = "grid" if used_grid else "flow"
    return blocks


def _laid_out_page(page: pdfpage.PageContent, body_size: float,
                   furniture: Set[Tuple[int, str]]) -> Tuple[List[_Block], bool]:
    runs = [r for r in page.runs if not r.artifact and r.text.strip()]
    if not runs:
        return [], False

    cells = _grid_cells(page)
    blocks: List[_Block] = []
    used_grid = False
    claimed: Set[int] = set()

    if cells:
        used_grid = True
        tables, claimed = _tables_from_grid(page, cells, runs)
        # Text outside every table keeps its place in the reading: the blocks
        # are ordered by where they sit down the page, not by kind.
        loose = [r for i, r in enumerate(runs) if i not in claimed]
        ordered: List[Tuple[float, Any]] = [(top, table) for top, table in tables]
        for block, top in _flow_blocks(loose, page, body_size, furniture):
            ordered.append((top, block))
        ordered.sort(key=lambda pair: pair[0])
        blocks = [item for _top, item in ordered]
    else:
        blocks = []
        for column in _columns(runs, page):
            blocks.extend(
                block for block, _top in
                _flow_blocks(column, page, body_size, furniture)
            )
    return blocks, used_grid


# -- columns ---------------------------------------------------------------

#: A gutter narrower than this is the space between two words in a wide line.
GUTTER = 14.0

#: How much of the page's width to look in. A margin note is not a column.
GUTTER_MARGIN = 0.15


def _columns(runs: List[pdfpage.Run], page: pdfpage.PageContent,
             depth: int = 0) -> List[List[pdfpage.Run]]:
    """A page set in columns, as one list of runs per column.

    **Reading a two-column page across the columns produces nonsense**, and it
    is the commonest way a reading can be wrong: a curriculum vitae with a
    sidebar comes out as the sidebar and the body interleaved line by line,
    which is worse than useless because it reads as though it were a document.

    The gutter is found by where nothing is written: a strip of the width that
    no piece of text crosses. What tells a gutter from the space between two
    columns **of a table** is the lines — in a table the two sides share their
    baselines, row by row, and in a page set in columns they do not.
    """
    if depth >= 3 or len(runs) < 20:
        return [runs]
    split = _gutter(runs, page)
    if split is None:
        return [runs]
    left = [r for r in runs if r.x1 <= split]
    right = [r for r in runs if r.x0 >= split]
    if min(len(left), len(right)) < len(runs) * 0.15:
        return [runs]

    if not _reads_as_columns(_lines_of(runs), split):
        return [runs]
    return _columns(left, page, depth + 1) + _columns(right, page, depth + 1)


#: How much of a page has to be written on one side of the gutter with nothing
#: beside it before the two halves are believed to be columns rather than a
#: table. A table is nearly nought by construction.
SOLO_SHARE = 0.25


def _reads_as_columns(lines: Sequence[Sequence[pdfpage.Run]], split: float) -> bool:
    """Whether the two sides of a gutter are columns or the sides of a table.

    **The question is not whether the sides line up — it is whether either side
    ever runs on alone.** In a table every row has a cell on both sides, by
    construction, so a quarter of the page standing alone on one side or the
    other cannot happen; in a page set in columns it always does, because the
    two columns were never written to line up. Both sides must do it at least
    once, and one of them twice running, or a table with one empty cell would
    pass.

    That leaves the borderless table, which draws nothing for the grid route to
    find — and it is caught by the same rule, having no solo lines either.
    """
    if not lines:
        return False
    counts = {"left": 0, "right": 0}
    longest = 0
    current = ""
    length = 0
    for line in lines:
        has_left = any(run.x1 <= split for run in line)
        has_right = any(run.x0 >= split for run in line)
        side = "left" if has_left and not has_right else (
            "right" if has_right and not has_left else "")
        if side:
            counts[side] += 1
            length = length + 1 if side == current else 1
            longest = max(longest, length)
        else:
            length = 0
        current = side
    solo = counts["left"] + counts["right"]
    return (
        solo >= len(lines) * SOLO_SHARE
        and counts["left"] > 0
        and counts["right"] > 0
        and longest >= 2
    )


def _gutter(runs: Sequence[pdfpage.Run], page: pdfpage.PageContent) -> Optional[float]:
    """The middle of the widest strip of the page nothing is written across."""
    if page.width <= 0:
        return None
    step = 2.0
    width = int(page.width / step) + 2
    covered = bytearray(width)
    for run in runs:
        first = max(0, int(run.x0 / step))
        last = min(width - 1, int(run.x1 / step) + 1)
        for i in range(first, last + 1):
            covered[i] = 1
    low = int(page.width * GUTTER_MARGIN / step)
    high = int(page.width * (1 - GUTTER_MARGIN) / step)
    best = None
    start = None
    for i in range(low, high + 1):
        if not covered[i]:
            if start is None:
                start = i
        elif start is not None:
            best = _wider(best, (start, i - 1))
            start = None
    if start is not None:
        best = _wider(best, (start, high))
    if best is None or (best[1] - best[0] + 1) * step < GUTTER:
        return None
    return (best[0] + best[1] + 1) / 2 * step


def _wider(best, candidate):
    if best is None:
        return candidate
    return candidate if (candidate[1] - candidate[0]) > (best[1] - best[0]) else best


# -- the drawn grid --------------------------------------------------------


def _grid_cells(page: pdfpage.PageContent) -> List[List[pdfpage.Shape]]:
    """The table cells this page draws, as rows.

    Two sources, because a table is drawn either way: rectangles, which is what
    a word processor writes, and rules, which is what a typesetter writes. Both
    end as the same list of boxes.
    """
    boxes = [s for s in page.shapes if s.width > 8 and s.height > 4
             and s.width < page.width * 0.99 and s.height < page.height * 0.9]
    boxes = _distinct(boxes)
    rows = _rows_of(boxes)
    if _plausible(rows):
        return rows
    ruled = _boxes_from_rules(page)
    rows = _rows_of(ruled)
    if _plausible(rows):
        return rows
    return []


def _distinct(boxes: Sequence[pdfpage.Shape]) -> List[pdfpage.Shape]:
    """One box per place: a cell drawn filled and then stroked is one cell."""
    out: Dict[Tuple[int, int, int, int], pdfpage.Shape] = {}
    for box in boxes:
        key = (round(box.x0), round(box.y0), round(box.x1), round(box.y1))
        kept = out.get(key)
        if kept is None or (box.filled and not kept.filled):
            out[key] = box
    return list(out.values())


def _rows_of(boxes: Sequence[pdfpage.Shape], tolerance: float = 2.0
             ) -> List[List[pdfpage.Shape]]:
    """Boxes sharing a top edge are one row."""
    rows: List[List[pdfpage.Shape]] = []
    for box in sorted(boxes, key=lambda b: (round(b.y0, 1), b.x0)):
        if rows and abs(rows[-1][0].y0 - box.y0) <= tolerance:
            rows[-1].append(box)
        else:
            rows.append([box])
    # A box that swallows a whole row is the row's own frame, not a cell.
    cleaned: List[List[pdfpage.Shape]] = []
    for row in rows:
        if len(row) > 1:
            widest = max(b.width for b in row)
            inner = [b for b in row if b.width < widest * 0.95]
            row = inner if len(inner) >= 2 else row
        cleaned.append(sorted(row, key=lambda b: b.x0))
    return cleaned


def _plausible(rows: Sequence[Sequence[pdfpage.Shape]]) -> bool:
    """Whether this really looks like a table and not like three stray boxes."""
    real = [row for row in rows if len(row) >= 2]
    if len(real) < 2:
        return False
    cells = sum(len(row) for row in real)
    return cells >= 4


def _boxes_from_rules(page: pdfpage.PageContent) -> List[pdfpage.Shape]:
    """Cells from the lines a table is ruled with, where it has no rectangles."""
    horizontal = sorted({round(y0, 1) for x0, y0, x1, y1 in page.rules
                         if abs(y1 - y0) < 0.6 and abs(x1 - x0) > 20})
    vertical = sorted({round(x0, 1) for x0, y0, x1, y1 in page.rules
                       if abs(x1 - x0) < 0.6 and abs(y1 - y0) > 8})
    horizontal = _merge_near(horizontal, 2.0)
    vertical = _merge_near(vertical, 2.0)
    if len(horizontal) < 3 or len(vertical) < 3:
        return []
    out: List[pdfpage.Shape] = []
    for top, bottom in zip(horizontal, horizontal[1:]):
        if bottom - top < 5:
            continue
        for left, right in zip(vertical, vertical[1:]):
            if right - left < 8:
                continue
            out.append(pdfpage.Shape(left, top, right, bottom, False, True, 1.0))
    return out if len(out) <= 4000 else []


def _merge_near(values: Sequence[float], tolerance: float) -> List[float]:
    out: List[float] = []
    for value in values:
        if out and value - out[-1] <= tolerance:
            continue
        out.append(value)
    return out


def _tables_from_grid(page: pdfpage.PageContent, rows: List[List[pdfpage.Shape]],
                      runs: List[pdfpage.Run]
                      ) -> Tuple[List[Tuple[float, _Table]], Set[int]]:
    """Text filed into the boxes that contain it, as one table per run of rows."""
    claimed: Set[int] = set()
    groups: List[List[List[pdfpage.Shape]]] = []
    for row in rows:
        if len(row) < 2:
            continue
        if groups and _continues(groups[-1][-1], row):
            groups[-1].append(row)
        else:
            groups.append([row])

    out: List[Tuple[float, _Table]] = []
    for group in groups:
        if len(group) < 2:
            continue
        width = max(len(row) for row in group)
        body: List[List[str]] = []
        bold_rows: List[bool] = []
        filled_rows: List[bool] = []
        for row in group:
            cells: List[str] = []
            bold = True
            for box in row:
                text, indices, all_bold = _text_in(box, runs)
                claimed.update(indices)
                cells.append(text)
                bold = bold and all_bold and bool(text)
            cells += [""] * (width - len(cells))
            body.append(cells)
            bold_rows.append(bold)
            filled_rows.append(all(b.filled and b.gray < 0.93 for b in row))
        if not any(cell.strip() for row in body for cell in row):
            continue
        head: List[str] = []
        if _is_head(body, bold_rows, filled_rows):
            head = body[0]
            body = body[1:]
        if len(body) < 2:
            # One row of cells is a box round a line of text — a title in a
            # frame, a banner across the top — and not a table. Its text comes
            # out as text, in its place; the boxes are still claimed, so
            # nothing is read twice.
            for cell in (head + [c for row in body for c in row]):
                if cell.strip():
                    out.append((group[0][0].y0, _Paragraph(_tidy(cell))))
            continue
        out.append((group[0][0].y0, _Table(head, body)))
    return out, claimed


def _continues(previous: Sequence[pdfpage.Shape], row: Sequence[pdfpage.Shape]) -> bool:
    """Whether this row belongs to the same table as the one above it."""
    gap = row[0].y0 - previous[0].y1
    if gap > 14 or gap < -6:
        return False
    left = min(b.x0 for b in previous), min(b.x0 for b in row)
    right = max(b.x1 for b in previous), max(b.x1 for b in row)
    return abs(left[0] - left[1]) < 24 and abs(right[0] - right[1]) < 24


def _text_in(box: pdfpage.Shape, runs: List[pdfpage.Run]
             ) -> Tuple[str, List[int], bool]:
    """Everything written inside one cell, its own lines kept apart."""
    inside: List[Tuple[int, pdfpage.Run]] = []
    for i, run in enumerate(runs):
        middle = (run.x0 + run.x1) / 2
        if box.x0 - 1 <= middle <= box.x1 + 1 and box.y0 - 1 <= run.y <= box.y1 + 1:
            inside.append((i, run))
    if not inside:
        return "", [], False
    indices = [i for i, _ in inside]
    lines = _lines_of([run for _, run in inside])
    text = "\n".join(_line_text(line) for line in lines)
    all_bold = all(run.bold for _, run in inside)
    return text.strip(), indices, all_bold


def _is_head(rows: List[List[str]], bold: List[bool], filled: List[bool]) -> bool:
    """Whether the first row of an untagged table is its head.

    Three things can say so and nothing else may: every cell of it is filled
    with a tint the rest of the table is not, every word of it is bold and the
    rest is not, or it alone has no empty cell while the body has plenty. Where
    none of them holds, the table is drawn with **no** head at all — the
    renderer is ours and it can do that, and inventing one would promote a line
    of the document into a place it was never in.
    """
    if len(rows) < 2:
        return False
    first = rows[0]
    if not all(cell.strip() for cell in first):
        return False
    rest = rows[1:]
    if filled and filled[0] and not any(filled[1:]):
        return True
    if bold and bold[0] and not any(bold[1:]):
        return True
    empties = sum(1 for row in rest for cell in row if not cell.strip())
    return empties >= max(2, len(rest))


# -- the baselines ---------------------------------------------------------


def _lines_of(runs: Sequence[pdfpage.Run]) -> List[List[pdfpage.Run]]:
    """Runs grouped into the lines they were drawn on, top to bottom."""
    lines: List[List[pdfpage.Run]] = []
    for run in sorted(runs, key=lambda r: (round(r.y, 1), r.x0)):
        if lines:
            last = lines[-1][-1]
            tolerance = max(1.5, min(last.size, run.size) * LINE_TOLERANCE)
            if abs(last.y - run.y) <= tolerance:
                lines[-1].append(run)
                continue
        lines.append([run])
    return [sorted(line, key=lambda r: r.x0) for line in lines]


def _line_text(line: Sequence[pdfpage.Run]) -> str:
    """One line's words, with the spaces the file never wrote put back."""
    out: List[str] = []
    previous: Optional[pdfpage.Run] = None
    for run in line:
        text = run.text
        if previous is not None:
            gap = run.x0 - previous.x1
            reference = min(previous.size, run.size) or 10.0
            if gap > reference * 0.18 and not out[-1].endswith(" ") and not text.startswith(" "):
                out.append(" ")
        out.append(text)
        previous = run
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def _split_columns(line: Sequence[pdfpage.Run]) -> List[List[pdfpage.Run]]:
    """A line whose pieces stand far apart is a row of cells, not a sentence."""
    columns: List[List[pdfpage.Run]] = [[]]
    previous: Optional[pdfpage.Run] = None
    for run in line:
        if previous is not None:
            gap = run.x0 - previous.x1
            reference = min(previous.size, run.size) or 10.0
            if gap > reference * COLUMN_GAP:
                columns.append([])
        columns[-1].append(run)
        previous = run
    return [c for c in columns if c]


def _flow_blocks(runs: Sequence[pdfpage.Run], page: pdfpage.PageContent,
                 body_size: float, furniture: Set[Tuple[int, str]]
                 ) -> List[Tuple[_Block, float]]:
    """One page's loose text as blocks, each with where it sat on the page."""
    out: List[Tuple[_Block, float]] = []
    for line in _lines_of(runs):
        text = _line_text(line)
        if not text:
            continue
        if (_band(line[0].y, page.height), text) in furniture:
            continue
        if _is_page_number(text, line, page):
            continue
        size = max(run.size for run in line)
        top = line[0].y

        columns = _split_columns(line)
        if len(columns) > 1 and _looks_tabular(columns):
            cells = [_line_text(column) for column in columns]
            out.append((_Table([], [cells]), top))
            continue

        marker = _list_marker(text)
        if marker is not None:
            bullet, body = marker
            out.append((_Item(bullet, body, _indent_of(line, page)), top))
            continue

        if size > body_size * HEADING_RATIO and len(text) < 200:
            level = _heading_level(size, body_size)
            out.append((_Heading(level, text), top))
            continue
        bold = all(run.bold for run in line)
        out.append((_Paragraph(text, bold=bold, size=size), top))
    return out


def _looks_tabular(columns: Sequence[Sequence[pdfpage.Run]]) -> bool:
    """Whether these columns are a row of a table rather than a wide heading."""
    if len(columns) < 2:
        return False
    return all(any(run.text.strip() for run in column) for column in columns)


_BULLETS = "•◦▪▫‣·–—*"


def _list_marker(text: str) -> Optional[Tuple[str, str]]:
    if not text:
        return None
    if text[0] in _BULLETS and len(text) > 1 and text[1] in " \t":
        return "-", text[1:].strip()
    m = re.match(r"^(\(?\d{1,3}[.)])\s+(.*)$", text)
    if m and len(m.group(2)) > 1:
        return m.group(1).rstrip(")."), m.group(2)
    # A lettered list, and only in lower case: `N. G. Chernyshevsky` is a name
    # and `A. Smith` is a name, and both begin exactly like `a) …` does.
    m = re.match(r"^([a-zа-я][.)])\s+(.+)$", text)
    if m and len(m.group(2)) > 2 and not re.match(r"^[A-ZА-Я]\.", m.group(2)):
        return "-", m.group(2)
    return None


def _indent_of(line: Sequence[pdfpage.Run], page: pdfpage.PageContent) -> int:
    return max(0, min(4, int((line[0].x0 - page.width * 0.06) // 18)))


def _heading_level(size: float, body: float) -> int:
    ratio = size / body if body else 1.0
    if ratio >= 2.0:
        return 1
    if ratio >= 1.6:
        return 2
    if ratio >= 1.35:
        return 3
    return 4


def _body_size(pages: Sequence[pdfpage.PageContent]) -> float:
    """The size most of the words are set in — everything is measured from it."""
    counts: Dict[int, int] = {}
    for page in pages:
        for run in page.runs:
            if run.artifact:
                continue
            key = int(round(run.size * 2))
            counts[key] = counts.get(key, 0) + len(run.text)
    if not counts:
        return 10.0
    return max(counts.items(), key=lambda pair: pair[1])[0] / 2 or 10.0


def _band(y: float, height: float) -> int:
    """Which strip of the page a line sits in, to a couple of points."""
    return int(round(y / 3.0)) if height else 0


def _repeating_lines(pages: Sequence[pdfpage.PageContent]) -> Set[Tuple[int, str]]:
    """Text that stands in the same place on page after page: the furniture.

    A running head is not part of the document, and neither is the footer with
    the company name in it. Both are found by the one rule that does not need
    to know what they say — they repeat, in the same place, and the document
    does not.
    """
    if len(pages) < 3:
        return set()
    counts: Dict[Tuple[int, str], int] = {}
    for page in pages:
        seen: Set[Tuple[int, str]] = set()
        for line in _lines_of([r for r in page.runs if r.text.strip()]):
            text = _line_text(line)
            if not text or len(text) > 120:
                continue
            near_edge = line[0].y < page.height * 0.12 or line[0].y > page.height * 0.88
            if not near_edge:
                continue
            key = (_band(line[0].y, page.height), text)
            if key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
    threshold = max(3, int(len(pages) * 0.5))
    return {key for key, count in counts.items() if count >= threshold}


_PAGE_NUMBER = re.compile(
    r"^(?:[-–—]\s*)?(?:page|стр\.?|страница|s\.|p\.)?\s*\d{1,4}"
    r"(?:\s*(?:/|of|из|iz)\s*\d{1,4})?\s*(?:[-–—])?$",
    re.IGNORECASE,
)


def _is_page_number(text: str, line: Sequence[pdfpage.Run],
                    page: pdfpage.PageContent) -> bool:
    if len(text) > 24 or not any(c.isdigit() for c in text):
        return False
    near_edge = line[0].y < page.height * 0.10 or line[0].y > page.height * 0.90
    return near_edge and bool(_PAGE_NUMBER.match(text.strip()))


# -- putting the lines back into paragraphs --------------------------------

_ENDS_SENTENCE = re.compile(r"[.!?:;»”\"'\)]\s*$")


def _join_paragraphs(blocks: Sequence[_Block]) -> List[_Block]:
    """Undoes the line breaks a page measure put in, and nothing else.

    This is what "without the textual noise" means in practice: a paragraph
    that was set forty characters wide arrives here as eight paragraphs, and
    leaving it that way would be showing the reader the page again. Two lines
    are one paragraph when the first does not end a sentence and the second
    does not start something new.
    """
    out: List[_Block] = []
    for block in blocks:
        if (
            isinstance(block, _Paragraph)
            and out
            and isinstance(out[-1], _Paragraph)
            and out[-1].bold == block.bold
            and _similar(out[-1].size, block.size)
            and _joins(out[-1].text, block.text)
        ):
            previous = out[-1].text
            joined = (previous[:-1] + block.text
                      if previous.endswith("-") and len(previous) > 1
                      and previous[-2].isalpha()
                      else previous + " " + block.text)
            out[-1] = _Paragraph(joined, bold=block.bold, size=out[-1].size)
            continue
        out.append(block)
    return out


def _gather_rows(blocks: Sequence[_Block]) -> List[_Block]:
    """Lines that stand in columns become one table; a lone one becomes text.

    The baseline route can only see one line at a time, and a line whose pieces
    stand far apart is a row of *something*. Whether it is a table is a
    question about its neighbours: three such lines in a row are a table, and
    one on its own is a heading with a date in the corner, which is how the
    top of every form is set. So the decision waits until the page is read.
    """
    out: List[_Block] = []
    for block in blocks:
        if (
            isinstance(block, _Table)
            and not block.head
            and len(block.rows) == 1
            and out
            and isinstance(out[-1], _Table)
            and not out[-1].head
            and len(out[-1].rows[0]) == len(block.rows[0])
        ):
            out[-1].rows.append(block.rows[0])
            continue
        out.append(block)
    settled: List[_Block] = []
    for block in out:
        if isinstance(block, _Table) and not block.head and len(block.rows) < 2:
            for cell in (block.rows[0] if block.rows else []):
                if cell.strip():
                    settled.append(_Paragraph(_tidy(cell)))
            continue
        settled.append(block)
    return settled


def _bold_headings(blocks: Sequence[_Block]) -> List[_Block]:
    """A short bold paragraph on its own is a heading; a long one is emphasis.

    Only now, with the lines joined: `…when the brand does` and `appear in
    responses.` are one bold sentence, and calling each of them a heading —
    which is what deciding line by line does — breaks the document into
    nonsense.
    """
    out: List[_Block] = []
    for block in blocks:
        if isinstance(block, _Paragraph) and block.bold and 0 < len(block.text) < 120:
            out.append(_Heading(5, block.text))
        else:
            out.append(block)
    return out


def _joins(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if len(first) < 30:
        return False
    if _ENDS_SENTENCE.search(first) and not first.endswith("-"):
        return False
    if second[0].isupper() and _ENDS_SENTENCE.search(first):
        return False
    if second.startswith(("#", "-", "|", "*", ">")):
        return False
    return True


def _is_drop_cap(pieces: Sequence[str], previous_size: float, size: float) -> bool:
    """Whether the piece just laid down is an initial, not a line."""
    if not pieces or previous_size < size * 1.5:
        return False
    return len(pieces[-1].strip()) == 1 and pieces[-1].strip().isalpha()


def _normalise_headings(blocks: Sequence[_Block]) -> None:
    """Turns the levels a document used into `#` through `######`, in order."""
    levels = sorted({block.level for block in blocks if isinstance(block, _Heading)})
    if not levels:
        return
    rank = {level: min(6, i + 1) for i, level in enumerate(levels)}
    for block in blocks:
        if isinstance(block, _Heading):
            block.shown = rank[block.level]


def _drop_repeats(blocks: List[_Block], pages: int) -> List[_Block]:
    """Throws away the line that stands on every page and says the same thing.

    The positional rule catches a running head by *where* it sits, and it needs
    the page to catch it; this catches the same thing by what it says, which is
    what is left once the structure tags have taken the page away. Both want
    the same evidence: it repeats and the document does not.
    """
    if pages < 3:
        return [b for b in blocks if not _is_lone_number(b)]
    counts: Dict[str, int] = {}
    for block in blocks:
        if isinstance(block, (_Paragraph, _Heading)) and len(block.text) <= 120:
            counts[block.text] = counts.get(block.text, 0) + 1
    threshold = max(3, int(pages * 0.5))
    furniture = {text for text, count in counts.items() if count >= threshold}
    return [
        b for b in blocks
        if not (isinstance(b, (_Paragraph, _Heading)) and b.text in furniture)
        and not _is_lone_number(b)
    ]


def _is_lone_number(block: _Block) -> bool:
    """A paragraph that is only a page number, which no document ever wrote."""
    return (
        isinstance(block, _Paragraph)
        and len(block.text) <= 12
        and bool(_PAGE_NUMBER.match(block.text.strip()))
        and any(c.isdigit() for c in block.text)
    )


#: A hyphen at the end of a line with a small letter under it is where a word
#: was broken to fit the measure, and joining the halves back is the whole
#: difference between reading a document and reading a page.
_HYPHENATION = re.compile(r"([^\W\d_])[-\u2010\u00ad]\s*\n\s*([^\W\d_])", re.UNICODE)


#: Every character that is a space of some width — thin, hair, figure,
#: no-break. A page uses them to justify a line; a reading has one space.
_SPACES = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")


def _similar(first: float, second: float) -> bool:
    """Whether two lines are set in the same size, near enough to be one thing."""
    if not first or not second:
        return True
    return abs(first - second) <= max(first, second) * 0.12


def _tidy(text: str) -> str:
    """One element's text as a sentence: no stray breaks, no doubled spaces."""
    text = text.replace("\u00ad", "")  # a soft hyphen is a hint, not a letter
    text = _SPACES.sub(" ", text)
    text = _HYPHENATION.sub(
        lambda m: m.group(1) + m.group(2) if m.group(2).islower()
        else m.group(1) + "-" + m.group(2),
        text,
    )
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


__all__ = ["Result", "convert"]
