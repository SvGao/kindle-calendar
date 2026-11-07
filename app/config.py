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
    low_battery_interval_min: int
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


@lru_cache
def get_settings() -> Settings:
    rotate = int(_env("ROTATE", "90")) % 360
    if rotate not in (0, 90, 180, 270):
        raise ValueError("ROTATE must be 0, 90, 180 or 270")
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
        night_start=_time(_env("NIGHT_START", "23:00")),
        night_end=_time(_env("NIGHT_END", "06:00")),
        low_battery_interval_min=int(_env("LOW_BATTERY_INTERVAL_MIN", "60")),
        token=_env("TOKEN"),
        stale_after_min=int(_env("STALE_AFTER_MIN", "120")),
    )
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    s.out_dir.mkdir(parents=True, exist_ok=True)
    return s
