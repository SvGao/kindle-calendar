import json
import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import events, light, notify, render, store
from app.config import get_settings
from app.main import app

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def clean_state():
    s = get_settings()
    for name in ("settings.json", "light.json", "device.json", "notify_state.json", "force_full"):
        (s.data_dir / name).unlink(missing_ok=True)
    yield


# --- anniversaries -------------------------------------------------------------


def test_solar_birthday_repeats_and_counts_age():
    item = {"name": "妈妈", "date": "1960-11-20", "lunar": False, "kind": "birthday"}
    when, years = events.next_occurrence(item, date(2026, 11, 9))
    assert when == date(2026, 11, 20) and years == 66
    when, years = events.next_occurrence(item, date(2026, 11, 21))
    assert when == date(2027, 11, 20) and years == 67


def test_feb29_falls_back_in_common_years():
    item = {"name": "x", "date": "02-29", "lunar": False, "kind": "anniversary"}
    assert events.next_occurrence(item, date(2027, 1, 1))[0] == date(2027, 2, 28)


def test_lunar_date_and_missing_30th():
    mid_autumn = {"name": "中秋", "date": "08-15", "lunar": True, "kind": "anniversary"}
    assert events.next_occurrence(mid_autumn, date(2026, 9, 1))[0] == date(2026, 9, 25)
    # 2026 腊月 has 29 days, so 腊月三十 falls back to 廿九.
    eve = {"name": "除夕", "date": "12-30", "lunar": True, "kind": "anniversary"}
    assert events.next_occurrence(eve, date(2027, 1, 1))[0] == date(2027, 2, 5)


def test_countdown_is_one_off():
    item = {"name": "旅行", "date": "2026-12-01", "lunar": False, "kind": "countdown"}
    assert events.next_occurrence(item, date(2026, 11, 9))[0] == date(2026, 12, 1)
    assert events.next_occurrence(item, date(2026, 12, 2)) is None


def test_upcoming_and_tag_text():
    items = [
        {"id": "a", "name": "结婚", "date": "2020-11-12", "lunar": False, "kind": "anniversary"},
        {"id": "b", "name": "今天", "date": "11-09", "lunar": False, "kind": "birthday"},
    ]
    up = events.upcoming(items, date(2026, 11, 9), 30)
    assert [e["id"] for e in up] == ["b", "a"]
    assert events.tag_text(up[0]) == "今天 今天"
    assert events.tag_text(up[1]) == "距结婚6周年3天"


def test_clean_event_rejects_garbage():
    assert store.clean_event({"name": "", "date": "2026-01-01"}) is None
    assert store.clean_event({"name": "x", "date": "2026-02-30"}) is None
    assert store.clean_event({"name": "x", "date": "01-05", "kind": "countdown"}) is None
    ok = store.clean_event({"name": "x", "date": "02-29", "kind": "birthday"})
    assert ok and ok["id"]


# --- light ---------------------------------------------------------------------


def test_light_level_zero_is_allowed():
    assert light.clamp(0) == 0 and light.clamp(-3) == 0 and light.clamp(99) == 24


def test_schedule_wraps_midnight():
    sched = ((time(6, 0), None), (time(23, 0), 0))
    assert light.pick(sched, time(2, 0), 12) == (0, time(23, 0))
    assert light.pick(sched, time(12, 0), 12) == (12, time(6, 0))
    nxt = light.next_boundary(sched, datetime(2026, 11, 9, 23, 30, tzinfo=TZ))
    assert nxt == datetime(2026, 11, 10, 6, 0, tzinfo=TZ)


# --- clock geometry --------------------------------------------------------------


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_clock_rect_matches_pil_rotation(rotate):
    """The rect must cover exactly the pixels PIL moves the landscape box to."""
    from PIL import Image

    s = get_settings()
    s = type(s)(**{**s.__dict__, "rotate": rotate})
    box = {"x": 254, "y": 84, "width": 141, "height": 348}
    img = Image.new("L", (s.width, s.height), 255)
    img.paste(0, (box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]))
    out = img.rotate(-rotate, expand=True) if rotate else img
    r = render.clock_device_rect(box, s)
    assert out.point(lambda v: 255 - v).getbbox() == (r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])


# --- HTTP ------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)  # no context manager: skips the lifespan (no scheduler, no Chromium)


def test_plan_lines_pass_device_validation(client):
    body = client.get("/api/plan").text
    pattern = re.compile(r"^[a-z_]+=[A-Za-z0-9_:.,-]*$")
    lines = body.strip().splitlines()
    assert lines and all(pattern.match(line) for line in lines), body
    keys = dict(line.split("=", 1) for line in lines)
    for k in ("epoch", "tz_offset_s", "utc", "clock_pack", "clock_flash", "sleep_s"):
        assert k in keys
    assert keys["tz_offset_s"] == "28800"


def test_manual_light_change_holds_until_next_slot(client):
    client.post("/api/light", json={"schedule": [["00:00", 0], ["23:59", 0]], "night_auto": 1})
    view = client.post("/api/light", json={"level": 15}).json()
    assert view["effective"] == 15 and view["hold"] is True
    view = client.post("/api/light", json={"resume": 1}).json()
    assert view["effective"] == 0 and view["hold"] is False


def test_light_text_carries_poster_and_full_flag(client):
    parts = client.get("/api/light?fmt=text").text.split()
    assert len(parts) == 4 and parts[3] == "0"
    client.post("/api/full-refresh")
    assert client.get("/api/light?fmt=text").text.split()[3] == "1"


def test_console_state_and_settings_roundtrip(client, monkeypatch):
    monkeypatch.setattr("app.main._render_job", lambda: _noop())
    r = client.post(
        "/api/settings",
        json={
            "anniversaries": [{"name": "生日", "date": "11-20", "kind": "birthday"}],
            "show_poem": False,
            "notify": {"kind": "bark", "url": "https://api.day.app/secretkey123456789"},
        },
    )
    j = r.json()
    assert j["anniversaries"][0]["name"] == "生日" and j["settings"]["show_poem"] is False
    assert "secretkey" not in json.dumps(j)  # the push key never goes back to the browser
    # Saving the masked value must keep the real one.
    client.post("/api/settings", json={"notify": {"url": j["notify"]["url"]}})
    assert store.read()["notify"]["url"].endswith("123456789")
    assert client.get("/console").status_code == 200


async def _noop():
    return {}


def test_notify_battery_once_then_resets():
    s = get_settings()
    cfg = {"kind": "bark", "url": "x", "battery": True, "weather_alert": False}
    st: dict = {}
    now = datetime(2026, 11, 9, 12, 0, tzinfo=TZ)
    dev = s.data_dir / "device.json"
    dev.write_text(json.dumps({"battery": 15, "charging": False}), encoding="utf-8")
    assert [m[0] for m in notify.pending_messages(now, s, cfg, st)] == ["Kindle 电量低"]
    assert notify.pending_messages(now, s, cfg, st) == []  # only once
    dev.write_text(json.dumps({"battery": 40, "charging": True}), encoding="utf-8")
    assert notify.pending_messages(now, s, cfg, st) == [] and st["battery_low"] is False
