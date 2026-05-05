from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable

from .constants import KEYWORDS_FILE, bundled_keywords_template_path


def normalize_keyword(text: str) -> str:
    return " ".join(text.strip().split())


def _parse_line(raw_line: str) -> tuple[bool, str] | None:
    line = normalize_keyword(raw_line)
    enabled = True
    match = re.match(r"^\[(x|х|v|1|да|\s)\]\s*(.*)$", line, re.IGNORECASE)
    if match:
        enabled = match.group(1).strip() != ""
        line = normalize_keyword(match.group(2))
    line = line.casefold().rstrip(" (").strip()
    if not line:
        return None
    if line.endswith(":") and "ключ" in line.casefold():
        return None
    # Часто после импорта из docx остаются служебные обрывки скобок.
    if line in {"(", ")", "-", "–", "—"}:
        return None
    if len(line) <= 2 and not line.isupper():
        return None
    return enabled, line


def parse_keywords(text: str) -> list[str]:
    return [keyword for enabled, keyword in parse_keyword_items(text) if enabled]


def parse_keyword_items(text: str) -> list[tuple[bool, str]]:
    keywords: list[str] = []
    items: list[tuple[bool, str]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        parsed = _parse_line(raw_line)
        if parsed is None:
            continue
        enabled, line = parsed
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(line)
        items.append((enabled, line))
    return items


def _ensure_keywords_file(path: Path = KEYWORDS_FILE) -> None:
    """Создаёт файл при отсутствии: в exe копирует шаблон из сборки, иначе пустой список."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    template = bundled_keywords_template_path()
    if template is not None and template.resolve() != path.resolve():
        try:
            shutil.copy2(template, path)
            return
        except OSError:
            pass
    path.write_text("", encoding="utf-8")


def load_keywords(path: Path = KEYWORDS_FILE) -> list[str]:
    _ensure_keywords_file(path)
    return parse_keywords(path.read_text(encoding="utf-8"))


def load_keyword_items(path: Path = KEYWORDS_FILE) -> list[tuple[bool, str]]:
    _ensure_keywords_file(path)
    return parse_keyword_items(path.read_text(encoding="utf-8"))


def save_keywords(keywords: Iterable[str], path: Path = KEYWORDS_FILE) -> None:
    clean = [(True, keyword) for keyword in parse_keywords("\n".join(keywords))]
    save_keyword_items(clean, path)


def save_keyword_items(
    items: Iterable[tuple[bool, str]],
    path: Path = KEYWORDS_FILE,
) -> None:
    clean = parse_keyword_items(
        "\n".join(f"[{'x' if enabled else ' '}] {keyword}" for enabled, keyword in items)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"[{'x' if enabled else ' '}] {keyword}" for enabled, keyword in clean]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def keywords_as_text(path: Path = KEYWORDS_FILE) -> str:
    _ensure_keywords_file(path)
    return path.read_text(encoding="utf-8")
