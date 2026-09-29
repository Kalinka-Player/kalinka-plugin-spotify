"""Owned librespot process, binary stdout and a private structured control socket."""

import asyncio
import json
import os
import socket
from pathlib import Path


class ProducerError(RuntimeError):
    pass


class Librespot:
    def __init__(self, executable, device_name, state_directory: Path):
        self.executable = executable
        self.device_name = device_name
        self.state_directory = state_directory
        self.process = None
        self.reader = self.writer = None
        self.stderr_task = None
        self.send_lock = asyncio.Lock()

    async def start(self):
        try:
            probe = await asyncio.create_subprocess_exec(
                self.executable,
                "--kalinka-capabilities",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
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
                "160",
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
        self.stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self):
        # Human logs are neither a control protocol nor safe credential output.
        process = self.process
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
        raise ProducerError(
            "librespot exited; check account authentication and network, then disable/re-enable Spotify"
        )

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
        if process is not None and process.returncode is None:
            # Only this plugin's child; no process-name matching or sound-card control.
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
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
