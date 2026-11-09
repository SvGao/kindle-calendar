"""Push notifications: low battery and new weather alerts.

Three transports, chosen in the console:

- ``bark``    the Bark iOS app; URL like ``https://api.day.app/<key>``
- ``wecom``   a WeCom (企业微信) group robot webhook URL
- ``webhook`` any URL that accepts ``POST {"title", "body"}`` as JSON

``check()`` runs from the scheduler every few minutes and only sends on a state
change (battery crossed the threshold, a new alert), so it never repeats the
same message.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from . import cache, device, store
from .config import Settings, get_settings

log = logging.getLogger(__name__)

BATTERY_LOW = 20
BATTERY_RESET = 30


def send(cfg: dict[str, Any], title: str, body: str) -> None:
    kind, url = cfg.get("kind"), (cfg.get("url") or "").strip()
    if kind == "none" or not url:
        raise ValueError("push is not configured")
    with httpx.Client(timeout=10) as c:
        if kind == "bark":
            r = c.get(f"{url.rstrip('/')}/{quote(title, safe='')}/{quote(body, safe='')}", params={"group": "Kindle"})
        elif kind == "wecom":
            r = c.post(url, json={"msgtype": "text", "text": {"content": f"{title}\n{body}"}})
        else:
            r = c.post(url, json={"title": title, "body": body})
        r.raise_for_status()


def _state_path(s: Settings):
    return s.data_dir / "notify_state.json"


def _read_state(s: Settings) -> dict[str, Any]:
    try:
        return json.loads(_state_path(s).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s: Settings, st: dict[str, Any]) -> None:
    _state_path(s).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def pending_messages(now: datetime, s: Settings, cfg: dict[str, Any], st: dict[str, Any]) -> list[tuple[str, str]]:
    """Work out what to send and update ``st`` in place.  Pure apart from reading caches."""
    msgs: list[tuple[str, str]] = []
    dev = device.read_state(s)

    batt = dev.get("battery")
    if isinstance(batt, int):
        low = batt < BATTERY_LOW and not dev.get("charging")
        if low and not st.get("battery_low"):
            st["battery_low"] = True
            if cfg.get("battery"):
                msgs.append(("Kindle 电量低", f"剩余 {batt}%，而且没在充电，检查一下数据线。"))
        elif st.get("battery_low") and (batt >= BATTERY_RESET or dev.get("charging")):
            st["battery_low"] = False

    wx, _ = cache.read("weather")
    titles = [a.get("title", "") for a in (wx or {}).get("alerts") or [] if a.get("title")]
    seen = st.get("alerts") or []
    fresh = [t for t in titles if t not in seen]
    if fresh and cfg.get("weather_alert"):
        msgs.append(("天气预警", "，".join(fresh)))
    st["alerts"] = titles[-20:]
    return msgs


def check(now: datetime | None = None, s: Settings | None = None) -> list[tuple[str, str]]:
    s = s or get_settings()
    now = now or datetime.now(s.tz)
    cfg = store.read(s)["notify"]
    st = _read_state(s)
    msgs = pending_messages(now, s, cfg, st)
    sent = []
    for title, body in msgs:
        try:
            send(cfg, title, body)
            sent.append((title, body))
        except Exception as e:  # noqa: BLE001
            log.warning("push failed (%s): %s", title, e)
    _write_state(s, st)
    return sent
