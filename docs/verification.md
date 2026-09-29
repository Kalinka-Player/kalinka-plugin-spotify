# Verification record

Checks run locally on 2026-09-28 and 2026-09-29. Offline checks used no Spotify credentials.
User-assisted local connection checks are recorded below; remote-speaker
playback remains unverified.

Receiver supervision in version 0.1.2 adds 18 regressions: actual subprocess
SIGKILL and exit-status recovery, exit detection with a full stdout pipe,
interruption of a blocked event handler, cleanup of old output/HTTP readers,
fresh playback after restart, pacing-failure recovery, capped exponential
backoff and its reset after stable operation, disable during retry, immediate
disable, and permanent-error/shutdown handling. Idle, paused, buffering and
queue-handoff sessions keep their receiver. All 128 plugin tests and Ruff
lint/format checks pass locally. No native librespot or renderer changes are
needed for this update.

Version 0.1.2 was built as a Debian package and installed on raspberrypi.local.
After the package's normal server restart, killing only the owned librespot
child with SIGKILL produced a replacement in 1.07 seconds. The old child was
reaped; server PID 205801 and renderer PID 186215 remained unchanged. Module
health changed from warning during retry to ready, the selected renderer was
preserved, and mDNS advertised Kalinka on the replacement's new port. Its
discovery endpoint returned `OK`. Installed Python sources match the tested
files. Resuming music after this induced failure still needs an app playback
check; discovery recovery does not promise uninterrupted playback.

On 2026-09-29 at 02:51:44 BST, repeated seeks on the Pi exhausted the two-capture
limit while previous HTTP readers were still closing. The plugin raised
`Previous renderer readers still hold the playback cache` and stopped its native
receiver; the Kalinka server and renderer remained running. Version 0.1.1 waits
up to five seconds for either retired capture to close. A timeout pauses and
releases the output while keeping Spotify Connect alive, with Play available to
retry. The disk limit and unfinished-stream HTTP semantics are unchanged.
Five regression cases failed before the fix. All 110 plugin tests now pass,
including delayed disconnects on ASGI 2.0/2.4, 20 alternating rapid seeks,
timeout/retry without restarting the producer, and disabling during the wait.

A subsequent live seek check exposed a separate renderer stall: its audio worker
treated a control-cancelled input wait as ordinary starvation and restarted the
buffer-fill loop before acknowledging the command. The pending pause blocked the
renderer command loop, including its heartbeat, until Spotify's watchdog expired.
The companion renderer patch now returns from an interrupted read to process the
explicit control. Two ARM64 regression tests reproduced unanswered pause/seek
commands before the fix and pass afterward. A third test confirms ordinary live
starvation remains buffering and resumes on incoming audio without entering
paused state. The Pi's emitter suite passes 19 tests, with one test requiring a
real-time audio device skipped; the checks used silent ALSA output.

The latest request-error fix retires superseded captures without raising an
`OSError` while their renderer still has an HTTP request open. Reads stop and
wait for the actual disconnect; no EOF is fabricated. The server now watches
disconnects even on ASGI 2.4 while a live reader is waiting for bytes, and
distinguishes a closed output socket from a real source-read failure. The new
seek, track-change, stop and disable regressions failed before the fix and pass
afterward. All 104 plugin tests and 122 relevant core tests pass.

On 2026-09-29, the renderer's unknown-format fallback was replaced with an
`Unsupported stream format` decoder error. Regression tests verify rejection
before an HTTP request, the faulted source token in events and snapshots,
deferred failure for a queued source, and recovery with a supported source.
The format-selection checks also cover FLAC/MP3/Vorbis MIME and bare names,
extension fallback, and speaker-test tones. The focused player, switcher,
state-translation and stream-identity suites passed 72 tests; 11 tests requiring
real-time audio hardware were skipped. The renderer executable built successfully,
and the updated companion patch applies to the clean pinned core commit.

Later on 2026-09-29, `raspberrypi.local` was upgraded using Debian packages:
renderer `0.4.3~dev16+g6162c0b.d260929` (built on its Debian 13 ARM64 system),
server `5.2.1~dev16+g6162c0b.d20260929`, and SDK `3.5.0`. Both services restarted
successfully; the renderer registered its new version with both servers and
retained its identity and output settings. The installed executable matches the
running process and links Vorbis successfully. Existing Pi plugins report ready.
This verifies deployment and registration; Spotify audio playback through the Pi
has not yet been confirmed. The Spotify receiver remains on the development host.

| Check | Result |
| --- | --- |
| Plugin pytest suite | 128 passed after receiver supervision |
| Plugin Ruff lint/format | Passed |
| Plugin wheel and source archive | Built successfully |
| Debian plugin package | `kalinka-plugin-spotify_0.1.0_all.deb` built and inspected successfully |
| Kalinka SDK | 23 passed |
| Kalinka server, excluding its separately gated playqueue suite | 1322 passed after rebasing the core PR onto current main |
| Existing dummydevice / Jamendo / localfiles / MusicCast tests | 7 / 85 / 1495 / 15 passed |
| Existing SDD benchmark unit tests | 88 passed |
| Core undefined-name and credential-logging gates (`make lint`) | Passed |
| Native renderer after the playback-clock fix | 410 tests: 395 passed, 15 hardware-dependent skips, no failures on the final run |
| Patched librespot | Latest hidden-song build and 17 native regressions passed on 2026-09-29; earlier pinned build/check also passed |
| Hidden-song skipping | User confirmed automatic/manual skipping; immediate hide-then-Next cache gap reproduced and fixed offline; retest pending |
| Unsupported renderer recovery | Decoder-error shutdown reproduced and fixed; repeated retries/position preservation/stale callbacks tested; live confirmation pending |
| Actual compiled pipe decoder output | 10 valid Ogg pages, 24,460 bytes, 8,000 ms |
| Actual compiled output after seeking to 4 seconds | Fresh BOS/headers and EOS, 6 valid pages, 14,032 bytes, 3,923 ms |
| Compiled pipe with acknowledgements withheld until packet receipt | Passed for both initial playback and seek |
| Live Vorbis prefix regression and native player settings tests | 41 passed |
| Playback snapshot clock and state translation (muted PipeWire output) | 18 passed; advancing-position regression failed before the fix |
| Buffering preserves the consumed position and stream format | Passed |
| Completed HTTP delivery waits for audible track completion | Passed; premature shutdown reproduced before the fix; truncated delivery still fails |
| Native Connect volume report without a command echo | Passed using the real player event channel and Connect state, without account/network/audio-device access |
| Live volume synchronization | User confirmed Spotify → Kalinka and Kalinka → Spotify both work |
| Queue handoff and Spotify reconnection | User confirmed Kalinka remains visible and reconnects without a restart |
| Keep Spotify selected through handoff | Offline native and plugin regressions passed; live app confirmation pending |
| Transfer from Kalinka to phone retains position | Album identity regression reproduced and fixed; user confirmed all repeated album transfers preserve position |
| Transfer from a Spotify-generated playlist to phone | User identified a hidden-song marker in Olivia Dean Radio; unhiding the song restored transfer playback |
| Server content, range and binding tests after initial-probe fix | 84 passed |
| Independent FFmpeg decode of both outputs | Passed |
| Both patches on clean pinned worktrees | `git apply --check` passed |

The renderer tests include local and HTTP FLAC, MP3 and Ogg playback, seek,
stream failures, unknown-length chunked Vorbis and a live pause longer than the
ordinary HTTP stall timeout. The subsequent live Vorbis input callback fix is
recorded below.

The plugin tests cover fragmented pages, checksums/format rejection, truncated
streams, asynchronous waits versus EOF, bounded disk and pinned-reader cleanup,
honest HTTP headers and ranges, explicit reconnect refusal, absent/slow readers,
media-time credit independent of bytes delivered, pause/resume during blocked
production, generation invalidation, metadata association, track-end pacing,
controls, takeover, child supervision, stderr draining, credential modes,
shutdown-before-start and bounded media time per producer packet.

Settings regression coverage reads the server's generated Spotify schema, writes
the enable toggle and device name through its configuration API, and reloads the
persisted overrides. This catches the inherited random module name that previously
produced `400 Invalid config key`. Startup failures are also checked for visible
health errors and credential-free log messages.

The local development server was restarted with Spotify enabled and the previously
built, capability-checked librespot executable installed under its development
prefix. Its API reports `spotify` / `Spotify Connect`, `ready`, and `Waiting for
Spotify`; the plugin-owned librespot process remains running. Renderer selection
uses Kalinka's normal controls, with no target-renderer field in plugin settings.
This startup check did not pair a Spotify account or play audio.

A subsequent user connection attempt exposed an audio-read timeout. Diagnostic
logging identified `Librespot.audio()` waiting for announced packet bytes. A
harness using the original compiled pipe sink reproduced it: a 128-byte packet
without a newline arrived only when the sink stopped, instead of before the
next credit. Bridge mode now flushes every packet. The compiled verifier checks
this ordering with actual Ogg data, both initially and after seeking. Python
tests also verify useful timeout/truncation errors and exception locations in
logs without including upstream exception text or partial packet contents.

The next local connection reached the renderer but exposed a second integration
mismatch: the renderer requests its first 384,000 bytes as `Range: bytes=0-383999`,
while the live endpoint rejected bounded ranges. Initial ranges starting at zero
now receive the complete live `200` response, without `Content-Length` or
`Content-Range`. Nonzero, suffix and multi-range requests remain rejected. Both
plugin HTTP tests and server content-route tests exercise the actual initial
range and the smallest bounded probe (`bytes=0-0`). The companion core patch
contains this fix.

A further paced-playback check exposed a decoder read-ahead deadlock: a complete
Ogg audio page was available, but the Vorbis read callback waited for its whole
requested buffer while the producer waited for playback progress. The new native
test provides only the headers and first audio page, with no EOF or later writes;
it failed before the fix and passes after it. The callback now waits for an item
and returns the available bytes. The Vorbis and native player settings suites
pass (41 tests), and the local renderer was rebuilt with this companion fix.

A subsequent local attempt played briefly and then repeated the same Spotify
position. The renderer had consumed about three seconds, but each periodic
snapshot still reported the position anchored at the last state change. This
reset upstream progress and exhausted the producer's media-time credit. Native
snapshots now advance that anchor to the capture time while playing and keep it
fixed while paused or buffering. Entering a refill also retains the consumed
position and stream format. The new snapshot test reproduced the zero-position
updates before the fix and passed afterward using muted real-time PipeWire
output. The existing slow-input test now checks that buffering retains its
one-second position. The local renderer and server were restarted with these
changes. Later local attempts reached longer playback, then exposed the
end-of-track shutdown described below.

The first full native run caught an intermittent rapid-seek test failure:
`PlaybackPositionTest.SeekAfterSeekLandsOnTheLastOne` observed the preceding
seek's position (3 seconds instead of 2). The test marks an asynchronously
collected event list, so a preceding event can arrive after that mark. Twenty
isolated repeats and the subsequent full suite passed. No seek implementation
or rapid-seek assertion was changed for this playback-clock fix.

The next local failure left the plugin in `No renderer is reading Spotify audio`
and removed its Connect advertisement. The missing-reader check did not distinguish
an interrupted transfer from a completed HTTP response whose final audio remained
buffered in the renderer. The regression test reproduces that premature shutdown
after the startup timeout. Full delivery now keeps the last producer credit held
until the renderer reports audible completion; incomplete or truncated delivery
still fails. The test also verifies that the next track starts after completion.
Pacing failures now log the error and capture counters without upstream metadata.
All 60 plugin tests and Ruff checks passed, and the Debian package was rebuilt.
After restarting the local server, mDNS advertised its Connect endpoint and that
endpoint returned the device name `Kalinka`. A real Spotify track transition
with this fix was not explicitly confirmed, though the user subsequently reported
that playback looked better.

Bidirectional volume control now routes Spotify requests through the existing
direct-playback volume API and reports the actual output level back to Connect.
The plugin's 78 passing tests include startup synchronization, mute/full volume,
device-step scaling, duplicate suppression, rapid slider updates, unavailable
volume controls and events after revocation. The native regression test checks
that incoming requests emit `VolumeChanged`, while reporting the renderer's level
updates Connect state without emitting another volume request. The updated
binary passed all four compiled Ogg/seek/credit checks and was installed locally.
After the server restart, the user confirmed both volume directions during live
playback. The `.deb` and source archive were rebuilt with these changes; older
native bridge binaries must also be rebuilt to advertise `"volume": true`.

Switching to the normal Kalinka queue now releases the Spotify output while
keeping its receiver process and discovery running. The plugin drains stale
events and audio until disconnect completes and a fresh connection arrives.
The native bridge drops the retired decoder and preload so reconnecting starts
with fresh Vorbis headers, including when returning to the same track. Tests
cover repeated handoffs with one child process, different revocation reasons,
revocation during a packet read, shutdown during handoff and controls racing
the revocation callback. All 85 plugin tests and Ruff checks passed. The updated
native binary passed the four compiled Ogg/seek/credit checks and was installed
locally. The user then confirmed that Kalinka stays visible and reconnects after
switching to its normal queue. The Debian package and source archive include
this fix; older native bridges must be rebuilt to advertise `"reconnect": true`
as well as volume support.

The subsequent handoff revision keeps Kalinka selected in Spotify, matching the
Qobuz input's behaviour. It reports paused at the last audible position, retires
the old decoder, and emits an ordered suspension marker. The plugin rejects
stale events until that marker and waits for a subsequent Play before acquiring
the output. The native test verifies that Connect remains active and paused,
late progress/end events cannot change the saved position, a paused seek stays
suspended, and Play issues a fresh load at the requested position. It uses a
missing local-file URI and the real player command lane, without account,
network or audio-device access. Both native bridge tests, all 89 Python tests
and the four compiled Ogg/seek/credit checks passed. The updated bridge is
installed locally for a user-assisted app check. This revision requires the
additional `"suspend": true` capability.

The next user check found that transferring Spotify from Kalinka to the phone
reset the position to zero, while transfer in the opposite direction preserved
it. The receiver's outgoing-transfer path previously ran ordinary disconnect
and stop handling, clearing its position/context while a state notification
could still be queued. Bridge mode now handles the cluster's change of active
device locally: it retains the last position, retires its decoder, and stops
publishing playback state once another device owns playback. Stale player events
cannot revive a notification or advance the old queue. A native regression
uses the actual cluster-update path at 65 seconds, both playing and paused,
then injects late zero-position/end events and calls a delayed notification.
It verifies that position and track remain intact and that no cloud call is
made through the unconnected test session. All three native bridge tests pass.
This fix requires rebuilding the bundled native bridge; the wire protocol is
unchanged.

The initial live retest still reset, then the user narrowed it to album playback
while playlist transfer worked. Temporary diagnostics compared only position,
timing and track-identity equality flags, without credentials or track names.
Spotify's returned transfer state contained the correct nonzero position; the
subsequent album reproduction also preserved it on the phone, but the user then
confirmed intermittent failures. Later captures show a correct nonzero position
in the published cloud state followed by zero when the phone takes over. The
outgoing-state guard alone does not resolve this; album identity is under
investigation. Do not treat the earlier successful reproduction as final
end-to-end validation.

One diagnostic restart also exposed the receiver reconnecting before any
renderer had registered. Output acquisition now pauses the Connect session
without terminating discovery when the output is unavailable; a later Play
retries normally. The regression exercises failed acquisition followed by
successful playback using a fresh stream. All 90 plugin tests pass.

The next candidate addresses album identity: upstream conversion generated a
new random UID for album entries that Spotify supplied without one. Bridge mode
now preserves an absent UID instead of inventing one. Transfer lookup also
rejects empty-UID equality, preventing a later album track from matching the
first UID-less entry. The regression failed before the change and now verifies
identity stability across conversion and context reload, retained supplied
playlist UIDs, the second album track's restored index and its 65-second
position. All four native bridge tests and the compiled audio checks pass.
The updated bridge is installed locally. After being asked to test at least
three transfers from multiple album tracks, seeking past one minute each time,
the user confirmed that all repeated transfers preserve position. Temporary
timing diagnostics have been removed, and the Debian package and source archive
have been rebuilt with the final implementation and verification record.

A later report found that transfer from a Spotify-generated playlist stopped
playback on the phone and removed its miniplayer. The album-only identity fix
still synthesized random IDs for playlist entries without supplied UIDs. Bridge
mode now preserves missing UIDs for all contexts, while retaining every supplied
UID. The new regression failed against the album-only fix and passes now: after
context reload and transfer, it checks the previous/current/next playlist items,
the nonzero current index, 65-second position, and subsequent track advancement,
both with and without Spotify-supplied item UIDs. All five native bridge tests
and four compiled Ogg/seek/credit checks pass. The updated native bridge is
installed locally. This is a candidate fix for the reported phone behaviour;
confirmation on the same Spotify-generated playlist is still pending.

The user subsequently specified a combined failure sequence: transfer an album
back and forth, switch to a Spotify-generated playlist while playing on Kalinka,
then transfer to the phone. Identity tests alone do not validate this sequence.
Temporary diagnostics now record context type codes, queue counts, positions and
identity-equality flags at load, context resolution and cluster transitions; the
plugin accepts only explicitly allowed numeric/boolean fields and phase names.
The diagnostic build is running locally, awaiting this exact reproduction.

A separate offline regression reproduces an unfinished album transfer surviving
a later explicit Play, with its context resolver still able to overwrite the new
selection. Bridge mode now cancels that pending transfer and resolver work before
loading the new selection, re-resolving an incomplete context even if its URI is
unchanged. The regression failed before this change and all six native bridge
tests now pass, as do the four compiled audio checks and 91 plugin tests. This
cancellation fix was initially held out of the diagnostic process so the
reported failure could be captured without changing its behaviour.

The user reproduced the drop with diagnostics at 23:54 on 2026-09-28. The trace
shows a successful transfer back and forth, a new playlist Play, and then an
empty phone playback state when ownership changes. Before the failure, the new
playlist's 50 tracks were resolved, its current URI/UID and cloud transfer state
matched, neither side of its queue contained foreign context entries, and no
transfer was pending. This rules out the unfinished-transfer cancellation bug
as the cause of that reproduction; the missing-UID change also did not resolve it.

The next candidate renews the playback session ID after an explicit new Play,
instead of carrying the previous session into a different playlist. Seek,
pause/resume, Kalinka-queue suspension and outgoing device transfer retain the
current session. This follows the context-loading approach in
[librespot-java StateWrapper](https://github.com/librespot-org/librespot-java/blob/master/player/src/main/java/xyz/gianlu/librespot/player/StateWrapper.java),
which renews its session on a context load; session reuse causing the phone's
cached state to fail remains a hypothesis, not an established protocol guarantee.
The new regression failed before the change and passes afterward; all seven
native bridge tests, 91 plugin tests and four compiled audio checks pass. Both
the session-renewal candidate and the independently tested cancellation fix are
now installed locally. Diagnostics additionally compare cloud/transfer session
identity and main-context identity without recording the IDs. Live verification
of the combined context-change sequence is pending.

The session-renewal candidate also reproduced the empty phone state at 00:00 on
2026-09-29. The user then identified a minus/stop-sign marker beside the affected
track in Olivia Dean Radio and reported that removing it restored playback.
Spotify documents this as the hidden-song control: the mobile client skips
hidden songs in that playlist, and tapping the minus icon unhides them. This
supports a hidden-track mismatch between librespot and the mobile app; it does
not establish that session identity or unfinished context work caused the drop.
The unsuccessful session-ID experiment and all temporary diagnostics were
removed. The independent, reproduced cancellation fix remains. The cleaned
build passes six native bridge regressions, 90 plugin tests and all four compiled
audio checks. Hidden-track handling by librespot remains a compatibility limit;
the user-confirmed workaround for this reproduction is to unhide the track.

Follow-up on 2026-09-29: the user hid Great Expectations in Olivia Dean Radio
and Kalinka still played it. Temporary diagnostics found no filtering metadata
in any of the 50 resolved playlist entries. A read-only authenticated collection
probe returned one contextual hidden-song entry, and the new runtime independently
loaded that entry after restart. The receiver now reads `playcontextbans` and
checks that list during selection and preloading. Fourteen native regressions
cover pagination parsing/tombstones, invalid responses, next/previous/preload,
context scoping, unhiding, runs longer than the queue window, repeat/all-hidden
termination, first-song selection, stale/failed refreshes and handoff protection,
alongside the existing transfer and volume cases. All 90 Python tests and four
compiled audio checks pass. Temporary flag diagnostics were removed. The fixed
receiver was installed locally. The user subsequently confirmed that both natural
advancement and Next skip an already-hidden song and continue playing.

The user then hid two upcoming tracks and pressed Next immediately: the first
hidden song played before the second Next skipped the other. The original
refresh points covered Play/transfer and a 15-second background interval, but
not Next. An offline reproduction using a newly changed collection failed
with the old code by selecting the first hidden song. Previous had the same
cache gap. Next/Previous now refresh before selection, as do natural track-end
and unavailable-track transitions. List application does not itself advance
playback during these commands, preventing a double advance when the current
song has also just been hidden. Seventeen native regressions pass, including
newly hidden consecutive tracks, natural advancement, Previous, and one-advance
semantics. Live confirmation of immediate hide-then-Next is pending.

A later renderer-switch failure at 01:01 on 2026-09-29 showed the selected
Raspberry Pi renderer reporting a FLAC decoder lost-sync error for the Spotify
Ogg stream. The plugin treated the output error as fatal, stopped librespot,
and disappeared from Spotify discovery. Five new regression cases reproduced
that shutdown. Renderer playback errors now suspend Connect and release only
the failed output, with a warning to select a compatible renderer and retry.
They preserve the last good playback position (or the current stream's starting
offset) rather than publishing an error callback's zero/missing position.
Tests cover initial decode failure, failure after playing, repeated attempts
against an incompatible output, recovery without restarting the receiver, and
late callbacks from the failed resources. All 95 Python tests and Ruff pass.
The native receiver is unchanged in this fix; live renderer-switch confirmation
is pending.

The first native live-pause test exposed a defect in the **test HTTP server**:
it retained a socket after advertising connection-close, preventing a lengthless
response from ending. That fixture was corrected, and the full native suite was
rerun successfully. The machine lacks system libvorbis development metadata;
CMake used the existing local libvorbis 1.3.7 installation at
`/tmp/kalinka-vorbis-deps/install` through `PKG_CONFIG_PATH`.

Expected environment warnings were observed in the existing server/localfiles
suites (HTTP test-client deprecation, SMB test-thread teardown). Cargo reports
an upstream future-compatibility warning for `num-bigint-dig 0.8.5`.

The GitHub workflow has been authored but not executed remotely. The separate
slow playqueue suite and opt-in full-system/Samba/hardware tests were not run;
playqueue implementation code was not changed. Direct-playback takeover and
normal queue preservation are covered by the passing server tests.

Real pairing/discovery, current Spotify service compatibility, audible latency,
progress alignment and remote renderer behavior still require
[`smoke-test.md`](smoke-test.md). Gapless chaining is not implemented; tracks are
separate resources and may have a short transition gap.

Debian packaging was checked against Kalinka's current plugin build scripts and
`docs/plugin-deb-packaging.md`. The package contains the wheel under
`/opt/kalinka/wheels`, the server restart trigger, and the standard removal hook.
Its metadata, Python entry point, payload checksums, root ownership and file
modes were verified after extraction. The removal hook was exercised against a
temporary fake venv for remove/deconfigure, upgrade, failed uninstall and absent
venv cases; no system installation was changed.

The package also built from the generated source archive outside a Git checkout,
when invoked from another working directory, with umask 0077 and an invalid stale
wheel in `dist/`. Only the freshly built wheel was packaged, and installed paths
kept their expected permissions. Shell syntax, Python lint and workflow YAML
checks passed. The GitHub workflow now builds and uploads the `.deb`; that remote
workflow and an actual apt installation have not been run.
