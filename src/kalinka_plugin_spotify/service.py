"""Spotify owns the queue; Kalinka owns the renderer and reports audible progress."""

import asyncio
import logging
import tempfile
import time
import traceback
from pathlib import Path

from kalinka_plugin_sdk.datamodel import (
    Album,
    Artist,
    CoverImage,
    EntityId,
    PlayerStateEnum,
    Track,
)
from kalinka_plugin_sdk.direct_playback import (
    HoldEnded,
    OutputUnavailable,
    RevokeReason,
)
from kalinka_plugin_sdk.inputmodule import ModuleAsset, TrackSource

from .cache import Capture, CaptureError
from .ogg import InvalidOgg, OggParser
from .process import ProducerError

logger = logging.getLogger(__name__)

ERRORS = {
    "authentication_failed": "Spotify authentication failed; pair again from the Spotify app using a Premium account.",
    "track_unavailable": "Spotify could not load this track as Ogg/Vorbis.",
    "incompatible_format": "Spotify supplied an incompatible audio format; only Ogg/Vorbis passthrough is supported.",
    "control_failed": "Spotify did not accept a playback control.",
}


class Service:
    def __init__(
        self,
        direct,
        producer,
        cache_directory: Path,
        *,
        budget_ms=2000,
        max_bytes=32 * 1024 * 1024,
        reader_timeout=30,
    ):
        self.direct, self.producer = direct, producer
        self.budget_ms, self.max_bytes = budget_ms, max_bytes
        self.reader_timeout = reader_timeout
        cache_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_directory.chmod(0o700)
        self.directory = tempfile.TemporaryDirectory(
            prefix="playback-", dir=cache_directory
        )
        self.capture = None
        self.captures = set()
        self.bridge_epoch = 0
        self.parser = OggParser()
        self.metadata = {}
        self.hold = None
        self.task = self.pacer = None
        self.cleanup_task = None
        self.commands = set()
        self.pending = None
        self.paused = False
        self.ended = False
        self.closed = False
        self.error = None
        self.output_error = None
        self.status = "Waiting for Spotify"
        self.offset_ms = 0
        self.state = None
        self.changed = asyncio.Event()
        self.started_at = time.monotonic()
        self.last_feedback = self.started_at
        self.feedback_sent_at = 0.0
        self.finished = False
        self.play_started = False
        self.generation = 0
        self.volume = None
        self.spotify_volume = None
        self.volume_pending = None
        self.volume_reporter = None
        self.awaiting_play = False
        self.suspending = None
        self.handoff_id = 0
        self.release_task = None
        self.event_lock = asyncio.Lock()

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        stage = "starting librespot"
        try:
            await self.producer.start()
            self.pacer = asyncio.create_task(self._pace())
            async for event in self.producer.events():
                if self.closed:
                    break
                stage = "handling librespot event"
                await self.event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Only locally constructed, credential-free errors reach settings.
            self.error = (
                str(exc)
                if isinstance(exc, (ProducerError, InvalidOgg, CaptureError))
                else "Spotify playback failed; disable/re-enable to reconnect."
            )
            self.status = self.error
            logger.error("Spotify Connect: %s", self.error)
            # Exception text/locals can contain upstream data. Code locations
            # and the exception type still identify unexpected failures.
            frames = traceback.extract_tb(exc.__traceback__)
            logger.error(
                "Spotify failure while %s: %s; %s",
                stage,
                type(exc).__name__,
                " -> ".join(
                    f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in frames
                ),
            )
        finally:
            await self._cleanup()

    async def _invalidate(self):
        self.generation += 1
        self.pending = None
        self.play_started = self.finished = False
        self.state = None
        if self.capture:
            # Stop serving bytes, but let replacing/releasing the renderer
            # close its HTTP request. Raising a read error first races that
            # disconnect and logs an ASGI failure on every seek or skip.
            await self.capture.retire(wait_for_disconnect=True)
            self.capture = None
        self.parser = OggParser()
        self.changed.set()
        # Flush renderer read-ahead promptly; pause until a replacement is valid.
        if self.hold and self.hold.active:
            await self.hold.pause()

    async def event(self, event):
        async with self.event_lock:
            try:
                await self._event(event)
            except OutputUnavailable:
                # The receiver can reconnect before the renderer after a server
                # restart. Keep Connect available and let Play retry the output.
                self._pause_for_output_error(
                    "Output unavailable; press Play in Spotify to retry"
                )
            except HoldEnded:
                # Revocation can race a play/pause call before the listener
                # receives on_revoked. Treat the ended hold as a handoff.
                if self.hold is not None and not self.hold.active:
                    self.on_revoked(RevokeReason.OUTPUT_LOST)
                else:
                    raise

    async def _event(self, event):
        kind = event["event"]
        if kind == "hidden_tracks":
            count = event.get("count")
            if type(count) is int and count >= 0:
                logger.info("Spotify hidden-song list updated: %d entries", count)
            return
        if self.suspending is not None:
            # The marker is serialized with the native player's packet writes.
            # Drain all older audio without allowing it to retake the queue.
            if kind == "packet":
                await self.producer.audio(event["length"])
            elif kind == "suspended" and event.get("id") == self.suspending:
                self.suspending = None
                self.bridge_epoch = event.get("epoch", self.bridge_epoch)
            if kind not in ("error", "ready"):
                return
        if self.awaiting_play:
            if kind == "playing":
                self.awaiting_play = False
            elif kind == "packet":
                await self.producer.audio(event["length"])
                return
        self.bridge_epoch = event.get("epoch", self.bridge_epoch)
        if kind == "error":
            raise ProducerError(
                ERRORS.get(event.get("code"), "Spotify process reported an error.")
            )
        if kind == "ready":
            if event.get("protocol") != 1:
                raise ProducerError("Unsupported librespot control protocol")
        elif kind == "connected":
            self.status = "Connected; Spotify manages the queue"
        elif kind in ("loading", "seeked"):
            await self._invalidate()
            self.offset_ms = int(event["position_ms"])
        elif kind == "track":
            await self._invalidate()
            self.metadata = event.copy()
            self.offset_ms = 0
        elif kind == "playing":
            if self.capture is None:
                self.offset_ms = int(event["position_ms"])
            self.paused = False
            if self.hold and self.play_started:
                await self.hold.resume()
            self.status = "Playing; Spotify manages the queue"
            self.changed.set()
        elif kind == "paused":
            self.paused = True
            if self.hold:
                await self.hold.pause()
            self.status = "Paused; Spotify manages the queue"
            self.changed.set()
        elif kind == "packet":
            await self._packet(event)
        elif kind == "volume":
            await self._set_volume(event.get("volume"))
        elif kind == "end":
            if self.capture:
                self.parser.finish()
                await self.capture.finish()
        elif kind in ("stopped", "disconnected"):
            await self._invalidate()
            if self.hold:
                await self.hold.release()
                self.hold = None
            self.volume = self.spotify_volume = self.volume_pending = None
            self.status = "Waiting for Spotify"

    async def _packet(self, event):
        data = await self.producer.audio(event["length"])
        if self.closed or self.awaiting_play:
            return
        pages = self.parser.feed(data)
        if self.capture is None:
            if not self.metadata:
                raise InvalidOgg("Audio arrived without track metadata")
            self.captures = {c for c in self.captures if not c.file.closed}
            if len(self.captures) >= 2:
                raise ProducerError(
                    "Previous renderer readers still hold the playback cache; reconnect Spotify."
                )
            self.capture = Capture(
                Path(self.directory.name),
                max_bytes=self.max_bytes,
                metadata=self.metadata.copy(),
                offset_ms=self.offset_ms,
            )
            self.captures.add(self.capture)
            self.started_at = self.last_feedback = time.monotonic()
        capture = self.capture
        if pages and pages[-1].time_ms - capture.produced_ms > 1500:
            raise InvalidOgg("Ogg packet exceeds the 1.5 second pacing allowance")
        for page in pages:
            if page.bos and capture.available:
                raise InvalidOgg("Unexpected chained stream without a track boundary")
            await capture.append(page.data)
            capture.produced_ms = page.time_ms
            if page.eos:
                self.parser.finish()
                await capture.finish()
        if (
            pages
            and pages[-1].headers_ready
            and capture.produced_ms > 0
            and not self.play_started
            and not self.awaiting_play
        ):
            if self.hold is None:
                self.hold = await self.direct.acquire("Spotify Connect", self)
                # The renderer's current level (including its startup ceiling)
                # wins over a cached Spotify volume when taking the output.
                try:
                    self.on_volume(await self.hold.get_volume())
                except Exception:
                    logger.warning("Spotify could not read the output volume")
            if self.closed or self.awaiting_play:
                return
            track = make_track(capture.metadata, capture.id)
            self.play_started = True
            source = TrackSource(
                source=ModuleAsset(module="spotify", asset_id=capture.id),
                format="ogg",
                sequential=True,
                timeline_offset_ms=capture.offset_ms,
            )
            await self.hold.play(source, track)
            if self.paused:
                await self.hold.pause()
        if self.awaiting_play:
            return
        self.pending = (event["id"], self.generation)
        self.changed.set()

    async def _pace(self):
        try:
            while not self.closed:
                self.changed.clear()
                now = time.monotonic()
                c = self.capture
                if c and self.pending and not self.awaiting_play:
                    packet_id, generation = self.pending
                    if not self.paused:
                        if now - self.last_feedback > self.reader_timeout:
                            raise ProducerError(
                                "Renderer stopped reporting playback; Spotify stopped."
                            )
                        if (
                            not c.readers
                            and not self.finished
                            # HTTP can finish before the last buffered audio.
                            # Keep the final credit held until on_finished,
                            # without treating full delivery as reader loss.
                            and not (c.complete and c.delivered == c.available)
                            and now - self.started_at > self.reader_timeout
                        ):
                            raise ProducerError(
                                "No renderer is reading Spotify audio; Spotify stopped."
                            )
                        played = c.played_ms
                        # Feedback is in media milliseconds. Extrapolate for at most
                        # one second so stale renderer reports never fund production.
                        if self.state and self.state.state == PlayerStateEnum.PLAYING:
                            played += min(1000, int((now - self.last_feedback) * 1000))
                        allowed = not self.play_started or (
                            bool(c.readers) and c.produced_ms <= played + self.budget_ms
                        )
                        # Hold the last packet's credit until audible completion.
                        # This prevents librespot advancing to the next track early.
                        if c.complete:
                            allowed = self.finished
                        if allowed and generation == self.generation:
                            self.pending = None
                            await self.producer.send("ack", id=packet_id)
                try:
                    await asyncio.wait_for(self.changed.wait(), 0.1)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = (
                str(exc) if isinstance(exc, ProducerError) else "Spotify pacing failed."
            )
            self.status = self.error
            c = self.capture
            logger.error(
                "Spotify pacing stopped: %s; complete=%s, delivered=%s/%s, readers=%s, produced_ms=%s, played_ms=%s",
                self.error,
                c.complete if c else None,
                c.delivered if c else None,
                c.available if c else None,
                len(c.readers) if c else 0,
                c.produced_ms if c else None,
                c.played_ms if c else None,
            )
            if self.task:
                self.task.cancel()

    def on_state(self, state):
        if self.closed or self.awaiting_play:
            return
        c = self.capture
        # Ignore state callbacks still in flight from the previous resource.
        if not c or not state.current_track or state.current_track.id.id != c.id:
            return
        if state.state == PlayerStateEnum.ERROR:
            logger.error(
                "Spotify renderer failed: %s", state.message or "No error detail"
            )
            # The receiver is healthy even when the selected output cannot
            # decode this source. Keep its discovery/session alive and suspend
            # at the last good position; an error often reports zero or None.
            self._pause_for_output_error(
                "Renderer could not play Spotify; select a compatible renderer "
                "and press Play in Spotify to retry"
            )
            return
        self.state = state
        self.last_feedback = time.monotonic()
        c.played_ms = max(0, (state.position or 0) - c.offset_ms)
        if state.state == PlayerStateEnum.PLAYING:
            self.output_error = None
        if (
            self.last_feedback - self.feedback_sent_at >= 1
            and state.position is not None
        ):
            self.feedback_sent_at = self.last_feedback
            self._command(
                "progress", position_ms=state.position, epoch=self.bridge_epoch
            )
        self.changed.set()

    def _pause_for_output_error(self, message):
        self.on_revoked(RevokeReason.OUTPUT_LOST)
        self.output_error = self.status = message

    def on_finished(self):
        if self.awaiting_play or not self.capture or not self.capture.complete:
            return
        self.finished = True
        self.changed.set()

    def on_command(self, request):
        if self.closed or self.awaiting_play:
            return
        fields = (
            {"position_ms": request.position_ms} if request.kind.value == "seek" else {}
        )
        self._command(request.kind.value, **fields)

    def _command(self, op, **fields):
        if len(self.commands) >= 32:
            self.error = "Spotify control queue is full."
            self.on_revoked(None)
            return

        async def send():
            try:
                await self.producer.send(op, **fields)
            except Exception:
                self.error = "Spotify playback control failed."
                self.on_revoked(None)

        task = asyncio.create_task(send())
        self.commands.add(task)
        task.add_done_callback(self.commands.discard)

    def on_revoked(self, reason):
        if self.closed:
            return
        if reason is not None and reason != RevokeReason.SHUTDOWN and not self.error:
            if self.awaiting_play:
                return
            # Latch before cleanup so queued packets/callbacks cannot take the
            # queue back. A new Play after native suspension can acquire.
            self.awaiting_play = True
            self.handoff_id += 1
            self.suspending = self.handoff_id
            position = self.state.position if self.state else None
            if position is None:
                position = self.offset_ms + (
                    self.capture.played_ms if self.capture else 0
                )
            self.pending = None
            self.status = "Paused; press Play in Spotify to resume"
            self.release_task = asyncio.create_task(
                self._release_output(max(0, int(position)), self.handoff_id)
            )
            self.changed.set()
            return
        self.closed = True
        self.status = self.error or "Spotify stopped"
        if self.task:
            self.task.cancel()
        self.changed.set()

    async def _release_output(self, position_ms, handoff_id):
        try:
            async with self.event_lock:
                for task in self.commands:
                    task.cancel()
                await asyncio.gather(*self.commands, return_exceptions=True)
                hold, self.hold = self.hold, None
                await self._invalidate()
                self.metadata = {}
                self.offset_ms = 0
                self.paused = True
                self.volume = self.spotify_volume = self.volume_pending = None
                self.volume_reporter = None
                if hold:
                    await hold.release()
                # Keep Connect selected. Its native suspension marker fences
                # stale packets; a later Play loads fresh headers at this point.
                await self.producer.send(
                    "suspend", id=handoff_id, position_ms=position_ms
                )
        except Exception:
            self.error = (
                "Spotify could not release playback; disable/re-enable to reconnect."
            )
            logger.error(self.error)
            self.on_revoked(None)

    async def _set_volume(self, volume):
        if (
            self.closed
            or self.awaiting_play
            or not self.hold
            or not self.hold.active
            or not self.volume
            or not self.volume.supported
            or self.volume.max_volume <= 0
            or type(volume) is not int
            or not 0 <= volume <= 65535
        ):
            return
        self.spotify_volume = volume
        self.volume_pending = None
        percent = (volume * 100 + 32767) // 65535
        current = round(self.volume.current_volume * 100 / self.volume.max_volume)
        if percent == current:
            self.on_volume(self.volume)
            return
        try:
            await self.hold.set_volume(percent)
            # The output's on_volume callback reports the level it actually
            # applies, including rounding to an amplifier's own volume steps.
        except Exception:
            logger.warning("Spotify could not change the output volume")
            self.on_volume(self.volume)

    def on_volume(self, volume):
        if self.closed or self.awaiting_play:
            return
        self.volume = volume
        if not volume or not volume.supported or volume.max_volume <= 0:
            self.volume_pending = None
            return
        current = max(0, min(volume.current_volume, volume.max_volume))
        value = (current * 65535 + volume.max_volume // 2) // volume.max_volume
        self.volume_pending = value if value != self.spotify_volume else None
        if self.volume_pending is not None and (
            self.volume_reporter is None or self.volume_reporter.done()
        ):
            self.volume_reporter = asyncio.create_task(self._report_volume())
            self.commands.add(self.volume_reporter)
            self.volume_reporter.add_done_callback(self.commands.discard)

    async def _report_volume(self):
        try:
            # A dragged slider can produce many changes before a socket write
            # completes. Keep only its latest level, outside the audio lane.
            while (
                not self.closed
                and not self.awaiting_play
                and self.volume_pending is not None
            ):
                value, self.volume_pending = self.volume_pending, None
                self.spotify_volume = value
                await self.producer.send("volume", volume=value)
        except Exception:
            self.spotify_volume = None
            logger.warning("Spotify could not report the output volume")

    async def _cleanup(self):
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self.cleanup_task)

    async def _close_resources(self):
        self.closed = True
        if self.release_task:
            self.release_task.cancel()
            await asyncio.gather(self.release_task, return_exceptions=True)
        if self.pacer:
            self.pacer.cancel()
            await asyncio.gather(self.pacer, return_exceptions=True)
        for task in self.commands:
            task.cancel()
        await asyncio.gather(*self.commands, return_exceptions=True)
        for capture in self.captures:
            await capture.retire(self.error, wait_for_disconnect=self.error is None)
        try:
            await self.producer.send("disconnect")
        except Exception:
            pass
        try:
            await self.producer.stop()
        finally:
            try:
                if self.hold:
                    await self.hold.release()
                    self.hold = None
            finally:
                self.directory.cleanup()

    async def stop(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._cleanup()


def make_track(metadata, generation):
    def entity(kind, value):
        return EntityId(source="spotify", type=kind, id=value or "unknown")

    artist = Artist(
        id=entity("artist", metadata.get("artist")), name=metadata.get("artist", "")
    )
    album = Album(
        id=entity("album", metadata.get("album")),
        title=metadata.get("album", ""),
        artist=artist,
        image=CoverImage(large=metadata.get("cover") or ""),
    )
    # Generation identity fences delayed renderer callbacks after a seek.
    return Track(
        id=entity("track", generation),
        title=metadata.get("title", "Spotify"),
        duration=int(metadata.get("duration_ms", 0)) // 1000,
        performer=artist,
        album=album,
    )
