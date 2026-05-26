from __future__ import annotations

from ..lm_table_analysis import (
    ANALYSIS_JSON_KEYS,
    ANALYSIS_TABLE_HEADERS_RU,
    call_lm_studio_chat,
    parse_single_field_json,
    single_field_system_prompt,
)
from .field_specs import FIELD_SEARCH_QUERY_RU
from .schemas import ChunkPayload
from .validate_cross import empty_to_null, reconcile_money_llm_with_chunks


def _field_header_ru(field_key: str) -> str:
    headers_tail = ANALYSIS_TABLE_HEADERS_RU[3:]
    for k, h in zip(ANALYSIS_JSON_KEYS, headers_tail):
        if k == field_key:
            return h
    return field_key


def _format_retrieval_context(hit_chunks: list[ChunkPayload]) -> str:
    blocks: list[str] = []
    for i, ch in enumerate(hit_chunks, start=1):
        loc = ch.file_name
        if ch.page is not None:
            loc += f", стр. {ch.page}"
        sec = f", раздел: {ch.section}" if ch.section else ""
        blocks.append(f"--- Фрагмент {i} ({loc}{sec}) ---\n{ch.text}")
    return "\n\n".join(blocks)


def _field_specific_instruction(field_key: str) -> str:
    if field_key == "partial_supply_allowed":
        return (
            "Дополнительная инструкция: определи делимость лота. Сначала смотри карточку извещения "
            "и список лотов. Затем ищи в документах фразы «заявка является делимой/неделимой», "
            "«частичная поставка», «поставка части оборудования». Если прямой фразы нет, оцени "
            "перечень товаров: несколько самостоятельных товарных позиций означает делимость. "
            "Верни короткий ответ с основанием."
        )
    if field_key == "lot_count":
        return (
            "Дополнительная инструкция: определи количество лотов или товарных позиций. "
            "Сначала используй список лотов из карточки извещения. Если список лотов отсутствует "
            "или слово «лот» не указано, посчитай самостоятельные позиции в перечне товаров, "
            "спецификации или техническом задании. Верни число с кратким основанием, например "
            "«1 (единая позиция)» или «4 (по перечню товаров)». Не придумывай число, если "
            "нет достаточного подтверждения."
        )
    if field_key == "procurement_subject":
        return (
            "Дополнительная инструкция: главным источником считай таблицу «Перечень товаров» "
            "из карточки. Верни конкретное наименование товара/работы, а не родовые слова "
            "«Продукция», «товар», «оборудование»."
        )
    if field_key == "starting_price":
        return (
            "Дополнительная инструкция: верни только конкретную сумму НМЦ/начальной цены из карточки "
            "или таблицы «Перечень товаров». Не возвращай фразы о том, где цена указана."
        )
    if field_key in {"application_deadline", "retender_date", "results_date"}:
        return (
            "Дополнительная инструкция: верни только точную дату и время, если они прямо указаны. "
            "Не возвращай правила вида «в течение 3 дней после протокола» или сообщения об отсутствии даты."
        )
    if field_key == "delivery_terms":
        return (
            "Дополнительная инструкция: верни конкретный срок/дату поставки. Если есть таблица "
            "«Перечень товаров» с колонкой «Ожидаемая дата поставки», используй эту дату. "
            "Не возвращай инструкцию «указывается в формате dd.mm.yyyy»."
        )
    if field_key == "application_fee":
        return (
            "Дополнительная инструкция: верни только конкретную стоимость/сбор за подачу заявки "
            "из карточки или документа. Не возвращай формулы распределения платы между участниками."
        )
    return ""


def extract_fields_via_retrieval(
    store,
    *,
    lm_base_url: str,
    lm_model: str,
    top_k: int,
    timeout_sec: int,
    progress=None,
    stop_flag=None,
) -> tuple[dict[str, str], str]:
    """Для каждого поля — отдельный retrieval и один запрос к LLM."""
    raw_parts: list[str] = []
    out: dict[str, str] = {}

    for field_key in ANALYSIS_JSON_KEYS:
        if stop_flag and stop_flag():
            break
        query = FIELD_SEARCH_QUERY_RU.get(field_key, field_key)
        if progress:
            progress(f"RAG: поле «{_field_header_ru(field_key)}»…")

        hits_idx = store.search(query, top_k=top_k)
        hit_chunks = [store.chunks[i] for i, _ in hits_idx if 0 <= i < len(store.chunks)]
        context = _format_retrieval_context(hit_chunks)

        label = _field_header_ru(field_key)
        user_prompt = (
            "Контекст включает полный текст страницы карточки процедуры секции Газпром и фрагменты документов.\n\n"
            f"Требуемое поле (ключ JSON): {field_key}\n"
            f"Человекочитаемое название: {label}\n\n"
            f"{_field_specific_instruction(field_key)}\n\n"
            "Контекст (фрагменты документов и карточки):\n"
            f"{context}\n"
        )

        try:
            raw = call_lm_studio_chat(
                lm_base_url,
                lm_model,
                single_field_system_prompt(label),
                user_prompt,
                timeout_sec=timeout_sec,
                max_tokens=900,
            )
            raw_parts.append(f"### {field_key}\n{raw}")

            val = parse_single_field_json(raw, field_key)
            val = empty_to_null(val)

            if field_key == "starting_price" and val:
                val = reconcile_money_llm_with_chunks(val, [c.text for c in hit_chunks])

            out[field_key] = "" if val is None else val
        except Exception as exc:
            raw_parts.append(f"### {field_key}\n[ошибка извлечения] {exc}")
            out[field_key] = ""

    combined_raw = "\n\n".join(raw_parts)
    return out, combined_raw
