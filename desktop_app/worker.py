from __future__ import annotations

import ast
import json
import re
import time
import traceback
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QThread, Signal, Slot

from etp_client import HARD_SERVER_LIMIT, EtpClient, _server_status_value

from .constants import ANALYSIS_DIR, EQUIPMENT_API_BASE_URL, VIEW_URL
from .document_text import _is_archive, prepare_documents_for_analysis
from .gpb_rag.pipeline import ragged_analysis_available, run_rag_table_analysis
from .gpb_rag.schemas import FieldSource
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
    if isinstance(value, (list, tuple, set)):
        return not any(str(item).strip() for item in value)
    text = str(value or "").strip()
    folded = text.casefold()
    if folded in {"", "—", "-", "null", "none", "[]", "{}", "не указано", "нет данных"}:
        return True
    # Перечисление пунктов (list-repr) и длинные содержательные значения — это реальные
    # данные. Модель оформляет «отказ» короткой фразой в начале ответа, поэтому маркеры
    # отказа ищем только в начале короткого однострочного значения. Иначе валидный список
    # (например, риски с пунктом «… если документы не представлены …») обнуляется целиком.
    if re.fullmatch(r"\[[\s\S]*\]", text) or len(text) > 300:
        return False
    head = folded[:150]
    return any(
        marker in head
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


def _format_bullet_list_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        items = []
        if re.fullmatch(r"\[[\s\S]*\]", text):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, (list, tuple, set)):
                items = [str(item).strip() for item in parsed if str(item).strip()]
        if not items:
            return text
    if len(items) <= 1:
        return items[0] if items else ""
    return "\n".join(f"• {item}" for item in items)


def _format_russian_date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""

    def _tz_to_gmt(raw: str) -> str:
        tz = (raw or "").strip()
        if not tz:
            return " [GMT +3]"
        if tz.upper() == "Z":
            return " [GMT +0]"
        gmt_match = re.search(r"GMT\s*([+-])\s*0?(\d{1,2})", tz, re.I)
        if gmt_match:
            return f" [GMT {gmt_match.group(1)}{int(gmt_match.group(2))}]"
        offset_match = re.search(r"([+-])(\d{2})(?::?(\d{2}))?", tz)
        if offset_match:
            return f" [GMT {offset_match.group(1)}{int(offset_match.group(2))}]"
        return " [GMT +3]"

    pattern = re.compile(
        r"\b(20\d{2})-(\d{2})-(\d{2})"
        r"(?:[T\s]+(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
        r"(?:\s*(\[(?:GMT|UTC)\s*[+-]\s*\d{1,2}\]|[+-]\d{2}:?\d{0,2}|Z))?"
        r")?",
        re.I,
    )

    def repl(match: re.Match[str]) -> str:
        year, month, day = match.group(1), match.group(2), match.group(3)
        hour, minute, second = match.group(4), match.group(5), match.group(6)
        if not hour:
            return f"{day}.{month}.{year}"
        return f"{day}.{month}.{year} {hour}:{minute}:{second or '00'}{_tz_to_gmt(match.group(7) or '')}"

    return pattern.sub(repl, text)


def _format_proc_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    text = re.sub(r"\.000(?=[+Z]|$)", "", text)
    text = text.replace("+03:00", " [GMT +3]").replace("+03", " [GMT +3]")
    return _format_russian_date_text(text)


def _normalize_card_datetime(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip(" .,:;")
    if not text:
        return ""
    iso_match = re.search(
        r"\b20\d{2}-\d{2}-\d{2}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{0,2}|\[(?:GMT|UTC)\s*[+-]\s*\d{1,2}\]))?)?",
        text,
        re.I,
    )
    if iso_match:
        return _format_proc_datetime(iso_match.group(0))
    ru_match = re.search(
        r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
        r"(?:\s*(?:\[?\s*(?:GMT|UTC)\s*([+-])\s*(\d{1,2})\s*\]?|([+-])(\d{2}):?(\d{2})?))?",
        text,
        re.I,
    )
    if not ru_match:
        return ""
    day = int(ru_match.group(1))
    month = int(ru_match.group(2))
    year = ru_match.group(3)
    hour = ru_match.group(4)
    minute = ru_match.group(5)
    second = ru_match.group(6) or "00"
    if not hour:
        return f"{day:02d}.{month:02d}.{year}"
    sign = ru_match.group(7) or ru_match.group(9) or "+"
    tz_hour = ru_match.group(8) or ru_match.group(10) or "3"
    return f"{day:02d}.{month:02d}.{year} {int(hour):02d}:{minute}:{second} [GMT {sign}{int(tz_hour)}]"


def _extract_retender_date_from_card(page_text: str) -> str:
    text = str(page_text or "").replace("\u00a0", " ")
    if not text.strip():
        return ""
    labels = (
        "Дата и время окончания срока подачи новых коммерческих предложений",
        "Окончание срока подачи новых коммерческих предложений",
    )
    for label in labels:
        match = re.search(re.escape(label), text, re.I)
        if not match:
            continue
        fragment = text[match.end() : match.end() + 350]
        value = _normalize_card_datetime(fragment)
        if value:
            return value
    return ""


def _source_to_dict(source: FieldSource) -> dict[str, Any]:
    return {
        "source_type": source.source_type,
        "label": source.label,
        "file_name": source.file_name,
        "text": source.text,
        "page": source.page,
        "section": source.section,
        "chunk_id": source.chunk_id,
        "url": source.url,
        "score": source.score,
    }


def _card_source(label: str, url: str, text: str = "") -> dict[str, Any]:
    return {
        "source_type": "card",
        "label": f"Карточка: {label}",
        "file_name": "",
        "text": text,
        "page": None,
        "section": label,
        "chunk_id": "",
        "url": url,
        "score": None,
    }


def _document_like_source(label: str, text: str = "") -> dict[str, Any]:
    return {
        "source_type": "document",
        "label": label,
        "file_name": label,
        "text": text,
        "page": None,
        "section": "",
        "chunk_id": "",
        "url": "",
        "score": None,
    }


def _document_sections_from_text(documents_text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    pattern = re.compile(r"^---\s*[^:\n\r]+:\s*(.*?)\s*---\s*$", re.M)
    matches = list(pattern.finditer(documents_text or ""))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(documents_text)
        file_name = str(match.group(1) or "").strip()
        text = (documents_text[start:end] or "").strip()
        if file_name and text:
            sections.append({"file_name": file_name, "text": text})
    return sections


def _best_document_source_for_value(
    documents_text: str,
    *,
    value: str,
    lot_name: str = "",
) -> dict[str, Any] | None:
    sections = _document_sections_from_text(documents_text)
    if not sections:
        return None
    value_text = str(value or "").strip()
    lot_text = str(lot_name or "").strip()
    value_terms = [t for t in re.split(r"\s+", value_text.casefold()) if len(t) >= 4][:8]
    lot_terms = [t for t in re.split(r"\s+", lot_text.casefold()) if len(t) >= 4][:8]

    def score(section: dict[str, str]) -> int:
        text = section["text"].casefold()
        points = 0
        if value_text and value_text.casefold() in text:
            points += 20
        if lot_text and lot_text.casefold() in text:
            points += 10
        points += sum(2 for term in value_terms if term in text)
        points += sum(1 for term in lot_terms if term in text)
        return points

    best = max(sections, key=score)
    if score(best) <= 0:
        return None
    raw_text = best["text"]
    needle = value_terms[0] if value_terms else (lot_terms[0] if lot_terms else "")
    pos = raw_text.casefold().find(needle) if needle else -1
    if pos >= 0:
        start = max(0, pos - 1200)
        end = min(len(raw_text), pos + 2400)
        snippet = raw_text[start:end]
    else:
        snippet = raw_text[:3600]
    file_name = best["file_name"]
    return {
        "source_type": "document",
        "label": f"Документ: {file_name}",
        "file_name": file_name,
        "text": snippet,
        "page": None,
        "section": "",
        "chunk_id": "",
        "url": "",
        "score": None,
    }


def _add_field_source(
    sources_by_field: dict[str, list[dict[str, Any]]],
    field_key: str,
    source: dict[str, Any],
    *,
    prepend: bool = True,
) -> None:
    bucket = sources_by_field.setdefault(field_key, [])
    if any(
        existing.get("source_type") == source.get("source_type")
        and existing.get("label") == source.get("label")
        and existing.get("file_name") == source.get("file_name")
        for existing in bucket
    ):
        return
    if prepend:
        bucket.insert(0, source)
    else:
        bucket.append(source)


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


def _product_rows_info_from_page_text(page_text: str) -> dict[str, Any]:
    text = str(page_text or "")
    if not text:
        return {}
    marker = "=== ВАЖНЫЙ ФРАГМЕНТ: таблица перечня товаров"
    pos = text.find(marker)
    if pos >= 0:
        block = text[pos : pos + 12000]
    else:
        match = re.search(r"Перечень\s+товаров[\s\S]{0,12000}", text, re.I)
        block = match.group(0) if match else ""
    if not block:
        return {}

    count_match = (
        re.search(r"Количество\s+строк/позиций:\s*(\d{1,4})", block, re.I)
        or re.search(r"Позиций\s+всего\s*[:\-]?\s*(\d{1,4})", block, re.I)
        or re.search(r"Всего\s+позиций\s*[:\-]?\s*(\d{1,4})", block, re.I)
    )
    count = _safe_int(count_match.group(1), default=0) if count_match else 0
    headers: list[str] = []
    header_match = re.search(r"Заголовки:\s*(.+)", block)
    if header_match:
        headers = [h.strip() for h in header_match.group(1).split("|") if h.strip()]

    rows: list[dict[str, Any]] = []
    rows_match = re.search(r"Строки\s+таблицы:\s*([\s\S]+?)(?:\n===|\Z)", block, re.I)
    if rows_match:
        for line in rows_match.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^\d+\.\s*", "", line)
            cells = [p.strip() for p in line.split("|") if p.strip()]
            if cells or line:
                rows.append({"text": line, "cells": cells})

    return {
        "count": count or len(rows),
        "source": "page_text_product_table",
        "text": block[:6000],
        "headers": headers,
        "rows": rows,
    }


def _best_product_rows_info(product_rows_info: Any, page_text: str) -> dict[str, Any]:
    current = product_rows_info if isinstance(product_rows_info, dict) else {}
    fallback = _product_rows_info_from_page_text(page_text)
    current_count = _product_rows_count(current)
    fallback_count = _product_rows_count(fallback)
    current_rows = current.get("rows") if isinstance(current.get("rows"), list) else []
    fallback_rows = fallback.get("rows") if isinstance(fallback.get("rows"), list) else []
    if fallback_count > current_count:
        return fallback
    if fallback_count == current_count and len(fallback_rows) > len(current_rows):
        merged = dict(current)
        merged["rows"] = fallback_rows
        if not merged.get("text"):
            merged["text"] = fallback.get("text", "")
        if not merged.get("headers"):
            merged["headers"] = fallback.get("headers", [])
        return merged
    return dict(current)


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


def _lot_items_from_product_text(product_rows_info: Any) -> list[dict[str, str]]:
    if not isinstance(product_rows_info, dict):
        return []
    count = _product_rows_count(product_rows_info)
    if count <= 1:
        return []
    text = re.sub(r"\s+", " ", str(product_rows_info.get("text") or "")).strip()
    if not text:
        return []

    unit_pattern = r"(?:Условн(?:ая|ые|ых)\s+единиц[аы]?|Штук[аи]?|шт\.?|комплект(?:ы|ов)?|ед\.?|услуг[аи]?|упаковк[аи]?)"
    items: list[dict[str, str]] = []
    for idx in range(1, count + 1):
        next_idx = idx + 1
        if idx < count:
            pattern = rf"(?:^|\s){idx}\s+(.+?)(?=\s+{next_idx}\s+|$)"
        else:
            pattern = rf"(?:^|\s){idx}\s+(.+?)(?:\s+Страница\b|\s+Позиций\s+всего\b|$)"
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        segment = match.group(1).strip(" |")
        if not segment:
            continue

        quantity = ""
        q_match = re.search(rf"\b(\d+(?:[.,]\d+)?)\s+{unit_pattern}\b", segment, re.I)
        if q_match:
            quantity = q_match.group(1)
            name = segment[: q_match.start()].strip(" |")
        else:
            money_pos = re.search(r"\d[\d\s]*,\d{2}\b", segment)
            name = segment[: money_pos.start()].strip(" |") if money_pos else segment

        name = re.sub(r"\s+", " ", name).strip()
        money_cells = re.findall(r"\d[\d\s]*,\d{2}\b", segment)
        delivery_date = _first_date_value([segment])
        if not delivery_date:
            year_match = re.search(r"\b20\d{2}\b", segment)
            delivery_date = year_match.group(0) if year_match else ""

        if not name or len(name) < 3:
            continue
        items.append(
            {
                "number": str(idx),
                "name": name[:1200],
                "price": money_cells[0].strip() if money_cells else "",
                "quantity": quantity,
                "delivery_date": delivery_date,
                "row_text": segment[:1200],
            }
        )
    seen_numbers = {str(item.get("number") or "") for item in items}
    for idx in range(1, count + 1):
        if str(idx) in seen_numbers:
            continue
        items.append(
            {
                "number": str(idx),
                "name": f"Позиция {idx} из перечня товаров",
                "price": "",
                "quantity": "",
                "delivery_date": "",
                "row_text": "",
            }
        )
    items.sort(key=lambda item: _safe_int(item.get("number"), default=9999))
    return items


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
    fallback_items = _lot_items_from_product_text(product_rows_info)
    if len(fallback_items) > len(items):
        return fallback_items
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
        lot_parsed["delivery_terms"] = _format_proc_datetime(item.get("delivery_date"))
    lot_parsed["lot_count"] = total_count
    if _is_empty_analysis_value(lot_parsed.get("partial_supply_allowed")):
        lot_parsed["partial_supply_allowed"] = "Нет, единый лот" if total_count != "1" else "Нет, одна товарная позиция/единый лот"
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


def _lot_row_specs(
    *,
    registry: str,
    detail_url: str,
    doc_primary: str,
    parsed: dict[str, str] | None,
    err_msg: str | None,
    product_rows_info: Any,
) -> list[dict[str, Any]]:
    items = _lot_items_from_product_rows(product_rows_info)
    if not items:
        row = build_result_row(registry, detail_url, doc_primary, parsed, err_msg)
        lot_name = str((parsed or {}).get("procurement_subject") or (parsed or {}).get("tender_title") or "").strip()
        return [{"registry": registry, "row": row, "item": None, "lot_name": lot_name}]

    out: list[dict[str, Any]] = []
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
        out.append(
            {
                "registry": row_registry,
                "row": build_result_row(row_registry, detail_url, doc_primary, lot_parsed, err_msg),
                "item": item,
                "lot_name": str(item.get("name") or lot_parsed.get("procurement_subject") or "").strip(),
            }
        )
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

    card_retender_date = _extract_retender_date_from_card(page_text)
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
    if card_retender_date:
        out["retender_date"] = card_retender_date
    elif not has_retrade_date and not proc.get("peretorg_possible") and not lot0.get("is_peretorg"):
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
        out[key] = _format_bullet_list_value(_clean_analysis_value(value))
    for key in ("application_deadline", "retender_date", "results_date", "delivery_terms"):
        if out.get(key):
            out[key] = _format_russian_date_text(out[key])

    return out


def _extract_technical_table_via_lm(
    *,
    registry: str,
    detail_url: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any = None,
    lot_name: str = "",
    lot_item: Any = None,
    lm_base_url: str,
    lm_model: str,
) -> tuple[dict[str, str], str]:
    product_info_text = ""
    if isinstance(product_rows_info, dict) and product_rows_info:
        product_info_text = json.dumps(product_rows_info, ensure_ascii=False, indent=2)[:16000]
    lot_item_text = ""
    if isinstance(lot_item, dict) and lot_item:
        lot_item_text = json.dumps(lot_item, ensure_ascii=False, indent=2)[:8000]
    prompt = build_technical_user_prompt(
        registry=registry,
        detail_url=detail_url,
        page_text=_trim_for_llm(page_text, 80_000),
        documents_text=_trim_for_llm(documents_text, 120_000),
        product_rows_info_text=product_info_text,
        lot_name=lot_name,
        lot_item_text=lot_item_text,
    )
    try:
        raw = call_lm_studio_chat(
            lm_base_url,
            lm_model,
            build_technical_system_prompt(),
            prompt,
            timeout_sec=900,
            max_tokens=4096,
        )
    except Exception as exc:
        raw = f"[ошибка извлечения технических параметров] {exc}"
        parsed = {}
        parsed = _apply_technical_defaults(
            parsed,
            lot_name=lot_name,
            page_text="\n".join((page_text, product_info_text, lot_item_text)),
            documents_text=documents_text,
        )
        return parsed, raw
    parsed = parse_technical_table_json(raw)
    parsed = _apply_technical_defaults(
        parsed,
        lot_name=lot_name,
        page_text="\n".join((page_text, product_info_text, lot_item_text)),
        documents_text=documents_text,
    )
    return parsed, raw


def _looks_generic_equipment_name(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    if _is_empty_analysis_value(text):
        return True
    return any(
        marker in text
        for marker in (
            "оборудование для выполнения",
            "текущий ремонт оборудования",
            "без указания инвентарного",
            "предмет закупки",
            "работы по ремонту",
        )
    )


def _extract_repair_equipment_names(text: str) -> str:
    src = re.sub(r"\s+", " ", str(text or " "))
    variants: list[str] = []

    title_match = re.search(
        r"ремонт\s+(.{10,220}?)(?:\s+для\s+нужд|\s+в\s+20\d{2}\s+году|\s*\(|$)",
        src,
        re.I,
    )
    if title_match:
        candidate = title_match.group(1).strip(" .,:;")
        if 5 <= len(candidate) <= 220:
            variants.append(candidate)

    if re.search(r"рентгеновск\w+\s+аппарат", src, re.I):
        variants.append("рентгеновские аппараты")
    if re.search(r"ультразвуков\w+\s+дефектоскоп", src, re.I):
        variants.append("ультразвуковые дефектоскопы")

    series: list[str] = []
    for pattern in (
        r"рентгеновских\s+аппаратов\s+серии\s+([A-ZА-ЯЁ0-9\- ]{2,30})",
        r"ультразвуковых\s+дефектоскопов\s+([A-ZА-ЯЁ0-9\- ]{2,30})",
    ):
        for match in re.finditer(pattern, src, re.I):
            item = re.sub(r"\s+", " ", match.group(0)).strip(" .,:;")
            if item and item not in series:
                series.append(item)
    if series:
        variants.append("; ".join(series[:6]))

    out: list[str] = []
    for variant in variants:
        value = re.sub(r"\s+", " ", variant).strip(" .,:;")
        if value and value.casefold() not in {x.casefold() for x in out}:
            out.append(value)
    return "; ".join(out[:4])


_TECHNICAL_NUMERIC_FIELDS = {
    "nominal_diameter_or_pipeline_diameter",
    "accuracy_class_or_flow_error",
    "flow_rate_or_range",
    "working_medium_pressure",
    "working_medium_temperature",
    "working_medium_density",
    "working_medium_viscosity",
    "ambient_air_temperature",
}


def _looks_like_year_or_date_technical_value(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.search(r"\b\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}\b", text):
        return True
    if re.search(r"\b(?:19|20)\d{2}[./]\d{1,2}[./]\d{1,2}\b", text):
        return True
    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})(?!\d)", text):
        start_year = int(match.group(1))
        end_year = int(match.group(2))
        if 1900 <= start_year <= 2100 and 1900 <= end_year <= 2100:
            return True
    return False


def _sanitize_technical_value(key: str, value: Any) -> str:
    text = _clean_analysis_value(value)
    if not text:
        return ""
    if key in _TECHNICAL_NUMERIC_FIELDS and _looks_like_year_or_date_technical_value(text):
        return ""
    if key == "flow_rate_or_range":
        # Расход должен быть техническим диапазоном, а не диапазоном годов/дат.
        numbers = re.findall(r"-?\d+(?:[,.]\d+)?", text)
        if len(numbers) >= 2:
            try:
                first = float(numbers[0].replace(",", "."))
                second = float(numbers[1].replace(",", "."))
            except ValueError:
                first = second = 0.0
            if 1900 <= first <= 2100 and 1900 <= second <= 2100:
                return ""
    return text


def _apply_flowmeter_questionnaire_fallback(
    parsed: dict[str, str],
    *,
    lot_name: str,
    documents_text: str,
) -> dict[str, str]:
    out = dict(parsed or {})
    combined = f"{lot_name}\n{documents_text}"
    folded = combined.casefold()
    if "расходомер" not in folded or "опросн" not in folded:
        return out

    compact = re.sub(r"[ \t\r\f\v]+", " ", combined)

    def empty(key: str) -> bool:
        return _is_empty_analysis_value(out.get(key))

    def set_if_empty(key: str, value: str) -> None:
        if value and empty(key):
            out[key] = value

    set_if_empty("equipment_type_name", "Расходомер-счетчик ультразвуковой")
    set_if_empty("measurement_method", "ультразвуковой")
    if "газ" in folded or "газопровод" in folded:
        set_if_empty("measured_medium_name", "природный газ")
        set_if_empty("measured_medium_type", "газ")

    flow_candidates: list[tuple[float, str, str]] = []
    for match in re.finditer(r"(?<![\d.,])(\d{2,7})\s*[-–—\n]\s*(\d{3,8})(?![\d.,])", compact):
        before = compact[max(0, match.start() - 180) : match.start()].casefold()
        after = compact[match.end() : match.end() + 80].casefold()
        context = f"{before} {after}"
        if "гост" in before:
            continue
        low_num = float(match.group(1).replace(",", "."))
        high_num = float(match.group(2).replace(",", "."))
        if 1900 <= low_num <= 2100 and 1900 <= high_num <= 2100:
            continue
        if not any(marker in context for marker in ("расход", "м³/ч", "м3/ч", "нм³/ч", "нм3/ч", "ст.м", "qmin", "qmax", "qном")):
            continue
        if high_num >= 1000 and high_num > low_num:
            flow_candidates.append((high_num, match.group(1), match.group(2)))
    if flow_candidates:
        _high_num, flow_min, flow_max = max(flow_candidates, key=lambda item: item[0])
        set_if_empty("flow_rate_or_range", f"{flow_min}-{flow_max} м³/ч")

    pressure_match = re.search(r"(?<![\d.,])(\d+,\d+)\s*[-–—\n]\s*(\d+,\d+)(?![\d.,])", compact)
    if pressure_match:
        low = pressure_match.group(1)
        high = pressure_match.group(2)
        set_if_empty("working_medium_pressure", f"{low}-{high} МПа")

    temp_match = re.search(r"(?<!\d)(-\d{1,3})\s*[-–—\n]\s*(\d{1,3})(?!\d)", compact)
    if temp_match:
        set_if_empty("working_medium_temperature", f"{temp_match.group(1)}...{temp_match.group(2)} °C")

    ambient_src = compact[temp_match.end() :] if temp_match else compact
    ambient_match = re.search(r"(?<!\d)(-\d{2,3})\s*[-–—\n]\s*(\d{2,3})(?!\d)", ambient_src)
    if ambient_match:
        set_if_empty("ambient_air_temperature", f"{ambient_match.group(1)}...{ambient_match.group(2)} °C")

    diameter_match = re.search(r"\bD\s*[nN]\s*(\d{2,4})\b|\bДу\s*(\d{2,4})\b", combined, re.I)
    if diameter_match:
        diameter = diameter_match.group(1) or diameter_match.group(2)
        set_if_empty("nominal_diameter_or_pipeline_diameter", f"DN {diameter}")

    density_candidates: list[tuple[float, str]] = []
    for match in re.finditer(r"(?<![\d.,])(0[,.]\d{3,5})(?![\d.,])", compact):
        value = float(match.group(1).replace(",", "."))
        if 0.5 <= value <= 1.2:
            density_candidates.append((value, match.group(1)))
    if density_candidates:
        _density_value, density_text = density_candidates[0]
        set_if_empty("working_medium_density", density_text.replace(".", ","))

    if re.search(r"\bPN\s*100\b|ГОСТ\s*33259", combined, re.I):
        set_if_empty("process_connection_method", "фланцевое присоединение PN100")

    if re.search(r"RS[-\s]?485|Modbus", combined, re.I):
        set_if_empty("additional_equipment", "интерфейс RS-485 Modbus RTU")

    accuracy_match = re.search(r"(?<!\d)(1[,.]5)\s*(?:%|$)", compact)
    if accuracy_match:
        set_if_empty("accuracy_class_or_flow_error", f"±{accuracy_match.group(1).replace('.', ',')} %")

    return out


def _apply_technical_defaults(
    parsed: dict[str, str],
    *,
    lot_name: str,
    page_text: str,
    documents_text: str,
) -> dict[str, str]:
    out = dict(parsed or {})
    out = _apply_flowmeter_questionnaire_fallback(out, lot_name=lot_name, documents_text=documents_text)
    combined = "\n".join((str(lot_name or ""), str(page_text or "")[:30_000], str(documents_text or "")[:80_000]))
    equipment_name = _extract_repair_equipment_names(combined)
    if equipment_name and _looks_generic_equipment_name(out.get("equipment_type_name")):
        out["equipment_type_name"] = equipment_name

    equipment_folded = str(out.get("equipment_type_name") or equipment_name).casefold()
    if _is_empty_analysis_value(out.get("measurement_method")):
        methods: list[str] = []
        if "рентген" in equipment_folded:
            methods.append("рентгенографический контроль")
        if "ультразвуков" in equipment_folded or "дефектоскоп" in equipment_folded:
            methods.append("ультразвуковой контроль")
        if methods:
            out["measurement_method"] = "; ".join(methods)

    for key, value in list(out.items()):
        if isinstance(value, list):
            value = "; ".join(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, str) and re.fullmatch(r"\s*\[[\s\S]*\]\s*", value):
            list_items = re.findall(r"""['"]([^'"]{2,300})['"]""", value)
            if list_items:
                value = "; ".join(item.strip() for item in list_items if item.strip())
        if key == "equipment_type_name" and _looks_generic_equipment_name(value):
            out[key] = ""
        else:
            out[key] = _sanitize_technical_value(key, value)
    return out


_EQUIPMENT_QUERY_KEYS = {
    "medium",
    "flowMin",
    "flowMax",
    "flow_unit",
    "pressureMin",
    "pressureMax",
    "pressure_unit",
    "tempMediumMin",
    "tempMediumMax",
    "tempAmbientMin",
    "tempAmbientMax",
    "temperature_unit",
    "accuracy",
    "diameter",
    "densityMin",
    "densityMax",
    "allowedEquipmentIds",
    "application",
    "gas_type",
}


def _json_object_from_text(raw: str) -> dict[str, Any]:
    obj = _first_json_object(raw)
    return obj if isinstance(obj, dict) else {}


def _number_or_none(value: Any) -> str:
    text = str(value or "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def _normalize_query_number(value: Any) -> str:
    text = str(value or "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return ""
    number = match.group(0)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return number


def _range_numbers(value: Any) -> tuple[str, str]:
    text = str(value or "").replace(",", ".")
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    if len(numbers) == 1:
        return numbers[0], numbers[0]
    return "", ""


def _fallback_equipment_query_from_technical(technical: dict[str, str]) -> dict[str, str]:
    medium_text = " ".join(
        str(technical.get(key) or "")
        for key in ("measured_medium_type", "measured_medium_name")
    ).casefold()
    equipment_text = str(technical.get("equipment_type_name") or "").casefold()
    if any(marker in equipment_text for marker in ("денситометр", "плотномер")):
        medium = "densitometer"
    elif any(marker in medium_text for marker in ("жидк", "вода", "нефт", "масл")):
        medium = "liquid"
    else:
        medium = "gas"
    query: dict[str, str] = {
        "medium": medium,
        "flow_unit": "м³/ч",
        "pressure_unit": "МПа",
        "temperature_unit": "°C",
        "application": "industrial",
    }
    if medium == "gas":
        query["gas_type"] = "natural" if "природ" in medium_text else "technological"

    flow_min, flow_max = _range_numbers(technical.get("flow_rate_or_range"))
    if flow_min:
        query["flowMin"] = flow_min
    if flow_max:
        query["flowMax"] = flow_max

    pressure_min, pressure_max = _range_numbers(technical.get("working_medium_pressure"))
    if pressure_min:
        query["pressureMin"] = pressure_min
    if pressure_max:
        query["pressureMax"] = pressure_max

    temp_min, temp_max = _range_numbers(technical.get("working_medium_temperature"))
    if temp_min:
        query["tempMediumMin"] = temp_min
    if temp_max:
        query["tempMediumMax"] = temp_max

    ambient_min, ambient_max = _range_numbers(technical.get("ambient_air_temperature"))
    if ambient_min:
        query["tempAmbientMin"] = ambient_min
    if ambient_max:
        query["tempAmbientMax"] = ambient_max

    accuracy = _number_or_none(technical.get("accuracy_class_or_flow_error"))
    if accuracy:
        query["accuracy"] = accuracy

    diameter = _number_or_none(technical.get("nominal_diameter_or_pipeline_diameter"))
    if diameter:
        query["diameter"] = diameter

    density_min, density_max = _range_numbers(technical.get("working_medium_density"))
    if density_min:
        query["densityMin"] = density_min
    if density_max:
        query["densityMax"] = density_max
    return query


def _is_yearish_number(value: Any) -> bool:
    try:
        number = float(str(value or "").replace(",", "."))
    except ValueError:
        return False
    return number.is_integer() and 1900 <= number <= 2100


def _sanitize_equipment_query(query: dict[str, str]) -> dict[str, str]:
    out = dict(query or {})
    numeric_keys = {
        "flowMin",
        "flowMax",
        "pressureMin",
        "pressureMax",
        "tempMediumMin",
        "tempMediumMax",
        "tempAmbientMin",
        "tempAmbientMax",
        "accuracy",
        "diameter",
        "densityMin",
        "densityMax",
    }
    for key in numeric_keys:
        if key in out:
            normalized = _normalize_query_number(out.get(key))
            if normalized:
                out[key] = normalized
            else:
                out.pop(key, None)
    paired_keys = (
        ("flowMin", "flowMax"),
        ("pressureMin", "pressureMax"),
        ("tempMediumMin", "tempMediumMax"),
        ("tempAmbientMin", "tempAmbientMax"),
        ("densityMin", "densityMax"),
    )
    for low_key, high_key in paired_keys:
        low = out.get(low_key)
        high = out.get(high_key)
        if _is_yearish_number(low) and _is_yearish_number(high):
            out.pop(low_key, None)
            out.pop(high_key, None)
    for single_key in ("accuracy", "diameter"):
        if _is_yearish_number(out.get(single_key)):
            out.pop(single_key, None)
    return out


def _equipment_api_url(endpoint: str, params: dict[str, str]) -> str:
    base = EQUIPMENT_API_BASE_URL.rstrip("/")
    endpoint = "/" + endpoint.lstrip("/")
    if not base.endswith("/api"):
        endpoint = "/api" + endpoint
    return base + endpoint + "?" + urlencode(params, doseq=False)


def _equipment_selection_rag_context(
    *,
    lot_name: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any,
    lot_item: Any,
) -> str:
    needles = (
        "расход",
        "диапазон расход",
        "давлен",
        "температур",
        "окружающ",
        "точност",
        "погрешност",
        "диаметр",
        "dn",
        "ду",
        "плотност",
        "вязкост",
        "опросн",
        "узел измерения",
        "измеряем",
        "среда",
    )
    lot_terms = [
        term.casefold()
        for term in re.findall(r"[A-Za-zА-Яа-яЁё0-9\-]{4,}", str(lot_name or ""))
        if len(term) >= 4
    ][:12]

    candidates: list[tuple[int, str, str]] = []

    def add_candidate(label: str, text: Any, base_score: int = 0) -> None:
        raw = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(raw) < 20:
            return
        label_folded = str(label or "").casefold()
        folded = f"{label_folded} {raw.casefold()}"
        score = base_score
        score += sum(3 for needle in needles if needle in folded)
        score += sum(2 for term in lot_terms if term in folded)
        if any(marker in folded for marker in ("опросн", " ол", "_ол", ".ол", "ол7", "ол1")):
            score += 18
        if any(marker in folded for marker in ("тз", "техническ", "техзадан", "таблица а")):
            score += 14
        if any(marker in label_folded for marker in (".pdf", ".xlsx", ".xls", "тз", "таблица", "опрос", " ол")):
            score += 10
        if any(marker in folded for marker in ("банковск", "обс", "электронн", "гарант", "персональн")):
            score -= 8
        if score <= 0:
            return
        focused = raw
        if len(raw) > 6000:
            folded_raw = raw.casefold()
            anchors = list(needles) + lot_terms + [
                "опросный лист",
                "техническое задание",
                "таблица а",
                "ду ",
                "dn",
                "мпа",
                "м³/ч",
                "м3/ч",
            ]
            positions = [
                pos
                for anchor in anchors
                if anchor and (pos := folded_raw.find(anchor.casefold())) >= 0
            ]
            if positions:
                snippets: list[str] = []
                for pos in sorted(set(positions))[:6]:
                    start = max(0, pos - 900)
                    end = min(len(raw), pos + 2200)
                    snippets.append(raw[start:end])
                focused = "\n...\n".join(snippets)
            else:
                focused = raw[:6000]
        candidates.append((score, label, focused[:9000]))

    add_candidate("Строка позиции", json.dumps(lot_item, ensure_ascii=False, indent=2) if isinstance(lot_item, dict) else "", 8)
    add_candidate("Перечень товаров", json.dumps(product_rows_info, ensure_ascii=False, indent=2) if isinstance(product_rows_info, dict) else "", 6)
    add_candidate("Карточка ЭТП", page_text[:40_000], 1)

    sections = _document_sections_from_text(documents_text)
    if sections:
        for section in sections:
            add_candidate(str(section.get("file_name") or "Документ"), section.get("text") or "", 0)
    else:
        chunks = re.split(r"\n{2,}", documents_text or "")
        for idx, chunk in enumerate(chunks[:250], start=1):
            add_candidate(f"Документ, фрагмент {idx}", chunk, 0)

    candidates.sort(key=lambda item: item[0], reverse=True)
    seen: set[str] = set()
    parts: list[str] = []
    for _score, label, text in candidates:
        signature = text[:300].casefold()
        if signature in seen:
            continue
        seen.add(signature)
        parts.append(f"--- {label} ---\n{text}")
        if sum(len(part) for part in parts) > 70_000 or len(parts) >= 14:
            break
    return "\n\n".join(parts)


def _equipment_query_via_lm(
    *,
    registry: str,
    lot_name: str,
    technical: dict[str, str],
    rag_context: str,
    lm_base_url: str,
    lm_model: str,
) -> tuple[dict[str, str], str]:
    prompt = (
        "На основе технической таблицы закупки и RAG-фрагментов документов подготовь query-параметры "
        "для мастера подбора расходомеров/денситометров. Ответь строго JSON-объектом "
        "без markdown. Не придумывай значения: если параметра нет в таблице, верни null.\n\n"
        "Разрешённые ключи JSON:\n"
        "medium, flowMin, flowMax, flow_unit, pressureMin, pressureMax, pressure_unit, "
        "tempMediumMin, tempMediumMax, tempAmbientMin, tempAmbientMax, temperature_unit, "
        "accuracy, diameter, densityMin, densityMax, allowedEquipmentIds, application, gas_type.\n\n"
        "Правила:\n"
        "- medium: gas, liquid или densitometer. Для плотномеров/денситометров medium=densitometer.\n"
        "- Для gas/liquid будет вызван /api/search-equipment, для densitometer — /api/search-densitometer.\n"
        "- pressureMin/pressureMax передавай числом; если в документах давление в бар, "
        "можно передать pressure_unit=бар, иначе pressure_unit=МПа.\n"
        "- температуры передавай в °C; temperature_unit=°C.\n"
        "- diameter всегда в мм, без DN/Ду.\n"
        "- flow_unit: для газа м³/ч или ст.м³/ч; для жидкости м³/ч или кг/ч. Если неясно, верни м³/ч.\n"
        "- Для liquid при массовом расходе кг/ч обязательно ищи densityMin/densityMax.\n"
        "- Для densitometer расход не нужен; важны densityMin/densityMax, pressure, temperature, accuracy, diameter.\n"
        "- application обычно industrial.\n"
        "- gas_type для природного газа natural, для технологического technological.\n\n"
        f"Реестровый номер: {registry}\n"
        f"Позиция: {lot_name or '[не указана]'}\n"
        "Техническая таблица JSON:\n"
        f"{json.dumps(technical, ensure_ascii=False, indent=2)}\n\n"
        "RAG-ФРАГМЕНТЫ ДЛЯ ПОДБОРА ПРИБОРА:\n"
        "-----\n"
        f"{rag_context or '[релевантные фрагменты не найдены]'}\n"
        "-----"
    )
    raw = call_lm_studio_chat(
        lm_base_url,
        lm_model,
        "Ты инженер по подбору расходомеров. Возвращай только JSON.",
        prompt,
        timeout_sec=180,
        max_tokens=1200,
    )
    obj = _json_object_from_text(raw)
    query: dict[str, str] = {}
    for key, value in obj.items():
        if key not in _EQUIPMENT_QUERY_KEYS or value is None:
            continue
        text = str(value).strip()
        if text and not _is_empty_analysis_value(text):
            query[key] = text
    return query, raw


def _technical_documents_context(
    *,
    lot_name: str,
    page_text: str,
    documents_text: str,
    product_rows_info: Any,
    lot_item: Any,
) -> str:
    context = _equipment_selection_rag_context(
        lot_name=lot_name,
        page_text=page_text,
        documents_text=documents_text,
        product_rows_info=product_rows_info,
        lot_item=lot_item,
    )
    if not context:
        return documents_text
    return (
        "ПРИОРИТЕТНЫЕ ТЕХНИЧЕСКИЕ ФРАГМЕНТЫ ДЛЯ ИЗВЛЕЧЕНИЯ ПАРАМЕТРОВ:\n"
        f"{context}\n\n"
        "ОСТАЛЬНОЙ ТЕКСТ ДОКУМЕНТОВ:\n"
        f"{_trim_for_llm(documents_text, 30_000)}"
    )


def _request_equipment_selection(query: dict[str, str]) -> dict[str, Any]:
    query = _sanitize_equipment_query(query)
    params = {key: value for key, value in query.items() if str(value or "").strip()}
    medium = str(params.get("medium") or "gas").strip().casefold()
    selection_keys = {
        "flowMin",
        "flowMax",
        "pressureMin",
        "pressureMax",
        "tempMediumMin",
        "tempMediumMax",
        "tempAmbientMin",
        "tempAmbientMax",
        "accuracy",
        "diameter",
        "densityMin",
        "densityMax",
        "allowedEquipmentIds",
    }
    if not params or not any(key in params for key in selection_keys):
        return {
            "status": "not_selected",
            "message": "Прибор не подобран: недостаточно технических параметров для запроса.",
            "query": params,
            "equipment": [],
        }
    if medium == "densitometer":
        endpoint = "/search-densitometer"
        params.pop("flowMin", None)
        params.pop("flowMax", None)
        params.pop("flow_unit", None)
        params.pop("application", None)
        params.pop("gas_type", None)
        params.pop("medium", None)
    else:
        endpoint = "/search-equipment"
        if medium not in {"gas", "liquid"}:
            params["medium"] = "gas"
        if params.get("medium") == "gas":
            params.pop("densityMin", None)
            params.pop("densityMax", None)
        if params.get("medium") == "liquid":
            params.pop("gas_type", None)
    url = _equipment_api_url(endpoint, params)
    payload: Any = None
    last_exc: BaseException | None = None
    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=40) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            last_exc = None
            break
        except HTTPError as exc:
            last_exc = exc
            if 400 <= getattr(exc, "code", 500) < 500:
                break
            if attempt < attempts:
                time.sleep(min(2 * attempt, 6))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(min(2 * attempt, 6))
    if last_exc is not None:
        return {
            "status": "error",
            "message": f"Прибор не подобран: ошибка запроса к мастеру подбора ({last_exc}).",
            "query": params,
            "url": url,
            "equipment": [],
        }

    equipment = payload.get("equipment") if isinstance(payload, dict) else None
    if not isinstance(equipment, list) or not equipment:
        return {
            "status": "not_found",
            "message": "Прибор не подобран: мастер подбора не вернул подходящих моделей.",
            "query": params,
            "url": url,
            "raw": payload,
            "equipment": [],
        }
    return {
        "status": "success",
        "message": f"Подобрано приборов: {len(equipment)}.",
        "query": params,
        "url": url,
        "raw": payload,
        "equipment": equipment,
    }


def _select_equipment_for_technical(
    *,
    registry: str,
    lot_name: str,
    technical: dict[str, str],
    page_text: str,
    documents_text: str,
    product_rows_info: Any,
    lot_item: Any,
    lm_base_url: str,
    lm_model: str,
) -> dict[str, Any]:
    lm_raw = ""
    rag_context = _equipment_selection_rag_context(
        lot_name=lot_name,
        page_text=page_text,
        documents_text=documents_text,
        product_rows_info=product_rows_info,
        lot_item=lot_item,
    )
    try:
        query, lm_raw = _equipment_query_via_lm(
            registry=registry,
            lot_name=lot_name,
            technical=technical,
            rag_context=rag_context,
            lm_base_url=lm_base_url,
            lm_model=lm_model,
        )
    except Exception as exc:
        query = _fallback_equipment_query_from_technical(technical)
        lm_raw = f"[ошибка LM-подготовки параметров подбора] {exc}"
    fallback_query = _fallback_equipment_query_from_technical(technical)
    for key, value in fallback_query.items():
        query.setdefault(key, value)
    query = _sanitize_equipment_query(query)
    selection = _request_equipment_selection(query)
    selection["lm_raw"] = lm_raw
    selection["rag_context_used"] = bool(rag_context.strip())
    return selection


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

    system_prompt = (
        "Ты аналитик закупок секции Газпром. Твоя задача — определить делимость заявки "
        "и количество лотов/товарных позиций. Не считай регулярными выражениями, а проанализируй "
        "смысл карточки извещения и документов. Ответь только JSON-объектом без markdown."
    )
    user_prompt = (
        f"Реестровый номер: {registry}\n"
        f"URL карточки: {detail_url}\n\n"
        "Определи:\n"
        "1. lot_count — количество товарных позиций/строк в перечне товаров. "
        "Если на странице есть отдельный список лотов, можно указать количество лотов, но не смешивай это с делимостью.\n"
        "2. partial_supply_allowed — делимая заявка/лот или нет.\n\n"
        "Правила:\n"
        "- Главный источник — полный текст страницы карточки/извещения ниже. Его нужно анализировать всегда.\n"
        "- Первый этап приложения уже попытался прочитать DOM-блок div.x-fieldset-bwrap с перечнем товаров. "
        "Если ниже указано, что найден 1 товар/строка, не меняй lot_count без явного основания, "
        "но обязательно проверь документы на формулировки о делимости заявки.\n"
        "- Сначала ищи список лотов и строку вроде «Позиций всего: N»/«Список лотов».\n"
        "- Если слово «лот» отсутствует, смотри перечень товаров: если самостоятельных товаров больше одного, "
        "укажи количество товаров как количество товарных позиций.\n"
        "- ВАЖНО: несколько товаров в перечне НЕ означают делимый лот. "
        "Неделимый лот может содержать 2 и более товара, которые должен поставить один поставщик.\n"
        "- partial_supply_allowed определяй только по прямым формулировкам: "
        "«лот делимый», «лот является неделимым», «поставка части допускается/не допускается», "
        "«заявка является делимой» и похожим условиям.\n"
        "- Если lot_count больше 1, но в тексте указано «лот является неделимым», "
        "верни lot_count как количество товаров и partial_supply_allowed = «Нет, лот неделимый».\n"
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
    if parsed_product_count > 0:
        lot_count = str(parsed_product_count)
    elif parsed_product_count == 1 and _is_empty_analysis_value(lot_count):
        lot_count = "1"
    combined = f"{page_text}\n{documents_text}".casefold()
    if re.search(r"лот\s+(?:является\s+)?неделим|неделим(?:ый|ая|ое)", combined, re.I):
        partial = "Нет, лот неделимый"
    elif _is_empty_analysis_value(partial):
        if re.search(r"лот\s+(?:является\s+)?делим|заявка\s+(?:является\s+)?делим", combined, re.I):
            partial = "Да, лот делимый"
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
        is_trading_portal = str(getattr(client, "platform_key", "")) == "gpb_trading_portal"
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
            elif is_trading_portal:
                # У Торгового портала свой набор текстовых статусов ЦЗ
                # («Черновик», «Уторговывание», «Исполнено» и т.д.). Не
                # перекодируем их через справочник Procedure.list секции Газпром.
                probe_filters = client_filters
            else:
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
                        if is_trading_portal:
                            login_hint = (
                                "В Chrome откройте Торговый портал, выполните вход до конца, "
                                "дождитесь загрузки списка ЦЗ и снова нажмите «Поиск»."
                            )
                        elif "etp.gpb.ru" in host:
                            login_hint = (
                                "В Chrome откройте секцию Бизнес.223, выполните вход до конца, "
                                "дождитесь загрузки списка процедур и снова нажмите «Поиск»."
                            )
                        else:
                            login_hint = (
                                "В Chrome: «Войти» → «ЕСИА + ЭП» → пройдите до конца, "
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
                    if "roseltorg" in host:
                        login_hint = (
                            "В Chrome откройте Росэлторг, выполните вход через ЭЦП до конца, "
                            "затем снова нажмите «Поиск»."
                        )
                    elif is_trading_portal:
                        login_hint = (
                            "В Chrome откройте Торговый портал, выполните вход до конца, "
                            "дождитесь загрузки списка ЦЗ и снова нажмите «Поиск»."
                        )
                    elif "etp.gpb.ru" in host:
                        login_hint = (
                            "В Chrome откройте секцию Бизнес.223, выполните вход до конца, "
                            "дождитесь загрузки списка процедур и снова нажмите «Поиск»."
                        )
                    else:
                        login_hint = (
                            "В Chrome: «Войти» → «ЕСИА + ЭП» → пройдите до конца, "
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
        sink["equipment_selection_by_registry"] = {}
        sink["sources_by_registry"] = {}
        sink["technical_sources_by_registry"] = {}

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
            product_rows_info = _best_product_rows_info(snap.get("product_rows_info") or {}, page_text)
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
            sources_by_field: dict[str, list[dict[str, Any]]] = {}

            if rag_available:
                try:
                    w.progress.emit(f"RAG: индексация и извлечение полей для {registry}…")
                    ingest_notes: list[str] = []
                    debug_dir = ANALYSIS_DIR / "rag_debug" / _safe_folder_name(registry)
                    parsed, raw_llm, rag_sources = run_rag_table_analysis(
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
                    sources_by_field = {
                        field: [_source_to_dict(source) for source in sources]
                        for field, sources in (rag_sources or {}).items()
                    }
                    for field_sources in sources_by_field.values():
                        for source in field_sources:
                            if source.get("source_type") == "card" and not source.get("url"):
                                source["url"] = detail_url
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
            if parsed:
                if parsed.get("customer_name"):
                    _add_field_source(
                        sources_by_field,
                        "customer_name",
                        _card_source("Наименование заказчика/организатора", detail_url, parsed.get("customer_name", "")),
                    )
                if parsed.get("tender_title"):
                    _add_field_source(
                        sources_by_field,
                        "tender_title",
                        _card_source("Наименование закупки", detail_url, parsed.get("tender_title", "")),
                    )
                if parsed.get("application_deadline"):
                    _add_field_source(
                        sources_by_field,
                        "application_deadline",
                        _card_source("Дата окончания подачи заявок", detail_url, parsed.get("application_deadline", "")),
                    )
                if parsed.get("retender_date"):
                    _add_field_source(
                        sources_by_field,
                        "retender_date",
                        _card_source(
                            "Дата и время окончания срока подачи новых коммерческих предложений",
                            detail_url,
                            parsed.get("retender_date", ""),
                        ),
                    )
                if parsed.get("results_date"):
                    _add_field_source(
                        sources_by_field,
                        "results_date",
                        _card_source("Дата подведения итогов", detail_url, parsed.get("results_date", "")),
                    )
                if parsed.get("starting_price"):
                    _add_field_source(
                        sources_by_field,
                        "starting_price",
                        _card_source("Начальная максимальная цена / Перечень товаров", detail_url, parsed.get("starting_price", "")),
                    )
                if parsed.get("application_fee"):
                    _add_field_source(
                        sources_by_field,
                        "application_fee",
                        _card_source("Взимание оператором платы", detail_url, parsed.get("application_fee", "")),
                    )
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
                _add_field_source(
                    sources_by_field,
                    "lot_count",
                    _card_source("Количество лотов / Перечень товаров", detail_url, lot_raw),
                )
                _add_field_source(
                    sources_by_field,
                    "partial_supply_allowed",
                    _card_source("Делимость лота / Перечень товаров", detail_url, lot_raw),
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
            product_items = _lot_items_from_product_rows(product_rows_info)
            if product_items:
                parsed = dict(parsed or {})
                parsed["lot_count"] = str(len(product_items))
                product_source = _card_source(
                    "Перечень товаров",
                    detail_url,
                    json.dumps(product_rows_info, ensure_ascii=False, indent=2)[:8000],
                )
                for field_key in ("procurement_subject", "starting_price", "delivery_terms", "lot_count", "partial_supply_allowed"):
                    _add_field_source(sources_by_field, field_key, product_source)
            sink["sources_by_registry"][registry] = sources_by_field
            lot_specs = _lot_row_specs(
                registry=registry,
                detail_url=detail_url,
                doc_primary=doc_primary,
                parsed=parsed,
                err_msg=err_msg,
                product_rows_info=product_rows_info,
            )
            for lot_spec in lot_specs:
                row_registry = str(lot_spec.get("registry") or registry)
                lot_name = str(lot_spec.get("lot_name") or "").strip()
                lot_item = lot_spec.get("item")
                try:
                    w.progress.emit(
                        "LM Studio: извлекаю технические параметры "
                        f"{row_registry}" + (f" ({lot_name[:80]})" if lot_name else "") + "…"
                    )
                    technical, technical_raw = _extract_technical_table_via_lm(
                        registry=row_registry,
                        detail_url=detail_url,
                        page_text=page_text,
                        documents_text=_technical_documents_context(
                            lot_name=lot_name,
                            page_text=page_text,
                            documents_text=documents_text,
                            product_rows_info=product_rows_info,
                            lot_item=lot_item,
                        ),
                        product_rows_info=product_rows_info,
                        lot_name=lot_name,
                        lot_item=lot_item,
                        lm_base_url=lm_base_url,
                        lm_model=lm_model,
                    )
                    sink["technical_by_registry"][row_registry] = technical
                    w.progress.emit(f"Мастер подбора: подбираю наш прибор для {row_registry}…")
                    selection = _select_equipment_for_technical(
                        registry=row_registry,
                        lot_name=lot_name,
                        technical=technical,
                        page_text=page_text,
                        documents_text=documents_text,
                        product_rows_info=product_rows_info,
                        lot_item=lot_item,
                        lm_base_url=lm_base_url,
                        lm_model=lm_model,
                    )
                    sink["equipment_selection_by_registry"][row_registry] = selection
                    tech_sources: dict[str, list[dict[str, Any]]] = {}
                    if product_rows_info:
                        source_text = (
                            json.dumps(lot_item, ensure_ascii=False, indent=2)[:8000]
                            if isinstance(lot_item, dict)
                            else json.dumps(product_rows_info, ensure_ascii=False, indent=2)[:8000]
                        )
                        tech_source = _card_source("Перечень товаров", detail_url, source_text)
                        for key, value in technical.items():
                            if value:
                                _add_field_source(tech_sources, key, tech_source)
                    if documents_text.strip():
                        for key, value in technical.items():
                            if not value:
                                continue
                            doc_source = _best_document_source_for_value(
                                documents_text,
                                value=str(value),
                                lot_name=lot_name,
                            )
                            if doc_source is not None:
                                _add_field_source(tech_sources, key, doc_source, prepend=False)
                    sink["technical_sources_by_registry"][row_registry] = tech_sources
                    previous_raw = str(sink["raw_by_registry"].get(registry) or "")
                    sink["raw_by_registry"][registry] = (
                        previous_raw
                        + ("\n\n" if previous_raw else "")
                        + f"### technical_parameters_{row_registry}\n"
                        + technical_raw
                    )
                except Exception as tech_exc:
                    sink["document_issues"].append(
                        {
                            "severity": "important",
                            "registry": row_registry,
                            "file": "LM Studio",
                            "message": (
                                "Не удалось извлечь вторую таблицу технических параметров: "
                                f"{tech_exc}"
                            ),
                        }
                    )
                rows.append(list(lot_spec.get("row") or []))

        sink["rows"] = rows
        w.session.emit(
            True,
            f"Анализ завершён: {len(rows)} процедур. LM Studio: {lm_base_url}, модель {lm_model}.",
        )

    return _run
