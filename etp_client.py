"""Лёгкий клиент ЭТП ГПБ для десктопного приложения.

Делает `Procedure.list` через уже авторизованный Chrome с удалённой
отладкой (DevTools на порту 9222). Не умеет логиниться — предполагается,
что пользователь авторизовался в Chrome сам (через ЕСИА+ЭП).

Использует тот же эндпоинт и тот же способ получения CSRF-токена
(`window.Main.requestToken` или `Index.index → result.auth_token`),
что и `parse_procedures.py`.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

DEVTOOLS_PORT = 9222
RPC_ENDPOINT = "/index.php?rpctype=direct&module=default&client=etp"
HARD_SERVER_LIMIT = 500  # сколько фактически отдаёт сервер за один вызов


@dataclass(frozen=True)
class BrowserLaunchConfig:
    label: str
    exe_path: Path
    user_data_dir: Path
    profile_dir: str = "Default"
    port: int = DEVTOOLS_PORT


def _resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name

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

_COLLECT_DOCUMENT_LINKS_JS = r"""
const callback = arguments[arguments.length - 1];
(() => {
  const exts = /\.(docx?|xlsx?|pdf|zip|rar|7z|rtf|txt|xml|csv)(?:[?#]|$)/i;
  const links = [];
  const seen = new Set();
  function push(href, text) {
    if (!href || seen.has(href)) return;
    seen.add(href);
    links.push({ href, text: (text || '').trim() });
  }
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    const href = a.href;
    const text = (a.innerText || a.textContent || '').trim();
    if (exts.test(href) || exts.test(text) || /download|file|attach|document/i.test(href)) {
      push(href, text);
    }
  }
  callback(links);
})();
"""

_DOWNLOAD_URL_JS = r"""
const callback = arguments[arguments.length - 1];
const url = arguments[0];
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 120000);
  try {
    const resp = await fetch(url, { credentials: 'include', signal: ctrl.signal });
    clearTimeout(to);
    if (!resp.ok) {
      callback({ ok: false, status: resp.status, statusText: resp.statusText });
      return;
    }
    const blob = await resp.blob();
    const reader = new FileReader();
    reader.onloadend = () => callback({
      ok: true,
      dataUrl: reader.result,
      contentType: blob.type || resp.headers.get('content-type') || '',
      disposition: resp.headers.get('content-disposition') || '',
    });
    reader.onerror = () => callback({ ok: false, error: 'FileReader error' });
    reader.readAsDataURL(blob);
  } catch (e) {
    clearTimeout(to);
    callback({ ok: false, error: String(e) });
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
        self.browser = BrowserLaunchConfig(
            label="Google Chrome",
            exe_path=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            user_data_dir=Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            port=port,
        )

    def configure_browser(
        self,
        label: str,
        exe_path: Path,
        user_data_dir: Path,
        profile_dir: str = "Default",
        port: int = DEVTOOLS_PORT,
    ) -> None:
        if (
            self.browser.exe_path == exe_path
            and self.browser.user_data_dir == user_data_dir
            and self.browser.profile_dir == profile_dir
            and self.port == port
        ):
            return
        self.close()
        self.port = port
        self.browser = BrowserLaunchConfig(
            label=label,
            exe_path=exe_path,
            user_data_dir=user_data_dir,
            profile_dir=profile_dir,
            port=port,
        )

    def is_chrome_running(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=1.5):
                return True
        except Exception:
            return False

    def ensure_chrome(self, timeout: int = 40) -> None:
        """Стартует выбранный Chromium-браузер, если он ещё не слушает DevTools."""
        if self.is_chrome_running():
            return
        browser = self.browser
        if not browser.exe_path.exists():
            raise FileNotFoundError(f"Не найден браузер: {browser.exe_path}")

        def launch(user_data_dir: Path, profile_dir: str = "Default") -> None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([
                str(browser.exe_path),
                f"--remote-debugging-port={self.port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={user_data_dir}",
                f"--profile-directory={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                "https://etpgaz.gazprombank.ru/#com/procedure/index",
            ], creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

        def wait_for_port(seconds: int) -> bool:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if self.is_chrome_running():
                    return True
                time.sleep(1)
            return False

        launch(browser.user_data_dir, browser.profile_dir)
        primary_timeout = min(15, timeout)
        if wait_for_port(primary_timeout):
            return

        # Если браузер уже был запущен обычным способом, Chromium часто просто
        # открывает новое окно старого процесса и игнорирует remote debugging.
        # В этом случае запускаем отдельный управляемый профиль приложения.
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", browser.label).strip("_")
        fallback_dir = local / "ETP_GPB_Search" / "browser_profiles" / safe_label
        launch(fallback_dir)
        if wait_for_port(max(5, timeout - primary_timeout)):
            self.browser = BrowserLaunchConfig(
                label=f"{browser.label} (управляемый профиль)",
                exe_path=browser.exe_path,
                user_data_dir=fallback_dir,
                profile_dir="Default",
                port=self.port,
            )
            return
        raise RuntimeError(
            f"{browser.label} открылся, но DevTools-порт {self.port} не поднялся. "
            "Закройте все окна этого браузера и попробуйте снова либо выберите другой браузер."
        )

    def connect(self) -> None:
        """Подключается к уже запущенному Chrome c DevTools."""
        if self.driver is not None:
            return
        if not self.is_chrome_running():
            raise RuntimeError(
                f"{self.browser.label} с DevTools на порту {self.port} не запущен."
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

    def _detail_url(self, proc_id: Any) -> str:
        return f"https://etpgaz.gazprombank.ru/#com/procedure/view/procedure/{proc_id}"

    def _safe_filename(self, name: str, default: str = "document") -> str:
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
        return clean[:180] or default

    def _filename_from_link(self, link: dict[str, Any], index: int) -> str:
        text = str(link.get("text") or "").strip()
        href = str(link.get("href") or "")
        for source in (text, href.rsplit("/", 1)[-1]):
            m = re.search(r"([^/?#]+\.(?:docx?|xlsx?|pdf|zip|rar|7z|rtf|txt|xml|csv))", source, re.I)
            if m:
                return self._safe_filename(m.group(1), f"document_{index}")
        return self._safe_filename(text or f"document_{index}", f"document_{index}")

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Открывает карточку процедуры в Chrome и скачивает найденные документы."""
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия подробной страницы.")

        registry = str(proc.get("registry_number") or proc.get("procedure_number") or proc_id)
        title = str(proc.get("title") or "")
        folder_name = self._safe_filename(f"{registry}_{title[:80]}", str(proc_id))
        out_dir = output_root / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        url = self._detail_url(proc_id)
        if progress:
            progress(f"Открываю подробную страницу {registry}: {url}")
        self.driver.get(url)
        links: list[dict[str, Any]] = []
        deadline = time.time() + 20
        while time.time() < deadline:
            found = self.driver.execute_async_script(_COLLECT_DOCUMENT_LINKS_JS)
            if isinstance(found, list) and found:
                links = found
                break
            time.sleep(1)

        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            if progress:
                progress(f"Проверяю файл {index}/{len(links)}")
            href = str((link or {}).get("href") or "")
            if not href:
                continue
            name = self._filename_from_link(link, index)
            target = out_dir / name
            stem, suffix = target.stem, target.suffix
            n = 2
            while target.exists():
                target = out_dir / f"{stem}_{n}{suffix}"
                n += 1
            if progress:
                progress(f"Скачиваю {registry}: {name}")
            res = self.driver.execute_async_script(_DOWNLOAD_URL_JS, href)
            if not isinstance(res, dict) or not res.get("ok"):
                errors.append(f"{name}: {res}")
                continue
            data_url = str(res.get("dataUrl") or "")
            if "," not in data_url:
                errors.append(f"{name}: пустой ответ")
                continue
            raw = base64.b64decode(data_url.split(",", 1)[1])
            target.write_bytes(raw)
            saved.append(str(target))

        return {
            "procedure": registry,
            "url": url,
            "folder": str(out_dir),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }

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


PROCEDURE_TYPE_LABELS = [
    "Запрос предложений",
    "Конкурентный отбор",
    "Маркетинговые исследования",
    "Обсуждение с участниками (первый этап)",
    "Запрос цен",
    "Конкурентный отбор с повышением стартовой цены",
    "Анализ рынка и сбор ценовой информации",
]

TREND_PUR_LABELS = {
    "001": "Конкурентный отбор",
    "002": "Конкурентный отбор",
    "003": "Запрос предложений",
    "004": "Запрос цен",
    "005": "Запрос цен",
    "006": "Маркетинговые исследования",
}

STATUS_LABELS = [
    "Активные",
    "Прием заявок",
    "Ожидает начала регистрации",
    "Ожидает начала процедуры",
    "Ожидает открытия доступа",
    "Регистрация для участия",
    "Повышение стартовой цены",
    "Вскрытие заявок",
    "Прием ценовой информации",
    "Завершение процедуры",
    "Рассмотрение заявок",
    "Подведение итогов",
]

STEP_ID_LABELS = {
    "registration": "Ожидает открытия доступа",
    "applic_access": "Прием заявок",
    "second_parts": "Рассмотрение заявок",
    "second_parts_review": "Рассмотрение заявок",
    "receiving_price_info": "Прием ценовой информации",
    "finalizing_procedure": "Завершение процедуры",
    "auction": "Повышение стартовой цены",
    "archive": "Архив",
}


def trend_pur_label(code: Any) -> str:
    if not code:
        return "—"
    return TREND_PUR_LABELS.get(str(code), str(code))


def step_id_label(step: Any) -> str:
    if not step:
        return "—"
    return STEP_ID_LABELS.get(str(step), str(step))
