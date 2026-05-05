from __future__ import annotations

import os
from pathlib import Path
import sys

APP_TITLE = "ЭТП ГПБ — поиск тендеров"

if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent.parent


def user_writable_root() -> Path:
    """Каталог пользовательских данных (запись без прав на папку с exe / сетевой шары)."""
    la = os.environ.get("LOCALAPPDATA")
    if la:
        return Path(la) / "ETP_GPB_Search"
    return Path.home() / ".etp_gpb_search"


# В exe ключевые слова не храним рядом с .exe: PyInstaller кладёт шаблон в _MEIPASS,
# а запись в LOCALAPPDATA доступна с любого ПК и не требует прав на каталог установки.
if getattr(sys, "frozen", False):
    KEYWORDS_FILE = user_writable_root() / "data" / "keywords.txt"
else:
    KEYWORDS_FILE = APP_ROOT / "data" / "keywords.txt"


def bundled_keywords_template_path() -> Path | None:
    """Путь к keywords.txt внутри сборки (datas → _MEIPASS/.../data), если файл есть."""
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / "data" / "keywords.txt")
    candidates.append(APP_ROOT / "_internal" / "data" / "keywords.txt")
    candidates.append(APP_ROOT / "data" / "keywords.txt")
    for p in candidates:
        if p.is_file():
            return p
    return None


CACHE_FILE = APP_ROOT / "cache" / "desktop_search_cache.json"
DOCUMENTS_DIR = APP_ROOT / "output" / "documents"
ANALYSIS_DIR = APP_ROOT / "output" / "analysis"
VIEW_URL = "https://etpgaz.gazprombank.ru/#com/procedure/view/procedure/{pid}"

# LM Studio (OpenAI-совместимый API) для разбора карточки процедуры
LM_STUDIO_BASE_URL = os.environ.get("LM_STUDIO_BASE_URL", "http://192.168.1.157:1234")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "openai/gpt-oss-120b")

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
