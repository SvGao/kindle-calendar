"""China public holidays and make-up workdays from NateScarlet/holiday-cn."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .. import cache

log = logging.getLogger(__name__)

URLS = [
    "https://cdn.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json",
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json",
]
REFRESH_MIN = 24 * 60


def _fetch(year: int) -> dict[str, Any]:
    last_error: Exception | None = None
    for url in URLS:
        try:
            r = httpx.get(url.format(year=year), timeout=15.0, follow_redirects=True)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_error = e
    raise RuntimeError(f"all holiday sources failed: {last_error}")


def get_holidays(year: int) -> dict[str, dict[str, Any]]:
    """Map 'YYYY-MM-DD' -> {'name': str, 'off': bool}. Empty dict if unavailable."""
    name = f"holiday-{year}"
    data, fetched_at = cache.read(name)
    if data is None or cache.age_minutes(fetched_at) > REFRESH_MIN:
        try:
            data = _fetch(year)
            cache.write(name, data)
        except Exception as e:  # noqa: BLE001
            log.warning("holiday fetch failed for %s: %s", year, e)
    if not data:
        return {}
    return {d["date"]: {"name": d["name"], "off": bool(d["isOffDay"])} for d in data.get("days", [])}
