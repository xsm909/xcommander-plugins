# Git

The repository the panel is standing in — its log, what is not pushed yet, and
what each commit touched.

Open it from Tools and it shows wherever the panel is. It follows: walk into a
repository and it is showing that one, walk out and it says so. Full screen or
in a panel, and in a panel it watches the panel beside it.

## Walking it

| | |
| --- | --- |
| the log | every commit, with the shape of the history drawn beside it |
| the working tree | at the head of the log when there is one: what is changed, and what of it is staged |
| a commit | what it touched — added, changed, deleted, renamed |
| a file | what changed in it, as a unified diff |

A file that is staged and then changed again says both, because that is the one
state somebody has to be told about rather than left to work out from two
letters. An untracked file is shown against nothing, which is what adding it
would look like.

The trail at the top is the way back out, level by level, the same way it is in
a panel.

## The braid

The lines down the left say what came from where: a lane per line of
development, a mark on every commit, a ring on a merge, and a curve wherever
one lane joins another.

**The lanes are worked out here and drawn by the application.** Which lane a
commit belongs in is a question about this repository — what its parents are,
which branch tips are still open above it — and only something holding the
repository can answer it. How wide a lane is, how a line bends and what colour
any of it is are questions about a window and a palette, and this plugin cannot
see either.

It used to be git's own `--graph` drawing, pasted into a column as characters.
That has one lane's worth of charm and three problems: it is the wrong font, it
cannot be coloured, and it prints rows that are *only* the picture, which had to
be kept as empty rows or the lines came apart. A row is a commit now.

The order is `--topo-order` whenever the braid is drawn, because by date a
parent committed after its child comes out above it — which happens the moment
anyone works on two branches in an afternoon — and then a line has nowhere to go.

A commit no remote has yet is drawn in the accent colour.

**Branches, tags and remotes** live in the pill where the path bar would be,
freshest first and the one you are on ticked. Pick one and the log is redrawn at
it — nothing is checked out, nothing moves. To open its files, go to it and
press the hourglass.

There is no list down the side any more: it said what the pill says and spent a
fifth of the window saying it.

## A commit is a folder

Press the **hourglass** in the bar — or the other button on a commit, or the
menu — and *this* panel stands inside that commit. It is an ordinary panel from
there: F3 reads a file as it was, F5 copies it out, Tab completes names in it.
The tree as it was, next to the tree as it is in the panel beside it, which is
the thing a two-panel file manager is for.

One way in, not two. There used to be a second row offering the same thing in
the other panel, which is the panel you are already looking at.

Read-only, and not by omission: history is what has happened, and a panel that
offered to write into it would be offering something git itself does not do.

## What is put aside

Stash in the bar takes everything changed here — new files included — puts it on
the stash and leaves the working tree clean. **Git → What is put aside** opens
the list of what is on there, on a page of its own over the log: each entry with
the branch it was made on and when, and underneath it the files it holds and the
difference of the file under the cursor. Escape comes back to the log.

Enter on an entry brings it back and takes it off the stash; the other button
offers that, bringing it back and leaving it there, and discarding it. Each of
the three asks first, and a pop git cannot land — because what is in hand would
be written over — comes back with git's own words about which file it was.

A stash is not in the history and takes no place in the braid, so the log does
not show them. What is on the stash is counted in the status line instead.

## What it will not do

**It does not touch the network until you press something.** Whether a commit is
pushed is answered from what the last fetch left on the disk: a tool that opens a
connection because you walked into a folder is a tool that hangs on a folder you
opened by accident. Fetch, pull and push are buttons, and they are the only
things here that leave the machine.

What it does write, it asks about first — staging, unstaging, discarding, the
stash, and switching to the branch being shown. A dirty tree refuses a switch
rather than negotiating one: that is where somebody else's merge conflict comes
from, and the tool that offered it is the one that gets blamed.

## The mark

`icon.png` is the Git logo by [Jason Long](https://git-scm.com/community/logos),
used under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). It is his
work, not ours, and it travels with this plugin under his licence.
