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

"""ComfyUI, in its native form — what the editor itself saves.

    {"nodes": [{"id": 12, "type": "KSampler", "pos": [x, y],
                "inputs": [{"name": "model", "type": "MODEL", "link": 4}],
                "outputs": [{"name": "LATENT", "type": "LATENT", "links": [7]}],
                "widgets_values": [123456, "randomize", 20, 8.0]}],
     "links": [[7, 12, 0, 13, 0, "LATENT"]],
     "groups": [...]}

Two things about it are worth knowing before reading the code:

- **A link is a flat list**, `[id, fromNode, fromSlot, toNode, toSlot, type]`,
  and the *slot* is an index into the node's `outputs`/`inputs` rather than a
  name. Everything downstream of here works in names, so the slot is looked up
  and turned into one.
- **`widgets_values` has no labels.** It is a bare list, in the order the node
  class declares its widgets, and the file does not say what those are. So the
  values are drawn with the labels the common nodes are known to use, and a node
  nobody has heard of gets its values numbered — which is still most of what a
  reader wants to see, and never a guess dressed up as a fact.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: Widget labels for the nodes almost every workflow contains. Anything not in
#: here is numbered rather than guessed at.
WIDGETS: Dict[str, List[str]] = {
    "KSampler": ["seed", "control", "steps", "cfg", "sampler", "scheduler", "denoise"],
    "KSamplerAdvanced": [
        "add noise", "seed", "control", "steps", "cfg", "sampler",
        "scheduler", "start", "end", "return noise",
    ],
    "CheckpointLoaderSimple": ["checkpoint"],
    "CLIPTextEncode": ["text"],
    "EmptyLatentImage": ["width", "height", "batch"],
    "LatentUpscale": ["method", "width", "height", "crop"],
    "SaveImage": ["prefix"],
    "LoadImage": ["image", "upload"],
    "VAELoader": ["vae"],
    "LoraLoader": ["lora", "strength model", "strength clip"],
    "ImageScale": ["method", "width", "height", "crop"],
    "ConditioningCombine": [],
}

#: What a node's class name says about its part in the graph. Matched as a
#: suffix or a substring, longest first, and it never decides a colour — only a
#: role, which the host colours from the palette.
ROLES: List[Tuple[str, str]] = [
    ("Loader", "input"),
    ("Load", "input"),
    ("Save", "output"),
    ("Preview", "output"),
    ("Sampler", "flow"),
    ("Encode", "pure"),
    ("Decode", "pure"),
    ("Note", "note"),
    ("Reroute", "variable"),
]


def looks_like(document: Any) -> bool:
    """Whether this is a ComfyUI workflow as the editor saves it."""
    if not isinstance(document, dict):
        return False
    nodes = document.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    return isinstance(first, dict) and "type" in first and "id" in first


def role_of(class_name: str) -> str:
    for needle, role in ROLES:
        if needle in class_name:
            return role
    return "normal"


def _fields(class_name: str, values: Any) -> List[dict]:
    """The values written on the face of the node.

    The prompt, the seed, the step count: this is most of what a workflow is
    opened to be read for. Long text is cut here rather than on the canvas, so
    what travels over the pipe is what can be read on a box.
    """
    if not isinstance(values, list) or not values:
        return []

    labels = WIDGETS.get(class_name, [])
    fields = []
    for index, value in enumerate(values):
        if isinstance(value, (dict, list)):
            continue
        label = labels[index] if index < len(labels) else "#%d" % (index + 1)
        text = str(value)
        if len(text) > 120:
            text = text[:119] + "…"
        fields.append({"label": label, "value": text})
    return fields


def _pins(entries: Any, fallback: str) -> List[dict]:
    pins = []
    for index, entry in enumerate(entries or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("label") or "%s%d" % (fallback, index)
        pin = {"id": str(name)}
        if entry.get("name"):
            pin["label"] = str(entry["name"])
        if entry.get("type") and isinstance(entry["type"], str):
            pin["type"] = entry["type"]
        pins.append(pin)
    return pins


def read(document: dict, cap: int) -> Tuple[dict, int]:
    """The workflow as the host's own document, and how many nodes were dropped.

    Past ``cap`` nodes the graph is cut and the count comes back with it. A
    canvas that silently shows two thirds of a workflow is a lie, and the page
    says so — the same rule the git log follows when it stops short.
    """
    raw = [node for node in document.get("nodes", []) if isinstance(node, dict)]
    dropped = max(0, len(raw) - cap)
    raw = raw[:cap]

    nodes: List[dict] = []
    #: (node id, slot index) -> pin name, for both ends of every wire.
    outputs: Dict[Tuple[str, int], str] = {}
    inputs: Dict[Tuple[str, int], str] = {}

    for entry in raw:
        node_id = str(entry.get("id"))
        class_name = str(entry.get("type") or "")
        position = entry.get("pos") or [0, 0]
        # Older files write `pos` as a dict keyed "0" and "1".
        if isinstance(position, dict):
            position = [position.get("0", 0), position.get("1", 0)]
        size = entry.get("size") or []
        if isinstance(size, dict):
            size = [size.get("0"), size.get("1")]

        ins = _pins(entry.get("inputs"), "in")
        outs = _pins(entry.get("outputs"), "out")
        for slot, pin in enumerate(ins):
            inputs[(node_id, slot)] = pin["id"]
        for slot, pin in enumerate(outs):
            outputs[(node_id, slot)] = pin["id"]

        node: dict = {
            "id": node_id,
            "title": str(entry.get("title") or class_name or node_id),
            "role": role_of(class_name),
            "x": float(position[0] if len(position) > 0 else 0),
            "y": float(position[1] if len(position) > 1 else 0),
        }
        if entry.get("title") and class_name:
            # The class is worth keeping when the user renamed the node: a
            # workflow full of "Sampler 2" says nothing about what it does.
            node["subtitle"] = class_name
        if isinstance(size, list) and size and isinstance(size[0], (int, float)):
            node["width"] = float(size[0])
        if entry.get("flags", {}).get("collapsed"):
            node["collapsed"] = True
        if entry.get("mode") in (2, 4):
            # Muted or bypassed in the editor. It is still in the graph, and
            # saying so is the point of drawing the file at all.
            node["badges"] = ["muted"]
        if ins:
            node["inputs"] = ins
        if outs:
            node["outputs"] = outs
        fields = _fields(class_name, entry.get("widgets_values"))
        if fields:
            node["fields"] = fields
        nodes.append(node)

    links = []
    for wire in document.get("links", []) or []:
        # `[id, from, fromSlot, to, toSlot, type]`, and older files pad it.
        if not isinstance(wire, list) or len(wire) < 5:
            continue
        source, from_slot, target, to_slot = wire[1], wire[2], wire[3], wire[4]
        link: dict = {
            "from": str(source),
            "to": str(target),
            "role": "data",
        }
        from_pin = outputs.get((str(source), int(from_slot or 0)))
        to_pin = inputs.get((str(target), int(to_slot or 0)))
        if from_pin:
            link["fromPin"] = from_pin
        if to_pin:
            link["toPin"] = to_pin
        if len(wire) > 5 and isinstance(wire[5], str):
            link["type"] = wire[5]
        links.append(link)

    groups = []
    for index, group in enumerate(document.get("groups", []) or []):
        if not isinstance(group, dict):
            continue
        bounds = group.get("bounding") or group.get("bounds") or []
        if len(bounds) < 4:
            continue
        groups.append(
            {
                "id": "g%d" % index,
                "title": str(group.get("title") or ""),
                "x": float(bounds[0]),
                "y": float(bounds[1]),
                "width": float(bounds[2]),
                "height": float(bounds[3]),
            }
        )

    return (
        {"nodes": nodes, "links": links, "groups": groups, "notes": []},
        dropped,
    )
