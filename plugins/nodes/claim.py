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

"""Which files are this reader's — the question the host asks before it decides
what opens on F3.

Its own module so that the self-test can ask it exactly as the host does.
`main.py` is the wiring and runs a plugin the moment it is imported, which is
why nothing may import it.
"""

from __future__ import annotations

import comfyapi
import comfyui
import godot
import n8n
import nodered
import parse
import png

#: The readers, in the order they are asked. **Exactly one may claim a file** —
#: `selftest.claims` holds that, and a new reader adds a row to it.
READERS = (comfyui, n8n, nodered, comfyapi)


def looks_like_a_graph(head: bytes) -> bool:
    """Whether the file whose first pages these are is a graph.

    Answering it is what puts a workflow on **F3** instead of one Shift+F3
    further on, and what keeps `package.json` out of the canvas.

    Two ways of answering, in order. A small file arrives whole, and then the
    honest answer is the one opening it would give — the readers' own
    `looks_like`, over a real document. A large one arrives cut, usually inside
    a string, and then each reader is asked about the *text* instead: the keys
    its editor writes near the front. **A wrong yes is worse than a missed
    one** — it takes a file away from the viewer that should have had it — so
    both halves are written to say no when they are not sure.
    """
    # **A picture is never claimed, workflow or no workflow.** F3 on an image
    # is the image: that is what the file is, and a reader that took it away
    # would be answering a question nobody asked. The graph inside it is one
    # Shift+F3 further on, which is where the plan put it.
    if png.is_png(head):
        return False

    try:
        text = head.decode("utf-8-sig", errors="replace")
    except Exception:  # noqa: BLE001
        return False

    # A Godot scene is the one graph here that is not JSON, so it is asked
    # first — nothing else would recognise it, and it recognises nothing else.
    if godot.signature(text):
        return True

    try:
        document = parse.first_document(text)
    except Exception:  # noqa: BLE001
        document = None
    if document is not None:
        return any(reader.looks_like(document) for reader in READERS)

    return any(reader.signature(text) for reader in READERS)
