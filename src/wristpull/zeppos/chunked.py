"""The Huami 2021 chunked transfer: messages to a 16-bit endpoint, split into MTU-sized chunks,
optionally AES-encrypted with a per-message key.

Chunk layout (the "extended" variant every Zepp OS device uses)::

    0x03 | flags | 0x00 | handle | count | [length u32 LE | endpoint u16 LE, first chunk only] | data

flags: 0x01 first chunk, 0x02 last chunk, 0x04 the receiver should ack, 0x08 encrypted.
Encrypted messages carry ``data + sequence u32 + crc32(data + sequence) u32``, zero-padded to 16
bytes and AES-ECB encrypted with ``session_key XOR handle`` (byte-wise). The receiver acks a chunk
with ``0x04 0x00 handle 0x01 count`` on the characteristic it *reads* from.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from . import aes

ATT_OVERHEAD = 3
FIRST_HEADER = 11
NEXT_HEADER = 5
FLAG_FIRST, FLAG_LAST, FLAG_ACK, FLAG_ENCRYPTED = 0x01, 0x02, 0x04, 0x08


def message_key(session_key: bytes, handle: int) -> bytes:
    return bytes(b ^ (handle & 0xFF) for b in session_key)


def chunk_size(mtu: int) -> int:
    return min(512, max(23, mtu) - ATT_OVERHEAD)


def ack(handle: int, count: int) -> bytes:
    return bytes([0x04, 0x00, handle & 0xFF, 0x01, count & 0xFF])


class Encoder:
    """Turns ``(endpoint, payload)`` into chunks. ``handle`` counts messages, ``sequence`` encrypted ones."""

    def __init__(self, mtu: int = 23) -> None:
        self.mtu = mtu
        self.handle = 0
        self.sequence = 0
        self.key: bytes | None = None

    def set_session(self, key: bytes, sequence: int) -> None:
        self.key = key
        self.sequence = sequence

    def encode(self, endpoint: int, payload: bytes, *, encrypt: bool) -> list[bytes]:
        self.handle = (self.handle + 1) & 0xFF
        length = len(payload)
        data = payload
        if encrypt:
            if self.key is None:
                raise RuntimeError("no session key: authenticate first")
            body = payload + struct.pack("<I", self.sequence & 0xFFFFFFFF)
            self.sequence = (self.sequence + 1) & 0xFFFFFFFF
            body += struct.pack("<I", zlib.crc32(body))
            body += b"\0" * (-len(body) % aes.BLOCK)
            data = aes.encrypt(message_key(self.key, self.handle), body)
        chunks: list[bytes] = []
        pos, count = 0, 0
        while pos < len(data) or count == 0:
            header = FIRST_HEADER if count == 0 else NEXT_HEADER
            take = min(len(data) - pos, chunk_size(self.mtu) - header)
            flags = (FLAG_ENCRYPTED if encrypt else 0) | (FLAG_FIRST if count == 0 else 0)
            if pos + take >= len(data):
                flags |= FLAG_LAST | FLAG_ACK
            head = bytes([0x03, flags, 0x00, self.handle, count])
            if count == 0:
                head += struct.pack("<IH", length, endpoint)
            chunks.append(head + data[pos : pos + take])
            pos += take
            count += 1
        return chunks


@dataclass(frozen=True)
class Message:
    endpoint: int
    payload: bytes
    handle: int
    count: int  # index of the last chunk (what an ack names)
    needs_ack: bool


class Decoder:
    """Reassembles chunks into messages; ``feed`` returns the message once its last chunk arrived."""

    def __init__(self) -> None:
        self.key: bytes | None = None
        self._handle: int | None = None
        self._endpoint = 0
        self._length = 0
        self._encrypted = False
        self._buffer = bytearray()
        self.last_handle = 0
        self.last_count = 0

    def reset(self) -> None:
        self._handle = None
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> Message | None:
        if not chunk or chunk[0] != 0x03:
            raise ValueError(f"not a chunk: {chunk.hex()}")
        if len(chunk) < NEXT_HEADER:
            raise ValueError(f"chunk too short: {chunk.hex()}")
        flags, handle, count = chunk[1], chunk[3], chunk[4]
        pos = NEXT_HEADER
        if self._handle is not None and handle != self._handle:
            raise ValueError(f"chunk of handle {handle} while reassembling {self._handle}")
        self.last_handle, self.last_count = handle, count
        if flags & FLAG_FIRST:
            if len(chunk) < FIRST_HEADER:
                raise ValueError(f"first chunk too short: {chunk.hex()}")
            self._length, self._endpoint = struct.unpack_from("<IH", chunk, pos)
            pos = FIRST_HEADER
            self._handle = handle
            self._encrypted = bool(flags & FLAG_ENCRYPTED)
            self._buffer = bytearray()
        elif self._handle is None:
            raise ValueError("continuation chunk without a first chunk")
        self._buffer += chunk[pos:]
        if not flags & FLAG_LAST:
            return None
        data = bytes(self._buffer)
        endpoint, length, encrypted = self._endpoint, self._length, self._encrypted
        self.reset()
        if encrypted:
            if self.key is None:
                raise RuntimeError("encrypted message before authentication")
            whole = data[: len(data) - len(data) % aes.BLOCK]
            data = aes.decrypt(message_key(self.key, handle), whole)[:length]
        return Message(endpoint, data, handle, count, bool(flags & FLAG_ACK))
