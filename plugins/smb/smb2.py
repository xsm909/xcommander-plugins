"""A small SMB2 client, standard library only.

Enough of MS-SMB2 to browse and transfer: negotiate, authenticate, connect to a
share, list a directory, read, write, create, delete and rename. Nothing about
printers, pipes, oplocks, leases or notifications.

Dialects 2.0.2 and 2.1 only. Both sign with HMAC-SHA256, which Python has;
SMB 3.x signs with AES-CMAC, which it does not, and guessing at that would mean
a client that appears to work and quietly fails to protect anything.

Field offsets are the whole substance of this file. A structure packed one byte
short does not raise — the server closes the connection, or worse, answers
something plausible — so the layouts below are written out in full rather than
computed, and each is labelled with the section of MS-SMB2 it comes from.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import uuid
from typing import Dict, Iterator, List, Optional, Tuple

from ntlm import authenticate_message, negotiate_message, parse_challenge

# --- constants ---------------------------------------------------------------

SMB2_MAGIC = b"\xfeSMB"

NEGOTIATE = 0x0000
SESSION_SETUP = 0x0001
LOGOFF = 0x0002
TREE_CONNECT = 0x0003
TREE_DISCONNECT = 0x0004
CREATE = 0x0005
CLOSE = 0x0006
READ = 0x0008
WRITE = 0x0009
QUERY_DIRECTORY = 0x000E
SET_INFO = 0x0011

DIALECT_2_0_2 = 0x0202
DIALECT_2_1 = 0x0210

FLAGS_SIGNED = 0x00000008

STATUS_SUCCESS = 0x00000000
STATUS_MORE_PROCESSING_REQUIRED = 0xC0000016
STATUS_NO_MORE_FILES = 0x80000006
STATUS_NO_SUCH_FILE = 0xC000000F
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_END_OF_FILE = 0xC0000011

NOT_FOUND = {
    STATUS_NO_SUCH_FILE,
    STATUS_OBJECT_NAME_NOT_FOUND,
    STATUS_OBJECT_PATH_NOT_FOUND,
}

# Access rights (MS-SMB2 2.2.13.1.1)
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_LIST_DIRECTORY = 0x00000001

SHARE_READ = 0x00000001
SHARE_WRITE = 0x00000002
SHARE_DELETE = 0x00000004
SHARE_ALL = SHARE_READ | SHARE_WRITE | SHARE_DELETE

# Create dispositions
SUPERSEDE = 0
OPEN = 1
CREATE_NEW = 2
OPEN_IF = 3
OVERWRITE_IF = 5

# Create options
DIRECTORY_FILE = 0x00000001
NON_DIRECTORY_FILE = 0x00000040
DELETE_ON_CLOSE = 0x00001000

ATTR_HIDDEN = 0x00000002
ATTR_SYSTEM = 0x00000004
ATTR_DIRECTORY = 0x00000010
ATTR_NORMAL = 0x00000080
ATTR_REPARSE_POINT = 0x00000400

FILE_ID_BOTH_DIRECTORY_INFORMATION = 0x25
FILE_RENAME_INFORMATION = 10
FILE_DISPOSITION_INFORMATION = 13


class SmbError(Exception):
    """A failure the server described, or one the transport ran into."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def is_not_found(self) -> bool:
        return self.status in NOT_FOUND


def _status_text(status: int) -> str:
    known = {
        0xC000000D: "invalid parameter",
        0xC000000F: "no such file",
        0xC0000022: "access denied",
        0xC0000034: "name not found",
        0xC000003A: "path not found",
        0xC0000035: "name collision",
        0xC000006D: "logon failure",
        0xC000006E: "account restriction",
        0xC00000CC: "no such share",
        0xC0000101: "directory not empty",
        0xC0000103: "not a directory",
    }
    return known.get(status, f"status 0x{status:08X}")


def _windows_time(ticks: int) -> Optional[float]:
    """Windows 100-nanosecond ticks since 1601 → Unix seconds."""
    if ticks in (0, 0x7FFFFFFFFFFFFFFF):
        return None
    return ticks / 10_000_000 - 11644473600


class Smb2Connection:
    """One TCP connection, one session, and the trees opened on it."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        domain: str = "",
        port: int = 445,
        timeout: float = 20.0,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.domain = domain
        self.port = port
        self.timeout = timeout

        self._socket: Optional[socket.socket] = None
        self._message_id = 0
        self._session_id = 0
        self._session_key = b""
        self._signing_required = False
        self._dialect = 0
        self.dialect = 0
        self._last_header = b"\x00" * 64
        self._trees: Dict[str, int] = {}
        self.max_read = 65536
        self.max_write = 65536

    # --- transport ----------------------------------------------------------

    def connect(self) -> None:
        self._socket = socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        )
        self._negotiate()
        self._authenticate()

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            for tree_id in list(self._trees.values()):
                try:
                    self._call(TREE_DISCONNECT, struct.pack("<HH", 4, 0), tree_id=tree_id)
                except OSError:
                    pass
            if self._session_id:
                self._call(LOGOFF, struct.pack("<HH", 4, 0))
        except (OSError, SmbError):
            pass
        finally:
            try:
                self._socket.close()
            finally:
                self._socket = None
                self._trees.clear()
                self._session_id = 0

    def _send(self, data: bytes) -> None:
        assert self._socket is not None
        # Direct TCP transport: a four-byte length, top byte zero (MS-SMB2 2.1).
        self._socket.sendall(struct.pack(">I", len(data)) + data)

    def _receive_exactly(self, count: int) -> bytes:
        assert self._socket is not None
        chunks = []
        remaining = count
        while remaining:
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise SmbError("the server closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _receive(self) -> bytes:
        length = struct.unpack(">I", self._receive_exactly(4))[0] & 0x00FFFFFF
        return self._receive_exactly(length)

    # --- framing ------------------------------------------------------------

    def _header(self, command: int, tree_id: int = 0, credits: int = 64) -> bytes:
        # CreditCharge is reserved until a dialect has been agreed, so the
        # negotiate that agrees one must send zero. Windows resets the
        # connection rather than answering if it does not.
        charge = 0 if command == NEGOTIATE else 1
        # Post-increment: the first message on a connection is MessageId 0.
        # Starting at 1 is not a mild protocol slip — Windows resets the
        # connection without a word, which reads exactly like a malformed
        # packet and sends you looking in the wrong place for a long time.
        message_id = self._message_id
        self._message_id += 1
        return struct.pack(
            "<4sHHIHHIIQIIQ16s",
            SMB2_MAGIC,
            64,          # StructureSize
            charge,      # CreditCharge
            0,           # Status (zero in a request)
            command,
            credits,     # CreditRequest
            0,           # Flags — signing is added after the body is known
            0,           # NextCommand
            message_id,
            0,           # Reserved / PID
            tree_id,
            self._session_id,
            b"\x00" * 16,
        )

    def _sign(self, message: bytes) -> bytes:
        """HMAC-SHA256 over the whole message, signature field zeroed first."""
        header = bytearray(message[:64])
        flags = struct.unpack_from("<I", header, 16)[0] | FLAGS_SIGNED
        struct.pack_into("<I", header, 16, flags)
        header[48:64] = b"\x00" * 16
        body = bytes(header) + message[64:]
        signature = hmac.new(self._session_key, body, hashlib.sha256).digest()[:16]
        return body[:48] + signature + body[64:]

    def _call(
        self,
        command: int,
        body: bytes,
        tree_id: int = 0,
        expect: Tuple[int, ...] = (STATUS_SUCCESS,),
    ) -> Tuple[int, bytes]:
        message = self._header(command, tree_id=tree_id) + body
        if self._signing_required and self._session_key:
            message = self._sign(message)
        self._send(message)

        response = self._receive()
        if len(response) < 64 or not response.startswith(SMB2_MAGIC):
            raise SmbError("the reply was not an SMB2 message")
        # Kept whole: the session id is assigned in the header of the reply to
        # the first session setup, and the tree id in the reply to a tree
        # connect. Neither appears in the body.
        self._last_header = response[:64]
        status = struct.unpack_from("<I", response, 8)[0]
        if status not in expect:
            raise SmbError(_status_text(status), status)
        return status, response[64:]

    @property
    def _reply_session_id(self) -> int:
        return struct.unpack_from("<Q", self._last_header, 40)[0]

    @property
    def _reply_tree_id(self) -> int:
        return struct.unpack_from("<I", self._last_header, 36)[0]

    # --- handshake ----------------------------------------------------------

    def _negotiate(self) -> None:
        dialects = (DIALECT_2_1, DIALECT_2_0_2)
        body = struct.pack(
            "<HHHHI16sQ",
            36,                  # StructureSize
            len(dialects),
            0x0001,              # SecurityMode: signing enabled
            0,                   # Reserved
            0,                   # Capabilities
            uuid.uuid4().bytes,  # ClientGuid
            0,                   # ClientStartTime
        ) + b"".join(struct.pack("<H", d) for d in dialects)

        _, payload = self._call(NEGOTIATE, body)
        (
            _size,
            security_mode,
            self._dialect,
        ) = struct.unpack_from("<HHH", payload, 0)
        # Deliberately capped at one credit's worth. From SMB 2.1 a read or
        # write larger than 64 KB has to pay for itself in credits —
        # CreditCharge must be ceil(bytes / 65536) — and a request that asks
        # for more while charging one is refused with STATUS_INVALID_PARAMETER.
        # Keeping every operation inside a single credit avoids running a
        # credit ledger for a gain that does not show up in practice: the
        # caller's larger reads are simply served by several of these.
        one_credit = 65536
        self.max_read = min(struct.unpack_from("<I", payload, 32)[0], one_credit)
        self.max_write = min(struct.unpack_from("<I", payload, 36)[0], one_credit)
        # Bit 1 is SIGNING_REQUIRED. Windows sets it on every server share.
        self._signing_required = bool(security_mode & 0x0002)

        self.dialect = self._dialect
        if self._dialect not in (DIALECT_2_0_2, DIALECT_2_1):
            raise SmbError(
                f"the server offered dialect 0x{self._dialect:04X}; this client "
                "speaks 2.0.2 and 2.1 only"
            )

    def _session_setup(self, token: bytes) -> Tuple[int, bytes]:
        body = struct.pack(
            "<HBBIIHHQ",
            25,      # StructureSize
            0,       # Flags
            0x01,    # SecurityMode: signing enabled
            0,       # Capabilities
            0,       # Channel
            88,      # SecurityBufferOffset — 64 header + 24 of this structure
            len(token),
            0,       # PreviousSessionId
        ) + token

        status, payload = self._call(
            SESSION_SETUP,
            body,
            expect=(STATUS_SUCCESS, STATUS_MORE_PROCESSING_REQUIRED),
        )
        offset, length = struct.unpack_from("<HH", payload, 4)
        # The offset counts from the start of the message, body starts at 64.
        blob = payload[offset - 64 : offset - 64 + length]
        return status, blob

    def _authenticate(self) -> None:
        status, challenge_blob = self._session_setup(negotiate_message())
        if status != STATUS_MORE_PROCESSING_REQUIRED:
            raise SmbError("the server skipped the NTLM challenge")

        # The session id is assigned with the challenge and must be echoed from
        # here on, including on the message that completes the setup.
        self._session_id = self._reply_session_id
        challenge, target_info, _flags = parse_challenge(challenge_blob)
        token, session_key = authenticate_message(
            self.user, self.password, self.domain, challenge, target_info
        )
        # The key is armed only once the server has accepted the response.
        # Signing the message that carries it makes Windows reject the whole
        # setup with STATUS_INVALID_PARAMETER — the session it would be signed
        # against does not exist yet.
        self._session_setup(token)
        self._session_key = session_key

    # --- shares -------------------------------------------------------------

    def tree(self, share: str) -> int:
        existing = self._trees.get(share.lower())
        if existing is not None:
            return existing

        path = f"\\\\{self.host}\\{share}".encode("utf-16-le")
        body = struct.pack("<HHHH", 9, 0, 72, len(path)) + path
        self._call(TREE_CONNECT, body)
        tree_id = self._reply_tree_id
        self._trees[share.lower()] = tree_id
        return tree_id

    # --- files --------------------------------------------------------------

    def _create(
        self,
        tree_id: int,
        path: str,
        access: int,
        disposition: int,
        options: int,
        attributes: int = ATTR_NORMAL,
    ) -> Tuple[bytes, int, int, Optional[float]]:
        """Opens a path and returns (file id, attributes, size, modified)."""
        name = path.strip("\\").encode("utf-16-le")
        body = struct.pack(
            "<HBBIQQIIIIIHHII",
            57,          # StructureSize
            0,           # SecurityFlags
            0,           # RequestedOplockLevel
            2,           # ImpersonationLevel: Impersonation
            0,           # SmbCreateFlags
            0,           # Reserved
            access,
            attributes,
            SHARE_ALL,
            disposition,
            options,
            120,         # NameOffset — 64 header + 56 of this structure
            len(name),
            0,           # CreateContextsOffset
            0,           # CreateContextsLength
        ) + (name or b"\x00" * 0)
        # A zero-length name still needs the buffer to exist for the root.
        if not name:
            body += b"\x00"

        _, payload = self._call(CREATE, body, tree_id=tree_id)
        file_id = payload[64:80]
        attrs = struct.unpack_from("<I", payload, 56)[0]
        size = struct.unpack_from("<Q", payload, 48)[0]
        modified = _windows_time(struct.unpack_from("<Q", payload, 24)[0])
        return file_id, attrs, size, modified

    def _close(self, tree_id: int, file_id: bytes) -> None:
        body = struct.pack("<HHI16s", 24, 0, 0, file_id)
        try:
            self._call(CLOSE, body, tree_id=tree_id)
        except SmbError:
            # Closing is housekeeping; a failure here must not mask whatever
            # the caller was actually doing.
            pass

    def list_directory(self, share: str, path: str) -> List[dict]:
        tree_id = self.tree(share)
        file_id, _, _, _ = self._create(
            tree_id,
            path,
            FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            OPEN,
            DIRECTORY_FILE,
            ATTR_DIRECTORY,
        )
        try:
            return list(self._query_directory(tree_id, file_id))
        finally:
            self._close(tree_id, file_id)

    def _query_directory(self, tree_id: int, file_id: bytes) -> Iterator[dict]:
        pattern = "*".encode("utf-16-le")
        first = True
        while True:
            body = struct.pack(
                "<HBBI16sHHI",
                33,
                FILE_ID_BOTH_DIRECTORY_INFORMATION,
                0x01 if first else 0,   # RESTART_SCANS on the first call
                0,
                file_id,
                96,                     # FileNameOffset
                len(pattern),
                65536,                  # OutputBufferLength
            ) + pattern
            first = False

            status, payload = self._call(
                QUERY_DIRECTORY,
                body,
                tree_id=tree_id,
                expect=(STATUS_SUCCESS, STATUS_NO_MORE_FILES),
            )
            if status == STATUS_NO_MORE_FILES:
                return

            offset, length = struct.unpack_from("<HI", payload, 2)
            buffer = payload[offset - 64 : offset - 64 + length]
            yield from _parse_directory(buffer)

    def read(self, share: str, path: str, offset: int, length: int) -> bytes:
        tree_id = self.tree(share)
        file_id, _, size, _ = self._create(
            tree_id,
            path,
            FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            OPEN,
            NON_DIRECTORY_FILE,
        )
        try:
            out = bytearray()
            remaining = min(length, max(size - offset, 0))
            while remaining > 0:
                chunk = min(remaining, self.max_read)
                body = struct.pack(
                    "<HBBIQ16sIIIHHB",
                    49, 80, 0, chunk, offset + len(out), file_id,
                    0, 0, 0, 0, 0, 0,
                )
                try:
                    _, payload = self._call(READ, body, tree_id=tree_id)
                except SmbError as e:
                    if e.status == STATUS_END_OF_FILE:
                        break
                    raise
                data_offset = payload[2]
                data_length = struct.unpack_from("<I", payload, 4)[0]
                if not data_length:
                    break
                out += payload[data_offset - 64 : data_offset - 64 + data_length]
                remaining -= data_length
            return bytes(out)
        finally:
            self._close(tree_id, file_id)

    def write(self, share: str, path: str, data: bytes, append: bool = False) -> None:
        tree_id = self.tree(share)
        file_id, _, size, _ = self._create(
            tree_id,
            path,
            FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            OPEN_IF if append else OVERWRITE_IF,
            NON_DIRECTORY_FILE,
        )
        try:
            position = size if append else 0
            sent = 0
            while sent < len(data):
                chunk = data[sent : sent + self.max_write]
                body = struct.pack(
                    "<HHIQ16sIIHHI",
                    49, 112, len(chunk), position + sent, file_id, 0, 0, 0, 0, 0,
                ) + chunk
                self._call(WRITE, body, tree_id=tree_id)
                sent += len(chunk)
        finally:
            self._close(tree_id, file_id)

    def mkdir(self, share: str, path: str) -> None:
        tree_id = self.tree(share)
        file_id, _, _, _ = self._create(
            tree_id, path, FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            CREATE_NEW, DIRECTORY_FILE, ATTR_DIRECTORY,
        )
        self._close(tree_id, file_id)

    def delete(self, share: str, path: str, is_directory: bool) -> None:
        tree_id = self.tree(share)
        file_id, _, _, _ = self._create(
            tree_id,
            path,
            DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            OPEN,
            (DIRECTORY_FILE if is_directory else NON_DIRECTORY_FILE) | DELETE_ON_CLOSE,
            ATTR_DIRECTORY if is_directory else ATTR_NORMAL,
        )
        # DELETE_ON_CLOSE does the work; the disposition below is what makes it
        # stick on servers that ignore the create option.
        try:
            self._set_info(
                tree_id, file_id, FILE_DISPOSITION_INFORMATION, b"\x01"
            )
        except SmbError:
            pass
        self._close(tree_id, file_id)

    def rename(self, share: str, source: str, target: str) -> None:
        tree_id = self.tree(share)
        file_id, attrs, _, _ = self._create(
            tree_id, source, DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE, OPEN, 0
        )
        try:
            name = target.strip("\\").encode("utf-16-le")
            info = struct.pack("<B7xQI", 0, 0, len(name)) + name
            self._set_info(tree_id, file_id, FILE_RENAME_INFORMATION, info)
        finally:
            self._close(tree_id, file_id)

    def _set_info(
        self, tree_id: int, file_id: bytes, info_class: int, data: bytes
    ) -> None:
        body = struct.pack(
            "<HBBIHHI16s",
            33, 1, info_class, len(data), 96, 0, 0, file_id,
        ) + data
        self._call(SET_INFO, body, tree_id=tree_id)

    def stat(self, share: str, path: str) -> Optional[dict]:
        """One entry, or None when there is nothing there."""
        if not path.strip("\\"):
            return {"name": "", "directory": True, "size": 0, "modified": None}
        tree_id = self.tree(share)
        try:
            file_id, attrs, size, modified = self._create(
                tree_id, path, FILE_READ_ATTRIBUTES | SYNCHRONIZE, OPEN, 0
            )
        except SmbError as e:
            if e.is_not_found:
                return None
            raise
        self._close(tree_id, file_id)
        return {
            "name": path.rstrip("\\").split("\\")[-1],
            "directory": bool(attrs & ATTR_DIRECTORY),
            "size": 0 if attrs & ATTR_DIRECTORY else size,
            "modified": modified,
        }

    # --- shares -------------------------------------------------------------

    def shares(self) -> List[dict]:
        """Every share the server admits to, hidden ones included.

        Hidden here means only that the name ends in `$` — a convention, not a
        permission. They are listed because someone typing a share name is
        usually after exactly those, and hiding them would mean the picker is
        less useful than typing.
        """
        tree_id = self.tree("IPC$")
        file_id, _, _, _ = self._create(
            tree_id,
            "srvsvc",
            FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            OPEN,
            NON_DIRECTORY_FILE,
        )
        try:
            self._transceive(tree_id, file_id, _Rpc.bind())
            reply = self._transceive(
                tree_id, file_id, _Rpc.request(15, _share_enum_stub(self.host))
            )
            return _parse_share_enum(_Rpc.stub_of(reply))
        finally:
            self._close(tree_id, file_id)

    def _transceive(self, tree_id: int, file_id: bytes, data: bytes) -> bytes:
        """One request-and-reply over a named pipe (FSCTL_PIPE_TRANSCEIVE)."""
        body = struct.pack(
            "<HHI16sIIIIIIII",
            57, 0, FSCTL_PIPE_TRANSCEIVE, file_id,
            120,          # InputOffset — 64 header + 56 of this structure
            len(data),
            0,            # MaxInputResponse
            0,            # OutputOffset: nothing sent in the output buffer
            0,            # OutputCount
            65536,        # MaxOutputResponse
            1,            # Flags: IS_FSCTL
            0,
        ) + data
        # A reply too big for one message comes back as an overflow rather than
        # an error; the share list is small enough that the first is the whole.
        _, payload = self._call(
            IOCTL, body, tree_id=tree_id, expect=(STATUS_SUCCESS, 0x80000005)
        )
        out_offset, out_count = struct.unpack_from("<II", payload, 32)
        return payload[out_offset - 64 : out_offset - 64 + out_count]


def _parse_directory(buffer: bytes) -> Iterator[dict]:
    """FileIdBothDirectoryInformation records (MS-FSCC 2.4.17)."""
    offset = 0
    while offset < len(buffer):
        next_offset = struct.unpack_from("<I", buffer, offset)[0]
        modified = _windows_time(struct.unpack_from("<Q", buffer, offset + 24)[0])
        size = struct.unpack_from("<Q", buffer, offset + 40)[0]
        attributes = struct.unpack_from("<I", buffer, offset + 56)[0]
        name_length = struct.unpack_from("<I", buffer, offset + 60)[0]
        name = buffer[offset + 104 : offset + 104 + name_length].decode(
            "utf-16-le", "replace"
        )

        if name not in (".", ".."):
            yield {
                "name": name,
                "directory": bool(attributes & ATTR_DIRECTORY),
                "link": bool(attributes & ATTR_REPARSE_POINT),
                "hidden": bool(attributes & (ATTR_HIDDEN | ATTR_SYSTEM)),
                "size": 0 if attributes & ATTR_DIRECTORY else size,
                "modified": modified,
            }

        if next_offset == 0:
            return
        offset += next_offset


# --- share enumeration -------------------------------------------------------
#
# Shares are not files, so listing them is not a file operation: it is a remote
# procedure call, NetrShareEnum, spoken over DCERPC through the `srvsvc` named
# pipe on the IPC$ share. Everything below is that one call — the bind, the
# request, and enough NDR to marshal a level-1 enumeration and read it back.

IOCTL = 0x000B
FSCTL_PIPE_TRANSCEIVE = 0x0011C017

SRVSVC_UUID = uuid.UUID("4b324fc8-1670-01d3-1278-5a47bf6ee188")
NDR_UUID = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")

SHARE_TYPE_NAMES = {
    0x00000000: "disk",
    0x00000001: "printer",
    0x00000002: "device",
    0x00000003: "ipc",
}
SHARE_TYPE_MASK = 0x0000000F
SHARE_TYPE_SPECIAL = 0x80000000


def _ndr_string(text: str) -> bytes:
    """A conformant, varying string: max count, offset, actual count, chars."""
    data = (text + "\x00").encode("utf-16-le")
    count = len(text) + 1
    out = struct.pack("<III", count, 0, count) + data
    return out + b"\x00" * ((-len(out)) % 4)


def _read_ndr_string(buffer: bytes, offset: int) -> Tuple[str, int]:
    max_count, _, actual = struct.unpack_from("<III", buffer, offset)
    start = offset + 12
    text = buffer[start : start + actual * 2].decode("utf-16-le").rstrip("\x00")
    end = start + actual * 2
    return text, end + ((-(end - offset)) % 4 and (4 - (end - offset) % 4) or 0)


class _Rpc:
    """The two DCERPC messages this needs, and nothing more."""

    @staticmethod
    def bind() -> bytes:
        def syntax(identifier: uuid.UUID, major: int, minor: int) -> bytes:
            return identifier.bytes_le + struct.pack("<HH", major, minor)

        context = struct.pack("<HBB", 0, 1, 0) + syntax(SRVSVC_UUID, 3, 0) + syntax(
            NDR_UUID, 2, 0
        )
        body = struct.pack("<IIII", 4280, 4280, 0, 1)[:12] + struct.pack("<I", 1)
        body = struct.pack("<HHI", 4280, 4280, 0) + struct.pack("<I", 1) + context
        header = struct.pack(
            "<BBBBIHHI", 5, 0, 11, 0x03, 0x00000010, 16 + len(body), 0, 1
        )
        return header + body

    @staticmethod
    def request(opnum: int, stub: bytes, call_id: int = 2) -> bytes:
        body = struct.pack("<IHH", len(stub), 0, opnum) + stub
        header = struct.pack(
            "<BBBBIHHI", 5, 0, 0, 0x03, 0x00000010, 16 + len(body), 0, call_id
        )
        return header + body

    @staticmethod
    def stub_of(pdu: bytes) -> bytes:
        """The payload of a response PDU, past both headers."""
        if len(pdu) < 24:
            raise SmbError("the RPC reply was too short")
        return pdu[24:]


def _share_enum_stub(server: str) -> bytes:
    """NetrShareEnum, level 1, asking for everything (MS-SRVS 3.1.4.8)."""
    return (
        struct.pack("<I", 0x00020000) + _ndr_string(f"\\\\{server}")
        + struct.pack("<I", 1)              # Level
        + struct.pack("<I", 1)              # switch value
        + struct.pack("<I", 0x00020004)     # pointer to the container
        + struct.pack("<I", 0)              # EntriesRead
        + struct.pack("<I", 0)              # Buffer: NULL, the server fills it
        + struct.pack("<I", 0xFFFFFFFF)     # PreferedMaximumLength
        + struct.pack("<I", 0x00020008)     # pointer to ResumeHandle
        + struct.pack("<I", 0)
    )


def _parse_share_enum(stub: bytes) -> List[dict]:
    """The NDR that comes back from NetrShareEnum at level 1."""
    entries_read = struct.unpack_from("<I", stub, 12)[0]
    buffer_pointer = struct.unpack_from("<I", stub, 16)[0]
    if not entries_read or not buffer_pointer:
        return []

    count = struct.unpack_from("<I", stub, 20)[0]
    # Fixed part of the array first: three fields per share, strings after.
    records = []
    offset = 24
    for _ in range(min(count, entries_read)):
        name_pointer, share_type, remark_pointer = struct.unpack_from(
            "<III", stub, offset
        )
        records.append([name_pointer, share_type, remark_pointer])
        offset += 12

    shares = []
    for name_pointer, share_type, remark_pointer in records:
        name = ""
        remark = ""
        if name_pointer:
            name, offset = _read_ndr_string(stub, offset)
        if remark_pointer:
            remark, offset = _read_ndr_string(stub, offset)
        kind = SHARE_TYPE_NAMES.get(share_type & SHARE_TYPE_MASK, "other")
        shares.append(
            {
                "name": name,
                "kind": kind,
                "hidden": name.endswith("$"),
                "comment": remark,
            }
        )
    return shares
