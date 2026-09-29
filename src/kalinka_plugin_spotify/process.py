"""Owned librespot process, binary stdout and a private structured control socket."""

import asyncio
import json
import os
import signal
import socket
from pathlib import Path

GRACEFUL_STOP_TIMEOUT = 3
TERMINATE_TIMEOUT = 1


class ProducerError(RuntimeError):
    pass


def forget_credentials(state_directory: Path):
    # Without saved credentials librespot waits for pairing in the app.
    (state_directory / "credentials.json").unlink(missing_ok=True)


class Librespot:
    def __init__(self, executable, device_name, state_directory: Path):
        self.executable = executable
        self.device_name = device_name
        self.state_directory = state_directory
        self.process = None
        self.reader = self.writer = None
        self.stderr_task = None
        self.send_lock = asyncio.Lock()
        # Older bridges report every sign-in failure as authentication_failed.
        self.signin_errors = False

    async def start(self):
        try:
            probe = await asyncio.create_subprocess_exec(
                self.executable,
                "--kalinka-capabilities",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ProducerError(
                "librespot is missing; install the pinned Kalinka pipe build"
            ) from None
        try:
            output, _ = await asyncio.wait_for(probe.communicate(), 5)
        except BaseException:
            if probe.returncode is None:
                probe.kill()
            await probe.wait()
            raise
        try:
            capabilities = json.loads(output)
            valid = (
                probe.returncode == 0
                and capabilities["protocol"] == 1
                and capabilities["librespot"] == "0.8.0"
                and capabilities["passthrough"] is True
                and capabilities["pipe"] is True
                and capabilities.get("volume") is True
                and capabilities.get("reconnect") is True
                and capabilities.get("suspend") is True
            )
            self.signin_errors = capabilities.get("signin_errors") is True
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ProducerError(
                "Unsupported librespot: requires 0.8.0 with passthrough-decoder and Kalinka bridge v1 with volume/reconnect/suspend support; rebuild the bundled patch"
            )
        self.state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_directory.chmod(0o700)
        for child in self.state_directory.iterdir():
            if child.is_file():
                child.chmod(0o600)
        parent, child = socket.socketpair()
        parent.setblocking(False)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("LIBRESPOT_", "KALINKA_CONTROL_"))
        }
        env["KALINKA_CONTROL_FD"] = str(child.fileno())
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.executable,
                "--backend",
                "pipe",
                "--passthrough",
                "--name",
                self.device_name,
                "--bitrate",
                "320",
                "--cache",
                str(self.state_directory),
                "--disable-audio-cache",
                "--volume-ctrl",
                "fixed",
                "--quiet",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(child.fileno(),),
                # The server owns shutdown. A terminal Ctrl+C must not kill
                # this child before the plugin can retire its HTTP streams.
                start_new_session=True,
                env=env,
                umask=0o077,
                limit=128 * 1024,
            )
            self.reader, self.writer = await asyncio.open_connection(
                sock=parent, limit=64 * 1024
            )
        except BaseException:
            parent.close()
            await self.stop()
            raise
        finally:
            child.close()
        self.stderr_task = asyncio.create_task(self._drain_stderr(self.process))

    async def _drain_stderr(self, process):
        # Human logs are neither a control protocol nor safe credential output.
        while await process.stderr.read(16 * 1024):
            pass

    async def events(self):
        while line := await self.reader.readline():
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(
                    event.get("event"), str
                ):
                    raise ValueError()
            except ValueError:
                raise ProducerError("Invalid librespot control event") from None
            yield event
        raise ProducerError("librespot control connection closed.")

    def forget_credentials(self):
        forget_credentials(self.state_directory)

    async def wait(self):
        process = self.process
        while process.returncode is None:
            try:
                return await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                # asyncio's pipe transports can postpone wait() completion
                # after child exit until a full stdout buffer is drained.
                # The returncode is still updated by its process watcher.
                pass
        return process.returncode

    async def audio(self, length):
        if not isinstance(length, int) or not 0 < length <= 1024 * 1024:
            raise ProducerError("Invalid compressed packet length")
        try:
            return await asyncio.wait_for(self.process.stdout.readexactly(length), 5)
        except TimeoutError:
            raise ProducerError(
                "librespot did not deliver the announced audio packet; update the Kalinka pipe build and reconnect."
            ) from None
        except asyncio.IncompleteReadError:
            raise ProducerError(
                "librespot closed an incomplete audio packet."
            ) from None

    async def send(self, op, **fields):
        async with self.send_lock:
            if self.writer is None or self.writer.is_closing():
                raise ProducerError("librespot control connection is closed")
            self.writer.write(json.dumps(dict(op=op, **fields)).encode() + b"\n")
            await asyncio.wait_for(self.writer.drain(), 2)

    async def stop(self):
        process, self.process = self.process, None
        if process is not None:
            # Playback consumers have stopped. Keep draining both lanes so a
            # final audio packet or control event cannot block native teardown.
            async def drain(reader):
                while await reader.read(64 * 1024):
                    pass

            drains = [
                asyncio.create_task(drain(reader))
                for reader in (process.stdout, self.reader)
                if reader is not None
            ]
            try:
                # librespot's graceful shutdown handler listens for SIGINT.
                # Escalate only if this owned child does not exit in time.
                for sig, timeout in (
                    (signal.SIGINT, GRACEFUL_STOP_TIMEOUT),
                    (signal.SIGTERM, TERMINATE_TIMEOUT),
                ):
                    if process.returncode is not None:
                        break
                    try:
                        process.send_signal(sig)
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout)
                        break
                    except TimeoutError:
                        pass
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                await asyncio.gather(*drains, return_exceptions=True)
            finally:
                for task in drains:
                    task.cancel()
                await asyncio.gather(*drains, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.writer = None
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
            self.stderr_task = None
