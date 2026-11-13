"""ECDH B-163, AES-128 and the chunked codec: the pieces every band message passes through."""

from __future__ import annotations

import pytest

from wristpull.zeppos import aes, ecdh
from wristpull.zeppos.chunked import Decoder, Encoder, ack, chunk_size, message_key


def test_aes_fips197_vector():
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert aes.encrypt(key, plain) == cipher
    assert aes.decrypt(key, cipher) == plain
    assert aes.decrypt(key, aes.encrypt(key, plain * 3)) == plain * 3
    with pytest.raises(ValueError):
        aes.encrypt(key, b"short")
    with pytest.raises(ValueError):
        aes.expand_key(b"\0" * 8)


def test_ecdh_curve_properties():
    assert ecdh.on_curve(ecdh.BASE_X, ecdh.BASE_Y)
    assert ecdh.point_mul(ecdh.BASE_X, ecdh.BASE_Y, ecdh.ORDER) == (0, 0)  # the base point has the published order
    a = ecdh.new_private(lambda n: bytes([0x5A] * n))
    b = ecdh.new_private(lambda n: bytes([0xC3] * n))
    pub_a, pub_b = ecdh.generate_public(a), ecdh.generate_public(b)
    assert len(pub_a) == ecdh.PUBLIC_SIZE and ecdh.on_curve(*ecdh._decode_point(pub_a))
    assert ecdh.shared_secret(a, pub_b) == ecdh.shared_secret(b, pub_a)
    assert ecdh.shared_secret(a, pub_b) != ecdh.shared_secret(a, pub_a)
    with pytest.raises(ValueError):
        ecdh.shared_secret(a, b"\x01" * 48)  # not on the curve
    with pytest.raises(ValueError):
        ecdh.generate_public(b"\x01" + b"\0" * 23)  # too small
    assert ecdh.field_mul(ecdh.field_inv(12345), 12345) == 1
    with pytest.raises(ZeroDivisionError):
        ecdh.field_inv(0)


def test_new_private_retries_small_values():
    draws = iter([b"\x01" + b"\0" * 23, b"\x7f" * 24])
    assert ecdh.new_private(lambda n: next(draws)) == b"\x7f" * 24


def test_chunked_plain_roundtrip_single_chunk():
    enc, dec = Encoder(23), Decoder()
    chunks = enc.encode(0x0082, b"\x03", encrypt=False)
    assert len(chunks) == 1 and chunks[0][:5] == bytes([0x03, 0x01 | 0x02 | 0x04, 0x00, 1, 0])
    msg = dec.feed(chunks[0])
    assert msg is not None and (msg.endpoint, msg.payload, msg.handle, msg.count, msg.needs_ack) == (0x82, b"\x03", 1, 0, True)
    assert ack(msg.handle, msg.count) == bytes([0x04, 0x00, 1, 0x01, 0])


def test_chunked_splits_on_mtu_and_reassembles():
    enc, dec = Encoder(23), Decoder()
    payload = bytes(range(52))  # 4 + 48: the auth request
    chunks = enc.encode(0x0082, payload, encrypt=False)
    assert [len(c) for c in chunks] == [20, 20, 20, 18]  # 9 + 15 + 15 + 13 data bytes
    assert chunks[0][1] == 0x01 and chunks[1][1] == 0x00 and chunks[-1][1] == 0x02 | 0x04
    assert [c[4] for c in chunks] == [0, 1, 2, 3]
    got = [dec.feed(c) for c in chunks]
    assert got[:-1] == [None, None, None] and got[-1].payload == payload
    assert chunk_size(247) == 244 and chunk_size(10) == 20 and chunk_size(600) == 512


def test_chunked_encrypted_roundtrip_and_sequence():
    key = bytes(range(16))
    enc, dec = Encoder(247), Decoder()
    enc.set_session(key, 0x11223344)
    dec.key = key
    first = enc.encode(0x004B, b"\x02", encrypt=True)
    assert len(first) == 1 and first[0][1] & 0x08 and len(first[0]) == 11 + 16  # 1 + 4 + 4 -> one AES block
    assert dec.feed(first[0]).payload == b"\x02"
    assert enc.sequence == 0x11223345 and enc.handle == 1
    second = enc.encode(0x004B, bytes(range(40)), encrypt=True)
    assert dec.feed(second[0]).payload == bytes(range(40))
    assert message_key(key, 2) == bytes(b ^ 2 for b in key)


def test_chunked_errors():
    dec = Decoder()
    with pytest.raises(ValueError):
        dec.feed(b"\x04\x00")
    with pytest.raises(ValueError):
        dec.feed(bytes([0x03, 0x00, 0x00, 1, 1, 9]))  # continuation without a first chunk
    enc = Encoder(23)
    with pytest.raises(RuntimeError):
        enc.encode(1, b"x", encrypt=True)  # no session
    chunks = Encoder(23).encode(0x0082, bytes(range(40)), encrypt=False)
    dec.feed(chunks[0])
    with pytest.raises(ValueError):
        dec.feed(bytes([0x03, 0x02, 0x00, 7, 1, 0]))  # another handle in the middle of a message
    dec.reset()
    key_dec = Decoder()
    with pytest.raises(RuntimeError):
        key_dec.feed(Encoder(247).encode(1, b"x", encrypt=False)[0][:1] + bytes([0x0B, 0, 1, 0]) + b"\x10\0\0\0\x01\0" + b"\0" * 16)


def test_connect_attempts_retry_then_raise():
    import asyncio

    from wristpull.zeppos.transport import attempts

    calls: list[int] = []
    waits: list[float] = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("busy")
        return "client"

    async def nap(seconds: float) -> None:
        waits.append(seconds)

    assert asyncio.run(attempts(flaky, tries=3, delay=0.5, sleep=nap)) == "client"
    assert len(calls) == 3 and waits == [0.5, 0.5]

    async def broken():
        raise RuntimeError("never")

    with pytest.raises(RuntimeError, match="never"):
        asyncio.run(attempts(broken, tries=2, delay=0, sleep=nap))


def test_ble_error_hints():
    from wristpull.zeppos.transport import DBUS_HINT, ble_error

    denied = ble_error(RuntimeError("[org.freedesktop.DBus.Error.AccessDenied] An AppArmor policy prevents this"), "scan failed")
    assert str(denied).startswith("scan failed: " + DBUS_HINT[:20]) and "AppArmor" in str(denied)
    assert "no powered Bluetooth adapter" in str(ble_error(RuntimeError("No powered Bluetooth adapters found"), "x"))
    off = str(ble_error(RuntimeError("('No Bluetooth adapters found.', <BleakBluetoothNotAvailableReason.NO_BLUETOOTH: 1>)"), "x"))
    assert "rfkill unblock bluetooth" in off
    assert str(ble_error(RuntimeError(""), "could not connect")) == "could not connect: RuntimeError"
    stuck = str(ble_error(TimeoutError(), "could not connect"))
    assert stuck.startswith("could not connect: timed out") and "bluetoothctl power off" in stuck
