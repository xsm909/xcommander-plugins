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
from urllib.parse import unquote, urlparse

from xcommander import Plugin, respond, table, text

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


def log_of(root: str, count: int, all_branches: bool, graph: bool) -> List[Commit]:
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
        "--all" if all_branches else "HEAD",
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


# -- the view ------------------------------------------------------------------


class Where:
    """What one open copy of the view is looking at.

    Three levels, and the trail at the top is how you walk back out of them —
    the same way back a panel has, which is the answer this application gives
    every view that goes into something.
    """

    __slots__ = ("root", "branch", "name", "level", "hash", "short", "subject",
                 "files")

    def __init__(self, root: str, branch: str, name: str) -> None:
        self.root = root
        self.branch = branch
        self.name = name
        self.level = "log"
        self.hash = ""
        self.short = ""
        self.subject = ""
        self.files: List[Tuple[str, str]] = []


#: What each open copy is looking at.
_at: Dict[str, Where] = {}

#: The rows the log last drew, so a press can say which commit it was. Kept
#: rather than re-read: the row index is only meaningful against the list the
#: user is actually looking at.
_log_cache: Dict[str, List[Commit]] = {}
_options: Dict[str, Dict[str, object]] = {}


def show(context, options: Optional[Dict[str, object]] = None) -> dict:
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
    _at[context.session] = Where(root, branch, name)

    commits = log_of(
        root,
        int(options.get("commits", 200) or 200),
        bool(options.get("allBranches", False)),
        bool(options.get("graph", True)),
    )
    local = unpushed(root)
    for commit in commits:
        commit.local = bool(commit.hash) and commit.hash in local

    _log_cache[context.session] = commits
    count = sum(1 for commit in commits if commit.local)

    return respond(
        content=content_of(commits, local),
        title="Git",
        trail=[name, branch],
        status="%s — %s%s"
        % (
            branch,
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


def show_file(at: Where, status: str, path: str) -> dict:
    """One file's difference in that commit."""
    at.level = "file"
    body = diff_of(at.root, at.hash, path)

    return respond(
        content=text(
            body or "Nothing to show for this file in this commit.",
            language="diff",
        ),
        trail=[at.name, at.branch, at.short, path],
        status="%s — %s in %s" % (path, _STATUS.get(status, status), at.short),
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
            return show_commit(context.session, at, commits[row])

        if at.level == "commit":
            if row >= len(at.files):
                return respond()
            status, path = at.files[row]
            return show_file(at, status, path)

        return respond()

    if event.kind == "step" and at is not None:
        # The trail is the way back, level by level: the repository and the
        # branch are the log, the hash is the commit it was opened from.
        if event.row is not None and event.row >= 2 and at.level == "file":
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
