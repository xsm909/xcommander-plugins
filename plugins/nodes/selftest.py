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

"""The readers, checked without the application.

Run it with `python3 selftest.py`. It needs nothing installed: the fixture is a
ComfyUI workflow small enough to read by eye, and every assertion is about a
thing that has gone wrong in a graph reader somewhere.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comfyui  # noqa: E402

WORKFLOW = {
    "nodes": [
        {
            "id": 1,
            "type": "CheckpointLoaderSimple",
            "pos": [20, 60],
            "size": [240, 90],
            "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}],
            "widgets_values": ["sd_xl_base_1.0.safetensors"],
        },
        {
            "id": 3,
            "type": "KSampler",
            "pos": [600, 60],
            "inputs": [{"name": "model", "type": "MODEL", "link": 1}],
            "outputs": [{"name": "LATENT", "type": "LATENT", "links": []}],
            "widgets_values": [812734, "randomize", 20, 8.0, "euler", "normal", 1.0],
            "mode": 4,
        },
        {
            "id": 9,
            "type": "SomethingNobodyHasHeardOf",
            "pos": [0, 400],
            "widgets_values": ["a", 7],
        },
    ],
    "links": [[1, 1, 0, 3, 0, "MODEL"]],
    "groups": [{"title": "Conditioning", "bounding": [280, 10, 300, 140]}],
}


def check(what: str, ok: bool) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        raise SystemExit(1)


def main() -> None:
    check("a workflow is recognised", comfyui.looks_like(WORKFLOW))
    check("a bare list is not", not comfyui.looks_like([1, 2, 3]))
    check("and neither is a package.json", not comfyui.looks_like({"name": "x"}))

    body, dropped = comfyui.read(WORKFLOW, 5000)
    nodes = {node["id"]: node for node in body["nodes"]}

    check("every node came through", len(nodes) == 3 and dropped == 0)

    # A slot is an index in the file and a name here: everything downstream
    # works in names, so the translation happens once, at the edge.
    wire = body["links"][0]
    check(
        "a link's slots became pin names",
        wire["from"] == "1"
        and wire["fromPin"] == "MODEL"
        and wire["to"] == "3"
        and wire["toPin"] == "model",
    )
    check("and it kept what travels through it", wire.get("type") == "MODEL")

    sampler = nodes["3"]
    check("a sampler is a flow node", sampler["role"] == "flow")
    check(
        "its widgets got the labels the class is known to use",
        sampler["fields"][0]["label"] == "seed"
        and sampler["fields"][0]["value"] == "812734"
        and sampler["fields"][2]["label"] == "steps",
    )
    check("and being bypassed is said out loud", sampler.get("badges") == ["muted"])

    unknown = nodes["9"]
    check(
        "a class nobody has heard of gets numbered values, not guessed ones",
        [field["label"] for field in unknown["fields"]] == ["#1", "#2"],
    )
    check("a loader is an input", nodes["1"]["role"] == "input")
    check("a group came through with its box", len(body["groups"]) == 1)

    # The cap, and the count that goes with it.
    small, cut = comfyui.read(WORKFLOW, 2)
    check("past the cap the graph is cut", len(small["nodes"]) == 2 and cut == 1)

    print(json.dumps(body["nodes"][0], indent=2))


if __name__ == "__main__":
    main()
