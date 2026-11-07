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
```

## Features

- Landscape 1448x1072 layout, rotated server-side to the Kindle framebuffer (1072x1448).
- Weather: QWeather (now, 7-day, air quality, alerts) when a key is set, otherwise Open-Meteo (no key).
- Lunar date, GanZhi year, solar terms, traditional festivals, daily 宜/忌 via `lunar-python`.
- Public holidays and make-up workdays from [holiday-cn](https://github.com/NateScarlet/holiday-cn).
- Every source is cached separately under `/data/cache`; a failed source never blanks the image.
- ETag on `/dash.png` so the Kindle only redraws when the image changed.
- Night window (no updates 23:00-06:00) and a morning full-refresh hint to clear ghosting.

## Run on fnOS (Docker Compose)

1. Copy this folder to the NAS, for example `/vol1/1000/docker/kindle-calendar`.
2. `cp .env.example .env` and edit it (QWeather key and API Host are optional).
3. In fnOS Docker, create a Compose project pointing at that folder, or run:
   ```bash
   docker compose up -d --build
   ```
4. Open `http://<nas-ip>:8090/preview.png` to check the landscape preview.

## API

| Endpoint | Purpose |
|---|---|
| `GET /dash.png` | Device image (1072x1448, 8-bit gray). Supports `If-None-Match` -> `304`. Header `X-Full-Refresh: 1` in the first slot after night ends |
| `GET /preview.png` | Landscape grayscale preview |
| `GET /preview` | Live HTML, handy for tweaking CSS in a browser |
| `GET\|POST /render` | Render now |
| `GET /api/next-wakeup` | Seconds the device should sleep (plain text), plus `X-Full-Refresh` |
| `POST /api/device` | Device report: `battery`, `charging`, `wifi_rssi` (form, JSON or query) |
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
| `NIGHT_START` / `NIGHT_END` | `23:00` / `06:00` | |

## Kindle side

Files in [device/](device/), for an always-plugged-in Kindle (no suspend):

1. Install FBInk through MRPI/KUAL.
2. Copy `device/` to `/mnt/us/kindle-calendar/`, copy `config.sh.example` to `config.sh`, set `SERVER`
   and the `FBINK` path.
3. Copy `device/extensions/kindle-calendar/` to `/mnt/us/extensions/`.
4. KUAL -> Kindle Calendar -> Start. Use "Stop (restore UI)" to get the normal Kindle UI back.

`dash.sh` has not been run on a real device yet; verify the FBInk path and the commands that stop
the stock UI (`stop lab126_gui`) on your firmware.

## Development

```bash
uv venv .venv
uv pip install -r requirements.txt
.venv/Scripts/python -m playwright install chromium
WEATHER_PROVIDER=fake .venv/Scripts/python -m pytest -q
WEATHER_PROVIDER=openmeteo .venv/Scripts/python -m uvicorn app.main:app --port 8090
```
