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

"""Find duplicates — the same file, kept in more than one place.

**Size first, then bytes.** Two files of different lengths cannot be the same
file, and that is answered by the listing itself: no reading, no hashing, no
waiting. Only where several files are the same length is anything opened, and
then the cheap end of each is read before the whole of it — a first block that
differs settles the question for a gigabyte in a few microseconds.

That order is the whole design. A tool that hashes everything it can see is a
tool that reads a disk to answer a question most of the disk was never a
candidate for.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from xcommander import (
    Plugin,
    cell,
    column,
    delete,
    local_path as sdk_local_path,
    navigate,
    respond,
    row,
    table,
    text,
)

VIEW_ID = "duplicates.find"

plugin = Plugin("org.xcommander.duplicates", "Find duplicates")

#: How often a scan that is still running redraws what it has found.
PUSH_SECONDS = 0.6

#: How much of a file is read before the whole of it. A block this size settles
#: nearly every pair that is not a real duplicate, and costs one seek.
HEAD_BYTES = 64 * 1024

#: Files smaller than this are not worth reporting: a hundred empty `__init__`
#: files are identical and nobody wants them listed.
LEAST_BYTES = 1024


try:  # noqa: SIM105 - the host's own, where the host is new enough to have it
    from xcommander import file_url
except ImportError:  # pragma: no cover - a host older than 1.0.0.301
    # **A compatibility shim, not a second implementation.** `file_url` arrived
    # in the SDK on 2026-08-18 with the bug it fixes; a plugin is installed
    # separately from the application, so this one can find itself running on a
    # host that predates it, and an ImportError at the top of the file is a
    # plugin that does not load at all. Delete this once no host without it is
    # in use.
    from urllib.parse import quote as _quote

    def file_url(path: str) -> str:
        if not path:
            return "file:///"
        if path.startswith("\\\\"):
            rest = path[2:].replace("\\", "/")
            server, _, inner = rest.partition("/")
            return "file://" + _quote(server) + "/" + _quote(inner, safe="/:")
        forward = path.replace("\\", "/")
        if not forward.startswith("/"):
            forward = "/" + forward
        return "file://" + _quote(forward, safe="/:")


def local_path(url: Optional[str]) -> Optional[str]:
    """The folder behind a `file:` url, or None for anything else.

    Duplicates are found by reading bytes, and reading every byte of a file
    over FTP to prove it is a copy of one on the disk is a use of somebody's
    connection they did not ask for.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "":
        return url
    return sdk_local_path(url)


def url_of(path: str) -> str:
    """Where the host is pointed at one of these files.

    `file_url`, never `"file://" + path`: on Windows that is one slash short,
    and the slash it is short of is what keeps the drive letter out of the
    URL's host — so `E:/Work/x` arrives as host `E:` and path `/Work/x`, and
    the drive is gone. Found in the git plugin on 2026-08-18 and the same line
    was here.
    """
    return file_url(path)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%d %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TB" % size


class Group:
    """One set of files that are the same file."""

    __slots__ = ("size", "paths")

    def __init__(self, size: int, paths: List[str]) -> None:
        self.size = size
        self.paths = paths

    @property
    def wasted(self) -> int:
        """What the copies past the first are costing."""
        return self.size * (len(self.paths) - 1)


class Scan:
    """What one search has found, and whether it is still looking."""

    __slots__ = ("root", "generation", "groups", "looking", "read", "seen",
                 "unreadable", "watchers", "_lock")

    def __init__(self, root: str) -> None:
        self.root = root
        self.generation = 0
        self.groups: List[Group] = []
        self.looking = True
        #: How many files have been opened, for the line along the bottom.
        self.read = 0
        self.seen = 0
        self.unreadable = 0
        self.watchers: Dict[str, str] = {}
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock


#: One search per folder, so two views of the same folder share the work.
_scans: Dict[str, Scan] = {}

#: Which folder each open copy of the view is looking at.
_where: Dict[str, str] = {}

#: The rows each session last drew, so a press can say which file it was.
_rows: Dict[str, List[Optional[str]]] = {}


def _walk(root: str) -> Dict[int, List[str]]:
    """Every file under [root], filed by its size."""
    by_size: Dict[int, List[str]] = {}
    for folder, _, names in os.walk(root, onerror=lambda _: None):
        for name in names:
            full = os.path.join(folder, name)
            try:
                if os.path.islink(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < LEAST_BYTES:
                continue
            by_size.setdefault(size, []).append(full)
    return by_size


def _digest(path: str, length: int) -> Optional[str]:
    """The first [length] bytes of a file, hashed. None when it cannot be read."""
    reader = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as handle:
            while length > 0:
                block = handle.read(min(length, 1024 * 1024))
                if not block:
                    break
                reader.update(block)
                length -= len(block)
    except OSError:
        return None
    return reader.hexdigest()


def _search(scan: Scan, generation: int) -> None:
    """Walks the tree, then reads only what could possibly be a duplicate."""
    by_size = _walk(scan.root)

    with scan.lock:
        if scan.generation != generation:
            return
        scan.seen = sum(len(paths) for paths in by_size.values())

    # Biggest first: the sets worth knowing about are the ones taking room, and
    # a search that is stopped halfway has then found the ones that matter.
    candidates = sorted(
        ((size, paths) for size, paths in by_size.items() if len(paths) > 1),
        key=lambda pair: pair[0],
        reverse=True,
    )

    last_push = 0.0
    for size, paths in candidates:
        if scan.generation != generation:
            return

        # The cheap end first. Everything of one size whose first block differs
        # is settled without the rest of the file being touched at all.
        heads: Dict[str, List[str]] = {}
        for path in paths:
            mark = _digest(path, min(size, HEAD_BYTES))
            with scan.lock:
                # Counted once per *file*, not once per read: the second pass
                # opens some of them again, and "8 files read of 6" is a
                # sentence that makes a reader distrust the rest of the line.
                scan.read += 1
                if mark is None:
                    scan.unreadable += 1
            if mark is not None:
                heads.setdefault(mark, []).append(path)

        for head, same_head in heads.items():
            if len(same_head) < 2:
                continue
            # Small enough that the head *was* the whole file: nothing left to
            # read, and nothing to prove.
            if size <= HEAD_BYTES:
                _found(scan, generation, Group(size, sorted(same_head)))
                continue

            whole: Dict[str, List[str]] = {}
            for path in same_head:
                mark = _digest(path, size)
                if mark is not None:
                    whole.setdefault(mark, []).append(path)
            for _, same in whole.items():
                if len(same) > 1:
                    _found(scan, generation, Group(size, sorted(same)))

        now = time.monotonic()
        if now - last_push >= PUSH_SECONDS:
            last_push = now
            _push(scan, generation)

    with scan.lock:
        if scan.generation != generation:
            return
        scan.looking = False
    _push(scan, generation)


def _found(scan: Scan, generation: int, group: Group) -> None:
    with scan.lock:
        if scan.generation != generation:
            return
        scan.groups.append(group)


def _start(scan: Scan) -> None:
    with scan.lock:
        scan.generation += 1
        generation = scan.generation
        scan.groups = []
        scan.looking = True
        scan.read = 0
        scan.seen = 0
        scan.unreadable = 0

    threading.Thread(
        target=_search, args=(scan, generation), daemon=True
    ).start()


def _scan_for(folder: str) -> Scan:
    scan = _scans.get(folder)
    if scan is None:
        scan = Scan(folder)
        _scans[folder] = scan
        _start(scan)
    return scan


def _content(scan: Scan, session: str) -> dict:
    """Every set found, biggest first, with its copies under it."""
    with scan.lock:
        groups = list(scan.groups)
        looking = scan.looking

    if not groups:
        return text(
            "Looking…" if looking
            else "Nothing here is kept twice — of the files worth counting, "
                 "which is everything from a kilobyte up."
        )

    rows: List[dict] = []
    subjects: List[Optional[str]] = []
    for group in groups:
        rows.append(
            row(
                [
                    cell(""),
                    cell("%d copies · %s each · %s wasted"
                         % (len(group.paths), human(group.size),
                            human(group.wasted))),
                ],
                role="strong",
            )
        )
        subjects.append(None)
        for path in group.paths:
            # Relative to what was searched, because the folder is named in
            # the title above and a column of absolute paths is a column of
            # the same prefix repeated.
            here = os.path.relpath(path, scan.root)
            rows.append(row([cell("", icon="copy"), here]))
            subjects.append(path)

    _rows[session] = subjects
    return table(
        [
            column("", kind="icon"),
            # A path, so the host writes it short and answers the mouse with
            # the whole of it — a list of duplicates is a list of paths that
            # differ only somewhere in the middle.
            column("File", flex=1, kind="path"),
        ],
        rows,
    )


def _status(scan: Scan) -> str:
    with scan.lock:
        groups = list(scan.groups)
        looking = scan.looking
        read = scan.read
        seen = scan.seen
        unreadable = scan.unreadable

    wasted = sum(group.wasted for group in groups)
    said = "%d set%s, %s wasted" % (
        len(groups), "" if len(groups) == 1 else "s", human(wasted)
    )
    if looking:
        return "%s — still looking, %d of %d read" % (said, read, seen)
    return "%s — %d file%s read of %d%s" % (
        said,
        read,
        "" if read == 1 else "s",
        seen,
        ", %d could not be read" % unreadable if unreadable else "",
    )


def _draw(scan: Scan, session: str) -> dict:
    return respond(
        content=_content(scan, session),
        title="Duplicates in %s" % (os.path.basename(scan.root.rstrip("/\\"))
                                    or scan.root),
        status=_status(scan),
        trail=[],
        commands=[
            {"id": "again", "label": "Look again", "icon": "refresh",
             "tooltip": "Look again"},
        ],
        menus=[
            {
                "label": "Duplicates",
                "accelerator": "d",
                "items": [
                    {"id": "again", "label": "Look again", "shortcut": "F5"},
                ],
            },
        ],
    )


def _push(scan: Scan, generation: int) -> None:
    """Redraws every open copy, without any of them having asked."""
    if scan.generation != generation:
        return
    for session in list(scan.watchers):
        answer = _draw(scan, session)
        plugin.update_view(
            VIEW_ID,
            session,
            content=answer.get("content"),
            title=answer.get("title"),
            status=answer.get("status"),
        )


@plugin.view(VIEW_ID, "Find duplicates",
             "The same file kept in more than one place, under this folder.")
def duplicates(context, event) -> dict:
    if event.kind == "open":
        folder = local_path(context.url)
        if folder is None:
            return respond(
                content=text("Duplicates are found by reading bytes, so this "
                             "looks on a disk rather than over a connection."),
                title="Find duplicates",
                status="",
            )
        if not os.path.isdir(folder):
            folder = os.path.dirname(folder)

        _where[context.session] = folder
        scan = _scan_for(folder)
        scan.watchers[context.session] = context.session
        return _draw(scan, context.session)

    folder = _where.get(context.session)
    scan = _scans.get(folder or "")
    if scan is None:
        return respond()

    if event.kind == "button" and event.id == "again":
        _start(scan)
        return _draw(scan, context.session)

    if event.kind in ("activate", "mark"):
        at_row = event.row
        subjects = _rows.get(context.session) or []
        if at_row is None or at_row < 0 or at_row >= len(subjects):
            return respond()
        path = subjects[at_row]
        if path is None:
            return respond()

        if event.kind == "activate":
            # The folder it is in, with the cursor on it: a duplicate is
            # something you go and look at before you decide which copy stays.
            folder_of, _, name = path.rpartition(os.sep)
            answer = _draw(scan, context.session)
            answer["actions"] = [navigate(url_of(folder_of), name=name)]
            answer["status"] = "%s — in the panel beside this one" % name
            return answer

        chosen = [subjects[one] for one in event.marked
                  if one < len(subjects) and subjects[one] is not None]
        if path not in chosen:
            chosen = [path]
        return respond(context_menu=[
            {
                "id": "go\x1f" + path,
                "label": "Go to it",
            },
            {},
            {
                "id": "drop\x1f" + "\x1e".join(chosen),
                "label": "Delete %s" % (
                    "these %d copies" % len(chosen) if len(chosen) > 1
                    else "this copy"
                ),
            },
        ])

    if event.kind == "button" and event.id and "\x1f" in event.id:
        kind, _, subject = event.id.partition("\x1f")
        if kind == "go":
            folder_of, _, name = subject.rpartition(os.sep)
            answer = _draw(scan, context.session)
            answer["actions"] = [navigate(url_of(folder_of), name=name)]
            return answer
        if kind == "drop":
            paths = [one for one in subject.split("\x1e") if one]
            # The host deletes: it asks in the application's own words and uses
            # the recycle bin. **A duplicate is still somebody's file**, and a
            # tool that quietly destroys one is a tool nobody should run twice.
            return respond(actions=[delete([url_of(one) for one in paths])])

    if event.kind == "deleted":
        _start(scan)
        answer = _draw(scan, context.session)
        if event.urls:
            answer["status"] = "%d copy deleted, looking again." % len(event.urls) \
                if len(event.urls) == 1 \
                else "%d copies deleted, looking again." % len(event.urls)
        return answer

    if event.kind == "key" and event.key == "f5":
        _start(scan)
        return _draw(scan, context.session)

    return respond()


@plugin.on_view_closed
def closed(view_id: str, session: str) -> None:
    folder = _where.pop(session, None)
    _rows.pop(session, None)
    scan = _scans.get(folder or "")
    if scan is not None:
        scan.watchers.pop(session, None)


plugin.run()
