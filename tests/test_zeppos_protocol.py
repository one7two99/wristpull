"""Payload builders and parsers of the Zepp OS protocol (pure functions)."""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from wristpull.zeppos import aes, protocol
from wristpull.zeppos.protocol import ProtocolError

TZ = ZoneInfo("Europe/Berlin")


def test_parse_auth_key_formats():
    key = protocol.parse_auth_key("0x00112233445566778899aabbccddeeff\n")
    assert key == bytes.fromhex("00112233445566778899aabbccddeeff")
    assert protocol.parse_auth_key("00112233445566778899AABBCCDDEEFF") == key
    with pytest.raises(ValueError):
        protocol.parse_auth_key("0x1234")
    with pytest.raises(ValueError):
        protocol.parse_auth_key("not hex at all, really not")


def test_auth_payloads():
    pub = bytes(range(48))
    assert protocol.auth_request(pub) == bytes([4, 2, 0, 2]) + pub
    random = bytes(range(16, 32))
    reply = bytes([0x10, 0x04, 0x01]) + random + pub
    challenge = protocol.parse_auth_challenge(reply)
    assert (challenge.random, challenge.public_key) == (random, pub)
    shared = bytes(range(48))
    auth_key = bytes([0xAA] * 16)
    keys = protocol.session_keys(shared, auth_key)
    assert keys.sequence == 0x03020100 and keys.key == bytes(b ^ 0xAA for b in shared[8:24])
    confirm = protocol.auth_confirm(random, auth_key, keys.key)
    assert confirm[0] == 5 and confirm[1:17] == aes.encrypt(auth_key, random) and confirm[17:] == aes.encrypt(keys.key, random)
    protocol.parse_auth_result(bytes([0x10, 0x05, 0x01]))
    with pytest.raises(ProtocolError, match="wrong key"):
        protocol.parse_auth_result(bytes([0x10, 0x05, 0x25]))
    with pytest.raises(ProtocolError):
        protocol.parse_auth_result(bytes([0x10, 0x05, 0x02]))
    with pytest.raises(ProtocolError):
        protocol.parse_auth_challenge(bytes([0x10, 0x04, 0x02]))
    with pytest.raises(ProtocolError):
        protocol.parse_auth_challenge(bytes([0x10, 0x04, 0x01, 1, 2]))


def test_parse_services():
    payload = bytes([0x04]) + struct.pack("<H", 3) + struct.pack("<HB", 0x004B, 0) + struct.pack("<HB", 0x0017, 1) + struct.pack("<HB", 0x0043, 0)
    assert protocol.parse_services(payload) == {0x4B: False, 0x17: True, 0x43: False}
    with pytest.raises(ProtocolError):
        protocol.parse_services(b"\x05\x00")


def test_set_time_payload_in_winter_and_summer():
    winter = datetime(2026, 11, 7, 18, 45, 30, 500_000, tzinfo=TZ)
    p = protocol.set_time_payload(winter)
    assert p[0] == 5 and struct.unpack_from("<HBBBBBB", p, 1) == (2026, 11, 7, 18, 45, 30, 6)  # Saturday = 6
    assert p[9] == 128 and p[10] == 0x00 and p[11] == 4  # fractions256, no DST, +1 h = 4 quarter hours
    summer = datetime(2026, 7, 1, 8, 0, tzinfo=TZ)
    assert protocol.set_time_payload(summer)[10:] == bytes([0x08, 8])
    with pytest.raises(ValueError):
        protocol.set_time_payload(datetime(2026, 1, 1))


def test_parse_device_info_from_the_strap():
    mac = bytes.fromhex("AABBCCDDEE02")
    raw = b"\x02\x01\x7f" + b"\0" * 7 + b"\x08" + mac[:3] + b"\xff\xfe" + mac[3:] + b"24400000000001\x000.132.24.2\x003.25.0.3\x00\x01W\x01\x9d\x00\x03\x01841000000000000\x00" + mac
    info = protocol.parse_device_info(raw)
    assert (info.serial, info.hardware, info.firmware, info.mac) == ("24400000000001", "0.132.24.2", "3.25.0.3", "AA:BB:CC:DD:EE:02")
    assert "3.25.0.3" in info.label()
    with pytest.raises(ProtocolError):
        protocol.parse_device_info(b"\x02\x02")
    with pytest.raises(ProtocolError):
        protocol.parse_device_info(b"\x02\x01only\0")


def test_fetch_payloads_and_timezone_byte():
    since = datetime(2026, 11, 6, 18, 47, tzinfo=TZ)
    p = protocol.fetch_since_payload(0x13, since)
    assert p == bytes([1, 0x13]) + struct.pack("<HBBBBBb", 2026, 11, 6, 18, 47, 0, 4)
    # the band answers in its own local time plus offset: +2 h here, i.e. the clock had summer time
    reply = bytes([0x10, 0x01, 0x01]) + struct.pack("<I", 10165) + struct.pack("<HBBBBBb", 2026, 10, 31, 18, 23, 0, 8) + b"\0"
    start = protocol.parse_fetch_start(reply)
    assert start.count == 10165 and start.tz_quarters == 8
    assert start.start_utc == datetime(2026, 10, 31, 16, 23, tzinfo=UTC)
    nothing = protocol.parse_fetch_start(bytes([0x10, 0x01, 0x01]) + struct.pack("<I", 0) + struct.pack("<HBBBBBb", 1970, 1, 1, 1, 0, 0, 4) + b"\0")
    assert nothing.count == 0
    with pytest.raises(ProtocolError):
        protocol.parse_fetch_start(bytes([0x10, 0x01, 0x02]))
    data = bytes(range(100))
    protocol.parse_fetch_end(bytes([0x10, 0x02, 0x01]) + struct.pack("<I", zlib.crc32(data)), data)
    protocol.parse_fetch_end(bytes([0x10, 0x02, 0x01]), data)  # no checksum given
    with pytest.raises(ProtocolError, match="checksum"):
        protocol.parse_fetch_end(bytes([0x10, 0x02, 0x01]) + struct.pack("<I", 1), data)
    with pytest.raises(ProtocolError):
        protocol.parse_fetch_end(bytes([0x10, 0x02, 0x00]), data)
    assert protocol.fetch_ack_payload(True) == b"\x03\x09" and protocol.fetch_ack_payload(False) == b"\x03\x01"
    assert protocol.is_fetch_ack_reply(b"\x10\x03\x01") and not protocol.is_fetch_ack_reply(b"\x10\x02\x01")
