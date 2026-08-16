# Node graphs

Files that **are** a node graph, drawn as one. A ComfyUI workflow read as text
is four thousand lines of coordinates; read as what it is, it is a dozen boxes
with the prompt and the seed written on them and the wires between them.

| format | file | what it looks like here |
| --- | --- | --- |
| ComfyUI | `workflow.json` | the graph the editor draws, with the widget values on the boxes |
| ComfyUI | a generated **PNG** | the workflow written into the picture it made — F3 shows the picture, Shift+F3 the graph |
| ComfyUI | API export | a prompt with no coordinates; the application lays it out |
| n8n | workflow export | wires keyed by node name, sticky notes and all |
| Node-RED | `flows.json` | one canvas per tab, and `link out` drawn to the `link in` it names |
| LiteGraph | anything | ComfyUI *is* LiteGraph; a class nobody knows gets numbered values rather than guessed labels |
| Godot | `.tscn`, `.escn` | the scene tree and the signals across it, read down the page |

A workflow opens on **F3**: the reader is asked about the file itself before
the application decides what opens it, so a `.json` that really is a graph is
drawn and `package.json` is still text.

## What it is not

**Not an editor** — nothing here can move a node, rewire anything or save. Not
a runner: no ComfyUI or n8n instance is contacted and nothing is executed. Not
a diff, which is a good idea and a separate one.

## Adding a format

A reader is a module with three things in it: `signature(text)` for the first
pages of a file, `looks_like(document)` for a whole one, and `read(document,
max_nodes)` returning the nodes, links, groups and notes. **Claims have to be
exclusive**, and `selftest.py` holds that as a matrix over every fixture —
exactly one reader per file, and none at all for the documents that are not
graphs. A new reader adds a row to it.

Run the self-test with no application around it:

```
python3 selftest.py
```
