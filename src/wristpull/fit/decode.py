"""Generic FIT decoding on top of fitdecode.

Every data message is reduced to a :class:`FitMessage` that exposes values by
field number *and* by profile name, so undocumented Garmin fields (e.g. body
battery in ``stress_level`` field 3) stay reachable.
"""

from __future__ import annotations

import io
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import fitdecode

from ..timeutil import iso_utc

_MISSING = object()


@dataclass
class FitMessage:
    num: int
    name: str
    values: dict[int, Any] = field(default_factory=dict)  # def_num -> scaled value (non-expanded fields)
    raw: dict[int, Any] = field(default_factory=dict)  # def_num -> raw value
    named: dict[str, Any] = field(default_factory=dict)  # name -> scaled value (incl. expanded/sub fields)
    field_names: dict[int, str] = field(default_factory=dict)  # def_num -> profile name (unknown_<n> if undocumented)

    def get(self, key: int | str, default: Any = None) -> Any:
        if isinstance(key, int):
            value = self.values.get(key, _MISSING)
        else:
            value = self.named.get(key, _MISSING)
        return default if value is _MISSING else value

    def get_raw(self, def_num: int, default: Any = None) -> Any:
        return self.raw.get(def_num, default)

    def timestamp(self) -> datetime | None:
        value = self.values.get(253)
        return value if isinstance(value, datetime) else None


@dataclass
class FitFile:
    path: str
    file_type: str | None = None
    time_created: datetime | None = None
    manufacturer: str | None = None
    product: str | None = None
    product_name: str | None = None
    serial_number: int | None = None
    messages: list[FitMessage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def of(self, *names_or_nums: str | int) -> list[FitMessage]:
        wanted_names = {n for n in names_or_nums if isinstance(n, str)}
        wanted_nums = {n for n in names_or_nums if isinstance(n, int)}
        return [m for m in self.messages if m.name in wanted_names or m.num in wanted_nums]

    @property
    def time_created_iso(self) -> str | None:
        return iso_utc(self.time_created) if self.time_created else None


def _message_name(frame: fitdecode.FitDataMessage) -> str:
    return frame.name if frame.name else f"unknown_{frame.global_mesg_num}"


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def decode_fit(source: Path | str | bytes, *, check_crc: bool = True) -> FitFile:
    """Decode a FIT file from a path or bytes into a :class:`FitFile`."""
    if isinstance(source, (bytes, bytearray)):
        fileish: Any = io.BytesIO(bytes(source))
        path_label = "<bytes>"
    else:
        fileish = str(source)
        path_label = str(source)

    crc_mode = fitdecode.CrcCheck.WARN if check_crc else fitdecode.CrcCheck.DISABLED
    result = FitFile(path=path_label)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with fitdecode.FitReader(fileish, check_crc=crc_mode, error_handling=fitdecode.ErrorHandling.WARN) as reader:
            for frame in reader:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                msg = FitMessage(num=frame.global_mesg_num, name=_message_name(frame))
                for fd in frame.fields:
                    if fd.def_num is not None and not fd.is_expanded:
                        msg.values[fd.def_num] = fd.value
                        msg.raw[fd.def_num] = fd.raw_value
                        msg.field_names[fd.def_num] = fd.name or f"unknown_{fd.def_num}"
                    if fd.name is not None and fd.name not in msg.named:
                        msg.named[fd.name] = fd.value
                result.messages.append(msg)
                if frame.global_mesg_num == 0 and result.file_type is None:
                    result.file_type = _as_str(msg.get("type"))
                    tc = msg.get("time_created")
                    result.time_created = tc if isinstance(tc, datetime) else None
                    result.manufacturer = _as_str(msg.get("manufacturer"))
                    result.product = _as_str(msg.get("garmin_product", msg.get("product")))
                    result.product_name = _as_str(msg.get("product_name"))
                    sn = msg.get("serial_number")
                    result.serial_number = int(sn) if isinstance(sn, int) else None
        result.warnings = [str(w.message) for w in caught]
    return result


def summarize(fit: FitFile, *, samples: int = 3) -> OrderedDict[str, dict[str, Any]]:
    """Per message type: count plus the fields seen with example values (for ``inspect``)."""
    out: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for msg in fit.messages:
        entry = out.setdefault(msg.name, {"num": msg.num, "count": 0, "fields": OrderedDict()})
        entry["count"] += 1
        for def_num, value in msg.values.items():
            key = f"{def_num}:{msg.field_names.get(def_num, f'unknown_{def_num}')}"
            bucket = entry["fields"].setdefault(key, [])
            if value is not None and len(bucket) < samples:
                bucket.append(value)
    return out
