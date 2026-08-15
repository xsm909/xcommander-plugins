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

"""Reading the JSON that is actually in the file.

`json.loads` refuses a file with anything after the closing brace, and real
files have plenty: both of the n8n workflows he handed over on 2026-08-15 came
off a web page with the workflow's title repeated three times on the end. A
reader that reads what is there is worth more here than one that is right about
the standard — and the shape it needs is already in the standard library.
"""

from __future__ import annotations

import json
from typing import Any


def first_document(text: str) -> Any:
    """The first JSON document in [text]. What follows it is not our business.

    Raises whatever `json` raises when there is no document at all — a file that
    does not start with one is not a graph, and saying so is the honest answer.
    """
    return json.JSONDecoder().raw_decode(text.lstrip("﻿ \t\r\n"), 0)[0]
