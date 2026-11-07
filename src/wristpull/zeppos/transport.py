"""BLE transport on top of bleak (BlueZ on Linux). The only module here that touches Bluetooth."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .band import BandError, NoBandError
from .protocol import SERVICE_MAIN

BLE_HINT = "Bluetooth support needs the 'ble' extra: uv sync --extra ble (or the Docker image, which has it)"
DBUS_HINT = (
    "no access to the system D-Bus / BlueZ: on the host add your user to the `bluetooth` group; inside Docker the "
    "default AppArmor profile blocks D-Bus, run `pull strap` natively or see README \"Running everything in Docker\""
)


def ble_error(exc: Exception, what: str) -> BandError:
    """A BandError with a hint for the usual BlueZ failures (D-Bus denied, no adapter)."""
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "accessdenied" in lowered or "apparmor" in lowered or "not authorized" in lowered or "permission" in lowered:
        return BandError(f"{what}: {DBUS_HINT} ({text})")
    if "no powered bluetooth" in lowered or ("adapter" in lowered and "not found" in lowered):
        return BandError(f"{what}: no powered Bluetooth adapter ({text})")
    return BandError(f"{what}: {text}")
STRAP_NAME = "Amazfit Helio Strap"


def _bleak():
    try:
        import bleak
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BandError(BLE_HINT) from exc
    return bleak


@dataclass(frozen=True)
class Found:
    address: str
    name: str | None
    rssi: int | None


async def scan(seconds: float = 8.0, *, adapter: str | None = None, all_devices: bool = False) -> list[Found]:
    """Advertising Zepp OS bands (main service FEE0 or an Amazfit name), strongest signal first."""
    bleak = _bleak()
    found: dict[str, Found] = {}

    def seen(device, adv) -> None:
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name or device.name
        if all_devices or SERVICE_MAIN in uuids or (name and "amazfit" in name.lower()):
            found[device.address] = Found(device.address, name, adv.rssi)

    kwargs = {"adapter": adapter} if adapter else {}
    try:
        async with bleak.BleakScanner(detection_callback=seen, **kwargs):
            import asyncio

            await asyncio.sleep(seconds)
    except BandError:
        raise
    except Exception as exc:  # noqa: BLE001 - bleak raises backend-specific errors
        raise ble_error(exc, "scan failed") from exc
    return sorted(found.values(), key=lambda f: -(f.rssi if f.rssi is not None else -999))


async def attempts(action, *, tries: int = 3, delay: float = 2.0, sleep=None) -> object:
    """Run ``action()`` (a coroutine factory) up to ``tries`` times; BlueZ often refuses a connection that
    follows a disconnect by a second or two. Raises the last error."""
    import asyncio

    sleep = sleep or asyncio.sleep
    last: Exception | None = None
    for attempt in range(tries):
        try:
            return await action()
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last = exc
            if attempt + 1 < tries:
                await sleep(delay)
    assert last is not None
    raise last


class BleakTransport:
    """One connection to one band. ``connect`` scans for the address first so BlueZ knows the device."""

    def __init__(self, address: str, *, adapter: str | None = None, timeout: float = 30.0) -> None:
        self.address = address
        self.adapter = adapter
        self.timeout = timeout
        self.mtu = 23
        self._client = None

    async def connect(self) -> None:
        bleak = _bleak()
        kwargs = {"adapter": self.adapter} if self.adapter else {}
        try:
            device = await bleak.BleakScanner.find_device_by_address(self.address, timeout=self.timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 - bleak raises backend-specific errors
            raise ble_error(exc, f"cannot look for {self.address}") from exc
        if device is None:
            raise NoBandError(f"band {self.address} not found: out of range, asleep, or connected to the phone (disconnect it in Gadgetbridge)")

        async def open_client():
            client = bleak.BleakClient(device, timeout=self.timeout, **kwargs)
            await client.connect()
            return client

        try:
            client = await attempts(open_client)
        except Exception as exc:  # noqa: BLE001 - bleak raises backend-specific errors
            raise ble_error(exc, f"could not connect to {self.address}") from exc
        self._client = client
        self.mtu = int(getattr(client, "mtu_size", 23) or 23)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    async def write(self, uuid: str, data: bytes) -> None:
        if self._client is None:
            raise BandError("not connected")
        await self._client.write_gatt_char(uuid, data, response=False)

    async def subscribe(self, uuid: str, callback: Callable[[bytes], None]) -> None:
        if self._client is None:
            raise BandError("not connected")
        await self._client.start_notify(uuid, lambda _char, data: callback(bytes(data)))


def open_transport(address: str, *, adapter: str | None = None) -> BleakTransport:
    """Default transport factory; tests replace it (conftest blocks it altogether)."""
    return BleakTransport(address, adapter=adapter)
