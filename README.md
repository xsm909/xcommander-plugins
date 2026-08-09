# xcommander plugins

Extensions for [xcommander](https://github.com/xsm909/xcommander), one folder
per plugin under [`plugins/`](plugins).

## Installing

In xcommander: **Settings → Plugins → Install from a repository…**, then paste

```
xsm909/xcommander-plugins
```

The app downloads this repository, lists what it finds, and installs the ones
you tick. Nothing here needs to be released or packaged first — the folders in
this repository *are* the plugins.

By hand works too: copy a folder from `plugins/` into the plugins directory
that Settings → Plugins shows, and press Rescan.

## What a plugin is

A folder holding a `plugin.json`. Two runtimes:

| | `declarative` | `python` |
| --- | --- | --- |
| What it is | JSON naming a built-in primitive | a real program, its own process |
| Platforms | all, including iOS and Android | desktop only |
| Can add a file system | no | yes |

Pick the weakest one that does the job — a declarative extension cannot crash
the app and runs where an interpreter cannot.

**Python plugins target Python 3.12** and get the standard library, nothing
else: the app runs one pinned interpreter rather than the machine's own, so a
plugin written once behaves the same everywhere. A plugin needing a third-party
package has to vendor it next to `main.py`.

See [the plugin documentation](https://github.com/xsm909/xcommander/blob/main/docs/plugins.md)
for the manifest, the extension points and the RPC protocol.

## Adding one

1. Make a folder under `plugins/`.
2. Write `plugin.json` — `id`, `name`, `version`, `apiVersion`, `runtime`.
3. Add a `README.md` saying what it does and what it claims.
4. Run `python3 tools/validate.py` before pushing.

`id` must be unique across this repository, and `apiVersion` must match the
host — a mismatch is refused rather than guessed at.

## Layout

```
plugins/<name>/plugin.json    the manifest, and the only required file
plugins/<name>/main.py        entry point for a python plugin
index.json                    generated summary, for tooling that wants one
tools/validate.py             checks every manifest here
```

`index.json` is a convenience, not a source of truth. The app reads the folders
directly, so a plugin is installable the moment it is committed — the index
never has to be right for this repository to work.

## Licence

MIT.
