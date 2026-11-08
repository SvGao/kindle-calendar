"""Front-light level shared by the phone page and the device loop.

On a Paperwhite the light slider lives in the stock UI -- and the dashboard stops
that UI to keep the framebuffer, so the slider disappears exactly when the panel
is up.  The server therefore holds the level, the device polls it once a minute
(it is already awake for the clock patch) and applies it with ``light.sh``.

State file: ``$DATA_DIR/light.json``

    {"level": 12, "applied": 12, "applied_at": "...", "updated": "..."}

``level`` is what the user asked for, ``applied`` is what the device last
reported back.  When they differ the device has not caught up yet.
"""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path

LEVEL_MIN = 1
LEVEL_MAX = 24
DEFAULT_LEVEL = 12


def clamp(value) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LEVEL
    return max(LEVEL_MIN, min(LEVEL_MAX, n))


def _path(data_dir: Path) -> Path:
    return data_dir / "light.json"


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
        elif key in ("applied_at", "updated"):
            state[key] = value
    _path(data_dir).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return state


def pick(
    schedule: tuple[tuple[time, int | None], ...],
    now: time,
    user_level: int,
) -> tuple[int, time | None]:
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


def now_iso() -> str:
    from .config import get_settings

    return datetime.now(get_settings().tz).isoformat(timespec="seconds")
