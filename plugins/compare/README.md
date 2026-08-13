# Compare folders

The left panel against the right one: what is only on one side, and what is on
both but not the same.

A two-panel file manager already has the two sides, so this asks for nothing —
open it from Tools and it compares wherever the panels happen to be standing.

## What a row says

| | |
| --- | --- |
| `<` | only on the left |
| `>` | only on the right |
| `≠` | on both, and different |
| `=` | on both and the same — hidden unless you ask for it |

Pressing a row shows **what is different about that file**, as a unified diff —
the thing `git diff` prints, coloured the way it colours it. The trail at the
top is the way back to the list, exactly as it is in a panel.

A file only one side has, one too big to read line by line, or one that is not
text says so instead. Binary files get git's own answer: they differ, or they
do not.

Folders are not rows of their own. A folder that exists on one side only turns
up as the files inside it, because an empty folder on one side is not a
difference anybody is looking for.

## What counts as different

Size first, always. Then, by default, the time — with two seconds of slack,
because a FAT volume keeps times to that and a file copied onto one comes back
looking a second older.

**Compare by content** reads both files and compares them byte for byte, for
the pairs whose size matches. It is the only answer that is certain, and it
reads every byte of both folders to get there. Blocks are compared as they
arrive, so two files that differ in their first kilobyte cost a kilobyte.

## What it does not do

**It does not move anything.** The answer is a list. Copying one side onto the
other is a separate decision, and the host has not been asked to allow it yet —
a plugin cannot write to your disk, and this one is not an exception.

## Where it reads from

Local folders are walked directly, which is much faster on a big disk.
Anything else — an archive, a share, whatever another plugin serves — is walked
through the application, so the same comparison works between a folder and a
zip without knowing the difference.

The walk runs on a thread and pushes what it has every half second, so a pair
of large trees fills in while you watch instead of arriving all at once after
a minute of nothing. It stops at 50 000 entries by default and says so.
