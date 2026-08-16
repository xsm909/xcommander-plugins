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

"""A Godot scene, drawn as what it is: a tree with signals across it.

**The one reader here that is not JSON**, and the one graph that is two graphs
at once. A `.tscn` is a list of sections — `[node name="Sprite" type="Sprite2D"
parent="."]` and then the properties of that node — plus a run of
`[connection]` lines at the end saying which signal of which node calls which
method of which other one.

So two kinds of wire are drawn and they are told apart by their role: the tree
that holds the scene together is `data`, because it is structure, and a signal
is `flow`, because that is what actually runs. The palette gives them different
colours without this file naming one.

There are no coordinates in a scene file, so the host lays it out — `layered`,
reading **top to bottom**, which is the way a scene tree is read everywhere
else it is drawn.

What is deliberately approximate, in the spirit the outline reader is:

- a property whose value runs over several lines is kept as its first line.
  Godot writes arrays and dictionaries that way, and a node's face is not where
  anybody reads one;
- `[ext_resource]` and `[sub_resource]` are not nodes. They are the scene's
  materials and shapes, and drawing them would double the boxes to say nothing
  about the shape of the scene;
- an instanced child scene keeps the name it is instanced under. What is inside
  it lives in another file, which is another graph.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: `[node name="Sprite" type="Sprite2D" parent="."]`
_SECTION = re.compile(r"^\[(\w+)([^\]]*)\]\s*$")

#: `name="Sprite"`, `index="1"`, `format=3`
_ATTRIBUTE = re.compile(r'(\w+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s\]]+)')

#: `position = Vector2(0, -8)`
_PROPERTY = re.compile(r"^([\w/]+)\s*=\s*(.+)$")

#: How much of a property's value is written on the box.
MAX_VALUE = 60

#: Nodes whose name says they are the thing that starts something happening.
#: Only used for the role, which is a hint to the palette and never a colour.
_EVENTS = ("Area", "Button", "Timer", "Input", "Touch", "Ray")


def signature(text: str) -> bool:
    """Whether the first pages of a file look like a scene.

    `[gd_scene` is the first line of every scene Godot writes. The second test
    is for the file that arrives without its head — a fragment pasted, or a
    scene inside an archive that started mid-way.
    """
    return "[gd_scene" in text or ('[node name=' in text and "type=" in text)


def looks_like(text: str) -> bool:
    """Whether this really is a scene: it has to have at least one node."""
    return signature(text) and "[node name=" in text


def read(text: str, max_nodes: int) -> Tuple[Dict[str, Any], int]:
    """The scene as the document the canvas draws, and how many nodes were cut.

    Ids are paths — `.` for the root, `Sprite/Head` for a grandchild — because
    that is what the file itself uses to name a node in a `parent` and in a
    `[connection]`. Nothing is invented, so nothing has to be translated back.
    """
    nodes: List[dict] = []
    links: List[dict] = []
    at_path: Dict[str, int] = {}
    dropped = 0
    current: dict | None = None

    def close() -> None:
        nonlocal current
        current = None

    for line in text.splitlines():
        section = _SECTION.match(line.strip())
        if section is not None:
            close()
            kind, rest = section.group(1), section.group(2)
            attributes = {
                key: value.strip('"')
                for key, value in _ATTRIBUTE.findall(rest)
            }

            if kind == "node":
                name = attributes.get("name")
                if not name:
                    continue
                if len(nodes) >= max_nodes:
                    dropped += 1
                    continue
                parent = attributes.get("parent")
                path = _path_of(name, parent)
                node = {
                    "id": path,
                    "title": name,
                    "subtitle": attributes.get("type", "")
                    or _instanced(attributes),
                    "role": _role_of(attributes),
                    "fields": [],
                }
                at_path[path] = len(nodes)
                nodes.append(node)
                current = node

                # The tree itself, drawn as wires: parent to child. It is
                # `data` rather than `flow` because it is what the scene *is*,
                # not what it does — the signals below are what runs.
                if parent is not None:
                    links.append(
                        {
                            "from": "." if parent == "." else parent,
                            "to": path,
                            "role": "data",
                        }
                    )
                continue

            if kind == "connection":
                signal = attributes.get("signal", "")
                method = attributes.get("method", "")
                links.append(
                    {
                        "from": attributes.get("from", "."),
                        "to": attributes.get("to", "."),
                        "role": "flow",
                        "label": "%s → %s" % (signal, method)
                        if signal and method
                        else signal or method,
                    }
                )
                continue

            # `[gd_scene]`, `[ext_resource]`, `[sub_resource]`: the scene's
            # materials, not its shape.
            continue

        if current is None:
            continue
        written = _PROPERTY.match(line.strip())
        if written is None:
            continue
        key, value = written.group(1), written.group(2).strip()

        # A node with a script on it is a node that runs, which is what `flow`
        # means here. The root keeps its own role: it is the scene.
        if key == "script" and current["role"] == "normal":
            current["role"] = "flow"

        # The face of a box holds a handful of properties. A scene node can
        # carry thirty, and a canvas of thirty-line boxes is a canvas nobody
        # can see the shape of — the rest are one press away in the panel.
        if len(current["fields"]) < 6:
            current["fields"].append({"label": key, "value": _short(value)})

    return {
        "nodes": nodes,
        "links": links,
        "groups": [],
        "notes": [],
        # A scene file carries no coordinates at all, so the host lays it out —
        # it is the one that measured the boxes. Top to bottom, which is how a
        # scene tree is drawn everywhere else.
        "layout": "layered",
        "direction": "tb",
    }, dropped


def _short(value: str) -> str:
    """One line of a value, cut where a box stops being readable."""
    one = " ".join(value.split())
    if len(one) <= MAX_VALUE:
        return one
    return one[: MAX_VALUE - 1] + "…"


def _path_of(name: str, parent: str | None) -> str:
    if parent is None:
        return "."
    if parent == ".":
        return name
    return "%s/%s" % (parent, name)


def _instanced(attributes: Dict[str, str]) -> str:
    return "instance" if "instance" in attributes else ""


def _role_of(attributes: Dict[str, str]) -> str:
    kind = attributes.get("type", "")
    if attributes.get("parent") is None:
        return "output"
    for needle in _EVENTS:
        if needle in kind:
            return "event"
    return "normal"
