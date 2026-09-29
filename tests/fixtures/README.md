`tone.ogg` is an eight-second, stereo 44.1 kHz sine wave, generated for these
tests. It contains no Spotify content or third-party recording.

Reproduce with FFmpeg and libvorbis:

```sh
ffmpeg -f lavfi -i 'sine=frequency=440:sample_rate=44100:duration=8' \
  -ac 2 -c:a libvorbis -q:a 3 tone.ogg
```
