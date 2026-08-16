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

"""Files that *are* a node graph, drawn as one.

A ComfyUI workflow is a picture saved as JSON. Read as text it is four thousand
lines of coordinates; read as what it is, it is a dozen boxes with the prompt and
the seed written on them and the wires between them. This turns the one into the
other, and the host draws it.

**One plugin, several readers**, which was his answer on 2026-08-15: less to
install, and the document schema stays honest by having more than one user of it.
ComfyUI's native form is the first; n8n, Node-RED and the API export follow.
LiteGraph documents need no reader of their own — ComfyUI *is* LiteGraph, and a
class the table has never heard of gets numbered values rather than guessed
labels, which is the honest answer for a graph from some other editor built on
it.

**How it wins the `.json` files that are its own, and only those.** Every one of
these formats is a `.json`, and `.json` belongs to the text viewer — a reader
that took the extension outright would open `package.json` as an empty canvas.
So it claims the extension at a lower priority *and* declares a **probe**: the
host hands it the first pages of the file before it settles the order, and a
file that really is a workflow opens on **F3**. Anything else falls through to
the text viewer exactly as before, and the graph stays one Shift+F3 away.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xcommander import Plugin, error, nodes  # noqa: E402

import comfyapi  # noqa: E402
import comfyui  # noqa: E402
import n8n  # noqa: E402
import nodered  # noqa: E402
import parse  # noqa: E402

plugin = Plugin("org.xcommander.nodes", "Node graphs")

#: Files are read whole; a workflow is a few hundred kilobytes. The cap is for
#: the one that is not.
MAX_BYTES = 64 << 20

#: Past this many nodes the graph is cut, and the page says how many went. A
#: canvas that silently shows two thirds of a workflow is a lie.
MAX_NODES = 5000


def _load(url: str):
    """The file as parsed JSON, or the refusal to show instead.

    Read through the host, so a workflow inside an archive or on a transport
    another plugin provides opens exactly like one on the disk.
    """
    try:
        raw = plugin.read_file(url, max_bytes=MAX_BYTES)
    except Exception as failure:  # noqa: BLE001
        return None, error("The file could not be read: %s" % failure)
    if not raw:
        return None, error("The file is empty.")

    # Decoded the way every reader in this application decodes text: UTF-8, a
    # BOM tolerated, and a damaged byte does not stop the parse.
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as failure:  # noqa: BLE001
        return None, error("The file is not text: %s" % failure)

    # The first document, and nothing about what follows it — see `parse`.
    try:
        return parse.first_document(text), None
    except Exception as failure:  # noqa: BLE001
        return None, error("This is not JSON: %s" % failure)


#: The readers, in the order they are asked. Exactly one may claim a file —
#: `selftest.claims` holds that, and a new reader adds a row to it.
READERS = (comfyui, n8n, nodered, comfyapi)


def looks_like_a_graph(head: bytes) -> bool:
    """Whether the file whose first pages these are is a graph — the question
    the host asks before it decides which viewer opens a `.json`.

    Answering it is what puts a workflow on **F3** instead of one Shift+F3
    further on, and what keeps `package.json` out of the canvas.

    Two ways of answering, in order. A small file arrives whole, and then the
    honest answer is the same one opening it would give — the readers' own
    `looks_like`, over a real document. A large one arrives cut, usually inside
    a string, and then each reader is asked about the *text* instead: the keys
    its editor writes near the front. **A wrong yes is worse than a missed
    one** — it takes a file away from the viewer that should have had it — so
    both halves are written to say no when they are not sure.
    """
    try:
        text = head.decode("utf-8-sig", errors="replace")
    except Exception:  # noqa: BLE001
        return False

    try:
        document = parse.first_document(text)
    except Exception:  # noqa: BLE001
        document = None
    if document is not None:
        return any(reader.looks_like(document) for reader in READERS)

    return any(reader.signature(text) for reader in READERS)


@plugin.viewer(
    "nodes.graph",
    "Node graph",
    extensions=["json"],
    priority=5,
    probe=looks_like_a_graph,
)
def graph(url: str) -> dict:
    document, refusal = _load(url)
    if refusal is not None:
        return refusal

    for reader in READERS:
        if not reader.looks_like(document):
            continue
        body, dropped = reader.read(document, MAX_NODES)
        return nodes(
            body["nodes"],
            links=body["links"],
            groups=body["groups"],
            notes=body["notes"],
            # A format that carries no coordinates says so, and the host works
            # them out — it is the one that measured the boxes.
            layout=body.get("layout", "given"),
            truncated=dropped > 0,
        )

    # A JSON file that is not a graph. Said plainly rather than drawn as an
    # empty canvas: the reader is behind the text viewer precisely so that this
    # is the rare case, and when it happens the honest answer is a sentence.
    return error(
        "This JSON is not a node graph the reader knows: "
        "ComfyUI workflows, their API exports, n8n exports and Node-RED "
        "flows are the ones it reads so far."
    )


plugin.run()
