import asyncio
import time

import pytest
from kalinka_plugin_sdk.datamodel import DeviceVolume, PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import (
    RevokeReason,
    TransportKind,
    TransportRequest,
)
from kalinka_server.live_content import serve
from starlette.requests import Request

from kalinka_plugin_spotify.ogg import InvalidOgg
from kalinka_plugin_spotify.process import ProducerError
from kalinka_plugin_spotify.service import Service


class Producer:
    def __init__(self):
        self.commands = []
        self.data = b""
        self.stopped = False
        self.starts = 0
        self.incoming = asyncio.Queue()
        self.audio_reads = []

    async def start(self):
        self.starts += 1

    async def events(self):
        while True:
            yield await self.incoming.get()

    async def audio(self, length):
        assert length == len(self.data)
        self.audio_reads.append(length)
        return self.data

    async def send(self, op, **fields):
        self.commands.append((op, fields))

    async def stop(self):
        self.stopped = True


class Hold:
    active = True
    renderer_id = "remote-speaker"

    def __init__(self):
        self.sources = []
        self.pauses = self.resumes = 0
        self.released = False
        self.volume = None
        self.volume_requests = []

    async def play(self, source, track):
        self.sources.append((source, track))

    async def pause(self):
        self.pauses += 1

    async def resume(self):
        self.resumes += 1

    async def get_volume(self):
        return self.volume

    async def set_volume(self, percent):
        self.volume_requests.append(percent)

    async def release(self):
        self.released = True
        self.active = False


class Direct:
    def __init__(self):
        self.hold = Hold()
        self.acquisitions = 0

    async def acquire(self, title, listener):
        assert title == "Spotify Connect"
        if not self.hold.active:
            self.hold = Hold()
        self.acquisitions += 1
        return self.hold


@pytest.fixture
async def service(tmp_path):
    s = Service(Direct(), Producer(), tmp_path)
    await s.event({"event": "connected"})
    await s.event(
        {
            "event": "track",
            "title": "Test track",
            "artist": "Artist",
            "album": "Album",
            "duration_ms": 8000,
        }
    )
    await s.event({"event": "playing", "position_ms": 0})
    yield s
    await s.stop()


async def feed(s, page, packet_id):
    s.producer.data = page.data
    await s.event(
        {
            "event": "packet",
            "length": len(page.data),
            "id": packet_id,
            "position_ms": page.time_ms,
        }
    )


async def wait_until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


async def started(s, pages):
    for i, page in enumerate(pages):
        await feed(s, page, i + 1)
        if s.play_started:
            return i
    raise AssertionError("no playback")


async def test_start_before_completion_and_metadata(service, pages):
    i = await started(service, pages)
    c = service.capture
    assert i < len(pages) - 1 and c.size is None
    source, track = service.hold.sources[-1]
    assert source.source.asset_id == c.id and source.sequential
    assert track.title == "Test track" and track.performer.name == "Artist"
    assert service.direct.acquisitions == 1


async def test_fast_producer_waits_for_played_not_delivered(service, pages):
    i = await started(service, pages)
    service.pacer = asyncio.create_task(service._pace())
    c = service.capture
    reader = await c.open(0, None)
    await wait_until(lambda: service.pending is None)
    next_page = next(p for p in pages[i + 1 :] if p.time_ms > service.budget_ms)
    # Include intermediate pages to preserve sequence validation.
    for index in range(i + 1, pages.index(next_page) + 1):
        await feed(service, pages[index], index + 1)
    await reader.read(c.available)
    count = len(service.producer.commands)
    await asyncio.sleep(0.03)
    assert len(service.producer.commands) == count
    assert c.delivered == c.available and c.played_ms == 0
    track = service.hold.sources[-1][1]
    service.on_state(
        PlaybackState(
            state=PlayerStateEnum.PLAYING,
            current_track=track,
            position=next_page.time_ms,
            timestamp_ns=time.monotonic_ns(),
        )
    )
    await wait_until(lambda: service.pending is None)
    assert service.producer.commands[-1][0] == "ack"
    assert (
        "progress",
        {"position_ms": next_page.time_ms, "epoch": 0},
    ) in service.producer.commands
    await reader.aclose()


async def test_absent_renderer_does_not_receive_unlimited_credit(service, pages):
    await started(service, pages)
    service.pacer = asyncio.create_task(service._pace())
    await asyncio.sleep(0.03)
    assert not service.producer.commands
    service.reader_timeout = 0.01
    service.last_feedback -= 1
    service.changed.set()
    await wait_until(lambda: service.error is not None)
    assert "Renderer" in service.error


async def test_pause_resume_while_buffer_full(service, pages):
    await started(service, pages)
    pending = service.pending
    await asyncio.wait_for(service.event({"event": "paused"}), 0.1)
    assert service.paused and service.hold.pauses == 1
    service.pacer = asyncio.create_task(service._pace())
    await asyncio.sleep(0.02)
    assert service.pending == pending and not service.producer.commands
    await service.event({"event": "playing", "position_ms": 0})
    assert not service.paused and service.hold.resumes == 1


async def test_seek_discards_stale_read_ahead_and_uses_fresh_url(service, pages):
    await started(service, pages)
    old = service.capture
    reader = await old.open(0, None)
    old_track = service.hold.sources[-1][1]
    await service.event({"event": "seeked", "position_ms": 4000})
    assert old.retired and service.hold.pauses == 1
    pending = asyncio.create_task(reader.read(100))
    await asyncio.sleep(0.01)
    assert not pending.done()  # Neither stale bytes nor an invented EOF.
    await reader.aclose()
    with pytest.raises(OSError, match="cancelled"):
        await pending
    assert old.file.closed
    await started(service, pages)
    assert service.capture.id != old.id
    source = service.hold.sources[-1][0]
    assert source.timeline_offset_ms == 4000
    service.on_state(PlaybackState(current_track=old_track, position=7000))
    assert service.capture.played_ms == 0
    assert service.direct.acquisitions == 1


@pytest.mark.parametrize("asgi_version", ["2.0", "2.4"])
@pytest.mark.parametrize("action", ["seeked", "track", "stopped", "disable"])
async def test_retired_http_stream_waits_for_renderer_disconnect(
    service, pages, asgi_version, action
):
    await started(service, pages)
    capture = service.capture
    response = await serve(
        capture, "audio/ogg", Request({"type": "http", "method": "GET", "headers": []})
    )
    disconnected = asyncio.Event()
    delivered = asyncio.Event()
    messages = []

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            delivered.set()

    request_task = asyncio.create_task(
        response(
            {"type": "http", "asgi": {"spec_version": asgi_version}}, receive, send
        )
    )
    try:
        await asyncio.wait_for(delivered.wait(), 1)
        if action == "disable":
            await service.stop()
        else:
            await service.event({"event": action, "position_ms": 4000})
        await asyncio.sleep(0.01)
        assert not request_task.done()
        assert capture.retired and not capture.file.closed
        assert all(m.get("more_body", True) for m in messages)
        disconnected.set()
        await asyncio.wait_for(request_task, 1)
        assert capture.file.closed and not capture.readers
    finally:
        disconnected.set()
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)


async def test_track_boundary_uses_new_metadata(service, pages):
    await started(service, pages)
    old = service.capture.id
    await service.event({"event": "loading", "position_ms": 0})
    await service.event({"event": "track", "title": "Next track", "duration_ms": 8000})
    await started(service, pages)
    assert service.capture.id != old
    assert service.hold.sources[-1][1].title == "Next track"


async def test_end_does_not_advance_spotify_until_audio_finishes(service, pages):
    for i, page in enumerate(pages):
        await feed(service, page, i + 1)
    service.pacer = asyncio.create_task(service._pace())
    await asyncio.sleep(0.02)
    assert service.capture.complete and not service.producer.commands
    service.on_finished()
    await wait_until(lambda: service.pending is None)
    assert service.producer.commands[-1] == ("ack", {"id": len(pages)})


@pytest.mark.parametrize(
    "complete, delivered_all, expected_error",
    [(True, True, False), (True, False, True), (False, True, True)],
)
async def test_reader_closing_after_delivery_waits_for_audible_end(
    service, pages, complete, delivered_all, expected_error, caplog
):
    for i, page in enumerate(pages if complete else pages[:-1]):
        await feed(service, page, i + 1)
    capture = service.capture
    reader = await capture.open(0, None)
    await reader.read(capture.available if delivered_all else capture.available - 1)
    if complete and delivered_all:
        assert await reader.read(1) == b""
    await reader.aclose()

    # Long tracks have already exceeded the missing-reader startup timeout
    # when HTTP finishes, even though their last buffered audio is still playing.
    service.started_at -= service.reader_timeout + 1
    pending = service.pending
    service.pacer = asyncio.create_task(service._pace())
    await asyncio.sleep(0.02)
    if expected_error:
        assert service.error == "No renderer is reading Spotify audio; Spotify stopped."
        assert service.error in caplog.text
        return

    assert service.error is None
    assert service.pending == pending
    assert not service.producer.commands
    service.on_finished()
    await wait_until(lambda: service.pending is None)
    assert service.producer.commands[-1] == ("ack", {"id": len(pages)})
    await service.event({"event": "loading", "position_ms": 0})
    await service.event({"event": "track", "title": "Next track", "duration_ms": 8000})
    await started(service, pages)
    assert service.capture is not capture
    assert service.hold.sources[-1][1].title == "Next track"
    assert service.error is None


@pytest.mark.parametrize("kind", list(TransportKind))
async def test_controls_are_forwarded_to_spotify(service, kind):
    service.on_command(
        TransportRequest(kind, position_ms=3000 if kind == TransportKind.SEEK else None)
    )
    await wait_until(lambda: bool(service.producer.commands))
    assert service.producer.commands[0][0] == kind.value
    if kind == TransportKind.SEEK:
        assert service.producer.commands[0][1]["position_ms"] == 3000


@pytest.mark.parametrize(
    "reason",
    [
        RevokeReason.QUEUE_PLAY,
        RevokeReason.OUTPUT_LOST,
        RevokeReason.TAKEN_BACK,
        RevokeReason.OTHER_HOLDER,
        RevokeReason.IDLE,
    ],
)
async def test_takeover_keeps_receiver_selected_and_waits_for_play(
    service, pages, reason
):
    service.start()
    await started(service, pages)
    old = service.capture
    hold = service.hold
    command_start = len(service.producer.commands)
    service.on_revoked(reason)
    assert not service.closed and service.awaiting_play
    service.on_command(TransportRequest(TransportKind.RESUME))
    await service.release_task
    assert service.hold is None and hold.released and old.file.closed
    assert service.producer.commands[command_start:] == [
        ("suspend", {"id": 1, "position_ms": 0})
    ]
    assert not service.producer.stopped and not service.task.done()
    # Commands, cached activation, and already announced bytes from the old
    # session must not interrupt the queue or create another playback hold.
    await service.event({"event": "connected"})
    await service.event({"event": "playing", "position_ms": 9000})
    await feed(service, pages[-1], 99)
    assert service.capture is None and service.direct.acquisitions == 1
    assert service.producer.audio_reads[-1] == len(pages[-1].data)
    assert service.pending is None
    assert service.producer.commands[command_start:] == [
        ("suspend", {"id": 1, "position_ms": 0})
    ]
    # A stale acknowledgement cannot unlock the current handoff.
    await service.event({"event": "suspended", "id": 0})
    assert service.suspending == 1
    await service.event({"event": "suspended", "id": 1})
    assert service.suspending is None and service.awaiting_play
    await service.event({"event": "paused", "position_ms": 0})
    await feed(service, pages[0], 100)
    assert service.capture is None

    # Play resumes the existing Connect selection; no connected event needed.
    await service.event({"event": "loading", "position_ms": 0})
    await service.event({"event": "track", "title": "Resumed", "duration_ms": 8000})
    await service.event({"event": "playing", "position_ms": 0})
    await started(service, pages)
    assert service.capture.id != old.id
    assert service.direct.acquisitions == 2
    assert service.producer.starts == 1 and not service.producer.stopped
    assert service.error is None
    await service.stop()
    assert service.producer.stopped and service.hold is None


async def test_handoff_waits_for_an_inflight_packet_without_closing_receiver(
    service, pages
):
    service.start()
    index = await started(service, pages)
    old = service.capture
    reading, proceed = asyncio.Event(), asyncio.Event()
    audio = service.producer.audio

    async def delayed_audio(length):
        reading.set()
        await proceed.wait()
        return await audio(length)

    service.producer.audio = delayed_audio
    packet = asyncio.create_task(feed(service, pages[index + 1], 100))
    await reading.wait()
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    proceed.set()
    await asyncio.wait_for(asyncio.gather(packet, service.release_task), 1)
    assert old.retired and old.file.closed
    assert service.capture is None and service.pending is None
    assert not service.closed and not service.producer.stopped
    assert not service.task.done()


async def test_disable_during_handoff_stops_receiver_and_cleans_up(service, pages):
    index = await started(service, pages)
    old = service.capture
    reading = asyncio.Event()

    async def blocked_audio(length):
        reading.set()
        await asyncio.Event().wait()

    service.producer.audio = blocked_audio
    service.start()
    await service.producer.incoming.put(
        {"event": "packet", "id": 100, "length": len(pages[index + 1].data)}
    )
    await reading.wait()
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    await asyncio.wait_for(service.stop(), 1)
    assert service.closed and service.producer.stopped
    assert old.file.closed and service.hold is None
    assert service.release_task.done()


async def test_repeated_handoffs_reuse_the_same_receiver(service, pages):
    service.start()
    ids = set()
    for _ in range(3):
        await started(service, pages)
        ids.add(service.capture.id)
        service.on_revoked(RevokeReason.QUEUE_PLAY)
        await service.release_task
        await service.event({"event": "suspended", "id": service.handoff_id})
        await service.event(
            {"event": "track", "title": "Same track", "duration_ms": 8000}
        )
        await service.event({"event": "playing", "position_ms": 0})
    assert len(ids) == 3
    assert service.producer.starts == 1 and not service.producer.stopped
    assert not service.closed and service.error is None


async def test_handoff_preserves_audible_position_and_seeks_while_paused(
    service, pages
):
    await started(service, pages)
    service.on_state(
        PlaybackState(current_track=service.hold.sources[-1][1], position=1234)
    )
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    await service.release_task
    assert service.producer.commands[-1] == ("suspend", {"id": 1, "position_ms": 1234})
    await service.event({"event": "suspended", "id": 1})
    await service.event({"event": "seeked", "position_ms": 4000})
    await service.event({"event": "paused", "position_ms": 4000})
    assert service.direct.acquisitions == 1 and service.awaiting_play
    await service.event({"event": "track", "title": "Same track", "duration_ms": 8000})
    await service.event({"event": "playing", "position_ms": 4000})
    await started(service, pages)
    assert service.hold.sources[-1][0].timeline_offset_ms == 4000
    assert service.direct.acquisitions == 2


async def test_transfer_away_and_back_after_handoff(service, pages):
    await started(service, pages)
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    await service.release_task
    await service.event({"event": "suspended", "id": 1})
    await service.event({"event": "disconnected"})
    assert service.hold is None and service.awaiting_play
    await service.event({"event": "connected"})
    await service.event({"event": "track", "title": "Transferred", "duration_ms": 8000})
    await service.event({"event": "playing", "position_ms": 0})
    await started(service, pages)
    assert service.direct.acquisitions == 2
    assert service.hold.sources[-1][1].title == "Transferred"


async def test_duplicate_revoke_does_not_suspend_a_resumed_stream(service, pages):
    await started(service, pages)
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    service.on_revoked(RevokeReason.OTHER_HOLDER)
    await service.release_task
    assert service.handoff_id == 1
    assert [cmd for cmd in service.producer.commands if cmd[0] == "suspend"] == [
        ("suspend", {"id": 1, "position_ms": 0})
    ]


async def test_ended_hold_before_revoke_callback_is_a_handoff(service, pages):
    from kalinka_plugin_sdk.direct_playback import HoldEnded

    async def ended_play(source, track):
        service.direct.hold.active = False
        raise HoldEnded("queue already owns the output")

    service.direct.hold.play = ended_play
    await started(service, pages)
    await service.release_task
    assert not service.closed and service.error is None
    assert service.hold is None and service.awaiting_play


async def test_missing_output_pauses_receiver_and_play_retries(service, pages):
    from kalinka_plugin_sdk.direct_playback import OutputUnavailable

    acquire = service.direct.acquire

    async def unavailable(*args):
        raise OutputUnavailable("renderer is still reconnecting")

    service.direct.acquire = unavailable
    for i, page in enumerate(pages):
        await feed(service, page, i + 1)
        if service.awaiting_play:
            break
    await service.release_task
    assert not service.closed and service.error is None
    assert service.hold is None and service.awaiting_play
    assert "Output unavailable" in service.status
    service.direct.acquire = acquire
    await service.event({"event": "suspended", "id": 1})
    await service.event({"event": "track", "title": "Retry", "duration_ms": 8000})
    await service.event({"event": "playing", "position_ms": 0})
    await started(service, pages)
    assert service.hold is not None and service.play_started


async def test_unannounced_boundary_rejected(service, pages):
    await started(service, pages)
    with pytest.raises(InvalidOgg):
        await feed(service, pages[0], 100)


@pytest.mark.parametrize(
    "code, text",
    [
        ("authentication_failed", "authentication"),
        ("incompatible_format", "incompatible"),
        ("track_unavailable", "Ogg/Vorbis"),
    ],
)
async def test_useful_process_failures(service, code, text):
    with pytest.raises(ProducerError, match=text):
        await service.event({"event": "error", "code": code})


@pytest.mark.parametrize("asgi_version", ["2.0", "2.4"])
@pytest.mark.parametrize("disconnect_index", [0, 1])
async def test_seek_waits_for_either_retired_http_reader(
    service, pages, asgi_version, disconnect_index
):
    requests = []
    pending = None
    try:
        for position in (1000, 6000):
            await started(service, pages)
            capture = service.capture
            response = await serve(
                capture,
                "audio/ogg",
                Request({"type": "http", "method": "GET", "headers": []}),
            )
            disconnected, delivered = asyncio.Event(), asyncio.Event()
            messages = []

            async def receive(disconnected=disconnected):
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message, messages=messages, delivered=delivered):
                messages.append(message)
                if message["type"] == "http.response.body":
                    delivered.set()

            task = asyncio.create_task(
                response(
                    {"type": "http", "asgi": {"spec_version": asgi_version}},
                    receive,
                    send,
                )
            )
            requests.append((disconnected, task, capture, messages))
            await asyncio.wait_for(delivered.wait(), 1)
            await service.event({"event": "seeked", "position_ms": position})

        pending = asyncio.create_task(feed(service, pages[0], 100))
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert service.capture is None
        assert len(service.captures) == 2
        assert not service.closed and service.error is None

        disconnected, task, released, _ = requests[disconnect_index]
        disconnected.set()
        await asyncio.wait_for(task, 1)
        await asyncio.wait_for(pending, 1)
        assert released.file.closed
        assert service.capture.offset_ms == 6000
        assert len(service.captures) == 2
        assert not requests[1 - disconnect_index][1].done()
        for _, _, _, messages in requests:
            assert all(message.get("more_body", True) for message in messages)
        for i, page in enumerate(pages[1:], 101):
            await feed(service, page, i)
            if service.play_started:
                break
        assert service.play_started and service.direct.acquisitions == 1
        assert service.hold.sources[-1][0].timeline_offset_ms == 6000
        assert not service.producer.stopped and service.error is None
    finally:
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        for disconnected, _, _, _ in requests:
            disconnected.set()
        await asyncio.gather(*(task for _, task, _, _ in requests))


async def test_stalled_retired_readers_pause_without_losing_connect(service, pages):
    service.reader_close_timeout = 0.02
    service.start()
    await wait_until(lambda: service.producer.starts == 1)
    readers = []
    for _ in range(2):
        await started(service, pages)
        readers.append(await service.capture.open(0, None))
        await service.event({"event": "seeked", "position_ms": 0})
    await feed(service, pages[0], 100)
    await asyncio.wait_for(service.release_task, 1)
    assert service.awaiting_play and service.output_error
    assert service.error is None and not service.closed
    assert not service.task.done() and not service.producer.stopped
    assert service.capture is None and len(service.captures) == 2
    assert all(not c.file.closed for c in service.captures)
    assert any(op == "suspend" for op, _ in service.producer.commands)
    assert not any(op == "disconnect" for op, _ in service.producer.commands)
    for reader in readers:
        await reader.aclose()
    assert all(c.file.closed for c in service.captures)
    await service.event({"event": "suspended", "id": service.suspending})
    await service.event({"event": "track", "title": "Retry", "duration_ms": 8000})
    await service.event({"event": "playing", "position_ms": 4000})
    await started(service, pages)
    assert service.play_started and not service.awaiting_play
    assert service.capture.offset_ms == 4000
    assert service.producer.starts == 1 and not service.producer.stopped
    assert service.direct.acquisitions == 2


async def test_repeated_fast_seeks_keep_cache_bounded(service, pages):
    service.start()
    await wait_until(lambda: service.producer.starts == 1)
    readers = []
    await started(service, pages)
    try:
        for index in range(20):
            readers.append(await service.capture.open(0, None))
            position = 1000 if index % 2 else 6000
            await service.event({"event": "seeked", "position_ms": position})
            replacement = asyncio.create_task(started(service, pages))
            try:
                if len(readers) == 2:
                    await asyncio.sleep(0.005)
                    assert not replacement.done()
                    await readers.pop(0).aclose()
                await asyncio.wait_for(replacement, 1)
            finally:
                replacement.cancel()
                await asyncio.gather(replacement, return_exceptions=True)
            assert len(service.captures) <= 2
            assert service.capture.offset_ms == position
            assert service.error is None and not service.closed
            assert service.play_started
        assert service.producer.starts == 1 and not service.producer.stopped
        assert service.direct.acquisitions == 1
    finally:
        for reader in readers:
            await reader.aclose()


async def test_disable_interrupts_waiting_for_retired_readers(service, pages):
    service.start()
    await wait_until(lambda: service.producer.starts == 1)
    readers = []
    for _ in range(2):
        await started(service, pages)
        readers.append(await service.capture.open(0, None))
        await service.event({"event": "seeked", "position_ms": 0})
    service.producer.data = pages[0].data
    await service.producer.incoming.put(
        {"event": "packet", "length": len(pages[0].data), "id": 100}
    )
    await asyncio.sleep(0.02)
    assert service.error is None and not service.task.done()
    await asyncio.wait_for(service.stop(), 0.5)
    assert service.closed and service.producer.stopped
    assert service.task.done() and service.error is None
    for reader in readers:
        await reader.aclose()
    assert all(c.file.closed for c in service.captures)


async def test_large_media_time_packet_cannot_bypass_credit_budget(service, pages):
    service.producer.data = b"".join(p.data for p in pages)
    with pytest.raises(InvalidOgg, match="pacing allowance"):
        await service.event(
            {"event": "packet", "length": len(service.producer.data), "id": 1}
        )
    assert not service.play_started
    assert service.capture.available == 0


async def test_renderer_volume_wins_over_spotify_cached_volume_on_acquire(
    service, pages
):
    service.direct.hold.volume = DeviceVolume(current_volume=24, max_volume=80)
    await service.event({"event": "volume", "volume": 65535})
    await started(service, pages)
    await wait_until(lambda: bool(service.producer.commands))
    assert service.producer.commands == [("volume", {"volume": 19661})]
    assert not service.hold.volume_requests


@pytest.mark.parametrize(
    "volume, percent", [(0, 0), (16384, 25), (32768, 50), (65535, 100)]
)
async def test_spotify_volume_controls_the_renderer(service, pages, volume, percent):
    service.direct.hold.volume = DeviceVolume(current_volume=24, max_volume=80)
    await started(service, pages)
    await wait_until(lambda: bool(service.producer.commands))
    await service.event({"event": "volume", "volume": volume})
    assert service.hold.volume_requests == [percent]
    # Kalinka's callback confirms the applied hardware level, in its own steps.
    service.on_volume(
        DeviceVolume(current_volume=round(percent * 80 / 100), max_volume=80)
    )
    await asyncio.sleep(0)
    assert service.error is None
    assert service.spotify_volume == volume


async def test_actual_volume_steps_are_reported_without_a_command_loop(service, pages):
    service.direct.hold.volume = DeviceVolume(current_volume=3, max_volume=10)
    await started(service, pages)
    await wait_until(lambda: bool(service.producer.commands))
    await service.event({"event": "volume", "volume": 32768})
    assert service.hold.volume_requests == [50]
    service.on_volume(DeviceVolume(current_volume=5, max_volume=10))
    await service.event({"event": "volume", "volume": 33000})
    # Both requests round to 50%; report the actual 5/10 level back to Spotify.
    await wait_until(
        lambda: service.producer.commands[-1] == ("volume", {"volume": 32768})
    )
    count = len(service.producer.commands)
    await service.event({"event": "volume", "volume": 32768})
    service.on_volume(DeviceVolume(current_volume=5, max_volume=10))
    await asyncio.sleep(0)
    assert service.hold.volume_requests == [50]
    assert len(service.producer.commands) == count


@pytest.mark.parametrize(
    "current, maximum, expected",
    [
        (0, 80, 0),
        (20, 80, 16384),
        (40, 80, 32768),
        (80, 80, 65535),
        (-2, 80, 0),
        (90, 80, 65535),
    ],
)
async def test_kalinka_volume_is_scaled_to_spotify(service, current, maximum, expected):
    service.on_volume(DeviceVolume(current_volume=current, max_volume=maximum))
    await wait_until(lambda: bool(service.producer.commands))
    assert service.producer.commands == [("volume", {"volume": expected})]


@pytest.mark.parametrize(
    "volume",
    [None, DeviceVolume(supported=False, max_volume=100), DeviceVolume(max_volume=0)],
)
async def test_unavailable_volume_does_not_set_the_output(service, pages, volume):
    service.direct.hold.volume = volume
    await started(service, pages)
    await service.event({"event": "volume", "volume": 32768})
    await asyncio.sleep(0)
    assert not service.hold.volume_requests
    assert not service.producer.commands


async def test_slider_changes_are_coalesced_while_the_control_socket_is_busy(service):
    entered, ready = asyncio.Event(), asyncio.Event()
    send = service.producer.send

    async def slow_send(op, **fields):
        entered.set()
        await ready.wait()
        await send(op, **fields)

    service.producer.send = slow_send
    service.on_volume(DeviceVolume(current_volume=1, max_volume=1000))
    await entered.wait()
    for volume in range(2, 501):
        service.on_volume(DeviceVolume(current_volume=volume, max_volume=1000))
    assert len(service.commands) == 1
    ready.set()
    await wait_until(lambda: service.volume_reporter.done())
    assert service.producer.commands == [
        ("volume", {"volume": 66}),
        ("volume", {"volume": 32768}),
    ]
    assert service.error is None


async def test_late_volume_events_after_revoke_do_not_change_output(service, pages):
    service.direct.hold.volume = DeviceVolume(current_volume=30, max_volume=100)
    await started(service, pages)
    await wait_until(lambda: bool(service.producer.commands))
    service.on_revoked(RevokeReason.OUTPUT_LOST)
    service.on_volume(DeviceVolume(current_volume=60, max_volume=100))
    await service.event({"event": "volume", "volume": 65535})
    await asyncio.sleep(0)
    await service.release_task
    assert not service.direct.hold.volume_requests
    assert service.producer.commands == [
        ("volume", {"volume": 19661}),
        ("suspend", {"id": 1, "position_ms": 0}),
    ]


@pytest.mark.parametrize("reported_position", [None, 0])
@pytest.mark.parametrize("had_playback", [False, True])
async def test_renderer_error_keeps_connect_alive_and_preserves_position(
    service, pages, reported_position, had_playback
):
    from kalinka_plugin_spotify import KalinkaPluginSpotify

    service.start()
    await service.event({"event": "loading", "position_ms": 4000})
    await service.event({"event": "playing", "position_ms": 4000})
    await started(service, pages)
    hold, capture = service.hold, service.capture
    track = hold.sources[-1][1]
    if had_playback:
        service.on_state(
            PlaybackState(
                state=PlayerStateEnum.PLAYING,
                current_track=track,
                position=5234,
                timestamp_ns=time.monotonic_ns(),
            )
        )
    service.on_state(
        PlaybackState(
            state=PlayerStateEnum.ERROR,
            current_track=track,
            position=reported_position,
            message="Flac decoder error: FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC",
            timestamp_ns=time.monotonic_ns(),
        )
    )
    assert not service.closed and service.error is None
    assert service.awaiting_play
    await service.release_task
    assert capture.retired and hold.released and service.hold is None
    assert service.producer.commands[-1] == (
        "suspend",
        {"id": 1, "position_ms": 5234 if had_playback else 4000},
    )
    assert not service.task.done() and not service.producer.stopped
    assert all(command != "disconnect" for command, _ in service.producer.commands)
    plugin = KalinkaPluginSpotify()
    plugin.enabled, plugin.service = True, service
    health = await plugin.get_state()
    assert health.state.value == "warning"
    assert "renderer" in health.message.lower() and "Play" in health.message


async def test_renderer_switch_failure_can_retry_repeatedly_then_resume(service, pages):
    service.start()
    await started(service, pages)
    # Changing the selected renderer already suspends this sequential source.
    service.on_revoked(RevokeReason.OUTPUT_LOST)
    await service.release_task
    failed_tracks = []
    for attempt in range(2):
        await service.event({"event": "suspended", "id": service.handoff_id})
        await service.event({"event": "loading", "position_ms": 1234})
        await service.event({"event": "track", "title": "Retry", "duration_ms": 8000})
        await service.event({"event": "playing", "position_ms": 1234})
        await started(service, pages)
        track = service.hold.sources[-1][1]
        failed_tracks.append(track)
        service.on_state(
            PlaybackState(
                state=PlayerStateEnum.ERROR,
                current_track=track,
                position=0,
                timestamp_ns=time.monotonic_ns(),
            )
        )
        assert not service.closed
        await service.release_task
        # Old audio/callbacks must not claim an output again during suspension.
        await feed(service, pages[-1], 99 + attempt)
        assert service.capture is None
        assert service.producer.commands[-1] == (
            "suspend",
            {"id": attempt + 2, "position_ms": 1234},
        )
    await service.event({"event": "suspended", "id": service.handoff_id})
    await service.event(
        {"event": "track", "title": "Compatible renderer", "duration_ms": 8000}
    )
    await service.event({"event": "playing", "position_ms": 1234})
    await started(service, pages)
    current_track = service.hold.sources[-1][1]
    service.on_state(
        PlaybackState(
            state=PlayerStateEnum.PLAYING,
            current_track=current_track,
            position=1500,
            timestamp_ns=time.monotonic_ns(),
        )
    )
    for track in failed_tracks:
        service.on_state(
            PlaybackState(
                state=PlayerStateEnum.ERROR,
                current_track=track,
                position=0,
                timestamp_ns=time.monotonic_ns(),
            )
        )
    assert service.output_error is None and service.error is None
    assert service.play_started and not service.awaiting_play
    assert service.handoff_id == 3
    assert service.producer.starts == 1 and not service.producer.stopped
    assert not service.task.done()
