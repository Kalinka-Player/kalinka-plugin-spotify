# Why the pipe bridge exists

The patch extends the **existing librespot executable** with a private pipe
bridge and a Spirc method for reporting external volume without a command echo.
It keeps `--backend pipe --passthrough`; compressed bytes leave stdout unchanged.
In bridge mode, file selection considers only Ogg Vorbis formats. Upstream's
320 kbps preference lists MP3 before 160 kbps Vorbis, and the passthrough decoder
cannot load MP3, so such a track would otherwise be skipped as unavailable.
It is opt-in through an inherited private Unix socket FD.
Without that FD, ordinary librespot playback is unchanged, apart from the two
explicit offline verification options.

Verified against [librespot v0.8.0](https://github.com/librespot-org/librespot/tree/v0.8.0):

- The [pipe sink](https://github.com/librespot-org/librespot/blob/v0.8.0/playback/src/audio_backend/pipe.rs)
  calls blocking `write_all` on the player thread. Stopping reads to pace that
  pipe can strand pause/seek behind the blocked write.
- [PlayerInternal](https://github.com/librespot-org/librespot/blob/v0.8.0/playback/src/player.rs)
  sends `TrackChanged` synchronously before playing the loaded decoder, sends
  `Seeked` after seeking it, and calls the sink with raw packets.
- [Event hooks](https://github.com/librespot-org/librespot/blob/v0.8.0/src/player_event_handler.rs)
  run from a separate event consumer. They do not frame stdout or expose a
  bidirectional control channel. Human-readable stderr is not used.
- The [passthrough decoder](https://github.com/librespot-org/librespot/blob/v0.8.0/playback/src/decoder/passthrough_decoder.rs)
  re-emits Vorbis headers after a seek but originally kept an old EOS in its
  pending writer buffer. For bridge mode, the patch drops that pending output
  at the discontinuity before issuing the new stream headers.
- The [Spirc API](https://github.com/librespot-org/librespot/blob/v0.8.0/connect/src/spirc.rs)
  supplies real pause/play/next/prev/seek/disconnect controls. These are library
  methods, so the patch wires them into the executable's existing event loop.

The selected version's [options](https://github.com/librespot-org/librespot/wiki/Options)
and [events](https://github.com/librespot-org/librespot/wiki/Events) are useful
operator references; the pinned source is the authority for ordering.

## Protocol 1

The supervisor creates a socketpair and passes one FD in `KALINKA_CONTROL_FD`.
It owns the other endpoint. There is no listening control port and no shell
hook, username, credential or access token in this protocol.

The executable writes bounded newline-delimited JSON events to the socket:

```json
{"event":"ready","protocol":1,"epoch":0}
{"event":"track","title":"Example","artist":"Artist","album":"Album","duration_ms":180000,"uri":"spotify:track:...","cover":null,"epoch":2}
{"event":"playing","position_ms":0,"epoch":2}
{"event":"packet","id":1,"length":12345,"position_ms":1000,"epoch":2}
```

`packet` is emitted immediately before the sink writes exactly `length` raw
bytes to stdout. The plugin reads exactly that many bytes before handling the
next event, then frames Ogg itself; stdout reads are not page boundaries.
In bridge mode the pipe sink flushes stdout after each write. Rust's stdout is
line buffered, including when redirected to a pipe: without this flush, a
packet's suffix can remain buffered while the player waits for its acknowledgement.
That makes the plugin time out waiting for bytes already announced by the bridge.
The socket writer is serialized. `loading`, `track`, and `seeked` increment the
epoch and revoke outstanding credit. Other events include `paused`, `stopped`,
`end`, `connected`, `disconnected`, and a fixed, credential-free `error.code`.
If Spotify rejects the account login while starting Connect, the code is
`authentication_failed`; other startup failures, such as a 503 from its token
service, are `service_unavailable`. Builds that report this distinction add
`"signin_errors": true` to their capability probe.
`volume` carries Spotify's requested level as an integer from 0 to 65535:

```json
{"event":"volume","volume":19661,"epoch":2}
```

Commands from the supervisor:

```json
{"op":"ack","id":1}
{"op":"pause"}
{"op":"resume"}
{"op":"next"}
{"op":"prev"}
{"op":"seek","position_ms":42000}
{"op":"progress","position_ms":42500,"epoch":2}
{"op":"volume","volume":19661}
{"op":"suspend","id":1,"position_ms":42500}
{"op":"disconnect"}
```

Only one compressed packet may be unacknowledged. Credit gates the player's
**next decoder read** through an `AtomicWaker`, after its command/load handling.
It does not block the sink or the command lane. The plugin drains stdout into
bounded storage regardless of the renderer's read rate, then withholds credit
based on Ogg granule time and a local clock anchored to timestamped renderer
state changes. The clock advances only during playback, freezes during pause
or buffering, and is corrected by each new control point. The plugin's timer
reports progress once a second even when the renderer emits no events.
A command can interrupt the wait, and a generation change drops obsolete
credit. A broken supervisor exits
the child; control writes have a two-second timeout. Local command queues are
bounded. Progress corrects the Connect clock without moving the decoder cursor;
stale epoch feedback is ignored.

Volume is a separate control path. Incoming Spotify requests are converted to
percentages and applied through the direct-playback hold's `set_volume()` API.
The hold reports actual device steps and their maximum; those are scaled back
to Spotify's 0–65535 range. The `volume` command calls `Spirc::report_volume()`,
which updates Connect state without emitting another `VolumeChanged` request.
This prevents feedback loops while preserving device rounding. The plugin
coalesces outbound slider updates and leaves audio packets untouched. Volume
events received before acquiring an output or after revocation are ignored;
the current renderer level wins when acquiring the output. Capability probes
include `"volume": true`, and the plugin rejects older bridge builds lacking it.

On output takeover the plugin sends `suspend` with a monotonically increasing
handoff ID and the last audible position, then retires its capture. Connect
remains active and selected, but reports paused. The player drops its decoder
and preload before emitting `{"event":"suspended","id":1,"epoch":3}` on the
same ordered lane as audio packets. The plugin drains and discards everything
before the matching marker, then waits for a new `playing` event before acquiring
the output. This prevents stale audio or an old Play event from retaking the queue.
Seeking while suspended changes the saved position without acquiring an output.
A later Play loads the current track afresh at that position, supplying new
Vorbis headers even for the same track. When a cluster update names another
Spotify device as active, Kalinka retires only its local audio and marks itself
inactive. It preserves the last position and does not publish a stopped/reset
state or an extra cloud disconnect after the recipient takes over. Queued
player events and delayed state notifications are ignored while inactive.
A renderer decode/playback error follows the same suspension path. It is an
output warning, not a receiver failure: librespot and discovery remain alive,
the failed capture is retired, and Play can acquire the newly selected renderer.
Error callbacks do not replace the saved position or send zero progress. The
warning clears after the new output reports playing; old-resource callbacks
cannot suspend that stream. Invalid Ogg data, capture failures and a stalled
renderer suspend the same way: the plugin has read every announced byte, so
stdout and the control lane stay aligned. For these, the plugin sends `resume`
after the matching `suspended` marker, once per track URI; the player then loads
the track afresh at the saved position. Shutdown still uses `disconnect`.
Capability probes include `"reconnect": true` and `"suspend": true`; older bridge
builds must be rebuilt.

Context entries without a Spotify item UID retain that absence in bridge
mode, instead of gaining a random UUID every time the context is resolved.
Supplied Spotify UIDs remain unchanged, including playlist entry UIDs. Transfer
lookup only compares nonempty UIDs; otherwise it matches the track URI, so two
UID-less album tracks cannot accidentally match the first entry. An offline
regression covers repeated conversion, context reload and transfer restoration
at a nonzero album index while retaining the saved position. Playlist coverage
also checks both absent and supplied UIDs through context reload, transfer and
track advancement. A new explicit Play cancels any unfinished previous transfer
and context resolution, preventing its old context from replacing the new list.
Bridge Play requests also discard empty `skip_to.track_uri` and
`skip_to.track_uid` fields before resolving the selection. An empty URI must
not hide a supplied UID, and an empty UID must not match the first UID-less
album entry instead of the supplied track index. Nonempty URI/UID selectors
retain their priority over the index, which some requests supply as zero.

The offline self-test also supports the inherited control socket. It emits
packet events and waits for credit after every sink write, including the last
one. The Python verifier reads the full announced length before acknowledging
it, so a later write, sink shutdown or process exit cannot hide missing flushes.

Hidden songs are read from Spotify's `playcontextbans` collection through
`POST /collection/v2/paging`, using the existing authenticated session. This
is a read operation; the bridge never changes the user's hidden-song settings.
Resolved playlist metadata alone did not contain these flags in the live check.
The reader follows pagination and tombstones and keeps context URIs alongside
track URIs. Refreshes run before Play/transfer, Next/Previous and automatic
track advancement, plus every 15 seconds in the background, with a five-second
timeout. Navigation applies the fresh list before choosing a song; only the
background refresh may independently advance past a newly hidden current song.
This prevents a single Next request from advancing twice. Failed or incomplete responses retain
the previous complete list; an older in-flight response cannot overwrite a newer
one. Without any successful refresh, hidden-song skipping is unavailable.

Selection and preloading skip hidden songs, including previous/repeat navigation.
Original context entries, UIDs and indices are retained, so upcoming songs become
playable again when unhidden and transfer identities stay stable. A bounded scan
handles long runs of hidden songs and stops an entirely hidden repeating queue.
Starting a context without a particular selection skips a hidden first song;
explicitly selecting a hidden song stops playback. A background refresh cannot
reacquire an output after queue handoff. `{"event":"hidden_tracks","count":1}`
reports a changed list's size without track IDs, context IDs or credentials.

The patch adds no credential handling, audio downloads or codec.
Upstream licensing is retained in `docs/librespot-LICENSE`.
