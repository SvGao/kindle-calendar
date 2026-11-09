"""Front-light level shared by the console and the device loop.

On a Paperwhite the light slider lives in the stock UI -- and the dashboard stops
that UI to keep the framebuffer, so the slider disappears exactly when the panel
is up.  The server therefore holds the level, the device polls it once a minute
(it is already awake for the clock patch) and applies it with ``light.sh``.

State file: ``$DATA_DIR/light.json``

    {"level": 12, "applied": 12, "applied_at": "...", "updated": "...",
     "night_auto": true, "schedule": [["23:00", 3], ["06:00", null]],
     "hold_until": "2026-11-09T23:00:00+08:00"}

``level``       the manual level (0-24, 0 = light off)
``applied``     what the device last reported back
``night_auto``  follow the schedule; ``None`` means use LIGHT_NIGHT_AUTO
``schedule``    slots edited in the console; ``None`` means use LIGHT_SCHEDULE
``hold_until``  a manual change made while the schedule runs wins until the next
                slot starts, then the schedule takes over again
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

LEVEL_MIN = 0
LEVEL_MAX = 24
DEFAULT_LEVEL = 12

Schedule = tuple[tuple[time, int | None], ...]


def clamp(value) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(LEVEL_MIN, min(LEVEL_MAX, n))


def _path(data_dir: Path) -> Path:
    return data_dir / "light.json"


def _parse_schedule(raw: Any) -> Schedule | None:
    """``[["23:00", 3], ["06:00", null]]`` -> sorted slots; ``None`` when absent or broken."""
    if not isinstance(raw, list):
        return None
    slots: list[tuple[time, int | None]] = []
    for item in raw:
        try:
            hhmm, level = item
            hh, mm = str(hhmm).split(":")
            at = time(int(hh), int(mm))
        except (TypeError, ValueError):
            continue
        slots.append((at, None if level is None else clamp(level)))
    slots.sort(key=lambda s: (s[0].hour, s[0].minute))
    # Two slots at the same minute make no sense; keep the last one entered.
    dedup: dict[tuple[int, int], tuple[time, int | None]] = {}
    for slot in slots:
        dedup[(slot[0].hour, slot[0].minute)] = slot
    return tuple(dedup.values())


def schedule_to_json(schedule: Schedule) -> list[list[Any]]:
    return [[at.strftime("%H:%M"), level] for at, level in schedule]


def read(data_dir: Path) -> dict:
    try:
        raw = json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    applied = raw.get("applied")
    night_auto = raw.get("night_auto")
    return {
        "level": clamp(raw.get("level", DEFAULT_LEVEL)),
        "applied": applied if isinstance(applied, int) else None,
        "applied_at": raw.get("applied_at"),
        "updated": raw.get("updated"),
        # None means "follow LIGHT_NIGHT_AUTO from the environment".
        "night_auto": night_auto if isinstance(night_auto, bool) else None,
        "schedule": _parse_schedule(raw.get("schedule")),
        "hold_until": raw.get("hold_until") or None,
    }


def write(data_dir: Path, **changes) -> dict:
    state = read(data_dir)
    for key, value in changes.items():
        if key == "level":
            state["level"] = clamp(value)
        elif key == "applied":
            state["applied"] = value if isinstance(value, int) else None
        elif key == "night_auto":
            # None clears the pin, so LIGHT_NIGHT_AUTO from .env applies again.
            state["night_auto"] = None if value is None else bool(value)
        elif key == "schedule":
            state["schedule"] = None if value is None else _parse_schedule(value)
        elif key in ("applied_at", "updated", "hold_until"):
            state[key] = value
    out = dict(state)
    out["schedule"] = None if state["schedule"] is None else schedule_to_json(state["schedule"])
    _path(data_dir).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return state


def pick(schedule: Schedule, now: time, user_level: int) -> tuple[int, time | None]:
    """Level in force at ``now``, and the slot that produced it.

    A slot is a start time plus a level; ``None`` means "the user's own value".
    Slots wrap around midnight, so the last entry covers the early hours until the
    first one comes round again.
    """
    if not schedule:
        return user_level, None
    mark = (now.hour, now.minute)
    chosen = schedule[-1]
    for slot in schedule:
        if (slot[0].hour, slot[0].minute) <= mark:
            chosen = slot
    at, level = chosen
    return (user_level if level is None else level), at


def next_boundary(schedule: Schedule, now: datetime) -> datetime | None:
    """When the next slot starts, as an aware datetime; ``None`` without a schedule."""
    if not schedule:
        return None
    best = None
    for at, _ in schedule:
        cand = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        if best is None or cand < best:
            best = cand
    return best


def held(state: dict, now: datetime) -> bool:
    until = state.get("hold_until")
    if not until:
        return False
    try:
        return now < datetime.fromisoformat(str(until))
    except (TypeError, ValueError):
        return False


def now_iso() -> str:
    from .config import get_settings

    return datetime.now(get_settings().tz).isoformat(timespec="seconds")
