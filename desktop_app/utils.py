from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    if len(s) >= 25 and (s[-6] in "+-") and s[-3] == ":":
        s = s[:-3] + s[-2:]
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def fmt_money(v: Optional[float], currency: str = "RUB") -> str:
    if v is None:
        return ""
    return f"{v:,.2f}".replace(",", " ") + (f" {currency}" if currency else "")


def fmt_date(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return (
        dt.strftime("%d.%m.%Y %H:%M")
        if (dt.hour or dt.minute)
        else dt.strftime("%d.%m.%Y")
    )
