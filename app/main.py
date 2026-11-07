"""HTTP API and scheduler."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from . import render, wakeup
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kindle-calendar")

_state: dict = {"last_render": None}


async def _render_job() -> dict:
    result = await asyncio.to_thread(render.render_once)
    _state["last_render"] = result
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    log.info("starting: provider=%s rotate=%s interval=%smin", s.provider, s.rotate, s.interval_min)
    scheduler = AsyncIOScheduler(timezone=s.tz)
    scheduler.add_job(
        _render_job,
        CronTrigger(minute=f"*/{s.interval_min}", timezone=s.tz),
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
    try:
        return json.loads((get_settings().data_dir / "device.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
    secs = wakeup.seconds_until_wakeup(now, interval, s.night_start, s.night_end)
    return PlainTextResponse(str(secs), headers=_refresh_headers(now))


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


@app.get("/healthz")
def healthz():
    s = get_settings()
    ok = render.image_path(s).exists()
    body = {"ok": ok, "provider": s.provider, "last_render": _state["last_render"], "device": _device_state()}
    return JSONResponse(body, status_code=200 if ok else 503)
