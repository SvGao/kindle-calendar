"""Weather data from QWeather, Open-Meteo, or a fake provider.

Normalized shape:
{
  "source": "qweather",
  "now": {"temp": 18, "text": "晴", "icon": "sunny", "humidity": 45,
          "wind_dir": "东北风", "wind_scale": "3"},
  "daily": [{"date": "2026-11-07", "temp_min": 12, "temp_max": 21,
             "text": "晴", "icon": "sunny"}, ...],   # today first, >= 4 days
  "aqi": {"value": 52, "category": "良"} | None,
  "alerts": [{"title": "大风蓝色预警", "color": "blue"}],
}
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import httpx

from .. import cache
from ..config import Settings

log = logging.getLogger(__name__)

CACHE_NAME = "weather"
REFRESH_MIN = 30
TIMEOUT = httpx.Timeout(15.0)

# --- icon categories -------------------------------------------------------


def qweather_icon(code: str | int) -> str:
    c = int(code)
    if c in (100, 150):
        return "sunny"
    if c in (101, 102, 103, 151, 152, 153):
        return "partly"
    if c == 104:
        return "overcast"
    if c in (302, 303, 304):
        return "thunder"
    if c in (313, 404, 405, 406, 456):
        return "sleet"
    if 300 <= c <= 399:
        return "rain"
    if 400 <= c <= 499:
        return "snow"
    if c in (500, 501, 509, 510, 514, 515):
        return "fog"
    if c in (502, 511, 512, 513):
        return "haze"
    if c in (503, 504, 507, 508):
        return "dust"
    return "unknown"


WMO = {
    0: ("晴", "sunny"),
    1: ("晴间多云", "partly"),
    2: ("多云", "partly"),
    3: ("阴", "overcast"),
    45: ("雾", "fog"),
    48: ("雾凇", "fog"),
    51: ("小毛毛雨", "rain"),
    53: ("毛毛雨", "rain"),
    55: ("大毛毛雨", "rain"),
    56: ("冻毛毛雨", "sleet"),
    57: ("冻毛毛雨", "sleet"),
    61: ("小雨", "rain"),
    63: ("中雨", "rain"),
    65: ("大雨", "rain"),
    66: ("冻雨", "sleet"),
    67: ("冻雨", "sleet"),
    71: ("小雪", "snow"),
    73: ("中雪", "snow"),
    75: ("大雪", "snow"),
    77: ("雪粒", "snow"),
    80: ("阵雨", "rain"),
    81: ("阵雨", "rain"),
    82: ("强阵雨", "rain"),
    85: ("阵雪", "snow"),
    86: ("强阵雪", "snow"),
    95: ("雷阵雨", "thunder"),
    96: ("雷阵雨伴冰雹", "thunder"),
    99: ("雷阵雨伴冰雹", "thunder"),
}

WIND_DIRS = ["北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风"]
BEAUFORT_KMH = [1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118]


def _wind_dir(deg: float) -> str:
    return WIND_DIRS[int((deg % 360) / 45 + 0.5) % 8]


def _beaufort(kmh: float) -> str:
    for scale, limit in enumerate(BEAUFORT_KMH):
        if kmh < limit:
            return str(scale)
    return "12"


# --- providers ---------------------------------------------------------------


def _qweather(s: Settings) -> dict[str, Any]:
    base = f"https://{s.qweather_host}"
    headers = {"X-QW-Api-Key": s.qweather_key}
    with httpx.Client(base_url=base, headers=headers, timeout=TIMEOUT) as c:
        now = c.get("/v7/weather/now", params={"location": s.location}).json()
        daily = c.get("/v7/weather/7d", params={"location": s.location}).json()
        if now.get("code") != "200" or daily.get("code") != "200":
            raise RuntimeError(f"qweather error now={now.get('code')} daily={daily.get('code')}")
        n = now["now"]
        result: dict[str, Any] = {
            "source": "qweather",
            "now": {
                "temp": int(n["temp"]),
                "text": n["text"],
                "icon": qweather_icon(n["icon"]),
                "humidity": int(n["humidity"]),
                "wind_dir": n.get("windDir", ""),
                "wind_scale": n.get("windScale", ""),
            },
            "daily": [
                {
                    "date": d["fxDate"],
                    "temp_min": int(d["tempMin"]),
                    "temp_max": int(d["tempMax"]),
                    "text": d["textDay"],
                    "icon": qweather_icon(d["iconDay"]),
                }
                for d in daily["daily"]
            ],
            "aqi": None,
            "alerts": [],
        }
        # Optional extras: failures here must not break the main forecast.
        try:
            air = c.get(f"/airquality/v1/current/{s.lat:.2f}/{s.lon:.2f}").json()
            idx = air.get("indexes") or []
            pick = next((i for i in idx if i.get("code") == "cn-mee"), idx[0] if idx else None)
            if pick:
                result["aqi"] = {"value": int(round(float(pick["aqi"]))), "category": pick.get("category", "")}
        except Exception as e:  # noqa: BLE001
            log.warning("air quality fetch failed: %s", e)
        try:
            al = c.get(f"/weatheralert/v1/current/{s.lat:.2f}/{s.lon:.2f}").json()
            for a in al.get("alerts") or []:
                title = a.get("headline") or (a.get("eventType") or {}).get("name", "")
                event = (a.get("eventType") or {}).get("name", "")
                color = (a.get("color") or {}).get("code", "")
                result["alerts"].append({"title": _short_alert(event, color) or title, "color": color})
        except Exception as e:  # noqa: BLE001
            log.warning("weather alert fetch failed: %s", e)
    return result


COLOR_CN = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色", "white": "白色"}


def _short_alert(event: str, color: str) -> str:
    if not event:
        return ""
    return f"{event}{COLOR_CN.get(color, '')}预警"


def _openmeteo(s: Settings) -> dict[str, Any]:
    params = {
        "latitude": s.lat,
        "longitude": s.lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min",
        "timezone": str(s.tz),
        "forecast_days": 5,
    }
    r = httpx.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    cur = j["current"]
    text, icon = WMO.get(int(cur["weather_code"]), ("未知", "unknown"))
    d = j["daily"]
    daily = []
    for i, day in enumerate(d["time"]):
        t, ic = WMO.get(int(d["weather_code"][i]), ("未知", "unknown"))
        daily.append(
            {
                "date": day,
                "temp_min": round(d["temperature_2m_min"][i]),
                "temp_max": round(d["temperature_2m_max"][i]),
                "text": t,
                "icon": ic,
            }
        )
    return {
        "source": "openmeteo",
        "now": {
            "temp": round(cur["temperature_2m"]),
            "text": text,
            "icon": icon,
            "humidity": round(cur["relative_humidity_2m"]),
            "wind_dir": _wind_dir(cur["wind_direction_10m"]),
            "wind_scale": _beaufort(cur["wind_speed_10m"]),
        },
        "daily": daily,
        "aqi": None,
        "alerts": [],
    }


def fake(today: date) -> dict[str, Any]:
    icons = [("晴", "sunny"), ("多云", "partly"), ("小雨", "rain"), ("阴", "overcast"), ("小雪", "snow")]
    return {
        "source": "fake",
        "now": {"temp": 18, "text": "晴", "icon": "sunny", "humidity": 45, "wind_dir": "东北风", "wind_scale": "3"},
        "daily": [
            {
                "date": (today + timedelta(days=i)).isoformat(),
                "temp_min": 10 + i,
                "temp_max": 19 + i,
                "text": icons[i][0],
                "icon": icons[i][1],
            }
            for i in range(5)
        ],
        "aqi": {"value": 52, "category": "良"},
        "alerts": [{"title": "大风蓝色预警", "color": "blue"}],
    }


# --- public API ---------------------------------------------------------------


def get_weather(s: Settings, today: date) -> tuple[dict[str, Any] | None, bool]:
    """Return (weather, stale). Uses the cache when fresh or when fetching fails."""
    if s.provider == "fake":
        return fake(today), False

    data, fetched_at = cache.read(CACHE_NAME)
    if data is not None and data.get("source") == s.provider and cache.age_minutes(fetched_at) < REFRESH_MIN:
        return data, False

    try:
        data = _qweather(s) if s.provider == "qweather" else _openmeteo(s)
        cache.write(CACHE_NAME, data)
        return data, False
    except Exception as e:  # noqa: BLE001
        log.error("weather fetch failed (%s): %s", s.provider, e)
        # QWeather has per-account hosts and quota limits; never let a hiccup
        # blank the screen when the keyless provider can still answer.
        if s.provider == "qweather":
            try:
                data = _openmeteo(s)
                cache.write(CACHE_NAME, data)
                log.warning("fell back to openmeteo for this render")
                return data, False
            except Exception as e2:  # noqa: BLE001
                log.error("openmeteo fallback failed: %s", e2)
        data, fetched_at = cache.read(CACHE_NAME)
        if data is None:
            return None, True
        return data, cache.age_minutes(fetched_at) > s.stale_after_min
