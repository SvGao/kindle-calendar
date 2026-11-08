"""Footer quote pool: a local, offline corpus of classical lines.

Built from chinese-poetry (唐诗三百首 / 宋词三百首 / 全宋词 / 诗经 / 幽梦影 /
增广贤文) by ``tools/build_quotes.py``. No API, no network, no per-request cost.

Each line carries scene tags (季节 / 节日 / 时段) so the footer can follow the
calendar: snow lines in winter, moon lines at mid-autumn, sleepy lines at night.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA = Path(__file__).parent / "data" / "quotes.json.gz"

_rows: list[dict[str, Any]] | None = None

# 11 月已供暖，按冬处理比按秋更贴近实际感受。
SEASON_BY_MONTH = {
    1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn",
    11: "winter", 12: "winter",
}

# Lunar festival name -> scene tag. Longest match first (春节 before 春).
FESTIVAL_TAG = {
    "除夕": "springfest", "春节": "springfest", "元旦节": "springfest",
    "元宵节": "lantern", "上元节": "lantern",
    "清明节": "qingming", "端午节": "duanwu",
    "中秋节": "midautumn", "重阳节": "chongyang",
}

STRIDE = 7919  # prime; keeps the walk from revisiting the same neighbours
FESTIVAL_SCENE = {"springfest", "lantern", "qingming", "duanwu", "midautumn", "chongyang"}

# 雪 lands inside 「杏花如雪」「梨花如雪」, which are spring images. Drop those from
# the winter pool so a November screen never shows blossom.
SEASON_CONFLICT = {
    "winter": ("杏", "桃", "梨", "海棠", "燕", "莺", "荷", "莲", "蝉"),
    "summer": ("雪", "梅", "腊", "霜"),
    "spring": ("雪", "霜", "荷", "莲", "蝉"),
    "autumn": ("杏", "桃", "燕", "莺", "荷", "莲", "蝉"),
}


def _load() -> list[dict[str, Any]]:
    global _rows
    if _rows is None:
        try:
            with gzip.open(DATA, "rt", encoding="utf-8") as fh:
                _rows = json.load(fh)
        except (OSError, ValueError) as e:  # noqa: BLE001
            log.error("quote pool unavailable: %s", e)
            _rows = []
    return _rows


def _size(text: str) -> int:
    n = len(text)
    if n <= 14:
        return 26
    if n <= 18:
        return 24
    return 22


def _source(row: dict[str, Any]) -> str:
    """「— 周邦彦《满庭芳》」, but never repeat the author as a book title."""
    s = (row.get("s") or "").strip()
    a = (row.get("a") or "").strip()
    if not s or s == "精选" or s == a:
        return ""
    # 词牌 often carries the opening line as a subtitle (「水调歌头・明月几时有」);
    # the footer only has room for the tune itself.
    for sep in ("・", "·"):
        if sep in s:
            s = s.split(sep)[0]
            break
    # 唐诗三百首 sometimes keeps the old 乐府 heading (「横吹曲辞 出塞」).
    if " " in s:
        s = s.split()[-1]
    # 诗经 entries carry their book in the title (「《诗经》关雎」); fold it back
    # into one「《诗经·关雎》」instead of printing「— 诗经《诗经》关雎」.
    if a and s.startswith(f"《{a}》"):
        rest = s[len(a) + 2 :]
        return f"《{a}·{rest}》" if rest else f"《{a}》"
    return s if s.startswith("《") else f"《{s}》"


def _scene_tags(now, festivals) -> list[str]:
    tags = [SEASON_BY_MONTH.get(now.month, "")]
    for f in festivals or ():
        t = FESTIVAL_TAG.get(f)
        if t:
            tags.append(t)
    h = now.hour
    if 5 <= h < 10:
        tags.append("morning")
    elif h >= 22 or h < 3:
        tags.append("night")
    return [t for t in tags if t]


def pick(now, festivals=()) -> dict[str, Any] | None:
    """Walk the pool with a fixed stride so a line never repeats for ~90 days.

    The index derives from the refresh slot, so the preview page and the e-ink
    poster always show the same line for the same 15-minute window. Two slots in
    three are drawn from lines tagged for the current season / festival / hour,
    which keeps snow verse out of July without making the rotation predictable.
    """
    rows = _load()
    if not rows:
        return None
    slot = now.toordinal() * 96 + now.hour * 4 + now.minute // 15

    pool = rows
    tags = _scene_tags(now, festivals)
    fest_tag = next((t for t in tags if t in FESTIVAL_SCENE), None)

    if fest_tag:
        # On the festival itself the whole day should feel like that festival, so
        # use the festival pool exclusively rather than mixing it into the season.
        themed = [r for r in rows if fest_tag in r.get("g", ())]
        if len(themed) >= 5:
            pool = themed
    elif tags and (slot * 2654435761) % 3 < 2:
        want = set(tags)
        themed = [r for r in rows if want.intersection(r.get("g", ()))]
        block = SEASON_CONFLICT.get(SEASON_BY_MONTH.get(now.month, ""), ())
        if block:
            themed = [r for r in themed if not any(w in r["t"] for w in block)]
        if len(themed) >= 60:  # a season pool this small would repeat within a week
            pool = themed

    row = pool[(slot * STRIDE) % len(pool)]
    return {
        "text": row["t"],
        "author": row.get("a", ""),
        "source": _source(row),
        "size": _size(row["t"]),
    }
