from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from etp_client import EtpClient


GPB_BUSINESS_PROCUREMENT_URL = "https://com.etpgpb.ru/"

GPB_BUSINESS_PROCUREMENT_STATUS_OPTIONS = [
    ("Опубликована", "published"),
    ("Прием заявок", "applications_acceptance"),
    ("Прием технических предложений", "technical_offers_acceptance"),
    ("Прием коммерческих предложений", "commercial_offers_acceptance"),
    ("Рассмотрение заявок", "applications_review"),
    ("Подведение итогов", "summing_up"),
    ("Переторжка", "rebidding"),
    ("Очная переторжка", "rebidding_trade"),
    ("Корректировка заявок", "applications_correction"),
    ("Ожидание переторжки", "rebidding_awaiting"),
    ("Отменена", "cancelled"),
    ("Архив", "archive"),
]

GPB_BUSINESS_PROCUREMENT_TYPE_OPTIONS = [
    ("Попозиционная закупка", "POSITIONS_TENDER"),
    ("Запрос предложений", "QUOTATION_REQUEST"),
    ("Попозиционная закупка с приемом технических предложений", "POSITIONS_TENDER_TECH_OFFERS"),
]

_STATUS_LABELS = {value: label for label, value in GPB_BUSINESS_PROCUREMENT_STATUS_OPTIONS}
_TYPE_LABELS = {value: label for label, value in GPB_BUSINESS_PROCUREMENT_TYPE_OPTIONS}


_COM_XHR_JSON_JS = r"""
const callback = arguments[arguments.length - 1];
const request = arguments[0] || {};
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let i = 0; i < 80; i++) {
    const href = String(location.href || '');
    const text = String(document.body && document.body.innerText || '');
    if (href.includes('openid-connect/auth')) {
      callback({ok: false, no_session: true, message: 'Требуется авторизация в ЕЛК.', url: href});
      return;
    }
    if (href.includes('com.etpgpb.ru') && !/Загрузка/i.test(text)) break;
    await wait(250);
  }

  const xhr = new XMLHttpRequest();
  const method = request.method || 'GET';
  const url = request.url || '/';
  xhr.open(method, url, true);
  xhr.withCredentials = true;
  xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
  xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
  const token = window.localStorage && window.localStorage.getItem('LS_TOKEN');
  if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
  const csrf = (String(document.cookie || '').match(/(?:^|;\s*)csrf-token=([^;]+)/) || [])[1];
  if (csrf) xhr.setRequestHeader('X-CSRF-Token', decodeURIComponent(csrf));
  if (request.body !== undefined && request.body !== null) {
    xhr.setRequestHeader('Content-Type', 'application/json');
  }
  xhr.onload = () => {
    let data = null;
    try { data = JSON.parse(xhr.responseText || 'null'); } catch (e) {}
    callback({
      ok: xhr.status >= 200 && xhr.status < 300,
      status: xhr.status,
      data,
      text: String(xhr.responseText || '').slice(0, 4000),
      url,
    });
  };
  xhr.onerror = () => callback({ok: false, status: xhr.status || 0, error: 'network_error', url});
  xhr.send(request.body !== undefined && request.body !== null ? JSON.stringify(request.body) : null);
})();
"""


_COM_DOWNLOAD_FILE_JS = r"""
const callback = arguments[arguments.length - 1];
const href = arguments[0];
(async () => {
  const xhr = new XMLHttpRequest();
  xhr.open('GET', href, true);
  xhr.withCredentials = true;
  xhr.responseType = 'blob';
  xhr.setRequestHeader('Accept', 'application/octet-stream, application/json, text/plain, */*');
  xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
  const token = window.localStorage && window.localStorage.getItem('LS_TOKEN');
  if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
  const csrf = (String(document.cookie || '').match(/(?:^|;\s*)csrf-token=([^;]+)/) || [])[1];
  if (csrf) xhr.setRequestHeader('X-CSRF-Token', decodeURIComponent(csrf));
  xhr.onload = () => {
    if (xhr.status < 200 || xhr.status >= 300) {
      callback({ok: false, status: xhr.status, error: 'http_error'});
      return;
    }
    const reader = new FileReader();
    reader.onloadend = () => callback({
      ok: true,
      status: xhr.status,
      contentType: xhr.getResponseHeader('content-type') || '',
      dataUrl: String(reader.result || ''),
    });
    reader.onerror = () => callback({ok: false, status: xhr.status, error: 'read_error'});
    reader.readAsDataURL(xhr.response);
  };
  xhr.onerror = () => callback({ok: false, status: xhr.status || 0, error: 'network_error'});
  xhr.send();
})();
"""


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_filename(name: str, default: str = "file") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", _safe_text(name)).strip(" .")
    return clean[:180] or default


def _first_lot(row: dict[str, Any]) -> dict[str, Any]:
    lots = row.get("lots")
    if isinstance(lots, list) and lots and isinstance(lots[0], dict):
        return lots[0]
    return {}


def _format_date_filter(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return _safe_text(value)


def _customer_name(customers: Any) -> str:
    if isinstance(customers, list) and customers:
        customer = customers[0]
        if isinstance(customer, dict):
            return _safe_text(customer.get("shortName") or customer.get("fullName"))
    return ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class GpbBusinessProcurementClient(EtpClient):
    """Клиент отдельной площадки «Закупки Бизнес» (`https://com.etpgpb.ru/`)."""

    platform_key = "gpb_business_procurement"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = GPB_BUSINESS_PROCUREMENT_URL
        self.target_host = "com.etpgpb.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return f"https://com.etpgpb.ru/procedure/container/{proc_id}"

    def _switch_to_etp_tab(self) -> bool:
        ok = super()._switch_to_etp_tab()
        if ok and self.driver is not None:
            try:
                if "com.etpgpb.ru" not in (self.driver.current_url or ""):
                    self.driver.get(self.target_url)
            except Exception:
                pass
        return ok

    def _request_json(self, method: str, url: str, body: Any = None, timeout: int = 90) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        try:
            self.driver.set_script_timeout(timeout)
            result = self.driver.execute_async_script(
                _COM_XHR_JSON_JS,
                {"method": method, "url": url, "body": body},
            )
        finally:
            try:
                self.driver.set_script_timeout(30)
            except Exception:
                pass
        return result if isinstance(result, dict) else {"ok": False, "error": "no_response", "raw": result}

    def _build_filters(self, client_filters: Any, query: Optional[str]) -> list[dict[str, str]]:
        filters: list[dict[str, str]] = []

        def add(name: str, value: Any) -> None:
            text = _safe_text(value)
            if text:
                filters.append({"name": name, "value": text})

        if client_filters is None:
            add("query", query)
            return filters

        registry = _safe_text(getattr(client_filters, "registry_contains", ""))
        title = _safe_text(getattr(client_filters, "title_contains", ""))
        organizer = _safe_text(getattr(client_filters, "organizer_contains", ""))
        customer = _safe_text(getattr(client_filters, "customer_contains", ""))
        okpd = _safe_text(getattr(client_filters, "okpd2_contains", ""))
        position = _safe_text(getattr(client_filters, "position_name_contains", ""))
        trend_values = tuple(getattr(client_filters, "trend_pur_values", ()) or ())
        step_ids = tuple(getattr(client_filters, "step_ids", ()) or ())

        add("query", query or getattr(client_filters, "quick_search", ""))
        add("registryNumber", registry)
        add("title", title)
        add("positionTitles", position)
        add("okpdCodes", okpd)
        add("fullName", organizer or customer)
        add("type", trend_values[0] if len(trend_values) == 1 else getattr(client_filters, "trend_pur", ""))
        add("status", step_ids[0] if len(step_ids) == 1 else "")
        add("priceStart", getattr(client_filters, "price_min", None))
        add("priceEnd", getattr(client_filters, "price_max", None))
        add("publishDateStart", _format_date_filter(getattr(client_filters, "published_from", None)))
        add("publishDateEnd", _format_date_filter(getattr(client_filters, "published_to", None)))
        add("applicationsAcceptanceDateEnd", _format_date_filter(getattr(client_filters, "end_to", None)))
        return filters

    def _normalize_row(self, raw: Any, index: int = 0) -> dict[str, Any]:
        payload = raw.get("row") if isinstance(raw, dict) and isinstance(raw.get("row"), dict) else raw
        if not isinstance(payload, dict):
            return {
                "id": index + 1,
                "registry_number": "",
                "title": _safe_text(payload),
                "source": self.platform_key,
                "raw": raw,
            }

        lot = _first_lot(payload)
        container_uuid = (payload.get("uuid") or raw.get("uuid")) if isinstance(raw, dict) else payload.get("uuid")
        registry = _safe_text(payload.get("registryNumber") or lot.get("registryNumber") or container_uuid)
        status = _safe_text(lot.get("status") or payload.get("status"))
        type_code = _safe_text(payload.get("type") or lot.get("type"))
        customers = lot.get("customers") or payload.get("customers")
        title = _safe_text(payload.get("title") or lot.get("title"))
        current_step = _dict(lot.get("currentStep"))
        organizer = _dict(payload.get("organizer"))

        proc = dict(payload)
        proc.update(
            {
                "id": container_uuid or index + 1,
                "procedure_id": container_uuid or index + 1,
                "container_uuid": container_uuid,
                "lot_uuid": lot.get("uuid"),
                "registry_number": registry,
                "procedure_number": registry,
                "title": title,
                "trend_pur": type_code,
                "trend_pur_label": _TYPE_LABELS.get(type_code, _safe_text(payload.get("typeCustomName")) or type_code),
                "procedure_type": type_code,
                "procedure_type_name": _TYPE_LABELS.get(type_code, _safe_text(payload.get("typeCustomName"))),
                "status": status,
                "status_name": _STATUS_LABELS.get(status, _safe_text(current_step.get("name")) or status),
                "status_label": _STATUS_LABELS.get(status, _safe_text(current_step.get("name")) or status),
                "step_label": _STATUS_LABELS.get(status, _safe_text(current_step.get("name")) or status),
                "short_name": _customer_name(customers) or _safe_text(organizer.get("shortName")),
                "full_name": _customer_name(customers) or _safe_text(organizer.get("fullName")),
                "date_published": _safe_text(payload.get("publishedAt")),
                "date_start_registration": _safe_text(current_step.get("dateStart")),
                "date_end_registration": _safe_text(current_step.get("dateEnd")),
                "total_price": _safe_text(lot.get("price")),
                "currency_name": _safe_text(lot.get("currency") or "RUB"),
                "lots_count": len(payload.get("lots") or []) or 1,
                "source": self.platform_key,
                "url": self._detail_url(container_uuid) if container_uuid else "",
            }
        )
        return proc

    def fetch_page(
        self,
        start: int = 0,
        limit: int = 25,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        query: Optional[str] = None,
        tag_id: Optional[int] = None,
        sort: str = "id",
        direction: str = "DESC",
        client_filters: Any = None,
        _recover_attempt: int = 0,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        try:
            current = self.driver.current_url or ""
        except Exception:
            current = ""
        if "com.etpgpb.ru" not in current or "/procedure/" in current:
            self.driver.get(self.target_url)
            time.sleep(3)

        filters = self._build_filters(client_filters, query)
        body = {"limit": limit, "offset": start, "filters": filters}
        debug = {
            "platform": self.platform_key,
            "method": "POST",
            "url": "/api/public/gate/grpc/v1/datagrid/container/grid/",
            "request_payload": body,
        }
        try:
            res = self._request_json("POST", "/api/public/gate/grpc/v1/datagrid/container/grid/", body)
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
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": None, "_debug": debug}

        debug["raw_response"] = {k: v for k, v in res.items() if k != "data"}
        if res.get("no_session"):
            return {
                "success": False,
                "no_session": True,
                "message": res.get("message") or "Требуется авторизация в ЕЛК.",
                "procedures": [],
                "totalCount": None,
                "_debug": debug,
            }
        if not res.get("ok") or not isinstance(res.get("data"), dict):
            return {
                "success": False,
                "error": res.get("error") or f"HTTP {res.get('status')}",
                "procedures": [],
                "totalCount": None,
                "_debug": debug,
            }

        data = res["data"]
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        procedures = [self._normalize_row(row, i) for i, row in enumerate(rows)]
        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        return {
            "success": True,
            "procedures": procedures,
            "totalCount": pagination.get("totalCount", len(procedures)),
            "_debug": debug,
        }

    def extract_procedure_card_text(
        self,
        proc: dict[str, Any],
        progress: Optional[Callable[[str], None]] = None,
        max_page_chars: int = 280_000,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        container_uuid = proc.get("container_uuid") or proc.get("id") or proc.get("procedure_id")
        if not container_uuid:
            raise RuntimeError("У закупки нет container uuid для открытия карточки.")
        registry = _safe_text(proc.get("registry_number") or container_uuid)
        url = _safe_text(proc.get("url") or self._detail_url(container_uuid))
        if progress:
            progress(f"Читаю карточку закупки {registry}: {url}")
        self.driver.get(url)
        time.sleep(1)
        api = self._request_json("GET", f"/api/public/gate/grpc/v1/container/get/{container_uuid}", timeout=90)
        if not api.get("ok") or not isinstance(api.get("data"), dict):
            raise RuntimeError(f"Не удалось прочитать карточку Закупки Бизнес: {api}")
        data = api["data"]
        try:
            page_text = self.driver.execute_script("return document.body && document.body.innerText || ''") or ""
        except Exception:
            page_text = ""
        structured = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        page_text = (
            "СТРУКТУРИРОВАННЫЕ ДАННЫЕ ЗАКУПКИ БИЗНЕС:\n"
            f"{structured}\n\nТЕКСТ КАРТОЧКИ:\n{page_text}"
        ).strip()
        if len(page_text) > max_page_chars:
            page_text = page_text[:max_page_chars] + "\n\n[…текст обрезан…]"

        documents = data.get("containerInfo", {}).get("documents")
        doc_links = []
        if isinstance(documents, list):
            for doc in documents:
                if not isinstance(doc, dict):
                    continue
                uuid = _safe_text(doc.get("uuid"))
                name = _safe_text(doc.get("name") or uuid)
                external = _safe_text(doc.get("externalUrl"))
                href = external or f"/api/public/gate/grpc/v1/container/getFile?uuid={uuid}"
                if uuid or external:
                    doc_links.append({"href": href, "text": name, "name": name, "uuid": uuid})
        return {
            "procedure": registry,
            "procedure_id": container_uuid,
            "url": url,
            "page_text": page_text,
            "doc_links": doc_links,
            "primary_doc_url": doc_links[0]["href"] if doc_links else "",
            "product_rows_info": {"lots": data.get("lots") or []},
            "char_count": len(page_text),
        }

    def download_document_link(self, link: dict[str, Any], output_dir: Path, index: int = 1) -> Path:
        assert self.driver is not None, "Сначала вызовите connect()"
        href = _safe_text((link or {}).get("href"))
        if not href:
            raise RuntimeError("Пустая ссылка на документ.")
        output_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_filename((link or {}).get("name") or (link or {}).get("text") or f"document_{index}", f"document_{index}")
        target = output_dir / name
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = output_dir / f"{stem}_{n}{suffix}"
            n += 1
        result = self.driver.execute_async_script(_COM_DOWNLOAD_FILE_JS, href)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Ошибка скачивания {name}: {result}")
        data_url = _safe_text(result.get("dataUrl"))
        if "," not in data_url:
            raise RuntimeError(f"Пустой ответ при скачивании {name}")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        if raw[:1] == b"{" and b"error" in raw[:200].lower():
            raise RuntimeError(f"Сервер вернул ошибку вместо файла: {raw[:300]!r}")
        target.write_bytes(raw)
        return target

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        card = self.extract_procedure_card_text(proc, progress=progress)
        registry = _safe_text(card.get("procedure") or proc.get("registry_number") or proc.get("id") or "com")
        title = _safe_text(proc.get("title"))
        folder = output_root / _safe_filename(f"{registry}_{title[:80]}", registry)
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        errors: list[str] = []
        links = card.get("doc_links") if isinstance(card.get("doc_links"), list) else []
        for index, link in enumerate(links, start=1):
            try:
                if progress:
                    progress(f"Скачиваю {registry}: {index}/{len(links)}")
                saved.append(str(self.download_document_link(link, folder, index=index)))
            except Exception as exc:
                errors.append(f"{(link or {}).get('text') or index}: {exc}")
        return {
            "procedure": registry,
            "url": card.get("url") or "",
            "folder": str(folder),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }
