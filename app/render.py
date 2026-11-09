"""Build the page context, render HTML, screenshot it, and post-process for e-ink."""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import math
import os
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from . import device, events, quotes, store
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


def _month_weeks(
    today: date, holidays: dict[str, dict[str, Any]], marks: dict[str, str] | None = None
) -> list[list[dict[str, Any]]]:
    marks = marks or {}
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
            mark = marks.get(d.isoformat())
            row.append(
                {
                    "day": d.day,
                    "other": d.month != today.month,
                    "today": d == today,
                    "label": mark or label,
                    "event": bool(mark),
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

    battery = device.battery_info(now, s)

    prefs = store.effective(s)
    anniversaries = store.read(s)["anniversaries"]
    event_tag = None
    try:
        near = events.upcoming(anniversaries, today, prefs["event_days"])
        if near:
            event_tag = {"text": events.tag_text(near[0]), "today": near[0]["days"] == 0}
        grid = cal_range(today)
        marks = events.by_date(anniversaries, grid[0], grid[1])
    except Exception as e:  # noqa: BLE001
        log.error("anniversaries failed: %s", e)
        marks = {}

    nowcast = weather.get_nowcast(s) if wx else None

    return {
        "width": s.width,
        "height": s.height,
        "ym": f"{today.year}年{today.month}月",
        "weekday": WEEKDAYS[today.weekday()],
        "day": today.day,
        "hh": now.strftime("%H"),
        "mm": now.strftime("%M"),
        "lunar": lunar,
        "holiday": holidays.get(today.isoformat()),
        "weather": wx,
        "today_fc": today_fc,
        "forecast": forecast,
        "weeks": _month_weeks(today, holidays, marks),
        "event_tag": event_tag,
        "nowcast": nowcast,
        "city": s.city_name,
        "updated": now.strftime("%H:%M"),
        "device_line": battery["text"] if battery else "",
        "battery_low": bool(battery and battery["low"]),
        "poem": quotes.pick(now, lunar.get("festivals") or ()) if prefs["show_poem"] else None,
        "warnings": warnings,
    }


def cal_range(today: date) -> tuple[date, date]:
    """First and last date shown in the month grid."""
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(today.year, today.month)
    return weeks[0][0], weeks[-1][-1]


def render_html(ctx: dict[str, Any]) -> str:
    return _env.get_template("dash.html").render(**ctx)


# --- image ---------------------------------------------------------------------


class _Chromium:
    """One long-lived headless Chromium, driven from a single dedicated thread.

    Launching Chromium costs 1-2 s of CPU on a desktop and more on a NAS; the
    clock patch alone used to pay that every minute.  Playwright's sync API is
    bound to the thread that started it, so every call is funnelled through a
    one-worker executor.  A crash or timeout throws the browser away and the next
    call starts a fresh one; it is also recycled every RECYCLE_AFTER uses so a
    slow memory leak can never build up.
    """

    RECYCLE_AFTER = 500

    def __init__(self) -> None:
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chromium")
        self._pw = None
        self._browser = None
        self._uses = 0

    def _close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = None
        self._uses = 0

    def _page(self, width: int, height: int):
        if self._browser is None or not self._browser.is_connected() or self._uses >= self.RECYCLE_AFTER:
            self._close()
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        self._uses += 1
        return self._browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)

    def _call(self, fn: Callable[[Any], Any], width: int, height: int):
        for attempt in (1, 2):
            page = None
            try:
                page = self._page(width, height)
                return fn(page)
            except Exception:
                if page is not None:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
                    page = None
                self._close()
                if attempt == 2:
                    raise
                log.warning("chromium call failed, retrying with a fresh browser", exc_info=True)
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise AssertionError("unreachable")

    def run(self, fn: Callable[[Any], Any], width: int, height: int, timeout: float = 600):
        return self._ex.submit(self._call, fn, width, height).result(timeout=timeout)


CHROMIUM = _Chromium()


def _load(page, html: str) -> None:
    page.set_content(html, wait_until="load")
    page.evaluate("document.fonts.ready")


def screenshot(html: str, width: int, height: int) -> bytes:
    def shoot(page):
        _load(page, html)
        return page.screenshot(type="png", full_page=False)

    return CHROMIUM.run(shoot, width, height)


CLOCK_PAD = 4  # bled around the .clock box so rounding never leaves a seam


def _clock_box(page, width: int, height: int) -> dict[str, int]:
    el = page.query_selector(".clock")
    if el is None:
        raise RuntimeError("clock box missing from template")
    b = el.bounding_box()
    if b is None:
        raise RuntimeError("clock box has no layout box")
    # Layout boxes are fractional; the framebuffer is not.  Snap outwards to whole
    # pixels and bleed a few pixels of the surrounding white so a 1px seam can
    # never show up next to the digits.
    box = {"x": max(0, math.floor(b["x"]) - CLOCK_PAD), "y": max(0, math.floor(b["y"]) - CLOCK_PAD)}
    box["width"] = min(width, math.ceil(b["x"] + b["width"]) + CLOCK_PAD) - box["x"]
    box["height"] = min(height, math.ceil(b["y"] + b["height"]) + CLOCK_PAD) - box["y"]
    return box


def screenshot_clock(html: str, width: int, height: int) -> tuple[bytes, dict[str, int]]:
    """Screenshot only the .clock box, plus its position in CSS pixels.

    Rendering from the *same* page as the poster (rather than a font-matched
    re-implementation) is the only way to guarantee the per-minute patch and the
    15-minute poster produce byte-identical glyphs -- otherwise the digits would
    visibly jitter every time one replaces the other.
    """

    def shoot(page):
        _load(page, html)
        box = _clock_box(page, width, height)
        return page.screenshot(type="png", clip=box), box

    return CHROMIUM.run(shoot, width, height)


def clock_device_rect(box: dict[str, float], s: Settings) -> dict[str, int]:
    """Map the landscape clock box into framebuffer coordinates of the device.

    ``to_eink`` rotates the landscape poster clockwise by ``rotate`` degrees, so
    for the default 90 a landscape point (x, y) lands at (H - y, x) on the device.
    """
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["width"], y0 + box["height"]
    W, H = s.width, s.height
    if s.rotate == 90:
        rect = (H - y1, x0, y1 - y0, x1 - x0)
    elif s.rotate == 180:
        rect = (W - x1, H - y1, x1 - x0, y1 - y0)
    elif s.rotate == 270:
        rect = (y0, W - x1, y1 - y0, x1 - x0)
    else:
        rect = (x0, y0, x1 - x0, y1 - y0)
    return dict(zip(("x", "y", "w", "h"), (int(round(v)) for v in rect)))


def render_clock(now: datetime, s: Settings | None = None) -> tuple[bytes, dict[str, int]]:
    """Render the clock patch alone, rotated to device orientation."""
    s = s or get_settings()
    html = render_html(build_context(now, s))
    patch, box = screenshot_clock(html, s.width, s.height)
    return to_eink(patch, s.rotate)[0], clock_device_rect(box, s)


# --- clock pack: all 1440 minutes, so the device can draw without the network ---


def clock_pack_key(s: Settings | None = None) -> str:
    """Changes whenever anything that shapes the digits changes."""
    s = s or get_settings()
    h = hashlib.sha256()
    for name in ("dash.html", "icons.html"):
        h.update((TEMPLATES / name).read_bytes())
    h.update(f"{s.width}x{s.height}r{s.rotate}g{GRAY_LEVELS}".encode())
    return h.hexdigest()[:12]


def clock_pack_path(s: Settings | None = None) -> Path:
    s = s or get_settings()
    return s.out_dir / f"clockpack-{clock_pack_key(s)}.tar"


_pack_lock = threading.Lock()


def build_clock_pack(s: Settings | None = None) -> Path:
    """Render every HH:MM patch into one tar of ``HHMM.png`` files (a few MB).

    The device downloads it once and from then on draws the minute locally, so
    the clock keeps running when the NAS or the Wi-Fi is down, and the NAS no
    longer renders anything per minute.  The patch content depends only on the
    clock's styling (the bleed around it is page background), so one pack serves
    every day; only the blit position, which /api/clock-box reports, can move.
    """
    s = s or get_settings()
    path = clock_pack_path(s)
    with _pack_lock:
        if path.exists():
            return path
        started = datetime.now()
        html = render_html(build_context(datetime.now(s.tz), s))

        def shoot_all(page):
            _load(page, html)
            box = _clock_box(page, s.width, s.height)
            shots = []
            for h in range(24):
                for m in range(60):
                    page.evaluate("t => { document.querySelector('.clock').textContent = t }", f"{h:02d}:{m:02d}")
                    shots.append((f"{h:02d}{m:02d}.png", page.screenshot(type="png", clip=box)))
            return shots

        shots = CHROMIUM.run(shoot_all, s.width, s.height, timeout=1800)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, png in shots:
                data = to_eink(png, s.rotate)[0]
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = int(started.timestamp())
                tar.addfile(info, io.BytesIO(data))
        for old in s.out_dir.glob("clockpack-*.tar"):
            old.unlink(missing_ok=True)
        _atomic_write(path, buf.getvalue())
        log.info(
            "clock pack %s built in %.0fs, %d bytes",
            path.name,
            (datetime.now() - started).total_seconds(),
            path.stat().st_size,
        )
        return path


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
