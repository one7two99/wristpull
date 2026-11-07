"""A minimal protocol-buffers wire decoder: enough for the Zepp OS workout summary, no dependency.

``decode`` returns ``{field_number: [values]}`` with raw values: ints for varints, bytes for
length-delimited fields (sub-messages, strings, packed repeated numbers) and fixed 32/64-bit fields.
Helpers turn those into floats, signed ints or packed lists.
"""

from __future__ import annotations

import struct

WIRE_VARINT, WIRE_FIXED64, WIRE_LENGTH, WIRE_FIXED32 = 0, 1, 2, 5


class ProtobufError(ValueError):
    pass


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise ProtobufError("truncated varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos
        if shift > 70:
            raise ProtobufError("varint too long")


def decode(data: bytes) -> dict[int, list[int | bytes]]:
    out: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        field, wire = key >> 3, key & 7
        value: int | bytes
        if wire == WIRE_VARINT:
            value, pos = read_varint(data, pos)
        elif wire == WIRE_FIXED64:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == WIRE_LENGTH:
            length, pos = read_varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        elif wire == WIRE_FIXED32:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ProtobufError(f"unsupported wire type {wire} for field {field}")
        if isinstance(value, bytes) and len(value) != (8 if wire == WIRE_FIXED64 else 4 if wire == WIRE_FIXED32 else len(value)):
            raise ProtobufError(f"truncated field {field}")
        if pos > len(data):
            raise ProtobufError(f"truncated field {field}")
        out.setdefault(field, []).append(value)
    return out


def first(fields: dict[int, list], number: int):
    values = fields.get(number)
    return values[0] if values else None


def as_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bytes) and len(value) == 4:
        return struct.unpack("<f", value)[0]
    if isinstance(value, bytes) and len(value) == 8:
        return struct.unpack("<d", value)[0]
    return float(value)


def as_int(value) -> int | None:
    """A varint as int32 (two's complement for values the encoder wrote as negative int32)."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return int.from_bytes(value, "little", signed=True)
    value = int(value)
    return value - (1 << 64) if value >= 1 << 63 else (value - (1 << 32) if (1 << 31) <= value < (1 << 32) else value)


def packed_ints(value) -> list[int]:
    """A packed repeated int field (bytes) or a single unpacked value."""
    if value is None:
        return []
    if isinstance(value, int):
        return [value]
    out: list[int] = []
    pos = 0
    while pos < len(value):
        item, pos = read_varint(value, pos)
        out.append(item)
    return out


def submessage(fields: dict[int, list], number: int) -> dict[int, list] | None:
    raw = first(fields, number)
    if raw is None:
        return None
    if not isinstance(raw, bytes):
        raise ProtobufError(f"field {number} is not a sub-message")
    return decode(raw)


def as_str(value) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
