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

"""Git — the repository the panel is standing in.

The log, what is in it, and what is not pushed yet. It follows the panel: walk
into a repository and it is showing that one, walk out of it and it says so.

**Nothing here reaches the network.** Whether a commit is pushed is answered
from what the last fetch left behind, because a tool that opens a connection
because you walked into a folder is a tool that hangs on a folder you did not
mean to open. What it does write — staging, unstaging, switching branch — it
asks about first, and a dirty tree refuses a switch rather than negotiating one.
Committing needs a line of typed text, which a plugin cannot ask for.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from xcommander import (
    DIRECTORY,
    ask,
    cell,
    chip,
    column,
    Entry,
    FILE,
    FileSystem,
    lay_out,
    Plugin,
    RpcError,
    file,
    navigate,
    notice,
    part,
    respond,
    row,
    split,
    table,
    text,
)

VIEW_ID = "git.log"

plugin = Plugin("org.xcommander.git", "Git")

#: Field separator inside one log line. A unit separator cannot appear in a
#: name, a date or a subject, which `|` and tabs certainly can.
FS = "\x1f"

#: How long any one git call may take. A repository on a sleeping disk should
#: make the view say so, not make the application wait for it.
TIMEOUT = 20


# -- talking to git -----------------------------------------------------------


def run(root: str, *args: str) -> Optional[str]:
    """One git command, or None when it failed or there is no git at all."""
    try:
        done = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")


def local_path(url: Optional[str]) -> Optional[str]:
    """The folder behind a `file:` url, or None for anything else.

    A repository is a thing on a disk. An archive or an FTP server does not
    have one, and asking git about a path it cannot see is a slow way to be
    told nothing.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("", "file"):
        return None
    path = unquote(parsed.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def repository_of(folder: str) -> Optional[str]:
    """The root of the repository [folder] is inside, if it is inside one."""
    if not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    top = run(folder, "rev-parse", "--show-toplevel")
    return top.strip() if top else None


# -- what it knows about a repository ------------------------------------------


class Commit:
    """One row of the log, and one commit — there is no other kind of row.

    There used to be. `git log --graph` draws with characters, so a merge
    fanning out takes a line of its own with no commit on it, and those had to
    be kept or the lines came apart. The host draws the braid from lanes now,
    so a row is a commit and nothing else.
    """

    __slots__ = ("hash", "short", "author", "date", "subject", "parents",
                 "refs", "email", "local")

    def __init__(self, hash: str, short: str, author: str, date: str,
                 subject: str, parents: List[str], refs: List[Tuple[str, str]],
                 email: str = "") -> None:
        self.hash = hash
        self.short = short
        self.author = author
        self.date = date
        self.subject = subject
        #: Full hashes, first parent first. What the shape of the log is made
        #: of, and the only thing that can say what that shape is.
        self.parents = parents
        #: What points at this commit: `(kind, name)`, kind being `head`,
        #: `branch`, `remote` or `tag`.
        self.refs = refs
        #: The author's address. Handed to the host **only** when the user has
        #: asked for pictures — sending it is asking for one to be fetched.
        self.email = email
        #: True when no remote has it yet — the thing a log is read for as
        #: often as not.
        self.local = False


def _refs_on(decoration: str) -> List[Tuple[str, str]]:
    """`%D` read into `(kind, name)`, in the order git prints them.

    Git says `HEAD -> main, origin/main, tag: v1.0`, and each of those means a
    different thing to somebody reading a log: where you are, where the remote
    was, and a name somebody nailed on.
    """
    found: List[Tuple[str, str]] = []
    for piece in decoration.split(","):
        name = piece.strip()
        if not name:
            continue
        if name.startswith("HEAD -> "):
            found.append(("head", name[len("HEAD -> "):]))
        elif name == "HEAD":
            found.append(("head", "HEAD"))
        elif name.startswith("tag: "):
            found.append(("tag", name[len("tag: "):]))
        elif "/" in name:
            found.append(("remote", name))
        else:
            found.append(("branch", name))
    return found


def log_of(root: str, count: int, all_branches: bool, graph: bool,
           ref: str = "HEAD") -> Tuple[List[Commit], bool]:
    """The log: what each commit is, what it came from, and what points at it.

    One call. `%P` is what the shape is made of and `%D` is what a reader looks
    for first, and asking for either separately would be walking the same
    history twice.

    **`--topo-order` when there is a graph, and it is not a preference.** By
    date, a parent committed after its child comes out *above* it — which
    happens the moment anyone works on two branches in an afternoon — and a
    line then has to be drawn downwards to a commit that is already above.
    Ordered by the shape, a parent is always below its children and the braid
    holds. `git log --graph` turns this on for itself; we draw the graph, so we
    turn it on for ourselves. Without a graph the dates are the more useful
    order and it stays off.

    Answers the rows **and whether there were more**. A log that stops has to
    say so: a fork whose other side is one commit past the end looks exactly
    like no fork at all, and silently is the one way it must not look.
    """
    fields = FS.join(["%H", "%h", "%an", "%ad", "%s", "%P", "%D", "%ae"])
    body = run(
        root,
        "log",
        *(["--topo-order"] if graph else []),
        "--all" if all_branches else ref,
        # One more than asked for, so "there are more" is a fact rather than a
        # guess — and free, rather than a second walk of the history to count
        # it. The extra one is dropped before anybody sees it.
        "--max-count=%d" % (count + 1),
        "--date=format:%Y-%m-%d %H:%M",
        "--pretty=format:" + fields,
    )
    if not body:
        return [], False

    rows: List[Commit] = []
    for line in body.splitlines():
        parts = line.split(FS)
        if len(parts) < 8:
            continue
        rows.append(
            Commit(
                parts[0],
                parts[1],
                parts[2],
                parts[3],
                parts[4],
                parts[5].split(),
                _refs_on(parts[6]),
                parts[7],
            )
        )

    more = len(rows) > count
    return (rows[:count] if more else rows), more


def unpushed(root: str) -> set:
    """Every commit no remote has, by full hash.

    `--not --remotes` is the whole question asked in git's own words: what is
    reachable from a branch and from no remote-tracking branch. Read from what
    the last fetch left on the disk — nothing here opens a connection.
    """
    body = run(root, "log", "--branches", "--not", "--remotes", "--pretty=format:%H")
    return set(body.split()) if body else set()


def state_of(root: str) -> Tuple[str, str]:
    """The branch, and how it stands against its remote and the working tree."""
    branch = (run(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if branch == "HEAD":
        # A detached head is a real state and worth saying plainly.
        short = (run(root, "rev-parse", "--short", "HEAD") or "").strip()
        branch = "detached at %s" % short if short else "detached"

    parts: List[str] = []

    counts = run(root, "rev-list", "--left-right", "--count", "@{u}...HEAD")
    if counts:
        pieces = counts.split()
        if len(pieces) == 2:
            behind, ahead = int(pieces[0]), int(pieces[1])
            if ahead:
                parts.append("%d ahead" % ahead)
            if behind:
                parts.append("%d behind" % behind)
    else:
        parts.append("no upstream")

    dirty = run(root, "status", "--porcelain")
    if dirty is None:
        pass
    elif dirty.strip():
        changed = len([line for line in dirty.splitlines() if line.strip()])
        parts.append("%d changed" % changed)
    else:
        parts.append("clean")

    return branch, ", ".join(parts)


# -- what it draws -------------------------------------------------------------

#: The row that stands for the working tree, at the head of the log. Not a
#: commit and deliberately shaped like one: it is where the eye goes first, and
#: it is what Fork puts there. It has no place in the braid — nothing
#: is committed, so there is nothing for a line to come from.
WORKING = "working"


def columns_of(graph: bool) -> List[dict]:
    """What a log is made of, and which part of it stretches.

    The one thing a grid of text could never say, and the reason the host grew
    columns at all: the braid must not stretch, the subject must, and the date
    must not wrap.
    """
    return [
        *([column("", kind="graph")] if graph else []),
        column("Commit", width=72, kind="mono"),
        column("Subject", flex=3),
        # The author as a ring with their initials in it, and the name beside
        # them. Wider than words alone need, because the ring takes its own
        # room and a name squeezed against it reads as one word.
        column("Author", width=140, kind="avatar"),
        column("When", width=118, align="right"),
    ]


def rows_of(commits: List[Commit], graph: bool,
            avatars: bool = False) -> List[dict]:
    """One row per commit, with the braid beside it if it is wanted.

    The lanes come from the parents, which is the only thing that knows the
    shape. `lay_out` is the host SDK's, so this plugin never writes that
    algorithm — and neither does the next one.
    """
    braids: List[Optional[dict]] = []
    if graph:
        # The working tree is not in the history and takes no lane: nothing is
        # committed, so there is nothing for a line to come from. Laid out
        # without it and put back in step afterwards.
        laid = lay_out(
            [(c.hash, c.parents) for c in commits if c.hash != WORKING]
        )
        at = 0
        for commit in commits:
            if commit.hash == WORKING:
                braids.append(None)
            else:
                braids.append(laid[at])
                at += 1

    rows: List[dict] = []
    for index, commit in enumerate(commits):
        cells: List[object] = [
            commit.short,
            cell(
                commit.subject,
                chips=[chip(name, kind) for kind, name in commit.refs],
            ),
            cell(commit.author, email=commit.email if avatars else None),
            commit.date,
        ]
        if graph:
            cells.insert(0, "")

        rows.append(
            row(
                cells,
                # The working tree is not in the history: `pending` is the row
                # saying it is real but not written down yet, which the host
                # draws a fifth lighter. Not pushed is the other thing about a
                # commit worth saying without words, and `accent` says "there
                # is something different about this one" without naming a
                # colour.
                role="pending" if commit.hash == WORKING
                else "accent" if commit.local
                else "normal",
                braid=braids[index] if graph else None,
            )
        )
    return rows


def content_of(commits: List[Commit], graph: bool,
               avatars: bool = False) -> dict:
    return table(columns_of(graph), rows_of(commits, graph, avatars))


#: What each setting is when nothing has said otherwise. The same numbers the
#: manifest declares, kept here as well because a toggle has to know what it is
#: toggling *from*: `not options.get(key)` reads a missing key as "off" and
#: turns a switch that is already on... on again.
_DEFAULTS: Dict[str, object] = {
    "commits": 1000,
    # **On, because the prototype is Fork.** A history has forks in it, and a
    # log of one branch cannot draw one: the commit the branch left from is in
    # it and the branch itself is not, so what was a fork comes out a straight
    # line. Showing only the branch you are on hides exactly the thing the
    # braid is drawn for.
    "allBranches": True,
    "graph": True,
    "sidebar": False,
    # Off, and it is the switch that makes the rule true rather than an
    # exception to it: a picture of somebody lives on a server, so nothing is
    # fetched until somebody has said they want that.
    "avatars": False,
}


def setting(options: Dict[str, object], key: str) -> object:
    return options.get(key, _DEFAULTS.get(key))


def menus_of(options: Dict[str, object]) -> List[dict]:
    return [
        {
            "label": "Repository",
            "accelerator": "r",
            "items": [
                {"id": "refresh", "label": "Read it again", "shortcut": "F5"},
                {},
                {
                    "id": "toggle.sidebar",
                    "label": "Branches, tags and remotes down the side",
                    "checked": bool(setting(options, "sidebar")),
                },
                {"id": "checkout", "label": "Switch to the branch being shown"},
                {
                    "id": "browse",
                    "label": "Open this commit in the panel beside this one",
                },
                {},
                {
                    "id": "toggle.allBranches",
                    "label": "Every branch, not only this one",
                    "checked": bool(setting(options, "allBranches")),
                },
                {
                    "id": "toggle.graph",
                    "label": "Draw the shape of the history",
                    "checked": bool(setting(options, "graph")),
                },
            ],
        },
    ]


# -- one commit ----------------------------------------------------------------

#: What `git show --name-status` puts in front of a path, as a mark the host
#: knows how to draw. The word travels with it for the tooltip: a glyph nobody
#: can ask the meaning of is a glyph to be guessed at.
_STATUS = {
    "A": ("added", "added"),
    "M": ("changed", "changed"),
    "D": ("deleted", "deleted"),
    "R": ("renamed", "renamed"),
    "C": ("copied", "copied"),
    "T": ("changed", "kind changed"),
    "U": ("conflict", "conflicted"),
}


def _mark(status: str) -> dict:
    """One `name-status` letter as a cell the host draws as a mark."""
    icon, word = _STATUS.get(status, ("changed", status))
    return cell(word, icon=icon)


def files_of(root: str, hash: str) -> List[Tuple[str, str]]:
    """What one commit touched: `(status, path)`, in git's own order."""
    body = run(
        root,
        "show",
        "--name-status",
        "--pretty=format:",
        "--no-color",
        hash,
    )
    if not body:
        return []

    files: List[Tuple[str, str]] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # A rename carries both names; the one that matters now is where the
        # file ended up.
        files.append((parts[0][0], parts[-1]))
    return files


def working_tree(root: str) -> List[Tuple[str, str, str]]:
    """What is changed and what is staged: `(index, tree, path)`.

    Straight from `status --porcelain`, whose two columns are exactly that
    question: what the index has that HEAD does not, and what the disk has that
    the index does not. `??` in both is a file git has never been told about.
    """
    body = run(root, "status", "--porcelain")
    if not body:
        return []

    changes: List[Tuple[str, str, str]] = []
    for line in body.splitlines():
        if len(line) < 4:
            continue
        # A rename reads `R  old -> new`; where it ended up is what matters.
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changes.append((line[0], line[1], path.strip('"')))
    return changes


def worktree_diff(root: str, index: str, tree: str, path: str) -> str:
    """One file's difference, from whichever side of it has changed."""
    if index == "?" or tree == "?":
        # Never seen by git, so there is nothing to compare it against — but
        # what is in it is exactly what would be added.
        body = run(root, "diff", "--no-color", "--no-index", os.devnull, path)
        return (body or "").lstrip("\n")

    if tree != " ":
        # Changed on the disk since it was staged, which is the difference the
        # user is looking at while they work.
        body = run(root, "diff", "--no-color", "--", path)
        if body and body.strip():
            return body.lstrip("\n")

    body = run(root, "diff", "--no-color", "--cached", "--", path)
    return (body or "").lstrip("\n")


def refs_of(root: str) -> List[Tuple[str, str, str, str]]:
    """Every branch, tag and remote branch: `(kind, name, subject, when)`.

    One call rather than three. `for-each-ref` is git's own answer to "what is
    there", and sorting by when it was last touched puts what somebody is
    working on at the top — which is where they will look for it.
    """
    body = run(
        root,
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)" + FS + "%(refname)" + FS
        + "%(contents:subject)" + FS + "%(committerdate:format:%Y-%m-%d %H:%M)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    if not body:
        return []

    refs: List[Tuple[str, str, str, str]] = []
    for line in body.splitlines():
        parts = line.split(FS)
        if len(parts) < 4:
            continue
        short, full, subject, when = parts[0], parts[1], parts[2], parts[3]
        if full.startswith("refs/heads/"):
            kind = "branch"
        elif full.startswith("refs/remotes/"):
            kind = "remote"
        else:
            kind = "tag"
        refs.append((kind, short, subject, when))
    return refs


# -- the few things that write --------------------------------------------------

#: What has been asked and is waiting for an answer, per session. The question
#: carries only an id across the round trip, so this is where the rest of it
#: waits.
_pending: Dict[str, Tuple[str, str]] = {}


def do(root: str, *args: str) -> Tuple[bool, str]:
    """Runs a git command that changes something, and says how it went.

    Separate from `run` because the answer that matters here is different: a
    reader wants the output, and a writer wants to know whether it worked and
    what git said when it did not — which is what the user has to be shown.
    """
    try:
        done = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as failure:
        return False, str(failure)

    if done.returncode == 0:
        return True, done.stdout.decode("utf-8", "replace").strip()
    message = done.stderr.decode("utf-8", "replace").strip()
    return False, message or "git refused, and said nothing about why."


def message_of(root: str, hash: str) -> str:
    return (run(root, "show", "-s", "--pretty=format:%s", hash) or "").strip()


def is_binary(root: str, hash: str, path: str) -> bool:
    """Whether git calls this change binary — its own answer, not a guess.

    `--numstat` counts the lines added and removed, and prints `-` for both
    when there are no lines to count. That is git saying "there is no diff of
    this to show", which is exactly the question being asked; guessing from the
    name would be wrong for a `.txt` full of NULs and for a `.dat` full of
    words.
    """
    body = run(root, "show", "--numstat", "--pretty=format:", hash, "--", path)
    for line in (body or "").splitlines():
        fields = line.split("\t")
        if len(fields) >= 3:
            return fields[0] == "-" and fields[1] == "-"
    return False


def worktree_is_binary(root: str, path: str) -> bool:
    """The same question about the working tree."""
    for args in (["diff", "--numstat", "--", path],
                 ["diff", "--numstat", "--cached", "--", path]):
        body = run(root, *args)
        for line in (body or "").splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                return fields[0] == "-" and fields[1] == "-"
    return False


def diff_of(root: str, hash: str, path: str) -> str:
    """One file's difference in one commit, as git prints it."""
    body = run(
        root,
        "show",
        "--no-color",
        "--pretty=format:",
        hash,
        "--",
        path,
    )
    return (body or "").lstrip("\n")


# -- a commit as a folder -------------------------------------------------------


def tree_url(root: str, ref: str, inner: str = "") -> str:
    """Where a panel goes to stand inside a commit.

    Shaped like the archive plugin's: the path is the path *inside* the tree
    and what it is inside rides in the query. That is what keeps "up one level"
    ordinary string work for the host rather than something every file system
    has to be asked about.
    """
    return "git:///%s?repo=%s&ref=%s" % (
        quote(inner.strip("/")),
        quote("file://" + root, safe=""),
        quote(ref, safe=""),
    )


def _split(url: str) -> Tuple[str, str, str]:
    """A `git:` url as `(repository root, ref, path inside)`."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    repo = local_path((query.get("repo") or [""])[0])
    ref = (query.get("ref") or [""])[0]
    if not repo or not ref:
        raise RpcError("This is not a place in a repository: %s" % url)
    return repo, ref, unquote(parsed.path or "").strip("/")


class GitFileSystem(FileSystem):
    """A commit, read as a folder.

    Read-only, and not by omission: history is what has happened, and a panel
    that offered to write into it would be offering something git itself does
    not do. Copying *out* of a commit is only reads, and that is the thing
    somebody actually wants — the file as it was, next to the file as it is.
    """

    scheme = "git"

    def __init__(self) -> None:
        #: The last blob read, so scrolling a file does not re-run git for
        #: every block the viewer asks for.
        self._blob: Optional[Tuple[str, bytes]] = None
        #: When each ref was committed. Every entry in a tree carries it,
        #: because "the tree at that revision" has exactly one date.
        self._dated: Dict[str, Optional[float]] = {}

    # -- listing -----------------------------------------------------------

    def list(self, url: str) -> List[Entry]:
        root, ref, inner = _split(url)
        body = run(root, "ls-tree", "--long", "%s:%s" % (ref, inner))
        if body is None:
            raise RpcError("%s is not in %s" % (inner or "/", ref))

        when = self._when(root, ref)
        entries: List[Entry] = []
        for line in body.splitlines():
            if "\t" not in line:
                continue
            head, name = line.split("\t", 1)
            parts = head.split()
            if len(parts) < 3:
                continue
            kind = parts[1]
            size = 0
            if len(parts) >= 4 and parts[3].isdigit():
                size = int(parts[3])
            entries.append(
                Entry(
                    name=name.strip('"'),
                    # A submodule is a commit inside a tree. It is a folder to
                    # look at even though nothing here can walk into it.
                    kind=DIRECTORY if kind in ("tree", "commit") else FILE,
                    size=size,
                    modified=when,
                    hidden=name.startswith("."),
                )
            )
        return entries

    def stat(self, url: str) -> Optional[Entry]:
        root, ref, inner = _split(url)
        if not inner:
            return Entry(name=ref, kind=DIRECTORY, modified=self._when(root, ref))

        parent, _, name = inner.rpartition("/")
        for entry in self.list(tree_url(root, ref, parent)):
            if entry.name == name:
                return entry
        return None

    # -- reading -----------------------------------------------------------

    def read(self, url: str, offset: int, length: int) -> bytes:
        root, ref, inner = _split(url)
        key = "%s\x00%s\x00%s" % (root, ref, inner)

        if self._blob is None or self._blob[0] != key:
            data = self._blob_of(root, ref, inner)
            self._blob = (key, data)
        return self._blob[1][offset:offset + length]

    def _blob_of(self, root: str, ref: str, inner: str) -> bytes:
        try:
            done = subprocess.run(
                ["git", "-C", root, "cat-file", "blob", "%s:%s" % (ref, inner)],
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as failure:
            raise RpcError("Could not read %s in %s: %s" % (inner, ref, failure))
        if done.returncode != 0:
            raise RpcError("%s is not a file in %s" % (inner, ref))
        return done.stdout

    def _when(self, root: str, ref: str) -> Optional[float]:
        if ref in self._dated:
            return self._dated[ref]
        stamp = run(root, "show", "-s", "--pretty=format:%ct", ref)
        try:
            when = float((stamp or "").strip())
        except ValueError:
            when = None
        self._dated[ref] = when
        return when


plugin.add_filesystem(GitFileSystem())


# -- the view ------------------------------------------------------------------


class Where:
    """What one open copy of the view is looking at.

    One page, not four levels. The branches down the side, the log, what the
    commit under the cursor touched, and the difference of the file under
    *that* cursor are all on it at once — so nothing here is a place you go to
    and come back from, and nothing you were reading goes off screen because
    you looked at something else.
    """

    __slots__ = ("root", "branch", "name", "hash", "short", "subject",
                 "author", "files", "file", "diff", "shown", "refs", "rows",
                 "sidebar", "avatars")

    def __init__(self, root: str, branch: str, name: str) -> None:
        self.root = root
        self.branch = branch
        self.name = name
        #: The commit whose files are showing. [WORKING] for the working tree.
        self.hash = ""
        self.short = ""
        self.subject = ""
        self.author = ""
        #: `(status, path)` for the commit being shown.
        self.files: List[Tuple[str, str]] = []
        #: Which of them the difference below is of.
        self.file = -1
        self.diff = ""
        #: A file to be *shown* rather than described — a picture, anything git
        #: has no diff for. Empty when the difference is words.
        self.shown = ""
        #: The branches, tags and remotes as last listed.
        self.refs: List[Tuple[str, str, str, str]] = []
        #: What each row of the sidebar stands for, or None for a heading.
        self.rows: List[Optional[str]] = []
        #: Whether the list of branches stands down the side as well. Off: the
        #: pill in the bar says which branch and opens the rest, and the room
        #: goes to the log.
        self.sidebar = False
        #: Whether the author's address is handed over, which is what asks the
        #: host to go and find their picture.
        self.avatars = False


#: What each open copy is looking at.
_at: Dict[str, Where] = {}

#: The rows the log last drew, so a press can say which commit it was. Kept
#: rather than re-read: the row index is only meaningful against the list the
#: user is actually looking at, and re-running `git log` because somebody
#: pressed Down would make the arrow keys cost a process each.
_log_cache: Dict[str, List[Commit]] = {}
_options: Dict[str, Dict[str, object]] = {}


# -- the page ------------------------------------------------------------------


def _files_content(at: Where) -> dict:
    """What the commit under the cursor touched."""
    if at.hash == WORKING:
        return table(
            [
                # Two marks, because a file has two states at once: what the
                # index has that HEAD does not, and what the disk has that the
                # index does not. One column saying both in words was a
                # sentence per row.
                column("", kind="icon"),
                column("", kind="icon"),
                column("File", flex=1),
            ],
            [
                row(
                    [
                        _staged_mark(status[0]),
                        _mark_of(status[0], (status + "  ")[1]),
                        path,
                    ],
                    # What is staged is what the next commit will be made of,
                    # and stands out; what is not is `pending` — there, real,
                    # and not going anywhere yet.
                    role="accent" if status[0] not in (" ", "?")
                    else "pending",
                )
                for status, path in at.files
            ],
        )

    return table(
        [column("", kind="icon"), column("File", flex=1)],
        [row([_mark(status), path]) for status, path in at.files],
    )


def _detail_title(at: Where) -> str:
    if not at.hash:
        return "Nothing selected"
    if at.hash == WORKING:
        staged = sum(1 for status, _ in at.files if status[0] not in (" ", "?"))
        return "Working tree — %d changed%s" % (
            len(at.files),
            ", %d staged" % staged if staged else "",
        )
    return "%s — %s%s" % (
        at.short,
        at.subject,
        " · %s" % at.author if at.author else "",
    )


#: What each shelf of the sidebar is called, in the order Fork stands them in.
_SHELVES = [("branch", "Branches"), ("remote", "Remotes"), ("tag", "Tags")]


def branch_pill(at: Where) -> dict:
    """The branch you are on, and the way to any other — in one button.

    **The panel's own drive button, in the tool's bar.** A list of branches
    down the side of the window says the same thing and spends a fifth of the
    window saying it, which is a poor trade in a tool whose point is the log.
    Pressing a row raises `button` with the ref's own name, which is all the
    plugin needs to answer it.
    """
    items: List[dict] = []
    for kind, heading in _SHELVES:
        on_shelf = [ref for ref in at.refs if ref[0] == kind]
        if not on_shelf:
            continue
        if items:
            items.append({})
        items.append({"label": heading})
        for _, name, _, _ in on_shelf:
            items.append({
                "id": "ref." + name,
                "label": name,
                "checked": name == at.branch,
            })

    return {
        "id": "refs",
        "label": at.branch or "no branch",
        "tooltip": "%s in %s — the branch being shown, and the way to another"
        % (at.branch or "detached", at.name),
        "items": items,
    }


def sidebar_of(at: Where) -> dict:
    """Every branch, remote and tag, standing beside the log rather than
    instead of it.

    **This is the difference between Fork and the tool this used to copy.** A
    list of branches on a page of its own is a place you go to and come back
    from; a list of branches down the side is something you glance at, and the
    glance is most of what it is for. It costs a part now, so it is one.

    The rows are kept flat with a heading between the shelves. A heading is a
    row that is `strong` and has nothing to press — which is the whole of what
    a heading is, and needs no shape of its own in the contract.
    """
    rows: List[dict] = []
    #: What each row *is*, so a press knows: `None` for a heading.
    at.rows = []

    for kind, heading in _SHELVES:
        on_shelf = [ref for ref in at.refs if ref[0] == kind]
        if not on_shelf:
            continue
        rows.append(row([cell("%s  %d" % (heading, len(on_shelf)))], role="dim"))
        at.rows.append(None)
        for _, name, subject, when in on_shelf:
            rows.append(
                row([
                    cell(chips=[chip(
                        name,
                        # The one you are on is drawn as the one you are on,
                        # here and in the log both.
                        "head" if kind == "branch" and name == at.branch
                        else kind,
                    )])
                ])
            )
            at.rows.append(name)

    if not rows:
        return text("This repository has no branches yet.")
    return table([column("", flex=1)], rows)


def page_of(at: Where, commits: List[Commit], graph: bool) -> dict:
    """The whole page: the log, and underneath it what the cursor is on.

    The shape Fork has, and the reason the host grew parts: reading a history
    means looking at a commit *without losing the list it is in*, and knowing
    what branches there are without going to look for them.
    """
    history = split(
        [
            part("log", content_of(commits, graph, at.avatars), weight=3),
            part("detail", _detail_content(at), weight=2,
                 title=_detail_title(at)),
        ]
    )
    if not at.sidebar:
        return history

    return split(
        [
            part("refs", sidebar_of(at), weight=1, title=at.name),
            part("history", history, weight=4),
        ],
        "horizontal",
    )


def _detail_content(at: Where) -> dict:
    """What the commit under the cursor touched, and the file under that one.

    The working tree is a commit like any other here: its row is at the head
    of the log, and pressing it fills these same two panes with what is
    changed and what is staged. One shape over both questions is why walking
    from one to the other is not a jolt.
    """
    if not at.hash:
        return text("Move down the log and what each commit touched shows here.")

    if not at.files:
        return text(
            "Nothing has changed since the last commit."
            if at.hash == WORKING
            else "This commit changed nothing on its own. A merge usually has "
            "not: what came with it belongs to the commits it joined."
        )

    return split(
        [
            part("files", _files_content(at), weight=2),
            part(
                "diff",
                file(at.shown) if at.shown
                else text(at.diff or "Nothing to show for this file.",
                          language="diff"),
                weight=3,
            ),
        ],
        "horizontal",
    )


def select_commit(at: Where, commit: Commit) -> None:
    """Points the bottom half at a commit, and at the first file in it."""
    at.hash = commit.hash
    at.short = commit.short
    at.subject = commit.subject or (
        "" if commit.hash == WORKING else message_of(at.root, commit.hash)
    )
    at.author = commit.author

    if commit.hash == WORKING:
        at.files = [
            (index + tree, path) for index, tree, path in working_tree(at.root)
        ]
    else:
        at.files = files_of(at.root, commit.hash)

    at.file = -1
    at.diff = ""
    at.shown = ""
    if at.files:
        select_file(at, 0)


def select_file(at: Where, index: int) -> None:
    """Points the difference at one of the files, and reads it.

    **A picture is shown rather than described.** git has no diff for a binary
    file and says so; where it says so, this points the host at the file itself
    and lets whatever claims that extension draw it. The plugin never learns
    what a PNG is, and the day a better image viewer is installed this shows
    that one.
    """
    if index < 0 or index >= len(at.files):
        return
    at.file = index
    at.shown = ""
    status, path = at.files[index]

    if at.hash == WORKING:
        if worktree_is_binary(at.root, path):
            # On the disk, which is the version being looked at.
            at.diff = ""
            at.shown = "file://" + quote(os.path.join(at.root, path))
            return
        at.diff = worktree_diff(at.root, status[0], (status + "  ")[1], path)
        return

    if is_binary(at.root, at.hash, path):
        # As it was at that commit, not as it is now — the whole reason for
        # looking at a commit at all. A file the commit *deleted* has no such
        # version, and git says nothing for it either.
        at.diff = ""
        at.shown = "" if status == "D" else tree_url(at.root, at.hash, path)
        return

    at.diff = diff_of(at.root, at.hash, path)


def show(context, options: Optional[Dict[str, object]] = None,
         ref: Optional[str] = None) -> dict:
    folder = local_path(context.url)
    if options is None:
        options = dict(plugin.settings)
    _options[context.session] = options

    if folder is None:
        return respond(
            content=text("A repository is a folder on a disk, and this is not one."),
            title="Git",
            status="",
            menus=menus_of(options),
        )

    root = repository_of(folder)
    if root is None:
        _at.pop(context.session, None)
        # No trail, and not by omission: an empty one *keeps* whatever was
        # there, so a tool that had walked into a repository and then out of
        # one would go on showing the repository's path.
        return respond(
            content=text("%s is not inside a git repository." % folder),
            title=os.path.basename(folder.rstrip("/\\")) or folder,
            status="",
            menus=menus_of(options),
            commands=[],
        )

    branch, state = state_of(root)
    name = os.path.basename(root.rstrip("/\\")) or root
    # A named ref is looked at *instead of* the branch you are on, and the
    # trail says which — nothing is checked out, nothing moves.
    showing = ref or branch
    at = Where(root, showing, name)
    _at[context.session] = at

    graph = bool(setting(options, "graph"))
    asked = int(setting(options, "commits") or 1000)
    commits, more = log_of(
        root,
        asked,
        bool(setting(options, "allBranches")),
        graph,
        ref or "HEAD",
    )
    local = unpushed(root)
    for commit in commits:
        commit.local = commit.hash in local

    # The working tree at the head of the log, where Fork puts it and
    # where the eye goes first. Only when there is something in it: a row that
    # says "nothing has changed" is a row that has to be read to learn nothing.
    changes = working_tree(root) if ref is None else []
    if changes:
        staged = sum(1 for index, _, _ in changes if index not in (" ", "?"))
        commits.insert(
            0,
            Commit(
                WORKING,
                "",
                "",
                "",
                "Working tree — %d changed%s"
                % (len(changes), ", %d staged" % staged if staged else ""),
                [],
                [],
            ),
        )

    at.refs = refs_of(root)
    at.sidebar = bool(setting(options, "sidebar"))
    at.avatars = bool(setting(options, "avatars"))
    _log_cache[context.session] = commits
    if commits:
        select_commit(at, commits[0])
    count = sum(1 for commit in commits if commit.local)

    return respond(
        content=page_of(at, commits, graph),
        # The pill says which branch, so the name beside it is the repository —
        # and there is no trail: nothing in this tool is a place you walk into
        # and back out of any more.
        title=name,
        trail=[],
        status="%s — %s%s%s"
        % (
            showing if ref is None else "%s (looking, not checked out)" % showing,
            state,
            ", %d not pushed" % count if count else "",
            # Never silently. A history that goes on past the end of what is
            # drawn takes its forks with it.
            ", the newest %d — there are more" % asked if more else "",
        ),
        menus=menus_of(options),
        commands=[
            branch_pill(at),
            {"id": "refresh", "label": "Read it again", "icon": "refresh"},
        ],
    )


def redraw(session: str, at: Where) -> dict:
    """The page again, from what is already known.

    **Nothing here runs `git log`.** Moving down the log has to cost one call
    for the files and one for the difference, and no more: an arrow key held
    down would otherwise walk a history by starting a process per row.
    """
    options = _options.get(session, dict(plugin.settings))
    commits = _log_cache.get(session) or []
    return respond(
        content=page_of(at, commits, bool(setting(options, "graph"))),
        status=_detail_title(at),
    )


def _staged_mark(index: str) -> dict:
    """The first column of `status --porcelain`: is it in the next commit?"""
    if index in (" ", "?"):
        return cell("")
    return cell("staged: %s" % _STATUS.get(index, ("changed", index))[1],
                icon="staged")


def _mark_of(index: str, tree: str) -> dict:
    """The second: what the disk has that the index does not.

    Both marks are drawn when both have something to say. A file can be staged
    and then changed again, and that is the one state a person has to be told
    about rather than left to work out from two letters.
    """
    if index == "?" or tree == "?":
        return cell("never added", icon="untracked")
    if tree == " ":
        return cell("")
    return _mark(tree)


@plugin.view(VIEW_ID, "Git", "The log of the repository this folder is in.")
def git(context, event) -> dict:
    if event.kind == "open":
        return show(context)

    at = _at.get(context.session)

    # The cursor coming to rest, and a row being opened, mean the same thing on
    # this page: show me that one. There is nowhere to *go* — the log stays
    # where it is and the half underneath fills in — so Enter has nothing to
    # add that moving there did not already do.
    if event.kind in ("cursor", "activate") and at is not None:
        at_row = event.row
        if at_row is None or at_row < 0:
            return respond()

        # A ref in the sidebar is only followed when it is *asked* for. The
        # cursor passing over one on its way down the list is not a request to
        # re-read the whole history at it.
        if event.part == "refs":
            if event.kind != "activate":
                return respond()
            if at_row >= len(at.rows):
                return respond()
            name = at.rows[at_row]
            if name is None:
                return respond()
            return show(context, _options.get(context.session), name)

        if event.part == "log":
            commits = _log_cache.get(context.session) or []
            if at_row >= len(commits):
                return respond()
            select_commit(at, commits[at_row])
            return redraw(context.session, at)

        if event.part == "files":
            if at_row >= len(at.files) or at_row == at.file:
                return respond()
            select_file(at, at_row)
            return redraw(context.session, at)

        return respond()

    if event.kind == "answered" and at is not None:
        question = _pending.pop("%s:%s" % (context.session, event.id or ""), None)
        if question is None or not event.accepted:
            # No is an answer, and the honest thing is to say nothing happened
            # rather than to leave the view looking as though it might have.
            return respond(status="Nothing was changed.")

        kind, subject = question
        if kind == "checkout":
            done, said = do(at.root, "checkout", subject)
            if not done:
                return respond(actions=[notice(said)])
            return show(context, _options.get(context.session))

        if kind == "stage":
            done, said = do(at.root, "add", "--", subject)
        elif kind == "unstage":
            done, said = do(at.root, "restore", "--staged", "--", subject)
        elif kind == "discard":
            done, said = do(at.root, "restore", "--", subject)
        else:
            return respond()

        if not done:
            return respond(actions=[notice(said)])
        # The tree is a different tree now, and the whole page has to say so:
        # the row at the head of the log counts what has changed.
        return show(context, _options.get(context.session))

    if event.kind == "mark" and at is not None:
        # The secondary press sends the panel beside this one *into* the
        # commit. A commander already has two sides; the point of a tree at a
        # revision is having it next to the tree as it is now.
        at_row = event.row
        if event.part == "refs" and at_row is not None and 0 <= at_row < len(at.rows):
            name = at.rows[at_row]
            if name is None:
                return respond()
            return respond(
                actions=[navigate(tree_url(at.root, name))],
                status="%s — opened beside this one" % name,
            )

        if (event.part == "files" and at.hash == WORKING
                and at_row is not None and 0 <= at_row < len(at.files)):
            state, path = at.files[at_row]
            index = (state + "  ")[0]
            if index not in (" ", "?"):
                kind, title, confirm = (
                    "unstage",
                    "Take %s out of the next commit?" % path,
                    "Unstage",
                )
            else:
                kind, title, confirm = (
                    "stage",
                    "Put %s in the next commit?" % path,
                    "Stage",
                )
            _pending["%s:%s" % (context.session, kind)] = (kind, path)
            return respond(
                actions=[ask(kind, title, "In %s." % at.name, confirm=confirm)]
            )

        if event.part == "log" and at_row is not None and at_row >= 0:
            commits = _log_cache.get(context.session) or []
            if at_row < len(commits) and commits[at_row].hash != WORKING:
                return respond(
                    actions=[navigate(tree_url(at.root, commits[at_row].hash))],
                    status="%s — opened beside this one" % commits[at_row].short,
                )
        return respond()

    if event.kind == "button":
        options = _options.get(context.session, dict(plugin.settings))
        if event.id == "refresh":
            return show(context, options)

        # A ref picked out of the pill. Its own name rides on the id, so
        # nothing has to be remembered between the menu opening and a row of
        # it being chosen.
        if event.id and event.id.startswith("ref."):
            if at is None:
                return respond()
            return show(context, options, event.id[len("ref."):])

        # A ref picked out of the pill. Its own name rides on the id, so
        # nothing has to be remembered between the menu opening and a row of
        # it being chosen.
        if event.id and event.id.startswith("ref."):
            if at is None:
                return respond()
            return show(context, options, event.id[len("ref."):])

        if event.id == "checkout":
            if at is None:
                return respond()
            branch = at.branch
            current = (run(at.root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
            if not branch or branch == current:
                return respond(actions=[notice("You are on %s already." % branch)])

            dirty = run(at.root, "status", "--porcelain") or ""
            if dirty.strip():
                # **Refused, not negotiated.** Switching with a dirty tree is
                # where somebody else's merge conflict comes from, and the tool
                # that offered it is the one that gets blamed for it.
                return respond(
                    actions=[
                        notice(
                            "There are changes that are not committed. "
                            "Deal with them first — this will not decide for you."
                        )
                    ]
                )

            _pending["%s:checkout" % context.session] = ("checkout", branch)
            return respond(
                actions=[
                    ask(
                        "checkout",
                        "Switch to %s?" % branch,
                        "Your working tree is clean, so nothing will be lost.",
                        confirm="Switch",
                    )
                ]
            )

        if event.id == "browse":
            # Whatever the bottom half is pointed at decides which commit that
            # is, or the tip when it is pointed at nothing.
            if at is None:
                return respond()
            ref = at.hash if at.hash and at.hash != WORKING else "HEAD"
            return respond(
                actions=[navigate(tree_url(at.root, ref))],
                status="%s — opened beside this one" % (at.short or ref),
            )

        if event.id and event.id.startswith("toggle."):
            key = event.id.split(".", 1)[1]
            options = dict(options)
            options[key] = not bool(setting(options, key))
            return show(context, options)

    return respond()


@plugin.on_view_closed
def closed(view_id: str, session: str) -> None:
    _at.pop(session, None)
    _log_cache.pop(session, None)
    _options.pop(session, None)


plugin.run()
