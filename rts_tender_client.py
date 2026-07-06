from __future__ import annotations

import base64
import html
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from etp_client import EtpClient


RTS_TENDER_URL = "https://223.rts-tender.ru/supplier/auction/Trade/Search.aspx"
RTS_TENDER_HOST = "223.rts-tender.ru"
RTS_TENDER_GRID_ID = "BaseMainContent_MainContent_jqgTrade"

RTS_TENDER_STATUS_OPTIONS = [
    ("Публикация извещения", "1"),
    ("Прием заявок", "2"),
    ("Рассмотрение заявок", "3"),
    ("Отменена", "4"),
    ("Ожидает начала торгов", "5"),
    ("Не состоялась", "6"),
    ("Идут торги", "7"),
    ("Подведение итогов", "8"),
    ("Заключение договоров", "9"),
    ("Завершена", "10"),
    ("Приостановлена", "11"),
    ("Ожидает переторжку", "12"),
    ("Переторжка", "13"),
    ("Рассмотрение и оценка первых частей заявок", "14"),
    ("Рассмотрение и оценка вторых частей заявок", "15"),
    ("Завершена без заключения договоров", "16"),
    ("Прием окончательных предложений", "18"),
    ("Прием предварительных заявок", "19"),
    ("Уточнение документации", "20"),
    ("Открытие доступа", "21"),
    ("Оценка заявок", "22"),
    ("Готова к публикации", "23"),
    ("Обсуждение предложений о функциональных характеристиках", "24"),
    ("Ожидает обсуждение предложений о функциональных характеристиках", "25"),
    ("Ожидает прием окончательных предложений", "26"),
    ("Ожидает подведения итогов", "27"),
    ("Обсуждение функциональных характеристик", "28"),
    ("Квалификация", "29"),
    ("Сопоставление ценовых предложений", "30"),
    ("Сопоставление дополнительных предложений", "31"),
    ("Обсуждения и уточнение документации", "96"),
    ("Прием заявок, окончательных предложений", "97"),
    ("Активна", "98"),
    ("Определение победителей", "99"),
    ("Формирование", "100"),
]

RTS_TENDER_TYPE_OPTIONS = [
    ("Аукцион", "4"),
    ("Конкурс", "2"),
    ("Запрос предложений", "1"),
    ("Запрос цен", "3"),
    ("Сбор коммерческих предложений", "16"),
    ("Аукцион (заявка в двух частях)", "6"),
    ("Редукцион (заявка в двух частях)", "8"),
    ("Запрос котировок", "9"),
    ("Запрос оферт", "29"),
    ("Редукцион", "7"),
    ("Закупка у единственного поставщика", "10"),
    ("Предварительный отбор", "14"),
    ("Конкурентные переговоры", "15"),
    ("Конкурентный отбор", "30"),
    ("Коммерческий отбор", "31"),
    ("Конкурс в электронной форме МСП", "22"),
    ("Аукцион в электронной форме МСП", "23"),
    ("Запрос предложений в электронной форме МСП", "24"),
    ("Запрос котировок в электронной форме МСП", "25"),
    ("Запрос предложений (заявка в двух частях)", "28"),
    ("Конкурс (заявка в двух частях)", "27"),
    ("Состязательный отбор", "32"),
]

_GRID_FETCH_JS = r"""
const callback = arguments[arguments.length - 1];
const overrides = arguments[0] || {};
(async () => {
  try {
    const grid = window.jQuery && jQuery('#BaseMainContent_MainContent_jqgTrade');
    if (!grid || !grid.length || !grid.jqGrid) {
      callback({ok: false, auth: false, error: 'grid_not_found'});
      return;
    }
    const base = grid.jqGrid('getGridParam', 'postData') || {};
    const data = Object.assign({}, base, overrides);
    data.nd = Date.now();
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(data)) {
      if (Array.isArray(value)) {
        for (const item of value) params.append(key, item);
      } else if (value !== null && value !== undefined) {
        params.append(key, value);
      }
    }
    const endpoint = '/supplier/auction/Trade/Search.aspx?jqGridID=BaseMainContent_MainContent_jqgTrade&' + params.toString();
    const response = await fetch(endpoint, {credentials: 'include'});
    const text = await response.text();
    let payload = null;
    try { payload = JSON.parse(text); } catch (e) {}
    callback({
      ok: response.ok,
      status: response.status,
      contentType: response.headers.get('content-type') || '',
      endpoint,
      payload,
      preview: text.slice(0, 400),
    });
  } catch (e) {
    callback({ok: false, error: String(e && e.message || e)});
  }
})();
"""

_DOWNLOAD_URL_JS = r"""
const callback = arguments[arguments.length - 1];
const href = arguments[0];
(async () => {
  try {
    const response = await fetch(href, {credentials: 'include'});
    if (!response.ok) {
      callback({ok: false, status: response.status, error: 'http_error'});
      return;
    }
    const blob = await response.blob();
    const reader = new FileReader();
    reader.onloadend = () => callback({
      ok: true,
      status: response.status,
      contentType: response.headers.get('content-type') || '',
      dataUrl: String(reader.result || ''),
    });
    reader.onerror = () => callback({ok: false, status: response.status, error: 'read_error'});
    reader.readAsDataURL(blob);
  } catch (e) {
    callback({ok: false, error: String(e && e.message || e)});
  }
})();
"""


def _safe_text(value: Any) -> str:
    text = str(value or "")
    for _ in range(2):
        text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


_RTS_STATUS_VALUE_BY_LABEL = {
    _safe_text(label): value for label, value in RTS_TENDER_STATUS_OPTIONS
}
_RTS_STATUS_VALUE_BY_LABEL["Прием коммерческих предложений"] = "2"


def _safe_filename(name: str, default: str = "file") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", _safe_text(name)).strip(" .")
    return clean[:180] or default


def _date_for_rts(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        y, m, d = text.split("-")
        return f"{d}.{m}.{y}"
    return text


class RtsTenderClient(EtpClient):
    """Клиент РТС-тендер 223-ФЗ через jqGrid endpoint сайта."""

    platform_key = "rts_tender"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = RTS_TENDER_URL
        self.target_host = RTS_TENDER_HOST

    def _detail_url(self, proc_id: Any) -> str:
        return f"https://{RTS_TENDER_HOST}/supplier/auction/Trade/View.aspx?Id={proc_id}&Logging=TradeByNumber"

    def _ensure_search_page(self) -> None:
        assert self.driver is not None, "Сначала вызовите connect()"
        if "223.rts-tender.ru" not in (self.driver.current_url or "") or "Trade/Search.aspx" not in self.driver.current_url:
            self.driver.get(self.target_url)
        deadline = time.time() + 35
        while time.time() < deadline:
            text = ""
            try:
                text = self.driver.execute_script("return String(document.body && document.body.innerText || '')")
                has_grid = self.driver.execute_script(
                    "return !!(window.jQuery && jQuery('#BaseMainContent_MainContent_jqgTrade').length);"
                )
            except Exception:
                has_grid = False
            if has_grid:
                return
            if "Авторизация" in text and "Имя пользователя" in text:
                raise RuntimeError("Для поиска на РТС-тендер нужно аутентифицироваться в выбранном Chrome.")
            time.sleep(0.5)
        raise RuntimeError("РТС-тендер не отдал таблицу поиска. Проверьте авторизацию и повторите поиск.")

    def _build_overrides(self, start: int, limit: int, client_filters: Any = None, query: Optional[str] = None) -> dict[str, Any]:
        page_size = max(1, min(int(limit or 25), 100))
        page = max(1, start // page_size + 1)
        data: dict[str, Any] = {
            "page": page,
            "rows": page_size,
            "sidx": "PublicationDate",
            "sord": "desc",
        }
        if client_filters is not None:
            quick = _safe_text(query or getattr(client_filters, "quick_search", ""))
            registry = _safe_text(getattr(client_filters, "registry_contains", ""))
            title = _safe_text(getattr(client_filters, "title_contains", ""))
            organizer = _safe_text(getattr(client_filters, "organizer_contains", ""))
            if registry:
                data["auto_Number"] = registry
            if title or quick:
                data["auto_Name"] = title or quick
                data["auto_UseTradeName"] = True
                data["auto_UseLotName"] = True
            if organizer:
                data["auto_Organizer"] = organizer
                data["auto_UseOrganizerName"] = True
            statuses = tuple(getattr(client_filters, "step_ids", ()) or ())
            if statuses:
                data["auto_TradeLotState"] = _safe_text(statuses[0])
            types = tuple(getattr(client_filters, "trend_pur_values", ()) or ())
            if not types:
                one_type = _safe_text(getattr(client_filters, "trend_pur", ""))
                types = (one_type,) if one_type else ()
            if types:
                data["manual_PurchaseMethods"] = list(types)
            price_min = getattr(client_filters, "price_min", None)
            price_max = getattr(client_filters, "price_max", None)
            if price_min is not None:
                data["auto_StartPriceMin"] = str(price_min)
            if price_max is not None:
                data["auto_StartPriceMax"] = str(price_max)
            published_from = _date_for_rts(getattr(client_filters, "published_from", None))
            published_to = _date_for_rts(getattr(client_filters, "published_to", None))
            if published_from:
                data["auto_PublicationDate.From"] = published_from
            if published_to:
                data["auto_PublicationDate.To"] = published_to
            end_from = _date_for_rts(getattr(client_filters, "end_from", None))
            end_to = _date_for_rts(getattr(client_filters, "end_to", None))
            if end_from:
                data["auto_ApplicationEndDate.From"] = end_from
            if end_to:
                data["auto_ApplicationEndDate.To"] = end_to
            okpd2 = _safe_text(getattr(client_filters, "okpd2_contains", ""))
            if okpd2:
                data["manual_Okpd2Codes"] = okpd2
        elif query:
            data["auto_Number"] = _safe_text(query)
        return data

    def _normalize_row(self, item: Any) -> dict[str, Any]:
        cells = item.get("cell") if isinstance(item, dict) else None
        cells = cells if isinstance(cells, list) else []
        def cell(index: int) -> str:
            return _safe_text(cells[index] if index < len(cells) else "")

        trade_id = cell(1) or item.get("id")
        status_label = cell(15)
        status_value = _RTS_STATUS_VALUE_BY_LABEL.get(status_label, status_label)
        title = cell(8) or cell(9)
        url = self._detail_url(trade_id)
        return {
            "id": trade_id,
            "procedure_id": trade_id,
            "remote_id": cell(0),
            "registry_number": cell(4) or trade_id,
            "procedure_number": cell(4) or trade_id,
            "oos_number": cell(5),
            "organizer": cell(6),
            "organizer_name": cell(6),
            "customer_region": cell(7),
            "title": title,
            "name": title,
            "lot_name": cell(9),
            "initial_price": cell(10),
            "sum_price": cell(10),
            "date_published": cell(3),
            "date_end_registration": cell(12),
            "date_start_trading": cell(13),
            "type_name": cell(14),
            "procedure_type_name": cell(14),
            "trend_pur_name": cell(14),
            "status": status_value,
            "status_name": status_label,
            "status_label": status_label,
            "step_label": status_label,
            "url": url,
            "card_url": url,
            "print_url": f"https://{RTS_TENDER_HOST}/supplier/auction/Trade/ViewPrintForm.aspx?Guid={cell(16)}&Logging=Printform" if cell(16) else "",
            "source": self.platform_key,
            "raw": item,
        }

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
        assert self.driver is not None, "Сначала вызовите connect()"
        try:
            self._ensure_search_page()
            result = self.driver.execute_async_script(
                _GRID_FETCH_JS,
                self._build_overrides(start, limit, client_filters, query),
            )
        except Exception as exc:
            if self._is_window_lost(exc) and _recover_attempt < 2 and self._recover_tab():
                return self.fetch_page(start, limit, date_from, date_to, query, tag_id, sort, direction, client_filters, _recover_attempt + 1)
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": 0}

        if not isinstance(result, dict) or not result.get("ok"):
            return {"success": False, "error": str(result), "procedures": [], "totalCount": 0}
        payload = result.get("payload")
        if not isinstance(payload, dict):
            preview = _safe_text(result.get("preview"))
            if "Авторизация" in preview:
                return {
                    "success": False,
                    "error": "Для поиска на РТС-тендер нужно аутентифицироваться в выбранном Chrome.",
                    "procedures": [],
                    "totalCount": 0,
                }
            return {"success": False, "error": f"Некорректный ответ РТС-тендер: {preview}", "procedures": [], "totalCount": 0}
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        normalized = [self._normalize_row(row) for row in rows if isinstance(row, dict)]
        return {
            "success": True,
            "procedures": normalized,
            "totalCount": int(payload.get("records") or len(normalized) or 0),
            "_debug": {"endpoint": result.get("endpoint"), "loaded": len(rows), "returned": len(normalized)},
        }

    def extract_procedure_card_text(
        self,
        proc: dict[str, Any],
        progress: Optional[Callable[[str], None]] = None,
        max_page_chars: int = 280_000,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия карточки РТС-тендер.")
        url = _safe_text(proc.get("card_url") or proc.get("url")) or self._detail_url(proc_id)
        if progress:
            progress(f"Открываю карточку РТС-тендер {proc.get('registry_number') or proc_id}: {url}")
        self.driver.get(url)
        time.sleep(3)
        page = self.driver.execute_script(
            """
            return {
              text: String(document.body && document.body.innerText || ''),
              title: String(document.title || ''),
              links: Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                href: a.href,
                text: String(a.innerText || a.textContent || '').trim(),
                title: a.title || '',
                onclick: a.getAttribute('onclick') || '',
              })),
            };
            """
        )
        text = str((page or {}).get("text") or "")
        if "Авторизация" in text and "Имя пользователя" in text:
            raise RuntimeError("Для просмотра карточки РТС-тендер нужно аутентифицироваться в выбранном Chrome.")
        links = self._document_links_from_page(page)
        structured = json.dumps(proc, ensure_ascii=False, default=str)
        return {
            "url": url,
            "page_text": f"СТРУКТУРИРОВАННЫЕ ДАННЫЕ РТС-ТЕНДЕР:\n{structured}\n\nТЕКСТ КАРТОЧКИ:\n{text[:max_page_chars]}",
            "document_links": links,
        }

    def _document_links_from_page(self, page: Any) -> list[dict[str, Any]]:
        if not isinstance(page, dict):
            return []
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in page.get("links") or []:
            if not isinstance(item, dict):
                continue
            href = _safe_text(item.get("href"))
            text = _safe_text(item.get("text") or item.get("title") or href.rsplit("/", 1)[-1])
            hay = f"{href} {text} {_safe_text(item.get('onclick'))}".casefold()
            if not href or href in seen:
                continue
            if any(skip in hay for skip in ("reglament", "soglashenie", "регламент", "соглашение", "соглашаюсь")):
                continue
            has_download_handler = "filedownloadhandler.ashx" in hay and "downloadtradedocumentwithlogging" in hay
            has_file_name = re.search(r"\.(zip|rar|7z|pdf|docx?|xlsx?|xlsm|rtf|txt|xml)(?:$|[?#])", f"{href} {text}", re.I)
            if not has_download_handler and not has_file_name:
                continue
            seen.add(href)
            result.append({"href": href, "text": text or f"document_{len(result) + 1}"})
        return result

    def _filename_from_link(self, link: dict[str, Any], index: int) -> str:
        text = _safe_text(link.get("text"))
        href = _safe_text(link.get("href"))
        for source in (text, href):
            match = re.search(r"([^/?#]+\.(?:zip|rar|7z|pdf|docx?|xlsx?|xlsm|rtf|txt|xml))", source, re.I)
            if match:
                return _safe_filename(match.group(1), f"document_{index}")
        return _safe_filename(text or f"document_{index}", f"document_{index}")

    def download_document_link(self, link: dict[str, Any], output_dir: Path, index: int = 1) -> Path:
        assert self.driver is not None, "Сначала вызовите connect()"
        href = _safe_text(link.get("href"))
        if not href:
            raise RuntimeError("Пустая ссылка на документ.")
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / self._filename_from_link(link, index)
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = output_dir / f"{stem}_{n}{suffix}"
            n += 1
        absolute = urljoin(self.driver.current_url, href)
        res = self.driver.execute_async_script(_DOWNLOAD_URL_JS, absolute)
        if not isinstance(res, dict) or not res.get("ok"):
            raise RuntimeError(f"Ошибка скачивания {target.name}: {res}")
        data_url = str(res.get("dataUrl") or "")
        if "," not in data_url:
            raise RuntimeError(f"Пустой ответ при скачивании {target.name}")
        target.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        return target

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id"))
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry or "rts_tender")
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
