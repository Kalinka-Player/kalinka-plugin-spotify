"""Pacing from timestamped renderer transitions, without periodic feedback."""

import asyncio
import time

import pytest
from kalinka_plugin_sdk.datamodel import PlaybackState, PlayerStateEnum
from kalinka_plugin_sdk.direct_playback import RevokeReason
from test_service import service as service  # noqa: F401
from test_service import started, wait_until


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now_ns = time.monotonic_ns()

        def monotonic_ns(self):
            return self.now_ns

        def monotonic(self):
            return self.now_ns / 1_000_000_000

        def advance(self, seconds):
            self.now_ns += int(seconds * 1_000_000_000)

    clock = Clock()
    monkeypatch.setattr("kalinka_plugin_spotify.service.time", clock)
    return clock


def control_point(service, clock, position, state=PlayerStateEnum.PLAYING, **kwargs):
    return PlaybackState(
        current_track=service.hold.sources[-1][1],
        state=state,
        position=position,
        timestamp_ns=clock.monotonic_ns(),
        **kwargs,
    )


def progress(service):
    return [
        fields["position_ms"]
        for op, fields in service.producer.commands
        if op == "progress"
    ]


async def test_local_ticks_pace_and_report_beyond_feedback_timeout(
    service, pages, clock
):
    await started(service, pages)
    capture = service.capture
    # Isolate the clock from Ogg framing: the producer is 50 s ahead and must
    # wait until the renderer has played enough to restore its 2 s budget.
    capture.produced_ms = 50_000
    reader = await capture.open(0, None)
    service.on_state(control_point(service, clock, 0))
    service.pacer = asyncio.create_task(service._pace())
    await wait_until(lambda: progress(service) == [0])
    # No callback or changed notification: only the pacer's timer wakes it.
    clock.advance(40)
    await wait_until(lambda: progress(service)[-1] == 40_000)
    assert service.pending is not None
    assert not service.awaiting_play

    clock.advance(9)
    await wait_until(lambda: service.pending is None)
    assert capture.played_ms == 49_000
    assert service.error is None and service.output_error is None

    # Progress continues even when no packet currently awaits credit.
    clock.advance(1)
    await wait_until(lambda: progress(service)[-1] == 50_000)
    await reader.aclose()


async def test_delayed_control_points_correct_the_clock(service, pages, clock, caplog):
    await started(service, pages)
    service.capture.produced_ms = 60_000
    initial = control_point(service, clock, 1000)
    clock.advance(2)  # Listener delivery was delayed; its timestamp is intact.
    service.on_state(initial)
    assert service.capture.played_ms == 3000
    clock.advance(10)
    correction = control_point(service, clock, 12_750)
    clock.advance(0.25)
    with caplog.at_level("DEBUG", logger="kalinka_plugin_spotify.service"):
        service.on_state(correction)
    assert "clock correction: -250 ms" in caplog.text
    assert service.capture.played_ms == 13_000
    clock.advance(1.75)
    assert service._position_at(clock.monotonic_ns()) == 14_750
    # The callback object remains the real control point, never a synthetic tick.
    assert service.state is correction and correction.position == 12_750


@pytest.mark.parametrize("state", [PlayerStateEnum.PAUSED, PlayerStateEnum.BUFFERING])
async def test_pause_or_buffering_freezes_credit_and_resume_reanchors(
    service, pages, clock, state
):
    await started(service, pages)
    capture = service.capture
    capture.produced_ms = 10_000
    reader = await capture.open(0, None)
    service.on_state(control_point(service, clock, 2000, state))
    service.pacer = asyncio.create_task(service._pace())
    await wait_until(lambda: progress(service) == [2000])
    clock.advance(60)
    service.changed.set()
    await wait_until(lambda: len(progress(service)) == 2)
    assert progress(service)[-1] == 2000
    assert service.pending is not None
    assert capture.played_ms == 2000 and not service.awaiting_play

    service.on_state(control_point(service, clock, 2000))
    clock.advance(7)
    service.changed.set()
    await wait_until(lambda: service.pending is None)
    assert capture.played_ms == 9000
    await reader.aclose()


async def test_handoff_uses_elapsed_position_with_seek_offset(service, pages, clock):
    await service.event({"event": "seeked", "position_ms": 40_000})
    await started(service, pages)
    service.capture.produced_ms = 60_000
    service.on_state(control_point(service, clock, 41_000))
    clock.advance(35)
    service.on_revoked(RevokeReason.QUEUE_PLAY)
    await service.release_task
    assert service.producer.commands[-1] == (
        "suspend",
        {"id": 1, "position_ms": 76_000},
    )


async def test_seek_replaces_clock_and_ignores_old_control_points(
    service, pages, clock
):
    await started(service, pages)
    service.capture.produced_ms = 60_000
    stale = control_point(service, clock, 1000)
    service.on_state(stale)
    clock.advance(40)
    await service.event({"event": "seeked", "position_ms": 4000})
    await started(service, pages)
    assert service._position_at(clock.monotonic_ns()) == 4000
    service.on_state(stale)
    assert service.state is None
    service.on_state(control_point(service, clock, 4000))
    clock.advance(0.25)
    assert service._position_at(clock.monotonic_ns()) == 4250


async def test_timer_cannot_play_unproduced_audio_or_finish_the_track(
    service, pages, clock
):
    await started(service, pages)
    capture = service.capture
    capture.produced_ms = 5000
    await capture.finish()
    reader = await capture.open(0, None)
    await reader.read(capture.available)
    await reader.aclose()
    service.on_state(control_point(service, clock, 0))
    service.pacer = asyncio.create_task(service._pace())
    clock.advance(60)
    service.changed.set()
    await wait_until(lambda: capture.played_ms == 5000)
    assert service.pending is not None and not service.finished
    service.on_finished()
    await wait_until(lambda: service.pending is None)
    assert not service.awaiting_play


async def test_missing_position_does_not_start_a_clock(service, pages, clock):
    await started(service, pages)
    service.on_state(control_point(service, clock, None))
    clock.advance(60)
    assert service._position_at(clock.monotonic_ns()) == 0
    await asyncio.sleep(0)
    assert not progress(service)
