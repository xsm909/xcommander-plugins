# Disk map

What is taking up the room, drawn as a ring: the folder you opened it on in the
middle, everything inside it around, biggest first, three levels deep.

- **Press a wedge** — or a row of the legend — to walk into it. The panel beside
  it follows, unless you turn that off.
- **Press the middle** to come back out.
- **Right-click** (or long-press) a wedge to put it on the list to go. Marking a
  folder marks everything inside it — that is what deleting a folder means — so
  the list only ever holds the topmost of each branch. Right-clicking something
  that is going because its folder is going takes the folder back off the list,
  and **Clear marks** takes everything off at once.
- **Clean up** hands that list to the application, which asks you first and uses
  the recycle bin where there is one. Only what actually went is taken off the
  map, so saying no leaves the picture true.

It runs full screen, in a panel, or from the Alt+F1 / Alt+F2 location menu —
Settings → Plugins decides which, from the surfaces the view declares.

## The scan is remembered

Walking a disk costs minutes, so it is walked once and kept for as long as the
app is running. Opening the same folder again draws it at once; the status line
says how old the measurement is. It is walked afresh when it has never been
walked, when the cache is older than **Measure again after** (30 minutes by
default), and whenever you press **Rescan** — which also stops a walk that is
under way.

While a walk is running the ring fills in front of you, level by level, because
the plugin pushes what it has found every half second rather than waiting for
the whole answer. Breadth first on purpose: depth first would show one wedge
filling the whole circle for the first minute.

## Settings

| | |
| --- | --- |
| Rings | How deep the chart goes. A panel always draws one fewer. |
| Wedges per ring | Everything past this is gathered into one wedge. |
| Measure again after | Minutes before a remembered scan is walked again. |
| Count hidden files | They take up room whether or not the panel shows them. |
| Walk the other panel along | Opening a folder here sends the panel beside it there. |
| Read local folders directly | Faster on a big local disk. Off walks everything through the application, which is the only way anything but the local disk is ever read. |

## What it demonstrates

Everything the view API grew for it: the `chart` content shape, `mark` and
`button` events, `plugin.list_dir`, `plugin.update_view` for work that outlasts
a call, and the `delete` action — the one thing a plugin is deliberately not
allowed to do for itself.
