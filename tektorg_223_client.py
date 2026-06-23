from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from tektorg_inter_rao_client import TektorgInterRaoClient
from tektorg_rosneft_client import _safe_filename, _safe_text


TEKTORG_223_URL = "https://www.tektorg.ru/223-fz/procedures"

TEKTORG_223_STATUS_OPTIONS = [
    ("Архив", "Архив"),
    ("Отменён", "Отменён"),
    ("Приём заявок", "Приём заявок"),
    ("Работа комиссии", "Работа комиссии"),
]

TEKTORG_223_TYPE_OPTIONS = [
    ("Аукцион", "Аукцион"),
    ("Закупка у единственного поставщика", "Закупка у единственного поставщика"),
    ("Запрос котировок", "Запрос котировок"),
    ("Запрос оферт", "Запрос оферт"),
    ("Запрос предложений", "Запрос предложений"),
    ("Запрос предоставления ценовой информации", "Запрос предоставления ценовой информации"),
    ("Запрос цен", "Запрос цен"),
    ("Конкурс", "Конкурс"),
]


class Tektorg223Client(TektorgInterRaoClient):
    """Клиент отдельной секции ТЭК-Торг «223-ФЗ и коммерческие закупки»."""

    platform_key = "tektorg_223"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_223_URL
        self.target_host = "www.tektorg.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return f"{TEKTORG_223_URL}/{proc_id}"

    def _looks_like_card_id_search(self, client_filters: Any = None, query: Optional[str] = None) -> str:
        if client_filters is not None:
            value = _safe_text(getattr(client_filters, "registry_contains", ""))
            if not value:
                value = _safe_text(getattr(client_filters, "quick_search", ""))
        else:
            value = _safe_text(query)
        return value if re.fullmatch(r"\d{6,}", value) else ""

    def _find_card_payload(self, value: Any, proc_id: str) -> dict[str, Any]:
        best: dict[str, Any] = {}
        best_score = 0
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                score = 0
                ids = (
                    item.get("id"),
                    item.get("procedureId"),
                    item.get("procedure_id"),
                    item.get("remoteId"),
                )
                if any(_safe_text(candidate) == proc_id for candidate in ids):
                    score += 4
                if any(key in item for key in ("title", "name", "registryNumber", "statusName", "typeName")):
                    score += 2
                if score > best_score:
                    best = item
                    best_score = score
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return best if best_score >= 4 else {}

    def _normalize_card_row(
        self,
        proc_id: str,
        payload: dict[str, Any],
        heading: str = "",
        text: str = "",
    ) -> dict[str, Any]:
        dates = payload.get("dates") if isinstance(payload.get("dates"), dict) else {}
        registry = _safe_text(
            payload.get("registryNumber")
            or payload.get("registry_number")
            or payload.get("procedureNumber")
            or payload.get("number")
        )
        if not registry:
            match = re.search(r"\b[А-ЯA-Z]{1,4}\d{5,}\b", text)
            registry = match.group(0) if match else proc_id
        title = _safe_text(payload.get("title") or payload.get("name") or heading)
        status = _safe_text(payload.get("statusName") or payload.get("status") or payload.get("status_name"))
        if not status:
            for _, label in TEKTORG_223_STATUS_OPTIONS:
                if label in text:
                    status = label
                    break
        type_name = _safe_text(payload.get("typeName") or payload.get("type") or payload.get("procedureType"))
        url = self._detail_url(proc_id)
        return {
            **payload,
            "id": int(proc_id) if proc_id.isdigit() else proc_id,
            "procedure_id": int(proc_id) if proc_id.isdigit() else proc_id,
            "registry_number": registry,
            "procedure_number": registry,
            "title": title,
            "name": title,
            "status_name": status,
            "status_label": status,
            "step_label": status,
            "type_name": type_name,
            "procedure_type_name": type_name,
            "trend_pur_name": type_name,
            "organizer": payload.get("organizerName") or payload.get("organizer"),
            "organizer_name": payload.get("organizerName") or payload.get("organizer"),
            "date_published": dates.get("datePublished") or payload.get("datePublished"),
            "date_end_registration": dates.get("dateEndRegistration") or payload.get("dateEndRegistration"),
            "initial_price": payload.get("sumPrice") or payload.get("initialPrice"),
            "sum_price": payload.get("sumPrice") or payload.get("initialPrice"),
            "url": url,
            "card_url": url,
            "source": self.platform_key,
            "raw": payload,
        }

    def _row_from_card_id(self, proc_id: str) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        url = self._detail_url(proc_id)
        self.driver.get(url)
        time.sleep(3)
        page = self.driver.execute_script(
            """
            const pageProps = {...(window.__NEXT_DATA__?.props?.pageProps || {})};
            delete pageProps._nextI18Next;
            return {
              text: String(document.body && document.body.innerText || ''),
              heading: String(document.querySelector('h1')?.innerText || ''),
              title: String(document.title || ''),
              pageProps,
            };
            """
        )
        text = _safe_text((page or {}).get("text"))
        if "Необходима авторизация" in text or "Для просмотра данной процедуры перейдите в личный кабинет" in text:
            raise RuntimeError("Для просмотра этой процедуры ТЭК-Торг нужно аутентифицироваться в Chromium Ghost.")
        if "Эта процедура более недоступна" in text or "404. Такой страницы нет" in text:
            raise RuntimeError(f"Процедура ТЭК-Торг 223-ФЗ {proc_id} не найдена или больше недоступна.")
        payload = self._find_card_payload((page or {}).get("pageProps"), proc_id)
        return self._normalize_card_row(proc_id, payload, _safe_text((page or {}).get("heading")), text)

    def fetch_page(
        self,
        start: int = 0,
        limit: int = 25,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        query: Optional[str] = None,
        tag_id: Optional[int] = None,
        sort: str = "actual",
        direction: str = "DESC",
        client_filters: Any = None,
        _recover_attempt: int = 0,
    ) -> dict[str, Any]:
        card_id = self._looks_like_card_id_search(client_filters, query)
        if card_id:
            try:
                row = self._row_from_card_id(card_id)
            except Exception as exc:
                return {"success": False, "error": str(exc), "procedures": [], "totalCount": 0}
            return {
                "success": True,
                "procedures": [row],
                "totalCount": 1,
                "_debug": {"url": row.get("card_url"), "loaded": 1, "returned": 1},
            }
        return super().fetch_page(
            start=start,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
            query=query,
            tag_id=tag_id,
            sort=sort,
            direction=direction,
            client_filters=client_filters,
            _recover_attempt=_recover_attempt,
        )

    def _normalize_row(self, raw: Any, index: int = 0) -> dict[str, Any]:
        row = super()._normalize_row(raw, index)
        subsection = _safe_text(row.get("subsectionAlias") or row.get("subsection_alias"))
        proc_id = row.get("id") or row.get("procedure_id") or index + 1
        if subsection:
            card_url = f"https://www.tektorg.ru/org/{subsection}/procedures/{proc_id}"
        else:
            card_url = self._detail_url(proc_id)
        row["url"] = card_url
        row["card_url"] = card_url
        return row

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id"))
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry or "tektorg_223")
        out_dir.mkdir(parents=True, exist_ok=True)

        card = self.extract_procedure_card_text(proc, progress=progress)
        links = [link for link in (card.get("document_links") or []) if isinstance(link, dict)]

        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            try:
                if progress:
                    progress(f"Скачиваю файл {index}/{len(links)}")
                saved.append(str(self.download_document_link(link, out_dir, index)))
            except Exception as exc:
                errors.append(f"{link.get('text') or link.get('href')}: {exc}")
        return {
            "procedure": registry,
            "url": card.get("url"),
            "folder": str(out_dir),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }
