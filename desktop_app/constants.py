from __future__ import annotations

from pathlib import Path

APP_TITLE = "ЭТП ГПБ — поиск тендеров"
CACHE_FILE = Path(__file__).resolve().parent.parent / "cache" / "desktop_search_cache.json"
KEYWORDS_FILE = Path(__file__).resolve().parent.parent / "data" / "keywords.txt"
VIEW_URL = "https://etpgaz.gazprombank.ru/#com/procedure/view/id/{pid}"

COLUMNS: list[tuple[str, str]] = [
    ("registry_number", "Реестровый №"),
    ("trend_pur_label", "Тип"),
    ("organizer", "Организатор"),
    ("title", "Наименование"),
    ("keyword_matches", "Ключевые слова"),
    ("tags_label", "Теги"),
    ("applics_count", "Намерений"),
    ("date_start_registration", "Приём заявок с"),
    ("date_end_registration", "Приём заявок до"),
    ("total_price", "Сумма"),
    ("step_label", "Статус"),
]
