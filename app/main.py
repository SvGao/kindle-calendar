"""HTTP API and scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import device, light, render, wakeup
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kindle-calendar")

_state: dict = {"last_render": None}


async def _render_job() -> dict:
    result = await asyncio.to_thread(render.render_once)
    _state["last_render"] = result
    return result


def _trigger_for(interval_min: int, tz):
    """A cron trigger for the given interval, or an interval one when cron cannot say it.

    Cron minute fields top out at 59, so 90 minutes has no ``*/90`` form; falling
    back to IntervalTrigger keeps the setting honest instead of silently rounding.
    """
    if interval_min < 60:
        return CronTrigger(minute=f"*/{interval_min}", timezone=tz)
    if interval_min % 60 == 0 and 0 < interval_min // 60 < 24:
        return CronTrigger(minute="0", hour=f"*/{interval_min // 60}", timezone=tz)
    return IntervalTrigger(minutes=interval_min, timezone=tz)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    log.info("starting: provider=%s rotate=%s interval=%smin", s.provider, s.rotate, s.interval_min)
    scheduler = AsyncIOScheduler(timezone=s.tz)
    scheduler.add_job(
        _render_job,
        _trigger_for(s.interval_min, s.tz),
        id="render",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.start()
    asyncio.create_task(_render_job())
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="kindle-calendar", lifespan=lifespan)


def check_token(request: Request) -> None:
    token = get_settings().token
    if token and request.query_params.get("token") != token:
        raise HTTPException(status_code=403, detail="bad token")


def _device_state() -> dict:
    return device.read_state()


def _refresh_headers(now: datetime) -> dict[str, str]:
    s = get_settings()
    due = wakeup.full_refresh_due(now, s.night_end, s.interval_min)
    return {"X-Full-Refresh": "1" if due else "0"}


@app.get("/dash.png", dependencies=[Depends(check_token)])
def dash_png(request: Request):
    s = get_settings()
    path = render.image_path(s)
    etag = render.current_etag(s)
    if not path.exists() or not etag:
        raise HTTPException(status_code=503, detail="no image rendered yet")
    headers = {"ETag": etag, "Cache-Control": "no-cache", **_refresh_headers(datetime.now(s.tz))}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/png", headers=headers)


_clock_cache: dict = {"key": None, "png": None, "box": None}


@app.get("/clock.png", dependencies=[Depends(check_token)])
def clock_png(request: Request):
    """Just the HH/MM block, for the device to blit every minute over the poster."""
    s = get_settings()
    now = datetime.now(s.tz)
    key = now.strftime("%H:%M")
    if _clock_cache["key"] != key or _clock_cache["png"] is None:
        png, box = render.render_clock(now, s)
        _clock_cache.update(key=key, png=png, box=box)
    etag = f'"c-{key}"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(_clock_cache["png"], media_type="image/png", headers=headers)


@app.get("/api/clock-box", dependencies=[Depends(check_token)])
def clock_box(fmt: str = "json"):
    """Where the patch goes in framebuffer coordinates.

    ``?fmt=text`` returns ``"x y w h"`` so the device shell script can read it
    with plain ``read`` instead of pulling a JSON parser onto the Kindle.
    """
    s = get_settings()
    # Recompute rather than trust the cache: the poster may have been rendered
    # with a new template since the last clock tick, and a stale rectangle would
    # smear a white patch over the wrong part of the screen.
    _, box = render.render_clock(datetime.now(s.tz), s)
    _clock_cache.update(box=box)
    box = _clock_cache["box"]
    if fmt == "text":
        return PlainTextResponse(f'{box["x"]} {box["y"]} {box["w"]} {box["h"]}')
    return JSONResponse(box)


@app.get("/preview.png", dependencies=[Depends(check_token)])
def preview_png():
    path = render.preview_path()
    if not path.exists():
        raise HTTPException(status_code=503, detail="no image rendered yet")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.get("/preview", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def preview_html():
    s = get_settings()
    return render.render_html(render.build_context(datetime.now(s.tz), s))


@app.api_route("/render", methods=["GET", "POST"], dependencies=[Depends(check_token)])
async def render_now():
    return await _render_job()


@app.get("/api/next-wakeup", response_class=PlainTextResponse, dependencies=[Depends(check_token)])
def next_wakeup():
    s = get_settings()
    now = datetime.now(s.tz)
    interval = s.interval_min
    dev = _device_state()
    battery = dev.get("battery")
    if isinstance(battery, int) and battery < 15 and not dev.get("charging"):
        interval = max(interval, s.low_battery_interval_min)
    secs = wakeup.seconds_until_wakeup(now, interval, s.night_start, s.night_end, s.night_sleep)
    return PlainTextResponse(str(secs), headers=_refresh_headers(now))


@app.get("/api/plan", response_class=PlainTextResponse, dependencies=[Depends(check_token)])
def device_plan():
    """Every policy knob the device loop needs, in one plain-text GET.

    The Kindle is deliberately dumb: it paints what it is given and asks here how
    to behave.  Clock on/off, full-refresh interval, sleep cap, how long to sleep
    and the front-light level all come from this response, so changing any of them
    is a server-side edit -- no new file on the device, no USB cable.

    Format is ``key=value``, one per line, values free of spaces so the device can
    sanity-check each line before assigning it to a shell variable.
    """
    s = get_settings()
    now = datetime.now(s.tz)
    view = _light_view(s)
    dev = _device_state()
    battery = dev.get("battery")
    low = isinstance(battery, int) and battery < device.LOW_BATTERY_PCT and not dev.get("charging")
    interval = max(s.interval_min, s.low_battery_interval_min) if low else s.interval_min
    secs = wakeup.seconds_until_wakeup(now, interval, s.night_start, s.night_end, s.night_sleep)

    rows = {
        "version": 1,
        "clock": int(s.clock_enabled),    # per-minute HH:MM patch
        "light": 1,                       # poll /api/light every minute
        "full_refresh_every": s.full_refresh_every,  # safety net after N partial updates
        "max_sleep_s": 1800,              # cap while backing off from failures
        "sleep_s": secs,                  # how long to wait before the next poster
        "poster_interval": interval,      # minutes, after battery adjustment
        "night": int(wakeup.is_night(now.time(), s.night_start, s.night_end)),
        "night_start": s.night_start.strftime("%H:%M"),
        "night_end": s.night_end.strftime("%H:%M"),
        "night_sleep": int(s.night_sleep),
        "light_level": view["effective"],
        "light_slot": view["slot"] or "-",
        "battery_low": int(low),
    }
    body = "".join(f"{k}={v}\n" for k, v in rows.items())
    return PlainTextResponse(body, headers=_refresh_headers(now))


def _int(v) -> int | None:
    try:
        return int(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


@app.post("/api/device", dependencies=[Depends(check_token)])
async def device_report(request: Request):
    data = dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        data.update(await request.json())
    elif "form" in ctype:
        data.update(dict(await request.form()))
    data.pop("token", None)
    state = {
        "battery": _int(data.get("battery")),
        "charging": str(data.get("charging", "")).strip() in ("1", "true", "yes"),
        "wifi_rssi": _int(data.get("wifi_rssi")),
        "seen_at": datetime.now(get_settings().tz).isoformat(),
    }
    (get_settings().data_dir / "device.json").write_text(json.dumps(state), encoding="utf-8")
    return state


@app.post("/api/log", dependencies=[Depends(check_token)])
async def put_log(request: Request):
    """The device pushes its dash.log here so we never need USB access again."""
    body = (await request.body()).decode("utf-8", "replace")
    path = get_settings().data_dir / "device.log"
    path.write_text(body[-20000:], encoding="utf-8")
    return {"ok": True, "bytes": len(body), "at": datetime.now(get_settings().tz).isoformat()}


@app.get("/api/log", response_class=PlainTextResponse, dependencies=[Depends(check_token)])
def get_log():
    path = get_settings().data_dir / "device.log"
    if not path.exists():
        return PlainTextResponse("", headers={"Cache-Control": "no-cache"})
    return PlainTextResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


def _light_view(s) -> dict:
    """What the device should be at right now, plus why.

    ``LIGHT_SCHEDULE`` maps times to levels (``23:00=3,06:00=user``).  The device
    polls this every minute, so the ramp needs no timer of its own and changing it
    is a server-side edit -- nothing to copy onto the Kindle.
    """
    state = light.read(s.data_dir)
    now = datetime.now(s.tz)
    night = wakeup.is_night(now.time(), s.night_start, s.night_end)
    auto = s.light_night_auto if state["night_auto"] is None else state["night_auto"]
    if auto:
        effective, slot = light.pick(s.light_schedule, now.time(), state["level"])
    else:
        effective, slot = state["level"], None
    return {**state, "effective": effective, "night": night, "night_auto": auto,
            "night_level": s.night_light_level, "dimmed": effective != state["level"],
            "slot": slot.strftime("%H:%M") if slot else None}


@app.get("/api/light", dependencies=[Depends(check_token)])
def light_get(fmt: str = "json"):
    """Front-light level the device should be at.

    ``?fmt=text`` returns ``"<effective> <applied>"`` -- two integers, no JSON, so
    the Kindle shell loop can read it with a plain ``set --``.
    """
    s = get_settings()
    view = _light_view(s)
    if fmt == "text":
        applied = view["applied"] if isinstance(view["applied"], int) else -1
        return PlainTextResponse(f'{view["effective"]} {applied}')
    return JSONResponse({**view, "device": _device_state()})


@app.post("/api/light", dependencies=[Depends(check_token)])
async def light_set(request: Request):
    """Set the level from the phone page: ``level=12`` absolute or ``step=-2``."""
    data = dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        data.update(await request.json())
    elif "form" in ctype:
        data.update(dict(await request.form()))
    data.pop("token", None)

    s = get_settings()
    state = light.read(s.data_dir)
    changes: dict = {}
    if str(data.get("night_auto", "")).strip() != "":
        changes["night_auto"] = str(data["night_auto"]).strip().lower() in ("1", "true", "yes", "on")
    if str(data.get("step", "")).strip():
        try:
            changes["level"] = state["level"] + int(str(data["step"]).strip())
        except (TypeError, ValueError):
            pass
    elif str(data.get("level", "")).strip():
        changes["level"] = light.clamp(data["level"])
    if "level" in changes or "night_auto" in changes:
        changes["updated"] = light.now_iso()
    light.write(s.data_dir, **changes)
    # Return the resolved view, not the raw state: the page needs `effective`
    # (which a schedule may override) to paint the right number and badge.
    return JSONResponse(_light_view(s))


@app.post("/api/light/applied", dependencies=[Depends(check_token)])
async def light_applied(request: Request):
    """The device reports what it actually set, so the page can stop saying "pending"."""
    data = dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        data.update(await request.json())
    elif "form" in ctype:
        data.update(dict(await request.form()))
    s = get_settings()
    value = data.get("level")
    try:
        applied = int(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad level")
    return JSONResponse(light.write(s.data_dir, applied=applied, applied_at=light.now_iso()))


_LIGHT_PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>前光亮度</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  body { font-family: -apple-system, "PingFang SC", "Noto Sans CJK SC", sans-serif;
         background: #f4f4f2; color: #111; padding: 24px 18px 44px; max-width: 460px; margin: 0 auto; }
  h1 { font-size: 16px; font-weight: 600; letter-spacing: .1em; color: #666; }
  .val { display: flex; align-items: baseline; gap: 8px; margin: 12px 0 4px; }
  .val b { font-size: 78px; line-height: 1; font-weight: 800; letter-spacing: -2px; }
  .val span { font-size: 19px; color: #999; }
  .badge { display: inline-block; margin-left: 10px; font-size: 13px; font-weight: 700; letter-spacing: .04em;
           border: 1.5px solid #111; border-radius: 999px; padding: 3px 10px; vertical-align: 6px; }
  .bar { display: flex; gap: 12px; margin: 20px 0; }
  .bar button { flex: 1; height: 68px; font-size: 32px; font-weight: 700; border: 2px solid #111;
                background: #fff; border-radius: 14px; color: #111; }
  .bar button:active { background: #111; color: #fff; }
  input[type=range] { width: 100%; height: 46px; accent-color: #111; background: transparent; }
  .presets { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-top: 8px; }
  .presets button { height: 56px; font-size: 20px; font-weight: 700; border-radius: 12px;
                    border: 2px solid #d4d4d0; background: #fff; color: #111; }
  .presets button.on { border-color: #111; background: #111; color: #fff; }
  .sw { display: flex; align-items: center; gap: 10px; margin: 24px 0 0; font-size: 15px; color: #333; }
  .sw input { width: 22px; height: 22px; accent-color: #111; }
  .plan { font-size: 13px; color: #9a9a94; margin: 8px 0 0 32px; line-height: 1.6; }
  .status { margin-top: 24px; font-size: 15px; line-height: 1.75; color: #555;
            border-top: 1px solid #dddbd6; padding-top: 16px; }
  .status b { color: #111; }
  .hint { font-size: 13px; color: #9a9a94; margin-top: 10px; line-height: 1.65; }
</style></head>
<body>
<h1>KINDLE 前光亮度</h1>
<div class="val"><b>__LEVEL__</b><span>/ 24</span>__BADGE__</div>
<div class="bar">
  <button onclick="setLevel(__MINUS__)">−</button>
  <button onclick="setLevel(__PLUS__)">+</button>
</div>
<input type="range" min="1" max="24" step="1" value="__LEVEL__" onchange="setLevel(this.value)">
<div class="presets">__PRESETS__</div>
<label class="sw"><input type="checkbox" __CHECKED__ onchange="setNight(this.checked)">按时段自动调光</label>
<div class="plan">__PLAN__</div>
<div class="status">__STATUS__</div>
<div class="hint">改完最多 1 分钟生效 —— 设备本来就每分钟醒一次贴时钟。</div>
<script>
async function post(data) {
  const f = new FormData();
  for (const k in data) f.append(k, data[k]);
  await fetch('/api/light', { method: 'POST', body: f });
  location.reload();
}
function setLevel(v) {
  v = Math.max(1, Math.min(24, parseInt(v, 10) || 12));
  post({ level: v });
}
function setNight(on) { post({ night_auto: on ? 1 : 0 }); }
</script>
</body></html>"""


def _status_line(s, view) -> str:
    """One sentence about where the screen actually is, versus where it should be."""
    applied = view["applied"]
    effective = view["effective"]
    dev = _device_state()
    seen = _short_time(dev.get("seen_at"))
    dimmed = view["dimmed"] and view["night_auto"]
    slot = view["slot"]

    if _minutes_since(dev.get("seen_at")) > 10:
        return (f"<b>设备离线</b>　最后一次上线 {seen}<br>"
                f"它联网后的下一分钟内会拉到 <b>{effective}</b>")
    if applied is None:
        return f"<b>等待设备拉取</b><br>设备上一次上线：{seen}"
    if applied == effective:
        where = f"（{slot} 起自动）" if dimmed else ""
        return f"设备已应用 <b>{applied}</b>{where}　{_short_time(view.get('applied_at'))}"
    return (f"设备当前 <b>{applied}</b>，目标 <b>{effective}</b>，等待下一分钟拉取"
            f"<br>设备上一次上线：{seen}")


@app.get("/light", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def light_page():
    """Brightness for the Kindle front light, plus the schedule it follows."""
    s = get_settings()
    view = _light_view(s)
    level = view["level"]
    effective = view["effective"]
    slot = view["slot"]

    presets = "".join(
        f'<button class="{"on" if v == level else ""}" onclick="setLevel({v})">{v}</button>'
        for v in (1, 5, 10, 16, 24)
    )
    badge = f'<span class="badge">{slot} 起 {effective}</span>' if view["dimmed"] and slot else ""
    plan = " · ".join(
        f'{at.strftime("%H:%M")} → {level if lv is None else lv}' for at, lv in s.light_schedule
    ) or "未设置时段，一直用上面的值"

    html = (
        _LIGHT_PAGE.replace("__LEVEL__", str(level))
        .replace("__MINUS__", str(max(1, level - 2)))
        .replace("__PLUS__", str(min(24, level + 2)))
        .replace("__PRESETS__", presets)
        .replace("__BADGE__", badge)
        .replace("__PLAN__", plan)
        .replace("__CHECKED__", "checked" if view["night_auto"] else "")
        .replace("__STATUS__", _status_line(s, view))
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _short_time(value) -> str:
    """``2026-11-08T22:51:00+08:00`` -> ``22:51``; unknown -> a dash."""
    if not value:
        return "—"
    text = str(value)
    return text[11:16] if len(text) >= 16 else text


def _minutes_since(value) -> float:
    """Age of a timestamp in minutes; a huge number when it is missing or unparsable."""
    if not value:
        return 1e9
    try:
        then = datetime.fromisoformat(str(value))
    except ValueError:
        return 1e9
    return (datetime.now(get_settings().tz) - then).total_seconds() / 60


@app.get("/healthz")
def healthz():
    s = get_settings()
    ok = render.image_path(s).exists()
    body = {"ok": ok, "provider": s.provider, "last_render": _state["last_render"], "device": _device_state()}
    return JSONResponse(body, status_code=200 if ok else 503)
