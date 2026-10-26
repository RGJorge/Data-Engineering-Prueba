"""Timestamp normalisation to UTC.

Firmwares deliver three dialects:
    * epoch seconds  (float)  -> tracks firmware A, detections firmware A
    * epoch millis   (int)    -> tracks firmware C, detections firmware B
    * ISO-8601 + offset       -> tracks firmware B, detections firmware C, clips

Everything is converted to a timezone-aware UTC datetime. The offsets seen in
the data are +01:00 / +02:00 (local European time), so converting to UTC can
move a reading across the year boundary -- which is exactly why the 2025 check
(fact 1) is applied *after* this step.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# A 2025 epoch is ~1.74e9 s, ~1.74e12 ms, ~1.74e15 us. These ranges never
# overlap, so magnitude alone tells the unit apart. We detect it from the value
# itself rather than trusting the firmware header: one firmware ships an A-style
# header (declares seconds) while actually writing milliseconds, which read as
# seconds lands in the year 57297. Magnitude detection absorbs that surprise.
_US_THRESHOLD = 1e14
_MS_THRESHOLD = 1e11


def from_epoch(value: float, unit: Optional[str] = None) -> datetime:
    """Convert an epoch number to a UTC datetime, inferring s / ms / us from the
    magnitude. ``unit`` is accepted for documentation but intentionally ignored:
    the data has proven the header's declared unit unreliable."""
    v = float(value)
    if v >= _US_THRESHOLD:
        v /= 1_000_000.0
    elif v >= _MS_THRESHOLD:
        v /= 1_000.0
    return datetime.fromtimestamp(v, tz=timezone.utc)


def from_iso(text: str) -> datetime:
    """Parse an ISO-8601 string (with or without offset) into UTC.

    Accepts a trailing 'Z' and explicit offsets like '+01:00'. A value with no
    offset is assumed to already be UTC.
    """
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_ms(dt: datetime) -> str:
    """Render as 'YYYY-MM-DDThh:mm:ss.SSSZ' (UTC, millisecond precision)."""
    dt = dt.astimezone(timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{dt.microsecond // 1000:03d}Z"


def in_year(dt: datetime, year: int) -> bool:
    """True if the UTC datetime falls within the given calendar year."""
    return dt.astimezone(timezone.utc).year == year
