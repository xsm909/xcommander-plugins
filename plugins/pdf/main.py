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

"""A PDF as markdown — the document, not the page.

**This is not a PDF viewer, and it is meant not to be.** Nothing here renders
anything: no PDFium, no system engine, no raster mode, no page thumbnails, and
not one third-party byte. F3 gives the document as text with its headings, its
lists and its tables, and the page as it was laid out is one Enter away in
whatever this machine already opens PDFs with — which is the right division of
labour, because a browser draws a page better than we ever would and *nothing*
shows a document without its furniture.

Everything the work is actually in lives next door: `pdfdoc` finds the objects,
`pdfcrypt` opens a file that was locked with the empty password, `pdffont` says
what the bytes mean, `pdfpage` runs a page to find out where the words sit, and
`pdfmd` decides what is a heading and what is a table. This file is the door.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xcommander import Plugin, error, markdown  # noqa: E402

import pdfcrypt  # noqa: E402
import pdfdoc  # noqa: E402
import pdfmd  # noqa: E402

plugin = Plugin("org.xcommander.pdf", "PDF as Markdown")

#: A PDF is mostly pictures by weight, and this viewer reads none of them — but
#: the file has to be read whole to be read at all, because the table saying
#: where everything is sits at the end of it.
MAX_BYTES = 256 << 20


@plugin.viewer(
    "pdf.markdown",
    "Document",
    extensions=["pdf"],
    # Above the hex dump and above nothing else: a .pdf is claimed by no other
    # viewer, so the number only settles the order in Shift+F3.
    priority=20,
)
def document(url: str) -> dict:
    started = time.time()
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return error("The file could not be read: %s" % failure)
    if not raw:
        return error("The file is empty.")

    # A PDF cannot be read from its beginning: the table saying where
    # everything is sits at the *end*. So a file cut short is not a shorter
    # document, it is an unreadable one — and saying which it is beats letting
    # the reader wonder why a book came out as three pages.
    if len(raw) >= MAX_BYTES:
        whole = (plugin.stat(url) or {}).get("size")
        if isinstance(whole, int) and whole > len(raw):
            return error(
                "This file is %d MB, and this viewer reads at most %d. A PDF "
                "keeps the table saying where everything is at its end, so a "
                "part of one cannot be read. Press Enter to open it in the "
                "system's own reader."
                % (whole >> 20, MAX_BYTES >> 20)
            )

    try:
        doc = pdfdoc.load(raw)
    except pdfcrypt.Locked as failure:
        return error("%s Press Enter to open it in the system's own reader." % failure)
    except pdfdoc.PdfError as failure:
        return error(str(failure))
    except Exception as failure:  # noqa: BLE001
        return error("This PDF could not be read: %s" % failure)

    max_pages = int(plugin.setting("maxPages", 0) or 0)
    max_characters = int(plugin.setting("maxCharacters", 0) or 0)
    try:
        found = pdfmd.convert(doc, max_pages=max_pages, max_characters=max_characters)
    except Exception as failure:  # noqa: BLE001
        return error("This PDF could not be read as a document: %s" % failure)

    if found.empty:
        # Never a blank screen: a file with no text in it says so, and says
        # what to do instead.
        return error(
            "%s\n\nPress Enter to open the pages themselves in the system's "
            "own reader." % (found.notes[0] if found.notes else
                             "There is nothing to read in this file.")
        )

    plugin.log(
        "%s: %d page(s) by the %s, %d characters, %d table(s), %.2fs"
        % (url, found.pages_read, _road(found.route), found.characters,
           found.tables, time.time() - started)
    )
    return markdown(_preamble(found) + found.markdown, truncated=found.truncated)


def _road(route: str) -> str:
    return {
        "tags": "file's own tags",
        "grid": "grid the pages draw",
        "flow": "baselines",
    }.get(route, route)


def _preamble(found: pdfmd.Result) -> str:
    """What the reader is owed before the document starts, and nothing more.

    Only ever a warning: how many pages were left out, that the reading is a
    guess, that a font could not be read. When the document came out clean this
    is empty, because a banner over every file is the noise this viewer exists
    to remove.
    """
    remarks = []
    if found.route == "flow":
        remarks.append(
            "This file carries no structure of its own and draws no grid, so "
            "what follows was worked out from where the words sit on the page."
        )
    if found.truncated and found.pages_read < found.pages:
        remarks.append(
            "The first %d of %d pages." % (found.pages_read, found.pages)
        )
    remarks.extend(found.notes)
    if not remarks:
        return ""
    return "> " + "\n> ".join(remarks) + "\n\n"


plugin.run()
