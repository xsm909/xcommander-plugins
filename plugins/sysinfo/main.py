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

"""What this machine is — operating system, processor, memory, graphics, disks.

Standard library only, because the host runs its own pinned interpreter and a
plugin cannot assume anything is installed alongside it.

Every fact is gathered defensively. A machine that answers no question about its
graphics card is a normal machine, not an error, so a probe that fails leaves a
row out rather than taking the whole report down with it.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Callable, List, Optional

from xcommander import Plugin, markdown

plugin = Plugin("org.xcommander.sysinfo")

#: Nothing here is worth waiting on. A hung `system_profiler` must not hang the
#: report; it just costs that one row. The manifest offers it as a setting,
#: because how long is too long depends on the machine.
DEFAULT_TIMEOUT_SECONDS = 6


def probe_timeout() -> float:
    return float(plugin.setting("probeTimeout", DEFAULT_TIMEOUT_SECONDS))


def run(command: List[str]) -> Optional[str]:
    """Runs a probe and returns its output, or None if it was no use."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=probe_timeout(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    return output or None


def first_line(value: Optional[str]) -> Optional[str]:
    return value.splitlines()[0].strip() if value else None


#: Both ways of saying the same number of bytes, and the step each divides by.
#: A disk is sold in decimal and reported by most systems in binary, which is
#: where "1 TB" and "931.3 GiB" come from — so which one to show is a setting
#: rather than a decision this plugin gets to make for everybody.
UNITS = {
    "binary": (1024.0, ("B", "KiB", "MiB", "GiB", "TiB", "PiB")),
    "decimal": (1000.0, ("B", "KB", "MB", "GB", "TB", "PB")),
}


def human_bytes(count: float) -> str:
    step, units = UNITS.get(str(plugin.setting("units", "binary")), UNITS["binary"])
    for unit in units:
        if abs(count) < step or unit == units[-1]:
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= step
    return f"{count:.1f} {units[-1]}"


# --- the probes -------------------------------------------------------------
#
# One function per platform per fact. Each returns a string or None, and the
# caller picks the one for the system it is on.


def cpu_name() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        return run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if system == "Windows":
        name = os.environ.get("PROCESSOR_IDENTIFIER")
        if name:
            return name
        return first_line(
            run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_Processor).Name",
                ]
            )
        )
    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or None


def total_memory() -> Optional[str]:
    system = platform.system()
    if system == "Darwin":
        raw = run(["sysctl", "-n", "hw.memsize"])
        return human_bytes(int(raw)) if raw and raw.isdigit() else None
    if system == "Linux":
        try:
            with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return human_bytes(int(line.split()[1]) * 1024)
        except (OSError, ValueError):
            pass
        return None
    if system == "Windows":
        raw = first_line(
            run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory",
                ]
            )
        )
        return human_bytes(int(raw)) if raw and raw.isdigit() else None
    return None


def graphics() -> List[str]:
    system = platform.system()
    if system == "Darwin":
        # `system_profiler` is slow but is the only thing that knows the name of
        # an Apple GPU; the timeout above keeps it from being a problem.
        output = run(["system_profiler", "SPDisplaysDataType"])
        if not output:
            return []
        names = []
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("Chipset Model:"):
                names.append(stripped.split(":", 1)[1].strip())
        return names
    if system == "Windows":
        output = run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController).Name",
            ]
        )
        return [l.strip() for l in output.splitlines() if l.strip()] if output else []
    if system == "Linux":
        output = run(["lspci"])
        if not output:
            return []
        return [
            line.split(":", 2)[-1].strip()
            for line in output.splitlines()
            if "VGA compatible controller" in line or "3D controller" in line
        ]
    return []


def mount_points() -> List[str]:
    system = platform.system()
    if system == "Windows":
        return [
            f"{letter}:\\"
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if os.path.exists(f"{letter}:\\")
        ]
    points = ["/"]
    volumes = "/Volumes" if system == "Darwin" else "/media"
    try:
        for name in sorted(os.listdir(volumes)):
            path = os.path.join(volumes, name)
            # A link in /Volumes points back at a disk already listed as "/".
            if os.path.isdir(path) and not os.path.islink(path):
                points.append(path)
    except OSError:
        pass
    return points


# --- the report -------------------------------------------------------------
#
# Markdown rather than a table: the answer has sections, not columns, and the
# host lets the whole thing be copied as text — which is the point, since this
# is the sort of thing people paste into a bug report.


class Report:
    """Collects Markdown, skipping anything the machine had nothing to say about."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self._pending_header = False

    def section(self, title: str) -> None:
        self.lines.append(f"\n## {title}\n")
        self._pending_header = True

    def add(self, label: str, value: Optional[str]) -> None:
        if not value:
            return
        if self._pending_header:
            self.lines.append("| | |")
            self.lines.append("| --- | --- |")
            self._pending_header = False
        # A pipe inside a value would end the cell early.
        self.lines.append(f"| {label} | {value.replace('|', '\\|')} |")

    def text(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


@plugin.command("sysinfo.show", "System information")
def show(args) -> dict:
    report = Report()
    report.lines.append(f"# {platform.node() or 'This machine'}")

    report.section("Operating system")
    report.add("System", f"{platform.system()} {platform.release()}")
    report.add("Version", platform.version())
    report.add("Architecture", platform.machine())

    report.section("Processor")
    report.add("Model", cpu_name())
    report.add("Logical cores", str(os.cpu_count() or ""))
    report.add("Memory", total_memory())

    # The graphics probe shells out to the slowest tool on every platform, and
    # a machine whose card nobody cares about pays for it on every report.
    cards = graphics() if plugin.setting("graphics", True) else []
    if cards:
        report.section("Graphics")
        for index, card in enumerate(cards):
            report.add("Card" if len(cards) == 1 else f"Card {index + 1}", card)

    disks = mount_points()
    if disks:
        report.section("Disks")
        for point in disks:
            try:
                usage = shutil.disk_usage(point)
            except OSError:
                continue
            used = usage.total - usage.free
            percent = (used / usage.total * 100) if usage.total else 0
            report.add(
                point,
                f"{human_bytes(usage.free)} free of {human_bytes(usage.total)}"
                f" ({percent:.0f}% used)",
            )

    report.section("Python")
    report.add("Interpreter", platform.python_version())
    report.add("Build", platform.python_implementation())

    return markdown(report.text())


plugin.run()
