"""Tiny JSON file cache with fetch timestamps."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings


def _path(name: str) -> Path:
    return get_settings().cache_dir / f"{name}.json"


def read(name: str) -> tuple[Any | None, datetime | None]:
    p = _path(name)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw["data"], datetime.fromisoformat(raw["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None, None


def write(name: str, data: Any) -> None:
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def age_minutes(fetched_at: datetime | None) -> float:
    if fetched_at is None:
        return float("inf")
    return (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
