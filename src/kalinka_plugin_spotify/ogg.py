"""Incremental, bounded Ogg framing and Vorbis timing validation. No decoding."""

import struct
from dataclasses import dataclass


class InvalidOgg(ValueError):
    pass


def _crc_table():
    table = []
    for value in range(256):
        value <<= 24
        for _ in range(8):
            value = (
                (value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)
            ) & 0xFFFFFFFF
        table.append(value)
    return table


_CRC = _crc_table()


def crc(data):
    value = 0
    for byte in data:
        value = ((value << 8) & 0xFFFFFFFF) ^ _CRC[(value >> 24) ^ byte]
    return value


@dataclass(frozen=True)
class Page:
    data: bytes
    serial: int
    granule: int
    bos: bool
    eos: bool
    time_ms: int
    headers_ready: bool


class OggParser:
    def __init__(self):
        self.buffer = bytearray()
        self.serial = None
        self.sequence = 0
        self.packet = bytearray()
        self.headers = 0
        self.rate = 0
        self.granule = 0
        self.ended = False

    def feed(self, data: bytes) -> list[Page]:
        if len(data) > 1024 * 1024:
            raise InvalidOgg("Oversized producer packet")
        self.buffer.extend(data)
        pages = []
        while len(self.buffer) >= 27:
            if self.buffer[:5] != b"OggS\x00":
                raise InvalidOgg(
                    "Expected Ogg version 0; PCM and transcoding are unsupported"
                )
            segments = self.buffer[26]
            header_size = 27 + segments
            if len(self.buffer) < header_size:
                break
            lacing = self.buffer[27:header_size]
            length = header_size + sum(lacing)
            if len(self.buffer) < length:
                break
            raw = bytes(self.buffer[:length])
            del self.buffer[:length]
            check = bytearray(raw)
            checksum = struct.unpack_from("<I", raw, 22)[0]
            check[22:26] = b"\0" * 4
            if crc(check) != checksum:
                raise InvalidOgg("Invalid Ogg page checksum")
            flags = raw[5]
            granule, serial, sequence = struct.unpack_from("<QII", raw, 6)
            bos, eos = bool(flags & 2), bool(flags & 4)
            if flags & ~7:
                raise InvalidOgg("Invalid Ogg flags")
            if bos:
                if self.serial is not None and not self.ended:
                    raise InvalidOgg("Discontinuity without a new playback generation")
                self.serial, self.sequence = serial, 0
                self.headers, self.rate, self.granule = 0, 0, 0
                self.packet.clear()
                self.ended = False
            if self.serial != serial or sequence != self.sequence or self.ended:
                raise InvalidOgg("Missing, reordered or multiplexed Ogg page")
            if bool(flags & 1) != bool(self.packet):
                raise InvalidOgg("Invalid continued Ogg packet")
            self.sequence += 1
            offset = header_size
            for size in lacing:
                self.packet.extend(raw[offset : offset + size])
                offset += size
                if len(self.packet) > 1024 * 1024:
                    raise InvalidOgg("Oversized Vorbis header/packet")
                if size < 255:
                    if self.headers < 3:
                        expected = (1, 3, 5)[self.headers]
                        if self.packet[:7] != bytes([expected]) + b"vorbis":
                            raise InvalidOgg("Only Ogg/Vorbis is supported")
                        if self.headers == 0:
                            if len(self.packet) != 30:
                                raise InvalidOgg("Invalid Vorbis identification header")
                            version, channels, rate = struct.unpack_from(
                                "<IBI", self.packet, 7
                            )
                            if version != 0 or channels not in (1, 2) or rate != 44100:
                                raise InvalidOgg(
                                    "librespot passthrough requires 44.1 kHz mono/stereo Vorbis"
                                )
                            self.rate = rate
                        self.headers += 1
                    self.packet.clear()
            if granule != 0xFFFFFFFFFFFFFFFF:
                if granule < self.granule:
                    raise InvalidOgg("Ogg media time went backwards")
                self.granule = granule
            self.ended = eos
            pages.append(
                Page(
                    raw,
                    serial,
                    granule,
                    bos,
                    eos,
                    self.granule * 1000 // self.rate if self.rate else 0,
                    self.headers == 3,
                )
            )
        return pages

    def finish(self):
        if self.buffer or self.packet or not self.ended:
            raise InvalidOgg("Truncated Ogg stream")
