from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode

from etp_client import EtpClient, HARD_SERVER_LIMIT
from desktop_app.params import ClientFilters


ROSELTORG_URL = (
    "https://business.roseltorg.ru/lk/orders/all?"
    "%22searchBy%22=%22procedures%22&%22searchByOrderType%22=%22buy%22&"
    "%22page%22=1&%22limit%22=30&%22sortProperty%22=%22datePublication%22&"
    "%22sortDirection%22=%22DESC%22"
)

ROSELTORG_PROCEDURE_TYPE_OPTIONS = [
    ("Закупка", "buy"),
    ("Продажа", "sale"),
]

ROSELTORG_SEARCH_BY_OPTIONS = [
    ("Процедуры", "procedures"),
    ("Лоты / позиции", "lots"),
]

ROSELTORG_STATUS_OPTIONS = [
    ("Прием заявок на допуск", "ProcedureAcceptanceAdmissionRequests"),
    ("Проверка заявок на допуск", "ProcedureVerificationAdmissionRequests"),
    ("Приём предложений", "Published"),
    ("Подведение итогов", "ReviewOffers"),
    ("В архиве", "procedureArchive"),
    ("Процедура отменена", "procedureCancelled"),
]

ROSELTORG_STATUS_LABELS = {
    "Published": "Приём предложений",
    "ReviewOffers": "Подведение итогов",
    "procedureArchive": "В архиве",
    "ProcedureArchive": "В архиве",
    "Archived": "В архиве",
    "procedureCancelled": "Процедура отменена",
    "ProcedureCancelled": "Процедура отменена",
    "Canceled": "Процедура отменена",
    "Cancelled": "Процедура отменена",
    "ProcedureAcceptanceAdmissionRequests": "Прием заявок на допуск",
    "ProcedureVerificationAdmissionRequests": "Проверка заявок на допуск",
}


_FETCH_PROCEDURES_JS = r"""
const callback = arguments[arguments.length - 1];
const endpoint = arguments[0];
(async () => {
  let token = '';
  try {
    const raw = localStorage.getItem('elk_token') || '';
    token = raw ? JSON.parse(raw) : '';
  } catch (e) {
    token = localStorage.getItem('elk_token') || '';
  }
  if (!token) {
    callback({ no_session: true, message: 'Нет активной сессии Росэлторга.' });
    return;
  }
  try {
    const resp = await fetch(endpoint, {
      method: 'GET',
      credentials: 'include',
      headers: {
        'Accept': 'application/json, text/plain, */*',
        'Authorization': `Bearer ${token}`,
      },
    });
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) {}
    callback({
      ok: resp.ok,
      status: resp.status,
      data,
      text: data ? '' : text.slice(0, 2000),
      no_session: resp.status === 401 || resp.status === 403,
    });
  } catch (e) {
    callback({ ok: false, error: String(e) });
  }
})();
"""


_CURRENT_USER_JS = r"""
try {
  const raw = localStorage.getItem('elk_token') || '';
  const token = raw ? JSON.parse(raw) : '';
  const payload = JSON.parse(decodeURIComponent(escape(atob(token.split('.')[1] || ''))));
  const user = payload.user || {};
  return [user.surname, user.name, user.patronymic].filter(Boolean).join(' ') || null;
} catch (e) {
  return null;
}
"""


class RoseltorgClient(EtpClient):
    """Клиент Росэлторг.Бизнес через авторизованную вкладку браузера."""

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = ROSELTORG_URL
        self.target_host = "business.roseltorg.ru"
        self._filters = ClientFilters()

    def set_client_filters(self, filters: ClientFilters) -> None:
        self._filters = filters

    def _detail_url(self, proc_id: Any) -> str:
        return f"https://business.roseltorg.ru/lk/orders/all/{proc_id}"

    def current_user_login(self) -> Optional[str]:
        if not self.driver:
            return None
        try:
            value = self.driver.execute_script(_CURRENT_USER_JS)
            return str(value) if value else None
        except Exception:
            return None

    def pull_token(self) -> str:
        if not self.driver:
            return ""
        try:
            token = self.driver.execute_script(
                """
                try {
                  const raw = localStorage.getItem('elk_token') || '';
                  return raw ? JSON.parse(raw) : '';
                } catch (e) {
                  return localStorage.getItem('elk_token') || '';
                }
                """
            ) or ""
        except Exception:
            token = ""
        self._token = str(token or "")
        return self._token

    def is_session_alive(self) -> bool:
        return bool(self.pull_token())

    def _api_endpoint(self, start: int, limit: int, query: Optional[str]) -> str:
        f = self._filters
        page = max(1, start // max(1, limit) + 1)
        search_by = f.purchase_form if f.purchase_form in {"procedures", "lots"} else "procedures"
        order_type = f.trend_pur if f.trend_pur in {"buy", "sale"} else "buy"
        template_briefs = ("10", "12") if order_type == "sale" else ("8", "9", "11")
        params: list[tuple[str, Any]] = [
            ("searchBy", search_by),
            ("searchByOrderType", order_type),
            ("page", page),
            ("limit", limit),
            ("visibility", "show-all"),
            ("offset", start),
            ("sort", json.dumps([{"property": "datePublication", "direction": "DESC"}], ensure_ascii=False)),
        ]
        for idx, template in enumerate(template_briefs):
            params.append((f"templateBrief[{idx}]", template))
        search_text = query or f.quick_search or f.registry_contains or f.title_contains
        if search_text:
            params.append(("query", str(search_text).strip()))
        if f.organizer_contains:
            params.append(("organizer", str(f.organizer_contains).strip()))
        status_param = "procedureStates" if search_by == "procedures" else "states"
        for idx, state in enumerate(str(item).strip() for item in f.step_ids if str(item).strip()):
            params.append((f"{status_param}[{idx}]", state))
        if f.published_from:
            params.append(("datePublicationFrom", f.published_from.strftime("%Y-%m-%d")))
        if f.published_to:
            params.append(("datePublicationTo", f.published_to.strftime("%Y-%m-%d")))
        if f.end_from:
            params.append(("dateAcceptanceApplicationsEndFrom", f.end_from.strftime("%Y-%m-%d")))
        if f.end_to:
            params.append(("dateAcceptanceApplicationsEndTo", f.end_to.strftime("%Y-%m-%d")))
        if f.results_from:
            params.append(("dateSummingUpFrom", f.results_from.strftime("%Y-%m-%d")))
        if f.results_to:
            params.append(("dateSummingUpTo", f.results_to.strftime("%Y-%m-%d")))
        if f.price_min is not None:
            params.append(("sumFrom", f.price_min))
        if f.price_max is not None:
            params.append(("sumTo", f.price_max))
        if f.applics_min is not None:
            params.append(("appCountFrom", f.applics_min))
        if f.applics_max is not None:
            params.append(("appCountTo", f.applics_max))
        return "/api/v1/procedures?" + urlencode(params)

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        lots = item.get("LotsList") if isinstance(item.get("LotsList"), list) else []
        regions = item.get("region") if isinstance(item.get("region"), list) else []
        region_names = [str(r.get("name")) for r in regions if isinstance(r, dict) and r.get("name")]
        lot_items: list[str] = []
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            for pos in lot.get("items") or []:
                if isinstance(pos, dict) and pos.get("name"):
                    lot_items.append(str(pos["name"]))
        state = str(item.get("state") or "")
        status_label = ROSELTORG_STATUS_LABELS.get(state, state)
        return {
            **item,
            "source": "roseltorg",
            "registry_number": item.get("number") or "",
            "procedure_number": item.get("number") or "",
            "title": item.get("name") or item.get("description") or "",
            "trend_pur_label": "Закупка",
            "trend_pur_name": "Закупка",
            "step_id": state,
            "step_label": status_label,
            "status_label": status_label,
            "short_name": org.get("shortName") or org.get("fullName") or "",
            "full_name": org.get("fullName") or org.get("shortName") or "",
            "org_inn": org.get("inn") or "",
            "org_kpp": org.get("kpp") or "",
            "date_published": item.get("createdAt"),
            "date_start_registration": item.get("createdAt"),
            "date_end_registration": item.get("replyUntil"),
            "date_results": item.get("acceptAt"),
            "total_price": item.get("initialSum"),
            "currency_name": "RUB" if str(item.get("currency") or "") == "643" else str(item.get("currency") or ""),
            "lots_count": item.get("countActualLotItems") or len(lots) or 1,
            "positions_count": sum(len(lot.get("items") or []) for lot in lots if isinstance(lot, dict)),
            "applics_count": item.get("countActualApplications") or item.get("countSubmittedApplications"),
            "region_name": ", ".join(region_names),
            "position_name": ", ".join(lot_items),
            "url": self._detail_url(item.get("id")),
            "tags": [
                "Закупка",
                status_label,
                "Открытая процедура" if not item.get("isClosed") else "Закрытая процедура",
            ],
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
        token = self._token or self.pull_token()
        endpoint = self._api_endpoint(start=start, limit=limit, query=query)
        request_debug = {
            "platform": "roseltorg",
            "method": "GET",
            "url": endpoint,
            "headers": {
                "Accept": "application/json, text/plain, */*",
                "Authorization": f"Bearer {token}",
            },
            "body": None,
            "token": token,
            "endpoint": endpoint,
        }
        try:
            result = self.driver.execute_async_script(_FETCH_PROCEDURES_JS, endpoint)
        except Exception as e:
            if self._is_window_lost(e) and _recover_attempt < 2:
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
            return {
                "success": False,
                "error": str(e),
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "selenium_error": str(e),
                },
            }
        if not isinstance(result, dict):
            return {
                "success": False,
                "error": "no_response",
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "raw_response": result,
                },
            }
        if result.get("no_session"):
            return {
                "success": False,
                "no_session": True,
                "message": "Нет активной сессии Росэлторга.",
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "raw_response": result,
                },
            }
        if not result.get("ok"):
            return {
                "success": False,
                "error": result.get("error") or result.get("text") or f"HTTP {result.get('status')}",
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "raw_response": result,
                },
            }
        data = result.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        return {
            "success": True,
            "procedures": [self._normalize_item(item) for item in items if isinstance(item, dict)],
            "totalCount": int(data.get("totalCount") or len(items)) if isinstance(data, dict) else len(items),
            "_debug": {
                **request_debug,
                "raw_response": result,
            },
        }
