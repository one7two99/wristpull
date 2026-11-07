"""Pure functions of the Zepp OS protocol: UUIDs, endpoints, payload builders and parsers. No I/O."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from . import aes, ecdh


def _uuid(short: str) -> str:
    return f"0000{short}-0000-3512-2118-0009af100700"


SERVICE_MAIN = "0000fee0-0000-1000-8000-00805f9b34fb"
CH_CHUNKED_WRITE = _uuid("0016")  # we write chunks here; the band notifies acks here
CH_CHUNKED_READ = _uuid("0017")  # the band's chunks arrive here; our acks go here
CH_ACTIVITY_CONTROL = _uuid("0004")  # fallback fetch control when endpoint 0x4b is absent
CH_ACTIVITY_DATA = _uuid("0005")  # raw fetch data, never encrypted

EP_SERVICES = 0x0000
EP_CONNECTION = 0x0015
EP_DEVICE_INFO = 0x0043
EP_TIME = 0x0047
EP_FETCH = 0x004B
EP_AUTH = 0x0082

RESPONSE = 0x10
SUCCESS = 0x01
AUTH_WRONG_KEY = 0x25
AUTH_KEY_SIZE = 16

CMD_FETCH_SINCE = 0x01
CMD_FETCH_DATA = 0x02
CMD_FETCH_ACK = 0x03
ACK_KEEP = 0x09
ACK_DELETE = 0x01

QUARTER_HOUR = 900


class ProtocolError(RuntimeError):
    """The band answered something this code does not understand."""


def parse_auth_key(text: str) -> bytes:
    """``0x…`` or bare hex, 32 digits; whitespace ignored."""
    digits = text.strip().removeprefix("0x").removeprefix("0X").strip()
    try:
        key = bytes.fromhex(digits)
    except ValueError as exc:
        raise ValueError("auth key must be 32 hex digits") from exc
    if len(key) != AUTH_KEY_SIZE:
        raise ValueError(f"auth key must be {AUTH_KEY_SIZE} bytes, got {len(key)}")
    return key


# --- authentication (endpoint 0x82, plaintext) --------------------------------------------------


def auth_request(public_key: bytes) -> bytes:
    return bytes([0x04, 0x02, 0x00, 0x02]) + public_key


@dataclass(frozen=True)
class AuthChallenge:
    random: bytes
    public_key: bytes


def parse_auth_challenge(payload: bytes) -> AuthChallenge:
    if len(payload) < 3 or payload[0] != RESPONSE or payload[1] != 0x04:
        raise ProtocolError(f"unexpected auth reply {payload.hex()}")
    if payload[2] != SUCCESS:
        raise ProtocolError(f"band rejected the public key (status 0x{payload[2]:02x})")
    if len(payload) < 3 + 16 + ecdh.PUBLIC_SIZE:
        raise ProtocolError("auth reply too short")
    return AuthChallenge(bytes(payload[3:19]), bytes(payload[19 : 19 + ecdh.PUBLIC_SIZE]))


@dataclass(frozen=True)
class SessionKeys:
    key: bytes
    sequence: int


def session_keys(shared: bytes, auth_key: bytes) -> SessionKeys:
    """Session key = shared[8:24] XOR auth key; the encrypted-message counter starts at shared[0:4]."""
    key = bytes(shared[8 + i] ^ auth_key[i] for i in range(AUTH_KEY_SIZE))
    return SessionKeys(key, struct.unpack_from("<I", shared)[0])


def auth_confirm(random: bytes, auth_key: bytes, session_key: bytes) -> bytes:
    return bytes([0x05]) + aes.encrypt(auth_key, random) + aes.encrypt(session_key, random)


def parse_auth_result(payload: bytes) -> None:
    """Raises on failure; ``AUTH_WRONG_KEY`` becomes a clear message."""
    if len(payload) < 3 or payload[0] != RESPONSE or payload[1] != 0x05:
        raise ProtocolError(f"unexpected auth result {payload.hex()}")
    if payload[2] == AUTH_WRONG_KEY:
        raise ProtocolError("the band rejected the auth key (wrong key for this band)")
    if payload[2] != SUCCESS:
        raise ProtocolError(f"authentication failed (status 0x{payload[2]:02x})")


# --- services (endpoint 0x00) ---------------------------------------------------------------------

SERVICES_REQUEST = bytes([0x03])


def parse_services(payload: bytes) -> dict[int, bool]:
    """``{endpoint: encrypted}`` from the ``0x04`` reply."""
    if not payload or payload[0] != 0x04 or len(payload) < 3:
        raise ProtocolError(f"unexpected services reply {payload.hex()}")
    (count,) = struct.unpack_from("<H", payload, 1)
    out: dict[int, bool] = {}
    for i in range(count):
        if 3 + 3 * i + 3 > len(payload):
            break
        endpoint, encrypted = struct.unpack_from("<HB", payload, 3 + 3 * i)
        out[endpoint] = encrypted == 1
    return out


# --- time (endpoint 0x47) -------------------------------------------------------------------------


def tz_quarters(moment: datetime) -> int:
    offset = moment.utcoffset()
    return int((offset or timedelta()).total_seconds() // QUARTER_HOUR)


def set_time_payload(now: datetime) -> bytes:
    """``05`` + the 11-byte current-time block Zepp OS expects (local wall time plus offset)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    dst = now.dst()
    return bytes([0x05]) + struct.pack(
        "<HBBBBBBBBb",
        now.year,
        now.month,
        now.day,
        now.hour,
        now.minute,
        now.second,
        now.isoweekday() % 7,
        int(now.microsecond / 1_000_000 * 256) & 0xFF,
        0x08 if dst else 0x00,
        tz_quarters(now),
    )


# --- device info (endpoint 0x43) ------------------------------------------------------------------

DEVICE_INFO_REQUEST = bytes([0x01])


@dataclass(frozen=True)
class DeviceInfo:
    serial: str
    hardware: str
    firmware: str
    mac: str | None = None
    extra: tuple[str, ...] = ()

    def label(self) -> str:
        return f"serial {self.serial}, firmware {self.firmware}, hardware {self.hardware}"


def _trailing_text(segment: bytes) -> str:
    """The printable ASCII run at the end of a NUL-separated segment (binary fields precede the strings)."""
    end = len(segment)
    start = end
    while start > 0 and 32 <= segment[start - 1] < 127:
        start -= 1
    return segment[start:end].decode("ascii")


def parse_device_info(payload: bytes) -> DeviceInfo:
    """``02 01 …``: a few binary bytes, then NUL-separated strings serial, hardware, firmware, … ."""
    if len(payload) < 2 or payload[0] != 0x02:
        raise ProtocolError(f"unexpected device info reply {payload.hex()}")
    if payload[1] != SUCCESS:
        raise ProtocolError(f"device info request failed (status 0x{payload[1]:02x})")
    texts = [t for t in (_trailing_text(seg) for seg in payload[2:].split(b"\0")) if len(t) >= 3]
    if len(texts) < 3:
        raise ProtocolError("device info without serial/hardware/firmware")
    mac = None
    if len(payload) >= 20:
        tail = payload[-6:]
        mac = ":".join(f"{b:02X}" for b in tail)
    return DeviceInfo(texts[0], texts[1], texts[2], mac, tuple(texts[3:]))


# --- fetch (endpoint 0x4b or characteristic 0004) -------------------------------------------------


def fetch_since_payload(kind: int, since: datetime) -> bytes:
    """``01 <kind> <year u16> <month> <day> <hour> <minute> <second=0> <tz quarters>`` in ``since``'s zone."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    return bytes([CMD_FETCH_SINCE, kind & 0xFF]) + struct.pack("<HBBBBBb", since.year, since.month, since.day, since.hour, since.minute, 0, tz_quarters(since))


@dataclass(frozen=True)
class FetchStart:
    count: int  # samples/packets the band will send (0: nothing new)
    start_utc: datetime  # of the first sample
    tz_quarters: int  # the band's UTC offset for that sample, in quarter hours


def parse_fetch_start(payload: bytes) -> FetchStart:
    """``10 01 01 <count u32> <year u16> <month> <day> <hour> <minute> <second> <tz>``."""
    if len(payload) < 3 or payload[0] != RESPONSE or payload[1] != CMD_FETCH_SINCE:
        raise ProtocolError(f"unexpected fetch reply {payload.hex()}")
    if payload[2] != SUCCESS:
        raise ProtocolError(f"the band refused the fetch (status 0x{payload[2]:02x})")
    if len(payload) < 15:
        raise ProtocolError(f"fetch reply too short: {payload.hex()}")
    (count,) = struct.unpack_from("<I", payload, 3)
    year, month, day, hour, minute, second, tz = struct.unpack_from("<HBBBBBb", payload, 7)
    if count == 0:
        return FetchStart(0, datetime(1970, 1, 1, tzinfo=UTC), tz)
    local = datetime(year, month, day, hour, minute, second)
    start = (local - timedelta(seconds=tz * QUARTER_HOUR)).replace(tzinfo=UTC)
    return FetchStart(count, start, tz)


def parse_fetch_end(payload: bytes, data: bytes) -> None:
    """``10 02 01 [crc32 u32]``: raises when the status or the checksum is wrong."""
    if len(payload) < 3 or payload[0] != RESPONSE or payload[1] != CMD_FETCH_DATA:
        raise ProtocolError(f"unexpected fetch-data reply {payload.hex()}")
    if payload[2] != SUCCESS:
        raise ProtocolError(f"fetch-data failed (status 0x{payload[2]:02x})")
    if len(payload) >= 7:
        (crc,) = struct.unpack_from("<I", payload, 3)
        if crc != zlib.crc32(data):
            raise ProtocolError(f"fetch checksum mismatch: band {crc:08x}, received {zlib.crc32(data):08x}")


def fetch_ack_payload(keep: bool) -> bytes:
    return bytes([CMD_FETCH_ACK, ACK_KEEP if keep else ACK_DELETE])


def is_fetch_ack_reply(payload: bytes) -> bool:
    return len(payload) >= 2 and payload[0] == RESPONSE and payload[1] == CMD_FETCH_ACK


def local_at(utc: datetime, tz: tzinfo) -> datetime:
    return utc.astimezone(tz)
