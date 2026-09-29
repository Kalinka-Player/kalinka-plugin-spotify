import asyncio

import pytest
from kalinka_plugin_sdk.live_content import LiveContentError

from kalinka_plugin_spotify.cache import Capture


async def test_delayed_production_and_pause_are_not_eof(tmp_path):
    c = Capture(tmp_path)
    r = await c.open(0, None)
    pending = asyncio.create_task(r.read(10))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await c.append(b"abc")
    assert await pending == b"abc"
    pending = asyncio.create_task(r.read(10))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await c.finish()
    assert await pending == b""
    assert c.size == 3 and c.intervals == [(0, 3)] and c.delivered == 3
    await c.retire()
    assert not c.file.closed
    await r.aclose()
    assert c.file.closed


async def test_cancel_wakes_waiters_and_preserves_pinned_file(tmp_path):
    c = Capture(tmp_path)
    r = await c.open(0, None)
    pending = asyncio.create_task(r.read(100))
    await asyncio.sleep(0)
    await c.retire()
    with pytest.raises(OSError, match="cancelled"):
        await pending
    assert not c.file.closed
    await r.aclose()
    assert c.file.closed
    with pytest.raises(LiveContentError) as error:
        await c.open(0, None)
    assert error.value.status == 410


async def test_no_silent_replay_on_reconnect(tmp_path):
    c = Capture(tmp_path)
    await c.append(b"abc")
    r = await c.open(0, None)
    await r.aclose()
    with pytest.raises(LiveContentError) as error:
        await c.open(0, None)
    assert error.value.status == 409
    with pytest.raises(LiveContentError):
        await c.open(1, None)
    await c.finish()
    r = await c.open(1, 3)
    assert await r.read(100) == b"bc"
    assert await r.read(100) == b""
    await r.aclose()
    await c.retire()


async def test_retired_capture_refuses_new_writes_and_readers(tmp_path):
    c = Capture(tmp_path)
    reader = await c.open(0, None)
    await c.retire(wait_for_disconnect=True)
    with pytest.raises(RuntimeError, match="no longer writable"):
        await c.append(b"stale audio")
    with pytest.raises(LiveContentError) as error:
        await c.open(0, None)
    assert error.value.status == 410
    await reader.aclose()
    assert c.file.closed


async def test_cache_bound(tmp_path):
    c = Capture(tmp_path, max_bytes=4)
    await c.append(b"abcd")
    with pytest.raises(RuntimeError, match="limit"):
        await c.append(b"e")
    assert c.available == 4
    await c.retire()
    assert c.file.closed


async def test_bounded_wait_and_reader_cancellation(tmp_path):
    c = Capture(tmp_path, timeout=0.01)
    r = await c.open(0, None)
    with pytest.raises(OSError, match="timed out"):
        await r.read(1)
    pending = asyncio.create_task(r.read(1))
    await r.aclose()
    with pytest.raises(OSError, match="cancelled"):
        await pending
    await c.retire()
