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

"""NTLMv2 authentication, with no dependencies at all.

SMB needs NTLM, NTLM needs MD4, and MD4 is no longer something Python can be
asked for: OpenSSL 3 dropped it from the default provider, so
``hashlib.new("md4")`` raises on any current build. It is 60 lines, so it is
here rather than being a reason to depend on something.

Only the client half is implemented, and only NTLMv2 — the v1 exchange is
broken and servers that still accept it also accept this.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time
from typing import Optional, Tuple

# --- MD4 ---------------------------------------------------------------------


def _left_rotate(value: int, count: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def md4(message: bytes) -> bytes:
    """RFC 1320. Present only because NTLM's NT hash is MD4 of UTF-16 text."""
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476]

    padded = bytearray(message)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0)
    padded += struct.pack("<Q", len(message) * 8)

    for offset in range(0, len(padded), 64):
        block = struct.unpack("<16I", padded[offset : offset + 64])
        a, b, c, d = h

        # Round 1
        for i in range(16):
            k = i
            s = (3, 7, 11, 19)[i % 4]
            f = (b & c) | (~b & d)
            value = _left_rotate(a + f + block[k], s)
            a, b, c, d = d, value, b, c

        # Round 2
        for i in range(16):
            k = (i % 4) * 4 + i // 4
            s = (3, 5, 9, 13)[i % 4]
            f = (b & c) | (b & d) | (c & d)
            value = _left_rotate(a + f + block[k] + 0x5A827999, s)
            a, b, c, d = d, value, b, c

        # Round 3
        order = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)
        for i in range(16):
            k = order[i]
            s = (3, 9, 11, 15)[i % 4]
            f = b ^ c ^ d
            value = _left_rotate(a + f + block[k] + 0x6ED9EBA1, s)
            a, b, c, d = d, value, b, c

        h = [(x + y) & 0xFFFFFFFF for x, y in zip(h, (a, b, c, d))]

    return struct.pack("<4I", *h)


# --- NTLM --------------------------------------------------------------------

NTLMSSP = b"NTLMSSP\x00"

NEGOTIATE_UNICODE = 0x00000001
NEGOTIATE_SIGN = 0x00000010
NEGOTIATE_SEAL = 0x00000020
NEGOTIATE_NTLM = 0x00000200
NEGOTIATE_ALWAYS_SIGN = 0x00008000
NEGOTIATE_EXTENDED_SESSION_SECURITY = 0x00080000
NEGOTIATE_TARGET_INFO = 0x00800000
NEGOTIATE_128 = 0x20000000
NEGOTIATE_KEY_EXCHANGE = 0x40000000
NEGOTIATE_56 = 0x80000000

CLIENT_FLAGS = (
    NEGOTIATE_UNICODE
    | NEGOTIATE_NTLM
    | NEGOTIATE_SIGN
    | NEGOTIATE_ALWAYS_SIGN
    | NEGOTIATE_EXTENDED_SESSION_SECURITY
    | NEGOTIATE_TARGET_INFO
    | NEGOTIATE_128
    | NEGOTIATE_56
)


def nt_hash(password: str) -> bytes:
    return md4(password.encode("utf-16-le"))


def ntowfv2(user: str, password: str, domain: str) -> bytes:
    """The NTLMv2 key: HMAC-MD5 of upper-cased user plus domain."""
    return hmac.new(
        nt_hash(password),
        (user.upper() + domain).encode("utf-16-le"),
        hashlib.md5,
    ).digest()


def negotiate_message() -> bytes:
    """Type 1. Carries flags and nothing else worth sending."""
    return NTLMSSP + struct.pack(
        "<IIHHIHHI",
        1,
        CLIENT_FLAGS,
        0, 0, 0,  # domain: absent
        0, 0, 0,  # workstation: absent
    )


def parse_challenge(message: bytes) -> Tuple[bytes, bytes, int]:
    """Type 2 → (server challenge, target info, flags)."""
    if not message.startswith(NTLMSSP) or struct.unpack_from("<I", message, 8)[0] != 2:
        raise ValueError("not an NTLM challenge")

    challenge = message[24:32]
    flags = struct.unpack_from("<I", message, 20)[0]

    info_length, _, info_offset = struct.unpack_from("<HHI", message, 40)
    target_info = message[info_offset : info_offset + info_length]
    return challenge, target_info, flags


def _timestamp_from(target_info: bytes) -> bytes:
    """The server's clock, if it offered one.

    Servers that require message integrity reject a response whose timestamp is
    not the one they sent, so echoing theirs is not politeness.
    """
    offset = 0
    while offset + 4 <= len(target_info):
        av_id, av_len = struct.unpack_from("<HH", target_info, offset)
        value = target_info[offset + 4 : offset + 4 + av_len]
        if av_id == 0:  # MsvAvEOL
            break
        if av_id == 7:  # MsvAvTimestamp
            return value
        offset += 4 + av_len
    # Windows epoch, 100-nanosecond ticks.
    return struct.pack("<Q", int((time.time() + 11644473600) * 10_000_000))


def authenticate_message(
    user: str,
    password: str,
    domain: str,
    challenge: bytes,
    target_info: bytes,
    workstation: str = "XCOMMANDER",
    client_challenge: Optional[bytes] = None,
) -> Tuple[bytes, bytes]:
    """Type 3, plus the session key the caller needs for SMB signing."""
    key = ntowfv2(user, password, domain)
    client_challenge = client_challenge or os.urandom(8)

    blob = (
        struct.pack("<BBHI", 1, 1, 0, 0)
        + _timestamp_from(target_info)
        + client_challenge
        + struct.pack("<I", 0)
        + target_info
        + struct.pack("<I", 0)
    )
    proof = hmac.new(key, challenge + blob, hashlib.md5).digest()
    nt_response = proof + blob
    session_key = hmac.new(key, proof, hashlib.md5).digest()

    domain_bytes = domain.encode("utf-16-le")
    user_bytes = user.encode("utf-16-le")
    host_bytes = workstation.encode("utf-16-le")
    # LM is deliberately empty: NTLMv2 does not need it and sending one only
    # offers a weaker thing to attack.
    lm_response = b"\x00" * 24

    offset = 64
    parts = []
    fields = b""
    for payload in (lm_response, nt_response, domain_bytes, user_bytes, host_bytes, b""):
        fields += struct.pack("<HHI", len(payload), len(payload), offset)
        parts.append(payload)
        offset += len(payload)

    # The order in the message is domain, user, workstation, LM, NT, session key.
    header = (
        NTLMSSP
        + struct.pack("<I", 3)
        + struct.pack("<HHI", len(lm_response), len(lm_response), 64)
        + struct.pack(
            "<HHI", len(nt_response), len(nt_response), 64 + len(lm_response)
        )
        + struct.pack(
            "<HHI",
            len(domain_bytes),
            len(domain_bytes),
            64 + len(lm_response) + len(nt_response),
        )
        + struct.pack(
            "<HHI",
            len(user_bytes),
            len(user_bytes),
            64 + len(lm_response) + len(nt_response) + len(domain_bytes),
        )
        + struct.pack(
            "<HHI",
            len(host_bytes),
            len(host_bytes),
            64
            + len(lm_response)
            + len(nt_response)
            + len(domain_bytes)
            + len(user_bytes),
        )
        + struct.pack("<HHI", 0, 0, 64 + len(lm_response) + len(nt_response)
                      + len(domain_bytes) + len(user_bytes) + len(host_bytes))
        + struct.pack("<I", CLIENT_FLAGS)
    )

    message = (
        header
        + lm_response
        + nt_response
        + domain_bytes
        + user_bytes
        + host_bytes
    )
    return message, session_key
