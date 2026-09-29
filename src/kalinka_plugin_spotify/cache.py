"""Bounded temporary playback storage with asynchronous readers and pinned lifetime."""

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

from kalinka_plugin_sdk.live_content import LiveContentError


async def disk_call(function, *args):
    # Don't close/reuse a descriptor while a cancelled worker still uses it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class CaptureError(RuntimeError):
    """A presentable, credential-free capture failure."""


class Capture:
    def __init__(
        self,
        directory: Path,
        *,
        max_bytes=64 * 1024 * 1024,
        timeout=1800,
        metadata=None,
        offset_ms=0,
    ):
        self.id = uuid.uuid4().hex
        self.file = tempfile.TemporaryFile(dir=directory)
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.metadata = metadata or {}
        self.offset_ms = offset_ms
        self.available = 0
        self.delivered = 0
        self.played_ms = 0
        self.produced_ms = 0
        self.complete = False
        self.failure = None
        self.cancelled = False
        self.retired = False
        self.readers = set()
        self.opened = False
        self.condition = asyncio.Condition()

    @property
    def size(self):
        return self.available if self.complete else None

    @property
    def intervals(self):
        return [(0, self.available)] if self.available else []

    async def append(self, data):
        async with self.condition:
            if self.retired or self.cancelled or self.failure or self.complete:
                raise RuntimeError("Capture is no longer writable")
            if self.available + len(data) > self.max_bytes:
                self.failure = "Spotify playback cache limit exceeded"
                self.condition.notify_all()
                raise CaptureError(self.failure)
            try:
                written = await disk_call(
                    os.pwrite, self.file.fileno(), data, self.available
                )
            except OSError:
                written = None
            if written != len(data):
                self.failure = "Spotify playback cache write failed"
                self.condition.notify_all()
                raise CaptureError(self.failure)
            self.available += written
            self.condition.notify_all()

    async def finish(self):
        async with self.condition:
            self.complete = True
            self.condition.notify_all()

    async def retire(self, failure=None, *, wait_for_disconnect=False):
        async with self.condition:
            self.retired = True
            self.cancelled |= not wait_for_disconnect or failure is not None
            self.failure = failure or self.failure
            self.condition.notify_all()
            self._close_if_unused()

    def _close_if_unused(self):
        if self.retired and not self.readers:
            self.file.close()

    async def wait_closed(self):
        async with self.condition:
            await self.condition.wait_for(lambda: self.file.closed)

    async def open(self, start, end):
        async with self.condition:
            if self.retired or self.failure:
                raise LiveContentError(410, "Playback generation ended")
            if len(self.readers) >= 2:
                raise LiveContentError(429, "Too many playback readers")
            if not self.complete and (start != 0 or self.opened):
                raise LiveContentError(
                    409, "Restart Spotify playback to obtain a fresh stream"
                )
            if start < 0 or start > self.available:
                raise LiveContentError(409, "Requested bytes are not available")
            reader = Reader(self, start, end)
            self.readers.add(reader)
            self.opened = True
            self.condition.notify_all()
            return reader


class Reader:
    def __init__(self, capture, start, end):
        self.capture, self.position, self.end = capture, start, end
        self.closed = False

    async def read(self, count):
        c = self.capture
        async with c.condition:
            try:
                await asyncio.wait_for(
                    c.condition.wait_for(
                        lambda: (
                            self.closed
                            or c.cancelled
                            or c.failure
                            or (
                                not c.retired
                                and (c.complete or self.position < c.available)
                            )
                        )
                    ),
                    c.timeout,
                )
            except TimeoutError:
                raise OSError("Spotify capture timed out waiting for audio") from None
            if self.closed or c.cancelled or c.failure:
                raise OSError(c.failure or "Spotify reader cancelled")
            remaining = c.available - self.position
            if self.end is not None:
                remaining = min(remaining, self.end - self.position)
            if remaining <= 0:
                return b""
            data = await disk_call(
                os.pread, c.file.fileno(), min(count, remaining), self.position
            )
            if not data:
                raise OSError("Spotify cache unexpectedly truncated")
            self.position += len(data)
            c.delivered = max(c.delivered, self.position)
            return data

    async def aclose(self):
        c = self.capture
        async with c.condition:
            self.closed = True
            c.readers.discard(self)
            c.condition.notify_all()
            c._close_if_unused()
