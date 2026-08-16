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

"""n8n, as the editor exports it.

    {"nodes": [{"id": "…", "name": "Loop Over Items", "type": "n8n-nodes-base…",
                "position": [x, y], "parameters": {…}}],
     "connections": {"Limit": {"main": [[{"node": "Loop Over Items",
                                          "type": "main", "index": 0}]]}}}

Three things about it decide most of the code:

- **Connections are keyed by the node's *name*, not by its id.** So the reader
  works in names and hands the host names as ids. Rename two nodes to the same
  thing in n8n and the file is already ambiguous; nothing here can mend that.
- **The output index is the position in the outer list**, and the *type* of the
  connection is its key: `main` is the flow, and everything beginning `ai_` is a
  model, a tool or a parser hanging off an agent. They are drawn as different
  pins, because in the editor they leave the node from different places and mean
  different things.
- **A sticky note is a node.** It has no wires and it carries what somebody wrote
  about the workflow, which is often the only documentation there is — so it
  becomes a note on the canvas rather than a box in the graph.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: What a node's type says about its part in the flow. Matched on the last
#: segment of `n8n-nodes-base.something`, longest first.
ROLES: List[Tuple[str, str]] = [
    ("Trigger", "event"),
    ("webhook", "event"),
    ("stickyNote", "note"),
    ("agent", "flow"),
    ("lmChat", "variable"),
    ("outputParser", "pure"),
    ("tool", "input"),
    ("set", "pure"),
    ("code", "pure"),
    ("if", "flow"),
    ("switch", "flow"),
    ("merge", "flow"),
    ("splitInBatches", "flow"),
    ("wait", "flow"),
    ("notion", "output"),
    ("gmail", "output"),
    ("airtable", "output"),
    ("httpRequest", "input"),
]

#: Parameters worth writing on the face of a node. Everything else in n8n's
#: parameter tree is nesting, credentials and editor state — and a box covered
#: in `{"__rl": true}` says less than a box with nothing on it.
INTERESTING = (
    "url",
    "method",
    "operation",
    "resource",
    "mode",
    "text",
    "prompt",
    "query",
    "amount",
    "unit",
    "maxItems",
    "fieldToSplitOut",
    "jsCode",
    "toolDescription",
    "description",
)


def looks_like(document: Any) -> bool:
    """Whether this is an n8n workflow export."""
    if not isinstance(document, dict):
        return False
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    return (
        isinstance(first, dict)
        and "name" in first
        and "type" in first
        and isinstance(document.get("connections"), dict)
    )


def signature(head: str) -> bool:
    """Whether the first pages of a file look like an n8n export.

    **Not `connections`**, which is the thing `looks_like` asks a whole document
    for: n8n writes its nodes first and its wires last, so in a real export the
    word does not appear until tens of kilobytes in — the self-test caught that
    on his own competitor-research workflow. What *is* at the front is the node
    shape, and `typeVersion` beside a `position` is n8n's alone: LiteGraph
    writes `pos`, Node-RED writes bare `x` and `y`, and neither versions its
    node types.
    """
    if '"typeVersion"' not in head:
        return False
    return '"position"' in head or '"connections"' in head

def role_of(kind: str) -> str:
    for needle, role in ROLES:
        if needle.lower() in kind.lower():
            return role
    return "normal"


def _short(kind: str) -> str:
    """`n8n-nodes-base.splitInBatches` → `splitInBatches`."""
    return kind.rsplit(".", 1)[-1]


def _fields(parameters: Any) -> List[dict]:
    if not isinstance(parameters, dict):
        return []
    fields = []
    for key in INTERESTING:
        if key not in parameters:
            continue
        value = parameters[key]
        if isinstance(value, (dict, list)):
            continue
        text = str(value).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 120:
            text = text[:119] + "…"
        fields.append({"label": key, "value": text})
    return fields


def read(document: dict, cap: int) -> Tuple[dict, int]:
    """The workflow as the host's own document, and how many nodes were dropped."""
    raw = [node for node in document.get("nodes", []) if isinstance(node, dict)]

    notes = []
    boxes = []
    for entry in raw:
        if _short(str(entry.get("type") or "")) == "stickyNote":
            notes.append(entry)
        else:
            boxes.append(entry)

    dropped = max(0, len(boxes) - cap)
    boxes = boxes[:cap]

    known = {str(entry.get("name")) for entry in boxes}
    connections = document.get("connections") or {}

    # Which pins each node needs. Worked out from the wires rather than declared
    # anywhere in the file: n8n says what is joined, never what the sockets are.
    outputs: Dict[str, List[str]] = {}
    inputs: Dict[str, List[str]] = {}
    links: List[dict] = []

    for source, kinds in connections.items():
        if source not in known or not isinstance(kinds, dict):
            continue
        for kind, slots in kinds.items():
            if not isinstance(slots, list):
                continue
            for index, wires in enumerate(slots):
                if not isinstance(wires, list):
                    continue
                pin = kind if len(slots) == 1 else "%s %d" % (kind, index)
                for wire in wires:
                    if not isinstance(wire, dict):
                        continue
                    target = str(wire.get("node"))
                    if target not in known:
                        continue
                    into = str(wire.get("type") or "main")
                    outputs.setdefault(source, [])
                    inputs.setdefault(target, [])
                    if pin not in outputs[source]:
                        outputs[source].append(pin)
                    if into not in inputs[target]:
                        inputs[target].append(into)
                    links.append(
                        {
                            "from": source,
                            "to": target,
                            "fromPin": pin,
                            "toPin": into,
                            # `main` is the order things run in; the `ai_` ones
                            # are what an agent is *made of*, and they read
                            # differently because they are different.
                            "role": "flow" if kind == "main" else "data",
                            "type": kind,
                        }
                    )

    nodes = []
    for entry in boxes:
        name = str(entry.get("name"))
        kind = str(entry.get("type") or "")
        position = entry.get("position") or [0, 0]
        node: dict = {
            "id": name,
            "title": name,
            "subtitle": _short(kind),
            "role": role_of(kind),
            "x": float(position[0] if len(position) > 0 else 0),
            "y": float(position[1] if len(position) > 1 else 0),
        }
        if entry.get("disabled"):
            node["badges"] = ["muted"]
        if inputs.get(name):
            node["inputs"] = [{"id": pin, "label": pin} for pin in inputs[name]]
        if outputs.get(name):
            node["outputs"] = [{"id": pin, "label": pin} for pin in outputs[name]]
        fields = _fields(entry.get("parameters"))
        if fields:
            node["fields"] = fields
        nodes.append(node)

    return (
        {
            "nodes": nodes,
            "links": links,
            "groups": [],
            "notes": [_note(entry) for entry in notes],
        },
        dropped,
    )


def _note(entry: dict) -> dict:
    """A sticky note, which in n8n is what documentation looks like."""
    parameters = entry.get("parameters") or {}
    position = entry.get("position") or [0, 0]
    return {
        "text": str(parameters.get("content") or ""),
        "x": float(position[0] if len(position) > 0 else 0),
        "y": float(position[1] if len(position) > 1 else 0),
        "width": float(parameters.get("width") or 240),
        "height": float(parameters.get("height") or 160),
    }
