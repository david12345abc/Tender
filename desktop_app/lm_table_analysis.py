"""Вызов LM Studio для заполнения таблицы анализа карточки секции Газпром."""
from __future__ import annotations

import json
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Колонки таблицы (после номера, ссылки на карточку и ссылки на файл — см. build_result_row).
ANALYSIS_JSON_KEYS: list[str] = [
    "customer_name",
    "tender_title",
    "procurement_subject",
    "application_deadline",
    "retender_date",
    "results_date",
    "starting_price",
    "partial_supply_allowed",
    "delivery_terms",
    "payment_terms",
    "certification_requirements",
    "contract_security",
    "application_fee",
    "supplier_risks",
]

ANALYSIS_TABLE_HEADERS_RU: list[str] = [
    "Номер тендера (запроса)",
    "Ссылка на закупку",
    "Ссылка на файл документации",
    "Наименование Заказчика",
    "Наименование тендера",
    "Предмет закупки (наименование поставляемого оборудования)",
    "Дата ограничения подачи заявки",
    "Дата переторга",
    "Дата подведения итогов по закупке",
    "Начальная минимальная цена (НМЦ)",
    "Возможна ли поставка части оборудования (делим ли лот)",
    "Срок поставки",
    "Условия оплаты",
    "Требования к сертификации",
    "Обеспечение исполнения договора",
    "Стоимость подачи заявки на участие",
    "Риски Поставщика/Исполнителя при нарушении условий договора",
]


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", t, re.I)
    if m:
        return m.group(1).strip()
    return t


def _first_json_decode(text: str) -> Any:
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            val, _ = dec.raw_decode(text[i:])
            return val
        except json.JSONDecodeError:
            continue
    raise ValueError("В ответе модели не найден валидный JSON.")


def parse_llm_table_json(raw: str) -> dict[str, str]:
    """Достаёт один объект полей из ответа модели."""
    text = _strip_code_fence(raw)
    obj = _first_json_decode(text)
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = obj[0]
    if not isinstance(obj, dict):
        raise ValueError("Ожидался JSON-объект (или массив из одного объекта).")
    out: dict[str, str] = {}
    for k in ANALYSIS_JSON_KEYS:
        v = obj.get(k)
        out[k] = "" if v is None else str(v).strip()
    return out


def single_field_system_prompt(field_label_ru: str) -> str:
    return (
        "Ты система извлечения структурированных данных из документов и текста карточки закупки (ЭТП).\n"
        "Тебе даётся ровно одно поле для заполнения.\n\n"
        "Правила:\n"
        "- Не придумывай значения.\n"
        "- Если в контексте нет данных для поля — верни null для этого поля.\n"
        "- Ответ только одним JSON-объектом (без markdown и текста вне JSON).\n"
        "- Используй только информацию из контекста пользователя.\n\n"
        f"Человекочитаемое название поля: {field_label_ru}\n"
        "Формат ответа: объект с единственным ключом — строкой из поля "
        "«Требуемое поле (ключ JSON)» в запросе пользователя — и значением типа string или null. "
        "Строковые значения на русском языке."
    )


def parse_single_field_json(raw: str, field_key: str) -> str | None:
    """Разбор ответа модели для одного поля."""
    text = _strip_code_fence(raw)
    obj = _first_json_decode(text)
    if not isinstance(obj, dict):
        raise ValueError("Ожидался JSON-объект.")
    if field_key in obj:
        v = obj[field_key]
    elif "value" in obj:
        v = obj["value"]
    elif len(obj) == 1:
        v = next(iter(obj.values()))
    else:
        raise ValueError(f"В объекте нет ключа «{field_key}».")
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def build_analysis_system_prompt() -> str:
    keys_line = ", ".join(ANALYSIS_JSON_KEYS)
    return (
        "Ты аналитик закупок по данным с российской ЭТП. "
        "Пользователь пришлёт полный текст страницы извещения о процедуре "
        "(сведения о процедуре, документация, организатор, список лотов и т.д.) "
        "и текст файлов документации, включая файлы из распакованных архивов. "
        "Извлеки факты из обоих источников и заполни поля. Если в тексте нет явного значения — "
        "укажи «не указано». Ответь ТОЛЬКО одним JSON-объектом без пояснений и без markdown. "
        f"Ключи строго на английском: {keys_line}. "
        "Все значения — строки на русском языке."
    )


def build_analysis_user_prompt(
    registry: str,
    detail_url: str,
    doc_links_summary: str,
    page_text: str,
    documents_text: str = "",
) -> str:
    return (
        f"Реестровый номер (для справки): {registry}\n"
        f"URL карточки: {detail_url}\n"
        f"Ссылки на файлы документации (если перечислены на странице): {doc_links_summary}\n\n"
        "Текст страницы извещения:\n"
        "-----\n"
        f"{page_text}\n"
        "-----\n\n"
        "Текст документов из карточки (включая файлы из распакованных архивов):\n"
        "-----\n"
        f"{documents_text or '[документы не найдены или текст не извлечён]'}\n"
        "-----\n"
    )


def call_lm_studio_chat(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: int = 900,
    max_tokens: int = 8192,
    retries: int = 2,
) -> str:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    raw = ""
    last_error: BaseException | None = None
    attempts = max(1, retries + 1)
    for attempt in range(1, attempts + 1):
        req = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            break
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:2000]
            except Exception:
                pass
            raise RuntimeError(f"LM Studio HTTP {e.code}: {e.reason}. {detail}") from e
        except (TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError, URLError, OSError) as e:
            last_error = e
            if attempt >= attempts:
                if isinstance(e, (TimeoutError, socket.timeout)):
                    raise RuntimeError(f"LM Studio не ответил за {timeout_sec} сек. (таймаут).") from e
                raise RuntimeError(
                    f"Не удалось подключиться к LM Studio после {attempts} попыток: {e}"
                ) from e
            time.sleep(min(2 * attempt, 5))
    if not raw:
        raise RuntimeError(f"Пустой ответ LM Studio. Последняя ошибка: {last_error}")

    data = json.loads(raw)
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Пустой ответ API: {raw[:1500]}")
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if not content:
        raise RuntimeError(f"Нет content в ответе: {raw[:1500]}")
    return str(content)


def build_result_row(
    registry: str,
    detail_url: str,
    doc_file_url: str,
    parsed: dict[str, str] | None,
    error: str | None,
) -> list[str]:
    """Строка для QTableWidget в порядке ANALYSIS_TABLE_HEADERS_RU."""
    if parsed is None:
        parsed = {k: "—" for k in ANALYSIS_JSON_KEYS}
    return [
        registry,
        detail_url,
        doc_file_url or "—",
        parsed.get("customer_name", "—"),
        parsed.get("tender_title", "—"),
        parsed.get("procurement_subject", "—"),
        parsed.get("application_deadline", "—"),
        parsed.get("retender_date", "—"),
        parsed.get("results_date", "—"),
        parsed.get("starting_price", "—"),
        parsed.get("partial_supply_allowed", "—"),
        parsed.get("delivery_terms", "—"),
        parsed.get("payment_terms", "—"),
        parsed.get("certification_requirements", "—"),
        parsed.get("contract_security", "—"),
        parsed.get("application_fee", "—"),
        parsed.get("supplier_risks", "—"),
    ]
