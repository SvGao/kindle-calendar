"""Chinese lunar calendar info, computed locally with lunar-python."""

from __future__ import annotations

from datetime import date
from typing import Any

from lunar_python import Solar


# Solar festivals worth showing; lunar-python also returns Western ones (万圣节, 感恩节 ...).
SOLAR_WHITELIST = {"元旦节", "妇女节", "植树节", "劳动节", "青年节", "儿童节", "建党节", "建军节", "教师节", "国庆节"}


def _festivals(solar, lunar) -> list[str]:
    return list(lunar.getFestivals()) + [f for f in solar.getFestivals() if f in SOLAR_WHITELIST]


def today_info(d: date) -> dict[str, Any]:
    solar = Solar.fromYmd(d.year, d.month, d.day)
    lunar = solar.getLunar()

    festivals = _festivals(solar, lunar)
    jieqi = lunar.getJieQi()

    next_term = ""
    if not jieqi:
        nxt = lunar.getNextJieQi()
        ns = nxt.getSolar()
        days = (date(ns.getYear(), ns.getMonth(), ns.getDay()) - d).days
        if days > 0:
            next_term = f"距{nxt.getName()}{days}天"

    return {
        "date": f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
        "year": f"{lunar.getYearInGanZhi()}{lunar.getYearShengXiao()}年",
        "jieqi": jieqi,
        "next_term": next_term,
        "festivals": festivals[:2],
        "yi": list(lunar.getDayYi())[:4],
        "ji": list(lunar.getDayJi())[:4],
    }


def day_label(d: date) -> str:
    """Short label under a day number in the month grid."""
    solar = Solar.fromYmd(d.year, d.month, d.day)
    lunar = solar.getLunar()
    for name in _festivals(solar, lunar):
        if len(name) <= 3:
            return name
    jieqi = lunar.getJieQi()
    if jieqi:
        return jieqi
    if lunar.getDay() == 1:
        return f"{lunar.getMonthInChinese()}月"
    return lunar.getDayInChinese()
