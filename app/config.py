"""Runtime settings, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _schedule(value: str) -> tuple[tuple[time, int | None], ...]:
    """Parse ``LIGHT_SCHEDULE``.

    ``"23:00=3,06:00=user"`` means: from 23:00 the panel sits at 3, from 06:00 it
    goes back to whatever the user last picked on the phone page.  ``user`` (or an
    empty value) is the only symbolic entry; anything else must be 1-24.
    """
    slots: list[tuple[time, int | None]] = []
    for part in value.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        hhmm, raw = part.split("=", 1)
        try:
            at = _time(hhmm.strip())
        except ValueError:
            continue
        raw = raw.strip().lower()
        if raw in ("", "user", "auto"):
            slots.append((at, None))
            continue
        try:
            level = int(raw)
        except ValueError:
            continue
        slots.append((at, max(1, min(24, level))))
    slots.sort(key=lambda s: (s[0].hour, s[0].minute))
    return tuple(slots)


@dataclass(frozen=True)
class Settings:
    tz: ZoneInfo
    data_dir: Path
    # Weather
    weather_provider: str  # auto | qweather | openmeteo | fake
    qweather_key: str
    qweather_host: str
    location: str  # QWeather location id
    lat: float
    lon: float
    city_name: str
    # Rendering
    width: int  # landscape design width
    height: int  # landscape design height
    rotate: int  # clockwise degrees applied before output (0, 90, 180, 270)
    # Schedule
    interval_min: int
    night_start: time
    night_end: time
    night_sleep: bool  # skip the poster refresh between night_start and night_end
    low_battery_interval_min: int
    full_refresh_every: int  # forced full refresh after N partial updates
    clock_enabled: bool  # per-minute HH:MM patch
    # Front light
    light_night_auto: bool  # master switch for the schedule below
    night_light_level: int
    light_schedule: tuple[tuple[time, int | None], ...]
    # Misc
    token: str
    stale_after_min: int

    @property
    def provider(self) -> str:
        if self.weather_provider != "auto":
            return self.weather_provider
        return "qweather" if self.qweather_key and self.qweather_host else "openmeteo"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"


def _bool(text) -> bool:
    return str(text).strip().lower() in ("1", "true", "yes", "on")


@lru_cache
def get_settings() -> Settings:
    rotate = int(_env("ROTATE", "90")) % 360
    if rotate not in (0, 90, 180, 270):
        raise ValueError("ROTATE must be 0, 90, 180 or 270")
    night_start = _time(_env("NIGHT_START", "23:00"))
    night_end = _time(_env("NIGHT_END", "06:00"))
    night_level = int(_env("NIGHT_LIGHT_LEVEL", "3"))
    # With no schedule at all it is exactly the old two-point behaviour: dim at
    # night_start, back to the user's level at night_end.  Built through the parser
    # so the list comes back sorted -- pick() walks it in time order.
    schedule = _schedule(_env("LIGHT_SCHEDULE")) or _schedule(
        f"{night_start:%H:%M}={night_level},{night_end:%H:%M}=user"
    )
    s = Settings(
        tz=ZoneInfo(_env("TZ", "Asia/Shanghai") or "Asia/Shanghai"),
        data_dir=Path(_env("DATA_DIR", "data")),
        weather_provider=_env("WEATHER_PROVIDER", "auto").lower(),
        qweather_key=_env("QWEATHER_KEY"),
        qweather_host=_env("QWEATHER_HOST").removeprefix("https://").rstrip("/"),
        location=_env("LOCATION", "101120201"),
        lat=float(_env("LAT", "36.07")),
        lon=float(_env("LON", "120.38")),
        city_name=_env("CITY_NAME", "青岛"),
        width=int(_env("WIDTH", "1448")),
        height=int(_env("HEIGHT", "1072")),
        rotate=rotate,
        interval_min=int(_env("RENDER_INTERVAL_MIN", "15")),
        night_start=night_start,
        night_end=night_end,
        night_sleep=_bool(_env("NIGHT_SLEEP", "0")),
        low_battery_interval_min=int(_env("LOW_BATTERY_INTERVAL_MIN", "60")),
        full_refresh_every=int(_env("FULL_REFRESH_EVERY", "24")),
        clock_enabled=_bool(_env("CLOCK_ENABLED", "1")),
        light_night_auto=_bool(_env("LIGHT_NIGHT_AUTO", "1")),
        night_light_level=night_level,
        light_schedule=schedule,
        token=_env("TOKEN"),
        stale_after_min=int(_env("STALE_AFTER_MIN", "120")),
    )
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    s.out_dir.mkdir(parents=True, exist_ok=True)
    return s
