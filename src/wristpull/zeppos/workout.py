"""Zepp OS workouts: the protobuf summary (fetch kind 0x05) and the BER-TLV details track (0x06).

Layouts from Gadgetbridge's ``ZeppOsActivitySummaryParser`` (``huami.proto``, message
``WorkoutSummary``) and ``ZeppOsActivityDetailsParser``. The Helio Strap reports automatically
recognised workouts with type code 0xdf and the ``ai`` flag; Gadgetbridge does not know that code
either, here it is ``auto_detected``.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ..gadgetbridge import is_accidental_workout
from ..rows import ParsedActivity, ParsedRows
from ..timeutil import iso_utc, local_date
from . import protobuf as pb

SUMMARY_VERSION = 0x8000
AUTO_DETECTED_CODE = 0xDF

# ZeppOsActivityType codes -> wristpull sport names (Garmin-like names where both devices record the same sport)
SPORTS: dict[int, str] = {
    0x01: "running", 0x02: "treadmill", 0x03: "walking", 0x04: "cycling", 0x05: "free_training", 0x06: "pool_swimming",
    0x07: "open_water_swimming", 0x08: "indoor_cycling", 0x09: "elliptical", 0x0A: "cycling", 0x0F: "hiking", 0x11: "tennis",
    0x15: "jump_rope", 0x17: "rowing", 0x18: "indoor_fitness", 0x28: "indoor_walking", 0x29: "curling", 0x2C: "ice_skating",
    0x2D: "indoor_ice_skating", 0x30: "bmx", 0x31: "hiit", 0x32: "core_training", 0x33: "aerobic_combo", 0x34: "strength_training",
    0x35: "stretching", 0x36: "stair_climber", 0x37: "flexibility", 0x39: "stepper", 0x3B: "gymnastics", 0x3C: "yoga", 0x3D: "pilates",
    0x40: "fishing", 0x41: "sailing", 0x42: "rowing", 0x43: "skateboarding", 0x45: "roller_skating", 0x46: "rock_climbing",
    0x47: "ballet", 0x48: "belly_dance", 0x49: "square_dance", 0x4A: "street_dance", 0x4B: "ballroom_dance", 0x4C: "dance",
    0x4D: "zumba", 0x4E: "cricket", 0x4F: "baseball", 0x50: "bowling", 0x51: "squash", 0x55: "basketball", 0x56: "softball",
    0x57: "gateball", 0x58: "volleyball", 0x59: "table_tennis", 0x5B: "handball", 0x5C: "badminton", 0x5D: "archery",
    0x5E: "horse_riding", 0x5F: "kendo", 0x60: "karate", 0x61: "boxing", 0x62: "judo", 0x63: "wrestling", 0x64: "tai_chi",
    0x65: "muay_thai", 0x66: "taekwondo", 0x67: "martial_arts", 0x68: "kickboxing", 0x6D: "aerobics", 0x6F: "mass_gymnastics",
    0x70: "latin_dance", 0x71: "jazz_dance", 0x72: "cardio_combat", 0x73: "hula_hoop", 0x74: "frisbee", 0x75: "darts",
    0x76: "kite_flying", 0x77: "tug_of_war", 0x7A: "beach_volleyball", 0x81: "parkour", 0x82: "cross_training",
    0x83: "race_walking", 0x84: "driving", 0x8A: "dragon_boat", 0x8C: "kayaking", 0x8F: "spinning", 0x90: "air_walker",
    0x91: "wall_ball", 0x92: "folk_dance", 0x93: "jujitsu", 0x94: "fencing", 0x95: "horizontal_bar", 0x96: "parallel_bars",
    0x97: "billiards", 0x98: "sepak_takraw", 0x99: "dodgeball", 0x9A: "water_polo", 0x9B: "finswimming", 0x9C: "artistic_swimming",
    0x9D: "snorkeling", 0x9E: "ice_hockey", 0x9F: "swing", 0xA0: "shuffleboard", 0xA1: "table_football", 0xA2: "shuttlecock",
    0xA3: "somatosensory_game", 0xA4: "futsal", 0xA5: "hip_hop", 0xA6: "pole_dance", 0xA7: "battle_rope", 0xA8: "breaking",
    0xA9: "hacky_sack", 0xAA: "bocce", 0xAB: "jai_alai", 0xAC: "flowriding", 0xAD: "chess", 0xAE: "checkers", 0xAF: "weiqi",
    0xB0: "bridge", 0xB1: "board_game", 0xB8: "bouldering", 0xB9: "modern_dance", 0xBC: "floorball", 0xBD: "esports",
    0xBF: "soccer", 0xCE: "e_bike", AUTO_DETECTED_CODE: "auto_detected",
}
SWIM_CODES = {0x06, 0x07, 0x9B, 0x9C}


def sport_name(code: int | None) -> str | None:
    if code is None:
        return None
    return SPORTS.get(code, f"zepp_{code:#04x}")


@dataclass
class WorkoutSummary:
    version: str | None = None
    type_code: int | None = None
    auto: bool = False
    start_utc: datetime | None = None
    total_duration_s: int | None = None
    workout_duration_s: int | None = None
    pause_s: int | None = None
    distance_m: float | None = None
    calories: int | None = None
    hr_avg: int | None = None
    hr_max: int | None = None
    hr_min: int | None = None
    steps: int | None = None
    cadence_avg: float | None = None  # per minute
    cadence_max: float | None = None
    stride_cm: int | None = None
    pace_avg: float | None = None  # seconds per metre
    pace_best: float | None = None
    altitude_max_m: float | None = None
    altitude_min_m: float | None = None
    altitude_avg_m: float | None = None
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    workout_load: int | None = None
    vo2_max: int | None = None
    hr_zone_bounds: list[int] = field(default_factory=list)
    hr_zone_times_s: list[int] = field(default_factory=list)
    temperature_avg: float | None = None
    base_latitude: float | None = None
    base_longitude: float | None = None

    @property
    def sport(self) -> str | None:
        return sport_name(self.type_code)

    def end_utc(self, start: datetime) -> datetime:
        return start + timedelta(seconds=self.total_duration_s or 0)


def parse_summary(data: bytes) -> WorkoutSummary:
    if len(data) < 2:
        raise ValueError("workout summary too short")
    version = struct.unpack_from("<H", data)[0]
    if version != SUMMARY_VERSION:
        raise ValueError(f"unknown workout summary version 0x{version:04x}")
    top = pb.decode(data[2:])
    s = WorkoutSummary(version=pb.as_str(pb.first(top, 1)))
    if (loc := pb.submessage(top, 2)) is not None:
        if (ts := pb.first(loc, 1)) is not None:
            s.start_utc = datetime.fromtimestamp(int(ts), UTC)
        lat, lon = pb.as_int(pb.first(loc, 5)), pb.as_int(pb.first(loc, 6))
        if lat and lon:
            s.base_latitude, s.base_longitude = lat / 6_000_000, lon / -6_000_000
    if (typ := pb.submessage(top, 3)) is not None:
        s.type_code = pb.as_int(pb.first(typ, 1))
        s.auto = pb.as_int(pb.first(typ, 2)) == 1
    if (dist := pb.submessage(top, 4)) is not None:
        s.distance_m = pb.as_float(pb.first(dist, 1))
    if (tm := pb.submessage(top, 7)) is not None:
        s.total_duration_s = pb.as_int(pb.first(tm, 1))
        s.workout_duration_s = pb.as_int(pb.first(tm, 2))
        s.pause_s = pb.as_int(pb.first(tm, 3))
    if (pace := pb.submessage(top, 10)) is not None:
        s.pace_avg, s.pace_best = pb.as_float(pb.first(pace, 1)), pb.as_float(pb.first(pace, 2))
    if (steps := pb.submessage(top, 11)) is not None:
        avg, mx = pb.as_float(pb.first(steps, 1)), pb.as_float(pb.first(steps, 2))
        s.cadence_avg = avg * 60 if avg is not None else None
        s.cadence_max = mx * 60 if mx is not None else None
        s.stride_cm = pb.as_int(pb.first(steps, 3))
        s.steps = pb.as_int(pb.first(steps, 4))
    if (alt := pb.submessage(top, 13)) is not None:
        for attr, num, scale in (("altitude_max_m", 1, 200), ("altitude_min_m", 2, 200), ("altitude_avg_m", 3, 200), ("elevation_gain_m", 5, 100), ("elevation_loss_m", 6, 100)):
            value = pb.as_int(pb.first(alt, num))
            setattr(s, attr, value / scale if value is not None else None)
    if (cal := pb.submessage(top, 16)) is not None:
        s.calories = pb.as_int(pb.first(cal, 1))
    if (freq := pb.submessage(top, 18)) is not None:
        avg, mx = pb.as_int(pb.first(freq, 1)), pb.as_int(pb.first(freq, 2))
        s.cadence_avg = float(avg) if avg else s.cadence_avg
        s.cadence_max = float(mx) if mx else s.cadence_max
    if (hr := pb.submessage(top, 19)) is not None:
        s.hr_avg, s.hr_max, s.hr_min = pb.as_int(pb.first(hr, 1)), pb.as_int(pb.first(hr, 2)), pb.as_int(pb.first(hr, 3))
    if (temp := pb.submessage(top, 20)) is not None:
        s.temperature_avg = pb.as_float(pb.first(temp, 1))
    if (te := pb.submessage(top, 21)) is not None:
        s.aerobic_te, s.anaerobic_te = pb.as_float(pb.first(te, 4)), pb.as_float(pb.first(te, 5))
        s.workout_load, s.vo2_max = pb.as_int(pb.first(te, 6)), pb.as_int(pb.first(te, 7))
    if (zones := pb.submessage(top, 22)) is not None:
        s.hr_zone_bounds = pb.packed_ints(pb.first(zones, 2))
        s.hr_zone_times_s = pb.packed_ints(pb.first(zones, 3))
    return s


# --- details -----------------------------------------------------------------------------------------

T_TIMESTAMP, T_GPS, T_GPS_DELTA, T_STATUS, T_SPEED, T_ALTITUDE, T_HEARTRATE, T_LAP, T_TEMPERATURE, T_STRENGTH_SET, T_SWIM_INTERVAL, T_DISTANCE = 1, 2, 3, 4, 5, 7, 8, 11, 13, 15, 20, 0x1F09
LENGTHS = {T_TIMESTAMP: (12,), T_GPS: (20, 28), T_GPS_DELTA: (8, 16), T_STATUS: (4,), T_SPEED: (8,), T_ALTITUDE: (6, 7), T_HEARTRATE: (3,), T_LAP: (99,), T_TEMPERATURE: (7,), T_STRENGTH_SET: (34,), T_SWIM_INTERVAL: (31,), T_DISTANCE: (6,)}
STATUS_NAMES = {1: "start", 4: "pause", 5: "resume", 6: "stop"}


@dataclass
class TrackPoint:
    time_ms: int
    hr: int | None = None
    cadence: int | None = None
    speed_mps: float | None = None
    distance_m: float | None = None
    altitude_m: float | None = None
    lat: float | None = None
    lon: float | None = None
    temperature_c: float | None = None


@dataclass(frozen=True)
class Lap:
    number: int
    hr: int
    pace_s_per_km: int
    calories: int
    distance_m: int
    duration_ms: int


@dataclass(frozen=True)
class StrengthSet:
    reps: int
    weight_kg: float | None


@dataclass
class Details:
    points: list[TrackPoint] = field(default_factory=list)
    laps: list[Lap] = field(default_factory=list)
    sets: list[StrengthSet] = field(default_factory=list)
    statuses: list[tuple[int, str]] = field(default_factory=list)  # (time_ms, name)
    unknown_tags: dict[int, int] = field(default_factory=dict)


def _tag(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    pos += 1
    if first & 0x1F != 0x1F:
        return first, pos
    tag = first
    while pos < len(data):
        b = data[pos]
        pos += 1
        tag = (tag << 8) | b
        if not b & 0x80:
            break
    return tag, pos


def _length(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    pos += 1
    if not first & 0x80:
        return first, pos
    n = first & 0x7F
    if n == 0 or n > 4:
        raise ValueError(f"unsupported BER length encoding {n}")
    value = int.from_bytes(data[pos : pos + n], "big")
    return value, pos + n


def _degrees(raw: int) -> float:
    return raw / 3_000_000


class _Track:
    """Builds the point list the way Gadgetbridge does: one point per second, later values win."""

    def __init__(self) -> None:
        self.details = Details()
        self.timestamp = 0
        self.offset = 0
        self.longitude = 0
        self.latitude = 0
        self.current = TrackPoint(0)

    def flush(self) -> None:
        if self.timestamp == 0:
            return
        t = round((self.timestamp + self.offset) / 1000) * 1000
        point = TrackPoint(t, self.current.hr, self.current.cadence, self.current.speed_mps, self.current.distance_m, self.current.altitude_m, _degrees(self.latitude) if self.latitude else None, _degrees(self.longitude) if self.longitude else None, self.current.temperature_c)
        if self.details.points and self.details.points[-1].time_ms == t:
            self.details.points[-1] = point
        else:
            self.details.points.append(point)

    def set_offset(self, raw: bytes) -> None:
        self.flush()
        self.offset = struct.unpack_from("<h", raw)[0]


def parse_details(data: bytes) -> Details:
    track = _Track()
    d = track.details
    pos = 0
    while pos < len(data):
        tag, pos = _tag(data, pos)
        length, pos = _length(data, pos)
        payload = data[pos : pos + length]
        pos += length
        if len(payload) != length:
            raise ValueError("truncated workout details")
        allowed = LENGTHS.get(tag)
        if allowed is None:
            d.unknown_tags[tag] = d.unknown_tags.get(tag, 0) + 1
            continue
        if length not in allowed:
            d.unknown_tags[tag] = d.unknown_tags.get(tag, 0) + 1
            continue
        if tag == T_TIMESTAMP:
            track.flush()
            track.timestamp = struct.unpack_from("<q", payload, 4)[0]
            track.offset = 0
        elif tag == T_GPS:
            track.longitude, track.latitude = struct.unpack_from("<ii", payload, 6)
        elif tag == T_GPS_DELTA:
            track.set_offset(payload)
            dlon, dlat = struct.unpack_from("<hh", payload, 2)
            if track.longitude or track.latitude:
                track.longitude += dlon
                track.latitude += dlat
        elif tag == T_STATUS:
            track.set_offset(payload)
            code = struct.unpack_from("<h", payload, 2)[0]
            d.statuses.append((track.timestamp + track.offset, STATUS_NAMES.get(code, f"unknown_{code}")))
            if code == 6:
                track.flush()
        elif tag == T_SPEED:
            track.set_offset(payload)
            cadence, _stride, pace = struct.unpack_from("<hhh", payload, 2)
            track.current.cadence = cadence
            if pace:
                track.current.speed_mps = 1000 / pace
        elif tag == T_DISTANCE:
            track.set_offset(payload)
            track.current.distance_m = struct.unpack_from("<I", payload, 2)[0] / 100
        elif tag == T_ALTITUDE:
            track.set_offset(payload)
            raw = struct.unpack_from("<i", payload, 2)[0]
            if length == 7:
                if raw != -1 and payload[6] != 0xFF:
                    track.current.altitude_m = raw / 100_000
            elif raw != -1:
                track.current.altitude_m = raw / 100
        elif tag == T_HEARTRATE:
            track.set_offset(payload)
            track.current.hr = payload[2]
        elif tag == T_TEMPERATURE:
            track.set_offset(payload)
            track.current.temperature_c = struct.unpack_from("<f", payload, 2)[0]
        elif tag == T_STRENGTH_SET:
            reps = struct.unpack_from("<H", payload, 15)[0]
            weight = struct.unpack_from("<H", payload, 18)[0]
            d.sets.append(StrengthSet(reps, weight / 10 if weight != 0xFFFF else None))
        elif tag == T_LAP:
            number = struct.unpack_from("<H", payload, 2)[0]
            hr = payload[5]
            pace, calories, distance = struct.unpack_from("<HHH", payload, 6)
            duration = struct.unpack_from("<i", payload, 16)[0]
            d.laps.append(Lap(number, hr, pace, calories, distance, duration))
        elif tag == T_SWIM_INTERVAL:
            track.set_offset(payload)  # intervals are not kept as rows yet (lengths are rare on a strap)
    track.flush()
    return d


# --- rows ---------------------------------------------------------------------------------------------


def workout_rows(summary: WorkoutSummary, details: Details | None, start: datetime, tz: ZoneInfo, source: str, device_serial: str | None, out: ParsedRows) -> ParsedActivity | None:
    """Append the activity (+ records, laps) and a VO2max metric to ``out``; None for an accidental start."""
    start = summary.start_utc or start
    end = summary.end_utc(start)
    distance = summary.distance_m if summary.distance_m else None
    if is_accidental_workout(summary.total_duration_s, summary.workout_duration_s, distance, summary.hr_avg):
        return None
    timer = summary.workout_duration_s
    avg_speed = (distance / timer) if distance and timer else None
    max_speed = (1 / summary.pace_best) if summary.pace_best and summary.pace_best > 0 and summary.type_code not in SWIM_CODES else None
    activity = ParsedActivity(
        row={
            "source": source,
            "start_utc": iso_utc(start),
            "end_utc": iso_utc(end),
            "tz": tz.key,
            "local_date": local_date(start, tz),
            "sport": summary.sport,
            "sub_sport": "auto_detected" if summary.auto else None,
            "profile_name": None,
            "elapsed_s": float(summary.total_duration_s) if summary.total_duration_s is not None else None,
            "timer_s": float(timer) if timer is not None else None,
            "distance_m": distance,
            "calories": summary.calories,
            "avg_hr": summary.hr_avg or None,
            "max_hr": summary.hr_max or None,
            "min_hr": summary.hr_min or None,
            "avg_speed_mps": avg_speed,
            "max_speed_mps": max_speed,
            "ascent_m": summary.elevation_gain_m or None,
            "descent_m": summary.elevation_loss_m or None,
            "avg_cadence": summary.cadence_avg or None,
            "max_cadence": summary.cadence_max or None,
            "avg_power_w": None,
            "max_power_w": None,
            "normalized_power_w": None,
            "aerobic_te": summary.aerobic_te or None,
            "anaerobic_te": summary.anaerobic_te or None,
            "training_load": float(summary.workout_load) if summary.workout_load else None,
            "training_stress_score": None,
            "avg_temperature_c": summary.temperature_avg or None,
            "avg_respiration": None,
            "num_laps": len(details.laps) if details and details.laps else None,
            "hr_zone_times_s": json.dumps(summary.hr_zone_times_s) if summary.hr_zone_times_s else None,
            "device_serial": device_serial,
        }
    )
    if details is not None:
        for p in details.points:
            if p.time_ms <= 0:
                continue
            activity.records.append(
                {
                    "ts_utc": iso_utc(datetime.fromtimestamp(p.time_ms / 1000, UTC)),
                    "hr": p.hr if p.hr else None,
                    "distance_m": p.distance_m,
                    "speed_mps": p.speed_mps,
                    "cadence": float(p.cadence) if p.cadence else None,
                    "altitude_m": p.altitude_m,
                    "lat": p.lat,
                    "lon": p.lon,
                    "power_w": None,
                    "temperature_c": p.temperature_c,
                    "respiration": None,
                }
            )
        for lap in details.laps:
            activity.laps.append(
                {
                    "lap_index": lap.number,
                    "start_utc": None,
                    "end_utc": None,
                    "elapsed_s": lap.duration_ms / 1000 if lap.duration_ms else None,
                    "timer_s": lap.duration_ms / 1000 if lap.duration_ms else None,
                    "distance_m": float(lap.distance_m) if lap.distance_m else None,
                    "avg_hr": lap.hr or None,
                    "max_hr": None,
                    "avg_speed_mps": 1000 / lap.pace_s_per_km if lap.pace_s_per_km else None,
                    "calories": lap.calories or None,
                    "ascent_m": None,
                    "descent_m": None,
                }
            )
    out.activities.append(activity)
    if summary.vo2_max:
        out.metrics.append({"source": source, "ts_utc": iso_utc(end), "tz": tz.key, "local_date": local_date(end, tz), "metric": "vo2_max", "value": float(summary.vo2_max), "sport": summary.sport})
    return activity
