# The Helio Strap over Bluetooth (source `zepp_ble`)

wristpull reads the Amazfit Helio Strap directly over Bluetooth Low Energy, without Gadgetbridge
and without a phone. The German planning document with the full reasoning is
`history/Helio-Strap-BLE-Konzept.md`; this page keeps the protocol facts, the spike results and
the design that was built on them. Status: phases 1 and 2 built 2026-11-07 (library, `pull strap`, TUI,
status, archive, reparse, workouts, PAI, maximum heart rate); phase 3 (the Raspberry Pi box) is open.

## Protocol (Zepp OS, as Gadgetbridge implements it)

| Layer | Facts |
|---|---|
| GATT | service `FEE0`; chunked write `0016`, chunked read `0017` (notify); fetch control `0004` (fallback), fetch data `0005` (notify, plaintext); Device Information `180A`. No OS-level bonding. |
| Framing | `0x03, flags, 0x00, handle, count [, length u32, endpoint u16]`; flags 0x01 first, 0x02 last, 0x04 ack wanted, 0x08 encrypted. Encrypted body = payload + sequence u32 + crc32 u32, zero-padded, AES-128-ECB with `session_key XOR handle`. Acks: `04 00 handle 01 count` on the characteristic one *reads* from. |
| Authentication | endpoint `0x0082`: `04 02 00 02` + our B-163 public key (48 bytes); band: `10 04 01` + random(16) + its key; `session_key = shared[8:24] XOR auth_key`, sequence = `shared[0:4]`; we send `05` + AES(random, auth_key) + AES(random, session_key); band: `10 05 01`, or `10 05 25` for a wrong key. |
| Services | endpoint `0x0000`, `03` → `04 n (endpoint u16, encrypted u8)×n`. On the strap endpoints `000a 000c 0017 0018 0019 001a 0030 0036 004d 0081` are encrypted; the fetch endpoint `004b` is not. |
| Time, info | `0x0047`: `05` + 11-byte local time block (year u16, month, day, hour, minute, second, weekday 0–6, fractions256, DST flag 0x08, offset in quarter hours). `0x0043`: `01` → `02 01` + binary + NUL-separated strings serial, hardware, firmware, … |
| Fetch | `0x004b`: `01 kind <local time 7 bytes + tz quarters>` → `10 01 01 count u32 <local start time + tz>`; `02` → packets `[counter][data]` on `0005`, then `10 02 01 crc32`; `03 09` keeps the data on the band (`03 01` lets it delete). The start time is the band's local time **with the offset byte of that moment**: the first stress buffer came with +2 h although the host was on CET. |

Fetch kinds and record layouts (verified byte-identical against Gadgetbridge's tables on 2026-11-07):

| Kind | Code | Record | wristpull table |
|---|---|---|---|
| activity | 0x01 | 8 B/min: kind, intensity, steps, hr, ?, sleep, deep, rem | `heart_rate`, `steps` (cumulative per local day) |
| stress | 0x13 | 1 B/min, 0xff = none | `stress` |
| spo2 | 0x25 | version byte 2, then 65 B: ts u32, value (bit 7 = automatic), 60 B | `spo2` |
| hrv | 0x49 | 6 B: ts, ?, value | `hrv_values` |
| resting_hr | 0x3a | 6 B: ts, tz, bpm | `resting_hr` |
| respiration | 0x38 | 8 B: ts, tz, rate, ?, ? | `respiration` |
| sleep | 0x48 | 594-B session reports (`gadgetbridge.decode_sleep_session`) | `sleep`, `sleep_stages` |
| hr_max | 0x3d | 6 B: ts, tz, bpm | `metrics` (`hr_max`) |
| pai | 0x0d | 102 B: type (5 = current), ts, tz, 31 ?, pai low/moderate/high f32, minutes ×3 i16, pai today f32, pai total f32, 39 ? | `metrics` (`pai_total`, `pai_today`) |
| workout | 0x05 + 0x06 | summary: version word 0x8000 + protobuf `WorkoutSummary` (huami.proto); details: BER-TLV track (timestamp, offsets, heart rate, speed, distance, altitude, GPS, laps, strength sets) | `activities`, `activity_records`, `activity_laps`, `vo2_max` metric |

## Spike results (2026-11-07, laptop with Intel 9460/9560, BlueZ 5.85, bleak)

- Pure-Python ECDH on B-163 (`zeppos/ecdh.py`, from tiny-ECDH-c) and AES-128 (`zeppos/aes.py`) were accepted by the band at the first attempt; `n·G = O` holds.
- Authentication, service list, time, device info (serial, hardware 0.132.24.2, firmware 3.25.0.3) in about one second.
- One session fetched every kind: 10 165 minutes of stress (the band's whole history since 2026-10-31), 2 880 activity minutes, 472 SpO2, 2 005 HRV, 1 440 respiration, 7 sleep sessions; 30 KB in under a second.
- Every overlapping value matched Gadgetbridge's export (742/742 stress, 1571/1571 activity minutes, 468/468 SpO2, 2005/2005 HRV, 6/6 sleep sessions) once the offset byte of the fetch reply was applied.
- The band still held data Gadgetbridge had fetched days earlier: deletion after a fetch is not what happens by default, so "keep on band" costs nothing.
- Only one host at a time: while Gadgetbridge is connected the band does not advertise.

## Design

- `src/wristpull/zeppos/`: `ecdh`, `aes`, `chunked` (codec), `protocol` (pure builders/parsers), `band` (the async client over a `Transport` protocol), `transport` (bleak; the only Bluetooth code), `fetch` (kinds, container file, parsers reusing `gadgetbridge.py`), `store` (SQL: `strap_sync`, replaced nights).
- `importers/strap.py`: `pull_strap` connects, authenticates, sets the time, asks every kind for data since `strap_sync` (first run: `[strap] since_days`), archives each answer as an immutable `WPZB` container under `raw/zepp_ble/<year>/zepp_<kind>/`, parses it and rebuilds the daily summaries. Activity is always asked from the local midnight of the last synced day because its step counts are cumulative per day (`replace_tables`). A newer sleep report replaces an older one that overlaps.
- Merge: `STRAP_PRIORITY = ("zepp_ble", "gadgetbridge_zepp")`, de-duplicated like the Garmin sources; the strap stays a separate device from the watch.
- The auth key comes from the environment (`WRISTPULL_STRAP_AUTHKEY`, in `.env` for Docker, like the coach API keys, exported in the shell for a native install). Never in the config, a file or the repository. `wristpull strap pair` checks it against the band and writes the address, `strap scan` finds the band, `strap info` shows where the key comes from and the sync state.
- Tests: `tests/fake_band.py` plays the band behind the same codec (ECDH + AES included); conftest blocks bleak. Buffers are synthetic (`tests/zepp_buffers.py`).

## Open

- Workouts: the band hands out one per request (summary since X, details for that start, next request after the end; ten per pull). The Helio Strap's workouts are all automatically detected (type code 0xdf, unknown to Gadgetbridge too), without distance; verified 2026-11-07: twelve summaries byte-identical to Gadgetbridge's blobs, details tracks one point per second. Swimming intervals are parsed but not stored as rows yet.
- Docker: Docker's default AppArmor profile denies the D-Bus calls (seen 2026-11-07 on Ubuntu 26.04), so `cli` runs under `contrib/apparmor/wristpull-cli` (docker-default plus `dbus (send, receive) bus=system`), installed once on the host.
- The box: Raspberry Pi image, scan loop, auth key provisioning for other users (needs a Zepp account once per band), licence question (Gadgetbridge is AGPL).
