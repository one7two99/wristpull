"""A fake Zepp OS band behind the ``Transport`` protocol: the other end of the real codec.

``FakeBandState`` holds the key, the clock offset and the data per fetch kind; ``FakeTransport``
answers the client's chunks the way the strap does (ECDH + AES authentication, service list, time,
device info, the three-step fetch with data packets on characteristic 0005). Everything is recorded
so tests can assert what the client asked for.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from wristpull.zeppos import aes, ecdh, protocol
from wristpull.zeppos.band import NoBandError
from wristpull.zeppos.chunked import Decoder, Encoder, ack
from wristpull.zeppos.fetch import BY_CODE, Kind
from wristpull.zeppos.protocol import (
    CH_ACTIVITY_CONTROL,
    CH_ACTIVITY_DATA,
    CH_CHUNKED_READ,
    CH_CHUNKED_WRITE,
    EP_AUTH,
    EP_DEVICE_INFO,
    EP_FETCH,
    EP_SERVICES,
    EP_TIME,
    QUARTER_HOUR,
)

FAKE_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
FAKE_ADDRESS = "AA:BB:CC:DD:EE:02"
MINUTE = timedelta(minutes=1)


@dataclass
class FakeBandState:
    auth_key: bytes = FAKE_KEY
    tz_quarters: int = 4  # CET
    serial: str = "24400000000001"
    hardware: str = "0.132.24.2"
    firmware: str = "3.25.0.3"
    fetch_endpoint: bool = True  # False: the band has no endpoint 0x4b, control goes over characteristic 0004
    # minute kinds: (utc of the first sample, bytes); record kinds: [(utc, record bytes)]
    minutes: dict[str, tuple[datetime, bytes]] = field(default_factory=dict)
    records: dict[str, list[tuple[datetime, bytes]]] = field(default_factory=dict)
    # recorders
    fetch_requests: list[tuple[str, datetime]] = field(default_factory=list)
    acks: list[tuple[str, bool]] = field(default_factory=list)
    time_set: list[bytes] = field(default_factory=list)
    authenticated: bool = False

    def services(self) -> dict[int, bool]:
        base = {EP_SERVICES: False, EP_TIME: False, EP_DEVICE_INFO: False, EP_AUTH: False, 0x0015: False}
        if self.fetch_endpoint:
            base[EP_FETCH] = True  # encrypted on purpose: exercises the AES path for every fetch
        return base

    def data_since(self, kind: Kind, since: datetime) -> tuple[datetime, bytes, int] | None:
        """(start utc, bytes, count) the band would send for ``kind`` from ``since`` on."""
        if kind.minutes:
            entry = self.minutes.get(kind.name)
            if entry is None:
                return None
            first, data = entry
            skip = max(0, int((since - first).total_seconds() // 60))
            if skip * kind.record >= len(data):
                return None
            return first + MINUTE * skip, data[skip * kind.record :], (len(data) - skip * kind.record) // kind.record
        recs = [(ts, rec) for ts, rec in sorted(self.records.get(kind.name, [])) if ts >= since]
        if not recs:
            return None
        body = b"".join(rec for _, rec in recs)
        if kind.name == "spo2":
            body = bytes([protocol.SPO2_VERSION]) + body if hasattr(protocol, "SPO2_VERSION") else b"\x02" + body
        return recs[0][0], body, len(recs)


class FakeTransport:
    """``Transport`` implementation that plays the band in ``state``. ``present=False`` fails to connect."""

    def __init__(self, state: FakeBandState, *, present: bool = True, mtu: int = 23) -> None:
        self.state = state
        self.present = present
        self.mtu = mtu
        self.connected = False
        self.callbacks: dict[str, Callable[[bytes], None]] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.client_acks: list[bytes] = []
        self._encoder = Encoder(mtu)
        self._decoder = Decoder()
        self._private: bytes | None = None
        self._random: bytes | None = None
        self._session: bytes | None = None
        self._shared: bytes | None = None

    async def connect(self) -> None:
        if not self.present:
            raise NoBandError(f"band {FAKE_ADDRESS} not found")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe(self, uuid: str, callback: Callable[[bytes], None]) -> None:
        assert self.connected
        self.callbacks[uuid] = callback

    async def write(self, uuid: str, data: bytes) -> None:
        assert self.connected, "write before connect"
        self.writes.append((uuid, bytes(data)))
        if uuid == CH_CHUNKED_WRITE:
            message = self._decoder.feed(bytes(data))
            if message is None:
                return
            if message.needs_ack:
                self._notify(CH_CHUNKED_WRITE, ack(message.handle, message.count))
            self._handle(message.endpoint, message.payload)
        elif uuid == CH_CHUNKED_READ:
            self.client_acks.append(bytes(data))
        elif uuid == CH_ACTIVITY_CONTROL:
            for reply in self._fetch(bytes(data)):
                self._notify(CH_ACTIVITY_CONTROL, reply)
        else:
            raise AssertionError(f"unexpected write to {uuid}")

    def _notify(self, uuid: str, data: bytes) -> None:
        cb = self.callbacks.get(uuid)
        if cb is not None:
            cb(data)

    def _reply(self, endpoint: int, payload: bytes) -> None:
        encrypt = self.state.services().get(endpoint, False) and self._session is not None
        for chunk in self._encoder.encode(endpoint, payload, encrypt=encrypt):
            self._notify(CH_CHUNKED_READ, chunk)

    def _handle(self, endpoint: int, payload: bytes) -> None:
        if endpoint == EP_AUTH:
            self._auth(payload)
        elif endpoint == EP_SERVICES:
            assert payload == protocol.SERVICES_REQUEST
            services = self.state.services()
            body = bytes([0x04]) + struct.pack("<H", len(services)) + b"".join(struct.pack("<HB", ep, 1 if enc else 0) for ep, enc in services.items())
            self._reply(EP_SERVICES, body)
        elif endpoint == EP_TIME:
            self.state.time_set.append(payload)
            self._reply(EP_TIME, b"\x06\x01")
        elif endpoint == EP_DEVICE_INFO:
            s = self.state
            mac = bytes.fromhex(FAKE_ADDRESS.replace(":", ""))
            body = b"\x02\x01\x7f" + b"\0" * 7 + b"\x08" + mac[:3] + b"\xff\xfe" + mac[3:] + "\0".join([s.serial, s.hardware, s.firmware, "W", "841000000000000"]).encode() + b"\0" + mac
            self._reply(EP_DEVICE_INFO, body)
        elif endpoint == EP_FETCH:
            assert self.state.fetch_endpoint, "the band has no fetch endpoint"
            for reply in self._fetch(payload):
                self._reply(EP_FETCH, reply)
        else:
            raise AssertionError(f"unexpected endpoint 0x{endpoint:04x}")

    def _auth(self, payload: bytes) -> None:
        if payload[0] == 0x04:
            assert payload[:4] == bytes([0x04, 0x02, 0x00, 0x02])
            client_pub = payload[4:52]
            self._private = ecdh.new_private()
            self._random = bytes(range(16))
            self._shared = ecdh.shared_secret(self._private, client_pub)
            self._reply(EP_AUTH, bytes([0x10, 0x04, 0x01]) + self._random + ecdh.generate_public(self._private))
        elif payload[0] == 0x05:
            assert self._shared is not None and self._random is not None
            keys = protocol.session_keys(self._shared, self.state.auth_key)
            ok = payload[1:17] == aes.encrypt(self.state.auth_key, self._random) and payload[17:33] == aes.encrypt(keys.key, self._random)
            if ok:
                self._session = keys.key
                self._encoder.set_session(keys.key, keys.sequence)
                self._decoder.key = keys.key
                self.state.authenticated = True
                self._reply(EP_AUTH, bytes([0x10, 0x05, 0x01]))
            else:
                self._reply(EP_AUTH, bytes([0x10, 0x05, protocol.AUTH_WRONG_KEY]))
        else:
            raise AssertionError(f"unexpected auth payload {payload.hex()}")

    _pending: tuple[Kind, bytes] | None = None

    def _fetch(self, payload: bytes) -> list[bytes]:
        cmd = payload[0]
        if cmd == protocol.CMD_FETCH_SINCE:
            kind = BY_CODE[payload[1]]
            year, month, day, hour, minute, second, tz = struct.unpack_from("<HBBBBBb", payload, 2)
            since = (datetime(year, month, day, hour, minute, second) - timedelta(seconds=tz * QUARTER_HOUR)).replace(tzinfo=UTC)
            self.state.fetch_requests.append((kind.name, since))
            found = self.state.data_since(kind, since)
            if found is None:
                self._pending = None
                return [bytes([0x10, 0x01, 0x01]) + struct.pack("<I", 0) + struct.pack("<HBBBBBb", 1970, 1, 1, 1, 0, 0, self.state.tz_quarters) + b"\0"]
            start, data, count = found
            self._pending = (kind, data)
            local = start + timedelta(seconds=self.state.tz_quarters * QUARTER_HOUR)
            return [bytes([0x10, 0x01, 0x01]) + struct.pack("<I", count) + struct.pack("<HBBBBBb", local.year, local.month, local.day, local.hour, local.minute, local.second, self.state.tz_quarters) + b"\0"]
        if cmd == protocol.CMD_FETCH_DATA:
            assert self._pending is not None, "fetch-data without a since request"
            kind, data = self._pending
            size = self.mtu - 3 - 1
            for i, pos in enumerate(range(0, len(data), size)):
                self._notify(CH_ACTIVITY_DATA, bytes([i & 0xFF]) + data[pos : pos + size])
            return [bytes([0x10, 0x02, 0x01]) + struct.pack("<I", zlib.crc32(data))]
        if cmd == protocol.CMD_FETCH_ACK:
            keep = payload[1] == protocol.ACK_KEEP
            kind = self._pending[0].name if self._pending else "?"
            self.state.acks.append((kind, keep))
            if not keep and self._pending is not None:
                self.state.minutes.pop(self._pending[0].name, None)
                self.state.records.pop(self._pending[0].name, None)
            self._pending = None
            return [bytes([0x10, 0x03, 0x01])]
        raise AssertionError(f"unexpected fetch command {payload.hex()}")
