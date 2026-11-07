"""The client side of a Zepp OS band session on top of an async :class:`Transport`.

``Band`` speaks the protocol (authentication, service list, time, device info, fetch) and knows
nothing about Bluetooth: :mod:`.transport` supplies a bleak-backed transport, tests a fake band.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from . import protocol
from .chunked import Decoder, Encoder, ack
from .ecdh import generate_public, new_private, shared_secret
from .fetch import Kind
from .protocol import (
    CH_ACTIVITY_CONTROL,
    CH_ACTIVITY_DATA,
    CH_CHUNKED_READ,
    CH_CHUNKED_WRITE,
    EP_AUTH,
    EP_DEVICE_INFO,
    EP_FETCH,
    EP_SERVICES,
    EP_TIME,
    DeviceInfo,
    FetchStart,
    ProtocolError,
)

Log = Callable[[str], None]


class BandError(RuntimeError):
    """Connection, protocol or authentication failure talking to the band."""


class NoBandError(BandError):
    """The band is not in range (or connected to another host)."""


class Transport(Protocol):
    """What :class:`Band` needs from a BLE link. Callbacks run on the event loop that awaits the band."""

    mtu: int

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def write(self, uuid: str, data: bytes) -> None: ...
    async def subscribe(self, uuid: str, callback: Callable[[bytes], None]) -> None: ...


@dataclass
class FetchResult:
    kind: Kind
    start: FetchStart
    data: bytes


@dataclass
class Band:
    transport: Transport
    auth_key: bytes
    timeout: float = 20.0
    log: Log = lambda _m: None
    services: dict[int, bool] = field(default_factory=dict)
    info: DeviceInfo | None = None

    def __post_init__(self) -> None:
        self._encoder = Encoder(23)
        self._decoder = Decoder()
        self._messages: asyncio.Queue = asyncio.Queue()
        self._control: asyncio.Queue = asyncio.Queue()
        self._data: list[bytes] = []
        self._data_errors: list[str] = []
        self._expected_counter = 0
        self._activity_subscribed = False
        self._tasks: set[asyncio.Task] = set()

    # --- link -------------------------------------------------------------------------------------

    async def connect(self) -> None:
        await self.transport.connect()
        self._encoder.mtu = max(23, int(getattr(self.transport, "mtu", 23) or 23))
        await self.transport.subscribe(CH_CHUNKED_READ, self._on_chunk)
        try:
            await self.transport.subscribe(CH_CHUNKED_WRITE, lambda _data: None)  # the band's acks of our chunks
        except Exception:  # noqa: BLE001 - not every stack lets us watch the write characteristic
            pass

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.transport.disconnect()

    def _on_chunk(self, data: bytes) -> None:
        try:
            message = self._decoder.feed(bytes(data))
        except (ValueError, RuntimeError) as exc:
            self.log(f"  ! bad chunk ignored: {exc}")
            self._decoder.reset()
            return
        if self._decoder.last_handle is not None and (message is None or message.needs_ack) and _chunk_needs_ack(data):
            self._spawn(self.transport.write(CH_CHUNKED_READ, ack(self._decoder.last_handle, self._decoder.last_count)))
        if message is not None:
            self._messages.put_nowait((message.endpoint, message.payload))

    def _on_control(self, data: bytes) -> None:
        self._control.put_nowait(bytes(data))

    def _on_data(self, data: bytes) -> None:
        data = bytes(data)
        if not data:
            return
        if data[0] != self._expected_counter & 0xFF:
            self._data_errors.append(f"packet {data[0]} arrived, expected {self._expected_counter & 0xFF}")
        self._expected_counter = data[0] + 1
        self._data.append(data[1:])

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, endpoint: int, payload: bytes, *, encrypt: bool | None = None) -> None:
        if encrypt is None:
            encrypt = self.services.get(endpoint, endpoint == EP_FETCH)
        for chunk in self._encoder.encode(endpoint, payload, encrypt=encrypt):
            await self.transport.write(CH_CHUNKED_WRITE, chunk)

    async def _expect(self, endpoint: int, timeout: float | None = None) -> bytes:
        deadline = timeout or self.timeout
        while True:
            try:
                got, payload = await asyncio.wait_for(self._messages.get(), deadline)
            except TimeoutError as exc:
                raise BandError(f"no answer from the band on endpoint 0x{endpoint:04x} within {deadline:g} s") from exc
            if got == endpoint:
                return payload
            self.log(f"  (ignoring message on endpoint 0x{got:04x})")

    # --- session ----------------------------------------------------------------------------------

    async def authenticate(self) -> None:
        private = new_private()
        await self._send(EP_AUTH, protocol.auth_request(generate_public(private)), encrypt=False)
        challenge = protocol.parse_auth_challenge(await self._expect(EP_AUTH))
        shared = shared_secret(private, challenge.public_key)
        keys = protocol.session_keys(shared, self.auth_key)
        self._encoder.set_session(keys.key, keys.sequence)
        self._decoder.key = keys.key
        await self._send(EP_AUTH, protocol.auth_confirm(challenge.random, self.auth_key, keys.key), encrypt=False)
        try:
            protocol.parse_auth_result(await self._expect(EP_AUTH))
        except ProtocolError as exc:
            raise BandError(str(exc)) from exc

    async def request_services(self) -> dict[int, bool]:
        await self._send(EP_SERVICES, protocol.SERVICES_REQUEST, encrypt=False)
        self.services = protocol.parse_services(await self._expect(EP_SERVICES))
        return self.services

    async def set_time(self, now: datetime) -> None:
        await self._send(EP_TIME, protocol.set_time_payload(now))
        reply = await self._expect(EP_TIME)
        if reply[:1] != b"\x06":
            self.log(f"  time reply {reply.hex()}")

    async def device_info(self) -> DeviceInfo:
        await self._send(EP_DEVICE_INFO, protocol.DEVICE_INFO_REQUEST)
        self.info = protocol.parse_device_info(await self._expect(EP_DEVICE_INFO))
        return self.info

    # --- fetch ------------------------------------------------------------------------------------

    @property
    def _fetch_over_endpoint(self) -> bool:
        return EP_FETCH in self.services

    async def _control_write(self, payload: bytes) -> None:
        if self._fetch_over_endpoint:
            await self._send(EP_FETCH, payload)
        else:
            await self.transport.write(CH_ACTIVITY_CONTROL, payload)

    async def _control_read(self, timeout: float | None = None) -> bytes:
        if self._fetch_over_endpoint:
            return await self._expect(EP_FETCH, timeout)
        try:
            return await asyncio.wait_for(self._control.get(), timeout or self.timeout)
        except TimeoutError as exc:
            raise BandError("no fetch answer from the band") from exc

    async def _subscribe_activity(self) -> None:
        if not self._activity_subscribed:
            await self.transport.subscribe(CH_ACTIVITY_CONTROL, self._on_control)
            await self.transport.subscribe(CH_ACTIVITY_DATA, self._on_data)
            self._activity_subscribed = True

    async def fetch_count(self, kind: Kind, since: datetime) -> FetchStart:
        """Ask what the band holds since ``since`` without transferring it (the band keeps the data)."""
        await self._subscribe_activity()
        await self._control_write(protocol.fetch_since_payload(kind.code, since))
        start = protocol.parse_fetch_start(await self._control_read())
        await self._control_write(protocol.fetch_ack_payload(True))
        await self._control_read()
        return start

    async def fetch(self, kind: Kind, since: datetime, *, keep: bool = True) -> FetchResult | None:
        """Everything of ``kind`` since ``since``; None when the band has nothing new."""
        await self._subscribe_activity()
        await self._control_write(protocol.fetch_since_payload(kind.code, since))
        start = protocol.parse_fetch_start(await self._control_read())
        if start.count == 0:
            await self._control_write(protocol.fetch_ack_payload(True))
            await self._control_read()
            return None
        self._data, self._data_errors, self._expected_counter = [], [], 0
        await self._control_write(bytes([protocol.CMD_FETCH_DATA]))
        end = await self._control_read(self.timeout + start.count / 100)
        data = b"".join(self._data)
        if self._data_errors:
            raise BandError(f"{kind.name}: packet sequence broken ({self._data_errors[0]})")
        protocol.parse_fetch_end(end, data)
        await self._control_write(protocol.fetch_ack_payload(keep))
        await self._control_read()
        return FetchResult(kind, start, data)


def _chunk_needs_ack(chunk: bytes) -> bool:
    return len(chunk) > 1 and bool(chunk[1] & 0x04)
