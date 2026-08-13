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

**Nothing here writes, and nothing here reaches the network.** Whether a commit
is pushed is answered from what the last fetch left behind, because a tool that
opens a connection because you walked into a folder is a tool that hangs on a
folder you did not mean to open. Everything that changes a repository — staging,
committing, checking out — is a later segment, and it waits on the host learning
how to let a plugin ask a question first.
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from xcommander import (
    DIRECTORY,
    Entry,
    FILE,
    FileSystem,
    Plugin,
    RpcError,
    navigate,
    respond,
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
    """One row of the log — or one line of the drawing beside it.

    A row with no [hash] is the graph carrying on between commits: `git log
    --graph` prints those, and dropping them would take the lines apart.
    """

    __slots__ = ("hash", "short", "author", "date", "subject", "graph", "local")

    def __init__(self, hash: str, short: str, author: str, date: str,
                 subject: str, graph: str) -> None:
        self.hash = hash
        self.short = short
        self.author = author
        self.date = date
        self.subject = subject
        #: What git drew to the left of this line: `*`, `|`, `|\` and the rest.
        self.graph = graph
        #: True when no remote has it yet — the thing a log is read for as
        #: often as not.
        self.local = False


def log_of(root: str, count: int, all_branches: bool, graph: bool,
           ref: str = "HEAD") -> List[Commit]:
    """The log, with git's own drawing of its shape down the left.

    **The graph is git's, not ours.** Working out which lane a commit belongs
    in is a solved problem solved badly by everyone who solves it again, and
    `--graph` prints the answer. It also prints lines that are only the drawing
    — a merge fanning out takes a line of its own — and those are kept as rows
    with nothing in them but the picture, because throwing them away breaks the
    lines they are part of.
    """
    fields = FS.join(["%H", "%h", "%an", "%ad", "%s"])
    body = run(
        root,
        "log",
        *(["--graph"] if graph else []),
        "--all" if all_branches else ref,
        "--max-count=%d" % count,
        "--date=format:%Y-%m-%d %H:%M",
        "--pretty=format:" + fields,
    )
    if not body:
        return []

    rows: List[Commit] = []
    for line in body.splitlines():
        parts = line.split(FS)
        if len(parts) < 5:
            # Drawing only: no commit on this line.
            if line.strip():
                rows.append(Commit("", "", "", "", "", line.rstrip()))
            continue

        # The hash sits at the end of the first field; whatever is in front of
        # it is what git drew.
        head = parts[0]
        hash = head[-40:]
        rows.append(
            Commit(hash, parts[1], parts[2], parts[3], parts[4],
                   head[:-40].rstrip())
        )
    return rows


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

#: The mark on a commit no remote has.
LOCAL = "↑"


def rows_of(commits: List[Commit], local: set) -> List[List[str]]:
    return [
        [
            LOCAL if commit.hash and commit.hash in local else "",
            commit.graph,
            commit.short,
            commit.subject,
            commit.author,
            commit.date,
        ]
        for commit in commits
    ]


def content_of(commits: List[Commit], local: set) -> dict:
    return table(
        ["", "", "Commit", "Subject", "Author", "When"],
        rows_of(commits, local),
    )


def menus_of(options: Dict[str, object]) -> List[dict]:
    return [
        {
            "label": "Repository",
            "accelerator": "r",
            "items": [
                {"id": "refresh", "label": "Read it again", "shortcut": "F5"},
                {},
                {"id": "refs", "label": "Branches, tags and remotes"},
                {
                    "id": "browse",
                    "label": "Open this commit in the panel beside this one",
                },
                {},
                {
                    "id": "toggle.allBranches",
                    "label": "Every branch, not only this one",
                    "checked": bool(options.get("allBranches", False)),
                },
                {
                    "id": "toggle.graph",
                    "label": "Draw the shape of the history",
                    "checked": bool(options.get("graph", True)),
                },
            ],
        },
    ]


# -- one commit ----------------------------------------------------------------

#: What `git show --name-status` puts in front of a path, in words.
_STATUS = {
    "A": "added",
    "M": "changed",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "kind changed",
}


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


#: The row that stands for the working tree, at the head of the log. Not a
#: commit and deliberately shaped like one: it is where the eye goes first, and
#: it is what Git Extensions puts there.
WORKING = "working"


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


def message_of(root: str, hash: str) -> str:
    return (run(root, "show", "-s", "--pretty=format:%s", hash) or "").strip()


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

    Three levels, and the trail at the top is how you walk back out of them —
    the same way back a panel has, which is the answer this application gives
    every view that goes into something.
    """

    __slots__ = ("root", "branch", "name", "level", "hash", "short", "subject",
                 "files", "refs")

    def __init__(self, root: str, branch: str, name: str) -> None:
        self.root = root
        self.branch = branch
        self.name = name
        self.level = "log"
        self.hash = ""
        self.short = ""
        self.subject = ""
        self.files: List[Tuple[str, str]] = []
        #: The branches, tags and remotes as last listed, so a press knows
        #: which row it was.
        self.refs: List[Tuple[str, str, str, str]] = []


#: What each open copy is looking at.
_at: Dict[str, Where] = {}

#: The rows the log last drew, so a press can say which commit it was. Kept
#: rather than re-read: the row index is only meaningful against the list the
#: user is actually looking at.
_log_cache: Dict[str, List[Commit]] = {}
_options: Dict[str, Dict[str, object]] = {}


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
        return respond(
            content=text("%s is not inside a git repository." % folder),
            title="Git",
            trail=[os.path.basename(folder.rstrip("/\\")) or folder],
            status="",
            menus=menus_of(options),
        )

    branch, state = state_of(root)
    name = os.path.basename(root.rstrip("/\\")) or root
    # A named ref is looked at *instead of* the branch you are on, and the
    # trail says which — nothing is checked out, nothing moves.
    showing = ref or branch
    _at[context.session] = Where(root, showing, name)

    commits = log_of(
        root,
        int(options.get("commits", 200) or 200),
        bool(options.get("allBranches", False)),
        bool(options.get("graph", True)),
        ref or "HEAD",
    )
    local = unpushed(root)
    for commit in commits:
        commit.local = bool(commit.hash) and commit.hash in local

    # The working tree at the head of the log, where Git Extensions puts it and
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
                "",
            ),
        )

    _log_cache[context.session] = commits
    count = sum(1 for commit in commits if commit.local)

    return respond(
        content=content_of(commits, local),
        title="Git",
        trail=[name, showing],
        status="%s — %s%s"
        % (
            showing if ref is None else "%s (looking, not checked out)" % showing,
            state,
            ", %d not pushed" % count if count else "",
        ),
        menus=menus_of(options),
        commands=[{"id": "refresh", "label": "Read it again", "icon": "refresh"}],
    )


def show_commit(session: str, at: Where, commit: Commit) -> dict:
    """What one commit touched."""
    at.level = "commit"
    at.hash = commit.hash
    at.short = commit.short
    at.subject = commit.subject or message_of(at.root, commit.hash)
    at.files = files_of(at.root, commit.hash)

    if not at.files:
        # A merge that brought nothing of its own is git's own answer, and it
        # is worth saying rather than showing an empty table.
        return respond(
            content=text(
                "This commit changed nothing on its own. A merge usually has "
                "not: what came with it belongs to the commits it joined."
            ),
            trail=[at.name, at.branch, at.short],
            status="%s — %s · %s" % (at.short, at.subject, commit.author),
        )

    return respond(
        content=table(
            ["", "File"],
            [[_STATUS.get(status, status), path] for status, path in at.files],
        ),
        trail=[at.name, at.branch, at.short],
        status="%s — %s · %s · %d file%s"
        % (
            at.short,
            at.subject,
            commit.author,
            len(at.files),
            "" if len(at.files) == 1 else "s",
        ),
    )


def show_working(at: Where) -> dict:
    """What is changed and what is staged, right now."""
    at.level = "working"
    at.hash = WORKING
    at.short = "working tree"
    at.subject = ""
    changes = working_tree(at.root)
    at.files = [(index + tree, path) for index, tree, path in changes]

    if not changes:
        return respond(
            content=text("Nothing has changed since the last commit."),
            trail=[at.name, at.branch, at.short],
            status="clean",
        )

    staged = sum(1 for index, _, _ in changes if index not in (" ", "?"))
    return respond(
        content=table(
            ["", "File"],
            [[_worktree_state(index, tree), path] for index, tree, path in changes],
        ),
        trail=[at.name, at.branch, at.short],
        status="%d changed%s"
        % (len(changes), ", %d staged" % staged if staged else ""),
    )


def _worktree_state(index: str, tree: str) -> str:
    """The two columns of `status --porcelain` said out loud.

    Both sides are named when both have something to say: a file can be staged
    and then changed again, and "staged, changed since" is the one state a
    person has to be told about rather than left to work out from two letters.
    """
    if index == "?" or tree == "?":
        return "untracked"

    words = []
    if index != " ":
        words.append("staged: %s" % _STATUS.get(index, index))
    if tree != " ":
        words.append(
            "%s%s" % ("changed since" if words else "", "" if words else
                      _STATUS.get(tree, tree))
        )
    return ", ".join(word for word in words if word)


def show_refs(at: Where) -> dict:
    """Every branch, tag and remote there is, freshest first."""
    at.level = "refs"
    at.refs = refs_of(at.root)

    if not at.refs:
        return respond(
            content=text("This repository has no branches yet."),
            trail=[at.name, at.branch, "refs"],
            status="",
        )

    branches = sum(1 for kind, _, _, _ in at.refs if kind == "branch")
    tags = sum(1 for kind, _, _, _ in at.refs if kind == "tag")
    remotes = sum(1 for kind, _, _, _ in at.refs if kind == "remote")

    return respond(
        content=table(
            ["", "Name", "Last commit", "When"],
            [
                [
                    # The one you are on says so, because that is the first
                    # thing anybody looks for in this list.
                    "→" if kind == "branch" and name == at.branch else kind,
                    name,
                    subject,
                    when,
                ]
                for kind, name, subject, when in at.refs
            ],
        ),
        trail=[at.name, at.branch, "refs"],
        status="%d branch%s, %d tag%s, %d remote%s — press one to see its log, "
        "or press it with the other button to open it in the panel beside this"
        % (
            branches,
            "" if branches == 1 else "es",
            tags,
            "" if tags == 1 else "s",
            remotes,
            "" if remotes == 1 else "s",
        ),
    )


def show_file(at: Where, status: str, path: str) -> dict:
    """One file's difference — in a commit, or against what is committed."""
    at.level = "file"

    if at.hash == WORKING:
        body = worktree_diff(at.root, status[0], status[1:2] or " ", path)
        where = "in the working tree"
    else:
        body = diff_of(at.root, at.hash, path)
        where = "in %s" % at.short

    return respond(
        content=text(
            body or "Nothing to show for this file %s." % where,
            language="diff",
        ),
        trail=[at.name, at.branch, at.short, path],
        status="%s — %s" % (path, where),
    )


@plugin.view(VIEW_ID, "Git", "The log of the repository this folder is in.")
def git(context, event) -> dict:
    if event.kind == "open":
        return show(context)

    at = _at.get(context.session)

    if event.kind == "activate" and at is not None:
        row = event.row
        if row is None or row < 0:
            return respond()

        if at.level == "log":
            commits = _log_cache.get(context.session) or []
            if row >= len(commits) or not commits[row].hash:
                # A row that is only the graph carrying on. Nothing to open.
                return respond()
            if commits[row].hash == WORKING:
                return show_working(at)
            return show_commit(context.session, at, commits[row])

        if at.level == "refs":
            if row >= len(at.refs):
                return respond()
            return show(context, _options.get(context.session), at.refs[row][1])

        if at.level in ("commit", "working"):
            if row >= len(at.files):
                return respond()
            status, path = at.files[row]
            return show_file(at, status, path)

        return respond()

    if event.kind == "mark" and at is not None:
        # The secondary press sends the panel beside this one *into* the
        # commit. A commander already has two sides; the point of a tree at a
        # revision is having it next to the tree as it is now.
        row = event.row
        if at.level == "refs" and row is not None and 0 <= row < len(at.refs):
            name = at.refs[row][1]
            return respond(
                actions=[navigate(tree_url(at.root, name))],
                status="%s — opened beside this one" % name,
            )

        if at.level == "log" and row is not None and row >= 0:
            commits = _log_cache.get(context.session) or []
            if row < len(commits) and commits[row].hash not in ("", WORKING):
                return respond(
                    actions=[navigate(tree_url(at.root, commits[row].hash))],
                    status="%s — opened beside this one" % commits[row].short,
                )
        return respond()

    if event.kind == "step" and at is not None:
        # The trail is the way back, level by level: the repository and the
        # branch are the log, the hash is the commit it was opened from.
        if event.row is not None and event.row >= 2 and at.level == "file":
            if at.hash == WORKING:
                return show_working(at)
            commits = _log_cache.get(context.session) or []
            for commit in commits:
                if commit.hash == at.hash:
                    return show_commit(context.session, at, commit)
        at.level = "log"
        return show(context, _options.get(context.session))

    if event.kind == "button":
        options = _options.get(context.session, dict(plugin.settings))
        if event.id == "refresh":
            return show(context, options)
        if event.id == "refs":
            if at is None:
                return respond()
            return show_refs(at)
        if event.id == "browse":
            # Whatever level is open decides which commit that is: the one
            # being looked at, or the tip when the whole log is.
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
            options[key] = not bool(options.get(key))
            return show(context, options)

    return respond()


@plugin.on_view_closed
def closed(view_id: str, session: str) -> None:
    _at.pop(session, None)
    _log_cache.pop(session, None)
    _options.pop(session, None)


plugin.run()
