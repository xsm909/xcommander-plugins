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

"""A name written before anybody agreed which code page to use.

Both formats have the problem and neither records the answer. A ZIP from before
UTF-8 says its names are code page 437 and means whatever the machine that wrote
them was set to; a tar says nothing at all and holds bytes. The archivers that
shipped in Russia wrote code page 866, Windows tools wrote 1251, and read the
wrong way round a file cannot be found again — which is not a cosmetic matter.

What is shared is the *deciding*, so it lives here and each format brings its own
bytes. Nothing is guessed for a name that says what it is.
"""

from __future__ import annotations

from typing import Optional

#: How a name with nothing declaring its encoding should be read.
AUTO = "auto"
OEM = "oem"
WINDOWS = "windows"
LITERAL = "literal"

#: The readings worth trying, in the order they are preferred on a tie.
#:
#: UTF-8 is in the list and is first because plenty of archivers wrote UTF-8
#: names without setting the flag that says so — the `zip` on every Linux box
#: does — and because a run of code-page bytes almost never happens to be valid
#: UTF-8, so a strict decode that succeeds is strong evidence on its own.
CANDIDATES = ("utf-8", "cp866", "cp1251")


def plausibility(text: str) -> int:
    """How much this reads like a file name somebody typed.

    A wrongly decoded name is not gibberish in any obvious way — 866 and 1251
    turn the same bytes into Cyrillic, just into *different* Cyrillic. What
    separates them is the bytes either side of the alphabet: 866 puts
    box-drawing characters there and 1251 puts currency signs and stray
    letters, and a name holding either is a name read the wrong way round. So
    this counts evidence rather than guessing.
    """
    score = 0
    for character in text:
        code = ord(character)
        if character.isalnum() or character in " ._-+()[]{}#@!,;'&~/":
            score += 2
        elif 0x2500 <= code <= 0x259F:  # box drawing and blocks
            score -= 4
        elif 0x00A0 <= code <= 0x00BF or code in (0x00A4, 0x00A6, 0x00A7):
            score -= 3  # currency and section marks: 1251 reading 866's letters
        elif 0xD800 <= code <= 0xDFFF:
            score -= 8  # a surrogate: bytes that are not text in any reading
        elif code < 0x20:
            score -= 8  # a control character is never in a name
        else:
            # Anything else at all — a dagger, a per-mille sign, a smart quote.
            # **This has to cost, not merely fail to pay.** UTF-8 Cyrillic read
            # as 1251 comes out as letters with such marks sprinkled between
            # them, and while they were worth nothing the wrong reading won on
            # length alone: fourteen mostly-letters beat nine all-letters, so a
            # perfectly good `Отчёт.txt` was "repaired" into mojibake.
            score -= 3
    return score


def repaired(raw: bytes, choice: str, fallback: str) -> str:
    """The name those bytes were meant to be.

    ``fallback`` is what the format's own decoding produced, and is what comes
    back when it is the most plausible reading or when the choice is to leave
    names alone.
    """
    if choice == LITERAL:
        return fallback
    if choice == OEM:
        return _decode(raw, "cp866", fallback)
    if choice == WINDOWS:
        return _decode(raw, "cp1251", fallback)

    best, score = fallback, plausibility(fallback)
    for codec in CANDIDATES:
        candidate = _decode(raw, codec, None)
        if candidate is None or candidate == fallback:
            continue
        rating = plausibility(candidate)
        if rating > score:
            best, score = candidate, rating
    return best


def _decode(raw: bytes, codec: str, fallback: Optional[str]) -> Optional[str]:
    try:
        return raw.decode(codec)
    except UnicodeDecodeError:
        return fallback
