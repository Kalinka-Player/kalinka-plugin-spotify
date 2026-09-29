# Companion Kalinka changes

The Spotify implementation belongs to this repository. Three generic Kalinka
capabilities are needed for its capture path:

The companion changes landed in
[KalinkaPlayer PR #225](https://github.com/Kalinka-Player/KalinkaPlayer/pull/225),
merge commit `ad91659ba259f9dbc195f3618653985116407263`.

| Component | Change |
| --- | --- |
| SDK 3.5 | `ContentInfo.live`, `LiveContent`/`LiveReader` protocol, explicit HTTP read errors; `TrackSource.sequential` and `timeline_offset_ms` |
| Server | Serve asynchronous unfinished content without a final length; accept bounded initial range probes with a full live response; poll renderer snapshots for sequential direct playback; report timeline offsets; revoke sequential holds on target changes/reconnects; fence stale stream callbacks |
| Renderer | Treat unknown HTTP length as unknown, parse ordinary finite Content-Length, accept live chunked responses, allow an explicitly live response to wait during pause, let the existing Vorbis decoder consume short reads without waiting for a full input buffer, report current playback position in snapshots, preserve the position when refilling after a stall, and reject unknown formats with `Unsupported stream format` instead of falling back to FLAC |

Kalinka's existing Ogg/Vorbis decoder is used, with its input callback adjusted
for paced live sources. Finite local/remote file serving still uses
the existing content path. Stream URLs use Kalinka's existing renderer-reachable
address binding and access conventions.

CI and release builds use that merged core commit directly through
`.github/actions/companion-core`. No separate Kalinka core patch is needed.
For local development, use a KalinkaPlayer checkout containing that commit
and install its SDK and server:

```sh
python -m pip install -e packages/kalinka-plugin-sdk -e packages/kalinka-server
```

Build/install the renderer from that checkout using Kalinka's normal build and
packaging instructions. Install SDK and server together. An SDK-only upgrade
is insufficient: the server must understand `ContentInfo.live`, and the
renderer must recognise `X-Kalinka-Live: 1` and unknown length and report advancing
playback snapshots. The plugin's `.deb` does not replace
the native renderer executable.

Unsupported-format reporting also requires a renderer upgrade on each device.
The renderer selects a decoder from a recognised MIME type or bare format name;
if metadata is absent or `application/octet-stream`, it checks the URL's file
extension. An unknown format produces a decoder error before fetching the stream,
and a supported source can play afterward. A queued unsupported source reports
its error when it becomes current. Updating only the Spotify plugin cannot change
the FLAC fallback in older renderer binaries.
