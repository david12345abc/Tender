from __future__ import annotations

import re
from collections import Counter


_RUB_RE = re.compile(
    r"(?:^|\s)(\d[\d\s\u00a0]{2,})(?:[,.]\d{1,2})?\s*(?:руб\.?|₽|р\.?)\b",
    re.I,
)


def money_candidates_from_text(text: str) -> list[str]:
    raw = _RUB_RE.findall(text or "")
    cleaned = []
    for x in raw:
        digits = re.sub(r"\D+", "", x)
        if len(digits) >= 5:
            cleaned.append(digits)
    return cleaned


def reconcile_money_llm_with_chunks(llm_value: str | None, chunk_texts: list[str]) -> str | None:
    """Если суммы из контекста расходятся — берём наиболее частое числовое значение."""
    if llm_value is None or not str(llm_value).strip():
        return llm_value
    normalized_llm = re.sub(r"\D+", "", str(llm_value))
    pool: list[str] = []
    for t in chunk_texts:
        pool.extend(money_candidates_from_text(t))
    if not pool or not normalized_llm:
        return llm_value
    if normalized_llm in pool:
        return llm_value
    cnt = Counter(pool)
    best, freq = cnt.most_common(1)[0]
    if freq >= 2:
        try:
            n = int(best)
            return f"{n:,}".replace(",", " ") + " руб."
        except ValueError:
            return llm_value
    return llm_value


def empty_to_null(s: str | None) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() in {"не указано", "—", "-", "н/д", "нет данных"}:
        return None
    return t
