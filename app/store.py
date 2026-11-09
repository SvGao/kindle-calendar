"""Settings edited from the web console, stored in ``$DATA_DIR/settings.json``.

``.env`` holds what is needed to boot (provider keys, location, token).  Anything a
person might want to flip from a phone -- anniversaries, display toggles, push
notifications -- lives here instead, so no container restart is needed.

A value of ``None`` means "not set in the console, use the .env default".
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings, get_settings

_lock = threading.Lock()

KINDS = ("birthday", "anniversary", "countdown")
NOTIFY_KINDS = ("none", "bark", "wecom", "webhook")

DEFAULTS: dict[str, Any] = {
    "anniversaries": [],
    "event_days": 30,  # show an upcoming anniversary on the poster this many days ahead
    "show_poem": True,
    "clock_enabled": None,  # None -> CLOCK_ENABLED
    "night_sleep": None,  # None -> NIGHT_SLEEP
    "clock_flash": True,  # flash the clock strip once an hour to clear ghosting
    "notify": {
        "kind": "none",
        "url": "",
        "battery": True,
        "weather_alert": False,
    },
}


def _path(s: Settings) -> Path:
    return s.data_dir / "settings.json"


def read(s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    try:
        raw = json.loads(_path(s).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k == "notify" and isinstance(v, dict):
            out["notify"].update({nk: nv for nk, nv in v.items() if nk in out["notify"]})
        elif k in out:
            out[k] = v
    return out


def _write(s: Settings, data: dict[str, Any]) -> None:
    p = _path(s)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


_MMDD = re.compile(r"^(\d{4}-)?\d{2}-\d{2}$")


def clean_event(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate one anniversary from the console; ``None`` drops it."""
    name = str(raw.get("name", "")).strip()[:12]
    when = str(raw.get("date", "")).strip()
    if not name or not _MMDD.match(when):
        return None
    lunar = bool(raw.get("lunar"))
    kind = raw.get("kind") if raw.get("kind") in KINDS else "anniversary"
    parts = [int(x) for x in when.split("-")]
    year, month, day = (parts if len(parts) == 3 else [None, *parts])
    if not (1 <= month <= 12 and 1 <= day <= (30 if lunar else 31)):
        return None
    if not lunar:
        try:
            date(year or 2000, month, day)  # 2000 is a leap year, so 02-29 passes
        except ValueError:
            return None
    if kind == "countdown" and year is None:
        return None  # a one-off countdown needs a year
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": name,
        "date": when,
        "lunar": lunar,
        "kind": kind,
    }


def update(changes: dict[str, Any], s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    with _lock:
        data = read(s)
        for key, value in changes.items():
            if key == "anniversaries" and isinstance(value, list):
                data["anniversaries"] = [e for e in (clean_event(v) for v in value if isinstance(v, dict)) if e]
            elif key == "event_days":
                try:
                    data["event_days"] = max(0, min(366, int(value)))
                except (TypeError, ValueError):
                    pass
            elif key in ("show_poem", "clock_flash"):
                data[key] = bool(value)
            elif key in ("clock_enabled", "night_sleep"):
                data[key] = None if value is None else bool(value)
            elif key == "notify" and isinstance(value, dict):
                n = data["notify"]
                if value.get("kind") in NOTIFY_KINDS:
                    n["kind"] = value["kind"]
                if "url" in value and not str(value["url"]).startswith("***"):
                    n["url"] = str(value["url"]).strip()[:500]
                for flag in ("battery", "weather_alert"):
                    if flag in value:
                        n[flag] = bool(value[flag])
        _write(s, data)
        return data


def effective(s: Settings | None = None) -> dict[str, Any]:
    """Console values with .env fallbacks resolved."""
    s = s or get_settings()
    d = read(s)
    return {
        "clock_enabled": s.clock_enabled if d["clock_enabled"] is None else d["clock_enabled"],
        "night_sleep": s.night_sleep if d["night_sleep"] is None else d["night_sleep"],
        "show_poem": d["show_poem"],
        "clock_flash": d["clock_flash"],
        "event_days": d["event_days"],
    }


def masked_url(url: str) -> str:
    """Show enough of a push URL to recognise it, never the key itself."""
    if not url:
        return ""
    if len(url) <= 24:
        return "***"
    return "***" + url[-6:]
