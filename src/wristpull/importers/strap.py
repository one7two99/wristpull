"""Pull the Helio Strap over Bluetooth (source ``zepp_ble``) and import what it sends.

Per fetch kind the band is asked for everything since the last sample wristpull stored
(``strap_sync``), or ``[strap] since_days`` back on the first run. Activity buffers always start at
a local midnight because their step counts are cumulative per day. Each answer is archived as an
immutable container file (:mod:`wristpull.zeppos.fetch`), parsed into the sample tables and the
data stays on the band unless ``keep_on_band`` is off.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..archive import RawArchive
from ..config import Config
from ..daily import rebuild_daily_summary
from ..runtime import STRAP_AUTHKEY_ENV
from ..timeutil import iso_utc, zone
from ..zeppos import SOURCE
from ..zeppos.band import Band, BandError, Transport
from ..zeppos.fetch import KINDS, WORKOUT_DETAILS, Buffer, Kind, buffer_end, pack_buffer, pack_workout, parse_buffer
from ..zeppos.protocol import ProtocolError, parse_auth_key
from ..zeppos.store import delete_overlapping_sleep, last_sync, set_last_sync
from .garmin import store_parsed

Log = Callable[[str], None]
TransportFactory = Callable[[str], Transport]
WORKOUT_ROUNDS = 10  # workouts per pull: one summary + one details fetch each (Gadgetbridge's limit)


@dataclass
class StrapStats:
    device: str | None = None
    address: str | None = None
    fetched: dict[str, int] = field(default_factory=dict)  # kind -> samples/records received
    files_new: int = 0
    files_skipped: int = 0
    rows: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    messages: list[str] = field(default_factory=list)
    stopped: bool = False

    def add_rows(self, counts: dict[str, int]) -> None:
        for key, value in counts.items():
            self.rows[key] = self.rows.get(key, 0) + value


AUTHKEY_HINT = f"set {STRAP_AUTHKEY_ENV} (in .env for Docker, or the shell) to the band's 16-byte key; `wristpull strap pair --authkey <hex>` checks it against the band"


def has_auth_key(env: Mapping[str, str] | None = None) -> bool:
    """Whether ``WRISTPULL_STRAP_AUTHKEY`` is set (the only place the key lives)."""
    env = os.environ if env is None else env
    return bool(env.get(STRAP_AUTHKEY_ENV, "").strip())


def read_auth_key(env: Mapping[str, str] | None = None) -> bytes:
    """The key from ``WRISTPULL_STRAP_AUTHKEY`` (``.env`` for Docker, the shell for a native install)."""
    env = os.environ if env is None else env
    text = env.get(STRAP_AUTHKEY_ENV, "").strip()
    if not text:
        raise BandError(f"no auth key: {AUTHKEY_HINT}")
    return parse_auth_key(text)


def since_for(kind: Kind, last: datetime | None, now: datetime, *, since_days: int, tz) -> datetime:
    """Where a fetch of ``kind`` starts: one minute after the last sample, or ``since_days`` back.

    Activity goes back to the local midnight of the last sample's day: its step counts are
    cumulative per local day and the parser needs the whole day in one buffer."""
    if last is None:
        start = now - timedelta(days=since_days)
    elif kind.name == "workout":
        start = last  # stored as the moment after the last workout already
    else:
        start = last + timedelta(minutes=1)
    if kind.name == "activity":
        local = (last or start).astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return start


def import_buffer(conn: sqlite3.Connection, config: Config, buffer: Buffer, *, origin: str, fetched_at: datetime, log: Log = lambda _m: None) -> tuple[str, dict[str, int]]:
    """Archive + parse one fetched buffer. Returns ('new'|'skipped'|'error', inserted counts)."""
    archive = RawArchive(config.raw_dir, conn)
    blob = pack_buffer(buffer)
    stored = archive.store(blob, source=SOURCE, kind=buffer.kind.raw_kind, original_name=buffer.file_name(), origin_path=origin, device_mtime=fetched_at, year=buffer.start_utc.year)
    if not stored.is_new:
        return "skipped", {}
    conn.commit()  # the archived file is a fact even when its parser fails below
    tz = zone(config.timezone)
    try:
        parsed = parse_buffer(buffer, tz, SOURCE)
        for night in parsed.sleep:
            delete_overlapping_sleep(conn, SOURCE, night.row["start_utc"], night.row["end_utc"])
        counts = store_parsed(conn, parsed, stored.raw_file_id)
        archive.mark_parsed(stored.raw_file_id, status="ok", fit_file_type=buffer.kind.raw_kind)
    except Exception as exc:  # noqa: BLE001 - keep the file, report the parser problem
        conn.rollback()
        archive.mark_parsed(stored.raw_file_id, status="error", error=str(exc)[:500])
        conn.commit()
        log(f"  ! {buffer.file_name()}: {exc}")
        return "error", {}
    conn.commit()
    return "new", counts


async def _pull(conn: sqlite3.Connection, config: Config, band: Band, stats: StrapStats, *, kinds: tuple[Kind, ...], dry_run: bool, keep: bool, since_days: int, log: Log, stop: Callable[[], bool] | None, clock: Callable[[], datetime]) -> None:
    tz = zone(config.timezone)
    await band.connect()
    try:
        await band.authenticate()
        await band.request_services()
        now = clock()
        await band.set_time(now.astimezone(tz))
        info = await band.device_info()
        stats.device = info.label()
        log(f"  band: {info.label()}")
        for kind in kinds:
            if stop is not None and stop():
                stats.stopped = True
                log("  stopped on request")
                return
            last = last_sync(conn, SOURCE, kind.name)
            since = since_for(kind, last, now, since_days=since_days, tz=tz)
            try:
                if dry_run:
                    start = await band.fetch_count(kind, since.astimezone(tz))
                    stats.fetched[kind.name] = start.count
                    log(f"  {kind.name}: {start.count} since {iso_utc(since)}" + (f" (band starts at {iso_utc(start.start_utc)})" if start.count else ""))
                    continue
                if kind.name == "workout":
                    await _pull_workouts(conn, config, band, stats, kind, since, tz=tz, keep=keep, now=now, log=log, stop=stop)
                    continue
                result = await band.fetch(kind, since.astimezone(tz), keep=keep)
            except ProtocolError as exc:
                stats.errors += 1
                stats.messages.append(f"{kind.name}: {exc}")
                log(f"  ! {kind.name}: {exc}")
                continue
            if result is None:
                stats.fetched[kind.name] = 0
                log(f"  {kind.name}: nothing new since {iso_utc(since)}")
                continue
            buffer = Buffer(kind, result.start.start_utc, result.start.tz_quarters, result.data)
            stats.fetched[kind.name] = buffer.samples
            state, counts = import_buffer(conn, config, buffer, origin=f"ble://{stats.address}/{kind.name}", fetched_at=now, log=log)
            if state == "new":
                stats.files_new += 1
                stats.add_rows(counts)
            elif state == "skipped":
                stats.files_skipped += 1
            else:
                stats.errors += 1
            # opaque kinds carry no timestamps we parse: continue from the fetch time, not from the buffer start
            set_last_sync(conn, SOURCE, kind.name, buffer_end(buffer) if kind.record else now)
            conn.commit()
            unit = "samples" if kind.record else "bytes"
            log(f"  {kind.name}: {buffer.samples} {unit} from {iso_utc(buffer.start_utc)} ({state}" + (", " + ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "") + ")")
    finally:
        await band.close()


async def _pull_workouts(conn: sqlite3.Connection, config: Config, band: Band, stats: StrapStats, kind: Kind, since: datetime, *, tz, keep: bool, now: datetime, log: Log, stop: Callable[[], bool] | None) -> None:
    """The band hands out one workout per request: its summary (0x05) since ``since``, then the details
    (0x06) for that start time; the next request continues after the workout's end."""
    fetched = 0
    for _round in range(WORKOUT_ROUNDS):
        if stop is not None and stop():
            stats.stopped = True
            return
        result = await band.fetch(kind, since.astimezone(tz), keep=keep)
        if result is None:
            break
        start = result.start.start_utc
        details = await band.fetch(WORKOUT_DETAILS, start.astimezone(tz), keep=keep)
        buffer = Buffer(kind, start, result.start.tz_quarters, pack_workout(result.data, details.data if details else b""))
        try:
            end = buffer_end(buffer)
        except ValueError as exc:
            stats.errors += 1
            stats.messages.append(f"workout at {iso_utc(start)}: {exc}")
            log(f"  ! workout at {iso_utc(start)}: {exc}")
            return
        fetched += 1
        state, counts = import_buffer(conn, config, buffer, origin=f"ble://{stats.address}/workout", fetched_at=now, log=log)
        if state == "new":
            stats.files_new += 1
            stats.add_rows(counts)
        elif state == "skipped":
            stats.files_skipped += 1
        else:
            stats.errors += 1
        log(f"  workout from {iso_utc(start)} to {iso_utc(end)} ({state}" + (", " + ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "") + ")")
        # the next request must start after this workout; a workout that ends in its start minute would come back forever
        since = max(end, start + timedelta(minutes=1))
        set_last_sync(conn, SOURCE, kind.name, since)
        conn.commit()
    stats.fetched[kind.name] = fetched
    if fetched == 0:
        log(f"  workout: nothing new since {iso_utc(since)}")


def pull_strap(
    conn: sqlite3.Connection,
    config: Config,
    *,
    transport_factory: TransportFactory | None = None,
    kinds: tuple[Kind, ...] = KINDS,
    dry_run: bool = False,
    keep: bool | None = None,
    since_days: int | None = None,
    log: Log = lambda _m: None,
    stop: Callable[[], bool] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> StrapStats:
    """Connect to the configured band, fetch every kind since its last sync, archive and import.

    ``transport_factory`` (address -> Transport) defaults to bleak; tests inject a fake band.
    Raises :class:`BandError` when the band cannot be reached or rejects the key."""
    stats = StrapStats(address=config.strap_address or None)
    if not config.strap_address:
        raise BandError("no band configured: set [strap] address (wristpull strap scan shows the candidates)")
    auth_key = read_auth_key()
    if transport_factory is None:
        from ..zeppos.transport import open_transport

        transport_factory = lambda address: open_transport(address, adapter=config.strap_adapter or None)  # noqa: E731
    band = Band(transport_factory(config.strap_address), auth_key, log=log)
    asyncio.run(
        _pull(
            conn,
            config,
            band,
            stats,
            kinds=kinds,
            dry_run=dry_run,
            keep=config.strap_keep_on_band if keep is None else keep,
            since_days=config.strap_since_days if since_days is None else since_days,
            log=log,
            stop=stop,
            clock=clock,
        )
    )
    if stats.rows and not dry_run:
        rebuild_daily_summary(conn, source=SOURCE)
        conn.commit()
    return stats
