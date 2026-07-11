"""Capture-date resolution chain: EXIF -> filename inference -> file mtime.

See docs/ARCHITECTURE.md §3.1. Every result carries its provenance
(`exif` | `filename` | `mtime`) and a `date_only` flag for matches that
carry no time component.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import TS_EXIF, TS_FILENAME, TS_MTIME

MIN_YEAR = 1990
_FUTURE_SLACK = timedelta(days=1)

# Ordered: specific patterns first, generic extractors last. First match wins.
BUILTIN_PATTERNS: list[str] = [
    # WhatsApp: IMG-20240711-WA0001.jpg (date only)
    r"(?:IMG|VID)-(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})-WA\d+",
    # Screenshot_2024-07-11-18-30-00.png / Screenshot 2024-07-11 at 18.30.00
    r"(?P<Y>\d{4})-(?P<M>\d{2})-(?P<D>\d{2})[-_ .](?:at[ _])?(?P<h>\d{2})[-.](?P<m>\d{2})[-.](?P<s>\d{2})",
    # signal-2024-07-11-183000.jpg
    r"(?P<Y>\d{4})-(?P<M>\d{2})-(?P<D>\d{2})-(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})",
    # Android/Pixel compact: IMG_20240711_183000.jpg, PXL_20240711_183000123.jpg,
    # generic 20240711-183000 / 20240711183000
    r"(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})[-_ .]?(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})",
    # Generic compact date only: 20240711
    r"(?P<Y>\d{4})(?P<M>\d{2})(?P<D>\d{2})",
    # Generic dashed date only: 2024-07-11
    r"(?P<Y>\d{4})-(?P<M>\d{2})-(?P<D>\d{2})",
]


@dataclass
class ResolvedDate:
    local: datetime  # naive local clock time
    tz_offset: str | None  # "+03:00" style, when known
    source: str  # exif | filename | mtime
    date_only: bool = False

    @property
    def iso(self) -> str:
        return self.local.strftime("%Y-%m-%dT%H:%M:%S")


def _within_bounds(dt: datetime, now: datetime) -> bool:
    return MIN_YEAR <= dt.year and dt <= now + _FUTURE_SLACK


def parse_exif_datetime(value: str) -> datetime | None:
    """Parse EXIF-style '2024:07:11 18:30:00' (with optional subseconds/offset)."""
    if not value:
        return None
    v = value.strip()
    # strip trailing offset like +03:00 / Z and subseconds
    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", v)
    if not m:
        return None
    try:
        dt = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    if dt.year < MIN_YEAR:  # cameras with unset clocks produce 0000/1970/1980
        return None
    return dt


def infer_date_from_filename(
    filename: str, extra_patterns: list[str] | None = None, now: datetime | None = None
) -> tuple[datetime, bool] | None:
    """Try the pattern library against a filename. Returns (dt, date_only) or None.

    User-supplied `extra_patterns` (regexes with named groups Y M D and
    optionally h m s) are tried before the built-ins; first match wins.
    Results outside sanity bounds (before 1990 or in the future) are rejected
    and the next pattern is tried.
    """
    now = now or datetime.now()
    for pattern in (extra_patterns or []) + BUILTIN_PATTERNS:
        m = re.search(pattern, filename)
        if not m:
            continue
        g = m.groupdict()
        try:
            date_only = g.get("h") is None
            dt = datetime(
                int(g["Y"]),
                int(g["M"]),
                int(g["D"]),
                int(g.get("h") or 0),
                int(g.get("m") or 0),
                int(g.get("s") or 0),
            )
        except (ValueError, KeyError):
            continue
        if not _within_bounds(dt, now):
            continue
        return dt, date_only
    return None


def resolve_capture_date(
    exif_meta: dict,
    filename: str,
    file_mtime: float | None,
    home_timezone: str = "UTC",
    extra_patterns: list[str] | None = None,
) -> ResolvedDate:
    """The chain: EXIF DateTimeOriginal/CreateDate -> filename -> mtime."""
    # 1. EXIF
    for key in ("DateTimeOriginal", "CreateDate", "CreationDate", "DateTimeCreated"):
        dt = parse_exif_datetime(str(exif_meta.get(key) or ""))
        if dt and _within_bounds(dt, datetime.now()):
            offset = None
            raw_off = str(exif_meta.get("OffsetTimeOriginal") or exif_meta.get("OffsetTime") or "")
            if re.fullmatch(r"[+-]\d{2}:\d{2}", raw_off.strip()):
                offset = raw_off.strip()
            return ResolvedDate(local=dt, tz_offset=offset, source=TS_EXIF)

    # 2. Filename inference
    inferred = infer_date_from_filename(filename, extra_patterns)
    if inferred:
        dt, date_only = inferred
        return ResolvedDate(local=dt, tz_offset=None, source=TS_FILENAME, date_only=date_only)

    # 3. mtime, rendered in the configured home timezone
    tz = ZoneInfo(home_timezone)
    mts = datetime.fromtimestamp(file_mtime or 0, tz=timezone.utc).astimezone(tz)
    offset = mts.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else None
    return ResolvedDate(local=mts.replace(tzinfo=None), tz_offset=offset, source=TS_MTIME)
