from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kalinka_plugin_spotify import KalinkaPluginSpotify, SpotifyConfig
from kalinka_plugin_spotify.process import Librespot, ProducerError


async def test_disabled_by_default_has_no_process(tmp_path):
    plugin = KalinkaPluginSpotify()
    config = SpotifyConfig()
    assert not config.enabled
    await plugin.setup(SimpleNamespace(config=config))
    assert plugin.service is None
    assert (await plugin.get_state()).state.value == "disabled"
    assert await plugin.get_interface().get_content_info("unknown") is None
    await plugin.shutdown()


async def test_missing_binary(tmp_path):
    process = Librespot(str(tmp_path / "missing"), "test", tmp_path / "state")
    with pytest.raises(ProducerError, match="missing"):
        await process.start()


@pytest.mark.parametrize("failure", ["missing", "unexpected"])
async def test_startup_failure_reaches_health_and_logs(
    tmp_path, monkeypatch, caplog, failure
):
    import asyncio

    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    if failure == "unexpected":
        monkeypatch.setattr(
            Librespot,
            "start",
            AsyncMock(side_effect=OSError("private-upstream-detail")),
        )
    plugin = KalinkaPluginSpotify()
    await plugin.setup(
        SimpleNamespace(
            config=SpotifyConfig(enabled=True, executable=str(tmp_path / "missing")),
            direct_playback=object(),
        )
    )
    try:
        await asyncio.wait_for(plugin.service.task, 2)
        state = await plugin.get_state()
        assert state.state.value == "error"
        assert state.message in caplog.text
        assert "private-upstream-detail" not in caplog.text
        if failure == "missing":
            assert "librespot is missing" in state.message
        else:
            assert "OSError" in caplog.text
            assert "in run" in caplog.text
        assert not Path(plugin.service.directory.name).exists()
    finally:
        await plugin.shutdown()


@pytest.mark.parametrize("failure", ["timeout", "truncated"])
async def test_incomplete_audio_packets_report_the_pipe_failure(tmp_path, failure):
    import asyncio

    error = (
        TimeoutError()
        if failure == "timeout"
        else asyncio.IncompleteReadError(b"private-packet", 100)
    )
    producer = Librespot("unused", "test", tmp_path)
    producer.process = SimpleNamespace(
        stdout=SimpleNamespace(readexactly=AsyncMock(side_effect=error))
    )
    message = (
        "announced audio packet" if failure == "timeout" else "incomplete audio packet"
    )
    with pytest.raises(ProducerError, match=message) as caught:
        await producer.audio(100)
    assert "private-packet" not in str(caught.value)


async def test_stock_binary_rejected(tmp_path):
    process = Librespot("/bin/true", "test", tmp_path / "state")
    with pytest.raises(ProducerError, match="Unsupported"):
        await process.start()


@pytest.mark.parametrize("missing_capability", ["volume", "reconnect", "suspend"])
async def test_bridge_without_required_capability_requires_rebuild(
    tmp_path, monkeypatch, missing_capability
):
    import json

    capabilities = dict(
        protocol=1,
        librespot="0.8.0",
        passthrough=True,
        pipe=True,
        volume=True,
        reconnect=True,
        suspend=True,
    )
    capabilities.pop(missing_capability)
    probe = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(
            return_value=(
                json.dumps(capabilities).encode(),
                b"",
            )
        ),
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=probe))
    process = Librespot("old-bridge", "test", tmp_path)
    with pytest.raises(
        ProducerError, match="volume/reconnect/suspend support; rebuild"
    ):
        await process.start()
    assert process.process is None


async def test_child_has_separate_audio_events_stderr_and_is_reaped(tmp_path):
    import sys
    import textwrap

    binary = tmp_path / "fake-librespot"
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""
        import json, os, socket, sys, time
        if '--kalinka-capabilities' in sys.argv:
            print(json.dumps(dict(protocol=1, librespot='0.8.0', passthrough=True, pipe=True, volume=True, reconnect=True, suspend=True)))
            raise SystemExit
        control = socket.socket(fileno=int(os.environ['KALINKA_CONTROL_FD']))
        directory = sys.argv[sys.argv.index('--cache') + 1]
        with open(os.path.join(directory, 'credentials.json'), 'w') as f:
            f.write('private test credential')
        control.sendall(b'{"event":"ready","protocol":1}\\n')
        os.write(2, b'credential=must-not-appear-in-events\\n' * 10000)
        control.sendall(b'{"event":"packet","id":1,"length":4}\\n')
        os.write(1, b'OggS')
        command = control.makefile().readline()
        control.sendall(json.dumps(dict(event='received', op=json.loads(command)['op'])).encode()+b'\\n')
        time.sleep(100)
    """)
    )
    binary.chmod(0o700)
    producer = Librespot(str(binary), "test", tmp_path / "credentials")
    await producer.start()
    child = producer.process
    try:
        events = producer.events()
        assert (await anext(events))["event"] == "ready"
        assert (await anext(events))["event"] == "packet"
        assert await producer.audio(4) == b"OggS"
        await producer.send("pause")
        assert (await anext(events))["op"] == "pause"
        assert ((tmp_path / "credentials").stat().st_mode & 0o777) == 0o700
        assert (
            (tmp_path / "credentials/credentials.json").stat().st_mode & 0o777
        ) == 0o600
    finally:
        await producer.stop()
    assert child.returncode is not None


async def test_stop_before_background_start_cleans_up(tmp_path):
    from kalinka_plugin_spotify.service import Service

    producer = SimpleNamespace(start=AsyncMock(), send=AsyncMock(), stop=AsyncMock())
    service = Service(None, producer, tmp_path)
    directory = service.directory.name
    service.start()
    await service.stop()
    from pathlib import Path

    assert not Path(directory).exists()
    producer.stop.assert_awaited_once()
