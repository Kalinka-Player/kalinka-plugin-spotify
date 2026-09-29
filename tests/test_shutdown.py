import asyncio
import json
import os
import signal
import sys
import textwrap
from pathlib import Path

import pytest

from kalinka_plugin_spotify.process import Librespot


@pytest.fixture
def graceful_receiver(tmp_path):
    binary = tmp_path / "librespot"
    binary.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""
        import json, os, signal, socket, sys
        from pathlib import Path
        if '--kalinka-capabilities' in sys.argv:
            print(json.dumps(dict(protocol=1, librespot='0.8.0', passthrough=True, pipe=True, volume=True, reconnect=True, suspend=True)))
            raise SystemExit
        directory = Path(sys.argv[sys.argv.index('--cache') + 1])
        mode = sys.argv[sys.argv.index('--name') + 1]
        control = socket.socket(fileno=int(os.environ['KALINKA_CONTROL_FD']))
        def shutdown(signum, frame):
            (directory / 'signal').write_text(str(signum))
            if mode == 'drain':
                for _ in range(32):
                    os.write(1, b'x' * 65536)
                    os.write(2, b'x' * 65536)
                    control.sendall(b'x' * 65536)
            raise SystemExit(0)
        signal.signal(signal.SIGINT, signal.SIG_IGN if mode in ('terminate', 'kill') else shutdown)
        if mode in ('terminate', 'kill'):
            signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == 'kill' else shutdown)
        (directory / 'ready').touch()
        control.sendall(b'{"event":"ready","protocol":1}\\n')
        # Let Python dispatch a pending signal even when it lands just before
        # recv() blocks. The real receiver uses Tokio's signal wakeup channel.
        control.settimeout(0.05)
        while True:
            try:
                if not control.recv(4096):
                    break
            except TimeoutError:
                pass
    """)
    )
    binary.chmod(0o700)
    return binary


@pytest.mark.parametrize("mode", ["idle", "streaming"])
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_terminal_signal_leaves_child_for_orderly_plugin_shutdown(
    tmp_path, graceful_receiver, mode, sig
):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    host = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("shutdown_host.py")),
        str(graceful_receiver),
        str(tmp_path),
        mode,
        env=env,
        start_new_session=True,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child_pid = None
    try:
        ready = await asyncio.wait_for(host.stdout.readline(), 5)
        assert ready, (await host.stderr.read()).decode()
        child_pid = json.loads(ready)["child"]
        os.killpg(host.pid, sig)
        output, errors = await asyncio.wait_for(host.communicate(), 5)
        assert host.returncode == 0, errors.decode()
        result = json.loads(output)
        assert result["alive_before_cleanup"], errors.decode()
        assert result["failure"] is None and result["http_error"] is None
        assert result["sessions"] == 1
        assert result["returncode"] == 0, errors.decode()
        assert result["readers_closed"] and result["cache_removed"]
        assert "ERROR" not in errors.decode()
        assert (tmp_path / "state/signal").read_text() == str(signal.SIGINT)
    finally:
        if host.returncode is None:
            host.kill()
        await host.wait()
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("mode", ["test", "drain", "terminate", "kill"])
async def test_stop_uses_graceful_signal_drains_pipes_and_bounds_escalation(
    tmp_path, graceful_receiver, monkeypatch, mode
):
    monkeypatch.setattr(
        "kalinka_plugin_spotify.process.GRACEFUL_STOP_TIMEOUT", 0.1, raising=False
    )
    monkeypatch.setattr(
        "kalinka_plugin_spotify.process.TERMINATE_TIMEOUT", 0.1, raising=False
    )
    producer = Librespot(str(graceful_receiver), mode, tmp_path / "state")
    await producer.start()
    child = producer.process
    events = producer.events()
    assert (await asyncio.wait_for(anext(events), 1))["event"] == "ready"
    await asyncio.wait_for(producer.stop(), 4)
    expected = -signal.SIGKILL if mode == "kill" else 0
    assert child.returncode == expected
    if mode != "kill":
        expected_signal = signal.SIGTERM if mode == "terminate" else signal.SIGINT
        assert (tmp_path / "state/signal").read_text() == str(expected_signal)
    assert child.stdout.at_eof()
    await events.aclose()
