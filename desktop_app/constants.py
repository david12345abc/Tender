from __future__ import annotations

import os
from pathlib import Path
import sys

APP_TITLE = "Секция Газпром — поиск тендеров"
APP_PUBLISHER = "ETP GPB Search"

if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent.parent


def _read_app_version() -> str:
    env_version = os.environ.get("ETP_GPB_APP_VERSION", "").strip()
    if env_version:
        return env_version
    meipass = getattr(sys, "_MEIPASS", None)
    candidates = [
        APP_ROOT / "app_version.txt",
        APP_ROOT / "_internal" / "app_version.txt",
    ]
    if meipass:
        candidates.append(Path(meipass) / "app_version.txt")
    for candidate in candidates:
        try:
            if candidate.is_file():
                version = candidate.read_text(encoding="utf-8-sig").strip()
                if version:
                    return version
        except Exception:
            pass
    return "1.0.0"


APP_VERSION = _read_app_version()


def user_writable_root() -> Path:
    """Каталог пользовательских данных (запись без прав на папку с exe / сетевой шары)."""
    la = os.environ.get("LOCALAPPDATA")
    if la:
        return Path(la) / "ETP_GPB_Search"
    return Path.home() / ".etp_gpb_search"


# Основной внешний список ключевых слов лежит рядом с приложением/проектом.
# Если его нет, приложение использует встроенный список из кода.
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


if getattr(sys, "frozen", False):
    DATA_ROOT = user_writable_root()
else:
    DATA_ROOT = APP_ROOT

CACHE_FILE = DATA_ROOT / "cache" / "desktop_search_cache.json"
DOCUMENTS_DIR = DATA_ROOT / "output" / "documents"
ANALYSIS_DIR = DATA_ROOT / "output" / "analysis"
VIEW_URL = "https://etpgaz.gazprombank.ru/#com/procedure/view/procedure/{pid}"

# LM Studio (OpenAI-совместимый API) для разбора карточки процедуры
LM_STUDIO_BASE_URL = os.environ.get("LM_STUDIO_BASE_URL", "http://192.168.1.157:1234")
LM_STUDIO_MODEL = os.environ.get("LM_STUDIO_MODEL", "yandexgpt-5-lite-8b-instruct")

# API мастера подбора наших приборов.
EQUIPMENT_API_BASE_URL = os.environ.get("EQUIPMENT_API_BASE_URL", "https://master-turbo-podbor.ru/")

# Адрес JSON-манифеста последнего релиза. Можно задать через переменную окружения
# или положить путь/URL в update_manifest_url.txt рядом с приложением либо в LOCALAPPDATA.
UPDATE_MANIFEST_ENV = "ETP_GPB_UPDATE_MANIFEST"
UPDATE_MANIFEST_FILE_NAME = "update_manifest_url.txt"

COLUMNS: list[tuple[str, str]] = [
    ("registry_number", "Реестровый №"),
    ("organizer", "Организатор"),
    ("title", "Наименование"),
    ("keyword_matches", "Ключевые слова"),
    ("date_start_registration", "Приём заявок с"),
    ("date_end_registration", "Приём заявок до"),
    ("total_price", "Сумма"),
    ("step_label", "Статус"),
    ("lot_divisibility", "Делимость лота"),
]
