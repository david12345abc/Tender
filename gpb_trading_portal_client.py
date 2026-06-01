from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from etp_client import EtpClient


GPB_TRADING_PORTAL_ORGANIZATION_ID = "5ec50776-63f0-41ff-87a1-6cd125f38e78"
GPB_TRADING_PORTAL_URL = (
    "https://etp.gpb.ru/"
    f"?organizationId={GPB_TRADING_PORTAL_ORGANIZATION_ID}"
    "#nsi/priceorder/all/tp"
)

GPB_TRADING_PORTAL_STATUS_OPTIONS = [
    ("Черновик", "1"),
    ("На рассмотрении у поставщиков", "2"),
    ("Отклонено поставщиком", "3"),
    ("На рассмотрении у заказчика", "4"),
    ("Отменено заказчиком", "5"),
    ("На оформлении заказа", "6"),
    ("Просрочено", "7"),
    ("Исполнено", "8"),
    ("Не исполнено", "9"),
    ("Уторговывание", "10"),
    ("Ожидает АСЭЗ", "11"),
    ("Закрыт", "12"),
]

GPB_TRADING_PORTAL_TYPE_OPTIONS = [
    ("Все ценовые запросы", ""),
    ("Прямой поставщик", "directSupplier"),
]

_TP_RPC_ENDPOINT = "/index.php?rpctype=direct&module=nsi&client=etp"

_TP_FETCH_PRICE_ORDERS_JS = r"""
const callback = arguments[arguments.length - 1];
const input = arguments[0] || {};
const explicitToken = arguments[1] || '';

(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = () => String(document.body && document.body.innerText || '');

  for (let i = 0; i < 80; i++) {
    const text = textOf();
    const href = String(location.href || '');
    if (href.includes('openid-connect/auth')) {
      callback({success: false, no_session: true, message: 'Требуется авторизация в ЕЛК.', url: href});
      return;
    }
    if (!/Соединяемся с сервером/i.test(text) && (window.Ext || text.length > 500)) break;
    await wait(250);
  }

  const token = explicitToken
    || (window.Main && (window.Main.requestToken || window.Main.token))
    || '';

  function rpc(action, method, data) {
    const tid = Date.now() % 1000000;
    return fetch('/index.php?rpctype=direct&module=nsi&client=etp', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action,
        method,
        data: [data],
        type: 'rpc',
        tid,
        token,
      }),
    }).then(async (resp) => {
      const contentType = (resp.headers.get('content-type') || '').toLowerCase();
      const text = await resp.text();
      const preview = text.slice(0, 500);
      if (!contentType.includes('application/json') || preview.trim().startsWith('<')) {
        return {ok: false, httpStatus: resp.status, contentType, preview};
      }
      try {
        return {ok: resp.ok, httpStatus: resp.status, json: JSON.parse(text), preview};
      } catch (e) {
        return {ok: false, httpStatus: resp.status, contentType, preview, error: String(e)};
      }
    });
  }

  function apiActions() {
    const out = {};
    const api = (window.Ext && Ext.app && Ext.app.REMOTING_API) || window.REMOTING_API || null;
    if (api && api.actions) {
      for (const [action, methods] of Object.entries(api.actions)) {
        out[action] = (methods || []).map((m) => ({name: m.name, len: m.len, formHandler: !!m.formHandler}));
      }
    }
    return out;
  }

  function collectActionMethods(actions) {
    const pairs = [];
    const priceRe = /(price\s*order|priceorder|ценов|order)/i;
    for (const [action, methods] of Object.entries(actions || {})) {
      if (!priceRe.test(action)) continue;
      for (const method of methods || []) {
        const name = method && method.name;
        if (!name || !/(list|all|get|load|search|index)/i.test(name)) continue;
        pairs.push([action, name]);
      }
    }
    const fallbacks = [
      ['Priceorder', 'getList'],
      ['Priceorder', 'getListAll'],
      ['Priceorder', 'loadOrder'],
      ['PriceOrder', 'getList'],
      ['PriceOrder', 'getListAll'],
      ['NsiPriceOrder', 'getList'],
    ];
    for (const pair of fallbacks) {
      if (!pairs.some((x) => x[0] === pair[0] && x[1] === pair[1])) pairs.push(pair);
    }
    return pairs;
  }

  function arraysIn(value, path = 'result', depth = 0) {
    if (depth > 4 || value == null) return [];
    if (Array.isArray(value)) return [{path, value}];
    if (typeof value !== 'object') return [];
    let out = [];
    for (const [key, nested] of Object.entries(value)) {
      out = out.concat(arraysIn(nested, `${path}.${key}`, depth + 1));
    }
    return out;
  }

  const payload = Object.assign({
    sort: input.sort || 'id',
    dir: input.direction || 'DESC',
    start: input.start || 0,
    limit: input.limit || 25,
    query: input.query || '',
    search: input.query || '',
    number_like: input.registry || '',
    order_number_like: input.registry || '',
    name_like: input.title || '',
    title_like: input.title || '',
    customer: input.customer || '',
    organizer: input.organizer || '',
    status: input.status || '',
    status_name: input.status || '',
    order_status: input.status || '',
    price_order_status: input.status || '',
    type: input.type || 'tp',
    portal: 'tp',
    tp: true,
  }, input.extra || {});

  const actions = apiActions();
  const attempts = [];
  for (const [action, method] of collectActionMethods(actions)) {
    let response;
    try {
      response = await rpc(action, method, payload);
    } catch (e) {
      attempts.push({action, method, error: String(e)});
      continue;
    }
    const result = response && response.json && (response.json.result || response.json);
    const arrays = arraysIn(result);
    const bestArray = arrays
      .filter((item) => item.value.length && item.value.some((row) => row && typeof row === 'object'))
      .sort((a, b) => b.value.length - a.value.length)[0];
    attempts.push({
      action,
      method,
      ok: !!(response && response.ok),
      httpStatus: response && response.httpStatus,
      arrayPath: bestArray && bestArray.path,
      count: bestArray && bestArray.value && bestArray.value.length,
      preview: response && response.preview,
    });
    if (bestArray) {
      callback({
        success: true,
        action,
        method,
        arrayPath: bestArray.path,
        rows: bestArray.value,
        totalCount: result.totalCount ?? result.total ?? result.count ?? bestArray.value.length,
        rawResult: result,
        actions,
        attempts,
        url: location.href,
      });
      return;
    }
  }

  function parseDomRow(row, idx) {
    const text = String(row.innerText || row.textContent || '').replace(/\r/g, '').trim();
    if (!text) return null;
    const lines = text
      .split(/\n+/)
      .map((line) => line.replace(/\s+/g, ' ').trim())
      .filter(Boolean);
    const joined = lines.join(' ');
    const numberIndex = lines.findIndex((line) => /^\d{5,}$/.test(line));
    if (numberIndex < 0) return null;
    const number = lines[numberIndex];
    const published = lines[numberIndex + 1] || '';
    let customer = lines[numberIndex + 2] || '';
    let title = lines[numberIndex + 3] || '';
    let status = lines[numberIndex + 4] || '';
    let deliveryPlace = lines[numberIndex + 5] || '';
    let endDate = lines[numberIndex + 6] || '';
    let deliveryDate = lines[numberIndex + 7] || '';
    if (!/\d{2}\.\d{2}\.\d{4}/.test(published) && lines.length >= 8) {
      customer = lines[numberIndex + 1] || customer;
      title = lines[numberIndex + 2] || title;
      status = lines[numberIndex + 3] || status;
      deliveryPlace = lines[numberIndex + 4] || deliveryPlace;
      endDate = lines[numberIndex + 5] || endDate;
      deliveryDate = lines[numberIndex + 6] || deliveryDate;
    }
    const anchor = row.querySelector && row.querySelector('a[href*="priceorder"], a[href*="orderId"]');
    const href = anchor ? anchor.href : '';
    const supplierMatch = joined.match(/supplierGuid\/([0-9a-f-]{20,})/i) || (href || '').match(/supplierGuid\/([0-9a-f-]{20,})/i);
    return {
      id: number,
      orderId: number,
      orderNumber: number,
      registry_number: number,
      procedure_number: number,
      publishDate: published,
      date_published: published,
      customerName: customer,
      orderName: title,
      title,
      statusName: status,
      status_name: status,
      status_label: status,
      deliveryPlace,
      date_end_registration: endDate,
      endDate,
      deliveryDate,
      supplierGuid: supplierMatch ? supplierMatch[1] : '',
      url: href,
      raw_text: text,
      _dom_index: idx,
    };
  }

  const domRows = Array.from(document.querySelectorAll('.x-grid3-row, .x-grid-row, tr, [role="row"]'))
    .map(parseDomRow)
    .filter(Boolean);
  callback({
    success: domRows.length > 0,
    domFallback: true,
    rows: domRows,
    totalCount: domRows.length,
    actions,
    attempts,
    url: location.href,
    bodyText: textOf().slice(0, 4000),
  });
})();
"""

_TP_EXTRACT_CARD_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let i = 0; i < 100; i++) {
    const text = String(document.body && document.body.innerText || '');
    if (String(location.href || '').includes('openid-connect/auth')) {
      callback({ok: false, no_session: true, message: 'Требуется авторизация в ЕЛК.', url: location.href});
      return;
    }
    if (!/Соединяемся с сервером/i.test(text) && text.length > 300) break;
    await wait(300);
  }
  const fileRe = /([^\n\r\t<>:"|?*]+?\.(?:docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv))/i;
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const docs = new Map();
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = a.href || a.getAttribute('href') || '';
    const text = clean([a.innerText, a.textContent, a.title, a.download].filter(Boolean).join(' '));
    const parentText = clean(a.closest('tr, .x-grid3-row, .x-grid-row, .x-panel, .x-fieldset')?.innerText || '');
    const name = (text.match(fileRe) || parentText.match(fileRe) || href.match(fileRe) || [null, text || href])[1];
    if (!href || href === 'javascript:;') continue;
    if (/PERSONAL_DATA_POLICY|InstructionFiles\/code/i.test(href) || /политик[аи] обработки персональных данных/i.test(text)) continue;
    if (/\/file\/get\//i.test(href) || fileRe.test(text) || fileRe.test(parentText) || fileRe.test(href)) {
      docs.set(href, {href, text: name || href});
    }
  }
  const pageText = String(document.body && document.body.innerText || '').replace(/\r/g, '').trim();
  callback({
    ok: true,
    url: location.href,
    pageText,
    charCount: pageText.length,
    docLinks: Array.from(docs.values()),
    productRowsInfo: {},
  });
})();
"""


def _first_value(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return ""


def _flatten_text(value: Any) -> str:
    values: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for nested in v.values():
                walk(nested)
        elif isinstance(v, (list, tuple, set)):
            for nested in v:
                walk(nested)
        elif v is not None:
            values.append(str(v))

    walk(value)
    return " ".join(values)


def _normalize_status_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    for label, raw_value in GPB_TRADING_PORTAL_STATUS_OPTIONS:
        if text == raw_value:
            return label
    folded = text.casefold().replace("ё", "е")
    for label, _value in GPB_TRADING_PORTAL_STATUS_OPTIONS:
        if folded == label.casefold().replace("ё", "е"):
            return label
    return text


def _compact_price_order_for_text(proc: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "registry_number",
        "title",
        "customer",
        "short_name",
        "status_name",
        "date_sent",
        "date_created",
        "date_response",
        "date_delivery",
        "date_delivery_end",
        "date_end_registration",
        "date_published",
        "order_delivery_regions",
        "delivery_address",
        "delivery_conditions",
        "other_requirements",
        "document_requirements",
        "total_price",
        "currency_description",
        "for_small_business",
        "free_supplier_participation",
        "use_price_without_nds",
        "comment",
        "customer_comment",
        "position_count",
        "responses_count",
        "requests_count",
    )
    out = {key: proc.get(key) for key in keys if proc.get(key) not in (None, "", [], {})}
    return out


class GpbTradingPortalClient(EtpClient):
    """Клиент «Торгового портала» (`#nsi/priceorder/.../tp`).

    Это отдельная секция etp.gpb.ru: она не использует статусы/типы секции
    Газпром и не должна проходить через Газпром-специфичную перекодировку.
    """

    platform_key = "gpb_trading_portal"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = GPB_TRADING_PORTAL_URL
        self.target_host = "etp.gpb.ru"

    def _detail_url(self, proc_id: Any, supplier_guid: Any = None) -> str:
        supplier = str(supplier_guid or "").strip()
        suffix = f"/supplierGuid/{supplier}" if supplier else ""
        return (
            "https://etp.gpb.ru/"
            f"?organizationId={GPB_TRADING_PORTAL_ORGANIZATION_ID}"
            f"#nsi/priceorder/directSupplier/orderId/{proc_id}{suffix}/tp"
        )

    def _switch_to_etp_tab(self) -> bool:
        ok = super()._switch_to_etp_tab()
        if ok and self.driver is not None:
            try:
                if "priceorder" not in (self.driver.current_url or ""):
                    self.driver.get(self.target_url)
            except Exception:
                pass
        return ok

    def _normalize_row(self, raw: Any, index: int = 0) -> dict[str, Any]:
        if not isinstance(raw, dict):
            text = str(raw or "").strip()
            return {
                "id": index + 1,
                "registry_number": "",
                "title": text,
                "status_name": "",
                "source": self.platform_key,
                "raw": raw,
            }
        proc_id = _first_value(raw, ("orderId", "order_id", "price_order_id", "priceOrderId", "id"))
        supplier_guid = _first_value(raw, ("supplierGuid", "supplier_guid", "supplier", "supplier_id", "supplierId"))
        registry = _first_value(
            raw,
            (
                "registry_number",
                "procedure_number",
                "reestr_number",
                "orderNumber",
                "order_number",
                "number",
                "priceOrderNumber",
                "request_number",
            ),
        ) or str(proc_id or "")
        title = _first_value(
            raw,
            (
                "title",
                "name",
                "orderName",
                "order_name",
                "subject",
                "purchaseSubject",
                "nomenclature",
                "raw_text",
            ),
        )
        status = _normalize_status_text(
            _first_value(
                raw,
                (
                    "status_name",
                    "statusName",
                    "status_label",
                    "statusLabel",
                    "state_name",
                    "stateName",
                    "stage_name",
                    "stageName",
                    "status",
                ),
            )
        )
        proc = dict(raw)
        proc.update(
            {
                "id": proc_id or raw.get("id") or index + 1,
                "procedure_id": proc_id or raw.get("id") or index + 1,
                "supplier_guid": supplier_guid,
                "registry_number": str(registry or ""),
                "procedure_number": str(registry or ""),
                "title": str(title or _flatten_text(raw)[:500]),
                "status_name": status,
                "status_label": status,
                "step_label": status,
                "short_name": _first_value(
                    raw,
                    (
                        "customerName",
                        "customer_name",
                        "customer",
                        "organizerName",
                        "organizer_name",
                        "companyName",
                        "contragentName",
                    ),
                ),
                "full_name": _first_value(raw, ("customerFullName", "customer_full_name", "organizationName")),
                "date_published": _first_value(
                    raw,
                    ("publishDate", "date_published", "date_sent", "date_created", "created_at", "createDate"),
                ),
                "date_start_registration": _first_value(raw, ("startDate", "date_start", "requestStartDate")),
                "date_end_registration": _first_value(
                    raw,
                    ("endDate", "date_response", "date_end", "requestEndDate", "finishDate", "date_finish", "deadline"),
                ),
                "date_results": _first_value(raw, ("winnerDate", "resultDate", "date_results")),
                "total_price": _first_value(raw, ("total_price", "price", "amount", "sum", "startPrice", "maxPrice")),
                "currency_name": (
                    "RUB"
                    if str(_first_value(raw, ("currency_name", "currency", "currencyCode")) or "") == "643"
                    else (_first_value(raw, ("currency_name", "currency", "currencyCode")) or "RUB")
                ),
                "source": self.platform_key,
            }
        )
        if proc_id:
            proc["url"] = self._detail_url(proc_id, supplier_guid)
        return proc

    def pull_token(self) -> str:
        if self.driver is not None:
            try:
                current = self.driver.current_url or ""
            except Exception:
                current = ""
            if "etp.gpb.ru" not in current and "id.etpgpb.ru" not in current:
                try:
                    self.driver.get(self.target_url)
                    time.sleep(1)
                except Exception:
                    pass
        return super().pull_token()

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
        if not self._token:
            self.pull_token()
        try:
            current = self.driver.current_url or ""
        except Exception:
            current = ""
        if "priceorder" not in current and "openid-connect/auth" not in current:
            self.driver.get(self.target_url)

        registry = str(getattr(client_filters, "registry_contains", "") or "").strip() if client_filters is not None else ""
        title = str(getattr(client_filters, "title_contains", "") or "").strip() if client_filters is not None else ""
        organizer = str(getattr(client_filters, "organizer_contains", "") or "").strip() if client_filters is not None else ""
        customer = str(getattr(client_filters, "customer_contains", "") or "").strip() if client_filters is not None else ""
        step_ids = tuple(getattr(client_filters, "step_ids", ()) or ()) if client_filters is not None else ()
        status = str(step_ids[0] if len(step_ids) == 1 else "").strip()
        trend = str(getattr(client_filters, "trend_pur", "") or "").strip() if client_filters is not None else ""
        payload = {
            "start": start,
            "limit": limit,
            "query": query or registry or title or "",
            "registry": registry,
            "title": title,
            "organizer": organizer,
            "customer": customer,
            "status": status,
            "type": trend or "tp",
            "date_from": date_from,
            "date_to": date_to,
            "sort": sort,
            "direction": direction,
        }
        debug = {
            "platform": self.platform_key,
            "method": "dynamic Ext.Direct RPC",
            "url": _TP_RPC_ENDPOINT,
            "request_payload": payload,
        }
        try:
            self.driver.set_script_timeout(90)
            res = self.driver.execute_async_script(_TP_FETCH_PRICE_ORDERS_JS, payload, self._token)
        except Exception as exc:
            if self._is_window_lost(exc) and _recover_attempt < 2 and self._recover_tab():
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
                    client_filters=client_filters,
                    _recover_attempt=_recover_attempt + 1,
                )
            return {"success": False, "error": str(exc), "procedures": [], "totalCount": None, "_debug": debug}
        finally:
            try:
                self.driver.set_script_timeout(30)
            except Exception:
                pass

        if not isinstance(res, dict):
            return {
                "success": False,
                "error": "no_response",
                "procedures": [],
                "totalCount": None,
                "_debug": {**debug, "raw_response": res},
            }
        debug["raw_response"] = {
            key: value
            for key, value in res.items()
            if key not in {"rows", "rawResult", "actions"}
        }
        debug["actions"] = res.get("actions")
        if res.get("no_session") or res.get("no_access"):
            return {
                "success": False,
                "no_session": True,
                "message": res.get("message") or "Требуется авторизация в ЕЛК.",
                "procedures": [],
                "totalCount": None,
                "_debug": debug,
            }
        rows = res.get("rows") if isinstance(res.get("rows"), list) else []
        procedures: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, row in enumerate(rows):
            proc = self._normalize_row(row, index=i)
            key = str(
                proc.get("orderId")
                or proc.get("id")
                or proc.get("registry_number")
                or proc.get("raw_text")
                or i
            )
            if key in seen:
                continue
            seen.add(key)
            procedures.append(proc)
        return {
            "success": bool(res.get("success")),
            "procedures": procedures,
            "totalCount": (
                len(procedures)
                if res.get("domFallback")
                else (res.get("totalCount") if res.get("totalCount") is not None else len(procedures))
            ),
            "_debug": debug,
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
            raise RuntimeError("У ЦЗ нет orderId для открытия карточки.")
        registry = str(proc.get("registry_number") or proc.get("procedure_number") or proc_id)
        url = str(proc.get("url") or self._detail_url(proc_id, proc.get("supplier_guid")))
        if progress:
            progress(f"Читаю карточку ЦЗ {registry}: {url}")
        self.driver.get(url)
        try:
            self.driver.set_script_timeout(90)
            raw = self.driver.execute_async_script(_TP_EXTRACT_CARD_JS)
        finally:
            self.driver.set_script_timeout(30)
        if not isinstance(raw, dict) or not raw.get("ok"):
            raise RuntimeError(f"Не удалось прочитать карточку Торгового портала: {raw}")
        page_text = str(raw.get("pageText") or "").strip()
        compact = _compact_price_order_for_text(proc)
        structured_text = (
            "СТРУКТУРИРОВАННЫЕ ДАННЫЕ ЦЕНОВОГО ЗАПРОСА ИЗ ТОРГОВОГО ПОРТАЛА:\n"
            f"{json.dumps(compact, ensure_ascii=False, indent=2, default=str)}"
            if compact
            else ""
        )
        if structured_text:
            page_text = structured_text + ("\n\nТЕКСТ КАРТОЧКИ:\n" + page_text if page_text else "")
        if len(page_text) > max_page_chars:
            page_text = page_text[:max_page_chars] + "\n\n[…текст обрезан…]"
        return {
            "procedure": registry,
            "procedure_id": proc_id,
            "url": url,
            "page_text": page_text,
            "doc_links": raw.get("docLinks") if isinstance(raw.get("docLinks"), list) else [],
            "primary_doc_url": "",
            "product_rows_info": raw.get("productRowsInfo") if isinstance(raw.get("productRowsInfo"), dict) else {},
            "char_count": int(raw.get("charCount") or len(page_text)),
        }

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        card = self.extract_procedure_card_text(proc, progress=progress)
        registry = str(card.get("procedure") or proc.get("registry_number") or proc.get("id") or "tp")
        title = str(proc.get("title") or "")
        folder = output_root / self._safe_filename(f"{registry}_{title[:80]}", str(proc.get("id") or "tp"))
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        errors: list[str] = []
        links = card.get("doc_links") if isinstance(card.get("doc_links"), list) else []
        for index, link in enumerate(links, start=1):
            try:
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
