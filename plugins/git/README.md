# Git

The repository the panel is standing in — its log, what is not pushed yet, and
what each commit touched.

Open it from Tools and it shows wherever the panel is. It follows: walk into a
repository and it is showing that one, walk out and it says so. Full screen or
in a panel, and in a panel it watches the panel beside it.

## Walking it

| | |
| --- | --- |
| the log | every commit, with git's own drawing of the shape of the history |
| the working tree | at the head of the log when there is one: what is changed, and what of it is staged |
| a commit | what it touched — added, changed, deleted, renamed |
| a file | what changed in it, as a unified diff |

A file that is staged and then changed again says both, because that is the one
state somebody has to be told about rather than left to work out from two
letters. An untracked file is shown against nothing, which is what adding it
would look like.

The trail at the top is the way back out, level by level, the same way it is in
a panel.

**The graph is git's own.** Working out which lane a commit belongs in is a
solved problem, solved badly by everyone who solves it again; `--graph` prints
the answer. It also prints lines that are nothing but the drawing — a merge
fanning out takes one — and those stay as rows with only the picture in them,
because dropping them breaks the lines they belong to.

`↑` marks a commit no remote has yet.

## What it will not do

**It does not write, and it does not touch the network.** Whether a commit is
pushed is answered from what the last fetch left on the disk: a tool that opens
a connection because you walked into a folder is a tool that hangs on a folder
you opened by accident.

Staging, committing, checking out — everything that changes a repository — is a
later segment, and it is waiting on something the application has to learn
first: how a plugin asks a question. Checking out a branch changes your working
tree, and doing that without asking is not a thing this will do.
