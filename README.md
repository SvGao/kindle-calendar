# kindle-calendar

E-ink calendar server for a jailbroken Kindle Paperwhite 3. It renders a landscape dashboard
(date, Chinese lunar calendar, solar terms, public holidays and make-up workdays, weather) to an
8-bit grayscale PNG that the Kindle fetches and shows with FBInk.

```
NAS (Docker)                                    Kindle PW3 (always plugged in)
scheduler every 15 min                          dash.sh loop
  data -> Jinja2 HTML -> Playwright screenshot    GET /dash.png (If-None-Match)
  -> grayscale, 16 levels, rotate -> dash.png     200 -> fbink REAGL (no flash)
FastAPI: /dash.png /api/next-wakeup ...           GET /api/next-wakeup -> sleep N
  same page -> clip the .clock box -> /clock.png  every minute: GET /clock.png
  /api/clock-box -> framebuffer rect                -> fbink REAGL at that rect
```

## Features

- Landscape 1448x1072 layout, rotated server-side to the Kindle framebuffer (1072x1448).
- Weather: QWeather (now, 7-day, air quality, alerts) when a key is set, otherwise Open-Meteo (no key).
- Lunar date, GanZhi year, solar terms, traditional festivals, daily 宜/忌 via `lunar-python`.
- Public holidays and make-up workdays from [holiday-cn](https://github.com/NateScarlet/holiday-cn).
- Every source is cached separately under `/data/cache`; a failed source never blanks the image.
- ETag on `/dash.png` so the Kindle only redraws when the image changed.
- Live clock: the HH:MM block is rendered from the same page as the poster (`/clock.png`, byte-identical
  glyphs) and blitted into the rectangle from `/api/clock-box` once a minute -- only that strip is
  repainted, so the rest of the screen never flickers.
- Night window `NIGHT_START`-`NIGHT_END` for the light schedule, plus a morning full-refresh hint to
  clear ghosting. The clock keeps ticking through it (see [Night](#night)).

## Run on fnOS (Docker Compose)

1. Copy this folder to the NAS, for example `/vol1/1000/docker/kindle-calendar`.
2. `cp .env.example .env` and edit it (QWeather key and API Host are optional).
3. In fnOS Docker, create a Compose project pointing at that folder, or run:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://<nas-ip>:8091/preview.png` to check the landscape preview.

## API

| Endpoint | Purpose |
|---|---|
| `GET /dash.png` | Device image (1072x1448, 8-bit gray). Supports `If-None-Match` -> `304`. Header `X-Full-Refresh: 1` in the first slot after night ends |
| `GET /clock.png` | Just the clock strip, device orientation, 8-bit gray. Supports `If-None-Match` |
| `GET /api/clock-box` | Where that strip belongs: `{"x","y","w","h"}`; `?fmt=text` gives `"x y w h"` |
| `GET /preview.png` | Landscape grayscale preview |
| `GET /preview` | Live HTML, handy for tweaking CSS in a browser |
| `GET\|POST /render` | Render now |
| `GET /api/plan` | Everything the device loop needs, `key=value` per line: `clock`, `light`, `full_refresh_every`, `max_sleep_s`, `sleep_s`, night window, light level |
| `GET /api/next-wakeup` | Seconds the device should sleep (plain text), plus `X-Full-Refresh`. Superseded by `/api/plan`, kept as a fallback |
| `POST /api/device` | Device report: `battery`, `charging`, `wifi_rssi` (form, JSON or query) |
| `GET /api/light` | Front-light level: `{"level","applied","applied_at"}`; `?fmt=text` gives `"<level> <applied>"` |
| `POST /api/light` | Set it from the phone page: `level=12` or `step=-2` |
| `POST /api/light/applied` | Device acknowledges what it actually set: `level=12` |
| `GET /light` | Phone page: big number, -/+, presets, slider, schedule switch, and what the device last applied |
| `GET /healthz` | Health check |

When `TOKEN` is set, every endpoint except `/healthz` needs `?token=...`.

## Configuration

See [.env.example](.env.example). Key settings:

| Variable | Default | Notes |
|---|---|---|
| `WEATHER_PROVIDER` | `auto` | `qweather`, `openmeteo` or `fake` |
| `QWEATHER_KEY` / `QWEATHER_HOST` | empty | From the QWeather console |
| `LOCATION` | `101120201` | QWeather location id (Qingdao) |
| `LAT` / `LON` | `36.07` / `120.38` | Open-Meteo, air quality, alerts |
| `ROTATE` | `90` | Clockwise rotation, use `270` if the picture is upside down |
| `RENDER_INTERVAL_MIN` | `15` | |
| `NIGHT_START` / `NIGHT_END` | `23:00` / `06:00` | Night window, used for the light ramp and the morning full refresh |
| `NIGHT_SLEEP` | `0` | `1` pauses the poster between the two times above. The clock patch keeps ticking either way, so `1` only freezes weather/calendar |
| `LIGHT_SCHEDULE` | empty | `"07:00=12,23:00=3,06:00=user"` -- front light by time of day, wrapping past midnight. `user` = whatever `/light` was set to. Empty falls back to the two rows below |
| `LIGHT_NIGHT_AUTO` | `1` | Master switch for the schedule (default seed for the checkbox on `/light`) |
| `NIGHT_LIGHT_LEVEL` | `3` | Level used by the built-in `NIGHT_START`/`NIGHT_END` schedule |

### Night

`dash.sh` wakes in short chunks and calls `clock_tick` on every minute boundary, so the time stays
live all night even with `NIGHT_SLEEP=1` -- that setting only stops the poster (weather, calendar)
from being re-rendered between `NIGHT_START` and `NIGHT_END`. With the default `NIGHT_SLEEP=0` the
poster keeps its 15-minute cadence too, which is what a deskside clock wants; the morning slot after
`NIGHT_END` still answers `X-Full-Refresh: 1` and does one flashing repaint to clear accumulated
ghosting.

The front light follows `LIGHT_SCHEDULE`: `GET /api/light` and `/api/plan` both return the level in
force right now, and the device picks the change up on its next minute tick. With no `LIGHT_SCHEDULE`
the schedule is just the two-point version of the night window.

## Policy lives on the server

`GET /api/plan` is the single source of truth for how the device behaves: whether to blit the clock
every minute, the full-refresh interval, the failure backoff cap, how long to sleep next, and the
front-light level. `dash.sh` parses that response and applies it; nothing policy-shaped is hardcoded
on the Kindle any more.

The practical consequence: changing the night window, the light schedule, the refresh cadence or
turning the clock off is an edit to `.env` plus a `docker restart` -- **no new file on the device, no
USB cable**. The device caches the last good plan in `plan.env` so a boot without network still gets
sane behaviour, and every line is validated against `^[a-z_]+=[A-Za-z0-9_:.,-]*$` before it is
assigned to a shell variable.

## Kindle side

Everything that runs on the device is checked in under [device/](device/) -- the only local file that
cannot be shared is `config.sh`, because it carries the server address and token:

| File | Role |
|---|---|
| `config.sh.example` | Template for `config.sh` (`SERVER`, `TOKEN`, `FBINK`, fallbacks) |
| `dash.sh` | The loop: fetch poster, blit the minute, poll the light, sleep, repeat |
| `start.sh` / `stop.sh` | Detach from KUAL (own session, survives the UI dying) / hand the screen back |
| `autostart.sh`, `autostart-install.sh`, `autostart-remove.sh` | upstart hook so the panel returns after a reboot |
| `light.sh`, `light-min/low/mid/high/max.sh`, `light-status.sh` | Front light through `lipc`, bypassing the stock UI |
| `full.sh` | One forced flashing repaint (clears ghosting on demand) |
| `probe.sh` | Read-only diagnostics: framebuffer geometry, fbink path, wifi, battery |
| `kual-menu.json`, `extensions/` | KUAL menu; the device copy lives at `/mnt/us/extensions/kindle-calendar/` |

Install:

1. Install FBInk through MRPI/KUAL.
2. Copy `device/` to `/mnt/us/kindle-calendar/`, copy `config.sh.example` to `config.sh`, set `SERVER`
   and the `FBINK` path.
3. Copy `device/extensions/kindle-calendar/` to `/mnt/us/extensions/`.
4. KUAL -> Kindle Calendar -> Start. Use "Stop (restore UI)" to get the normal Kindle UI back.

`config.sh` keeps only boot-critical values: `SERVER`, `TOKEN`, `FBINK`, and `FULL_REFRESH_EVERY` as a
fallback for the first loop before `/api/plan` answers. `CLOCK=0` and `LIGHT=0` are local escape
hatches for debugging; everything else (clock on/off, the real full-refresh cadence, sleep cap, light
level) comes from the server. `device/plan.env` is the cached copy of the last good plan and is
gitignored, as are the image, ETag, log and light-cache files the loop writes next to the scripts.

`dash.sh` sleeps in chunks that land exactly on the minute boundary, so the clock patch is never late.
The patch rectangle is re-read after every new poster, because it depends on the template and `ROTATE`;
if `fbink` refuses the region blit the feature disables itself and waits for the next poster instead of
retrying 60 times an hour.

`light.sh` restores the front light (`light.sh <0-24>` or `light.sh restore`) after a framework restart,
which always drops it to the floor -- that is what makes the screen go dark when a USB cable is plugged in.

### Front light while the dashboard owns the screen

The brightness slider lives in the stock UI, and the dashboard stops that UI -- so once the panel is up
there is no slider and no KUAL menu to fall back on. The server holds the level instead:

1. Open `http://<nas>:8091/light` on a phone and set the level.
2. `dash.sh` polls `GET /api/light?fmt=text` once a minute, in the same tick it already wakes for the
   clock, and calls `light.sh <level>` when the value changed. It then POSTs the value back to
   `/api/light/applied`, so the page can show "设备已应用" instead of "等待设备拉取".

Because the poll rides on the minute tick it costs no extra wake-ups. `LIGHT=0` in `config.sh` still
works as a local override, but the server can switch it off too via `/api/plan`.

## Development

```bash
uv venv .venv
uv pip install -r requirements.txt
.venv/Scripts/python -m playwright install chromium
WEATHER_PROVIDER=fake .venv/Scripts/python -m pytest -q
WEATHER_PROVIDER=openmeteo .venv/Scripts/python -m uvicorn app.main:app --port 8090
```
