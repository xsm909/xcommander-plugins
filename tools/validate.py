#!/usr/bin/env python3
"""Checks every plugin in this repository, and rewrites index.json.

Run before pushing:

    python3 tools/validate.py          # check only
    python3 tools/validate.py --write  # check, then regenerate index.json

The app installs straight from the folders, so nothing here is required for a
plugin to work. What this catches is the class of mistake that only shows up
once someone has already installed the thing: a duplicate id, a manifest that
is not valid JSON, an entry script named in the manifest but missing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ROOT / "plugins"
INDEX = ROOT / "index.json"

API_VERSION = 1
RUNTIMES = {"python", "declarative"}
REQUIRED = ("id", "name", "version", "apiVersion", "runtime")


def check(folder: Path, seen: dict[str, str]) -> tuple[dict | None, list[str]]:
    """Returns the manifest and every problem found with it."""
    problems: list[str] = []
    manifest_path = folder / "plugin.json"

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, [f"plugin.json is not valid JSON: {e}"]

    for key in REQUIRED:
        if key not in manifest:
            problems.append(f"missing {key!r}")

    plugin_id = manifest.get("id")
    if plugin_id in seen:
        problems.append(f"id {plugin_id!r} is already used by {seen[plugin_id]}")
    elif plugin_id:
        seen[plugin_id] = folder.name

    if manifest.get("apiVersion") != API_VERSION:
        problems.append(
            f"apiVersion is {manifest.get('apiVersion')}, host speaks {API_VERSION}"
        )

    runtime = manifest.get("runtime")
    if runtime not in RUNTIMES:
        problems.append(f"runtime {runtime!r} is not one of {sorted(RUNTIMES)}")

    # A python plugin names an entry script, and it has to be there — the app
    # discovers the folder, then fails to start it, which reads as a bug in the
    # app rather than a missing file.
    if runtime == "python":
        entry = manifest.get("entry", "main.py")
        if not (folder / entry).exists():
            problems.append(f"entry {entry!r} does not exist")

    return manifest, problems


def main() -> int:
    if not PLUGINS.is_dir():
        print(f"no {PLUGINS.relative_to(ROOT)} directory", file=sys.stderr)
        return 1

    seen: dict[str, str] = {}
    index: list[dict] = []
    failed = False

    for folder in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        if not (folder / "plugin.json").exists():
            print(f"{folder.name}: no plugin.json, skipped")
            continue

        manifest, problems = check(folder, seen)
        if problems:
            failed = True
            for problem in problems:
                print(f"{folder.name}: {problem}", file=sys.stderr)
            continue

        assert manifest is not None
        print(f"{folder.name}: ok")
        index.append(
            {
                "folder": folder.name,
                "id": manifest["id"],
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest.get("description", ""),
                "runtime": manifest["runtime"],
                "apiVersion": manifest["apiVersion"],
                "platforms": manifest.get("platforms", []),
                "pythonMin": manifest.get("pythonMin"),
            }
        )

    if failed:
        return 1

    if "--write" in sys.argv:
        INDEX.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {INDEX.name} with {len(index)} plugin(s)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
