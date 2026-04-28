from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .constants import KEYWORDS_FILE


def normalize_keyword(text: str) -> str:
    return " ".join(text.strip().split())


def parse_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = normalize_keyword(raw_line).casefold()
        line = line.rstrip(" (").strip()
        if not line:
            continue
        if line.endswith(":") and "ключ" in line.casefold():
            continue
        # Часто после импорта из docx остаются служебные обрывки скобок.
        if line in {"(", ")", "-", "–", "—"}:
            continue
        if len(line) <= 2 and not line.isupper():
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(line)
    return keywords


def load_keywords(path: Path = KEYWORDS_FILE) -> list[str]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return []
    return parse_keywords(path.read_text(encoding="utf-8"))


def save_keywords(keywords: Iterable[str], path: Path = KEYWORDS_FILE) -> None:
    clean = parse_keywords("\n".join(keywords))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(clean) + ("\n" if clean else ""), encoding="utf-8")


def keywords_as_text(path: Path = KEYWORDS_FILE) -> str:
    return "\n".join(load_keywords(path))
