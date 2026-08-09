"""SMB2 as an xcommander file system.

The protocol itself lives in `smb2.py`; this file is the adapter between it and
what the host asks for. There is no third-party code here or beside it: SMB has
no equivalent of `ftplib` in the standard library, so the client is ours, and
it is deliberately limited to dialects 2.0.2 and 2.1 — those sign with
HMAC-SHA256, which Python can do, while SMB 3.x needs AES that it cannot.

A URL is `smb://user@host/share/path`. The share is simply the first segment:
SMB has no concept of a path that spans shares, so nothing is lost by treating
it as an ordinary part of the path, and connections stay one field shorter.
"""

from __future__ import annotations

import posixpath
import time
from typing import Dict, List, Optional, Tuple

from xcommander import DIRECTORY, Entry, FILE, FileSystem, LINK, Plugin, Root, RpcError
from xcommander.fs import query_of, split_url

from smb2 import SmbError, Smb2Connection

plugin = Plugin("org.xcommander.smb")

#: Connections are dropped after this long unused. SMB servers close idle
#: sessions themselves, and reconnecting is cheap next to holding one open.
IDLE_SECONDS = 120


class _Session:
    def __init__(self, connection: Smb2Connection):
        self.connection = connection
        self.touched = time.monotonic()

    @property
    def is_stale(self) -> bool:
        return time.monotonic() - self.touched > IDLE_SECONDS


def _require_share(share: str, url: str) -> None:
    """Shares are not files: nothing but listing works at the server level."""
    if not share:
        raise RpcError("%s names no share — an SMB path starts with one" % url)


def _split_share(path: str) -> Tuple[str, str]:
    """`/documents/reports/q3` → (`documents`, `reports\\q3`)."""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return "", ""
    return parts[0], "\\".join(parts[1:])


class SmbFileSystem(FileSystem):
    scheme = "smb"

    def __init__(self):
        self._sessions: Dict[tuple, _Session] = {}

    # -- connection handling ----------------------------------------------

    def _connect(self, url: str) -> Tuple[Smb2Connection, str, str]:
        host, port, user, password, path = split_url(url)
        if not host:
            raise RpcError("No server in %s" % url)

        options = query_of(url)
        domain = options.get("domain", "")
        share, inner = _split_share(path)

        key = (host, port or 445, user or "", domain)
        cached = self._sessions.get(key)
        if cached is not None and not cached.is_stale:
            cached.touched = time.monotonic()
            return cached.connection, share, inner
        if cached is not None:
            self._drop(key)

        connection = Smb2Connection(
            host, user or "", password or "", domain=domain, port=port or 445
        )
        try:
            connection.connect()
        except (SmbError, OSError) as failure:
            raise RpcError("Cannot connect to %s: %s" % (host, failure))

        self._sessions[key] = _Session(connection)
        plugin.log(
            "Connected to %s as %s (SMB 2.%d)"
            % (host, user or "guest", (connection.dialect >> 4) & 0xF)
        )
        return connection, share, inner

    def _drop(self, key: tuple) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            try:
                session.connection.close()
            except (SmbError, OSError):
                pass

    def close_all(self) -> None:
        for key in list(self._sessions):
            self._drop(key)

    # -- navigation --------------------------------------------------------

    def roots(self) -> List[Root]:
        # Saved connections belong to the host: it renders the form this plugin
        # describes and keeps them in smb_connect.ini.
        return []

    def default_location(self) -> str:
        return "smb:///"

    def list(self, url: str) -> List[Entry]:
        connection, share, path = self._connect(url)

        # No share yet: the server itself is the directory, and its shares are
        # what is in it. Browsing to one is then an ordinary step into a
        # folder, and the round trip doubles as proof the credentials work.
        if not share:
            try:
                shares = connection.shares()
            except SmbError as failure:
                raise RpcError("Cannot list the shares on %s: %s"
                               % (connection.host, failure))
            return [
                Entry(name=row["name"], kind=DIRECTORY, hidden=row["hidden"])
                for row in shares
                if row["kind"] == "disk"
            ]

        try:
            rows = connection.list_directory(share, path)
        except SmbError as failure:
            raise RpcError("Cannot list %s: %s" % (url, failure))

        entries = []
        for row in rows:
            kind = DIRECTORY if row["directory"] else FILE
            if row.get("link"):
                kind = LINK
            entries.append(
                Entry(
                    name=row["name"],
                    kind=kind,
                    size=row["size"],
                    modified=row["modified"],
                    hidden=row.get("hidden", False),
                )
            )
        return entries

    def stat(self, url: str) -> Optional[Entry]:
        connection, share, path = self._connect(url)
        if not share:
            return Entry(name=connection.host, kind=DIRECTORY)
        try:
            row = connection.stat(share, path)
        except SmbError as failure:
            raise RpcError("Cannot read %s: %s" % (url, failure))
        if row is None:
            return None
        return Entry(
            name=row["name"] or share,
            kind=DIRECTORY if row["directory"] else FILE,
            size=row["size"],
            modified=row["modified"],
        )

    # -- transfer ----------------------------------------------------------

    def read(self, url: str, offset: int, length: int) -> bytes:
        connection, share, path = self._connect(url)
        _require_share(share, url)
        try:
            return connection.read(share, path, offset, length)
        except SmbError as failure:
            raise RpcError("Cannot read %s: %s" % (url, failure))

    def write(self, url: str, data: bytes, mode: str) -> None:
        connection, share, path = self._connect(url)
        _require_share(share, url)
        try:
            connection.write(share, path, data, append=(mode == "append"))
        except SmbError as failure:
            raise RpcError("Cannot write %s: %s" % (url, failure))

    # -- changes -----------------------------------------------------------

    def mkdir(self, url: str) -> None:
        connection, share, path = self._connect(url)
        _require_share(share, url)
        try:
            connection.mkdir(share, path)
        except SmbError as failure:
            raise RpcError("Cannot create %s: %s" % (url, failure))

    def delete(self, url: str) -> None:
        connection, share, path = self._connect(url)
        _require_share(share, url)
        try:
            row = connection.stat(share, path)
            if row is None:
                raise RpcError("%s does not exist" % url)
            if row["directory"]:
                self._delete_tree(connection, share, path)
            else:
                connection.delete(share, path, False)
        except SmbError as failure:
            raise RpcError("Cannot delete %s: %s" % (url, failure))

    def _delete_tree(self, connection: Smb2Connection, share: str, path: str) -> None:
        """Depth first: SMB refuses to remove a directory with anything in it."""
        for row in connection.list_directory(share, path):
            child = "\\".join(p for p in (path, row["name"]) if p)
            if row["directory"]:
                self._delete_tree(connection, share, child)
            else:
                connection.delete(share, child, False)
        connection.delete(share, path, True)

    def rename(self, source: str, target: str) -> None:
        connection, share, path = self._connect(source)
        _, target_share, target_path = self._connect(target)
        if target_share.lower() != share.lower():
            raise RpcError("SMB cannot rename across shares; copy instead")
        try:
            connection.rename(share, path, target_path)
        except SmbError as failure:
            raise RpcError("Cannot rename %s: %s" % (source, failure))


smb = plugin.add_filesystem(SmbFileSystem())


@plugin.command("smb.disconnect", "Disconnect all SMB sessions")
def disconnect_all(_args):
    smb.close_all()
    return {"ok": True}


@plugin.on_shutdown
def disconnect():
    smb.close_all()


plugin.run()
