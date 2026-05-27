from __future__ import annotations

import json
import re
import traceback
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from etp_client import HARD_SERVER_LIMIT, EtpClient, _server_status_value

from .constants import ANALYSIS_DIR, VIEW_URL
from .document_text import _is_archive, prepare_documents_for_analysis
from .gpb_rag.pipeline import ragged_analysis_available, run_rag_table_analysis
from .lm_table_analysis import (
    build_analysis_system_prompt,
    build_analysis_user_prompt,
    build_technical_system_prompt,
    build_technical_user_prompt,
    build_result_row,
    call_lm_studio_chat,
    parse_llm_table_json,
    parse_technical_table_json,
)
from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import SearchParams


def _safe_folder_name(name: str, default: str = "procedure") -> str:
    import re

    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return clean[:120] or default


def _trim_for_llm(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[текст обрезан для повторного запроса к модели]"


def _analysis_filled_count(parsed: dict[str, str] | None) -> int:
    if not parsed:
        return 0
    empty_values = {"", "—", "-", "null", "none", "не указано", "нет данных"}
    return sum(
        1
        for value in parsed.values()
        if str(value or "").strip().casefold() not in empty_values
    )


def _is_empty_analysis_value(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    if text in {"", "—", "-", "null", "none", "не указано", "нет данных"}:
        return True
    return any(
        marker in text
        for marker in (
            "в контексте нет",
            "в контексте не",
            "не указано конкрет",
            "не указана конкрет",
            "нет информации",
            "не удалось определить",
            "прямо не называют",
            "не представлена",
            "не представлены",
            "не указана точная",
            "не указано точн",
            "указывается в формате",
            "согласно закупочной документации",
        )
    )


def _clean_analysis_value(value: Any) -> str:
    return "" if _is_empty_analysis_value(value) else str(value or "").strip()


def _format_proc_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    text = re.sub(r"\.000(?=[+Z]|$)", "", text)
    text = text.replace("+03:00", " [GMT +3]").replace("+03", " [GMT +3]")
    return text


def _first_json_object(text: str) -> dict[str, Any]:
    dec = json.JSONDecoder()
    for i, ch in enumerate(str(text or "")):
        if ch != "{":
            continue
        try:
            value, _ = dec.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("В ответе модели не найден JSON-объект.")


def _lot_count_number(value: Any) -> int:
    text = str(value or "").strip()
    match = re.search(r"\d{1,4}", text)
    if not match:
        return 0
    return _safe_int(match.group(0), default=0)


def _looks_negative_divisibility(value: Any) -> bool:
    text = str(value or "").casefold()
    return any(marker in text for marker in ("нет", "неделим", "единый лот", "одна позиция", "1 позиция"))


def _parse_lot_count_response(raw: str) -> tuple[str, str]:
    obj = _first_json_object(raw)
    lot_count = "" if obj.get("lot_count") is None else str(obj.get("lot_count") or "").strip()
    partial = (
        ""
        if obj.get("partial_supply_allowed") is None
        else str(obj.get("partial_supply_allowed") or "").strip()
    )
    return lot_count, partial


def _product_rows_count(product_rows_info: Any) -> int:
    if not isinstance(product_rows_info, dict):
        return 0
    return _safe_int(product_rows_info.get("count"), default=0)


def _cell_by_header(headers: list[str], cells: list[str], *needles: str) -> str:
    folded_needles = tuple(n.casefold() for n in needles)
    for i, header in enumerate(headers):
        h = str(header or "").casefold()
        if all(n in h for n in folded_needles) and i < len(cells):
            return str(cells[i] or "").strip()
    return ""


def _first_date_value(cells: list[str]) -> str:
    for cell in cells:
        match = re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}\.\d{2}\.\d{4}\b", cell)
        if match:
            return match.group(0)
    return ""


def _lot_items_from_product_rows(product_rows_info: Any) -> list[dict[str, str]]:
    if not isinstance(product_rows_info, dict):
        return []
    headers = [str(h or "").strip() for h in (product_rows_info.get("headers") or [])]
    raw_rows = product_rows_info.get("rows") or []
    items: list[dict[str, str]] = []
    for idx, row in enumerate(raw_rows, start=1):
        if isinstance(row, dict):
            cells = [str(c or "").strip() for c in (row.get("cells") or []) if str(c or "").strip()]
            row_text = str(row.get("text") or "").strip()
        else:
            cells = []
            row_text = str(row or "").strip()
        if not cells and row_text:
            cells = [p.strip() for p in re.split(r"\s{2,}|\|", row_text) if p.strip()]
        if not cells and not row_text:
            continue
        if any("страница" in c.casefold() for c in cells) or any("позиций всего" in c.casefold() for c in cells):
            continue

        number = _cell_by_header(headers, cells, "№") or (cells[0] if cells and cells[0].isdigit() else str(idx))
        name = _cell_by_header(headers, cells, "наименование")
        if not name:
            text_candidates = [
                c for c in cells
                if not re.fullmatch(r"[\d\s.,]+", c)
                and "условная единица" not in c.casefold()
                and "руб" not in c.casefold()
            ]
            name = max(text_candidates, key=len, default=(cells[0] if cells else row_text))
        price_with_vat = (
            _cell_by_header(headers, cells, "цена", "ндс")
            or _cell_by_header(headers, cells, "сумма", "ндс")
        )
        price_without_vat = (
            _cell_by_header(headers, cells, "цена", "без")
            or _cell_by_header(headers, cells, "сумма", "без")
        )
        if not price_with_vat and len(cells) >= 6:
            # Типовая таблица ЭТП: №, Наименование, Код МТР, Количество, ЕИ,
            # Цена за весь объем с НДС, ...
            price_with_vat = cells[5]
        money_cells = [
            c for c in cells
            if re.search(r"\d[\d\s]*,\d{2}\b", c)
        ]
        if money_cells:
            price_with_vat = money_cells[0]
        price = price_with_vat or price_without_vat
        delivery_date = (
            _first_date_value(cells)
            or _cell_by_header(headers, cells, "ожидаемая", "дата")
            or _cell_by_header(headers, cells, "дата", "постав")
        )
        quantity = _cell_by_header(headers, cells, "количество")
        items.append(
            {
                "number": str(number or idx),
                "name": name[:1200],
                "price": price,
                "quantity": quantity,
                "delivery_date": delivery_date,
                "row_text": row_text,
            }
        )
    return items


def _apply_lot_item_to_parsed(
    parsed: dict[str, str] | None,
    item: dict[str, str],
    total_count: str,
    *,
    single_item: bool,
) -> dict[str, str]:
    lot_parsed = dict(parsed or {})
    lot_name = str(item.get("name") or "").strip()
    if lot_name:
        lot_parsed["procurement_subject"] = lot_name
        if not single_item:
            lot_parsed["tender_title"] = lot_name
    if item.get("price"):
        lot_parsed["starting_price"] = str(item["price"]).strip()
    if item.get("delivery_date"):
        lot_parsed["delivery_terms"] = str(item["delivery_date"]).strip()
    lot_parsed["lot_count"] = total_count
    lot_parsed["partial_supply_allowed"] = (
        "Нет, одна товарная позиция/единый лот"
        if total_count == "1"
        else f"Да, делимая: {total_count} товарных позиций/лотов"
    )
    return lot_parsed


def _rows_for_lots(
    *,
    registry: str,
    detail_url: str,
    doc_primary: str,
    parsed: dict[str, str] | None,
    err_msg: str | None,
    product_rows_info: Any,
) -> list[list[str]]:
    items = _lot_items_from_product_rows(product_rows_info)
    if not items:
        return [build_result_row(registry, detail_url, doc_primary, parsed, err_msg)]

    out: list[list[str]] = []
    total_count = str(_product_rows_count(product_rows_info) or len(items))
    single_item = len(items) == 1
    for item in items:
        lot_no = str(item.get("number") or "").strip()
        lot_parsed = _apply_lot_item_to_parsed(
            parsed,
            item,
            total_count,
            single_item=single_item,
        )
        row_registry = registry if single_item else f"{registry} / позиция {lot_no or len(out) + 1}"
        out.append(build_result_row(row_registry, detail_url, doc_primary, lot_parsed, err_msg))
    return out


def _apply_proc_defaults(
    parsed: dict[str, str] | None,
    proc: dict[str, Any],
    proc_title: str,
    page_text: str = "",
) -> dict[str, str]:
    out = dict(parsed or {})

    def empty(value: Any) -> bool:
        return _is_empty_analysis_value(value)

    lot0 = ((proc.get("lots") or [{}])[0] or {}) if isinstance(proc.get("lots"), list) else {}
    customers_info = lot0.get("customers_info") if isinstance(lot0, dict) else None
    customer_from_lot = ""
    if isinstance(customers_info, dict) and customers_info:
        first_info = next(iter(customers_info.values()), {}) or {}
        customer_from_lot = str(first_info.get("name") or "").strip()
    lot_customers = lot0.get("customers") if isinstance(lot0, dict) else None
    if not customer_from_lot and isinstance(lot_customers, list) and lot_customers:
        customer_from_lot = str(lot_customers[0] or "").strip()
    organizer = str(
        proc.get("organizer")
        or proc.get("customer_name")
        or proc.get("full_name")
        or proc.get("short_name")
        or customer_from_lot
        or ""
    ).strip()
    customer = str(out.get("customer_name") or "").casefold()
    if organizer and (
        empty(out.get("customer_name"))
        or "наименование участника" in customer
        or "участник конкурентной закупки" in customer
    ):
        out["customer_name"] = organizer

    if proc_title and (empty(out.get("tender_title")) or len(proc_title) > len(str(out.get("tender_title") or "")) + 8):
        out["tender_title"] = proc_title

    deadline = str(
        proc.get("date_end_registration")
        or proc.get("application_deadline")
        or proc.get("date_end")
        or ""
    ).strip()
    if deadline and empty(out.get("application_deadline")):
        out["application_deadline"] = _format_proc_datetime(deadline)

    results_date = str(
        proc.get("date_end_second_parts_review")
        or proc.get("step_second_parts")
        or ""
    ).strip()
    if results_date and empty(out.get("results_date")):
        out["results_date"] = _format_proc_datetime(results_date)

    has_retrade_date = any(
        str(proc.get(key) or lot0.get(key) or "").strip()
        for key in (
            "date_begin_final_offers",
            "date_end_final_offers",
            "date_begin_prices_matching",
            "date_end_prices_matching",
            "date_begin_comparisons_additional_price",
            "peretorg_date",
        )
    )
    if not has_retrade_date and not proc.get("peretorg_possible") and not lot0.get("is_peretorg"):
        out["retender_date"] = ""

    total_price = str(
        proc.get("total_price")
        or proc.get("price")
        or lot0.get("start_price")
        or ""
    ).strip()
    if total_price and empty(out.get("starting_price")):
        out["starting_price"] = total_price

    if empty(out.get("application_fee")):
        fee_match = re.search(
            r"(?:Взимание\s+Оператором\s+платы|Сумма,\s*блокируемая\s+при\s+подаче\s+заявки)[^\n\r]*[:\t]\s*([0-9][0-9\s\u00a0]*,\d{2})",
            page_text or "",
            re.I,
        )
        if fee_match:
            out["application_fee"] = fee_match.group(1).replace("\u00a0", " ").strip()

    for key, value in list(out.items()):
        out[key] = _clean_analysis_value(value)

    return out


def _extract_technical_table_via_lm(
    *,
    registry: str,
    detail_url: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any = None,
    lm_base_url: str,
    lm_model: str,
) -> tuple[dict[str, str], str]:
    product_info_text = ""
    if isinstance(product_rows_info, dict) and product_rows_info:
        product_info_text = json.dumps(product_rows_info, ensure_ascii=False, indent=2)[:16000]
    prompt = build_technical_user_prompt(
        registry=registry,
        detail_url=detail_url,
        page_text=_trim_for_llm(page_text, 80_000),
        documents_text=_trim_for_llm(documents_text, 120_000),
        product_rows_info_text=product_info_text,
    )
    raw = call_lm_studio_chat(
        lm_base_url,
        lm_model,
        build_technical_system_prompt(),
        prompt,
        timeout_sec=900,
        max_tokens=4096,
    )
    parsed = parse_technical_table_json(raw)
    return parsed, raw


def _extract_lot_count_from_card_via_lm(
    *,
    registry: str,
    detail_url: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any = None,
    lm_base_url: str,
    lm_model: str,
) -> tuple[str, str, str]:
    """Отдельный запрос к LM: всегда отдаём полную карточку для количества лотов."""
    parsed_product_count = _product_rows_count(product_rows_info)
    product_info_text = ""
    if isinstance(product_rows_info, dict) and product_rows_info:
        product_info_text = json.dumps(product_rows_info, ensure_ascii=False, indent=2)[:12000]
    if parsed_product_count > 1:
        return (
            str(parsed_product_count),
            f"Да, делимая: {parsed_product_count} товарных позиций/лотов",
            (
                "### product_rows_dom\n"
                "Количество лотов/товарных позиций определено первым этапом из DOM-блока "
                "div.x-fieldset-bwrap.\n"
                f"{product_info_text}"
            ),
        )

    system_prompt = (
        "Ты аналитик закупок секции Газпром. Твоя задача — определить делимость заявки "
        "и количество лотов/товарных позиций. Не считай регулярными выражениями, а проанализируй "
        "смысл карточки извещения и документов. Ответь только JSON-объектом без markdown."
    )
    user_prompt = (
        f"Реестровый номер: {registry}\n"
        f"URL карточки: {detail_url}\n\n"
        "Определи:\n"
        "1. lot_count — количество лотов или самостоятельных товарных позиций.\n"
        "2. partial_supply_allowed — делимая заявка/лот или нет.\n\n"
        "Правила:\n"
        "- Главный источник — полный текст страницы карточки/извещения ниже. Его нужно анализировать всегда.\n"
        "- Первый этап приложения уже попытался прочитать DOM-блок div.x-fieldset-bwrap с перечнем товаров. "
        "Если ниже указано, что найден 1 товар/строка, не меняй lot_count без явного основания, "
        "но обязательно проверь документы на формулировки о делимости заявки.\n"
        "- Сначала ищи список лотов и строку вроде «Позиций всего: N»/«Список лотов».\n"
        "- Если слово «лот» отсутствует, смотри перечень товаров: если самостоятельных товаров больше одного, "
        "укажи количество товаров как количество товарных позиций и признак делимости.\n"
        "- Если lot_count больше 1, partial_supply_allowed обязан быть положительным: "
        "«Да, делимая: N товарных позиций/лотов».\n"
        "- Если lot_count равен 1, partial_supply_allowed должен быть отрицательным или нейтральным: "
        "«Нет, одна позиция/единый лот», если нет другой прямой формулировки.\n"
        "- Документы используй как дополнительное подтверждение, если карточки недостаточно.\n"
        "- Если число определить нельзя, верни null для lot_count и объясни в partial_supply_allowed, "
        "что делимость не определена по доступному тексту.\n\n"
        "Формат ответа строго:\n"
        "{\"lot_count\": string|null, \"partial_supply_allowed\": string|null}\n\n"
        "ДАННЫЕ ПЕРВОГО ЭТАПА ИЗ DOM-БЛОКА div.x-fieldset-bwrap:\n"
        "-----\n"
        f"{product_info_text or '[перечень товаров в DOM не найден или не распознан]'}\n"
        "-----\n\n"
        "ПОЛНЫЙ ТЕКСТ СТРАНИЦЫ КАРТОЧКИ / ИЗВЕЩЕНИЯ:\n"
        "-----\n"
        f"{page_text or '[текст карточки не извлечён]'}\n"
        "-----\n\n"
        "РЕЛЕВАНТНЫЙ/ИЗВЛЕЧЁННЫЙ ТЕКСТ ДОКУМЕНТОВ ДЛЯ ПОДТВЕРЖДЕНИЯ:\n"
        "-----\n"
        f"{_trim_for_llm(documents_text, 80_000) or '[текст документов не извлечён]'}\n"
        "-----\n"
    )
    raw = call_lm_studio_chat(
        lm_base_url,
        lm_model,
        system_prompt,
        user_prompt,
        timeout_sec=900,
        max_tokens=1200,
    )
    lot_count, partial = _parse_lot_count_response(raw)
    if parsed_product_count == 1 and _is_empty_analysis_value(lot_count):
        lot_count = "1"
    if _lot_count_number(lot_count) > 1 and _looks_negative_divisibility(partial):
        correction_prompt = (
            "Ты вернул противоречивый ответ по закупке.\n"
            f"Предыдущий JSON/ответ:\n{raw}\n\n"
            f"В нём lot_count = {lot_count!r}, то есть товарных позиций/лотов больше одной, "
            f"но partial_supply_allowed = {partial!r}, что означает неделимость.\n\n"
            "Перепроверь полный текст карточки ниже и верни исправленный JSON. "
            "Если lot_count больше 1, partial_supply_allowed должен быть положительным: "
            "«Да, делимая: N товарных позиций/лотов». Ответ строго JSON без markdown.\n\n"
            "ПОЛНЫЙ ТЕКСТ СТРАНИЦЫ КАРТОЧКИ / ИЗВЕЩЕНИЯ:\n"
            "-----\n"
            f"{page_text or '[текст карточки не извлечён]'}\n"
            "-----\n\n"
            "Формат ответа строго:\n"
            "{\"lot_count\": string|null, \"partial_supply_allowed\": string|null}\n"
        )
        corrected_raw = call_lm_studio_chat(
            lm_base_url,
            lm_model,
            system_prompt,
            correction_prompt,
            timeout_sec=900,
            max_tokens=1200,
        )
        corrected_lot_count, corrected_partial = _parse_lot_count_response(corrected_raw)
        return corrected_lot_count, corrected_partial, raw + "\n\n### lot_count_full_card_correction\n" + corrected_raw
    return lot_count, partial, raw


def _apply_lot_count_from_card_lm(
    parsed: dict[str, str] | None,
    *,
    registry: str,
    detail_url: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any = None,
    lm_base_url: str,
    lm_model: str,
) -> tuple[dict[str, str] | None, str]:
    lot_count, partial, raw = _extract_lot_count_from_card_via_lm(
        registry=registry,
        detail_url=detail_url,
        page_text=page_text,
        documents_text=documents_text,
        product_rows_info=product_rows_info,
        lm_base_url=lm_base_url,
        lm_model=lm_model,
    )
    if parsed is None:
        parsed = {}
    if not _is_empty_analysis_value(lot_count):
        parsed["lot_count"] = lot_count
    if not _is_empty_analysis_value(partial):
        parsed["partial_supply_allowed"] = partial
    return parsed, raw


def _safe_int(value, default: int = 0) -> int:
    text = str(value or "").strip()
    if not text or text in {"-", "—", "–"}:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


class Worker(QObject):
    """Универсальный работник: выполняет одну задачу за жизнь.

    Сигналы:
        progress(str)      — сообщения о прогрессе
        session(bool, str) — результат проверки сессии
        batch(list, int, int) — загружена пачка: procedures, start, total
        debug(str)        — сырой запрос/ответ API для диагностики
        error(str)         — неперехваченное исключение
        finished()         — всегда вызывается после run()
    """

    progress = Signal(str)
    session = Signal(bool, str)
    batch = Signal(list, int, int)
    debug = Signal(str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn: Callable[["Worker"], None]) -> None:
        super().__init__()
        self._fn = fn
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def is_stop_requested(self) -> bool:
        return self._stop

    @Slot()
    def run(self) -> None:
        try:
            self._fn(self)
        except Exception as e:
            tb = traceback.format_exc()
            self.error.emit(f"{type(e).__name__}: {e}\n{tb}")
        finally:
            self.finished.emit()


class TaskRunner(QObject):
    """Запускает `Worker` в отдельном QThread. Гарантирует корректное завершение."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None

    def is_running(self) -> bool:
        return self._thread is not None

    def start(
        self,
        fn: Callable[[Worker], None],
        on_progress: Optional[Callable[[str], None]] = None,
        on_session: Optional[Callable[[bool, str], None]] = None,
        on_batch: Optional[Callable[[list, int, int], None]] = None,
        on_debug: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> Worker:
        if self._thread is not None:
            raise RuntimeError("Task already running")

        thread = QThread(self.parent())
        worker = Worker(fn)
        worker.moveToThread(thread)

        if on_progress:
            worker.progress.connect(on_progress)
        if on_session:
            worker.session.connect(on_session)
        if on_batch:
            worker.batch.connect(on_batch)
        if on_debug:
            worker.debug.connect(on_debug)
        if on_error:
            worker.error.connect(on_error)

        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        def _cleanup() -> None:
            self._thread = None
            self._worker = None
            if on_done:
                try:
                    on_done()
                except Exception:
                    traceback.print_exc()

        thread.finished.connect(_cleanup)
        thread.started.connect(worker.run)

        self._thread = thread
        self._worker = worker
        thread.start()
        return worker

    def request_stop(self) -> None:
        if self._worker:
            self._worker.request_stop()

    def shutdown(self, wait_ms: int = 3000) -> None:
        if self._worker:
            self._worker.request_stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(wait_ms)
        self._thread = None
        self._worker = None


# -----------------------------------------------------------------------------
# Задачи (запускаются внутри Worker)
# -----------------------------------------------------------------------------

def make_search_task(
    client: EtpClient,
    params: SearchParams,
    start: int,
    batches_left: int,
    client_filters=None,
) -> Callable[[Worker], None]:
    """Задача: запустить Chrome (если надо), подключиться, проверить сессию,
    скачать одну или несколько пачек.

    batches_left — сколько пачек подряд скачать. 1 = «одна». 9999 = «всё».
    """

    def _run(w: Worker) -> None:
        if w.is_stop_requested():
            return

        if not client.is_chrome_running():
            w.progress.emit(f"Запускаю {client.browser.label} с DevTools…")
            try:
                client.ensure_chrome(timeout=45)
            except Exception as e:
                w.error.emit(f"Не удалось запустить Chrome: {e}")
                return
        if w.is_stop_requested():
            return

        if client.driver is None:
            w.progress.emit(f"Подключаюсь к {client.browser.label} DevTools…")
            try:
                client.connect()
            except Exception as e:
                w.error.emit(f"Ошибка подключения к Chrome: {e}")
                return

        if w.is_stop_requested():
            return

        w.progress.emit("Получаю CSRF-токен…")
        try:
            client.pull_token()
        except Exception:
            pass

        if w.is_stop_requested():
            return

        cur_start = start
        loaded_this_task = 0
        accepted_this_task = 0
        total: Optional[int] = None
        pages_done = 0
        last_next_start = cur_start
        last_emitted_start = cur_start
        seen_keys: set[str] = set()
        probe_model = ProcedureTableModel()
        probe_proxy = ProcedureFilterProxy()
        probe_proxy.setSourceModel(probe_model)
        probe_filters = client_filters
        server_filter_variants = [client_filters]
        is_roseltorg = "roseltorg" in str(getattr(client, "target_host", ""))
        if client_filters is not None:
            if is_roseltorg:
                # Росэлторг уже фильтрует форму на сервере. Локально оставляем
                # только ключевые слова: это наш дополнительный отбор по названию.
                probe_filters = replace(
                    client_filters,
                    quick_search="",
                    registry_contains="",
                    unique_number_contains="",
                    organizer_contains="",
                    customer_contains="",
                    customer_region_contains="",
                    customer_agent_contains="",
                    title_contains="",
                    okpd2_contains="",
                    okved2_contains="",
                    guarantee_min=None,
                    guarantee_max=None,
                    responsible_contains="",
                    trend_pur="",
                    step_ids=(),
                    purchase_form="",
                    applics_min=None,
                    applics_max=None,
                    lots_min=None,
                    lots_max=None,
                    price_min=None,
                    price_max=None,
                    published_from=None,
                    published_to=None,
                    end_from=None,
                    end_to=None,
                    results_from=None,
                    results_to=None,
                    special_features_contains="",
                    position_name_contains="",
                    national_regime_contains="",
                )
            if not is_roseltorg:
                # Для секции Газпром дополнительные фильтры отправляются в Procedure.list
                # теми же полями, что использует сайт. Локально оставляем только
                # фильтр ключевых слов, которого нет в форме ЭТП.
                step_ids = tuple(getattr(client_filters, "step_ids", ()) or ())
                trend_values = tuple(getattr(client_filters, "trend_pur_values", ()) or ())
                concrete_step_ids = tuple(
                    step_id
                    for step_id in step_ids
                    if str(step_id).casefold().replace("ё", "е") != "активные"
                    and _server_status_value((step_id,)) != -2
                )
                local_step_ids = concrete_step_ids if step_ids else ()
                if len(step_ids) > 1 or len(trend_values) > 1:
                    step_ids_for_api = (
                        tuple(
                            step_id
                            for step_id in (concrete_step_ids or step_ids)
                            if _server_status_value((step_id,)) is not None
                        )
                        if len(step_ids) > 1
                        else (None,)
                    )
                    trend_values_for_api = trend_values if len(trend_values) > 1 else (None,)
                    server_filter_variants = []
                    for step_id in step_ids_for_api:
                        for trend_value in trend_values_for_api:
                            kwargs: dict[str, Any] = {}
                            if step_id is not None:
                                kwargs["step_ids"] = (step_id,)
                            if trend_value is not None:
                                kwargs["trend_pur"] = trend_value
                            server_filter_variants.append(replace(client_filters, **kwargs))
                    server_filter_variants = server_filter_variants or [client_filters]
                probe_filters = replace(
                    client_filters,
                    quick_search="",
                    registry_contains="",
                    unique_number_contains="",
                    organizer_contains="",
                    customer_contains="",
                    customer_region_contains="",
                    customer_agent_contains="",
                    title_contains="",
                    okpd2_contains="",
                    okved2_contains="",
                    guarantee_min=None,
                    guarantee_max=None,
                    responsible_contains="",
                    trend_pur="",
                    step_ids=local_step_ids,
                    purchase_form="",
                    applics_min=None,
                    applics_max=None,
                    lots_min=None,
                    lots_max=None,
                    price_min=None,
                    price_max=None,
                    published_from=None,
                    published_to=None,
                    end_from=None,
                    end_to=None,
                    results_from=None,
                    results_to=None,
                    special_features_contains="",
                    position_name_contains="",
                    national_regime_contains="",
                )
            probe_proxy.set_filters(probe_filters)
        aggregate_total = 0
        aggregate_processed = 0
        for filter_variant in server_filter_variants:
            cur_start = start
            variant_total: Optional[int] = None
            pages_done = 0
            set_client_filters = getattr(client, "set_client_filters", None)
            if callable(set_client_filters):
                set_client_filters(filter_variant)

            while True:
                if w.is_stop_requested():
                    return
                request_limit = max(1, int(params.limit or HARD_SERVER_LIMIT))
                if is_roseltorg:
                    request_limit = min(request_limit, 30)
                w.progress.emit(
                    "Ищу процедуры..."
                    + (f" Найдено подходящих: {accepted_this_task}." if accepted_this_task else "")
                )
                fetch_kwargs = {
                    "start": cur_start,
                    "limit": request_limit,
                    "date_from": params.date_from or None,
                    "date_to": params.date_to or None,
                    "query": (
                        params.query
                        or (
                            getattr(filter_variant, "quick_search", "")
                            if filter_variant is not None
                            else ""
                        )
                        or None
                    ),
                    "tag_id": params.tag_id,
                    "sort": params.sort,
                    "direction": params.direction,
                }
                if not is_roseltorg:
                    fetch_kwargs["client_filters"] = filter_variant
                res = client.fetch_page(**fetch_kwargs)
                debug_payload = res.get("_debug") if isinstance(res, dict) else None
                if debug_payload is not None:
                    try:
                        filters_debug = (
                            asdict(filter_variant)
                            if is_dataclass(filter_variant)
                            else filter_variant
                        )
                        w.debug.emit(
                            json.dumps(
                                {
                                    "page_start": cur_start,
                                    "request_limit": request_limit,
                                    "accepted_before_page": accepted_this_task,
                                    "client_filters": filters_debug,
                                    "api": debug_payload,
                                },
                                ensure_ascii=False,
                                indent=2,
                                default=str,
                            )
                        )
                    except Exception:
                        w.debug.emit(str(debug_payload))
                if w.is_stop_requested():
                    return
                if res.get("error"):
                    err_text = str(res["error"])
                    err_low = err_text.lower()
                    if (
                        "unexpected token '<'" in err_low
                        or "not valid json" in err_low
                        or "json.parse" in err_low
                    ):
                        host = str(getattr(client, "target_host", ""))
                        login_hint = (
                            "В Chrome откройте секцию Бизнес.223, выполните вход до конца, "
                            "дождитесь загрузки списка процедур и снова нажмите «Поиск»."
                            if "etp.gpb.ru" in host
                            else "В Chrome: «Войти» → «ЕСИА + ЭП» → пройдите до конца, "
                            "затем снова нажмите «Поиск»."
                        )
                        w.session.emit(
                            False,
                            f"Сессия не активна или требуется авторизация.\n\n{login_hint}",
                        )
                        return
                    if (
                        "no such window" in err_low
                        or "web view not found" in err_low
                        or "target window already closed" in err_low
                        or "target frame detached" in err_low
                        or "invalid session id" in err_low
                    ):
                        platform_name = "Росэлторга" if is_roseltorg else "ЭТП"
                        short = (
                            f"Вкладка {platform_name} была закрыта или браузер потерял сессию. "
                            "Открыл её заново — попробуйте ещё раз нажать «Поиск»."
                        )
                        w.error.emit(short)
                    else:
                        w.error.emit(f"Сервер вернул ошибку: {err_text}")
                    return
                if res.get("no_access") or res.get("no_session"):
                    msg = res.get("message") or "Нет доступа / сессия не активна."
                    host = str(getattr(client, "target_host", ""))
                    login_hint = (
                        "В Chrome откройте Росэлторг, выполните вход через ЭЦП до конца, "
                        "затем снова нажмите «Поиск»."
                        if "roseltorg" in host
                        else "В Chrome откройте секцию Бизнес.223, выполните вход до конца, "
                        "дождитесь загрузки списка процедур и снова нажмите «Поиск»."
                        if "etp.gpb.ru" in host
                        else "В Chrome: «Войти» → «ЕСИА + ЭП» → пройдите до конца, "
                        "затем снова нажмите «Поиск»."
                    )
                    w.session.emit(
                        False,
                        f"{msg}\n\n{login_hint}",
                    )
                    return
                procs = res.get("procedures") or []
                if variant_total is None:
                    variant_total = _safe_int(res.get("totalCount"), len(procs))
                    aggregate_total += variant_total
                    total = aggregate_total
                accepted = procs
                if probe_filters is not None:
                    probe_model.set_rows(procs)
                    accepted = []
                    for source_row in probe_proxy.filtered_source_rows():
                        row = probe_model.row_at(source_row)
                        if row is not None:
                            accepted.append(row)
                deduped: list[dict] = []
                for row in accepted:
                    key = str(
                        row.get("id")
                        or row.get("registry_number")
                        or row.get("procedure_number")
                        or row.get("procedure_number2")
                        or id(row)
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    deduped.append(row)
                next_start = cur_start + len(procs)
                aggregate_processed += len(procs)
                last_next_start = aggregate_processed
                if deduped:
                    w.batch.emit(deduped, aggregate_processed, aggregate_total or 0)
                    last_emitted_start = aggregate_processed
                    accepted_this_task += len(deduped)
                else:
                    w.batch.emit([], aggregate_processed, aggregate_total or 0)
                    last_emitted_start = aggregate_processed
                loaded_this_task += len(procs)
                pages_done += 1
                reached_user_batch = accepted_this_task >= request_limit
                if not procs:
                    break
                if variant_total and next_start >= variant_total:
                    break
                if batches_left == 1 and reached_user_batch:
                    # Один пользовательский батч = примерно request_limit строк,
                    # которые прошли клиентские фильтры и попадут в таблицу.
                    break
                if batches_left != 1 and pages_done >= batches_left:
                    break
                cur_start = next_start

        if last_emitted_start != last_next_start:
            w.batch.emit([], last_next_start, aggregate_total or 0)
        w.session.emit(True, "Готово.")

    return _run


def make_download_documents_task(
    client: EtpClient,
    procedures: list[dict],
    output_dir: Path,
) -> Callable[[Worker], None]:
    """Задача: скачать документы по выбранным процедурам."""

    def _run(w: Worker) -> None:
        if not procedures:
            w.error.emit("Не выбраны процедуры для скачивания документов.")
            return

        if not client.is_chrome_running():
            w.progress.emit(f"Запускаю {client.browser.label} с DevTools…")
            try:
                client.ensure_chrome(timeout=45)
            except Exception as e:
                w.error.emit(f"Не удалось запустить Chrome: {e}")
                return

        if client.driver is None:
            w.progress.emit(f"Подключаюсь к {client.browser.label} DevTools…")
            try:
                client.connect()
            except Exception as e:
                w.error.emit(f"Ошибка подключения к Chrome: {e}")
                return

        results: list[dict] = []
        for index, proc in enumerate(procedures, start=1):
            if w.is_stop_requested():
                return
            registry = proc.get("registry_number") or proc.get("procedure_number") or proc.get("id")
            w.progress.emit(f"Скачиваю документы {index}/{len(procedures)}: {registry}")
            try:
                result = client.download_procedure_documents(
                    proc,
                    output_dir,
                    progress=w.progress.emit,
                )
                saved_paths = [Path(p) for p in (result.get("saved") or [])]
                archive_paths = [p for p in saved_paths if p.is_file() and _is_archive(p)]
                if archive_paths:
                    unpack_dir = Path(str(result.get("folder") or output_dir)) / "разархивированные_документы"
                    issues: list[dict] = []
                    prepare_documents_for_analysis(
                        archive_paths,
                        unpack_dir,
                        progress=w.progress.emit,
                        issues=issues,
                        registry=str(registry or ""),
                    )
                    result["unpacked_folder"] = str(unpack_dir)
                    result["unpack_issues"] = issues
                if w.is_stop_requested():
                    return
                results.append(result)
                w.progress.emit(
                    f"{registry}: скачано {len(result.get('saved') or [])} "
                    f"из {result.get('found') or 0} файлов"
                )
            except Exception as e:
                results.append({"procedure": registry, "saved": [], "errors": [str(e)]})
                w.progress.emit(f"{registry}: ошибка скачивания: {e}")

        saved_count = sum(len(r.get("saved") or []) for r in results)
        error_count = sum(len(r.get("errors") or []) for r in results)
        w.session.emit(
            True,
            f"Скачивание завершено. Файлов: {saved_count}, ошибок: {error_count}. "
            f"Папка: {output_dir}",
        )

    return _run


def make_analyze_procedure_task(
    client: EtpClient,
    procedures: list[dict],
    lm_base_url: str,
    lm_model: str,
    sink: dict,
) -> Callable[[Worker], None]:
    """Карточка секции Газпром → текст страницы и документов → при наличии зависимостей RAG (FAISS+e5) поштучное извлечение полей в LM Studio; иначе один запрос ко всему тексту."""

    def _run(w: Worker) -> None:
        sink.clear()
        sink["rows"] = []
        sink["raw_by_registry"] = {}
        sink["title_by_registry"] = {}
        sink["unpacked_docs_by_registry"] = {}
        sink["document_issues"] = []
        sink["technical_by_registry"] = {}

        if not procedures:
            w.error.emit("Не выбраны процедуры для анализа.")
            return

        if not client.is_chrome_running():
            w.progress.emit(f"Запускаю {client.browser.label} с DevTools…")
            try:
                client.ensure_chrome(timeout=45)
            except Exception as e:
                w.error.emit(f"Не удалось запустить Chrome: {e}")
                return

        if client.driver is None:
            w.progress.emit(f"Подключаюсь к {client.browser.label} DevTools…")
            try:
                client.connect()
            except Exception as e:
                w.error.emit(f"Ошибка подключения к Chrome: {e}")
                return

        # Важно сделать это ДО чтения документов: OCR может загрузить Paddle,
        # после чего на Windows импорт torch иногда падает на shm.dll.
        rag_available = ragged_analysis_available()
        rows: list[list[str]] = []

        for index, proc in enumerate(procedures, start=1):
            if w.is_stop_requested():
                return
            registry = str(
                proc.get("registry_number") or proc.get("procedure_number") or proc.get("id") or ""
            )
            proc_title = str(proc.get("title") or proc.get("name") or "").strip()
            sink["title_by_registry"][registry] = proc_title
            w.progress.emit(f"Сбор текста карточки {index}/{len(procedures)}: {registry}")
            try:
                snap = client.extract_procedure_card_text(proc, progress=w.progress.emit)
            except Exception as e:
                pid = proc.get("id") or proc.get("procedure_id") or ""
                detail = VIEW_URL.format(pid=pid) if pid else ""
                rows.append(build_result_row(registry, detail, "", None, str(e)))
                sink["raw_by_registry"][registry] = f"Ошибка сбора страницы: {e}"
                continue

            page_text = str(snap.get("page_text") or "")
            detail_url = str(snap.get("url") or "")
            doc_primary = str(snap.get("primary_doc_url") or "")
            doc_list = snap.get("doc_links") or []
            product_rows_info = snap.get("product_rows_info") or {}
            doc_summary = "; ".join(
                str((d or {}).get("href") or "")
                for d in (doc_list if isinstance(doc_list, list) else [])
                if isinstance(d, dict) and (d.get("href"))
            )[:4000]
            downloaded_docs: list[Path] = []
            documents_text = ""
            unpacked_dir = ANALYSIS_DIR / "разархивированные_документы" / _safe_folder_name(registry)
            unpacked_dir.mkdir(parents=True, exist_ok=True)
            sink["unpacked_docs_by_registry"][registry] = str(unpacked_dir)
            try:
                snapshot = unpacked_dir / "_карточка_страницы_полный_текст.txt"
                snapshot.write_text(
                    f"URL карточки: {detail_url}\nРеестровый номер: {registry}\n\n{page_text}",
                    encoding="utf-8",
                )
            except Exception:
                pass
            if not isinstance(doc_list, list) or not doc_list:
                note = "На странице карточки не найдены ссылки на документы для скачивания."
                (unpacked_dir / "Документы_не_найдены.txt").write_text(note, encoding="utf-8")
                sink["document_issues"].append(
                    {
                        "severity": "important",
                        "registry": registry,
                        "file": "",
                        "message": note,
                    }
                )
                documents_text += f"\n--- Документы ---\n[{note}]\n"
            else:
                docs_dir = ANALYSIS_DIR / "_downloaded_docs" / _safe_folder_name(registry)
                for doc_index, link in enumerate(doc_list, start=1):
                    if w.is_stop_requested():
                        return
                    if not isinstance(link, dict) or not link.get("href"):
                        continue
                    try:
                        w.progress.emit(
                            f"Скачиваю документ {doc_index}/{len(doc_list)} для анализа: {registry}"
                        )
                        downloaded_docs.append(
                            client.download_document_link(link, docs_dir, index=doc_index)
                        )
                    except Exception as e:
                        err_note = (
                            f"Документ {doc_index}: {(link or {}).get('text') or (link or {}).get('href')}\n"
                            f"Не удалось скачать: {e}\n"
                        )
                        (unpacked_dir / f"Ошибка_скачивания_{doc_index}.txt").write_text(
                            err_note,
                            encoding="utf-8",
                        )
                        sink["document_issues"].append(
                            {
                                "severity": "critical",
                                "registry": registry,
                                "file": str((link or {}).get("text") or (link or {}).get("href") or ""),
                                "message": f"Не удалось скачать документ: {e}",
                            }
                        )
                        documents_text += (
                            f"\n--- Документ {doc_index}: {(link or {}).get('text') or (link or {}).get('href')} ---\n"
                            f"[не удалось скачать: {e}]\n"
                        )
                if downloaded_docs:
                    try:
                        extracted_text, extracted_folder = prepare_documents_for_analysis(
                            downloaded_docs,
                            unpacked_dir,
                            progress=w.progress.emit,
                            issues=sink["document_issues"],
                            registry=registry,
                        )
                    except Exception as prep_exc:
                        sink["document_issues"].append(
                            {
                                "severity": "critical",
                                "registry": registry,
                                "file": "",
                                "message": f"Не удалось подготовить документы: {prep_exc}",
                            }
                        )
                        extracted_text = ""
                        extracted_folder = unpacked_dir
                    documents_text += "\n" + extracted_text
                    try:
                        Path(extracted_folder).mkdir(parents=True, exist_ok=True)
                    except Exception:
                        pass
                    sink["unpacked_docs_by_registry"][registry] = str(extracted_folder)
                else:
                    note = "Ссылки на документы были найдены, но скачать документы не удалось."
                    (unpacked_dir / "Документы_не_скачаны.txt").write_text(note, encoding="utf-8")
                    sink["document_issues"].append(
                        {
                            "severity": "critical",
                            "registry": registry,
                            "file": "",
                            "message": note,
                        }
                    )

            parsed = None
            raw_llm = ""
            err_msg: str | None = None
            rag_used = False

            if rag_available:
                try:
                    w.progress.emit(f"RAG: индексация и извлечение полей для {registry}…")
                    ingest_notes: list[str] = []
                    debug_dir = ANALYSIS_DIR / "rag_debug" / _safe_folder_name(registry)
                    parsed, raw_llm = run_rag_table_analysis(
                        registry=registry,
                        page_text=page_text,
                        card_url=detail_url,
                        unpacked_dir=unpacked_dir,
                        lm_base_url=lm_base_url,
                        lm_model=lm_model,
                        progress=w.progress.emit,
                        stop_flag=w.is_stop_requested,
                        debug_dir=debug_dir,
                        ingest_notes_out=ingest_notes,
                    )
                    rag_used = True
                    sink["raw_by_registry"][registry] = raw_llm
                    if _analysis_filled_count(parsed) < 3:
                        rag_used = False
                        sink["document_issues"].append(
                            {
                                "severity": "important",
                                "registry": registry,
                                "file": "RAG",
                                "message": (
                                    "RAG вернул слишком мало заполненных полей; "
                                    "повторяю анализ одним запросом по полному тексту карточки и документов."
                                ),
                            }
                        )
                    for note in ingest_notes:
                        sink["document_issues"].append(
                            {
                                "severity": "important",
                                "registry": registry,
                                "file": "",
                                "message": note,
                            }
                        )
                except Exception as rag_exc:
                    sink["document_issues"].append(
                        {
                            "severity": "important",
                            "registry": registry,
                            "file": "RAG",
                            "message": (
                                "RAG-пайплайн недоступен или завершился ошибкой; "
                                f"используется один запрос ко всему тексту: {rag_exc}"
                            ),
                        }
                    )

            if not rag_used:
                system_prompt = build_analysis_system_prompt()
                w.progress.emit(f"Запрос к LM Studio ({lm_model}) для {registry}…")
                try:
                    user_prompt = build_analysis_user_prompt(
                        registry, detail_url, doc_summary, page_text, documents_text
                    )
                    raw_llm = call_lm_studio_chat(
                        lm_base_url, lm_model, system_prompt, user_prompt, timeout_sec=900
                    )
                    sink["raw_by_registry"][registry] = raw_llm
                    parsed = parse_llm_table_json(raw_llm)
                except Exception as e:
                    first_err = str(e)
                    sink["document_issues"].append(
                        {
                            "severity": "important",
                            "registry": registry,
                            "file": "LM Studio",
                            "message": (
                                f"Первый запрос к модели не выполнен: {first_err}. "
                                "Пробую укороченный контекст."
                            ),
                        }
                    )
                    try:
                        w.progress.emit(
                            f"Повторный запрос к LM Studio с укороченным контекстом: {registry}…"
                        )
                        short_prompt = build_analysis_user_prompt(
                            registry,
                            detail_url,
                            doc_summary,
                            _trim_for_llm(page_text, 60_000),
                            _trim_for_llm(documents_text, 20_000),
                        )
                        raw_llm = call_lm_studio_chat(
                            lm_base_url,
                            lm_model,
                            system_prompt,
                            short_prompt,
                            timeout_sec=900,
                        )
                        sink["raw_by_registry"][registry] = raw_llm
                        parsed = parse_llm_table_json(raw_llm)
                    except Exception as retry_error:
                        err_msg = str(retry_error)
                        sink["document_issues"].append(
                            {
                                "severity": "critical",
                                "registry": registry,
                                "file": "LM Studio",
                                "message": f"Повторный запрос к модели не выполнен: {err_msg}",
                            }
                        )
                        sink["raw_by_registry"][registry] = (
                            raw_llm
                            + ("\n---\n" if raw_llm else "")
                            + f"Первый запрос: {first_err}\nПовторный запрос: {err_msg}"
                        )

            parsed = _apply_proc_defaults(parsed, proc, proc_title, page_text)
            try:
                w.progress.emit(f"LM Studio: определяю количество лотов по полной карточке {registry}…")
                parsed, lot_raw = _apply_lot_count_from_card_lm(
                    parsed,
                    registry=registry,
                    detail_url=detail_url,
                    page_text=page_text,
                    documents_text=documents_text,
                    product_rows_info=product_rows_info,
                    lm_base_url=lm_base_url,
                    lm_model=lm_model,
                )
                previous_raw = str(sink["raw_by_registry"].get(registry) or "")
                sink["raw_by_registry"][registry] = (
                    previous_raw
                    + ("\n\n" if previous_raw else "")
                    + "### lot_count_full_card\n"
                    + lot_raw
                )
            except Exception as lot_exc:
                sink["document_issues"].append(
                    {
                        "severity": "important",
                        "registry": registry,
                        "file": "LM Studio",
                        "message": (
                            "Не удалось отдельно определить количество лотов "
                            f"по полной карточке: {lot_exc}"
                        ),
                    }
                )
            try:
                w.progress.emit(f"LM Studio: извлекаю технические характеристики {registry}…")
                technical, technical_raw = _extract_technical_table_via_lm(
                    registry=registry,
                    detail_url=detail_url,
                    page_text=page_text,
                    documents_text=documents_text,
                    product_rows_info=product_rows_info,
                    lm_base_url=lm_base_url,
                    lm_model=lm_model,
                )
                sink["technical_by_registry"][registry] = technical
                previous_raw = str(sink["raw_by_registry"].get(registry) or "")
                sink["raw_by_registry"][registry] = (
                    previous_raw
                    + ("\n\n" if previous_raw else "")
                    + "### technical_table\n"
                    + technical_raw
                )
            except Exception as tech_exc:
                sink["document_issues"].append(
                    {
                        "severity": "important",
                        "registry": registry,
                        "file": "LM Studio",
                        "message": (
                            "Не удалось извлечь вторую таблицу технических характеристик: "
                            f"{tech_exc}"
                        ),
                    }
                )
            rows.extend(
                _rows_for_lots(
                    registry=registry,
                    detail_url=detail_url,
                    doc_primary=doc_primary,
                    parsed=parsed,
                    err_msg=err_msg,
                    product_rows_info=product_rows_info,
                )
            )

        sink["rows"] = rows
        w.session.emit(
            True,
            f"Анализ завершён: {len(rows)} процедур. LM Studio: {lm_base_url}, модель {lm_model}.",
        )

    return _run
