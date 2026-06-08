from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from etp_client import EtpClient


TEKTORG_ROSNEFT_URL = "https://www.tektorg.ru/rosneft/procedures"
TEKTORG_ROSNEFT_HOST = "www.tektorg.ru"

TEKTORG_ROSNEFT_STATUS_OPTIONS = [
    ("Архив", "Архив"),
    ("Отменён", "Отменён"),
    ("Приём заявок", "Приём заявок"),
    ("Работа комиссии", "Работа комиссии"),
]

TEKTORG_ROSNEFT_TYPE_OPTIONS = [
    ("Аукцион", "Аукцион"),
    ("Закупка у единственного поставщика", "Закупка у единственного поставщика"),
    ("Запрос котировок", "Запрос котировок"),
    ("Запрос оферт", "Запрос оферт"),
    ("Запрос предложений", "Запрос предложений"),
    ("Запрос цен", "Запрос цен"),
    ("Конкурс", "Конкурс"),
]

_DOWNLOAD_EXT_RE = re.compile(
    r"\.(?:docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv)(?:$|[?#])",
    re.I,
)

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

_COLLECT_DOCUMENT_LINKS_JS = r"""
return Array.from(document.querySelectorAll('a[href]'))
  .map((a) => ({
    href: a.href,
    text: String(a.innerText || a.textContent || '').trim(),
    download: a.getAttribute('download') || '',
  }))
  .filter((x) => {
    const hay = `${x.href} ${x.text}`.toLowerCase();
    return /\.(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv)(?:$|[?#])/i.test(x.href)
      || /документ|документац|извещен|техническ|скачать|download|файл|протокол/i.test(hay);
  });
"""


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_filename(name: str, default: str = "file") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", _safe_text(name)).strip(" .")
    return clean[:180] or default


def _normalize_status(value: Any) -> str:
    return _safe_text(value).casefold().replace("ё", "е")


def _format_date_filter(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return _safe_text(value)


class TektorgRosneftClient(EtpClient):
    """Клиент публичной секции ТЭК-Торг «НК Роснефть»."""

    platform_key = "tektorg_rosneft"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_ROSNEFT_URL
        self.target_host = TEKTORG_ROSNEFT_HOST

    def _detail_url(self, proc_id: Any) -> str:
        return f"{TEKTORG_ROSNEFT_URL}/{proc_id}"

    def _switch_to_etp_tab(self) -> bool:
        ok = super()._switch_to_etp_tab()
        if ok and self.driver is not None:
            try:
                if "tektorg.ru" not in (self.driver.current_url or ""):
                    self.driver.get(self.target_url)
            except Exception:
                pass
        return ok

    def _build_query(self, start: int, client_filters: Any = None, query: Optional[str] = None) -> str:
        params: dict[str, str] = {}
        page = max(1, start // 15 + 1)
        if page > 1:
            params["page"] = str(page)
        if client_filters is not None:
            registry = _safe_text(getattr(client_filters, "registry_contains", ""))
            if registry:
                params["registryNumber"] = registry
            price_min = getattr(client_filters, "price_min", None)
            price_max = getattr(client_filters, "price_max", None)
            if price_min is not None:
                params["sumPrice_start"] = str(price_min)
            if price_max is not None:
                params["sumPrice_end"] = str(price_max)
            published = _format_date_filter(getattr(client_filters, "published_from", None))
            if published:
                params["datePublished"] = published
            end = _format_date_filter(getattr(client_filters, "end_to", None))
            if end:
                params["dateEndRegistration"] = end
        elif query:
            params["registryNumber"] = query
        return ("?" + urlencode(params)) if params else ""

    def _next_listing_state(self) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        data = self.driver.execute_script(
            "return window.__NEXT_DATA__ && window.__NEXT_DATA__.props && "
            "window.__NEXT_DATA__.props.pageProps && "
            "window.__NEXT_DATA__.props.pageProps.initialReduxState && "
            "window.__NEXT_DATA__.props.pageProps.initialReduxState.listingProcedures || null;"
        )
        return data if isinstance(data, dict) else {}

    def _normalize_row(self, raw: Any, index: int = 0) -> dict[str, Any]:
        row = raw if isinstance(raw, dict) else {}
        proc_id = row.get("id") or index + 1
        dates = row.get("dates") if isinstance(row.get("dates"), dict) else {}
        status = _safe_text(row.get("statusName"))
        type_name = _safe_text(row.get("typeName"))
        registry = _safe_text(row.get("registryNumber") or proc_id)
        title = _safe_text(row.get("title"))
        return {
            **row,
            "id": proc_id,
            "procedure_id": proc_id,
            "remote_id": row.get("remoteId"),
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
            "organizer": row.get("organizerName"),
            "organizer_name": row.get("organizerName"),
            "date_published": dates.get("datePublished"),
            "date_end_registration": dates.get("dateEndRegistration"),
            "initial_price": row.get("sumPrice"),
            "sum_price": row.get("sumPrice"),
            "url": self._detail_url(proc_id),
            "card_url": self._detail_url(proc_id),
            "etp_link": row.get("etpLink"),
            "source": self.platform_key,
            "raw": row,
        }

    def _matches_selected_statuses(self, proc: dict[str, Any], client_filters: Any) -> bool:
        selected = tuple(getattr(client_filters, "step_ids", ()) or ())
        if not selected:
            return True
        status = _normalize_status(proc.get("status_name"))
        return any(_normalize_status(value) == status for value in selected)

    def _matches_selected_types(self, proc: dict[str, Any], client_filters: Any) -> bool:
        selected = tuple(getattr(client_filters, "trend_pur_values", ()) or ())
        if not selected:
            value = _safe_text(getattr(client_filters, "trend_pur", ""))
            selected = (value,) if value else ()
        if not selected:
            return True
        type_name = _normalize_status(proc.get("type_name"))
        return any(_normalize_status(value) == type_name for value in selected)

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
        url = self.target_url + self._build_query(start, client_filters, query)
        try:
            self.driver.get(url)
            deadline = time.time() + 25
            state: dict[str, Any] = {}
            while time.time() < deadline:
                state = self._next_listing_state()
                if isinstance(state.get("data"), list):
                    break
                time.sleep(0.5)
        except Exception as exc:
            if self._is_window_lost(exc) and _recover_attempt < 2 and self._recover_tab():
                return self.fetch_page(
                    start=start,
                    limit=limit,
                    date_from=date_from,
                    date_to=date_to,
                    query=query,
                    tag_id=tag_id,
                    sort=sort,
                    direction=direction,
                    client_filters=client_filters,
                    _recover_attempt=_recover_attempt + 1,
                )
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": 0}

        raw_rows = state.get("data") if isinstance(state.get("data"), list) else []
        rows = [self._normalize_row(row, start + idx) for idx, row in enumerate(raw_rows)]
        if client_filters is not None:
            rows = [
                row for row in rows
                if self._matches_selected_statuses(row, client_filters)
                and self._matches_selected_types(row, client_filters)
            ]
        total = int(state.get("total") or len(raw_rows) or 0)
        return {
            "success": True,
            "procedures": rows,
            "totalCount": total,
            "_debug": {"url": url, "loaded": len(raw_rows), "returned": len(rows)},
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
            raise RuntimeError("У процедуры нет id для открытия подробной страницы.")
        url = self._detail_url(proc_id)
        if progress:
            progress(f"Открываю карточку ТЭК-Торг {proc.get('registry_number') or proc_id}: {url}")
        self.driver.get(url)
        time.sleep(3)
        page = self.driver.execute_script(
            """
            return {
              text: String(document.body && document.body.innerText || ''),
              links: Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                href: a.href,
                text: String(a.innerText || a.textContent || '').trim(),
                download: a.getAttribute('download') || '',
              })),
              nextData: window.__NEXT_DATA__ || null,
            };
            """
        )
        text = str((page or {}).get("text") or "")[:max_page_chars]
        links = self._document_links_from_page(page)
        structured = json.dumps(proc, ensure_ascii=False, default=str)
        return {
            "url": url,
            "page_text": f"СТРУКТУРИРОВАННЫЕ ДАННЫЕ ТЭК-ТОРГ:\n{structured}\n\nТЕКСТ КАРТОЧКИ:\n{text}",
            "document_links": links,
        }

    def _document_links_from_page(self, page: Any) -> list[dict[str, Any]]:
        if not isinstance(page, dict):
            return []
        result: list[dict[str, Any]] = []
        for item in page.get("links") or []:
            if not isinstance(item, dict):
                continue
            href = _safe_text(item.get("href"))
            text = _safe_text(item.get("text"))
            if not href:
                continue
            if "api.tektorg.ru/open-api/documents/procedure/" in href or _DOWNLOAD_EXT_RE.search(href):
                result.append({"href": href, "text": text or href.rsplit("/", 1)[-1]})
        return result

    def _filename_from_link(self, link: dict[str, Any], index: int) -> str:
        text = _safe_text(link.get("text"))
        href = _safe_text(link.get("href"))
        for source in (text, href.rsplit("/", 1)[-1]):
            m = re.search(
                r"([^/?#]+\.(?:docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv))",
                source,
                re.I,
            )
            if m:
                return _safe_filename(m.group(1), f"document_{index}")
        return _safe_filename(text or f"document_{index}", f"document_{index}")

    def download_document_link(self, link: dict[str, Any], output_dir: Path, index: int = 1) -> Path:
        assert self.driver is not None, "Сначала вызовите connect()"
        href = _safe_text(link.get("href"))
        if not href:
            raise RuntimeError("Пустая ссылка на документ.")
        output_dir.mkdir(parents=True, exist_ok=True)
        name = self._filename_from_link(link, index)
        target = output_dir / name
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = output_dir / f"{stem}_{n}{suffix}"
            n += 1
        absolute = urljoin(self.driver.current_url, href)
        res = self.driver.execute_async_script(_DOWNLOAD_URL_JS, absolute)
        if isinstance(res, dict) and res.get("ok"):
            data_url = str(res.get("dataUrl") or "")
            if "," not in data_url:
                raise RuntimeError(f"Пустой ответ при скачивании {name}")
            target.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            return target
        if "api.tektorg.ru/open-api/documents/procedure/" not in absolute:
            raise RuntimeError(f"Ошибка скачивания {name}: {res}")
        request = Request(
            absolute,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/octet-stream, application/json, */*",
            },
        )
        with urlopen(request, timeout=120) as response:
            target.write_bytes(response.read())
        return target

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id"))
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry or "tektorg")
        out_dir.mkdir(parents=True, exist_ok=True)

        card = self.extract_procedure_card_text(proc, progress=progress)
        links = list(card.get("document_links") or [])
        etp_link = _safe_text(proc.get("etp_link") or proc.get("etpLink"))
        if etp_link:
            if progress:
                progress(f"Открываю старую карточку ТЭК-Торг для документов: {etp_link}")
            try:
                self.driver.get(etp_link)
                time.sleep(8)
                legacy_links = self.driver.execute_script(_COLLECT_DOCUMENT_LINKS_JS)
                if isinstance(legacy_links, list):
                    links.extend(item for item in legacy_links if isinstance(item, dict))
            except Exception:
                pass

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in links:
            href = _safe_text(link.get("href"))
            if not href or href in seen:
                continue
            seen.add(href)
            deduped.append(link)

        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(deduped, start=1):
            try:
                if progress:
                    progress(f"Скачиваю файл {index}/{len(deduped)}")
                saved.append(str(self.download_document_link(link, out_dir, index)))
            except Exception as exc:
                errors.append(f"{link.get('text') or link.get('href')}: {exc}")
        return {
            "procedure": registry,
            "url": card.get("url"),
            "folder": str(out_dir),
            "found": len(deduped),
            "saved": saved,
            "errors": errors,
        }
