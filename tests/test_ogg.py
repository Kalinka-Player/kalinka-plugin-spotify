import struct

import pytest

from kalinka_plugin_spotify.ogg import InvalidOgg, OggParser, crc


def ogg_page(serial, sequence, granule, packets, flags=0):
    lacing = b"".join(
        b"\xff" * (len(packet) // 255) + bytes([len(packet) % 255])
        for packet in packets
    )
    header = b"OggS\0" + struct.pack("<BQIII", flags, granule, serial, sequence, 0)
    page = bytearray(header + bytes([len(lacing)]) + lacing + b"".join(packets))
    page[22:26] = crc(page).to_bytes(4, "little")
    return bytes(page)


def silent_stream(pages, granules):
    # Digital silence makes each Vorbis packet a couple of bytes, so pages
    # reach Ogg's 255-segment limit before any byte-size limit.
    silence = [b"\0\0"] * 255
    eos = len(granules) - 1
    audio = b"".join(
        ogg_page(pages[0].serial, 2 + i, granule, silence, 4 if i == eos else 0)
        for i, granule in enumerate(granules)
    )
    return pages[0].data + pages[1].data + audio


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


def test_page_limit_follows_vorbis_long_blocks(pages):
    parser = OggParser()
    parser.feed(pages[0].data)
    # tone.ogg, like Spotify's streams, uses libvorbis' 256/2048 blocks.
    assert parser.max_page_ms == 5922
    silent = OggParser().feed(silent_stream(pages, [255 * 1024, 510 * 1024]))
    assert [p.time_ms for p in silent[2:]] == [5921, 11842]


def test_reject_invalid_vorbis_block_sizes(pages):
    data = bytearray(pages[0].data)
    data[27 + data[26] + 28] = 0xE8  # 16384-sample long blocks
    data[22:26] = b"\0" * 4
    data[22:26] = crc(data).to_bytes(4, "little")
    with pytest.raises(InvalidOgg, match="identification"):
        OggParser().feed(data)
