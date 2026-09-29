import pytest

from kalinka_plugin_spotify.ogg import InvalidOgg, OggParser, crc


@pytest.mark.parametrize("fragment", [1, 7, 26, 255, 4096])
def test_partial_pages(ogg, fragment):
    parser = OggParser()
    pages = []
    for offset in range(0, len(ogg), fragment):
        pages.extend(parser.feed(ogg[offset : offset + fragment]))
    parser.finish()
    assert b"".join(page.data for page in pages) == ogg
    assert pages[0].bos and pages[-1].eos
    assert pages[-1].time_ms == 8000
    assert all(p.headers_ready for p in pages[1:])


def test_corrupt_page(ogg):
    data = bytearray(ogg)
    data[40] ^= 1
    with pytest.raises(InvalidOgg, match="checksum"):
        OggParser().feed(data)


def test_reject_pcm():
    with pytest.raises(InvalidOgg, match="PCM"):
        OggParser().feed(b"\0" * 100)


def test_reject_other_codec(pages):
    data = bytearray(pages[0].data)
    data[28:35] = b"OpusHea"
    data[22:26] = b"\0" * 4
    data[22:26] = crc(data).to_bytes(4, "little")
    with pytest.raises(InvalidOgg, match="Vorbis"):
        OggParser().feed(data)


def test_truncated_is_not_complete(ogg):
    parser = OggParser()
    parser.feed(ogg[:-1])
    with pytest.raises(InvalidOgg, match="Truncated"):
        parser.finish()


def test_chain_and_reordered_pages(ogg, pages):
    parser = OggParser()
    assert len(parser.feed(ogg + ogg)) == len(pages) * 2
    parser.finish()
    with pytest.raises(InvalidOgg, match="reordered"):
        OggParser().feed(pages[0].data + pages[2].data)
