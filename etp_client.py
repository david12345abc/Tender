"""Лёгкий клиент ЭТП ГПБ для десктопного приложения.

Делает `Procedure.list` через уже авторизованный Chrome с удалённой
отладкой (DevTools на порту 9222). Не умеет логиниться — предполагается,
что пользователь авторизовался в Chrome сам (через ЕСИА+ЭП).

Использует тот же эндпоинт и тот же способ получения CSRF-токена
(`window.Main.requestToken` или `Index.index → result.auth_token`),
что и `parse_procedures.py`.
"""
from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

DEVTOOLS_PORT = 9222
RPC_ENDPOINT = "/index.php?rpctype=direct&module=default&client=etp"
HARD_SERVER_LIMIT = 25  # сколько фактически отдаёт сервер за один вызов

_INDEX_INDEX_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 10000);
  try {
    const resp = await fetch('/index.php?rpctype=direct&module=default&client=etp', {
      method: 'POST',
      credentials: 'include',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action: 'Index', method: 'index', data: null,
        type: 'rpc', tid: 1, token: '',
      }),
    });
    clearTimeout(to);
    const j = await resp.json();
    const r = j.result || {};
    callback({
      success: !!r.success,
      token: r.auth_token || null,
      userLogin: (r.user && (r.user.login || r.user.username)) || null,
    });
  } catch (e) {
    clearTimeout(to);
    callback({ error: String(e) });
  }
})();
"""

_FETCH_LIST_JS = r"""
const callback = arguments[arguments.length - 1];
const payload = arguments[0];
const explicitToken = arguments[1] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 25000);
  try {
    const token = explicitToken
      || (window.Main && (window.Main.requestToken || window.Main.token))
      || '';
    const data = Object.assign({}, payload);
    delete data.__tid;
    const resp = await fetch('/index.php?rpctype=direct&module=default&client=etp', {
      method: 'POST',
      credentials: 'include',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action: 'Procedure', method: 'list',
        data: [data], type: 'rpc',
        tid: payload.__tid || 1, token: token,
      }),
    });
    clearTimeout(to);
    const j = await resp.json();
    const r = j.result || {};
    callback({
      success: r.success !== false,
      procedures: r.procedures || [],
      totalCount: r.totalCount != null ? r.totalCount : null,
      no_access: !!r.no_access,
      no_session: !!r.no_session,
      message: r.message || null,
      usedToken: token ? (token.slice(0, 12) + '…') : '',
    });
  } catch (e) {
    clearTimeout(to);
    callback({ error: String(e) });
  }
})();
"""


class EtpClient:
    """Thin wrapper над Selenium-сессией, привязанной к Chrome DevTools.

    Жизненный цикл:
        c = EtpClient()
        c.ensure_chrome()      # стартует Chrome (через start_chrome.ps1) если надо
        c.connect()            # подключается к DevTools
        if not c.is_session_alive():
            # попросить пользователя войти вручную, а потом повторить
            ...
        res = c.fetch_page(start=0, limit=25, date_from="23.04.2025")
        c.close()
    """

    def __init__(self, port: int = DEVTOOLS_PORT) -> None:
        self.port = port
        self.driver: Optional[webdriver.Chrome] = None
        self._token: str = ""

    def is_chrome_running(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1.5):
                return True
        except Exception:
            return False

    def ensure_chrome(self, timeout: int = 40) -> None:
        """Стартует Chrome через start_chrome.ps1, если он ещё не слушает DevTools."""
        if self.is_chrome_running():
            return
        script = Path(__file__).parent / "start_chrome.ps1"
        if not script.exists():
            raise FileNotFoundError(f"Не найден {script}")
        subprocess.Popen(
            [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_chrome_running():
                return
            time.sleep(1)
        raise RuntimeError(
            f"Chrome с DevTools не стартовал за {timeout} сек."
        )

    def connect(self) -> None:
        """Подключается к уже запущенному Chrome c DevTools."""
        if self.driver is not None:
            return
        if not self.is_chrome_running():
            raise RuntimeError(
                f"Chrome с DevTools на порту {self.port} не запущен. "
                "Запусти его сначала через start_chrome.ps1."
            )
        opts = Options()
        opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.port}")
        self.driver = webdriver.Chrome(options=opts)
        self.driver.set_script_timeout(30)
        self._switch_to_etp_tab()

    def _switch_to_etp_tab(self) -> bool:
        """Ищет живую вкладку с etpgaz и переключается на неё. Иначе открывает новую."""
        if not self.driver:
            return False
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            handles = []
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                if "etpgaz.gazprombank.ru" in (self.driver.current_url or ""):
                    return True
            except Exception:
                continue
        # Нет живой вкладки с ЭТП — открываем новую. Сначала пробуем использовать
        # любую живую вкладку, иначе создаём новую через JS.
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                break
            except Exception:
                continue
        try:
            self.driver.execute_script(
                "window.open('https://etpgaz.gazprombank.ru/#com/procedure/index', '_blank');"
            )
        except Exception:
            pass
        try:
            for h in self.driver.window_handles:
                self.driver.switch_to.window(h)
                if "etpgaz.gazprombank.ru" in (self.driver.current_url or ""):
                    return True
        except Exception:
            pass
        try:
            self.driver.get("https://etpgaz.gazprombank.ru/#com/procedure/index")
            return True
        except Exception:
            return False

    def _is_window_lost(self, err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "no such window" in msg
            or "web view not found" in msg
            or "target window already closed" in msg
            or "target frame detached" in msg
            or "invalid session id" in msg
            or "disconnected" in msg
        )

    def _recover_tab(self) -> bool:
        """Пересоединяется к Chrome и переключается на живую вкладку ЭТП."""
        # Если сам драйвер умер — пересоздаём его.
        try:
            _ = self.driver and self.driver.window_handles
        except Exception:
            try:
                if self.driver is not None:
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
            finally:
                self.driver = None
            try:
                self.connect()
            except Exception:
                return False
            return True
        return self._switch_to_etp_tab()

    def pull_token(self) -> str:
        """Достаёт CSRF-токен из SPA или Index.index."""
        if not self.driver:
            return ""
        try:
            tok = (
                self.driver.execute_script(
                    "const m = window.Main || {}; "
                    "return String(m.requestToken || m.token || '');"
                )
                or ""
            )
        except Exception:
            tok = ""
        if not tok:
            try:
                resp = self.driver.execute_async_script(_INDEX_INDEX_JS)
                if isinstance(resp, dict) and resp.get("token"):
                    tok = str(resp["token"])
            except Exception:
                pass
        if tok:
            self._token = tok
        return tok

    def current_user_login(self) -> Optional[str]:
        if not self.driver:
            return None
        try:
            return self.driver.execute_script(
                "const u = (window.Main && Main.user) || {}; "
                "return u.login || u.username || null;"
            )
        except Exception:
            return None

    def is_session_alive(self) -> bool:
        """Быстрая проверка: Procedure.list отдаёт данные без ошибки доступа."""
        if not self.driver:
            return False
        if not self._token:
            self.pull_token()
        res = self.fetch_page(start=0, limit=1)
        return bool(
            res.get("success")
            and not res.get("no_session")
            and not res.get("no_access")
        )

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
        """Один RPC Procedure.list. Сервер жёстко ограничивает отдачу ~25 штук."""
        assert self.driver is not None, "Сначала вызовите connect()"
        if not self._token:
            self.pull_token()

        payload: dict[str, Any] = {
            "sort": sort,
            "dir": direction,
            "with_affiliates": True,
            "query": query or "",
            "tag_id": tag_id,
            "limit": limit,
            "start": start,
            "__tid": int(time.time() * 1000) % 1_000_000,
        }
        if date_from:
            payload["date_published_from"] = date_from
        if date_to:
            payload["date_published_to"] = date_to

        try:
            res = self.driver.execute_async_script(
                _FETCH_LIST_JS, payload, self._token
            )
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
            }
        if not isinstance(res, dict):
            return {
                "success": False,
                "error": "no_response",
                "procedures": [],
                "totalCount": None,
            }

        if (res.get("no_access") or res.get("no_session")) and not res.get("error"):
            new_tok = self.pull_token()
            if new_tok and new_tok != self._token:
                self._token = new_tok
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
        return res

    def close(self) -> None:
        """Отсоединяется от Chrome, НЕ закрывая сам браузер."""
        if self.driver is not None:
            try:
                self.driver.command_executor.close()
            except Exception:
                pass
            self.driver = None


TREND_PUR_LABELS = {
    "001": "Открытый конкурс",
    "002": "Открытый аукцион / редукцион",
    "003": "Запрос предложений",
    "004": "Запрос котировок",
    "005": "Закупка у единственного поставщика",
    "006": "Иное",
}

STEP_ID_LABELS = {
    "registration": "Приём заявок",
    "applic_access": "Открытие заявок",
    "second_parts": "Рассмотрение вторых частей",
    "second_parts_review": "Рассмотрение вторых частей",
    "receiving_price_info": "Получение ценовой информации",
    "finalizing_procedure": "Завершение процедуры",
    "auction": "Проведение аукциона",
    "archive": "В архиве",
}


def trend_pur_label(code: Any) -> str:
    if not code:
        return "—"
    return TREND_PUR_LABELS.get(str(code), str(code))


def step_id_label(step: Any) -> str:
    if not step:
        return "—"
    return STEP_ID_LABELS.get(str(step), str(step))
