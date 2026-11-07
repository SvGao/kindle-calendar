import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

os.environ.setdefault("WEATHER_PROVIDER", "fake")
os.environ.setdefault("DATA_DIR", "data-test")

from app import render  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.sources import lunar_info  # noqa: E402

NOW = datetime(2026, 11, 7, 14, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_lunar_info_has_fields():
    info = lunar_info.today_info(NOW.date())
    assert info["date"] and info["year"]
    assert lunar_info.day_label(NOW.date())


def test_context_and_html():
    ctx = render.build_context(NOW)
    assert ctx["day"] == 7 and ctx["weekday"] == "星期六"
    assert len(ctx["forecast"]) == 3
    assert any(c["today"] for week in ctx["weeks"] for c in week)
    html = render.render_html(ctx)
    assert "农历" in html and "青岛" in html


def test_to_eink_output_format():
    s = get_settings()
    src = Image.new("RGBA", (s.width, s.height), (120, 130, 140, 255))
    buf = io.BytesIO()
    src.save(buf, "PNG")
    device_png, preview_png = render.to_eink(buf.getvalue(), 90)
    img = Image.open(io.BytesIO(device_png))
    assert img.mode == "L" and img.size == (1072, 1448)
    assert len(set(img.getdata())) <= 16
    assert Image.open(io.BytesIO(preview_png)).size == (1448, 1072)


def test_full_render():
    pytest.importorskip("playwright")
    result = render.render_once(NOW)
    if not result["ok"] and "Executable doesn't exist" in result.get("error", ""):
        pytest.skip("playwright browser not installed")
    assert result["ok"], result
    img = Image.open(render.image_path())
    assert img.mode == "L" and img.size == (1072, 1448)
