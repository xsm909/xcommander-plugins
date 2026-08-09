"""FTP transport for xcommander.

This is the reference example for the whole plugin idea: FTP is not built into
the core, it is a plugin that registers the ``ftp:`` scheme and implements the
same operations the local disk does. Everything is stdlib — no pip install.

URLs look like ``ftp://user:password@host:port/path``. Credentials typed in the
Go to dialog are carried in the URL; bookmarks are stored beside this file.
"""

from __future__ import annotations

import ftplib
import posixpath
import time
from typing import Dict, List, Optional

from xcommander import DIRECTORY, Entry, FILE, FileSystem, LINK, Plugin, Root, RpcError
from xcommander.fs import query_of, split_url


def _flag(options: Dict[str, str], key: str, default: bool) -> bool:
    value = options.get(key)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")

plugin = Plugin("org.xcommander.ftp", "FTP")

# Connections idle longer than this are reopened rather than trusted. The
# manifest offers it as a setting, so a server that hangs up sooner than the
# default assumes can be told about without editing this file.
DEFAULT_IDLE_TIMEOUT = 120.0


def idle_timeout() -> float:
    return float(plugin.setting("idleTimeout", DEFAULT_IDLE_TIMEOUT))


class _Connection:
    def __init__(self, ftp: ftplib.FTP):
        self.ftp = ftp
        self.touched = time.monotonic()

    def is_stale(self) -> bool:
        return time.monotonic() - self.touched > idle_timeout()


class FtpFileSystem(FileSystem):
    scheme = "ftp"

    def __init__(self):
        self._connections: Dict[tuple, _Connection] = {}

    # -- connection handling ----------------------------------------------

    def _connect(self, url: str):
        host, port, user, password, path = split_url(url)
        if not host:
            raise RpcError("No host in %s" % url)

        # Options the connection dialog collected ride along as query
        # parameters; the host has no idea what they mean, which is the point.
        options = query_of(url)
        passive = _flag(options, "passive", True)
        tls = _flag(options, "tls", False)
        anonymous = _flag(options, "anonymous", False)
        if anonymous:
            user, password = "anonymous", "anonymous@"

        key = (host, port or 21, user or "anonymous", tls)
        cached = self._connections.get(key)
        if cached is not None and not cached.is_stale():
            try:
                cached.ftp.voidcmd("NOOP")
                cached.touched = time.monotonic()
                return cached.ftp, path
            except ftplib.all_errors:
                self._connections.pop(key, None)

        # FTP_TLS speaks AUTH TLS; the plain class cannot be upgraded later.
        ftp = ftplib.FTP_TLS(timeout=30) if tls else ftplib.FTP(timeout=30)
        try:
            ftp.connect(host, port or 21)
            ftp.login(user or "anonymous", password or "anonymous@")
            if tls:
                # Without this the control channel is encrypted but the data
                # channel is not, which is worse than being told it failed.
                ftp.prot_p()
            ftp.set_pasv(passive)
        except ftplib.all_errors as failure:
            raise RpcError("Cannot connect to %s: %s" % (host, failure))

        self._connections[key] = _Connection(ftp)
        plugin.log(
            "Connected to %s as %s%s"
            % (host, user or "anonymous", " over TLS" if tls else "")
        )
        return ftp, path

    def close_all(self) -> None:
        for connection in self._connections.values():
            try:
                connection.ftp.quit()
            except ftplib.all_errors:
                try:
                    connection.ftp.close()
                except OSError:
                    pass
        self._connections.clear()

    # -- navigation --------------------------------------------------------

    def roots(self) -> List[Root]:
        # Saved connections belong to the host: it renders the dialog this
        # plugin describes and keeps them in ftp_connect.ini, so there is
        # nothing for the plugin to remember.
        return []

    def default_location(self) -> str:
        return "ftp:///"

    def list(self, url: str) -> List[Entry]:
        ftp, path = self._connect(url)
        try:
            return self._list_mlsd(ftp, path)
        except ftplib.error_perm:
            # MLSD is optional; fall back to parsing a Unix-style LIST.
            return self._list_lines(ftp, path)

    def _list_mlsd(self, ftp: ftplib.FTP, path: str) -> List[Entry]:
        entries = []
        for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
            if name in (".", ".."):
                continue
            kind_fact = facts.get("type", "file")
            if kind_fact in ("dir", "cdir", "pdir"):
                kind = DIRECTORY
            elif kind_fact.startswith("OS.unix=slink"):
                kind = LINK
            else:
                kind = FILE
            entries.append(
                Entry(
                    name=name,
                    kind=kind,
                    size=int(facts.get("size", 0) or 0),
                    modified=_parse_mlsd_time(facts.get("modify")),
                    hidden=name.startswith("."),
                )
            )
        return entries

    def _list_lines(self, ftp: ftplib.FTP, path: str) -> List[Entry]:
        lines: List[str] = []
        ftp.retrlines("LIST %s" % path, lines.append)

        entries = []
        for line in lines:
            parsed = _parse_list_line(line)
            if parsed is not None:
                entries.append(parsed)
        return entries

    def stat(self, url: str) -> Optional[Entry]:
        ftp, path = self._connect(url)
        name = posixpath.basename(path.rstrip("/")) or "/"

        # SIZE only works on files, and only in binary mode; a failure is the
        # cheapest way to learn the target is a directory.
        try:
            ftp.voidcmd("TYPE I")
            size = ftp.size(path)
        except ftplib.all_errors:
            size = None

        if size is not None:
            return Entry(name=name, kind=FILE, size=size, hidden=name.startswith("."))

        try:
            current = ftp.pwd()
            ftp.cwd(path)
            ftp.cwd(current)
            return Entry(name=name, kind=DIRECTORY, hidden=name.startswith("."))
        except ftplib.all_errors:
            return None

    # -- transfers ---------------------------------------------------------

    def read(self, url: str, offset: int, length: int) -> bytes:
        ftp, path = self._connect(url)
        ftp.voidcmd("TYPE I")
        connection = ftp.transfercmd("RETR %s" % path, rest=offset or None)
        try:
            buffer = bytearray()
            while len(buffer) < length:
                chunk = connection.recv(min(65536, length - len(buffer)))
                if not chunk:
                    break
                buffer.extend(chunk)
        finally:
            connection.close()
            # The server still owes a completion reply; it may be an error if we
            # closed early, which is expected for a partial read.
            try:
                ftp.voidresp()
            except ftplib.all_errors:
                pass
        return bytes(buffer)

    def write(self, url: str, data: bytes, mode: str) -> None:
        ftp, path = self._connect(url)
        ftp.voidcmd("TYPE I")
        command = "APPE" if mode == "append" else "STOR"
        connection = ftp.transfercmd("%s %s" % (command, path))
        try:
            connection.sendall(data)
        finally:
            connection.close()
            ftp.voidresp()

    def mkdir(self, url: str) -> None:
        ftp, path = self._connect(url)
        ftp.mkd(path)

    def delete(self, url: str) -> None:
        ftp, path = self._connect(url)
        try:
            ftp.delete(path)
        except ftplib.error_perm:
            self._remove_tree(ftp, path)

    def _remove_tree(self, ftp: ftplib.FTP, path: str) -> None:
        for name, facts in ftp.mlsd(path, facts=["type"]):
            if name in (".", ".."):
                continue
            child = posixpath.join(path, name)
            if facts.get("type") == "dir":
                self._remove_tree(ftp, child)
            else:
                ftp.delete(child)
        ftp.rmd(path)

    def rename(self, source: str, target: str) -> None:
        ftp, source_path = self._connect(source)
        _, _, _, _, target_path = split_url(target)
        ftp.rename(source_path, target_path)


def _parse_mlsd_time(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value[:14], "%Y%m%d%H%M%S"))
    except ValueError:
        return None


_MONTHS = {
    name: index + 1
    for index, name in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    )
}


def _parse_list_time(month: str, day: str, last: str) -> Optional[float]:
    """Reads the date from a Unix ``LIST`` line.

    The last column is either ``HH:MM`` for something recent or a year for
    anything older, and the year is simply missing in the first case — the
    convention is that it is within the last six months, so take the most
    recent such date rather than defaulting to this year and showing files
    dated in the future.
    """
    number = _MONTHS.get(month)
    if number is None:
        return None

    try:
        day_number = int(day)
        now = time.localtime()
        if ":" in last:
            hour, minute = (int(part) for part in last.split(":", 1))
            year = now.tm_year
            stamp = time.mktime(
                (year, number, day_number, hour, minute, 0, 0, 1, -1)
            )
            if stamp > time.time() + 86400:
                stamp = time.mktime(
                    (year - 1, number, day_number, hour, minute, 0, 0, 1, -1)
                )
            return stamp
        return time.mktime((int(last), number, day_number, 0, 0, 0, 0, 1, -1))
    except (ValueError, OverflowError):
        return None


def _parse_list_line(line: str) -> Optional[Entry]:
    """Parses one line of a Unix-style ``LIST`` response.

    Servers vary; anything that does not parse is skipped rather than guessed at.
    """
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None

    permissions, name = parts[0], parts[8]
    if name in (".", ".."):
        return None

    if permissions.startswith("d"):
        kind = DIRECTORY
    elif permissions.startswith("l"):
        kind = LINK
        name = name.split(" -> ")[0]
    else:
        kind = FILE

    try:
        size = int(parts[4])
    except ValueError:
        size = 0

    return Entry(
        name=name,
        kind=kind,
        size=size,
        modified=_parse_list_time(parts[5], parts[6], parts[7]),
        hidden=name.startswith("."),
    )


filesystem = plugin.add_filesystem(FtpFileSystem())


@plugin.command("ftp.disconnect", "Disconnect all FTP sessions")
def disconnect_all(_args):
    filesystem.close_all()
    return {"ok": True}


@plugin.on_shutdown
def disconnect():
    filesystem.close_all()


@plugin.on_settings_changed
def apply_settings(settings):
    # Open control connections were opened under the old rules; dropping them
    # is cheaper than reasoning about which of them the new ones still allow.
    filesystem.close_all()
    plugin.log("Idle timeout is now %ss" % idle_timeout())


plugin.run()
