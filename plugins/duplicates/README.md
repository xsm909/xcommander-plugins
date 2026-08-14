# Find duplicates

The same file, kept in more than one place. Open it from Tools and it searches
the folder the panel is standing in, everything below it included.

Each set is one heading and the copies under it: how many there are, how big
each one is, and **what the copies past the first are costing**. Biggest first,
because that is the order anybody actually wants them in.

## How it looks

**Size first, then bytes.** Two files of different lengths cannot be the same
file, and the listing already knows their lengths — so nothing is opened for
them at all. Only where several files are the same size is anything read, and
then the first block of each before the whole of it: one block that differs
settles a gigabyte in microseconds.

That order is the whole design. A tool that hashes everything it can see is a
tool that reads a whole disk to answer a question most of the disk was never a
candidate for.

Anything under a kilobyte is left out. A hundred empty files are identical and
nobody wants them listed.

It looks on a disk and not over a connection: proving that a file on an FTP
server is a copy of one here means reading every byte of it, and that is a use
of somebody's line they did not ask for.

## What you can do with a row

| | |
| --- | --- |
| Enter | go to that copy — the panel beside this one opens its folder with the cursor on it |
| the other button | the same, and **delete this copy** |

Deleting goes through the application, which asks in its own words and uses the
recycle bin. A duplicate is still somebody's file: nothing here destroys one
quietly. What is deleted is what is picked out with Insert, or the row under the
cursor when nothing is.

The search runs again by itself after anything is deleted, so what is left on
screen is what is on the disk.

## While it is looking

It draws what it has found as it goes, and the line along the bottom says how
far in it is. A big tree is a big walk; the sets it has already found are real
and can be acted on before it finishes.
