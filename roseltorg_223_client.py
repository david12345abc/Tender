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


ROSELTORG_223_URL = "https://corp.roseltorg.ru/#procedures"

ROSELTORG_223_PROCEDURE_TYPE_OPTIONS = [
    ("Аукцион", "17"),
    ("Аукцион МСП", "3"),
    ("Закупка с полки (закупка в электронном магазине)", "shelf"),
    ("Запрос котировок (цен)", "16"),
    ("Запрос котировок МСП", "2"),
    ("Запрос о предоставлении ценовой информации", "priceinf"),
    ("Запрос предложений", "15"),
    ("Запрос предложений МСП", "1"),
    ("Запрос предложений на повышение", "20"),
    ("Конкурентные переговоры", "competitiveneg"),
    ("Конкурс", "18"),
    ("Конкурс МСП", "4"),
    ("Котировочная сессия", "quotationsession"),
    ("Предварительный отбор", "19"),
]

ROSELTORG_223_STATUS_OPTIONS = [
    ("Вскрытие конвертов", "LotOpeningEnvelopes"),
    ("Вскрытие конвертов заявок без цп", "LotAdvancedPreQualificationOpeningEnvelopes"),
    ("Вскрытие конвертов по переторжке", "LotRebiddingFirstPartsReview"),
    ("Закрыт", "LotClosed"),
    ("Запрет заключение договора по требованию контрольных органов", "SuspendConclusionContract"),
    ("Обсуждение функциональных характеристик", "LotFeaturesDiscuss"),
    ("Ожидание ОЦП", "LotWaitFinalPriceProposals"),
    ("Ожидание переторжки", "LotWaitRebidding"),
    ("Ожидание приема заявок", "LotWaitAcceptApplications"),
    ("Ожидание приема заявок без цп", "LotWaitAdvancedPreQualificationAcceptApplications"),
    ("Ожидание приема квалификационных заявок", "LotWaitPreQualificationAcceptApplications"),
    ("Ожидание размещения предложений", "LotWaitApplicationsDistribution"),
    ("Ожидание рассмотрения вскрытия конвертов", "LotWaitOpeningEnvelopes"),
    ("Ожидание рассмотрения первых частей", "LotWaitFirstPartsReview"),
    ("Ожидание торгов", "LotWaitAuction"),
    ("Ожидание уторговывания", "LotWaitBargain"),
    ("Отбор предложений", "LotApplicationsSelection"),
    ("Переторжка", "LotRebidding"),
    ("Планирование переторжки", "LotRebiddingCreatePlanning"),
    ("Подведение итогов", "LotSummingUp"),
    ("Подведение итогов квал отбора", "LotPreQualificationSummingUp"),
    ("Подведение итогов с рассмотрением заявок", "LotTransitToSummingUp"),
    ("Подготовка торгов", "LotAuctionPlanning"),
    ("Подтверждение ЦП", "LotPostQualification"),
    ("Подтверждение ЦП уторговывания", "LotPostBargain"),
    ("Прием заявок", "LotAcceptApplications"),
    ("Прием заявок без цп", "LotAdvancedPreQualificationAcceptApplications"),
    ("Прием заявок окончен", "LotEndAcceptApplications"),
    ("Прием квалификационных заявок", "LotPreQualificationAcceptApplications"),
    ("Приостановка по требованию контрольных органов", "FasStopedLot"),
    ("Проведение ОЦП", "LotFinalPriceProposals"),
    ("Рассмотрение вторых частей", "LotSecondPartsReview"),
    ("Рассмотрение единственной заявки", "LotReviewSingleApplication"),
    ("Рассмотрение заявок", "LotSecondPartsReviewApplication"),
    ("Рассмотрение заявок без цп", "LotAdvancedPreQualificationSecondPartsReviewApplication"),
    ("Рассмотрение и оценка окончательных предложений", "LotFinalOffersExam"),
    ("Рассмотрение первых частей", "LotFirstPartsReview"),
    ("Редактирование извещения", "LotProcedurePublication"),
    ("Торги", "LotAuction"),
    ("Уторговывание", "LotBargaining"),
]

ROSELTORG_223_STATUS_LABELS = {value: label for label, value in ROSELTORG_223_STATUS_OPTIONS}
ROSELTORG_223_TYPE_LABELS = {value: label for label, value in ROSELTORG_223_PROCEDURE_TYPE_OPTIONS}


_CORP_FETCH_JSON_JS = r"""
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
    callback({ok: false, no_session: true, message: 'Нет активной сессии Росэлторг 223-ФЗ. Выполните авторизацию в Chromium.'});
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


_CORP_DOWNLOAD_JS = r"""
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


class Roseltorg223Client(EtpClient):
    """Клиент секции Росэлторг 223-ФЗ (`https://corp.roseltorg.ru`)."""

    platform_key = "roseltorg_223"
    target_host_name = "corp.roseltorg.ru"
    procedures_config = "corp-procedures"
    lots_config = "corp-lots"
    session_message = "Нет активной сессии Росэлторг 223-ФЗ. Выполните авторизацию в Chromium."

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = ROSELTORG_223_URL
        self.target_host = self.target_host_name
        self._filters = ClientFilters()
        self._lot_id_cache: dict[str, Any] = {}

    def set_client_filters(self, filters: ClientFilters) -> None:
        self._filters = filters

    def _detail_url(self, proc_id: Any, lot_id: Any = None) -> str:
        if lot_id:
            return f"https://{self.target_host_name}/#msp_lotinfo/{proc_id}/{lot_id}"
        return f"https://{self.target_host_name}/#msp_lotinfo/{proc_id}"

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
        return self.driver.execute_async_script(_CORP_FETCH_JSON_JS, path)

    def _api_endpoint(self, start: int, limit: int, query: Optional[str]) -> str:
        filters: dict[str, Any] = {}
        f = self._filters
        search_text = _safe_text(query or f.quick_search or f.registry_contains)
        digits = re.sub(r"\D+", "", search_text)
        if search_text:
            if digits and len(digits) <= 8:
                filters["id"] = digits
            else:
                filters["registrationNumber"] = search_text
        if f.registry_contains:
            filters["registrationNumber"] = _safe_text(f.registry_contains)
        procedure_type = f.trend_pur or (f.trend_pur_values[0] if len(f.trend_pur_values) == 1 else "")
        if procedure_type:
            filters["templateDescriptionBrief"] = [_safe_text(procedure_type)]
        states = [_safe_text(item) for item in f.step_ids if _safe_text(item)]
        if states:
            filters["lotState"] = states
        if f.customer_contains:
            filters["customerName"] = _safe_text(f.customer_contains)
        if f.customer_agent_contains:
            filters["customerInn"] = _safe_text(f.customer_agent_contains)
        if f.organizer_contains:
            filters["organizerName"] = _safe_text(f.organizer_contains)
        if f.responsible_contains:
            filters["contactPerson"] = _safe_text(f.responsible_contains)
        if f.published_from:
            filters["firstPublicationDate::gte"] = _format_date_filter(f.published_from)
        if f.published_to:
            filters["firstPublicationDate::lte"] = _format_date_filter(f.published_to)
        if f.end_from:
            filters["acceptanceApplicationsDateEnd::gte"] = _format_date_filter(f.end_from)
        if f.end_to:
            filters["acceptanceApplicationsDateEnd::lte"] = _format_date_filter(f.end_to)
        if f.price_min is not None:
            filters["initialSum::gte"] = f.price_min
        if f.price_max is not None:
            filters["initialSum::lte"] = f.price_max

        page = max(1, start // max(1, limit) + 1)
        encoded_filters = quote(json.dumps(filters, ensure_ascii=False, separators=(",", ":")))
        return (
            f"/api/v1/elastica/configurations/{self.procedures_config}/search"
            f"?visibility=all&subVisibility=active&filters={encoded_filters}"
            f"&page={page}&offset={start}&limit={limit}"
        )

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        proc_id = item.get("id")
        lot_id = item.get("lotId")
        if not lot_id:
            lot_id = item.get("lot_id") or self._lot_id_for_procedure(proc_id)
        type_code = _safe_text(item.get("templateDescriptionBrief") or item.get("procedureType"))
        type_label = _safe_text(item.get("templateDescription")) or ROSELTORG_223_TYPE_LABELS.get(type_code, type_code)
        status_label = _safe_text(item.get("state") or item.get("lotStateName"))
        state = _safe_text(item.get("lotState") or status_label)
        return {
            **item,
            "source": self.platform_key,
            "id": proc_id,
            "procedure_id": proc_id,
            "lot_id": lot_id,
            "registry_number": item.get("registrationNumber") or "",
            "procedure_number": item.get("registrationNumber") or "",
            "title": item.get("procedureName") or "",
            "trend_pur": type_code,
            "trend_pur_label": type_label,
            "trend_pur_name": type_label,
            "step_id": state,
            "step_label": status_label,
            "status_label": status_label,
            "status_name": status_label,
            "short_name": item.get("organizerName") or "",
            "full_name": item.get("organizerName") or "",
            "date_published": item.get("firstPublicationDate"),
            "date_start_registration": item.get("firstPublicationDate"),
            "date_end_registration": item.get("acceptanceApplicationsEndView"),
            "total_price": item.get("initialSum"),
            "currency_name": "RUB",
            "responsible_name": item.get("contactPerson") or "",
            "lots_count": item.get("lotCount") or 1,
            "url": self._detail_url(proc_id, lot_id),
            "card_url": self._detail_url(proc_id, lot_id),
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
        request_debug = {"platform": self.platform_key, "method": "GET", "url": endpoint, "endpoint": endpoint, "body": None}
        try:
            result = self._request_json(endpoint)
        except Exception as exc:
            if self._is_window_lost(exc) and _recover_attempt < 2 and self._recover_tab():
                self._token = ""
                self.pull_token()
                return self.fetch_page(start, limit, date_from, date_to, query, tag_id, sort, direction, _recover_attempt + 1)
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": None, "_debug": request_debug}
        if not isinstance(result, dict):
            return {"success": False, "error": "no_response", "procedures": [], "totalCount": None, "_debug": request_debug}
        if result.get("no_session"):
            return {
                "success": False,
                "no_session": True,
                "message": result.get("message") or self.session_message,
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

    def _lot_id_for_procedure(self, proc_id: Any) -> Any:
        key = _safe_text(proc_id)
        if not key:
            return None
        if key in self._lot_id_cache:
            return self._lot_id_cache[key]
        encoded = quote(json.dumps({"procedureId": proc_id}, ensure_ascii=False, separators=(",", ":")))
        result = self._request_json(
            f"/api/v1/elastica/configurations/{self.lots_config}/search?filters={encoded}&page=1&offset=0&limit=25"
        )
        data = result.get("data") if isinstance(result, dict) else {}
        rows = data.get("data") if isinstance(data, dict) else []
        lot_id = None
        if isinstance(rows, list) and rows:
            first = rows[0]
            if isinstance(first, dict):
                lot_id = first.get("id")
        self._lot_id_cache[key] = lot_id
        return lot_id

    def _procedure_info(self, proc: dict[str, Any]) -> dict[str, Any]:
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id.")
        result = self._request_json(f"/api/v1/procedures/{proc_id}")
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Не удалось получить карточку процедуры: {result}")
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    def _ensure_lot_id(self, proc: dict[str, Any]) -> Any:
        lot_id = proc.get("lot_id") or proc.get("lotId")
        if lot_id:
            return lot_id
        proc_id = proc.get("id") or proc.get("procedure_id")
        return self._lot_id_for_procedure(proc_id)

    def _document_links_from_info(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        docs = info.get("DocumentsList")
        if isinstance(docs, list):
            for item in docs:
                if not isinstance(item, dict):
                    continue
                href = _safe_text(item.get("url"))
                if href:
                    name = _safe_text(item.get("name") or href.rsplit("/", 1)[-1])
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
        if href.startswith("/"):
            href = f"https://{self.target_host_name}{href}"
        output_dir.mkdir(parents=True, exist_ok=True)
        name = _safe_filename((link or {}).get("name") or (link or {}).get("text") or f"document_{index}", f"document_{index}")
        target = output_dir / name
        stem, suffix = target.stem, target.suffix
        n = 2
        while target.exists():
            target = output_dir / f"{stem}_{n}{suffix}"
            n += 1
        if self.target_host_name in href:
            assert self.driver is not None, "Сначала вызовите connect()"
            res = self.driver.execute_async_script(_CORP_DOWNLOAD_JS, href)
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
        lot_id = self._ensure_lot_id(proc)
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id") or "roseltorg_223")
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry)
        out_dir.mkdir(parents=True, exist_ok=True)
        url = proc.get("card_url") or proc.get("url") or self._detail_url(proc.get("id"), lot_id)
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
        lot_id = self._ensure_lot_id(proc)
        url = proc.get("card_url") or proc.get("url") or self._detail_url(proc.get("id"), lot_id)
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
