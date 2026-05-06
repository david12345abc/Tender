from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .constants import KEYWORDS_FILE, bundled_keywords_template_path

DEFAULT_KEYWORDS_TEXT = """[x] ультразвуковой
[x] кориолисовый
[x] массовый
[x] термомассовые
[x] термоанемометрические
[x] узел измерения количества
[x] кип
[x] кипиа
[x] контрольно-измерительные
[x] преобразователь расхода
[x] измерительное оборудование
[x] вычислитель расхода
[x] датчик расхода
[x] оборудование
[x] измеритель расхода
[x] счетчик жидкости
[x] ротаметр
[x] блок измерительный
[x] система измерения количества
[x] средство измерения
[x] счетчик
[x] теплосчетчик
[x] вычислитель расхода газа
[x] поверка счетчиков
[x] поверка расходомеров
[x] метрологическая поверка
[x] техническое обслуживание узлов учета
[x] монтаж узла учета
[x] пуско-наладка
[x] расходомер жидкости
[x] расходомер электромагнитный
[x] расходомер-счетчик газа
[x] преобразователь расхода газа
[x] комплекс измерительный расхода
[x] ультразвуковой преобразователь расхода
[x] turbo
[x] flow
[x] портативный расходомер
[x] ууг
[x] пуг
[x] пург
[x] шуург
[x] куург
[x] грп
[x] грпб
[x] грпш
[x] газорегуляторный пункт блочный
[x] шкафной газорегуляторный пункт
[x] пункт редуцирования газа
[x] прг
[x] пргш
[x] газораспределительная станция
[x] газоизмерительная
[x] станция
[x] узел редуцирования
[x] регулятор давления газа
[x] поверка средств измерений
[x] калибровка приборов
[x] метрологическое обеспечение
[x] реконструкция узла учета
[x] модернизация грп/грс
[x] пусконаладочные работы
[x] пнр
[x] проектно-изыскательские работы
[x] научно-исследовательские работы
[x] нир
[x] опытно-конструкторские работы
[x] окр
[x] техническое перевооружение
[x] капитальный ремонт оборудования
[x] монтаж средств измерений
[x] демонтаж оборудования
[x] сервисное обслуживание
[x] гарантийное обслуживание
[x] пожизненная гарантия
[x] водопроливная
[x] установка
[x] преобразователи давления
[x] стенд/стенд испытаний
[x] поверочная установка
[x] датчик давления
[x] телеметр
[x] измерит комплекс
[x] газовое оборудование
[x] спу
[x] спу-3
[x] спу-5
[x] спу-7
[x] средства измерения
[x] метрологическое оборудование
[x] пир
[x] гис
[x] приборы учета газа
[x] счетчики газа
[x] узел учета газа
[x] расход газа
[x] расход жидкости
[x] расход
[x] газорегуляторный пункт
[x] пункт учета расхода газа
[x] пункт учета газа
[x] кориолисовый расходомер
[x] ультразвуковой расходомер
[x] массовый расходомер
[x] расходомер газа
[x] расходомер
[x] смарт счетчики
[x] гранд
[x] интеллектуальный счетчик газа (интеллектуальные)
[x] преобразователи расхода газа
[x] комплекс измерительный
[x] преобразователь / датчик / сенсор
[x] учет / измерение / контроль / мониторинг
[x] поверка / калибровка / метрологическая аттестация
[x] узел / пункт / станция / система
[x] оборудование / аппаратура / приборы / средства
[x] техническое обслуживание / то / сервис / сопровождение
[x] поставка / закупка / приобретение / оснащение
[x] модернизация / реконструкция / перевооружение / обновление
[x] грс
[x] коммерческий учет газа
[x] асу грс/гис
[x] система учета газа
[x] блочный пункт учета
[x] шкафной пункт учета
[x] счетчик технологического учета
[x] счетчик коммерческого учета
[x] счетчик гранд
[x] смарт-счетчик газа
[x] счетчик газа ультразвуковой
[x] счетчик газа
[x] расходомер массовый
[x] расходомер ультразвуковой
[x] измерительный комплекс
[x] пункт учета
[x] узел измерения расхода
[x] узел учета
[x] ультразвуковых
"""


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


def _read_keywords_text(path: Path = KEYWORDS_FILE) -> str:
    """Читает внешний файл, шаблон из сборки или встроенный список."""
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
    template = bundled_keywords_template_path()
    if template is not None and template.resolve() != path.resolve():
        try:
            return template.read_text(encoding="utf-8")
        except OSError:
            pass
    return DEFAULT_KEYWORDS_TEXT


def load_keywords(path: Path = KEYWORDS_FILE) -> list[str]:
    return parse_keywords(_read_keywords_text(path))


def load_keyword_items(path: Path = KEYWORDS_FILE) -> list[tuple[bool, str]]:
    return parse_keyword_items(_read_keywords_text(path))


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
    return _read_keywords_text(path)
