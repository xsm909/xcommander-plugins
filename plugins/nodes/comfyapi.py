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

"""ComfyUI's *API* export — the one a server is actually sent.

    {"12": {"class_type": "KSampler",
            "inputs": {"seed": 812734, "steps": 20,
                       "model": ["11", 0], "positive": ["6", 0]},
            "_meta": {"title": "Sample"}}}

It is the same graph as the native file with everything about *drawing* it
thrown away, and that is what makes it worth reading:

- **There are no coordinates.** None, anywhere. So the document says
  `layout: "layered"` and the host works the positions out — which it can and
  this cannot, the spacing depending on how wide the boxes came out in the
  user's own font.
- **A wire and a value live in the same dictionary.** An input is either a
  literal — the seed, the number of steps — or the two-element list
  `[nodeId, slot]`, which is a wire. Telling them apart is the whole of the
  parse: the lists become links, the rest becomes the fields written on the box.
- **The slot is an index and there is no name for it**, because the node classes
  that would name it live in the server. So an output pin is called `out N`, and
  the input keeps the name the dictionary gave it, which is the good half.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from comfyui import ROLES  # the class-name rules are the same graph, so reuse


def looks_like(document: Any) -> bool:
    """Whether this is an API-format prompt.

    Every value is an object with a `class_type` — which is a shape nothing else
    has, and specific enough that no ordinary JSON is mistaken for one.
    """
    if not isinstance(document, dict) or not document:
        return False
    if "nodes" in document or "connections" in document:
        return False
    for value in document.values():
        if not isinstance(value, dict) or "class_type" not in value:
            return False
    return True


def signature(head: str) -> bool:
    """An API export is a map of nodes each carrying a `class_type`, which is a
    word no other format here writes."""
    return '"class_type"' in head

def role_of(class_name: str) -> str:
    for needle, role in ROLES:
        if needle in class_name:
            return role
    return "normal"


def _wire(value: Any) -> Tuple[str, int] | None:
    """`["11", 0]` is a wire; anything else is a value written on the box."""
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], (int, float))
    ):
        return str(value[0]), int(value[1])
    return None


def read(document: dict, cap: int) -> Tuple[dict, int]:
    entries = list(document.items())
    dropped = max(0, len(entries) - cap)
    entries = entries[:cap]
    known = {str(key) for key, _ in entries}

    nodes: List[dict] = []
    links: List[dict] = []
    #: Which output slots each node was actually asked for. The file never lists
    #: a node's outputs, so the pins are whatever somebody joined to.
    used: Dict[str, set] = {}

    for key, entry in entries:
        node_id = str(key)
        class_name = str(entry.get("class_type") or "")
        title = str((entry.get("_meta") or {}).get("title") or class_name or node_id)

        inputs: List[dict] = []
        fields: List[dict] = []
        for name, value in (entry.get("inputs") or {}).items():
            joined = _wire(value)
            if joined is not None:
                source, slot = joined
                if source not in known:
                    continue
                inputs.append({"id": str(name), "label": str(name)})
                used.setdefault(source, set()).add(slot)
                links.append(
                    {
                        "from": source,
                        "to": node_id,
                        "fromPin": "out %d" % slot,
                        "toPin": str(name),
                        "role": "data",
                    }
                )
                continue

            if isinstance(value, (dict, list)):
                continue
            text = str(value).replace("\n", " ").strip()
            if not text:
                continue
            if len(text) > 120:
                text = text[:119] + "…"
            fields.append({"label": str(name), "value": text})

        node: dict = {
            "id": node_id,
            "title": title,
            "role": role_of(class_name),
            # No coordinates in this format at all. Zero here and the host lays
            # them out; see `layout` below.
            "x": 0,
            "y": 0,
        }
        if title != class_name and class_name:
            node["subtitle"] = class_name
        if inputs:
            node["inputs"] = inputs
        if fields:
            node["fields"] = fields
        nodes.append(node)

    by_id = {node["id"]: node for node in nodes}
    for source, slots in used.items():
        node = by_id.get(source)
        if node is None:
            continue
        node["outputs"] = [{"id": "out %d" % slot} for slot in sorted(slots)]

    return (
        {
            "nodes": nodes,
            "links": links,
            "groups": [],
            "notes": [],
            "layout": "layered",
        },
        dropped,
    )
