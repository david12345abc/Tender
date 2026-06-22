from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from desktop_app.params import ClientFilters
from etp_client import EtpClient, HARD_SERVER_LIMIT


ROSELTORG_44_URL = "https://gos.roseltorg.ru/#procedures"

ROSELTORG_44_PROCEDURE_TYPE_OPTIONS = [
    ("Электронный аукцион", "1"),
    ("Электронный конкурс", "3"),
    ("Электронный конкурс по ч.19 ст.48 44-ФЗ", "8"),
    ("Запрос котировок в электронной форме", "6"),
    ("Закупка у единственного поставщика", "9"),
]

ROSELTORG_44_STATUS_OPTIONS = [
    ("Прием заявок", "LotAcceptApplications"),
    ("Отбор предложений", "LotSelectionProposals"),
    ("Рассмотрение первых частей", "LotFirstPartsReview"),
    ("Ожидание подачи ЦП", "LotWaitPriceOffer"),
    ("Подача ЦП", "LotPriceOffer"),
    ("Торги", "LotAuctionStart"),
    ("Рассмотрение вторых частей", "LotSecondPartsReview"),
    ("Подведение итогов", "LotSummingUp"),
    ("Заключение контракта. Публикация проекта контракта заказчиком", "LotComposeContractDraft"),
    ("Заключение контракта. Доработка проекта контракта по протоколу разногласий", "LotComposeContractResolveRefusal"),
    ("Заключение контракта. Подписание контракта победителем", "LotComposeContractSupplierSignature"),
    ("Заключение контракта. Подписание контракта заказчиком", "LotComposeContractCustomerSignature"),
    ("Заключение контракта (совместная закупка)", "LotComposeContractJointPurchase"),
    ("Заключение контракта (мультипобедитель)", "LotComposeContractMultiCount"),
    ("Процедура приостановлена по требованию контролирующего органа", "FasStopedLot"),
    ("Приостановлена на приёме заявок по техническим причинам", "LotStoppedAfterAcceptApplications"),
    ("Приостановлена на торгах по техническим причинам", "LotStoppedAfterAuction"),
    ("Приостановлена на подаче ОЦП по техническим причинам", "LotStoppedAfterRebidding"),
]

ROSELTORG_44_STATUS_LABELS = {value: label for label, value in ROSELTORG_44_STATUS_OPTIONS}
ROSELTORG_44_TYPE_LABELS = {value: label for label, value in ROSELTORG_44_PROCEDURE_TYPE_OPTIONS}


_GOS_FETCH_JSON_JS = r"""
const callback = arguments[arguments.length - 1];
const path = arguments[0];
(async () => {
  let token = '';
  try {
    const raw = localStorage.getItem('app-state-authtoken') || '';
    token = raw ? JSON.parse(raw) : '';
  } catch (e) {
    token = localStorage.getItem('app-state-authtoken') || '';
  }
  if (!token) {
    callback({ok: false, no_session: true, message: 'Нет активной сессии Росэлторг 44-ФЗ.'});
    return;
  }
  try {
    const resp = await fetch(path, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Accept': 'application/json, text/plain, */*',
        'Authorization': `Bearer ${token}`,
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) {}
    callback({
      ok: resp.ok,
      status: resp.status,
      data,
      text: data ? '' : text.slice(0, 4000),
      no_session: resp.status === 401 || resp.status === 403,
      path,
    });
  } catch (e) {
    callback({ok: false, error: String(e && e.message || e), path});
  }
})();
"""


_GOS_DOWNLOAD_JS = r"""
const callback = arguments[arguments.length - 1];
const href = arguments[0];
(async () => {
  let token = '';
  try {
    const raw = localStorage.getItem('app-state-authtoken') || '';
    token = raw ? JSON.parse(raw) : '';
  } catch (e) {
    token = localStorage.getItem('app-state-authtoken') || '';
  }
  try {
    const resp = await fetch(href, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Accept': 'application/octet-stream, application/json, text/plain, */*',
        'Authorization': token ? `Bearer ${token}` : '',
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!resp.ok) {
      callback({ok: false, status: resp.status, error: 'http_error'});
      return;
    }
    const blob = await resp.blob();
    const reader = new FileReader();
    reader.onloadend = () => callback({
      ok: true,
      status: resp.status,
      contentType: resp.headers.get('content-type') || '',
      dataUrl: String(reader.result || ''),
    });
    reader.onerror = () => callback({ok: false, status: resp.status, error: 'read_error'});
    reader.readAsDataURL(blob);
  } catch (e) {
    callback({ok: false, error: String(e && e.message || e)});
  }
})();
"""


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_filename(name: str, default: str = "file") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", _safe_text(name)).strip(" .")
    return clean[:180] or default


def _format_date_filter(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return _safe_text(value)


class Roseltorg44Client(EtpClient):
    """Клиент секции Росэлторг 44-ФЗ (`https://gos.roseltorg.ru`)."""

    platform_key = "roseltorg_44"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = ROSELTORG_44_URL
        self.target_host = "gos.roseltorg.ru"
        self._filters = ClientFilters()

    def set_client_filters(self, filters: ClientFilters) -> None:
        self._filters = filters

    def _detail_url(self, proc_id: Any, lot_id: Any = None) -> str:
        if lot_id:
            return f"https://gos.roseltorg.ru/#lotinfo/{proc_id}/{lot_id}"
        return f"https://gos.roseltorg.ru/#lotinfo/{proc_id}"

    def pull_token(self) -> str:
        if not self.driver:
            return ""
        try:
            token = self.driver.execute_script(
                """
                try {
                  const raw = localStorage.getItem('app-state-authtoken') || '';
                  return raw ? JSON.parse(raw) : '';
                } catch (e) {
                  return localStorage.getItem('app-state-authtoken') || '';
                }
                """
            ) or ""
        except Exception:
            token = ""
        self._token = str(token or "")
        return self._token

    def is_session_alive(self) -> bool:
        return bool(self.pull_token())

    def _request_json(self, path: str) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        return self.driver.execute_async_script(_GOS_FETCH_JSON_JS, path)

    def _api_endpoint(self, start: int, limit: int, query: Optional[str]) -> str:
        filters: dict[str, Any] = {}
        f = self._filters
        search_text = _safe_text(query or f.quick_search or f.registry_contains)
        digits = re.sub(r"\D+", "", search_text)
        if digits and len(digits) >= 6:
            filters["registrationNumber"] = digits
        if f.registry_contains:
            filters["registrationNumber"] = _safe_text(f.registry_contains)
        procedure_type = f.trend_pur or (f.trend_pur_values[0] if len(f.trend_pur_values) == 1 else "")
        if procedure_type:
            filters["procedureType"] = _safe_text(procedure_type)
        states = [_safe_text(item) for item in f.step_ids if _safe_text(item)]
        if states:
            filters["state"] = states
        if f.customer_contains:
            filters["customerName"] = _safe_text(f.customer_contains)
        if f.customer_agent_contains:
            filters["customerInn"] = _safe_text(f.customer_agent_contains)
        if f.organizer_contains:
            filters["organizerName"] = _safe_text(f.organizer_contains)
        if f.customer_region_contains:
            filters["region"] = _safe_text(f.customer_region_contains)
        if f.responsible_contains:
            filters["contactPerson"] = _safe_text(f.responsible_contains)
        if f.published_from:
            filters["publicationDateFrom"] = _format_date_filter(f.published_from)
        if f.published_to:
            filters["publicationDateTo"] = _format_date_filter(f.published_to)
        if f.end_from:
            filters["acceptanceApplicationsDateEndFrom"] = _format_date_filter(f.end_from)
        if f.end_to:
            filters["acceptanceApplicationsDateEndTo"] = _format_date_filter(f.end_to)
        if f.results_from:
            filters["dateSummingUpEndFrom"] = _format_date_filter(f.results_from)
        if f.results_to:
            filters["dateSummingUpEndTo"] = _format_date_filter(f.results_to)
        if f.price_min is not None:
            filters["initialSum::gte"] = f.price_min
        if f.price_max is not None:
            filters["initialSum::lte"] = f.price_max

        page = max(1, start // max(1, limit) + 1)
        sort = quote(json.dumps([{"property": "publicationDate", "direction": "DESC"}], ensure_ascii=False))
        encoded_filters = quote(json.dumps(filters, ensure_ascii=False, separators=(",", ":")))
        return (
            "/api/v1/elastica/configurations/procedures/search"
            f"?sort={sort}&filters={encoded_filters}&visibility=active"
            f"&page={page}&offset={start}&limit={limit}"
        )

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        state = _safe_text(item.get("lotState") or item.get("state"))
        status_label = _safe_text(item.get("lotStateName") or item.get("stateName")) or ROSELTORG_44_STATUS_LABELS.get(state, state)
        proc_id = item.get("id")
        lot_id = item.get("lotId")
        procedure_type = _safe_text(item.get("procedureType"))
        type_label = _safe_text(item.get("purchaseMethodETP") or item.get("purchaseMethodName")) or ROSELTORG_44_TYPE_LABELS.get(procedure_type, procedure_type)
        url = self._detail_url(proc_id, lot_id)
        return {
            **item,
            "source": self.platform_key,
            "id": proc_id,
            "procedure_id": proc_id,
            "lot_id": lot_id,
            "registry_number": item.get("registrationNumber") or "",
            "procedure_number": item.get("registrationNumber") or "",
            "title": item.get("procedureName") or "",
            "trend_pur": procedure_type,
            "trend_pur_label": type_label,
            "trend_pur_name": type_label,
            "step_id": state,
            "step_label": status_label,
            "status_label": status_label,
            "status_name": status_label,
            "short_name": item.get("organizerShortName") or item.get("customers") or "",
            "full_name": item.get("organizerFullName") or item.get("organizerShortName") or "",
            "customer_name": item.get("customers") or "",
            "date_published": item.get("publicationDate") or item.get("publicationDateExport"),
            "date_start_registration": item.get("publicationDate") or item.get("publicationDateExport"),
            "date_end_registration": item.get("acceptanceApplicationsDateEnd") or item.get("acceptanceApplicationsDateEndExport"),
            "date_results": item.get("dateSummingUpEnd") or item.get("dateSummingUpEndExport"),
            "total_price": item.get("initialSum"),
            "currency_name": "RUB",
            "region_name": item.get("region") or "",
            "responsible_name": item.get("contactPerson") or "",
            "url": url,
            "card_url": url,
            "tags": [type_label, status_label],
        }

    def fetch_page(
        self,
        start: int = 0,
        limit: int = HARD_SERVER_LIMIT,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        query: Optional[str] = None,
        tag_id: Optional[int] = None,
        sort: str = "id",
        direction: str = "DESC",
        _recover_attempt: int = 0,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        endpoint = self._api_endpoint(start=start, limit=limit, query=query)
        request_debug = {
            "platform": self.platform_key,
            "method": "GET",
            "url": endpoint,
            "body": None,
            "endpoint": endpoint,
        }
        try:
            result = self._request_json(endpoint)
        except Exception as exc:
            if self._is_window_lost(exc) and _recover_attempt < 2:
                if self._recover_tab():
                    self._token = ""
                    self.pull_token()
                    return self.fetch_page(
                        start=start,
                        limit=limit,
                        date_from=date_from,
                        date_to=date_to,
                        query=query,
                        tag_id=tag_id,
                        sort=sort,
                        direction=direction,
                        _recover_attempt=_recover_attempt + 1,
                    )
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": None, "_debug": request_debug}
        if not isinstance(result, dict):
            return {"success": False, "error": "no_response", "procedures": [], "totalCount": None, "_debug": request_debug}
        if result.get("no_session"):
            return {
                "success": False,
                "no_session": True,
                "message": result.get("message") or "Нет активной сессии Росэлторг 44-ФЗ.",
                "procedures": [],
                "totalCount": None,
                "_debug": {**request_debug, "raw_response": result},
            }
        if not result.get("ok"):
            return {
                "success": False,
                "error": result.get("error") or result.get("text") or f"HTTP {result.get('status')}",
                "procedures": [],
                "totalCount": None,
                "_debug": {**request_debug, "raw_response": result},
            }
        data = result.get("data") or {}
        items = data.get("data") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        total = data.get("count") if isinstance(data, dict) else len(items)
        return {
            "success": True,
            "procedures": [self._normalize_item(item) for item in items if isinstance(item, dict)],
            "totalCount": int(total or len(items)),
            "_debug": {**request_debug, "raw_response": result},
        }

    def _procedure_info(self, proc: dict[str, Any]) -> dict[str, Any]:
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id.")
        result = self._request_json(f"/api/v1/procedure/{proc_id}/info")
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Не удалось получить карточку процедуры: {result}")
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def _document_links_from_info(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        docs = info.get("DocumentsList") if isinstance(info.get("DocumentsList"), dict) else {}
        links: list[dict[str, Any]] = []
        notice_print = docs.get("noticePrintForm") if isinstance(docs.get("noticePrintForm"), dict) else {}
        if notice_print.get("url"):
            links.append({"href": notice_print.get("url"), "text": "Печатная форма извещения.html", "name": "Печатная форма извещения.html"})
        for item in docs.get("noticeAttachments") or []:
            if not isinstance(item, dict):
                continue
            href = _safe_text(item.get("url"))
            if href:
                name = _safe_text(item.get("name") or item.get("docDescription") or href.rsplit("/", 1)[-1])
                links.append({"href": href, "text": name, "name": name})
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in links:
            href = _safe_text(link.get("href"))
            if href and href not in seen:
                seen.add(href)
                deduped.append(link)
        return deduped

    def download_document_link(self, link: dict[str, Any], output_dir: Path, index: int = 1) -> Path:
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
        if "gos.roseltorg.ru" in href:
            assert self.driver is not None, "Сначала вызовите connect()"
            res = self.driver.execute_async_script(_GOS_DOWNLOAD_JS, href)
            if not isinstance(res, dict) or not res.get("ok"):
                raise RuntimeError(f"Ошибка скачивания {name}: {res}")
            data_url = _safe_text(res.get("dataUrl"))
            if "," not in data_url:
                raise RuntimeError(f"Пустой ответ при скачивании {name}")
            raw = base64.b64decode(data_url.split(",", 1)[1])
        else:
            req = Request(href, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
            with urlopen(req, timeout=120) as response:
                raw = response.read()
        if not raw:
            raise RuntimeError(f"Пустой файл {name}")
        target.write_bytes(raw)
        return target

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id") or "roseltorg_44")
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry)
        out_dir.mkdir(parents=True, exist_ok=True)
        url = proc.get("card_url") or proc.get("url") or self._detail_url(proc.get("id"), proc.get("lot_id"))
        if progress:
            progress(f"Открываю карточку {registry}: {url}")
        self.driver.get(str(url))
        info = self._procedure_info(proc)
        links = self._document_links_from_info(info)
        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            try:
                if progress:
                    progress(f"Скачиваю {registry}: {link.get('text') or index}")
                saved.append(str(self.download_document_link(link, out_dir, index=index)))
            except Exception as exc:
                errors.append(f"{link.get('text') or index}: {exc}")
        return {"procedure": registry, "url": str(url), "folder": str(out_dir), "found": len(links), "saved": saved, "errors": errors}

    def extract_procedure_card_text(
        self,
        proc: dict[str, Any],
        progress: Optional[Callable[[str], None]] = None,
        max_page_chars: int = 280_000,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        url = proc.get("card_url") or proc.get("url") or self._detail_url(proc.get("id"), proc.get("lot_id"))
        if progress:
            progress(f"Открываю карточку: {url}")
        self.driver.get(str(url))
        info = self._procedure_info(proc)
        links = self._document_links_from_info(info)
        try:
            page_text = self.driver.execute_script("return String(document.body && document.body.innerText || '')") or ""
        except Exception:
            page_text = ""
        structured = json.dumps(info, ensure_ascii=False, indent=2, default=str)
        text = (structured + "\n\n" + _safe_text(page_text))[:max_page_chars]
        return {"url": str(url), "page_text": text, "doc_links": links}
