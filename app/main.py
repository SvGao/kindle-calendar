"""HTTP API, web console and scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import cache, device, events, light, notify, render, store, wakeup
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kindle-calendar")

_state: dict = {"last_render": None, "pack_building": False}


async def _render_job() -> dict:
    started = datetime.now()
    result = await asyncio.to_thread(render.render_once)
    result["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    _state["last_render"] = result
    return result


async def _pack_job() -> None:
    """Build the 1440-minute clock pack if the current one is missing."""
    s = get_settings()
    if render.clock_pack_path(s).exists() or _state["pack_building"]:
        return
    _state["pack_building"] = True
    try:
        await asyncio.to_thread(render.build_clock_pack, s)
    except Exception:  # noqa: BLE001
        log.exception("clock pack build failed")
    finally:
        _state["pack_building"] = False


async def _notify_job() -> None:
    try:
        sent = await asyncio.to_thread(notify.check)
        for title, _ in sent:
            log.info("push sent: %s", title)
    except Exception:  # noqa: BLE001
        log.exception("notify check failed")


async def _startup() -> None:
    await _render_job()
    await _pack_job()


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
    scheduler.add_job(_notify_job, IntervalTrigger(minutes=5, timezone=s.tz), id="notify", max_instances=1)
    scheduler.start()
    asyncio.create_task(_startup())
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="kindle-calendar", lifespan=lifespan)


def check_token(request: Request) -> None:
    token = get_settings().token
    if token and request.query_params.get("token") != token:
        raise HTTPException(status_code=403, detail="bad token")


async def _body(request: Request) -> dict:
    """Query string, then a JSON or form body on top. ``token`` is dropped."""
    data: dict = dict(request.query_params)
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        payload = await request.json()
        if isinstance(payload, dict):
            data.update(payload)
    elif "form" in ctype:
        data.update(dict(await request.form()))
    data.pop("token", None)
    return data


# --- forced full refresh (console button) ------------------------------------


def _force_path():
    return get_settings().data_dir / "force_full"


def _force_pending() -> bool:
    return _force_path().exists()


def _poster_ver() -> str:
    etag = render.current_etag(get_settings()) or ""
    return etag.strip('"')[:8] or "0"


def _refresh_headers(now: datetime) -> dict[str, str]:
    s = get_settings()
    due = wakeup.full_refresh_due(now, s.night_end, s.interval_min) or _force_pending()
    return {"X-Full-Refresh": "1" if due else "0"}


# --- device endpoints ----------------------------------------------------------


@app.get("/dash.png", dependencies=[Depends(check_token)])
def dash_png(request: Request):
    s = get_settings()
    path = render.image_path(s)
    etag = render.current_etag(s)
    if not path.exists() or not etag:
        raise HTTPException(status_code=503, detail="no image rendered yet")
    headers = {"ETag": etag, "Cache-Control": "no-cache", **_refresh_headers(datetime.now(s.tz))}
    _force_path().unlink(missing_ok=True)  # the flag is delivered with this response
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/png", headers=headers)


_clock_cache: dict = {"key": None, "png": None, "box": None}


@app.get("/clock.png", dependencies=[Depends(check_token)])
def clock_png(request: Request):
    """Just the HH:MM block, for devices without the clock pack."""
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
    # with a new layout since the last tick, and a stale rectangle would smear a
    # white patch over the wrong part of the screen.
    _, box = render.render_clock(datetime.now(s.tz), s)
    _clock_cache.update(box=box)
    if fmt == "text":
        return PlainTextResponse(f'{box["x"]} {box["y"]} {box["w"]} {box["h"]}')
    return JSONResponse(box)


@app.get("/clock-pack.tar", dependencies=[Depends(check_token)])
async def clock_pack():
    """All 1440 clock patches as ``HHMM.png`` in one tar, for offline ticking."""
    path = render.clock_pack_path(get_settings())
    if not path.exists():
        asyncio.create_task(_pack_job())
        raise HTTPException(status_code=503, detail="clock pack is being built, try again later")
    return FileResponse(path, media_type="application/x-tar", headers={"Cache-Control": "no-cache"})


@app.get("/api/next-wakeup", response_class=PlainTextResponse, dependencies=[Depends(check_token)])
def next_wakeup():
    s = get_settings()
    now = datetime.now(s.tz)
    interval = s.interval_min
    dev = device.read_state()
    battery = dev.get("battery")
    if isinstance(battery, int) and battery < 15 and not dev.get("charging"):
        interval = max(interval, s.low_battery_interval_min)
    secs = wakeup.seconds_until_wakeup(
        now, interval, s.night_start, s.night_end, store.effective(s)["night_sleep"]
    )
    return PlainTextResponse(str(secs), headers=_refresh_headers(now))


@app.get("/api/plan", response_class=PlainTextResponse, dependencies=[Depends(check_token)])
def device_plan():
    """Every policy knob the device loop needs, in one plain-text GET.

    The Kindle is deliberately dumb: it paints what it is given and asks here how
    to behave.  Format is ``key=value``, one per line, values free of spaces so the
    device can sanity-check each line before assigning it to a shell variable.
    """
    s = get_settings()
    now = datetime.now(s.tz)
    device.touch(now)
    prefs = store.effective(s)
    view = _light_view(s, now)
    dev = device.read_state()
    battery = dev.get("battery")
    low = isinstance(battery, int) and battery < device.LOW_BATTERY_PCT and not dev.get("charging")
    interval = max(s.interval_min, s.low_battery_interval_min) if low else s.interval_min
    secs = wakeup.seconds_until_wakeup(now, interval, s.night_start, s.night_end, prefs["night_sleep"])
    pack = render.clock_pack_path(s)

    rows = {
        "version": 2,
        "clock": int(prefs["clock_enabled"]),  # per-minute HH:MM patch
        "clock_pack": render.clock_pack_key(s) if pack.exists() else "-",
        "clock_flash": int(prefs["clock_flash"]),  # flash the clock strip on the hour
        "light": 1,  # poll /api/light every minute
        "full_refresh_every": s.full_refresh_every,  # safety net after N partial updates
        "max_sleep_s": 1800,  # cap while backing off from failures
        "sleep_s": secs,  # how long to wait before the next poster
        "poster_interval": interval,  # minutes, after battery adjustment
        "night": int(wakeup.is_night(now.time(), s.night_start, s.night_end)),
        "night_start": s.night_start.strftime("%H:%M"),
        "night_end": s.night_end.strftime("%H:%M"),
        "night_sleep": int(prefs["night_sleep"]),
        "light_level": view["effective"],
        "light_slot": view["slot"] or "-",
        "battery_low": int(low),
        # Time sync: the device sets its clock from these, so the minute it draws
        # is the server's minute.  tz_offset_s lets it compute local HH:MM itself
        # without trusting whatever timezone the Kindle shell happens to have.
        "epoch": int(now.timestamp()),
        "tz_offset_s": int(now.utcoffset().total_seconds()),
        "utc": now.astimezone(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S"),
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
    data = await _body(request)
    s = get_settings()
    state = {
        "battery": _int(data.get("battery")),
        "charging": str(data.get("charging", "")).strip() in ("1", "true", "yes"),
        "wifi_rssi": _int(data.get("wifi_rssi")),
        "seen_at": datetime.now(s.tz).isoformat(),
    }
    (s.data_dir / "device.json").write_text(json.dumps(state), encoding="utf-8")
    device.touch(datetime.now(s.tz))
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
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return PlainTextResponse(text, headers={"Cache-Control": "no-cache"})


# --- front light -----------------------------------------------------------------


def _schedule(s, state):
    """Console schedule if one was saved, otherwise LIGHT_SCHEDULE from .env."""
    return (state["schedule"], "console") if state["schedule"] is not None else (s.light_schedule, "env")


def _light_view(s, now: datetime | None = None) -> dict:
    """What the device should be at right now, plus why."""
    now = now or datetime.now(s.tz)
    state = light.read(s.data_dir)
    schedule, source = _schedule(s, state)
    auto = s.light_night_auto if state["night_auto"] is None else state["night_auto"]
    hold = auto and light.held(state, now)
    if auto and not hold:
        effective, slot = light.pick(schedule, now.time(), state["level"])
    else:
        effective, slot = state["level"], None
    nxt = light.next_boundary(schedule, now) if auto else None
    return {
        **{k: v for k, v in state.items() if k != "schedule"},
        "effective": effective,
        "night_auto": auto,
        "hold": hold,
        "dimmed": effective != state["level"],
        "slot": slot.strftime("%H:%M") if slot else None,
        "next_change": nxt.strftime("%H:%M") if nxt else None,
        "schedule": light.schedule_to_json(schedule),
        "schedule_source": source,
    }


@app.get("/api/light", dependencies=[Depends(check_token)])
def light_get(fmt: str = "json"):
    """Front-light level the device should be at.

    ``?fmt=text`` returns ``"<effective> <applied> <poster> <full>"``: the level,
    what the device last applied, a short id of the current poster and whether a
    full refresh is pending.  The device polls this every minute, so a changed
    poster id lets it fetch a new poster right away instead of at the next slot.
    """
    s = get_settings()
    now = datetime.now(s.tz)
    device.touch(now)
    view = _light_view(s, now)
    if fmt == "text":
        applied = view["applied"] if isinstance(view["applied"], int) else -1
        return PlainTextResponse(f'{view["effective"]} {applied} {_poster_ver()} {int(_force_pending())}')
    return JSONResponse({**view, "device": device.read_state()})


@app.post("/api/light", dependencies=[Depends(check_token)])
async def light_set(request: Request):
    """Set the light from the console.

    ``level=12`` absolute or ``step=-2``; ``night_auto=0|1``; ``resume=1`` drops a
    manual hold; ``schedule=[["23:00",3],["06:00",null]]`` replaces the schedule
    (``null`` resets it to LIGHT_SCHEDULE from .env).
    """
    data = await _body(request)
    s = get_settings()
    now = datetime.now(s.tz)
    state = light.read(s.data_dir)
    changes: dict = {}
    if str(data.get("night_auto", "")).strip() != "":
        changes["night_auto"] = str(data["night_auto"]).strip().lower() in ("1", "true", "yes", "on")
        changes["hold_until"] = None
    if "schedule" in data:
        sched = data["schedule"]
        if isinstance(sched, str):
            try:
                sched = json.loads(sched)
            except ValueError:
                raise HTTPException(status_code=400, detail="bad schedule") from None
        changes["schedule"] = sched
        changes["hold_until"] = None
    level = None
    if str(data.get("step", "")).strip():
        try:
            level = light.clamp(_light_view(s, now)["effective"] + int(str(data["step"]).strip()))
        except (TypeError, ValueError):
            pass
    elif str(data.get("level", "")).strip():
        level = light.clamp(data["level"])
    if level is not None:
        changes["level"] = level
        # While the schedule runs, a manual change wins until the next slot starts;
        # otherwise moving the slider at night would appear to do nothing.
        # During a "my level" slot the new value simply is the user's level.
        view = _light_view(s, now)
        if view["night_auto"] and (view["hold"] or view["dimmed"]):
            schedule, _ = _schedule(s, state)
            nxt = light.next_boundary(schedule, now)
            changes["hold_until"] = nxt.isoformat() if nxt else None
        else:
            changes["hold_until"] = None
    if str(data.get("resume", "")).strip() in ("1", "true"):
        changes["hold_until"] = None
    if changes:
        changes["updated"] = light.now_iso()
    light.write(s.data_dir, **changes)
    return JSONResponse(_light_view(s, now))


@app.post("/api/light/applied", dependencies=[Depends(check_token)])
async def light_applied(request: Request):
    """The device reports what it actually set, so the console can stop saying "pending"."""
    data = await _body(request)
    s = get_settings()
    try:
        applied = int(str(data.get("level")).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bad level") from None
    state = light.write(s.data_dir, applied=applied, applied_at=light.now_iso())
    return JSONResponse({k: v for k, v in state.items() if k != "schedule"})


# --- web console -----------------------------------------------------------------


@app.get("/", include_in_schema=False)
def root(request: Request):
    return RedirectResponse(f"/console?{request.url.query}" if request.url.query else "/console")


@app.get("/light", include_in_schema=False)
def light_page(request: Request):
    """Old phone page; the console has replaced it."""
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/console{q}#light")


@app.get("/console", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def console_page():
    # Plain file, not a Jinja template: the page fills itself from /api/console,
    # and its JS is full of braces Jinja would try to parse.
    html = (Path(__file__).parent / "templates" / "console.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


def _ago(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return round((now - dt).total_seconds() / 60, 1)


@app.get("/api/console", dependencies=[Depends(check_token)])
def console_state():
    """Everything the console shows, in one call."""
    s = get_settings()
    now = datetime.now(s.tz)
    dev = device.read_state()
    contact = device.last_contact(now, s)
    minutes = _ago(contact, now)
    data = store.read(s)
    prefs = store.effective(s)
    wx, wx_at = cache.read("weather")
    return {
        "now": now.isoformat(timespec="seconds"),
        "city": s.city_name,
        "device": {
            "online": minutes is not None and minutes <= 5,
            "minutes": minutes,
            "last_contact": contact.isoformat(timespec="seconds") if contact else None,
            "battery": dev.get("battery"),
            "charging": dev.get("charging"),
            "report_at": dev.get("seen_at"),
        },
        "render": _state["last_render"],
        "pack": {"ready": render.clock_pack_path(s).exists(), "building": _state["pack_building"]},
        "full_refresh_pending": _force_pending(),
        "weather": {
            "provider": s.provider,
            "fetched_at": wx_at.astimezone(s.tz).isoformat(timespec="minutes") if wx_at else None,
            "minutes": round(cache.age_minutes(wx_at)) if wx_at else None,
            "now": (wx or {}).get("now"),
        },
        "light": _light_view(s, now),
        "settings": {
            **prefs,
            "clock_enabled_env": s.clock_enabled,
            "night_sleep_env": s.night_sleep,
            "night_start": s.night_start.strftime("%H:%M"),
            "night_end": s.night_end.strftime("%H:%M"),
            "interval_min": s.interval_min,
        },
        "anniversaries": data["anniversaries"],
        "upcoming": events.upcoming(data["anniversaries"], now.date()),
        "notify": {**data["notify"], "url": store.masked_url(data["notify"]["url"])},
    }


@app.post("/api/settings", dependencies=[Depends(check_token)])
async def settings_set(request: Request):
    data = await _body(request)
    store.update(data)
    asyncio.create_task(_render_job())  # so the change shows up on the next device poll
    return console_state()


@app.post("/api/full-refresh", dependencies=[Depends(check_token)])
async def full_refresh():
    """Ask the device for one flashing repaint; it notices within a minute."""
    _force_path().touch()
    return {"ok": True}


@app.post("/api/notify/test", dependencies=[Depends(check_token)])
async def notify_test():
    cfg = store.read()["notify"]
    try:
        await asyncio.to_thread(notify.send, cfg, "Kindle 日历测试", "如果你看到这条消息，推送就配置好了。")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True}


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


@app.get("/healthz")
def healthz():
    s = get_settings()
    ok = render.image_path(s).exists()
    body = {"ok": ok, "provider": s.provider, "last_render": _state["last_render"], "device": device.read_state()}
    return JSONResponse(body, status_code=200 if ok else 503)
