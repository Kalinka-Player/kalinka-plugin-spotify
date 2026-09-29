# Real Spotify smoke test

These checks need a Spotify Premium account, phone, Kalinka server and remote
renderer. They were **not run** by the offline test suite.

1. Install the companion core/SDK/renderer changes and the plugin. Build the
   pinned librespot patch and run `scripts/verify-librespot.py` against the
   installed binary. Keep Spotify disabled initially; local FLAC/MP3/Ogg should
   still play normally. Queue several local tracks and note the queue position.
2. Put the phone on the server's discovery network. Select a renderer on a
   different machine in Kalinka. Enable Spotify, choose its advertised name in
   the Spotify app and pair. Confirm credential directory/file modes 0700/0600.
3. Start a track. Confirm sound comes from the selected remote renderer, the
   normal queue remains intact, metadata changes to Spotify, and status says
   Spotify owns the queue. Check that only the renderer opens the audio device.
   The renderer should fetch the server's `/content/spotify/<generation>` URL.
4. Inspect the *renderer request* (do not open an extra unfinished GET: it is
   deliberately rejected). An unfinished response must be `audio/ogg`, without
   Content-Length, with `Accept-Ranges: none` and `Cache-Control: no-store`.
   A HEAD probe is safe. Confirm playback begins while capture is unfinished.
5. Play for at least five minutes and through two track changes. Compare
   Spotify time, Kalinka time and audible track boundaries. Target a difference
   within the default lead budget (about 3.5 seconds, or 9 seconds across
   silent passages); record startup delay,
   drift and transition gaps. No next track should audibly replace the previous
   one early. Confirm cache usage stays under its configured bounds.
6. Pause in Spotify while buffered audio exists. Sound should pause promptly.
   Leave paused for over 20 seconds, resume, and confirm pause was not treated
   as HTTP EOF or a renderer stall. Repeat using Kalinka's pause/resume controls.
   Change volume in Spotify, then in Kalinka or on the output device. Both apps
   should show the applied level, including zero/mute and the device's rounding.
   Drag each slider quickly; playback should continue without a control loop.
   Initial Spotify connection must reflect Kalinka's startup volume ceiling.
7. Seek forwards/backwards repeatedly, including while paused; use next and
   previous in both apps. Each discontinuity needs a new URL and valid headers.
   Confirm there is no burst of stale audio and displayed time includes the
   upstream seek position. A seek near the end must not hang indefinitely.
8. Slow or disconnect the renderer network. Production must stop gaining credit
   and fail within the 30-second absent-reader/feedback budget. Restore it and
   explicitly restart Spotify playback as directed by status. A reconnect must
   never replay the beginning of an old generation silently.
9. Start an ordinary Kalinka source. Confirm Spotify pauses with Kalinka still
   selected, the preserved queue plays, and the same receiver process stays up.
   Press Play in Spotify without opening its device picker. Confirm Spotify
   reacquires the output and a fresh stream resumes at the last audible position.
   Repeat while Spotify is already paused and after seeking in the paused app.
   Repeat several times and with
   another input plugin if available. Change target renderer and verify the old
   Spotify hold ends; Play in Spotify should use the newly selected target.
   Also transfer Spotify to the phone and back using its device picker after
   seeking at least a minute into a track. Both directions must retain the
   position. Repeat at least three times using different album tracks, including
   a track after the first, then check both a user-created playlist and a
   Spotify-generated playlist. The phone must retain its miniplayer, current
   track, position and playing/paused state. Repeat while paused, including
   after a Kalinka-queue handoff.
   Also test the combined sequence without restarting: play an album, transfer
   phone → Kalinka → phone → Kalinka, switch to a Spotify-generated playlist
   while Kalinka remains selected, then transfer to the phone. Repeat both
   immediately after returning to Kalinka and after the album has played awhile.
   Use tracks without Spotify's hidden-song/minus marker. A user reproduction
   in Olivia Dean Radio stopped on the phone until the affected track was
   unhidden. Initial hidden-song skipping was subsequently confirmed; the
   consecutive-hidden-song cases below still need verification.
10. Kill only the plugin-owned librespot child while idle, then while playing.
    Confirm a new child advertises the same name without changing the server PID;
    old playback readers/holds must close, and new playback must use a fresh URL.
    Repeat failures to check the increasing retry delay and visible retry status.
    Disable during retry, pause and active capture. Confirm no further child is
    spawned, no lingering child, released readers and no retained audio files.
    Finally replay local FLAC,
    MP3 and Ogg on the same renderer.

Record versions, device models, network topology, measured drift/latency, and
any failure without including credentials. Spotify service changes can affect
pairing/playback independently of these deterministic tests.

## Hidden songs

1. Hide a song in a Spotify-generated playlist, then start the preceding song
   on Kalinka. Let it end naturally: playback should skip the hidden song.
2. While playing, hide the next two songs and immediately press Next. It should
   go straight to the first allowed song. Repeat with natural track completion.
   Also hide the current song and the next one before pressing Next: it should
   select the first allowed song without advancing twice.
3. Try Next, Previous, shuffle and context repeat around the same hidden entry.
   It must not play, and transfer to the phone must preserve the selected song.
4. Unhide it and restart playlist playback (or wait 15 seconds). It should play
   again. The same song in an unrelated album/playlist must remain playable.
5. Hide the first song and start the whole playlist without selecting a track.
   Playback should start at the next allowed song. Explicitly selecting the
   hidden song should stop playback.
6. Hand playback to Kalinka's queue, then change a hidden-song setting. The
   periodic refresh must not reclaim the output.

## Unsupported renderer recovery

1. While Spotify plays through a supported renderer, select a renderer that
   cannot decode its Ogg stream. Playback should pause.
2. Press Play in Spotify twice, allowing each attempt to fail. Kalinka must
   remain visible and selected in Spotify; plugin settings should show an output
   warning, and the receiver process must remain running.
3. Select the supported renderer in Kalinka and press Play in Spotify. Playback
   should resume at the saved position, with the output warning cleared, without
   restarting the server or toggling the plugin.
