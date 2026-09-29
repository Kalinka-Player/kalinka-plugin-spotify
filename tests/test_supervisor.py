import asyncio
import signal
import sys
import textwrap
from pathlib import Path

import pytest
from kalinka_plugin_sdk.direct_playback import RevokeReason
from test_service import Direct, Producer, feed, started, wait_until

from kalinka_plugin_spotify.process import Librespot, ProducerError
from kalinka_plugin_spotify.service import Service
from kalinka_plugin_spotify.supervisor import Supervisor


@pytest.fixture
async def receiver(tmp_path):
    sessions = []
    direct = Direct()

    def create():
        service = Service(direct, Producer(), tmp_path)
        sessions.append(service)
        return service

    supervisor = Supervisor(create, retry_delay=0.01, max_retry_delay=0.04)
    supervisor.start()
    await wait_until(lambda: sessions and sessions[0].exit_watcher is not None)
    yield supervisor, sessions
    await supervisor.stop()


async def test_dead_child_restarts_after_releasing_output_and_failing_old_reader(
    receiver, pages
):
    supervisor, sessions = receiver
    old = sessions[0]
    await old.event({"event": "track", "title": "Old track", "duration_ms": 8000})
    await started(old, pages)
    capture, hold = old.capture, old.hold
    reader = await capture.open(0, None)
    await reader.read(capture.available)
    blocked = asyncio.create_task(reader.read(100))
    try:
        old.producer.exited.set_result(-signal.SIGKILL)
        await wait_until(lambda: len(sessions) == 2 and sessions[1].producer.starts)
        new = sessions[1]
        assert old.producer.stopped and hold.released
        assert old.cleanup_task.done() and old.pacer.done()
        assert not Path(old.directory.name).exists()
        with pytest.raises(OSError, match="librespot exited"):
            await blocked
        await reader.aclose()
        assert capture.file.closed
        assert supervisor.error is None and not supervisor.closed
        assert supervisor.capture is None
        assert new.metadata == {} and new.pending is None and new.hold is None
        # A new session accepts fresh audio and acquires the current renderer.
        await new.event({"event": "track", "title": "New track", "duration_ms": 8000})
        await started(new, pages)
        assert supervisor.capture.id != capture.id
        assert new.hold.sources[-1][1].title == "New track"
        assert new.direct.acquisitions == 2
    finally:
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
        await reader.aclose()


async def test_child_exit_interrupts_blocked_event_handler(receiver):
    _, sessions = receiver
    entered = asyncio.Event()

    async def blocked_event(event):
        entered.set()
        await asyncio.Future()

    sessions[0].event = blocked_event
    sessions[0].producer.incoming.put_nowait({"event": "connected"})
    await asyncio.wait_for(entered.wait(), 1)
    sessions[0].producer.exited.set_result(7)
    await wait_until(lambda: len(sessions) == 2 and sessions[1].producer.starts)
    assert sessions[0].task.done() and sessions[0].producer.stopped


async def test_renderer_stall_suspends_without_restarting_discovery(receiver, pages):
    supervisor, sessions = receiver
    service = sessions[0]
    await service.event({"event": "track", "title": "Track", "duration_ms": 8000})
    await started(service, pages)
    service.last_feedback -= service.reader_timeout + 1
    service.changed.set()
    await wait_until(lambda: service.awaiting_play)
    await service.release_task
    assert "Renderer stopped reporting" in supervisor.output_error
    assert "retrying" in supervisor.output_error
    assert supervisor.error is None and len(sessions) == 1
    assert not service.producer.stopped and not service.task.done()
    # A fresh load gives the renderer a new HTTP resource to open.
    await service.event({"event": "suspended", "id": service.handoff_id})
    await wait_until(lambda: service.producer.commands[-1] == ("resume", {}))


async def test_pacing_failure_also_recovers_discovery(receiver, pages):
    _, sessions = receiver
    old = sessions[0]

    async def broken_send(op, **fields):
        raise ProducerError("librespot control connection is closed")

    old.producer.send = broken_send
    await old.event({"event": "track", "title": "Track", "duration_ms": 8000})
    await feed(old, pages[0], 1)
    await wait_until(lambda: len(sessions) == 2 and sessions[1].producer.starts)
    assert "control connection" in old.error
    assert old.producer.stopped


@pytest.mark.parametrize("stable", [False, True])
async def test_repeated_failures_back_off_and_healthy_run_resets_delay(
    receiver, caplog, stable
):
    supervisor, sessions = receiver
    supervisor.stable_after = 0 if stable else 60
    for index in range(4):
        await wait_until(
            lambda: len(sessions) > index and sessions[index].producer.starts
        )
        sessions[index].producer.exited.set_result(1)
        await wait_until(lambda: len(sessions) > index + 1)
    delays = [
        record.message
        for record in caplog.records
        if record.name.endswith("supervisor")
    ]
    expected = [0.01] * 4 if stable else [0.01, 0.02, 0.04, 0.04]
    assert delays == [f"Spotify Connect restarting in {d:g} seconds" for d in expected]


async def test_disable_during_backoff_does_not_spawn_another_process(receiver):
    supervisor, sessions = receiver
    sessions[0].producer.exited.set_result(0)
    await wait_until(lambda: supervisor.retrying)
    assert supervisor.error is None
    assert "restarting" in supervisor.status
    await supervisor.stop()
    await asyncio.sleep(0.06)
    assert supervisor.closed and supervisor.task.done()
    assert len(sessions) == 1 and sessions[0].producer.stopped


@pytest.mark.parametrize("action", ["stop", "shutdown", "authentication", "protocol"])
async def test_intentional_stop_and_permanent_errors_do_not_restart(receiver, action):
    supervisor, sessions = receiver
    if action == "stop":
        await supervisor.stop()
    elif action == "shutdown":
        sessions[0].on_revoked(RevokeReason.SHUTDOWN)
    else:
        event = (
            {"event": "error", "code": "authentication_failed"}
            if action == "authentication"
            else {"event": "ready", "protocol": 999}
        )
        sessions[0].producer.incoming.put_nowait(event)
    await wait_until(lambda: supervisor.closed)
    await asyncio.sleep(0.03)
    assert len(sessions) == 1 and sessions[0].producer.stopped
    if action in ("authentication", "protocol"):
        assert supervisor.error


@pytest.mark.parametrize("state", ["idle", "paused", "handoff", "buffering"])
async def test_healthy_receiver_is_not_restarted_without_audio(receiver, pages, state):
    supervisor, sessions = receiver
    service = sessions[0]
    if state != "idle":
        await service.event({"event": "track", "title": "Track", "duration_ms": 8000})
        await started(service, pages)
    if state == "paused":
        await service.event({"event": "paused", "position_ms": 0})
    elif state == "handoff":
        service.on_revoked(RevokeReason.QUEUE_PLAY)
        await service.release_task
    await asyncio.sleep(0.06)
    assert len(sessions) == 1
    assert supervisor.error is None and not supervisor.closed
    assert service.producer.starts == 1 and not service.producer.stopped


@pytest.fixture
def native_receiver(tmp_path):
    binary = tmp_path / "fake-librespot"
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""
        import json, os, socket, sys
        if '--kalinka-capabilities' in sys.argv:
            print(json.dumps(dict(protocol=1, librespot='0.8.0', passthrough=True, pipe=True, volume=True, reconnect=True, suspend=True)))
            raise SystemExit
        control = socket.socket(fileno=int(os.environ['KALINKA_CONTROL_FD']))
        control.sendall(b'{"event":"ready","protocol":1}\\n')
        for line in control.makefile():
            command = json.loads(line)['op']
            if command == 'exit':
                os._exit(17)
            if command == 'flood':
                while True:
                    os.write(1, b'x' * 65536)
            if command == 'disconnect':
                break
    """)
    )
    binary.chmod(0o700)
    return binary


@pytest.mark.parametrize("exit_mode", ["signal", "exit"])
async def test_actual_subprocess_exit_is_reaped_and_replaced(
    tmp_path, native_receiver, exit_mode
):
    sessions = []

    def create():
        service = Service(
            Direct(),
            Librespot(str(native_receiver), "test", tmp_path / "credentials"),
            tmp_path / "cache",
        )
        sessions.append(service)
        return service

    supervisor = Supervisor(create, retry_delay=0.01)
    supervisor.start()
    try:
        await wait_until(lambda: sessions and sessions[0].exit_watcher is not None)
        child = sessions[0].producer.process
        if exit_mode == "signal":
            child.send_signal(signal.SIGKILL)
        else:
            await sessions[0].producer.send("exit")
        await wait_until(
            lambda: len(sessions) == 2 and sessions[1].exit_watcher is not None
        )
        replacement = sessions[1].producer.process
        assert replacement.pid != child.pid and replacement.returncode is None
        assert child.returncode == (-signal.SIGKILL if exit_mode == "signal" else 17)
        assert sessions[0].cleanup_task.done()
        assert not Path(sessions[0].directory.name).exists()
    finally:
        await supervisor.stop()
    assert all(s.producer.process is None for s in sessions)
    assert supervisor.task.done()


async def test_immediate_disable_never_starts_child(tmp_path, native_receiver):
    created = []
    supervisor = Supervisor(lambda: created.append(True))
    supervisor.start()
    await supervisor.stop()
    assert not created and supervisor.task.done()


async def test_child_exit_is_detected_even_with_undrained_stdout(
    tmp_path, native_receiver
):
    producer = Librespot(str(native_receiver), "test", tmp_path / "credentials")
    await producer.start()
    child = producer.process
    waiter = asyncio.create_task(producer.wait())
    try:
        await producer.send("flood")
        # Exercise asyncio's real pipe backpressure, which defers existing
        # Process.wait() futures even after the OS reports the exit status.
        await wait_until(lambda: child.stdout._paused)
        child.kill()
        assert await asyncio.wait_for(waiter, 2) == -signal.SIGKILL
    finally:
        await asyncio.wait_for(producer.stop(), 4)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    assert child.stdout.at_eof()
