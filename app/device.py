"""Device status pushed by the Kindle via POST /api/device.

The dashboard image is rendered here on the NAS, so the Kindle's own battery
cannot be drawn by the device. This module is the bridge: the device reports
battery/charging every cycle, we keep the last report on disk and paint it into
the footer on the next render.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .config import Settings, get_settings

# A report older than this means the device has stopped talking to us; the
# number is still shown but marked as uncertain.
STALE_AFTER = timedelta(minutes=90)

# Below this the footer prepends a warning triangle. It is a wider net than the
# 15% throttle in main.py: the icon is a heads-up, the throttle is battery saving.
LOW_BATTERY_PCT = 20


def read_state(s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    try:
        return json.loads((s.data_dir / "device.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _age(now: datetime, seen: str, tz: Any) -> timedelta | None:
    """Age of a report, tolerating a naive `now` against an aware `seen_at`."""
    try:
        seen_dt = datetime.fromisoformat(seen)
    except ValueError:
        return None
    if seen_dt.tzinfo is None and now.tzinfo is not None:
        seen_dt = seen_dt.replace(tzinfo=now.tzinfo)
    elif seen_dt.tzinfo is not None and now.tzinfo is None:
        # Match the naive side: convert into the app's zone, then drop the tzinfo.
        seen_dt = seen_dt.astimezone(tz).replace(tzinfo=None)
    return now - seen_dt


def status_line(now: datetime, s: Settings | None = None) -> str:
    """Footer text such as "电量 87%", or "" when we have never heard from it.

    Charging state is deliberately *not* painted: the Kindle is always on a
    charger here, and the flag flips the moment the cable moves -- but the footer
    only repaints on the 15-minute poster, so the label would be wrong most of
    the time it is visible. The percentage alone is always accurate enough.
    """
    info = battery_info(now, s)
    return info["text"] if info else ""


def battery_info(now: datetime, s: Settings | None = None) -> dict[str, Any] | None:
    """{"pct", "text", "low"} for the footer, or None when never reported."""
    s = s or get_settings()
    st = read_state(s)
    batt = st.get("battery")
    if not isinstance(batt, int):
        return None

    text = f"电量 {batt}%"

    seen = st.get("seen_at")
    if seen:
        age = _age(now, seen, s.tz)
        if age is not None and age > STALE_AFTER:
            hours = int(age.total_seconds() // 3600)
            text += f"（{hours}h前）" if hours else "（旧）"
    return {"pct": batt, "text": text, "low": batt < LOW_BATTERY_PCT}
