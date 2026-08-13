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

"""Compare folders — the left panel against the right one.

The two-panel application already has the two sides; this says what is on one
and not the other, and what is on both but not the same. Nothing is written:
the answer is a list, and moving files is a separate decision that the host has
not been asked to allow yet.

The walk runs on a thread and pushes what it has every half second, because a
pair of trees takes longer than any sensible call timeout and a tool that shows
nothing for a minute reads as a tool that has hung.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import difflib

from xcommander import Plugin, notice, respond, table, text

VIEW_ID = "compare.folders"

plugin = Plugin("org.xcommander.compare", "Compare folders")

#: What a row is. The glyph is the whole of the answer at a glance.
ONLY_LEFT = "<"
ONLY_RIGHT = ">"
DIFFERENT = "≠"
SAME = "="

#: The same four, in words, for the line along the bottom.
_states = {
    ONLY_LEFT: "only on the left",
    ONLY_RIGHT: "only on the right",
    DIFFERENT: "different",
    SAME: "the same",
}


class Entry:
    """One file, on one side."""

    __slots__ = ("size", "mtime")

    def __init__(self, size: int, mtime: float) -> None:
        self.size = size
        self.mtime = mtime


class Comparison:
    """One run of the walk, and the answer as it fills in.

    Held per session, so a click does not start it again — the whole point of
    a session outliving a call.
    """

    __slots__ = (
        "left",
        "right",
        "rows",
        "done",
        "stopped",
        "abandoned",
        "scanned",
        "compared",
        "by_content",
        "showing",
        "lock",
    )

    def __init__(self, left: str, right: str, by_content: bool) -> None:
        self.left = left
        self.right = right
        self.rows: List[Tuple[str, str, str, str]] = []
        self.done = False
        self.stopped = False
        #: Set when a later run has taken over. The thread checks it and
        #: leaves, and its pushes are dropped — without this a slow walk goes
        #: on drawing over the answer that replaced it, which is exactly what
        #: happens when somebody turns content comparison on halfway through.
        self.abandoned = False
        self.scanned = 0
        #: How many pairs were opened and read, which is the whole difference
        #: between the two ways of comparing and has to be visible.
        self.compared = 0
        self.by_content = by_content
        #: The file whose difference is on screen instead of the list, if any.
        #: A walk that is still running keeps filling the rows behind it and
        #: says nothing, rather than snatching the diff away mid-read.
        self.showing: Optional[str] = None
        self.lock = threading.Lock()


# The state each open copy of the view is holding.
_runs: Dict[str, Comparison] = {}
_options: Dict[str, Dict[str, object]] = {}


# -- reading a side -----------------------------------------------------------


def local_path(url: Optional[str]) -> Optional[str]:
    """The file system path behind a `file:` url, or None for anything else.

    Only local folders are walked directly. Everything else — an archive, a
    share, whatever a plugin serves — goes through the host, which is the only
    thing that knows how to read it.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("", "file"):
        return None
    path = unquote(parsed.path)
    # A Windows url is `file:///C:/x`, and the leading slash is not part of it.
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def is_hidden(name: str) -> bool:
    """A dotfile. Not the whole truth on Windows, where hidden is an attribute
    rather than a name — but the host says so for the entries it lists, and
    this only stands in for the local walk."""
    return name.startswith(".")


def walk(root: Optional[str], url: Optional[str], recursive: bool, limit: int,
         hidden: bool, run: Comparison) -> Dict[str, Entry]:
    """Every file under one side, keyed by its path relative to the root.

    Directories are not rows of their own. A folder that exists on one side
    only shows up as the files inside it, which is what somebody comparing two
    trees is actually asking about — an empty folder on one side is not a
    difference worth a line.
    """
    found: Dict[str, Entry] = {}

    if root is not None:
        for base, dirs, files in os.walk(root):
            if not recursive:
                dirs[:] = []
            elif not hidden:
                dirs[:] = [name for name in dirs if not is_hidden(name)]
            for name in files:
                if not hidden and is_hidden(name):
                    continue
                full = os.path.join(base, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                found[os.path.relpath(full, root).replace(os.sep, "/")] = Entry(
                    stat.st_size, stat.st_mtime
                )
                run.scanned += 1
                if len(found) >= limit:
                    run.stopped = True
                    return found
        return found

    # Through the host: slower, and the only way anything but a local disk is
    # ever read.
    pending = [("", url)]
    while pending:
        prefix, at = pending.pop()
        try:
            entries = plugin.list_dir(at)
        except Exception:
            continue
        for entry in entries:
            name = entry.get("name") or ""
            if not hidden and (entry.get("hidden") or is_hidden(name)):
                continue
            relative = "%s/%s" % (prefix, name) if prefix else name
            if entry.get("kind") == "dir":
                if recursive:
                    pending.append((relative, entry.get("url")))
                continue
            found[relative] = Entry(
                int(entry.get("size") or 0),
                float(entry.get("modified") or 0) / 1000.0,
            )
            run.scanned += 1
            if len(found) >= limit:
                run.stopped = True
                return found
    return found


BLOCK = 1 << 20


def same_content(left: Tuple[Optional[str], str], right: Tuple[Optional[str], str],
                 size: int) -> bool:
    """Whether two files of equal size hold the same bytes.

    Each side is `(local path or None, url)`. Two local files are read
    directly, which on a big folder is the difference between a tool you use
    and one you start and walk away from; anything else goes through the host,
    which is the only thing that can read it at all.

    Compared block by block as they arrive, so two files that differ in their
    first kilobyte cost a kilobyte rather than two whole files.
    """
    if left[0] is not None and right[0] is not None:
        try:
            with open(left[0], "rb") as a, open(right[0], "rb") as b:
                while True:
                    here = a.read(BLOCK)
                    there = b.read(BLOCK)
                    if here != there:
                        return False
                    if not here:
                        return True
        except OSError:
            # Unreadable is not the same as different, but it is not the same
            # as identical either, and saying "different" is the answer that
            # makes somebody look.
            return False

    offset = 0
    while offset < size:
        want = min(BLOCK, size - offset)
        try:
            here = plugin.read_file(left[1], max_bytes=want, offset=offset)
            there = plugin.read_file(right[1], max_bytes=want, offset=offset)
        except Exception:
            return False
        if here != there:
            return False
        if not here:
            break
        offset += len(here)
    return True


# -- the walk itself ----------------------------------------------------------


def local_file(root: Optional[str], relative: str) -> Optional[str]:
    """The file on disk behind a relative path, when the side is a local one."""
    if root is None:
        return None
    return os.path.join(root, *relative.split("/"))


def child_url(root: str, relative: str) -> str:
    return "%s/%s" % (root.rstrip("/"), "/".join(
        part for part in relative.split("/") if part
    ))


def compare(session: str, run: Comparison, options: Dict[str, object]) -> None:
    """Walks both sides and fills in the rows, pushing as it goes."""
    recursive = bool(options.get("recursive", True))
    show_same = bool(options.get("same", False))
    by_content = bool(options.get("content", False))
    slack = float(options.get("seconds", 2))
    limit = int(options.get("limit", 50000))
    hidden = bool(options.get("hidden", True))

    left_root = local_path(run.left)
    right_root = local_path(run.right)

    left = walk(left_root, run.left, recursive, limit, hidden, run)
    if run.abandoned:
        return
    push(session, run)
    right = walk(right_root, run.right, recursive, limit, hidden, run)
    if run.abandoned:
        return

    last = time.monotonic()
    rows: List[Tuple[str, str, str, str]] = []

    for relative in sorted(set(left) | set(right)):
        if run.abandoned:
            return

        here = left.get(relative)
        there = right.get(relative)

        if here is None:
            state = ONLY_RIGHT
        elif there is None:
            state = ONLY_LEFT
        elif here.size != there.size:
            # Size settles it before anything is read. Two files of different
            # lengths cannot hold the same bytes, and reading them to find
            # that out would be the slowest way to learn nothing.
            state = DIFFERENT
        elif by_content:
            run.compared += 1
            state = SAME if same_content(
                (local_file(left_root, relative), child_url(run.left, relative)),
                (local_file(right_root, relative), child_url(run.right, relative)),
                here.size,
            ) else DIFFERENT
        elif abs(here.mtime - there.mtime) > slack:
            state = DIFFERENT
        else:
            state = SAME

        if state == SAME and not show_same:
            continue

        rows.append((
            state,
            relative,
            describe(here),
            describe(there),
        ))

        # Half a second, the same beat the disk map pushes on: often enough to
        # look alive, rarely enough not to spend the walk drawing.
        if time.monotonic() - last > 0.5:
            with run.lock:
                run.rows = list(rows)
            push(session, run)
            last = time.monotonic()

    with run.lock:
        run.rows = rows
        run.done = True
    push(session, run)


# -- what is different about one file ----------------------------------------

#: Past this a side is not read for a diff. Two files this size are not being
#: read line by line by anybody.
MAX_DIFF = 4 << 20


def read_side(local: Optional[str], url: str) -> Optional[bytes]:
    """One side of a pair, or None when it is not there to be read."""
    if local is not None:
        try:
            with open(local, "rb") as handle:
                return handle.read(MAX_DIFF + 1)
        except OSError:
            return None
    try:
        return plugin.read_file(url, max_bytes=MAX_DIFF + 1)
    except Exception:
        return None


def decode_text(data: bytes) -> Optional[str]:
    """Bytes as lines of text, or None when they are not text at all.

    The same rules the application uses when it draws a file: a mark decides,
    then the shape of the bytes, and every kind of line ending is a line
    ending. A diff of two files read as the wrong alphabet is a diff in which
    every line has changed, which is worse than saying nothing.
    """
    # Plain ASCII line endings in front of two-byte text, which is what
    # appending to a UTF-16 file from a shell leaves behind. The host reads
    # them the same way; a diff whose two sides were decoded by different
    # rules would show differences that are not in the files.
    prefix = 0
    while prefix + 1 < len(data) and data[prefix] in (0x0D, 0x0A) and data[prefix + 1]:
        prefix += 1
    if prefix:
        rest = decode_text(data[prefix:])
        if rest is None:
            return None
        return data[:prefix].decode("ascii", "replace").replace(
            "\r\n", "\n"
        ).replace("\r", "\n") + rest

    if data.startswith(b"\xff\xfe"):
        text = data[2:].decode("utf-16-le", "replace")
    elif data.startswith(b"\xfe\xff"):
        text = data[2:].decode("utf-16-be", "replace")
    else:
        sample = data[:512]
        nuls = sample.count(0)
        if nuls * 4 > len(sample):
            evens = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
            odds = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i + 1] == 0)
            pairs = len(sample) // 2
            if odds > pairs / 2 and odds > evens:
                text = data.decode("utf-16-le", "replace")
            elif evens > pairs / 2 and evens > odds:
                text = data.decode("utf-16-be", "replace")
            else:
                # Damaged: the pairing changes side partway through. Everything
                # that is not a hole is still text.
                text = bytes(b for b in data if b).decode("utf-8", "replace")
        elif 0 in data:
            return None  # Binary, and nobody wants a diff of that.
        else:
            text = data.decode("utf-8-sig", "replace")

    return text.replace("\r\n", "\n").replace("\r", "\n")


def diff_of(run: Comparison, relative: str) -> dict:
    """The two sides of one file, as `git diff` would print them."""
    left_bytes = read_side(
        local_file(local_path(run.left), relative), child_url(run.left, relative)
    )
    right_bytes = read_side(
        local_file(local_path(run.right), relative), child_url(run.right, relative)
    )

    if left_bytes is None or right_bytes is None:
        return text("This file is only on one side, so there is nothing to compare.")

    if len(left_bytes) > MAX_DIFF or len(right_bytes) > MAX_DIFF:
        return text("Too big to compare line by line.")

    left_text = decode_text(left_bytes)
    right_text = decode_text(right_bytes)
    if left_text is None or right_text is None:
        return text(
            "Binary files differ."
            if left_bytes != right_bytes
            else "Binary files are the same."
        )

    lines = list(
        difflib.unified_diff(
            left_text.splitlines(keepends=True),
            right_text.splitlines(keepends=True),
            fromfile="left/%s" % relative,
            tofile="right/%s" % relative,
            n=3,
        )
    )
    if not lines:
        return text("The two are the same, line for line.", language="diff")

    # A file that does not end in a newline gets git's own remark rather than a
    # diff whose last two lines run together.
    body = "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )
    return text(body, language="diff")


def describe(entry: Optional[Entry]) -> str:
    if entry is None:
        return ""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.mtime))
    return "%s  %s" % (human(entry.size), stamp)


def human(size: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024.0
    return str(size)


def push(session: str, run: Comparison) -> None:
    # A walk that has been taken over says nothing more: its answer is to a
    # question nobody is asking any more.
    if run.abandoned or run.showing is not None:
        return
    plugin.update_view(
        VIEW_ID,
        session,
        content=content_of(run),
        status=status_of(run),
    )


# -- what it draws ------------------------------------------------------------


def content_of(run: Comparison) -> dict:
    with run.lock:
        rows = list(run.rows)
    return table(
        ["", "Name", "Left", "Right"],
        [list(row) for row in rows],
    )


def status_of(run: Comparison) -> str:
    # Counted under the lock and in one pass: the walk is adding rows while
    # this runs, and three separate passes would each see a different list.
    with run.lock:
        rows = len(run.rows)
        done = run.done
        only_left = 0
        only_right = 0
        differ = 0
        for row in run.rows:
            if row[0] == ONLY_LEFT:
                only_left += 1
            elif row[0] == ONLY_RIGHT:
                only_right += 1
            elif row[0] == DIFFERENT:
                differ += 1

    # **What it compared by, always.** Without this the answer is the same
    # sentence whichever way the question was asked, and turning content
    # comparison on looks like a switch that does nothing — which is exactly
    # how it was reported, on two folders where the two answers happen to
    # agree.
    how = "by content" if run.by_content else "by size and time"

    if not done:
        return "Walking %s… %d read, %d listed" % (how, run.scanned, rows)

    read = ""
    if run.by_content:
        read = ", %d pair%s read through" % (
            run.compared,
            "" if run.compared == 1 else "s",
        )

    if run.stopped:
        return "Stopped at the limit — %d rows, %s. %d only left, %d only right, %d differ" % (
            rows, how, only_left, only_right, differ
        )
    if rows == 0:
        return "The two sides are the same, %s%s" % (how, read)
    return "%d only left, %d only right, %d differ — %s%s" % (
        only_left, only_right, differ, how, read
    )


def menus_of(options: Dict[str, object]) -> List[dict]:
    """The view's own menu, which it owns while it is full screen."""
    return [
        {
            "label": "Compare",
            "accelerator": "c",
            "items": [
                {"id": "rescan", "label": "Compare again", "shortcut": "F5"},
                {},
                {
                    "id": "toggle.recursive",
                    "label": "Walk subfolders",
                    "checked": bool(options.get("recursive", True)),
                },
                {
                    "id": "toggle.same",
                    "label": "Show what matches",
                    "checked": bool(options.get("same", False)),
                },
                {
                    "id": "toggle.content",
                    "label": "Compare by content",
                    "checked": bool(options.get("content", False)),
                },
                {
                    "id": "toggle.hidden",
                    "label": "Include hidden files",
                    "checked": bool(options.get("hidden", True)),
                },
            ],
        },
    ]


def start(context, options: Optional[Dict[str, object]] = None) -> dict:
    """Opens a comparison of the two panels, and starts walking them.

    [options] is what the menu has been toggled to, or the plugin's settings on
    the first open. Passed in rather than read here, because a toggle changes
    what counts as a difference and the walk has to be started with the new
    answer rather than with the saved one.
    """
    left = context.url
    right = context.other_url

    if not left or not right:
        return respond(
            content=table(["", "Name", "Left", "Right"], []),
            title="Compare folders",
            status="This needs a folder on both sides.",
        )

    if options is None:
        options = dict(plugin.settings)
    _options[context.session] = options

    # Whatever was walking for this session is told to stop before the new one
    # starts. Two walks pushing at one view is an answer that flickers between
    # two questions.
    previous = _runs.get(context.session)
    if previous is not None:
        previous.abandoned = True

    run = Comparison(left, right, bool(options.get("content", False)))
    _runs[context.session] = run

    thread = threading.Thread(
        target=compare, args=(context.session, run, options), daemon=True
    )
    thread.start()

    return respond(
        content=content_of(run),
        title="Compare folders",
        trail=[name_of(left), "against", name_of(right)],
        status=status_of(run),
        menus=menus_of(options),
        commands=[{"id": "rescan", "label": "Compare again", "icon": "refresh"}],
    )


def name_of(url: str) -> str:
    path = local_path(url) or unquote(urlparse(url).path)
    return os.path.basename(path.rstrip("/\\")) or path


@plugin.view(VIEW_ID, "Compare folders", "The left panel against the right one.")
def folders(context, event) -> dict:
    if event.kind == "open":
        return start(context)

    if event.kind == "activate":
        run = _runs.get(context.session)
        if run is None or event.row is None or event.row < 0:
            return respond()
        with run.lock:
            rows = list(run.rows)
        if event.row >= len(rows):
            return respond()

        state, relative, _, _ = rows[event.row]
        run.showing = relative
        return respond(
            content=diff_of(run, relative),
            # The way back is the trail's, because that is the way back
            # everywhere else in this application.
            trail=[name_of(run.left), "against", name_of(run.right), relative],
            status="%s — %s" % (relative, _states.get(state, "")),
        )

    if event.kind == "step":
        # Any level above the file is the list again.
        run = _runs.get(context.session)
        if run is None:
            return respond()
        run.showing = None
        return respond(
            content=content_of(run),
            trail=[name_of(run.left), "against", name_of(run.right)],
            status=status_of(run),
        )

    if event.kind == "button":
        options = _options.get(context.session, dict(plugin.settings))

        if event.id == "rescan":
            return start(context)

        if event.id and event.id.startswith("toggle."):
            key = event.id.split(".", 1)[1]
            options = dict(options)
            options[key] = not bool(options.get(key))
            # Changing what counts as a difference changes the answer, so the
            # walk runs again rather than filtering whatever it happened to
            # keep. The menu comes back with the box in its new state.
            return start(context, options)

        return respond(actions=[notice("Nothing to do.")])

    return respond()


@plugin.on_view_closed
def closed(view_id: str, session: str) -> None:
    _runs.pop(session, None)
    _options.pop(session, None)


plugin.run()
