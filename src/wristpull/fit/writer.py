"""A minimal FIT encoder: synthetic test fixtures and the workout files pushed to the watch.

Little-endian definition/data messages, scalar and
array fields, strings, header and file CRC. No real device data is involved.
"""

from __future__ import annotations

import struct
from datetime import datetime

from ..timeutil import utc_to_fit

BASE_TYPES: dict[str, tuple[int, str, int]] = {
    # name: (base type byte, struct format, size)
    "enum": (0x00, "B", 1),
    "sint8": (0x01, "b", 1),
    "uint8": (0x02, "B", 1),
    "sint16": (0x83, "h", 2),
    "uint16": (0x84, "H", 2),
    "sint32": (0x85, "i", 4),
    "uint32": (0x86, "I", 4),
    "string": (0x07, "s", 1),
    "float32": (0x88, "f", 4),
    "float64": (0x89, "d", 8),
    "uint8z": (0x0A, "B", 1),
    "uint16z": (0x8B, "H", 2),
    "uint32z": (0x8C, "I", 4),
    "byte": (0x0D, "B", 1),
    "sint64": (0x8E, "q", 8),
    "uint64": (0x8F, "Q", 8),
    "uint64z": (0x90, "Q", 8),
}

CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
]


def fit_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[byte & 0xF]
        tmp = CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def ts(dt: datetime) -> int:
    """FIT timestamp (seconds since 1989-12-31) for an aware datetime."""
    return utc_to_fit(dt)


Field = tuple[int, str, object]  # (def_num, base type name, value)


class FitWriter:
    """``big_endian=True`` writes the byte order Garmin's own tools use (the watch accepts both)."""

    def __init__(self, *, big_endian: bool = False) -> None:
        self._body = bytearray()
        self._locals: dict[tuple, int] = {}
        self._next_local = 0
        self._arch = 1 if big_endian else 0
        self._prefix = ">" if big_endian else "<"

    def _define(self, global_num: int, fields: list[Field]) -> int:
        key = (global_num, tuple((num, typ, self._size(typ, val)) for num, typ, val in fields))
        if key in self._locals:
            return self._locals[key]
        local = self._next_local % 16
        self._next_local += 1
        # drop any older definition using this local number
        for k, v in list(self._locals.items()):
            if v == local:
                del self._locals[k]
        self._locals[key] = local
        self._body += bytes([0x40 | local, 0, self._arch])  # definition header, reserved, architecture
        self._body += struct.pack(self._prefix + "HB", global_num, len(fields))
        for num, typ, val in fields:
            self._body += bytes([num, self._size(typ, val), BASE_TYPES[typ][0]])
        return local

    @staticmethod
    def _size(typ: str, val: object) -> int:
        code, fmt, size = BASE_TYPES[typ]
        if typ == "string":
            return len(str(val).encode("utf-8")) + 1
        if isinstance(val, (list, tuple)):
            return size * len(val)
        return size

    def message(self, global_num: int, fields: list[Field]) -> None:
        """Append one data message, defining its layout on first use."""
        local = self._define(global_num, fields)
        self._body += bytes([local])
        for num, typ, val in fields:
            code, fmt, size = BASE_TYPES[typ]
            if typ == "string":
                self._body += str(val).encode("utf-8") + b"\x00"
            elif isinstance(val, (list, tuple)):
                self._body += struct.pack(self._prefix + fmt * len(val), *val)
            else:
                self._body += struct.pack(self._prefix + fmt, val)

    def finish(self) -> bytes:
        header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(self._body), b".FIT")
        header += struct.pack("<H", fit_crc(header))
        data = header + bytes(self._body)
        return data + struct.pack("<H", fit_crc(data))
