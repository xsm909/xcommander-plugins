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

"""Node-RED, as `flows.json` holds it.

    [{"id": "t1", "type": "tab", "label": "Flow 1"},
     {"id": "n1", "type": "inject", "z": "t1", "x": 150, "y": 100,
      "wires": [["n2"]]},
     {"id": "n2", "type": "debug", "z": "t1", "x": 380, "y": 100, "wires": []},
     {"id": "c1", "type": "mqtt-broker", "name": "the broker"}]

One flat array holding four different kinds of thing, and telling them apart is
most of this reader:

- **A `tab` is a canvas, not a node.** Every node names its tab in `z`, and each
  tab has *its own coordinate space* starting near the origin. Drawn as the file
  says, four flows land on top of one another in a heap. So the tabs are stacked
  down the page, each shifted clear of the one above, and each gets a group box
  with its label on it — which is what the plan meant by "tabs are the groups".
- **A node with no `x` is not on any canvas.** Brokers, credentials, servers:
  Node-RED calls them configuration nodes and draws them in a side panel. A box
  for each would be a canvas of things nobody wired to anything.
- **Wires join nodes, not ports.** `wires` is a list per output, each holding the
  ids it feeds; nothing anywhere names an input socket. So an output pin is its
  index and the input is left off entirely — the document allows that on purpose,
  and it is this format it was allowed for.
- **`link out` jumps rather than wires.** It carries `links`, the ids of the
  `link in` nodes it reaches, usually on another tab. Those are drawn too: a jump
  that is not drawn is a flow that appears to stop dead.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: What a type says about a node's part in the flow. Matched as a substring,
#: longest and most specific first.
ROLES: List[Tuple[str, str]] = [
    ("inject", "event"),
    ("catch", "event"),
    ("status", "event"),
    ("mqtt in", "event"),
    ("http in", "event"),
    ("websocket in", "event"),
    ("watch", "event"),
    ("link in", "variable"),
    ("link out", "variable"),
    ("link call", "flow"),
    ("debug", "output"),
    ("http response", "output"),
    ("mqtt out", "output"),
    ("websocket out", "output"),
    ("file out", "output"),
    ("e-mail", "output"),
    ("function", "pure"),
    ("change", "pure"),
    ("template", "pure"),
    ("json", "pure"),
    ("csv", "pure"),
    ("xml", "pure"),
    ("range", "pure"),
    ("switch", "flow"),
    ("split", "flow"),
    ("join", "flow"),
    ("batch", "flow"),
    ("delay", "flow"),
    ("trigger", "flow"),
    ("http request", "input"),
    ("file in", "input"),
    ("exec", "input"),
]

#: Properties worth writing on the face of a box. The rest of a Node-RED node is
#: editor state, credentials and rule trees — and a box covered in `payloadType`
#: says less than a box with nothing on it.
INTERESTING = (
    "topic",
    "payload",
    "url",
    "method",
    "property",
    "func",
    "template",
    "filename",
    "repeat",
    "crontab",
    "command",
    "action",
    "mode",
    "rules",
    "complete",
)

#: What separates one tab's block from the next, in document units. Wide enough
#: that two flows never read as one.
TAB_GAP = 220


def looks_like(document: Any) -> bool:
    """Whether this is a Node-RED flow file.

    A flat list whose entries all carry an `id` and a `type`, and which has at
    least one thing that is placed on a canvas or names a tab. A bare list of
    dictionaries from somewhere else fails the second half.
    """
    if not isinstance(document, list) or not document:
        return False
    entries = [entry for entry in document if isinstance(entry, dict)]
    if len(entries) != len(document):
        return False
    if not all("id" in entry and "type" in entry for entry in entries):
        return False
    return any(
        entry.get("type") == "tab" or ("z" in entry and "wires" in entry)
        for entry in entries
    )


def role_of(kind: str) -> str:
    lowered = kind.lower()
    for needle, role in ROLES:
        if needle in lowered:
            return role
    return "normal"


def _fields(entry: dict) -> List[dict]:
    fields = []
    for key in INTERESTING:
        if key not in entry:
            continue
        value = entry[key]
        if isinstance(value, dict):
            continue
        if isinstance(value, list):
            # A rule tree is not readable on a box; how many there are is.
            if not value:
                continue
            fields.append({"label": key, "value": "%d" % len(value)})
            continue
        text = str(value).replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 120:
            text = text[:119] + "…"
        fields.append({"label": key, "value": text})
    return fields


def _placed(entry: dict) -> bool:
    """Whether this entry is drawn on a canvas at all."""
    return "x" in entry and "y" in entry and entry.get("type") not in ("tab",)


def read(document: list, cap: int) -> Tuple[dict, int]:
    """The flows as the host's own document, and how many nodes were dropped."""
    entries = [entry for entry in document if isinstance(entry, dict)]

    tabs = [entry for entry in entries if entry.get("type") == "tab"]
    subflows = [entry for entry in entries if entry.get("type") == "subflow"]
    canvases = tabs + subflows

    comments = [
        entry
        for entry in entries
        if entry.get("type") == "comment" and _placed(entry)
    ]
    boxes = [
        entry
        for entry in entries
        if _placed(entry)
        and entry.get("type") not in ("comment", "group", "subflow")
    ]
    groups = [
        entry
        for entry in entries
        if entry.get("type") == "group" and "x" in entry and "y" in entry
    ]

    dropped = max(0, len(boxes) - cap)
    boxes = boxes[:cap]
    known = {str(entry.get("id")) for entry in boxes}

    # Every tab starts its own coordinates near the origin, so laid out as the
    # file says they would be drawn one on top of another. Each tab's block is
    # shifted clear of the one above it instead.
    order = [str(entry.get("id")) for entry in canvases]
    for entry in boxes:
        home = str(entry.get("z") or "")
        if home not in order:
            order.append(home)

    shift: Dict[str, float] = {}
    running = 0.0
    for home in order:
        mine = [entry for entry in boxes + comments if str(entry.get("z") or "") == home]
        mine += [entry for entry in groups if str(entry.get("z") or "") == home]
        if not mine:
            continue
        top = min(float(entry.get("y") or 0) for entry in mine)
        bottom = max(
            float(entry.get("y") or 0) + float(entry.get("h") or 0) for entry in mine
        )
        shift[home] = running - top
        running += (bottom - top) + TAB_GAP

    def place(entry: dict) -> Tuple[float, float]:
        home = str(entry.get("z") or "")
        return (
            float(entry.get("x") or 0),
            float(entry.get("y") or 0) + shift.get(home, 0.0),
        )

    # Which nodes have anything coming in. Node-RED never says; the wires do.
    fed = set()
    links: List[dict] = []
    for entry in boxes:
        source = str(entry.get("id"))
        wires = entry.get("wires")
        if isinstance(wires, list):
            for index, targets in enumerate(wires):
                if not isinstance(targets, list):
                    continue
                pin = "out" if len(wires) == 1 else "%d" % (index + 1)
                for target in targets:
                    target = str(target)
                    if target not in known:
                        continue
                    fed.add(target)
                    links.append(
                        {
                            "from": source,
                            "to": target,
                            "fromPin": pin,
                            "role": "flow",
                        }
                    )
        # The jump. `link out` and `link call` name where they land, and it is
        # usually on a different tab — undrawn, the flow looks as if it stops.
        if "link" in str(entry.get("type") or ""):
            for target in entry.get("links") or []:
                target = str(target)
                if target not in known or target == source:
                    continue
                fed.add(target)
                links.append(
                    {
                        "from": source,
                        "to": target,
                        "role": "data",
                        "type": "link",
                        "label": "link",
                    }
                )

    nodes = []
    for entry in boxes:
        identifier = str(entry.get("id"))
        kind = str(entry.get("type") or "")
        name = str(entry.get("name") or "").strip()
        x, y = place(entry)
        node: dict = {
            "id": identifier,
            "title": name or kind,
            "role": role_of(kind),
            "x": x,
            "y": y,
        }
        if name:
            node["subtitle"] = kind
        # Disabled in the editor is `d`, and it is worth saying: a flow with a
        # dead node in the middle of it is a flow that does not run.
        if entry.get("d"):
            node["badges"] = ["muted"]
        if identifier in fed:
            node["inputs"] = [{"id": "in", "label": ""}]
        wires = entry.get("wires")
        if isinstance(wires, list) and wires:
            node["outputs"] = [
                {"id": "out" if len(wires) == 1 else "%d" % (index + 1),
                 "label": "" if len(wires) == 1 else "%d" % (index + 1)}
                for index in range(len(wires))
            ]
        fields = _fields(entry)
        if fields:
            node["fields"] = fields
        nodes.append(node)

    return (
        {
            "nodes": nodes,
            "links": links,
            "groups": _groups(canvases, groups, boxes, comments, shift),
            "notes": [_note(entry, place(entry)) for entry in comments],
        },
        dropped,
    )


def _note(entry: dict, at: Tuple[float, float]) -> dict:
    """A comment node: a title and, often, a paragraph nobody else records."""
    title = str(entry.get("name") or "").strip()
    body = str(entry.get("info") or "").strip()
    text = "\n\n".join(part for part in (title, body) if part)
    return {
        "text": text,
        "x": at[0],
        "y": at[1],
        "width": 260.0,
        "height": 120.0,
    }


def _groups(
    canvases: List[dict],
    groups: List[dict],
    boxes: List[dict],
    comments: List[dict],
    shift: Dict[str, float],
) -> List[dict]:
    """A box per tab, and one per group the author drew themselves."""
    drawn: List[dict] = []

    for tab in canvases:
        home = str(tab.get("id"))
        mine = [
            entry
            for entry in boxes + comments
            if str(entry.get("z") or "") == home
        ]
        if not mine:
            continue
        left = min(float(entry.get("x") or 0) for entry in mine)
        right = max(float(entry.get("x") or 0) for entry in mine)
        top = min(float(entry.get("y") or 0) for entry in mine)
        bottom = max(float(entry.get("y") or 0) for entry in mine)
        offset = shift.get(home, 0.0)
        drawn.append(
            {
                "id": home,
                "title": str(tab.get("label") or tab.get("name") or "Flow"),
                # Room for the boxes themselves, whose width the host measures
                # and this cannot know.
                "x": left - 60,
                "y": top + offset - 60,
                "width": (right - left) + 320,
                "height": (bottom - top) + 160,
            }
        )

    for group in groups:
        home = str(group.get("z") or "")
        drawn.append(
            {
                "id": str(group.get("id")),
                "title": str(group.get("name") or ""),
                "x": float(group.get("x") or 0),
                "y": float(group.get("y") or 0) + shift.get(home, 0.0),
                "width": float(group.get("w") or 0),
                "height": float(group.get("h") or 0),
            }
        )

    return drawn
