"""When should the device fetch next, and when should it do a full (flashing) refresh."""

from __future__ import annotations

from datetime import datetime, time, timedelta

# Wake a little after the server's render slot so the new image is ready.
RENDER_GRACE_S = 60
MIN_SLEEP_S = 60


def is_night(t: time, start: time, end: time) -> bool:
    if start == end:
        return False
    if start > end:  # crosses midnight
        return t >= start or t < end
    return start <= t < end


def _next_at(now: datetime, t: time) -> datetime:
    target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def next_slot(
    now: datetime,
    interval_min: int,
    night_start: time,
    night_end: time,
    skip_night: bool = False,
) -> datetime:
    """Next render slot aligned to the interval.

    ``skip_night`` keeps the old behaviour of jumping straight to ``night_end``;
    it is off by default because the panel is meant to read as a clock and the
    clock patch keeps ticking through the night either way -- sleeping only froze
    the poster (weather, calendar), which looked like a stalled screen.
    """
    if skip_night and is_night(now.time(), night_start, night_end):
        return _next_at(now, night_end)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    step = interval_min * 60
    slot = midnight + timedelta(seconds=(int(elapsed // step) + 1) * step)
    if skip_night and is_night(slot.time(), night_start, night_end):
        return _next_at(now, night_end)
    return slot


def seconds_until_wakeup(
    now: datetime,
    interval_min: int,
    night_start: time,
    night_end: time,
    skip_night: bool = False,
) -> int:
    target = next_slot(now, interval_min, night_start, night_end, skip_night) + timedelta(seconds=RENDER_GRACE_S)
    return max(MIN_SLEEP_S, int((target - now).total_seconds()))


def full_refresh_due(now: datetime, night_end: time, interval_min: int) -> bool:
    """True during the first slot after the night ends (the morning full refresh)."""
    start = now.replace(hour=night_end.hour, minute=night_end.minute, second=0, microsecond=0)
    return start <= now < start + timedelta(minutes=interval_min)
