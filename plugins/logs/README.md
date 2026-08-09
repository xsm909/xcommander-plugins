# Log files

Claims `.log`, `.out` and `.err` for F3 and shows them as plain UTF-8 text.

Declarative, so there is no code here and nothing to run: the manifest names
the host's `text` primitive and configures it. It loads on every platform,
including the ones where plugin code cannot run at all.

`maxBytes` is 8 MB. Beyond that the viewer reports the file as truncated rather
than trying to hold a log that grew without anyone watching — which is the
normal state of a log file, not an edge case.

Why this exists as a plugin: the core deliberately cannot view a file. Without
something claiming `.log`, F3 on one says so and stops.
