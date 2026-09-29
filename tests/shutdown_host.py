"""Isolated server process used by the terminal-signal regression test."""

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from kalinka_server.live_content import serve
from starlette.requests import Request

from kalinka_plugin_spotify.cache import Capture
from kalinka_plugin_spotify.process import Librespot
from kalinka_plugin_spotify.service import Service
from kalinka_plugin_spotify.supervisor import Supervisor


async def main():
    binary, directory, mode = sys.argv[1:]
    directory = Path(directory)
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    sessions = []

    def create():
        service = Service(
            None, Librespot(binary, "test", directory / "state"), directory / "cache"
        )
        sessions.append(service)
        return service

    supervisor = Supervisor(create, retry_delay=0.05)
    supervisor.start()
    disconnected = asyncio.Event()
    http = None
    try:
        async with asyncio.timeout(5):
            while not (directory / "state/ready").exists():
                await asyncio.sleep(0.01)
        service = supervisor.session
        child = service.producer.process
        if mode == "streaming":
            capture = Capture(Path(service.directory.name))
            service.capture = capture
            service.captures.add(capture)
            await capture.append(b"OggS")
            response = await serve(
                capture,
                "audio/ogg",
                Request({"type": "http", "method": "GET", "headers": []}),
            )
            delivered = asyncio.Event()

            async def receive():
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.body":
                    delivered.set()

            http = asyncio.create_task(
                response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
                )
            )
            await asyncio.wait_for(delivered.wait(), 1)
        print(json.dumps({"child": child.pid}), flush=True)
        await stopping.wait()
        # Uvicorn drains connections before invoking plugin lifespan shutdown.
        # A receiver in this process group used to exit during this interval,
        # fail the pending HTTP read, and potentially trigger an unwanted retry.
        await asyncio.sleep(0.2)
        alive_before_cleanup = child.returncode is None
        failure = service.error
        disconnected.set()
        http_error = None
        if http:
            try:
                await http
            except OSError as exc:
                http_error = str(exc)
        await supervisor.stop()
        print(
            json.dumps(
                {
                    "alive_before_cleanup": alive_before_cleanup,
                    "failure": failure,
                    "http_error": http_error,
                    "sessions": len(sessions),
                    "returncode": child.returncode,
                    "readers_closed": not service.capture
                    or not service.capture.readers,
                    "cache_removed": not Path(service.directory.name).exists(),
                }
            ),
            flush=True,
        )
    finally:
        disconnected.set()
        if http:
            http.cancel()
            await asyncio.gather(http, return_exceptions=True)
        await supervisor.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
