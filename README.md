# kalinka-plugin-spotify

Optional **server-side Spotify Connect input** for Kalinka. Disabled by default.
Spotify manages the queue; Kalinka sends captured Ogg/Vorbis to its selected
renderer. The renderer can be on another machine and is the only process that
opens audio hardware.

```text
Spotify app ── Connect ── librespot 0.8.0 + small pipe bridge
                              │ binary Ogg/Vorbis stdout
                              ▼
                  plugin → bounded temporary capture
                              │ Kalinka /content/spotify/<generation>
                              ▼
                     selected Kalinka renderer → audio output
                              │ actual playback snapshots
                              └── plugin pacing / Spotify progress feedback
```

Spotify streams at its highest Ogg/Vorbis quality, 320 kbps, falling back to
160 or 96 kbps Vorbis when a track has no 320 kbps file. Spotify Lossless (FLAC)
is unavailable: its keys are not served to librespot, and upstream has declined
to support it.

This plugin does not browse or index Spotify content, download a music library,
transcode audio, or provide codec support. Kalinka already decodes Ogg/Vorbis.

## Requirements and installation

Linux, Python 3.11+, a Spotify Premium account, and Kalinka **SDK 3.5 plus the
matching server and renderer changes** are required. The companion changes add
unfinished HTTP content and sequential direct playback; see
[`docs/core-compatibility.md`](docs/core-compatibility.md). An older renderer must
be upgraded too, even if it already supports Ogg/Vorbis.

On the Kalinka server (Debian 13, Raspberry Pi OS 64-bit, DietPi or Ubuntu 24.04; amd64 or arm64), install or upgrade to the latest release with:

```sh
curl -fsSL https://raw.githubusercontent.com/Kalinka-Player/kalinka-plugin-spotify/main/scripts/install-latest.sh | sudo bash
```

The script picks the package for the machine's architecture, checks it against the release's `SHA256SUMS` and installs it with apt; the server restarts to load it. The package bundles the patched librespot, so nothing needs building. Then set a device name, enable Spotify Connect in Kalinka's settings and select a renderer through Kalinka's usual output selector. The Spotify device name identifies the Connect receiver; output selection stays in Kalinka's normal renderer selector.

### From source

Build the pinned executable and verify it:

```sh
./scripts/build-librespot.sh
python -m pip install -e '.[dev]'
python scripts/verify-librespot.py build/librespot/target/release/librespot
```

The build needs Git, a Rust toolchain compatible with the pinned Cargo.lock,
Cargo, a C compiler and pkg-config. It enables `passthrough-decoder`,
`with-libmdns`, and `rustls-tls-native-roots`; no audio hardware backend is built.
The exact upstream revision is
`d36f9f1907e8cc9d68a93f8ebc6b627b1bf7267d` (librespot v0.8.0).
The patch is in [`patches/librespot-0.8.0-kalinka.patch`](patches/librespot-0.8.0-kalinka.patch).
The offline verifier runs the compiled Vorbis passthrough decoder and pipe sink,
checks their Ogg framing/checksums/timing, and repeats after a seek. It also
acknowledges each announced packet only after receiving all its bytes, catching
stdout buffering that would otherwise deadlock live playback. Rebuild older
Kalinka pipe executables to include the per-packet flush fix.

Install the Python package into the **server's** environment and set Spotify's
`executable` setting to the absolute path of that binary. The default is
`/usr/libexec/kalinka-plugin-spotify/librespot`, where the Debian package installs it.
Select a renderer through Kalinka's usual output selector, enable Spotify Connect,
and restart the plugin using Kalinka's settings. The default device name is
`Kalinka (<hostname>)`, for example `Kalinka (raspberrypi)`. An explicitly configured
device name takes precedence; output selection stays in Kalinka's renderer selector.

The executable is checked on every start. A stock executable without the
Kalinka bridge or a build without passthrough fails with a settings error.
Actual captured bytes must pass Ogg/Vorbis validation before playback starts.
There is no PCM fallback.

The plugin supervises its own receiver process. An unexpected exit, closed
control connection or fatal playback-session failure releases the old renderer
hold and starts a fresh receiver automatically. Retries wait 1, 2, 4, 8, 16 and
then at most 30 seconds; one minute of stable operation resets the delay.
Settings show the retry status. Discovery returns without a Kalinka server
restart, but interrupted playback may need selecting Kalinka and pressing Play
again in Spotify. Credentials and the selected renderer are preserved.

Spotify sign-in failures also retry, with delays growing to at most five
minutes, so the device returns by itself when Spotify recovers. This covers
service errors such as a 503 from Spotify's token service. If Spotify rejects
the saved sign-in itself, the plugin forgets it and the receiver restarts ready
for pairing: select Kalinka in the Spotify app. Older bridge builds report both
cases alike, so with them the plugin retries without forgetting the sign-in.

To pair another account or pair again by hand, turn on **Unpair Spotify account
on next restart** in the plugin settings and restart Kalinka. The saved sign-in
is deleted once and the setting turns itself off; the receiver then waits for
pairing, so select Kalinka in the Spotify app with a Premium account.

Normal buffering, pause and queue handoff do not restart the receiver. Disabling
the plugin or shutting down Kalinka cancels retries and stops its child. Missing
or incompatible executables and startup failures remain visible settings errors
requiring correction. This watches process exit
and session failures; it does not treat an idle receiver as an unresponsive one.

The receiver and capability probe run outside the server's terminal process
group, so Ctrl+C lets Kalinka shut down its streams before stopping librespot.
The plugin requests graceful receiver shutdown and continues draining its pipes;
it escalates to termination after three seconds and kill after one more second
only if the child remains running.

If the selected renderer cannot play the stream, Spotify pauses and the plugin
shows an output warning while keeping the Connect receiver available. Select a
compatible renderer in Kalinka and press Play in Spotify to retry.

The patched receiver reads Spotify's playlist-specific hidden-song list and
skips those tracks during queue playback. Hiding a song in one playlist does
not hide it in an album or another playlist. The list refreshes before Play,
transfers, Next/Previous and automatic advancement, and every 15 seconds while
connected. Rebuild the
patched executable to add this support to an older installation.

## Debian package

The Debian package follows Kalinka's plugin packaging convention: its wheel
goes in `/opt/kalinka/wheels`, and the `kalinka-server-restart` dpkg trigger
lets the server's bootstrap install it into `/opt/kalinka/venv`. Removal uses
the same pip-uninstall hook as the other plugins.

Unlike the other plugins, the package is architecture-specific: it also installs the patched librespot at `/usr/libexec/kalinka-plugin-spotify/librespot`. `scripts/build_deb.sh` takes that executable and packages it for the build machine's architecture, without cross-compiling; `dpkg-shlibdeps` derives the library dependencies from it, so the build distro sets the oldest distro the package installs on. Build on Debian 12 (bookworm), whose glibc is older than any supported server's, so one package per architecture covers them all:

```sh
./scripts/build-librespot.sh
python3 -m venv .venv
.venv/bin/pip install build
PYTHON=.venv/bin/python ./scripts/build_deb.sh build/librespot/target/release/librespot
sudo apt install ./kalinka-plugin-spotify_*_$(dpkg --print-architecture).deb
```

Build requirements are Python 3.11+, the Python `build` module, `dpkg-dev` and binutils, plus the librespot toolchain above. The version comes from the newest `kalinka-plugin-spotify-v*` tag via setuptools-scm, so build from a clone with its tags; an untagged commit builds a `~dev` version that sorts before the next release. The `.deb` is written in the repository root. Existing `dist/` artifacts are preserved.

The package depends on `kalinka-server`, SDK `>=3.5,<4`, Python `>=3.11` and CA certificates. The companion server and renderer changes are also required; installing this plugin package does not upgrade their streaming implementation.

### Releasing

Push a `kalinka-plugin-spotify-vX.Y.Z` tag. The Release workflow runs the tests, builds librespot and the `.deb` natively on amd64 and arm64 runners inside a Debian 12 container, verifies each executable's pipe output, and publishes both packages with `SHA256SUMS` to a GitHub release marked latest, which `install-latest.sh` installs. A `## X.Y.Z` section in `CHANGELOG.md`, if present, leads the release notes. To rebuild an existing tag, run the workflow by hand with that tag.

## Pairing and credentials

With the server and phone on the same discovery network, choose the configured
device name in Spotify's **Connect to a device** picker. This uses librespot's
zeroconf pairing and credential cache, not password login or a Spotify Web API
client secret. mDNS and the librespot discovery endpoint must be reachable from
the phone. The selected renderer needs to reach the server's normal HTTP port.

Credentials live under Kalinka's `state_dir()/spotify` (normally
`/var/lib/kalinka/spotify`; honours `KALINKA_PREFIX`). The directory is 0700,
existing credential files are tightened to 0600, and the child runs with umask
0077. Credentials are never settings values, URL parameters, API fields or
forwarded log messages. Stderr is drained separately and discarded. Upstream
audio caching is disabled. To forget pairing, disable the plugin and remove
this plugin's credential directory as the server's service user.

## Playback and buffering

The plugin acquires `DirectPlayback`, preserving Kalinka's normal queue and
renderer session. Now-playing metadata is tagged “Spotify Connect”; Spotify
owns queue order. Pause/resume, next/previous and seek in Kalinka are forwarded
to librespot's real `Spirc` controls. Spotify controls come back as ordered
structured events. Volume works in both directions: Spotify's slider controls
Kalinka's selected volume device, and Kalinka/device changes update Spotify.
The renderer's current level is synchronized on acquisition, including its
startup volume ceiling. Compressed audio remains unchanged; volume is applied
by Kalinka's output device, without Spotify software attenuation or normalization.
The patched librespot binary must advertise `"volume": true`, `"reconnect": true`
and `"suspend": true`
in its capability probe; rebuild it from this repository's patch when upgrading
an older bridge.

Audio starts after valid Vorbis headers and the first timed Ogg audio page,
without waiting for the whole track. Each seek/skip creates an independently
decodable stream and a new immutable URL. Pause stops the renderer immediately,
including when production is waiting for credit. Resume continues the same
resource. A discontinuity retires the old capture and replaces renderer read-ahead.
Retired readers stop delivering bytes and wait for the renderer to close its HTTP
request; they do not fabricate EOF or raise a read failure during a normal seek,
skip or shutdown. Actual source failures still abort the response.

The default media read-ahead budget is **2 seconds**, adjustable from 0.5–5
seconds. Credit is based on actual renderer playback snapshots, polled once a
second, with at most one second of extrapolation. One producer packet, a single
Ogg page, can be in flight beyond that budget. Music pages span a fraction of a
second, but silence packs up to 255 tiny Vorbis packets into one page: 5.9
seconds for Spotify's 44.1 kHz streams. Packets spanning more media time than
one page can carry are rejected. Default steady-state maximum lead is therefore
about **3.5 seconds** during music and up to **9 seconds** across silence, plus
control/network scheduling. Produced bytes, HTTP-delivered bytes and
played milliseconds are tracked separately. The renderer's time also corrects
Spotify's Connect clock once a second. Missing readers or feedback pause playback
after 30 seconds; pausing does not grant further credit.

The capture is a private temporary file with a **64 MiB per-generation cap**,
about 28 minutes at 320 kbps (adjustable to 128 MiB). At most two generations can
remain pinned across a seek, for a default **128 MiB disk cap**, and at most two
readers per generation.
Rapid seeks wait up to five seconds for an old reader to close before allocating
another capture. If cleanup stalls, playback pauses and Spotify Connect stays
available; press Play to retry. No earlier bytes are evicted or substituted.
Exceeding a track's byte cap aborts its HTTP response and pauses Spotify at the
last audible position; very long tracks may need a larger setting. Files are unlinked temporary storage, outside music scanning, and close
after the last reader releases them.
Memory holds bounded control messages, one compressed packet (max 1 MiB), Ogg
framing state and small I/O chunks. It never accumulates a whole track in RAM.

An unfinished HTTP GET is `200 audio/ogg`, `Cache-Control: no-store`, without
`Content-Length` or byte-range advertising. An initial range starting at zero,
including the renderer's bounded `Range: bytes=0-383999` probe, receives that
same 200. Other unfinished ranges receive **409**, not
an invented EOF/416. A second initial reader is also rejected, preventing a
retry from silently replaying stale audio. After receiver recovery, restart
playback in Spotify to get a fresh resource after a broken HTTP connection. Completed, retained resources
support correct finite `206` ranges and `416 bytes */<final-size>`.

Temporary lack of bytes is not EOF, including pause. Readers wait asynchronously
outside plugin RPC calls; the maximum idle read is 30 minutes. The renderer
honours the explicit live-response marker during this wait. Reader cancellation,
process failure, disable and shutdown wake readers and clean up owned resources.

Starting a normal Kalinka source, losing the renderer, changing the target or
taking playback back ends Spotify's hold and pauses its Connect session.
Kalinka stays selected in Spotify. Press Play in Spotify to acquire a new hold
on Kalinka's currently selected renderer and resume at the last reported audible
position. Pending audio and old renderer callbacks cannot take the queue back.
The native bridge drops the old decoder at handoff, then loads a fresh stream
with Vorbis headers when playback resumes. Selecting another device in Spotify
still transfers playback normally. Disabling the plugin and server shutdown
stop and reap the owned process without restarting it.

Failures confined to one stream keep librespot and discovery running: invalid
Ogg framing, cache limits or write errors, and a renderer that stops reading or
reporting playback. The capture is retired and Spotify pauses at the last audible
position, as after a renderer error. Once librespot confirms the suspension, the
plugin resumes once, reloading the track there with fresh headers and a new HTTP
resource. Each track gets one automatic retry; a repeat failure, such as a corrupt
page at the same position, waits for Play. Renderer decode errors are not retried.
Tracks Spotify reports as unavailable, including a failed preload of the next
track, are logged and skipped by Spotify. Only receiver failures restart it:
librespot exiting, broken control or audio framing, and failed controls. These
clean up the old session before restarting discovery. The normal queue is retained and resumes only
when explicitly played.

## Current limits

- Local Premium-account playback, volume, handoff and repeated transfers have
  been checked with the user. Remote-renderer playback and the remaining cases
  in the smoke-test checklist still need confirmation; offline tests alone do
  not establish current Spotify service compatibility.
- Track transitions use independent resources. The current DirectPlayback API
  replaces one source and has no “prepare next” operation, so transitions may
  have a short gap. The final packet's credit waits for audible completion;
  librespot does not run multiple tracks ahead. Gapless chaining across tracks
  is not implemented by this plugin.
- HTTP reconnects during capture require a fresh Spotify playback generation.
  Arbitrary upstream byte-range seeking is unavailable on a pipe.
- Spotify software volume, local Spotify files and non-Vorbis streams are not
  supported. Incompatible formats fail visibly.

## Development

```sh
python -m pytest
python -m ruff check src tests scripts
python -m build
```

Tests use generated Ogg fixtures and fake producers/renderers. HTTP integration
tests also require the companion `kalinka-server` source installed in the test
environment. See [`docs/verification.md`](docs/verification.md) for results and
[`docs/smoke-test.md`](docs/smoke-test.md) for the account/hardware procedure.
See [`docs/bridge.md`](docs/bridge.md) for the patch's data/control contract.
