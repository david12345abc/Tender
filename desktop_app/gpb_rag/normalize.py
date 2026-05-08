from __future__ import annotations

import re
from collections import Counter


_LINE_JUNK_RE = re.compile(r"^\s*(стр\.?\s*\d+|страница\s*\d+)\s*$", re.I)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_repeated_headers_footers(text: str, min_occurrences: int = 4) -> str:
    """Удаляет короткие строки, которые повторяются почти на каждой странице (шапки/колонтитулы)."""
    lines = text.split("\n")
    counts = Counter()
    for ln in lines:
        s = ln.strip()
        if not s or len(s) > 120:
            continue
        if _LINE_JUNK_RE.match(s):
            counts[s] += 1
            continue
        counts[s] += 1

    threshold = max(min_occurrences, len(lines) // 80)
    drop = {s for s, c in counts.items() if c >= threshold and len(s) <= 120}

    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s in drop or _LINE_JUNK_RE.match(s):
            continue
        out.append(ln.rstrip())
    return normalize_whitespace("\n".join(out))


def light_ocr_fixes(text: str) -> str:
    """Мягкая правка OCR без изменения смысла таблиц и списков."""
    return text
