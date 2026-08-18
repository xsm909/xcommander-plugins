# Playlists

A playlist is a folder written down: every line names a file that lives
somewhere else. So Enter opens one as a folder of its tracks, and everything
the application already does to a file works on each of them — play it with F3,
copy it out with F5, look at what it is.

F3 on the playlist itself lists what it names, how long each track is, where it
points, and **whether it is still there**. A playlist outlives the files it was
written for; a list of twelve tracks with four of them gone is a list that has
told you nothing.

Three formats: `.m3u` (in whatever alphabet the machine that wrote it used),
`.m3u8` (the same thing in UTF-8) and `.pls` (the INI file Winamp's station
lists still arrive in). Relative lines resolve against the playlist, not against
wherever the panel happens to be standing, and a line pointing at a stream is
shown as what it is rather than as a file that is missing.

Nothing here plays anything. A track is an ordinary file with an ordinary name,
so whichever viewer claims an `.mp3` gets it.

    python3 selftest.py     # the reader, against lists written the way players write them
