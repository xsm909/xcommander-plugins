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

**Branches, tags and remotes** are under Repository in the menu, freshest first;
the one you are on wears the same mark it wears in the log. Press one for its
log; press it with the other button to open its tree in the panel beside this
one.

## A commit is a folder

Press a commit with the **other button** — or ask for it from the menu — and the
panel beside this one stands inside that commit. It is an ordinary panel from
there: F3 reads a file as it was, F5 copies it out, Tab completes names in it.
The tree as it was, next to the tree as it is, which is the thing a two-panel
file manager is for.

Read-only, and not by omission: history is what has happened, and a panel that
offered to write into it would be offering something git itself does not do.

## What it will not do

**It does not touch the network.** Whether a commit is pushed is answered from
what the last fetch left on the disk: a tool that opens a connection because you
walked into a folder is a tool that hangs on a folder you opened by accident.

What it does write, it asks about first — staging, unstaging, and switching to
the branch being shown. A dirty tree refuses a switch rather than negotiating
one: that is where somebody else's merge conflict comes from, and the tool that
offered it is the one that gets blamed. Committing is still absent, because it
needs a line of typed text and a plugin can only be given a yes or a no.

## The mark

`icon.png` is the Git logo by [Jason Long](https://git-scm.com/community/logos),
used under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). It is his
work, not ours, and it travels with this plugin under his licence.
