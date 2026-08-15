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

import comfyapi  # noqa: E402
import comfyui  # noqa: E402
import n8n  # noqa: E402
import parse  # noqa: E402

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


#: The real files he handed over on 2026-08-15, kept beside the reader that
#: reads them. They are the only honest test of a format: a fixture written by
#: hand tests what the author believed, not what an exporter writes.
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def real_files() -> None:
    # The last number is how many wires name a node the file does not contain.
    # The ComfyUI fixture carries one on purpose: dropping it is the host's job,
    # not the reader's, and the page says how many went.
    for name, reader, least, loose_expected in (
        ("comfyui-workflow.json", comfyui, 4, 1),
        ("comfyui-api-prompt.json", comfyapi, 7, 0),
        ("n8n-competitor-research.json", n8n, 20, 0),
        ("n8n-emails-to-notion.json", n8n, 10, 0),
    ):
        path = os.path.join(FIXTURES, name)
        if not os.path.exists(path):
            print("skip " + name)
            continue

        with open(path, encoding="utf-8") as handle:
            text = handle.read()

        # **The file as it really is.** Both n8n exports came off a web page
        # with the workflow's title repeated three times after the closing
        # brace; `json.loads` refuses the lot, and the graph would never open.
        document = parse.first_document(text)
        check("%s parses despite what is after it" % name, document is not None)
        check("%s is recognised" % name, reader.looks_like(document))

        body, dropped = reader.read(document, 5000)
        check(
            "%s came through: %d nodes, %d wires"
            % (name, len(body["nodes"]), len(body["links"])),
            len(body["nodes"]) >= least and dropped == 0,
        )

        # Every wire has a node at both ends, or the host drops it and the page
        # says so. A reader that leaks dangling wires is a reader with a bug.
        ids = {node["id"] for node in body["nodes"]}
        loose = [
            wire
            for wire in body["links"]
            if wire["from"] not in ids or wire["to"] not in ids
        ]
        check(
            "%s leaves exactly the loose wires the file has (%d)"
            % (name, loose_expected),
            len(loose) == loose_expected,
        )


def n8n_shape() -> None:
    workflow = {
        "nodes": [
            {
                "id": "1",
                "name": "When clicking",
                "type": "n8n-nodes-base.manualTrigger",
                "position": [0, 0],
                "parameters": {},
            },
            {
                "id": "2",
                "name": "Agent",
                "type": "@n8n/n8n-nodes-langchain.agent",
                "position": [200, 0],
                "parameters": {"prompt": "do the thing"},
            },
            {
                "id": "3",
                "name": "Sticky Note",
                "type": "n8n-nodes-base.stickyNote",
                "position": [-40, -60],
                "parameters": {"content": "## Try it", "width": 300, "height": 200},
            },
        ],
        "connections": {
            "When clicking": {
                "main": [[{"node": "Agent", "type": "main", "index": 0}]]
            },
        },
    }

    check("an n8n export is recognised", n8n.looks_like(workflow))
    body, _ = n8n.read(workflow, 5000)
    nodes = {node["id"]: node for node in body["nodes"]}

    check("a sticky note is a note, not a box", len(body["nodes"]) == 2)
    check("and it kept what was written on it", body["notes"][0]["text"].startswith("## Try it"))
    check("a trigger is an event", nodes["When clicking"]["role"] == "event")
    check(
        "connections are keyed by name, and that is what the ids are",
        body["links"][0]["from"] == "When clicking"
        and body["links"][0]["to"] == "Agent",
    )
    check(
        "the run order is a flow wire, not a data one",
        body["links"][0]["role"] == "flow",
    )
    check(
        "pins are worked out from the wires, because the file never says",
        nodes["Agent"]["inputs"][0]["id"] == "main",
    )
    check(
        "and a parameter worth reading is on the face of the node",
        nodes["Agent"]["fields"][0]["value"] == "do the thing",
    )


def api_shape() -> None:
    prompt = {
        "11": {"class_type": "CheckpointLoaderSimple",
               "inputs": {"ckpt_name": "sd_xl.safetensors"}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "a lighthouse", "clip": ["11", 1]}},
        "12": {"class_type": "KSampler",
               "_meta": {"title": "Sample it"},
               "inputs": {"seed": 812734, "steps": 20,
                          "model": ["11", 0], "positive": ["6", 0]}},
    }

    check("an API prompt is recognised", comfyapi.looks_like(prompt))
    check("a native workflow is not mistaken for one",
          not comfyapi.looks_like(WORKFLOW))
    check("and neither is a package.json",
          not comfyapi.looks_like({"name": "x", "version": "1"}))

    body, _ = comfyapi.read(prompt, 5000)
    nodes = {node["id"]: node for node in body["nodes"]}

    check("it asks the host to lay it out", body["layout"] == "layered")
    check("because there are no coordinates in it at all",
          all(node["x"] == 0 and node["y"] == 0 for node in body["nodes"]))

    # The whole of the parse: a two-element list is a wire, everything else is
    # a value to write on the face of the node.
    check("a list of [node, slot] became a wire", len(body["links"]) == 3)
    check("and a literal became a field",
          any(f["label"] == "seed" and f["value"] == "812734"
              for f in nodes["12"]["fields"]))
    check("a wire is not also written on the box",
          all(f["label"] not in ("model", "positive")
              for f in nodes["12"]["fields"]))
    check("the title comes from _meta when the file has one",
          nodes["12"]["title"] == "Sample it")
    check("and the class stays as the subtitle",
          nodes["12"]["subtitle"] == "KSampler")
    check("an output pin exists for every slot somebody joined to",
          [pin["id"] for pin in nodes["11"]["outputs"]] == ["out 0", "out 1"])


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

    n8n_shape()
    api_shape()
    real_files()

    print(json.dumps(body["nodes"][0], indent=2))


if __name__ == "__main__":
    main()
