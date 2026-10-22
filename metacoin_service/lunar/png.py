"""Minimal grayscale PNG writer (stdlib only) for product previews."""
import struct
import zlib


def _chunk(tag, data):
    c = tag + data
    return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)


def gray_png(rows, scale=4):
    """rows: list of lists of 0..255 ints (None -> magenta nodata marker via a palette); scale: integer pixel replication."""
    h, w = len(rows), len(rows[0])
    raw = bytearray()
    for r in rows:
        line = bytearray()
        for v in r:
            px = bytes((255, 0, 255)) if v is None else bytes((v, v, v))
            line += px * scale
        raw += b'\x00' + bytes(line) * 1
        for _ in range(scale - 1):
            raw += b'\x00' + bytes(line)
    ihdr = struct.pack('>IIBBBBB', w * scale, h * scale, 8, 2, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + _chunk(b'IEND', b'')
