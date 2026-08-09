"""Disk map — what is taking up the room, drawn as a ring.

A folder becomes a chart of everything inside it, biggest first, several levels
deep. Press a wedge to walk into it, press it with the right button to put it
on the list to go, and press Clean up to have the application delete the lot —
to the recycle bin, after asking, because a plugin does not get to delete
things on its own.

The scan runs on a thread and pushes what it has found every half second, so a
whole drive fills the ring in front of you instead of arriving all at once
after a minute of nothing.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from typing import Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

from xcommander import (
    Plugin,
    button,
    chart,
    delete,
    navigate,
    notice,
    open_viewer,
    respond,
    segment,
)

VIEW_ID = "diskmap.rings"

plugin = Plugin("org.xcommander.diskmap", "Disk map")


# -- what a scan builds -------------------------------------------------------


class Node:
    """One folder or file in the tree the scan is building."""

    __slots__ = ("name", "url", "size", "is_dir", "children", "parent")

    def __init__(self, name: str, url: str, is_dir: bool, parent: Optional["Node"]):
        self.name = name
        self.url = url
        self.is_dir = is_dir
        self.size = 0
        self.children: List[Node] = []
        self.parent = parent

    def grow(self, bytes_added: int) -> None:
        """Adds to this node and to everything it sits inside.

        Carrying the size up as each file is found — rather than summing at the
        end — is what lets the chart be drawn while the scan is still running.
        """
        node: Optional[Node] = self
        while node is not None:
            node.size += bytes_added
            node = node.parent

    def detach(self) -> None:
        """Takes this node out of the tree, shrinking its ancestors."""
        if self.parent is None:
            return
        try:
            self.parent.children.remove(self)
        except ValueError:
            return
        size, node = self.size, self.parent
        while node is not None:
            node.size -= size
            node = node.parent
        self.parent = None


class Scan:
    """The measured tree of one folder, kept for as long as the plugin runs.

    Walking a disk is minutes of work, so it is done once and remembered. Two
    panels looking at the same folder share this — they are looking at the same
    disk — while each keeps its own idea of where it is and what it has picked
    out.
    """

    def __init__(self, url: str):
        self.url = url
        self.lock = threading.RLock()
        self.root = Node(_name_of(url), url, True, None)
        self.files = 0
        self.unreadable = 0
        self.scanning = False
        #: When the walk finished, by the clock the staleness check uses. None
        #: while nothing has ever completed.
        self.finished_at: Optional[float] = None
        #: Bumped to tell a running walk that its answer is no longer wanted.
        self.generation = 0
        #: Sessions to redraw when something is found.
        self.watchers: Dict[str, "Session"] = {}

    def age(self) -> Optional[float]:
        """Seconds since the walk finished, or None if it never has."""
        if self.finished_at is None:
            return None
        return time.time() - self.finished_at


class Session:
    """One open copy of the view: where it is looking and what it has marked.

    The same view can be in both panels and full screen at once, so this is per
    session; the measured tree underneath is not.
    """

    def __init__(self, session_id: str, surface: str, scan: Scan):
        self.id = session_id
        self.surface = surface
        self.scan = scan
        self.focus = scan.root
        self.marked: Dict[str, Node] = {}
        # What the wedges of the last chart stood for, so an event that names
        # one by number can be turned back into a node.
        self.shown: List[Optional[Node]] = []

    @property
    def lock(self) -> threading.RLock:
        return self.scan.lock


_scans: Dict[str, Scan] = {}
_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


# -- urls ---------------------------------------------------------------------


def _name_of(url: str) -> str:
    path = unquote(urlparse(url).path).rstrip("/")
    return path.rsplit("/", 1)[-1] or url


def _local_path(url: str) -> Optional[str]:
    """The native path behind a ``file://`` URL, or None for anything else."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = unquote(parsed.path)
    # Windows arrives as /C:/Users/... — the leading slash belongs to the URL,
    # not to the path.
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


#: `C:` and friends: on Windows the first path segment is the volume, not a
#: folder, and it names the root instead of hanging below it.
_DRIVE = re.compile(r"^[A-Za-z]:$")


def _child_url(parent: str, name: str) -> str:
    return parent.rstrip("/") + "/" + quote(name, safe="")


# -- sizes --------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human(size: float) -> str:
    unit = 0
    while size >= 1024 and unit < len(_UNITS) - 1:
        size /= 1024.0
        unit += 1
    if unit == 0:
        return "%d B" % size
    return "%.1f %s" % (size, _UNITS[unit])


# -- the scan -----------------------------------------------------------------

#: How often the ring is redrawn while a scan is running. Often enough to look
#: alive, seldom enough that the drawing is not the expensive part.
PUSH_SECONDS = 0.5


def _entries(url: str) -> List[dict]:
    """One directory, read the fastest way that is honest for its location.

    Anything that is not a local folder goes through the host, which is what
    makes the same map work on an archive or an FTP server. Local folders can
    take the short way, and on a disk with a few hundred thousand files the
    difference is minutes.
    """
    if plugin.setting("direct", True):
        native = _local_path(url)
        if native is not None:
            return _scandir(native, url)
    return plugin.list_dir(url)


def _scandir(native: str, url: str) -> List[dict]:
    found = []
    with os.scandir(native) as entries:
        for entry in entries:
            try:
                # Never follow a link: it is somebody else's bytes, and a loop
                # of them would be a scan that never ends.
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            found.append(
                {
                    "name": entry.name,
                    "url": _child_url(url, entry.name),
                    "kind": "dir" if is_dir else "file",
                    "size": size,
                    "hidden": entry.name.startswith("."),
                }
            )
    return found


def _walk(scan: Scan, generation: int) -> None:
    """Measures the tree, pushing the chart as it goes.

    Breadth first, and that is not a detail: depth first would spend the first
    minute inside one folder, and the chart would show a single wedge filling
    the whole circle because it is the only thing anything is known about yet.
    Level by level, every wedge grows at once and the ring is honest from the
    first second.
    """
    include_hidden = bool(plugin.setting("hidden", True))
    pending = deque([scan.root])
    last_push = 0.0

    while pending:
        if scan.generation != generation:
            return
        node = pending.popleft()

        try:
            entries = _entries(node.url)
        except Exception:  # noqa: BLE001 - one unreadable folder is not fatal
            with scan.lock:
                scan.unreadable += 1
            continue

        with scan.lock:
            for entry in entries:
                if not include_hidden and entry.get("hidden"):
                    continue
                if entry.get("kind") == "link":
                    continue

                is_dir = entry.get("kind") == "dir"
                child = Node(
                    entry.get("name") or "?",
                    entry.get("url") or _child_url(node.url, entry.get("name") or ""),
                    is_dir,
                    node,
                )
                node.children.append(child)
                if is_dir:
                    pending.append(child)
                else:
                    scan.files += 1
                    child.grow(int(entry.get("size") or 0))

        now = time.monotonic()
        if now - last_push >= PUSH_SECONDS:
            last_push = now
            _push(scan, generation)

    with scan.lock:
        if scan.generation != generation:
            return
        scan.scanning = False
        scan.finished_at = time.time()
    _push(scan, generation)


def _push(scan: Scan, generation: int) -> None:
    """Redraws every open view of this tree, without any of them having asked."""
    if scan.generation != generation:
        return
    with scan.lock:
        drawn = [
            (session, _draw(session), [label for label, _ in _trail_of(session)])
            for session in scan.watchers.values()
        ]

    for session, (content, title, status), trail in drawn:
        plugin.update_view(
            VIEW_ID,
            session.id,
            content=content,
            title=title,
            status=status,
            trail=trail,
        )


def _start_scan(scan: Scan) -> None:
    """Throws away what was measured and measures it again."""
    with scan.lock:
        scan.generation += 1
        generation = scan.generation
        scan.root = Node(_name_of(scan.url), scan.url, True, None)
        scan.files = 0
        scan.unreadable = 0
        scan.scanning = True
        scan.finished_at = None
        # Every watcher was looking at nodes that no longer exist.
        for session in scan.watchers.values():
            session.focus = scan.root
            session.marked.clear()

    threading.Thread(
        target=_walk,
        args=(scan, generation),
        name="disk-map scan",
        daemon=True,
    ).start()


def _scan_for(url: str) -> Scan:
    """The measured tree for a folder, scanned if there is nothing usable.

    A scan is kept for as long as the plugin runs, because walking a disk costs
    minutes and the answer does not change much in an hour. It is measured
    again only when it has never been measured, when it has gone stale, or when
    the user presses Rescan — which is the button that exists for exactly the
    case where the cache is wrong.
    """
    with _sessions_lock:
        scan = _scans.get(url)
        if scan is None:
            scan = Scan(url)
            _scans[url] = scan

    with scan.lock:
        if scan.scanning:
            return scan
        age = scan.age()
        fresh = age is not None and age < _max_age_seconds()
    if not fresh:
        _start_scan(scan)
    return scan


def _max_age_seconds() -> float:
    minutes = plugin.setting("maxAge", 30)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        minutes = 30.0
    # Zero is a real answer: measure it again every time it is opened.
    return max(0.0, minutes) * 60.0


# -- what is on the list to go ------------------------------------------------


def _condemned(session: Session, node: Node) -> bool:
    """Whether this node is going, itself or because something above it is.

    Marking a folder marks what is inside it — that is what deleting a folder
    means, and a chart that showed the folder doomed and its contents untouched
    would be telling the user something that is not true.
    """
    walk: Optional[Node] = node
    while walk is not None:
        if walk.url in session.marked:
            return True
        walk = walk.parent
    return False


def _marked_ancestor(session: Session, node: Node) -> Optional[Node]:
    """The node above this one that put it on the list, if any."""
    walk = node.parent
    while walk is not None:
        if walk.url in session.marked:
            return walk
        walk = walk.parent
    return None


# -- drawing ------------------------------------------------------------------


def _draw(session: Session):
    """Builds the chart, the title and the status line from the tree.

    Called with the session's lock held: the scan is adding to the same tree
    from its own thread.
    """
    rings = max(1, int(plugin.setting("rings", 3) or 3))
    slices = max(3, int(plugin.setting("slices", 12) or 12))
    if session.surface == "panel":
        # A panel is half the width of the window and gets one ring fewer.
        rings = max(1, rings - 1)
        slices = min(slices, 8)

    segments: List[dict] = []
    shown: List[Optional[Node]] = []

    def add(node: Node, parent_index: int, ring: int) -> None:
        if ring >= rings:
            return
        children = sorted(
            (child for child in node.children if child.size > 0),
            key=lambda child: child.size,
            reverse=True,
        )
        for child in children[:slices]:
            segments.append(
                segment(
                    child.name,
                    child.size,
                    parent=parent_index,
                    url=child.url,
                    marked=_condemned(session, child),
                    folder=child.is_dir,
                    detail=human(child.size),
                )
            )
            shown.append(child)
            index = len(segments) - 1
            if child.is_dir:
                add(child, index, ring + 1)

        rest = sum(child.size for child in children[slices:])
        if rest > 0:
            # Gathered rather than dropped: a ring whose wedges do not add up
            # to the whole is a ring that lies about where the room went.
            segments.append(
                segment(
                    "%d more" % (len(children) - slices),
                    rest,
                    parent=parent_index,
                    detail=human(rest),
                )
            )
            shown.append(None)

    add(session.focus, -1, 0)
    session.shown = shown

    scan = session.scan
    marked_size = sum(node.size for node in session.marked.values())
    buttons = []
    if session.marked:
        buttons.append(
            button(
                "clean",
                "Clean up %d item(s), %s" % (len(session.marked), human(marked_size)),
                danger=True,
            )
        )
        # The way back from a slip of the hand. Picking things out one at a
        # time is easy to do by accident and tedious to undo the same way.
        buttons.append(button("unmark", "Clear %d mark(s)" % len(session.marked)))
    if session.focus.parent is not None:
        buttons.append(button("up", "Up"))
    buttons.append(button("rescan", "Rescan" if not scan.scanning else "Stop"))

    content = chart(
        segments,
        label=session.focus.name,
        detail=human(session.focus.size),
        buttons=buttons,
    )

    status = "%s in %d file(s)" % (human(scan.root.size), scan.files)
    if scan.scanning:
        status = "Scanning… " + status
    else:
        status += " · measured %s" % _ago(scan.age())
    if scan.unreadable:
        status += " · %d folder(s) could not be read" % scan.unreadable
    if session.marked:
        status += " · %d marked, %s" % (len(session.marked), human(marked_size))

    return content, _path_of(session), status


def _path_of(session: Session) -> str:
    """The whole path as one line, for anywhere a trail cannot be drawn."""
    labels = [label for label, _ in _trail_of(session)]
    head, tail = labels[0], "/".join(labels[1:])
    if head == "/":
        return "/" + tail
    return head + "/" + tail if tail else head


def _ago(seconds: Optional[float]) -> str:
    """How old the measurement is, so the map never pretends to be live."""
    if seconds is None:
        return "not yet"
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return "%d minutes ago" % (seconds // 60)
    return "%d hours ago" % (seconds // 3600)


def _trail_of(session: Session) -> List[tuple]:
    """The address bar: the **whole** path, as `(label, url)`, outermost first.

    Built from the URL rather than by walking the tree, because the tree starts
    wherever the map was opened. Walking it gave a trail beginning at that
    folder — the panel beside it showed `/Users/mac/…` while the map claimed
    `mac` was the top of the world. A path bar that disagrees with the panel
    next to it about where you are is worse than no path bar.

    The levels above what has been measured are real levels and are pressable;
    going to one points the map at it, which is what pressing a parent in a
    path bar has always meant.
    """
    url = session.focus.url
    parsed = urlparse(url)
    segments = [part for part in parsed.path.split("/") if part]
    query = ("?" + parsed.query) if parsed.query else ""
    base = "%s://%s" % (parsed.scheme, parsed.netloc)

    steps: List[tuple] = []
    first = 0
    if parsed.netloc:
        # A server is the top of its own tree, and is named as one.
        steps.append((parsed.netloc, base + "/" + query))
    elif segments and _DRIVE.match(unquote(segments[0])):
        steps.append((unquote(segments[0]), base + "/" + segments[0] + query))
        first = 1
    else:
        steps.append(("/", base + "/" + query))

    for index in range(first, len(segments)):
        path = "/" + "/".join(segments[: index + 1])
        steps.append((unquote(segments[index]), base + path + query))
    return steps


def _find(node: Node, url: str) -> Optional[Node]:
    """The node for a URL, if it is inside what has been measured."""
    if node.url == url:
        return node
    if not url.startswith(node.url.rstrip("/") + "/"):
        return None
    for child in node.children:
        found = _find(child, url)
        if found is not None:
            return found
    return None


def _answer(session: Session, actions: Optional[List[dict]] = None) -> dict:
    with session.lock:
        content, title, status = _draw(session)
        trail = [label for label, _ in _trail_of(session)]
    return respond(
        content=content,
        title=title,
        status=status,
        trail=trail,
        actions=actions,
    )


# -- the view -----------------------------------------------------------------


@plugin.view(VIEW_ID, "Disk map", "A ring of what is taking up the room.")
def disk_map(context, event):
    if event.kind == "open":
        if not context.url:
            return respond(status="Open this on a folder.")
        session = _open_session(context)
        return _answer(session)

    session = _sessions.get(context.session)
    if session is None:
        return None

    if event.kind == "activate":
        return _activate(session, event.row)

    if event.kind == "mark":
        return _mark(session, event.row)

    if event.kind == "step":
        return _step(session, event.row)

    if event.kind == "button":
        return _button(session, event.id)

    if event.kind == "deleted":
        return _deleted(session, event.urls)

    if event.kind == "key":
        if event.key in ("backspace", "left"):
            return _up(session)
        if event.key == " ":
            return None
    return None


def _open_session(context) -> Session:
    """Attaches this copy of the view to the tree for what it is pointed at."""
    return _point(context.session, context.surface, context.url)


def _point(session_id: str, surface: str, url: str) -> Session:
    """Points one open copy of the view at a folder, measuring it if need be.

    Shared by opening the view and by pressing a level of the path bar that is
    above whatever was measured — which is the same act: the map is now looking
    somewhere else, and the tree for it is either remembered or walked.
    """
    scan = _scan_for(url)

    with _sessions_lock:
        previous = _sessions.get(session_id)
        session = Session(session_id, surface, scan)
        # Marks are about the disk, not about the window, so re-opening the
        # same folder keeps what was picked out. A different folder does not.
        if previous is not None and previous.scan is scan:
            session.marked = previous.marked
            # Only if a rescan has not replaced the tree those nodes were in.
            if _root_of(previous.focus) is scan.root:
                session.focus = previous.focus
        _sessions[session_id] = session

    if previous is not None and previous.scan is not scan:
        with previous.scan.lock:
            previous.scan.watchers.pop(session_id, None)

    with scan.lock:
        scan.watchers[session.id] = session
    return session


def _root_of(node: Node) -> Node:
    while node.parent is not None:
        node = node.parent
    return node


def _node_at(session: Session, row: Optional[int]) -> Optional[Node]:
    if row is None or row < 0:
        return None
    with session.lock:
        if row >= len(session.shown):
            return None
        return session.shown[row]


def _walk_to(node: Node) -> Optional[List[dict]]:
    """What to ask of the host when the map moves to a folder.

    Every way of moving answers this the same way — a wedge, the middle of the
    ring, Up, a level of the address bar. They are one act with four gestures,
    and having only the wedge walk the panel along was a gap, not a design.
    """
    if not plugin.setting("follow", True):
        return None
    return [navigate(node.url)]


def _activate(session: Session, row: Optional[int]) -> Optional[dict]:
    # The middle of the ring is the way back out.
    if row is not None and row < 0:
        return _up(session)

    node = _node_at(session, row)
    if node is None:
        return None

    if not node.is_dir:
        return _answer(session, [open_viewer(node.url)])

    with session.lock:
        session.focus = node
    return _answer(session, _walk_to(node))


def _step(session: Session, index: Optional[int]) -> Optional[dict]:
    """A level of the address bar was pressed: go straight to it."""
    if index is None:
        return None

    with session.lock:
        trail = _trail_of(session)
        if index < 0 or index >= len(trail):
            return None
        url = trail[index][1]
        if url == session.focus.url:
            return None

        # Inside what has been measured, so it costs nothing.
        node = _find(session.scan.root, url)
        if node is not None:
            session.focus = node
            return _answer(session, _walk_to(node))

        surface = session.surface

    # Above the folder the map was opened on. Pressing a parent in a path bar
    # has always meant "show me that", so the map goes there — and the cache
    # decides whether that is instant or a walk.
    moved = _point(session.id, surface, url)
    return _answer(moved, _walk_to(moved.focus))


def _up(session: Session) -> dict:
    with session.lock:
        parent = session.focus.parent
        if parent is None:
            # Not the top of the disk, only of what was measured — the address
            # bar goes further, and saying so is more use than a flat refusal.
            return respond(
                actions=[notice("The map starts here. The path above it is in "
                                "the address bar.")]
            )
        session.focus = parent
    return _answer(session, _walk_to(parent))


def _mark(session: Session, row: Optional[int]) -> Optional[dict]:
    """Puts one thing on the list to go, or takes it back off.

    Marking a folder covers everything inside it, so the list only ever holds
    the topmost of each branch: a child already covered by its parent adds
    nothing to what will be deleted, and would be counted twice in the total.
    """
    node = _node_at(session, row)
    if node is None:
        return None

    with session.lock:
        if node.url in session.marked:
            del session.marked[node.url]
        else:
            covering = _marked_ancestor(session, node)
            if covering is not None:
                # Pressing something that is going because its folder is going
                # can only mean one thing: not that folder after all.
                del session.marked[covering.url]
            else:
                session.marked[node.url] = node
                # Anything inside it is now covered and no longer its own item.
                for url, marked in list(session.marked.items()):
                    if marked is not node and _marked_ancestor(session, marked):
                        del session.marked[url]

    return _answer(session)


def _button(session: Session, button_id: Optional[str]) -> Optional[dict]:
    if button_id == "up":
        return _up(session)

    if button_id == "unmark":
        with session.lock:
            count = len(session.marked)
            session.marked.clear()
        if count == 0:
            return None
        return _answer(session, [notice("Nothing is marked any more.")])

    if button_id == "rescan":
        scan = session.scan
        with scan.lock:
            running = scan.scanning
            if running:
                # The same button stops a walk that is under way: bumping the
                # generation is what the walking thread checks between folders.
                scan.generation += 1
                scan.scanning = False
                scan.finished_at = time.time()
        if not running:
            _start_scan(scan)
        return _answer(session)

    if button_id == "clean":
        with session.lock:
            urls = list(session.marked.keys())
        if not urls:
            return None
        # The host asks the user, uses the recycle bin where there is one, and
        # tells us what actually went. Nothing is taken off the map here.
        return respond(actions=[delete(urls)])

    return None


def _deleted(session: Session, urls: List[str]) -> dict:
    with session.lock:
        for url in urls:
            node = session.marked.pop(url, None)
            if node is None:
                continue
            # The focus cannot stay inside something that is no longer there.
            walk = session.focus
            while walk is not None:
                if walk is node:
                    session.focus = node.parent or session.scan.root
                    break
                walk = walk.parent
            node.detach()
    return _answer(session)


@plugin.on_view_closed
def forget(view_id: str, session_id: str) -> None:
    """Lets go of one open copy — but not of what it measured.

    The tree stays: closing the map and opening it again is the commonest thing
    anyone does with it, and re-walking the disk each time would make the cache
    pointless. A walk still running is left to finish, so what it was for ends
    up in the cache rather than being thrown away half done.
    """
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return
    with session.scan.lock:
        session.scan.watchers.pop(session_id, None)


plugin.run()
