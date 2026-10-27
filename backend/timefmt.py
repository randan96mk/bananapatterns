"""12-hour IST times for everything people read (Telegram alerts, chart images, the dashboard API):
"6:01 AM", "3:15 PM". Settings and schedules stay HH:MM (24-hour) internally; only the display changes."""

from datetime import datetime, time
from typing import Union


def ampm(v: Union[str, time, datetime]) -> str:
    """'15:15' / time(15, 15) / a datetime -> '3:15 PM'; '00:05' -> '12:05 AM'; '12:00' -> '12:00 PM'."""
    if isinstance(v, str):
        h, m = (int(x) for x in v.strip().split(":")[:2])
    else:
        h, m = v.hour, v.minute
    return f"{h % 12 or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def ampm_date(dt: datetime) -> str:
    """'6:00 AM, 28 Oct'."""
    return f"{ampm(dt)}, {dt.day} {dt:%b}"
