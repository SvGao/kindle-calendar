"""Build the page context, render HTML, screenshot it, and post-process for e-ink."""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from .config import Settings, get_settings
from .sources import holiday, lunar_info, weather

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"
WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
SHORT_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
GRAY_LEVELS = 16

_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_lock = threading.Lock()


# --- context -------------------------------------------------------------------


def _month_weeks(today: date, holidays: dict[str, dict[str, Any]]) -> list[list[dict[str, Any]]]:
    cal = calendar.Calendar(firstweekday=6)  # Sunday first
    weeks = []
    for week in cal.monthdatescalendar(today.year, today.month):
        row = []
        for d in week:
            h = holidays.get(d.isoformat())
            try:
                label = lunar_info.day_label(d)
            except Exception:  # noqa: BLE001
                label = ""
            row.append(
                {
                    "day": d.day,
                    "other": d.month != today.month,
                    "today": d == today,
                    "label": label,
                    "badge": ("休" if h["off"] else "班") if h else None,
                }
            )
        weeks.append(row)
    return weeks


def _forecast(daily: list[dict[str, Any]], today: date) -> tuple[dict | None, list[dict]]:
    by_date = {d["date"]: d for d in daily}
    today_fc = by_date.get(today.isoformat())
    future = [d for d in daily if d["date"] > today.isoformat()][:3]
    out = []
    for i, d in enumerate(future):
        dd = date.fromisoformat(d["date"])
        label = ["明天", "后天"][i] if i < 2 and (dd - today).days == i + 1 else SHORT_WEEKDAYS[dd.weekday()]
        out.append({**d, "label": label})
    return today_fc, out


def build_context(now: datetime, s: Settings | None = None) -> dict[str, Any]:
    s = s or get_settings()
    today = now.date()
    warnings: list[str] = []

    try:
        lunar = lunar_info.today_info(today)
    except Exception as e:  # noqa: BLE001
        log.error("lunar failed: %s", e)
        lunar = {"date": "", "year": "", "jieqi": "", "next_term": "", "festivals": [], "yi": [], "ji": []}

    holidays = holiday.get_holidays(today.year)
    if not holidays:
        warnings.append("⚠ 节假日数据缺失")

    wx, stale = weather.get_weather(s, today)
    if wx is None:
        warnings.append("⚠ 天气不可用")
    elif stale:
        warnings.append("⚠ 天气数据过期")

    today_fc, forecast = _forecast(wx["daily"], today) if wx else (None, [])

    return {
        "width": s.width,
        "height": s.height,
        "ym": f"{today.year}年{today.month}月",
        "weekday": WEEKDAYS[today.weekday()],
        "day": today.day,
        "lunar": lunar,
        "holiday": holidays.get(today.isoformat()),
        "weather": wx,
        "today_fc": today_fc,
        "forecast": forecast,
        "weeks": _month_weeks(today, holidays),
        "city": s.city_name,
        "updated": now.strftime("%H:%M"),
        "warnings": warnings,
    }


def render_html(ctx: dict[str, Any]) -> str:
    return _env.get_template("dash.html").render(**ctx)


# --- image ---------------------------------------------------------------------


def screenshot(html: str, width: int, height: int) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.set_content(html, wait_until="load")
            page.evaluate("document.fonts.ready")
            return page.screenshot(type="png", full_page=False)
        finally:
            browser.close()


def to_eink(png: bytes, rotate: int) -> tuple[bytes, bytes]:
    """Return (device_png, landscape_preview_png): 8-bit grayscale, 16 levels, no alpha."""
    img = Image.open(io.BytesIO(png)).convert("RGB").convert("L")
    step = 255 / (GRAY_LEVELS - 1)
    img = img.point(lambda v: int(round(round(v / step) * step)))

    preview = io.BytesIO()
    img.save(preview, "PNG", optimize=True)

    device_img = img.rotate(-rotate, expand=True) if rotate else img  # PIL rotates counter-clockwise
    out = io.BytesIO()
    device_img.save(out, "PNG", optimize=True)
    return out.getvalue(), preview.getvalue()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def image_path(s: Settings | None = None) -> Path:
    return (s or get_settings()).out_dir / "dash.png"


def preview_path(s: Settings | None = None) -> Path:
    return (s or get_settings()).out_dir / "preview.png"


def etag_path(s: Settings | None = None) -> Path:
    return (s or get_settings()).out_dir / "dash.etag"


def current_etag(s: Settings | None = None) -> str | None:
    try:
        return etag_path(s).read_text().strip() or None
    except OSError:
        return None


def render_once(now: datetime | None = None, s: Settings | None = None) -> dict[str, Any]:
    """Render and publish a new image. Never removes the previous image on failure."""
    s = s or get_settings()
    now = now or datetime.now(s.tz)
    with _lock:
        try:
            html = render_html(build_context(now, s))
            device_png, preview_png = to_eink(screenshot(html, s.width, s.height), s.rotate)
        except Exception as e:  # noqa: BLE001
            log.exception("render failed, keeping previous image")
            return {"ok": False, "error": str(e)}

        etag = '"' + hashlib.sha256(device_png).hexdigest()[:32] + '"'
        changed = etag != current_etag(s)
        if changed:
            _atomic_write(image_path(s), device_png)
            _atomic_write(etag_path(s), etag.encode())
        _atomic_write(preview_path(s), preview_png)
        log.info("render ok changed=%s etag=%s", changed, etag)
        return {"ok": True, "changed": changed, "etag": etag, "at": now.isoformat()}
