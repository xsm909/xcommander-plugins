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

"""Opening a PDF that was locked with the empty password.

Most encrypted PDFs are not secret. They carry an owner password that forbids
printing or copying and a *user* password that is empty, so every reader opens
them without asking anybody anything — and a viewer that refused them would be
refusing files their owners can already read in a browser.

**RC4 and AES are written out here, in Python, because the standard library has
neither.** `hashlib` brings MD5, SHA-256, SHA-384 and SHA-512, which is the
whole of the key derivation; what is missing is the ciphers themselves. RC4 is
twenty lines. AES decryption is a table and four steps, and it is used on a page
of text at a time, so its speed is beside the point.

Nothing here attacks anything: a file whose user password is not empty is
reported as locked and that is the end of it.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Dict, List, Optional, Set

#: The string the specification pads a password with, in full.
PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56,
    0xFF, 0xFA, 0x01, 0x08, 0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80,
    0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A,
])


class Locked(Exception):
    """The file needs a password this viewer was not given."""


# -- RC4 -------------------------------------------------------------------


def rc4(key: bytes, data: bytes) -> bytes:
    state = list(range(256))
    j = 0
    length = len(key)
    if length == 0:
        return data
    for i in range(256):
        j = (j + state[i] + key[i % length]) & 0xFF
        state[i], state[j] = state[j], state[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        out[n] = byte ^ state[(state[i] + state[j]) & 0xFF]
    return bytes(out)


# -- AES -------------------------------------------------------------------

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D, 0x9A]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


_MUL: Dict[int, bytes] = {}
for _f in (9, 11, 13, 14):
    _table = bytearray(256)
    for _x in range(256):
        _a, _b, _r = _x, _f, 0
        while _b:
            if _b & 1:
                _r ^= _a
            _a = _xtime(_a)
            _b >>= 1
        _table[_x] = _r
    _MUL[_f] = bytes(_table)


def _expand_key(key: bytes) -> List[List[int]]:
    """The round keys, as a list of 16-byte rounds."""
    nk = len(key) // 4
    rounds = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [sum(words[4 * r:4 * r + 4], []) for r in range(rounds + 1)]


def _decrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    state = [block[i] ^ round_keys[-1][i] for i in range(16)]
    mul9, mul11, mul13, mul14 = _MUL[9], _MUL[11], _MUL[13], _MUL[14]
    for r in range(len(round_keys) - 2, -1, -1):
        # Inverse shift rows.
        shifted = [0] * 16
        for column in range(4):
            for row in range(4):
                shifted[((column + row) % 4) * 4 + row] = state[column * 4 + row]
        # Inverse substitution, then the round key.
        key = round_keys[r]
        state = [_INV_SBOX[shifted[i]] ^ key[i] for i in range(16)]
        if r == 0:
            break
        # Inverse mix columns.
        mixed = [0] * 16
        for column in range(4):
            a0, a1, a2, a3 = state[column * 4:column * 4 + 4]
            mixed[column * 4 + 0] = mul14[a0] ^ mul11[a1] ^ mul13[a2] ^ mul9[a3]
            mixed[column * 4 + 1] = mul9[a0] ^ mul14[a1] ^ mul11[a2] ^ mul13[a3]
            mixed[column * 4 + 2] = mul13[a0] ^ mul9[a1] ^ mul14[a2] ^ mul11[a3]
            mixed[column * 4 + 3] = mul11[a0] ^ mul13[a1] ^ mul9[a2] ^ mul14[a3]
        state = mixed
    return bytes(state)


def _encrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    state = [block[i] ^ round_keys[0][i] for i in range(16)]
    last = len(round_keys) - 1
    for r in range(1, last + 1):
        state = [_SBOX[b] for b in state]
        shifted = [0] * 16
        for column in range(4):
            for row in range(4):
                shifted[column * 4 + row] = state[((column + row) % 4) * 4 + row]
        state = shifted
        if r != last:
            mixed = [0] * 16
            for column in range(4):
                a0, a1, a2, a3 = state[column * 4:column * 4 + 4]
                mixed[column * 4 + 0] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
                mixed[column * 4 + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
                mixed[column * 4 + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
                mixed[column * 4 + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)
            state = mixed
        key = round_keys[r]
        state = [state[i] ^ key[i] for i in range(16)]
    return bytes(state)


def aes_cbc_decrypt(key: bytes, data: bytes) -> bytes:
    """AES-CBC with the initialisation vector in front, as PDF stores it."""
    if len(data) <= 16:
        return b""
    round_keys = _expand_key(key)
    previous = data[:16]
    body = data[16:]
    body = body[:len(body) - len(body) % 16]
    out = bytearray()
    for offset in range(0, len(body), 16):
        block = body[offset:offset + 16]
        plain = _decrypt_block(block, round_keys)
        out += bytes(a ^ b for a, b in zip(plain, previous))
        previous = block
    if out:
        padding = out[-1]
        if 1 <= padding <= 16 and len(out) >= padding:
            del out[len(out) - padding:]
    return bytes(out)


def aes_ecb_no_padding(key: bytes, data: bytes) -> bytes:
    """AES-CBC with a zero vector and no padding — what revision 6 hashes with."""
    round_keys = _expand_key(key)
    out = bytearray()
    previous = b"\0" * 16
    for offset in range(0, len(data) - 15, 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], previous))
        cipher = _encrypt_block(block, round_keys)
        out += cipher
        previous = cipher
    return bytes(out)


def _aes_cbc_encrypt_no_padding(key: bytes, iv: bytes, data: bytes) -> bytes:
    round_keys = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data) - 15, 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], previous))
        previous = _encrypt_block(block, round_keys)
        out += previous
    return bytes(out)


# -- the standard security handler ----------------------------------------


def _hash_r6(password: bytes, salt: bytes, extra: bytes) -> bytes:
    """The iterated hash revision 6 introduced (ISO 32000-2, 7.6.4.3.4)."""
    k = hashlib.sha256(password + salt + extra).digest()
    i = 0
    while True:
        k1 = (password + k + extra) * 64
        e = _aes_cbc_encrypt_no_padding(k[:16], k[16:32], k1)
        modulo = sum(e[:16]) % 3
        if modulo == 0:
            k = hashlib.sha256(e).digest()
        elif modulo == 1:
            k = hashlib.sha384(e).digest()
        else:
            k = hashlib.sha512(e).digest()
        i += 1
        if i >= 64 and e[-1] <= i - 32:
            break
    return k[:32]


class Decryptor:
    """Turns the `/Encrypt` dictionary into "here is the key for object N"."""

    def __init__(self, encrypt: Dict[str, Any], first_id: bytes, password: bytes = b""):
        self.skip: Set[int] = set()
        filter_name = str(encrypt.get("Filter") or "")
        if filter_name and filter_name != "Standard":
            raise Locked(
                "This file is locked by %s, which is not the standard scheme "
                "and cannot be opened here." % filter_name
            )
        self.v = int(encrypt.get("V") or 0)
        self.r = int(encrypt.get("R") or 0)
        length = int(encrypt.get("Length") or 40)
        owner = _as_bytes(encrypt.get("O"))
        user = _as_bytes(encrypt.get("U"))
        permissions = int(encrypt.get("P") or 0) & 0xFFFFFFFF
        self.encrypt_metadata = encrypt.get("EncryptMetadata", True) is not False

        # Which cipher, and with what key length. From version 4 onwards the
        # answer is in a named crypt filter rather than in the dictionary.
        self.method = "RC4"
        if self.v >= 4:
            crypt_filters = encrypt.get("CF") or {}
            chosen = str(encrypt.get("StmF") or "Identity")
            self.string_filter = str(encrypt.get("StrF") or "Identity")
            entry = crypt_filters.get(chosen) if isinstance(crypt_filters, dict) else None
            if chosen == "Identity":
                self.method = "Identity"
            elif isinstance(entry, dict):
                cfm = str(entry.get("CFM") or "V2")
                self.method = {"AESV2": "AES", "AESV3": "AES", "None": "Identity"}.get(cfm, "RC4")
                if "Length" in entry:
                    entry_length = int(entry.get("Length") or 0)
                    length = entry_length * 8 if entry_length <= 64 else entry_length
        else:
            self.string_filter = ""

        if self.r >= 5:
            self.key = self._key_r5(password, owner, user, encrypt)
            self.method = "AES"
        else:
            self.key = self._key_r4(password, owner, permissions, first_id, length)
            if not self._user_password_matches(user, first_id):
                raise Locked(
                    "This file is locked with a password. Open it in a reader "
                    "that can ask for one."
                )
        self.key_length = len(self.key)

    # -- keys --------------------------------------------------------------

    def _key_r4(self, password: bytes, owner: bytes, permissions: int,
                first_id: bytes, length: int) -> bytes:
        padded = (password + PAD)[:32]
        digest = hashlib.md5()
        digest.update(padded)
        digest.update(owner[:32])
        digest.update(struct.pack("<I", permissions))
        digest.update(first_id)
        if self.r >= 4 and not self.encrypt_metadata:
            digest.update(b"\xff\xff\xff\xff")
        key = digest.digest()
        size = max(5, min(16, length // 8)) if self.r >= 3 else 5
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:size]).digest()
        return key[:size]

    def _user_password_matches(self, user: bytes, first_id: bytes) -> bool:
        if not user:
            return True  # nothing to check against; try to read it anyway
        if self.r == 2:
            return rc4(self.key, PAD) == user[:32]
        digest = hashlib.md5(PAD + first_id).digest()
        value = rc4(self.key, digest)
        for i in range(1, 20):
            value = rc4(bytes(b ^ i for b in self.key), value)
        return value[:16] == user[:16]

    def _key_r5(self, password: bytes, owner: bytes, user: bytes,
                encrypt: Dict[str, Any]) -> bytes:
        """Revision 5 and 6: the key is wrapped in `/UE`, not derived."""
        password = password[:127]
        validation, salt = user[32:40], user[40:48]
        if self.r == 5:
            check = hashlib.sha256(password + validation).digest()
        else:
            check = _hash_r6(password, validation, b"")
        if user and check != user[:32]:
            raise Locked(
                "This file is locked with a password. Open it in a reader "
                "that can ask for one."
            )
        if self.r == 5:
            intermediate = hashlib.sha256(password + salt).digest()
        else:
            intermediate = _hash_r6(password, salt, b"")
        wrapped = _as_bytes(encrypt.get("UE"))
        if len(wrapped) < 32:
            raise Locked("This file's encryption key is missing or damaged.")
        return _aes_cbc_decrypt_no_padding(intermediate, b"\0" * 16, wrapped[:32])

    def _object_key(self, num: int, gen: int) -> bytes:
        if self.r >= 5:
            return self.key
        extra = struct.pack("<I", num)[:3] + struct.pack("<I", gen)[:2]
        if self.method == "AES":
            extra += b"sAlT"
        key = hashlib.md5(self.key + extra).digest()
        return key[:min(len(self.key) + 5, 16)]

    # -- using them --------------------------------------------------------

    def decrypt_bytes(self, data: bytes, num: int, gen: int) -> bytes:
        if self.method == "Identity" or not data:
            return data
        key = self._object_key(num, gen)
        if self.method == "AES":
            return aes_cbc_decrypt(key, data)
        return rc4(key, data)

    def decrypt_stream(self, data: bytes, num: int, gen: int,
                       stream_dict: Dict[str, Any], resolve) -> bytes:
        kind = resolve(stream_dict.get("Type"))
        if str(kind or "") == "XRef":
            return data  # never encrypted: it says where the key itself is
        if str(kind or "") == "Metadata" and not self.encrypt_metadata:
            return data
        filters = resolve(stream_dict.get("Filter"))
        names = filters if isinstance(filters, list) else [filters]
        if any(str(resolve(n) or "") == "Crypt" for n in names):
            return data
        return self.decrypt_bytes(data, num, gen)

    def decrypt_strings(self, value: Any, num: int, gen: int) -> Any:
        """Every string inside one object, in place — dictionaries and all."""
        if isinstance(value, bytes):
            return self.decrypt_bytes(value, num, gen)
        if isinstance(value, list):
            return [self.decrypt_strings(item, num, gen) for item in value]
        if isinstance(value, dict):
            return {k: self.decrypt_strings(v, num, gen) for k, v in value.items()}
        if hasattr(value, "dict") and hasattr(value, "raw"):
            value.dict = self.decrypt_strings(value.dict, num, gen)
            return value
        return value


def _aes_cbc_decrypt_no_padding(key: bytes, iv: bytes, data: bytes) -> bytes:
    round_keys = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data) - 15, 16):
        block = data[offset:offset + 16]
        plain = _decrypt_block(block, round_keys)
        out += bytes(a ^ b for a, b in zip(plain, previous))
        previous = block
    return bytes(out)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("latin-1", "replace")
    return b""


__all__ = ["Decryptor", "Locked", "aes_cbc_decrypt", "rc4"]
