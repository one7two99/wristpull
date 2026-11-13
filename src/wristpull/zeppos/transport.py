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


ADAPTER_HINT = (
    "if `wristpull strap scan` sees the band, the host's adapter may be stuck in a scan (`bluetoothctl show` says "
    "Discovering: yes): `bluetoothctl power off && bluetoothctl power on` resets it"
)


RFKILL_HINT = "the host's Bluetooth is switched off (`rfkill list bluetooth` says soft blocked, or the desktop's Bluetooth toggle): `rfkill unblock bluetooth`"


def ble_error(exc: Exception, what: str) -> BandError:
    """A BandError with a hint for the usual BlueZ failures (D-Bus denied, no adapter, a stuck adapter)."""
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if isinstance(exc, TimeoutError) or "timeout" in lowered:
        return BandError(f"{what}: timed out ({text}); {ADAPTER_HINT}")
    if "accessdenied" in lowered or "apparmor" in lowered or "not authorized" in lowered or "permission" in lowered:
        return BandError(f"{what}: {DBUS_HINT} ({text})")
    if "no bluetooth adapters" in lowered:
        return BandError(f"{what}: {RFKILL_HINT} ({text})")
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


async def _adapter_properties(adapter: str | None):
    """The ``org.freedesktop.DBus.Properties`` interface of the BlueZ adapter (dbus-fast, which bleak brings)."""
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus

    path = f"/org/bluez/{adapter or 'hci0'}"
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    introspection = await bus.introspect("org.bluez", path)
    proxy = bus.get_proxy_object("org.bluez", path, introspection)
    return bus, proxy.get_interface("org.freedesktop.DBus.Properties")


async def adapter_discovering(adapter: str | None = None) -> bool | None:
    """Is the adapter in a discovery nobody of ours started (the stuck state)? ``None`` when BlueZ cannot be asked."""
    try:
        bus, props = await _adapter_properties(adapter)
        try:
            return bool((await props.call_get("org.bluez.Adapter1", "Discovering")).value)
        finally:
            bus.disconnect()
    except Exception:  # noqa: BLE001 - no D-Bus, no adapter: then there is nothing to reset either
        return None


async def power_cycle_adapter(adapter: str | None = None, *, sleep=None) -> None:
    """``bluetoothctl power off && bluetoothctl power on`` over D-Bus: ends a discovery BlueZ got stuck in."""
    import asyncio

    from dbus_fast import Variant

    sleep = sleep or asyncio.sleep
    bus, props = await _adapter_properties(adapter)
    try:
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", False))
        await sleep(1.0)
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await sleep(2.0)
    finally:
        bus.disconnect()


class BleakTransport:
    """One connection to one band. ``connect`` scans for the address first so BlueZ knows the device; when
    the scan sees nothing while the adapter reports a discovery in progress, the adapter is power-cycled once
    (the stuck-scan state) and the scan repeated."""

    def __init__(self, address: str, *, adapter: str | None = None, timeout: float = 30.0, log: Callable[[str], None] | None = None,
                 discovering=None, reset=None, sleep=None) -> None:
        self.address = address
        self.adapter = adapter
        self.timeout = timeout
        self.log = log or (lambda _m: None)
        self._discovering = discovering or adapter_discovering
        self._reset = reset or power_cycle_adapter
        self._sleep = sleep
        self.mtu = 23
        self._client = None
        self.resets = 0

    async def _find(self, bleak, kwargs):
        try:
            return await bleak.BleakScanner.find_device_by_address(self.address, timeout=self.timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 - bleak raises backend-specific errors
            raise ble_error(exc, f"cannot look for {self.address}") from exc

    async def connect(self) -> None:
        client = await self.open_client()
        self._client = client
        self.mtu = int(getattr(client, "mtu_size", 23) or 23)

    async def open_client(self):
        """Scan, reset the adapter if it is stuck, connect; the connected bleak client (tests call this directly)."""
        bleak = _bleak()
        kwargs = {"adapter": self.adapter} if self.adapter else {}

        async def open_client():
            # Scan again on every attempt: BlueZ forgets an unbonded device shortly after a disconnect, so a
            # BLEDevice found a few seconds ago may already be gone ("device … not found").
            device = await self._find(bleak, kwargs)
            if device is None and self.resets == 0 and await self._discovering(self.adapter):
                self.log(f"  adapter stuck in a discovery: power-cycling it, then looking for {self.address} again")
                self.resets += 1
                try:
                    await self._reset(self.adapter)
                except Exception as exc:  # noqa: BLE001 - the reset is best effort; the hint below names the manual way
                    self.log(f"  adapter reset failed: {exc}")
                else:
                    device = await self._find(bleak, kwargs)
            if device is None:
                raise NoBandError(f"band {self.address} not found: out of range, asleep, or connected to the phone (disconnect it in Gadgetbridge); {ADAPTER_HINT}")
            client = bleak.BleakClient(device, timeout=self.timeout, **kwargs)
            await client.connect()
            return client

        try:
            return await attempts(open_client, sleep=self._sleep)
        except NoBandError:
            raise
        except Exception as exc:  # noqa: BLE001 - bleak raises backend-specific errors
            raise ble_error(exc, f"could not connect to {self.address}") from exc

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


def open_transport(address: str, *, adapter: str | None = None, log: Callable[[str], None] | None = None) -> BleakTransport:
    """Default transport factory; tests replace it (conftest blocks it altogether)."""
    return BleakTransport(address, adapter=adapter, log=log)
