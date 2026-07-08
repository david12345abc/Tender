from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urljoin

from desktop_app.params import ClientFilters
from etp_client import EtpClient, HARD_SERVER_LIMIT


ROSELTORG_URL = "https://com.roseltorg.ru/#com/procedure/index"
ROSELTORG_HOST = "com.roseltorg.ru"

ROSELTORG_PROCEDURE_TYPE_OPTIONS = [
    ("Аукцион на повышение", "1"),
    ("Аукцион", "2"),
    ("Аукцион на понижение", "3"),
    ("Запрос предложений", "4"),
    ("Запрос котировок", "5"),
    ("Конкурс", "6"),
    ("Запрос цен", "7"),
    ("Продажа посредством публичного предложения", "21"),
]

ROSELTORG_SEARCH_BY_OPTIONS: list[tuple[str, str]] = []

ROSELTORG_STATUS_OPTIONS = [
    ("Не подписан", "0"),
    ("Не опубликован", "1"),
    ("Прием заявок", "2"),
    ("Вскрытие конвертов", "3"),
    ("Рассмотрение заявок", "4"),
    ("Торги", "5"),
    ("Подведение итогов", "6"),
    ("Заключение договора", "7"),
    ("Архив", "8"),
    ("Приостановлен", "9"),
    ("Отменен", "10"),
    ("Преддоговорные переговоры", "12"),
    ("Подача окончательных ценовых предложений", "14"),
    ("Рассмотрение вторых частей заявок", "15"),
    ("Заключение дополнительного соглашения", "17"),
    ("Ожидает приема заявок", "18"),
]

ROSELTORG_STATUS_LABELS = {value: label for label, value in ROSELTORG_STATUS_OPTIONS}
ROSELTORG_STATUS_LABELS.update({"20": "Ожидает приема заявок", "21": "В ожидании подтверждения отмены в ЕИС"})


_DIRECT_RPC_JS = r"""
const callback = arguments[arguments.length - 1];
const action = arguments[0];
const method = arguments[1];
const payload = arguments[2] || {};
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 30000);
  try {
    const token = (window.Main && (window.Main.requestToken || window.Main.token)) || '';
    if (!token) {
      callback({ success: false, no_session: true, message: 'Нет активной сессии Росэлторга.' });
      return;
    }
    const body = {
      action,
      method,
      data: [payload],
      type: 'rpc',
      tid: payload.__tid || 1,
      token,
    };
    delete body.data[0].__tid;
    const resp = await fetch(`/index.php?rpctype=direct&module=default&action=${action}.${method}`, {
      method: 'POST',
      credentials: 'include',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify(body),
    });
    clearTimeout(to);
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    const text = await resp.text();
    const preview = text.slice(0, 200).trim();
    if (!contentType.includes('application/json') || preview.startsWith('<')) {
      callback({
        success: false,
        no_session: resp.status === 401 || resp.status === 403 || preview.startsWith('<'),
        status: resp.status,
        contentType,
        preview,
      });
      return;
    }
    const decoded = JSON.parse(text);
    const event = Array.isArray(decoded) ? decoded[0] : decoded;
    const result = (event && event.result) || {};
    callback({
      success: result.success !== false,
      result,
      status: resp.status,
      usedToken: token ? `${token.slice(0, 10)}...` : '',
    });
  } catch (e) {
    clearTimeout(to);
    callback({ success: false, error: String(e) });
  }
})();
"""

_CURRENT_USER_JS = r"""
try {
  const raw = localStorage.getItem('ssw') || '{}';
  const data = JSON.parse(raw);
  if (data.role && data.role !== 'user') return null;
  const user = (window.Main && window.Main.user) || {};
  const names = [user.lastname || user.surname, user.firstname || user.name, user.patronymic].filter(Boolean);
  return names.join(' ') || data.login || null;
} catch (e) {
  return null;
}
"""

_SESSION_ALIVE_JS = r"""
try {
  const token = (window.Main && (window.Main.requestToken || window.Main.token)) || '';
  const raw = localStorage.getItem('ssw') || '{}';
  const role = JSON.parse(raw).role || '';
  return Boolean(token && role !== 'guest');
} catch (e) {
  return false;
}
"""

_CARD_TEXT_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let i = 0; i < 60; i++) {
    const text = String(document.body && document.body.innerText || '');
    if ((text.includes('Сведения о процедуре') || text.includes('Извещение о проведении')) && text.length > 500) break;
    await wait(250);
  }
  const links = [];
  const seen = new Set();
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = a.href || '';
    const text = String(a.innerText || a.textContent || '').trim();
    if (!href || seen.has(href)) continue;
    if (href.includes('/file/get/') || /\.(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml)(?:[?#]|$)/i.test(href)) {
      seen.add(href);
      links.push({ href, text });
    }
  }
  callback({
    ok: true,
    url: location.href,
    pageText: String(document.body && document.body.innerText || '').trim(),
    docLinks: links,
  });
})();
"""

_DOWNLOAD_URL_JS = r"""
const callback = arguments[arguments.length - 1];
const href = arguments[0];
(async () => {
  try {
    const resp = await fetch(href, { credentials: 'include' });
    const blob = await resp.blob();
    const reader = new FileReader();
    reader.onloadend = () => callback({
      ok: resp.ok,
      status: resp.status,
      dataUrl: reader.result,
      contentType: resp.headers.get('content-type') || '',
      disposition: resp.headers.get('content-disposition') || '',
    });
    reader.onerror = () => callback({ ok: false, error: 'Не удалось прочитать файл.' });
    reader.readAsDataURL(blob);
  } catch (e) {
    callback({ ok: false, error: String(e) });
  }
})();
"""


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_filename(name: str, default: str = "file") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", _safe_text(name)).strip(" ._")
    return (cleaned or default)[:180]


def _date_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _safe_text(value.get("date") or value.get("value") or value.get("formatted"))
    return _safe_text(value)


class RoseltorgClient(EtpClient):
    """Клиент секции Росэлторг — Коммерческие закупки (`com.roseltorg.ru`)."""

    platform_key = "roseltorg"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = ROSELTORG_URL
        self.target_host = ROSELTORG_HOST
        self._filters = ClientFilters()

    def set_client_filters(self, filters: ClientFilters) -> None:
        self._filters = filters

    def _detail_url(self, proc_id: Any) -> str:
        return f"https://{ROSELTORG_HOST}/#com/procedure/view/procedure/{proc_id}"

    def current_user_login(self) -> Optional[str]:
        if not self.driver:
            return None
        try:
            value = self.driver.execute_script(_CURRENT_USER_JS)
            return str(value) if value else None
        except Exception:
            return None

    def is_session_alive(self) -> bool:
        if not self.driver:
            return False
        try:
            return bool(self.driver.execute_script(_SESSION_ALIVE_JS))
        except Exception:
            return False

    def _status_values(self) -> list[int]:
        values: list[int] = []
        for raw in getattr(self._filters, "step_ids", ()) or ():
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                values.append(int(text))
            except ValueError:
                for value, label in ROSELTORG_STATUS_LABELS.items():
                    if label.casefold() == text.casefold():
                        values.append(int(value))
                        break
        return values

    def _build_payload(
        self,
        start: int,
        limit: int,
        query: Optional[str],
        status: Optional[int] = None,
    ) -> dict[str, Any]:
        f = self._filters
        payload: dict[str, Any] = {
            "start": max(0, int(start or 0)),
            "limit": max(1, min(int(limit or HARD_SERVER_LIMIT), HARD_SERVER_LIMIT)),
            "sort": "id",
            "dir": "DESC",
        }
        search_text = _safe_text(
            query
            or f.quick_search
            or f.title_contains
            or f.organizer_contains
        )
        registry = _safe_text(f.registry_contains or f.unique_number_contains)
        if registry:
            payload["registry_number"] = registry
        elif search_text:
            payload["query"] = search_text
        if f.organizer_contains and not search_text:
            payload["full_name"] = _safe_text(f.organizer_contains)
        if f.trend_pur:
            try:
                payload["procedure_type"] = int(str(f.trend_pur).strip())
            except ValueError:
                payload["procedure_type"] = str(f.trend_pur).strip()
        if status is not None:
            payload["status"] = status
        if f.price_min is not None:
            payload["price_from"] = f.price_min
        if f.price_max is not None:
            payload["price_to"] = f.price_max
        if f.published_from:
            payload["date_published_from"] = f.published_from.strftime("%d.%m.%Y")
        if f.published_to:
            payload["date_published_to"] = f.published_to.strftime("%d.%m.%Y")
        if f.end_from:
            payload["date_end_registration_from"] = f.end_from.strftime("%d.%m.%Y")
        if f.end_to:
            payload["date_end_registration_to"] = f.end_to.strftime("%d.%m.%Y")
        if f.results_from:
            payload["date_fulfilled_from"] = f.results_from.strftime("%d.%m.%Y")
        if f.results_to:
            payload["date_fulfilled_to"] = f.results_to.strftime("%d.%m.%Y")
        return payload

    def _call_rpc(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        result = self.driver.execute_async_script(_DIRECT_RPC_JS, "Procedure", method, payload)
        return result if isinstance(result, dict) else {"success": False, "error": "no_response", "raw": result}

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        lots = item.get("lots") if isinstance(item.get("lots"), list) else []
        lot0 = lots[0] if lots and isinstance(lots[0], dict) else {}
        proc_id = item.get("id") or lot0.get("procedure_id")
        status_code = str(lot0.get("status") or item.get("stage") or "")
        status_label = ROSELTORG_STATUS_LABELS.get(status_code, status_code)
        type_label = _safe_text(item.get("procedure_type_custom_name") or item.get("procedure_type") or lot0.get("procedure_type"))
        title = _safe_text(item.get("title") or lot0.get("subject"))
        customers = lot0.get("customers") if isinstance(lot0.get("customers"), list) else []
        return {
            **item,
            "source": self.platform_key,
            "id": proc_id,
            "procedure_id": proc_id,
            "lot_id": lot0.get("id") or lot0.get("lot_id"),
            "registry_number": item.get("registry_number") or item.get("remote_id") or "",
            "procedure_number": item.get("registry_number") or "",
            "title": title,
            "trend_pur": item.get("procedure_type"),
            "trend_pur_label": type_label,
            "trend_pur_name": type_label,
            "step_id": status_code,
            "step_label": status_label,
            "status_label": status_label,
            "short_name": item.get("full_name") or "",
            "full_name": item.get("full_name") or "",
            "customer_name": ", ".join(_safe_text(x) for x in customers if _safe_text(x)),
            "date_published": _date_value(item.get("date_published")),
            "date_start_registration": _date_value(lot0.get("date_start_registration")),
            "date_end_registration": _date_value(lot0.get("date_end_registration") or item.get("min_date_end_reg")),
            "date_results": _date_value(lot0.get("date_fulfilled") or lot0.get("date_end_second_parts_review")),
            "total_price": item.get("total_price") or lot0.get("start_price"),
            "currency_name": item.get("currency_name") or "RUB",
            "lots_count": len(lots) or 1,
            "positions_count": len(lot0.get("units") or lot0.get("positions") or []),
            "applics_count": lot0.get("applics_count") or item.get("applics_count"),
            "position_name": _safe_text(lot0.get("subject")),
            "url": self._detail_url(proc_id),
            "card_url": self._detail_url(proc_id),
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
        statuses = self._status_values()
        selected_statuses: list[Optional[int]] = statuses or [None]
        rows: list[dict[str, Any]] = []
        total = 0
        debug_calls: list[dict[str, Any]] = []
        seen: set[str] = set()
        for status in selected_statuses:
            payload = self._build_payload(start=start if len(selected_statuses) == 1 else 0, limit=limit, query=query, status=status)
            debug_calls.append(payload)
            try:
                result = self._call_rpc("list", payload)
            except Exception as e:
                if self._is_window_lost(e) and _recover_attempt < 2 and self._recover_tab():
                    return self.fetch_page(start, limit, date_from, date_to, query, tag_id, sort, direction, _recover_attempt + 1)
                return {
                    "success": False,
                    "error": str(e),
                    "procedures": [],
                    "totalCount": None,
                    "_debug": {"platform": self.platform_key, "body": payload, "selenium_error": str(e)},
                }
            if not result.get("success"):
                return {
                    "success": False,
                    "no_session": bool(result.get("no_session")),
                    "message": result.get("message") or "Нет активной сессии Росэлторга. Авторизуйтесь на com.roseltorg.ru.",
                    "error": result.get("error") or result.get("preview"),
                    "procedures": [],
                    "totalCount": None,
                    "_debug": {"platform": self.platform_key, "body": payload, "raw_response": result},
                }
            data = result.get("result") or {}
            items = data.get("procedures") if isinstance(data, dict) else []
            if not isinstance(items, list):
                items = []
            total += int(data.get("totalCount") or len(items)) if isinstance(data, dict) else len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                norm = self._normalize_item(item)
                key = str(norm.get("id") or norm.get("registry_number") or len(rows))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(norm)
        return {
            "success": True,
            "procedures": rows,
            "totalCount": total,
            "_debug": {"platform": self.platform_key, "method": "Procedure.list", "body": debug_calls},
        }

    def extract_procedure_card_text(
        self,
        proc: dict[str, Any],
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия карточки Росэлторга.")
        url = _safe_text(proc.get("card_url") or proc.get("url")) or self._detail_url(proc_id)
        if progress:
            progress(f"Открываю карточку Росэлторг {proc.get('registry_number') or proc_id}: {url}")
        self.driver.get(url)
        time.sleep(1.5)
        result = self.driver.execute_async_script(_CARD_TEXT_JS)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError("Не удалось прочитать карточку Росэлторга.")
        return {
            "page_text": result.get("pageText") or "",
            "doc_links": result.get("docLinks") or [],
            "url": result.get("url") or url,
        }

    def _filename_from_link(self, link: dict[str, Any], index: int) -> str:
        href = _safe_text(link.get("href"))
        text = _safe_text(link.get("text"))
        tail = unquote(href.rstrip("/").split("/")[-1]) if href else ""
        name = tail if "." in tail else text
        return _safe_filename(name, f"document_{index}")

    def download_document_link(self, link: dict[str, Any], output_dir: Path, index: int = 1) -> Path:
        assert self.driver is not None, "Сначала вызовите connect()"
        href = _safe_text(link.get("href"))
        if not href:
            raise RuntimeError("У документа нет ссылки.")
        href = urljoin(f"https://{ROSELTORG_HOST}/", href)
        result = self.driver.execute_async_script(_DOWNLOAD_URL_JS, href)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(result.get("error") if isinstance(result, dict) else "Не удалось скачать документ.")
        data_url = str(result.get("dataUrl") or "")
        if "," not in data_url:
            raise RuntimeError("Браузер не вернул содержимое документа.")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / self._filename_from_link(link, index)
        if not target.suffix:
            target = target.with_suffix(".bin")
        target.write_bytes(raw)
        return target

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        registry = _safe_text(proc.get("registry_number") or proc.get("id") or "procedure")
        title = _safe_text(proc.get("title"))
        folder = output_root / _safe_filename(f"{registry}_{title[:80]}", registry)
        card = self.extract_procedure_card_text(proc, progress=progress)
        links = [link for link in card.get("doc_links") or [] if isinstance(link, dict)]
        if not links:
            return {"success": False, "downloaded": [], "errors": ["Документы в карточке не найдены."], "folder": str(folder)}
        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            try:
                if progress:
                    progress(f"Скачиваю документ {index}/{len(links)}")
                saved.append(str(self.download_document_link(link, folder, index=index)))
            except Exception as exc:
                errors.append(f"{link.get('text') or link.get('href') or index}: {exc}")
        return {"success": bool(saved), "downloaded": saved, "errors": errors, "folder": str(folder)}
