import asyncio

import pytest
from kalinka_server.live_content import serve
from starlette.requests import Request

from kalinka_plugin_spotify.cache import Capture


def request(method="GET", range=None):
    headers = [(b"range", range.encode())] if range else []
    return Request({"type": "http", "method": method, "headers": headers})


@pytest.mark.parametrize(
    "initial_range", [None, "bytes=0-", "bytes=0-383999", "bytes=0-0"]
)
async def test_headers_are_honest(tmp_path, initial_range):
    c = Capture(tmp_path)
    await c.append(b"abc")
    head = await serve(c, "audio/ogg", request("HEAD"))
    assert "content-length" not in head.headers
    assert head.headers["accept-ranges"] == "none"
    assert head.headers["cache-control"] == "no-store"
    response = await serve(c, "audio/ogg", request(range=initial_range))
    assert response.status_code == 200
    assert "content-range" not in response.headers
    assert "content-length" not in response.headers
    assert response.headers["content-type"] == "audio/ogg"
    iterator = response.body_iterator
    assert await anext(iterator) == b"abc"
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0.01)
    assert not pending.done()
    await c.append(b"def")
    assert await pending == b"def"
    await c.finish()
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    await response.reader.aclose()
    await c.retire()


@pytest.mark.parametrize(
    "range",
    ["bytes=1-", "bytes=100-200", "bytes=-5", "bytes=0-20,40-60", "bytes=0-invalid"],
)
async def test_unavailable_ranges_are_not_eof(tmp_path, range):
    c = Capture(tmp_path)
    response = await serve(c, "audio/ogg", request(range=range))
    assert response.status_code == 409
    assert "content-range" not in response.headers
    await c.retire()


async def test_completed_ranges(tmp_path):
    c = Capture(tmp_path)
    await c.append(b"abcdef")
    await c.finish()
    response = await serve(c, "audio/ogg", request(range="bytes=2-4"))
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-4/6"
    assert response.headers["content-length"] == "3"
    assert b"".join([data async for data in response.body_iterator]) == b"cde"
    await response.reader.aclose()
    response = await serve(c, "audio/ogg", request(range="bytes=9-"))
    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */6"
    await c.retire()


async def test_disconnect_closes_reader_even_while_waiting(tmp_path):
    c = Capture(tmp_path)
    response = await serve(c, "audio/ogg", request())
    received = asyncio.Event()

    async def receive():
        await received.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            received.set()

    await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
    assert not c.readers
    await c.retire()
