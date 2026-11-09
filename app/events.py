"""Anniversaries, birthdays and countdowns entered in the console.

Each item is ``{"name", "date", "lunar", "kind"}`` where ``date`` is ``YYYY-MM-DD``
or ``MM-DD`` (year unknown).  Birthdays and anniversaries repeat every year;
a countdown is one-off and disappears once its day has passed.  Lunar dates
follow the lunar calendar (a 三十 in a 29-day month falls back to 廿九).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from lunar_python import Lunar, Solar

KIND_CN = {"birthday": "生日", "anniversary": "纪念日", "countdown": "倒计时"}


def _parts(item: dict[str, Any]) -> tuple[int | None, int, int]:
    p = [int(x) for x in str(item["date"]).split("-")]
    return (p[0], p[1], p[2]) if len(p) == 3 else (None, p[0], p[1])


def _lunar_to_solar(year: int, month: int, day: int) -> date | None:
    for d in (day, day - 1):
        try:
            s = Lunar.fromYmd(year, month, d).getSolar()
            return date(s.getYear(), s.getMonth(), s.getDay())
        except Exception:  # noqa: BLE001  invalid day for that month
            continue
    return None


def _solar(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:  # 02-29 in a common year
        return date(year, month, day - 1)


def next_occurrence(item: dict[str, Any], today: date) -> tuple[date, int | None] | None:
    """(date, years) of the next occurrence on or after ``today``.

    ``years`` is the age / anniversary count when the original year is known.
    """
    year, month, day = _parts(item)
    if item.get("kind") == "countdown":
        if year is None:
            return None
        when = _lunar_to_solar(year, month, day) if item.get("lunar") else _solar(year, month, day)
        return (when, None) if when and when >= today else None

    if item.get("lunar"):
        ly = Solar.fromYmd(today.year, today.month, today.day).getLunar().getYear()
        for y in (ly - 1, ly, ly + 1):
            when = _lunar_to_solar(y, month, day)
            if when and when >= today:
                return when, (y - year if year else None)
        return None

    for y in (today.year, today.year + 1):
        when = _solar(y, month, day)
        if when >= today:
            return when, (y - year if year else None)
    return None


def _label(item: dict[str, Any], years: int | None) -> str:
    name = item["name"]
    if years and years > 0:
        # "妈妈生日" -> "妈妈65岁生日", "结婚纪念日" -> "结婚6周年纪念日"
        if item.get("kind") == "birthday":
            if name.endswith("生日") and len(name) > 2:
                return f"{name[:-2]}{years}岁生日"
            return f"{name}{years}岁"
        if item.get("kind") == "anniversary":
            if name.endswith("纪念日") and len(name) > 3:
                return f"{name[:-3]}{years}周年纪念日"
            return f"{name}{years}周年"
    return name


def upcoming(items: list[dict[str, Any]], today: date, within: int = 366) -> list[dict[str, Any]]:
    """Every item's next occurrence within ``within`` days, nearest first."""
    out = []
    for item in items:
        try:
            nxt = next_occurrence(item, today)
        except Exception:  # noqa: BLE001  a broken entry must not break the poster
            continue
        if not nxt:
            continue
        when, years = nxt
        days = (when - today).days
        if days > within:
            continue
        out.append(
            {
                "id": item.get("id"),
                "name": item["name"],
                "label": _label(item, years),
                "kind": item.get("kind"),
                "kind_cn": KIND_CN.get(item.get("kind"), ""),
                "lunar": bool(item.get("lunar")),
                "date": when.isoformat(),
                "days": days,
                "years": years,
            }
        )
    out.sort(key=lambda e: (e["days"], e["name"]))
    return out


def tag_text(e: dict[str, Any]) -> str:
    """Short text for the poster's tag row."""
    if e["days"] == 0:
        return f"今天 {e['label']}"
    return f"距{e['label']}{e['days']}天"


def by_date(items: list[dict[str, Any]], start: date, end: date) -> dict[str, str]:
    """``{iso_date: short name}`` for the month grid between start and end inclusive."""
    out: dict[str, str] = {}
    for e in upcoming(items, start, (end - start).days):
        out.setdefault(e["date"], short_name(e["name"]))
    return out


def short_name(name: str) -> str:
    """Three characters for the grid: "妈妈生日" reads better as "妈妈" than "妈妈生"."""
    for suffix in ("生日", "纪念日", "周年"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return name[:3]
