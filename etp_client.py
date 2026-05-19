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
from datetime import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen

from selenium.common.exceptions import SessionNotCreatedException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.webdriver import WebDriver as ChromeWebDriver
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.webdriver import WebDriver as EdgeWebDriver

DEVTOOLS_PORT = 9222
RPC_ENDPOINT = "/index.php?rpctype=direct&module=default&client=etp"
HARD_SERVER_LIMIT = 500  # сколько фактически отдаёт сервер за один вызов
ETP_URL = "https://etpgaz.gazprombank.ru/#com/procedure/index"

SERVER_STATUS_BY_LABEL = {
    # Значения взяты из ExtJS-комбобокса сайта: поле status в Procedure.list.
    "все": -1,
    "активные": -2,
    "прием заявок": 2,
    "приём заявок": 2,
    "ожидает начала регистрации": 201,
    "ожидает начала процедуры": 202,
    "ожидает открытия доступа": 21,
    "регистрация для участия": 28,
    "повышение стартовой цены": 29,
    "вскрытие заявок": 3,
    "прием ценовой информации": 30,
    "приём ценовой информации": 30,
    "завершение процедуры": 31,
    "рассмотрение заявок": 4,
    "подведение итогов": 6,
}


@dataclass(frozen=True)
class BrowserLaunchConfig:
    key: str
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
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    const text = await resp.text();
    const preview = text.slice(0, 160).trim();
    if (!contentType.includes('application/json') || preview.startsWith('<')) {
      callback({
        success: false,
        no_session: true,
        message: 'Сессия не активна или требуется авторизация.',
        status: resp.status,
        contentType,
        preview,
      });
      return;
    }
    const j = JSON.parse(text);
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
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    const text = await resp.text();
    const preview = text.slice(0, 160).trim();
    if (!contentType.includes('application/json') || preview.startsWith('<')) {
      callback({
        success: false,
        procedures: [],
        totalCount: null,
        no_access: false,
        no_session: true,
        message: 'Сессия не активна или требуется авторизация.',
        status: resp.status,
        contentType,
        preview,
      });
      return;
    }
    const j = JSON.parse(text);
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

_SINGLE_WINDOW_GUARD_JS = r"""
(() => {
  if (window.__etpSingleWindowGuardInstalled) return;
  window.__etpSingleWindowGuardInstalled = true;

  const sameWindow = (url) => {
    if (url && typeof url === 'string' && url.trim()) {
      window.location.assign(url);
    }
    return window;
  };

  try {
    window.open = sameWindow;
  } catch (e) {}

  const normalizeTargets = () => {
    try {
      document.querySelectorAll('a[target], form[target]').forEach((el) => {
        el.removeAttribute('target');
      });
    } catch (e) {}
  };

  normalizeTargets();
  try {
    new MutationObserver(normalizeTargets).observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['target'],
    });
  } catch (e) {}

  document.addEventListener('click', (event) => {
    const link = event.target && event.target.closest ? event.target.closest('a[target="_blank"]') : null;
    if (!link || !link.href) return;
    event.preventDefault();
    window.location.assign(link.href);
  }, true);
})();
"""


def _date_to_etp_iso(value: Optional[str], end_of_day: bool = False) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        time_part = "23:59:59" if end_of_day else "00:00:00"
        return f"{value:%Y-%m-%d}T{time_part}+03:00"
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text:
        return text
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            time_part = "23:59:59" if end_of_day else "00:00:00"
            return f"{dt:%Y-%m-%d}T{time_part}+03:00"
        except ValueError:
            pass
    return text


def _server_status_value(labels: tuple[Any, ...]) -> Optional[int]:
    if len(labels) != 1:
        return None
    raw_value = labels[0]
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
        pass
    label = re.sub(r"\s+", " ", str(raw_value or "").casefold().replace("ё", "е")).strip()
    return SERVER_STATUS_BY_LABEL.get(label)


def _payload_preview(raw: bytes) -> str:
    text = raw[:800].decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", text).strip()


def _download_payload_error(name: str, raw: bytes, content_type: str = "") -> str | None:
    lowered_name = str(name or "").lower()
    suffix = Path(name).suffix.lower()
    prefix = raw.lstrip()[:16].lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"{")):
        return (
            "ЭТП вернула HTML/JSON-ответ вместо файла. "
            f"content-type={content_type or '—'}, preview={_payload_preview(raw)}"
        )
    if re.search(r"\.(?:zip|rar|7z)\.\d{3}$", lowered_name):
        return None
    if suffix == ".zip" and not raw.startswith(b"PK"):
        return f"Файл .zip не похож на ZIP-архив. content-type={content_type or '—'}, preview={_payload_preview(raw)}"
    if suffix == ".rar" and not raw.startswith(b"Rar!"):
        return f"Файл .rar не похож на RAR-архив. content-type={content_type or '—'}, preview={_payload_preview(raw)}"
    if suffix == ".7z" and not raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        return f"Файл .7z не похож на 7-Zip-архив. content-type={content_type or '—'}, preview={_payload_preview(raw)}"
    return None


def _purchase_form_value(value: str) -> int:
    text = str(value or "").casefold()
    if "электрон" in text:
        return 0
    if "бумаж" in text:
        return 1
    return -1

_COLLECT_DOCUMENT_LINKS_JS = r"""
const callback = arguments[arguments.length - 1];
(() => {
  const exts = /\.(docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv)(?:[?#]|$)/i;
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

_EXTRACT_PROCEDURE_VIEW_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const bodyLen = () => String(document.body && document.body.innerText || "").length;

  for (let i = 0; i < 60; i++) {
    const t = String(document.body && document.body.innerText || "");
    if (
      /Сведения о процедуре/i.test(t)
      || /Извещение о проведении/i.test(t)
      || String(location.href || "").includes("procedure/view")
    ) {
      if (bodyLen() > 200) break;
    }
    await wait(400);
  }

  const scrollMax = Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0,
    1500
  );
  for (let y = 0; y <= scrollMax; y += 450) {
    window.scrollTo(0, y);
    await wait(100);
  }
  window.scrollTo(0, scrollMax);
  await wait(200);
  window.scrollTo(0, 0);
  await wait(150);

  // Часть блоков карточки подгружается только на активной вкладке ExtJS — по очереди кликаем вкладки.
  try {
    const tabInners = Array.from(
      document.querySelectorAll(".x-tab-inner, .x-tab-inner-default, .x-tab-inner-el")
    );
    const seenLabels = new Set();
    for (const el of tabInners) {
      const label = String(el.innerText || el.textContent || "").trim().slice(0, 160);
      if (!label || seenLabels.has(label)) continue;
      seenLabels.add(label);
      try {
        el.click();
        await wait(320);
      } catch (e) {}
    }
  } catch (e) {}

  await wait(450);

  const scrollMax2 = Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0,
    scrollMax,
    1500
  );
  for (let y = 0; y <= scrollMax2; y += 450) {
    window.scrollTo(0, y);
    await wait(90);
  }
  window.scrollTo(0, scrollMax2);
  await wait(220);
  window.scrollTo(0, 0);
  await wait(150);

  function bestPageText() {
    const chunks = [];
    const pushEl = (el) => {
      if (!el) return;
      const t = String(el.innerText || el.textContent || "").trim();
      if (t.length > 400) chunks.push(t);
    };
    pushEl(document.body);
    const sels = [
      ".x-region-center",
      ".x-border-region-center",
      ".x-panel-body-default",
      ".x-panel-body",
      "#procedureview",
      "#procedure-view",
      "[id*=procedure][id*=view]",
      ".x-viewport-body",
      "[role='main']",
    ];
    for (const sel of sels) {
      try {
        document.querySelectorAll(sel).forEach((el) => pushEl(el));
      } catch (e) {}
    }
    chunks.sort((a, b) => b.length - a.length);
    const body = String(document.body && document.body.innerText || "").trim();
    const richest = chunks.length ? chunks[0] : "";
    return body.length >= richest.length ? body : richest;
  }
  const pageText = bestPageText();

  const exts = /\.(docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv)(?:[?#]|$)/i;
  const docLinks = [];
  const seen = new Set();
  for (const a of Array.from(document.querySelectorAll("a[href]"))) {
    const href = a.href || "";
    const tx = (a.innerText || a.textContent || "").trim();
    if (!href || seen.has(href)) continue;
    if (exts.test(href) || exts.test(tx) || /download|attach|file|document/i.test(href)) {
      seen.add(href);
      docLinks.push({ href, text: tx.slice(0, 240) });
    }
  }

  callback({
    ok: true,
    pageText,
    docLinks,
    url: location.href,
    charCount: pageText.length,
  });
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
        self.driver: Optional[ChromeWebDriver | EdgeWebDriver] = None
        self._token: str = ""
        self.target_url = ETP_URL
        self.target_host = "etpgaz.gazprombank.ru"
        self.browser = BrowserLaunchConfig(
            key="chrome",
            label="Google Chrome",
            exe_path=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            user_data_dir=Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            port=port,
        )

    def configure_browser(
        self,
        key: str,
        label: str,
        exe_path: Path,
        user_data_dir: Path,
        profile_dir: str = "Default",
        port: int = DEVTOOLS_PORT,
    ) -> None:
        if (
            self.browser.key == key
            and self.browser.exe_path == exe_path
            and self.browser.user_data_dir == user_data_dir
            and self.browser.profile_dir == profile_dir
            and self.port == port
        ):
            return
        self.close()
        self.port = port
        self.browser = BrowserLaunchConfig(
            key=key,
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

    def _devtools_json(self, endpoint: str) -> Any:
        with urlopen(f"http://127.0.0.1:{self.port}{endpoint}", timeout=2) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _running_browser_version(self) -> str:
        try:
            payload = self._devtools_json("/json/version")
        except Exception:
            return ""
        browser = str(payload.get("Browser") or "")
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", browser)
        return match.group(1) if match else ""

    def _has_devtools_page(self) -> bool:
        try:
            targets = self._devtools_json("/json/list")
        except Exception:
            return False
        if not isinstance(targets, list):
            return False
        return any(target.get("type") == "page" for target in targets if isinstance(target, dict))

    def _open_devtools_page(self, url: str) -> bool:
        endpoint = f"/json/new?{quote(url, safe=':/?=&')}"
        request = Request(f"http://127.0.0.1:{self.port}{endpoint}", method="PUT")
        try:
            with urlopen(request, timeout=3):
                return True
        except Exception:
            return False

    def _ensure_devtools_page(self, timeout: int = 8) -> None:
        deadline = time.time() + timeout
        opened = False
        while time.time() < deadline:
            if self._has_devtools_page():
                return
            if not opened:
                opened = self._open_devtools_page(self.target_url) or self._open_devtools_page("about:blank")
            time.sleep(0.5)
        raise RuntimeError(
            f"{self.browser.label} слушает DevTools на порту {self.port}, но не отдаёт открытые вкладки. "
            "Закройте все окна выбранного браузера и запустите поиск снова."
        )

    def _matching_chromedriver_service(self) -> Optional[ChromeService]:
        version = self._running_browser_version()
        major = version.split(".", 1)[0] if version else ""
        if not major:
            return None
        try:
            from webdriver_manager.chrome import ChromeDriverManager

            return ChromeService(ChromeDriverManager(driver_version=major).install())
        except Exception:
            return None

    def _driver_version_hint(self, exc: Exception) -> str:
        text = str(exc)
        if "unable to discover open pages" in text:
            return (
                f"{self.browser.label} слушает DevTools на порту {self.port}, "
                "но ChromeDriver не видит открытые вкладки. Закройте все окна выбранного "
                "браузера и запустите поиск снова. Исходная ошибка Selenium: "
                f"{text}"
            )
        if "This version of ChromeDriver only supports Chrome version" not in text:
            return text
        version = self._running_browser_version()
        version_part = f" версии {version}" if version else ""
        return (
            f"Не удалось подобрать ChromeDriver для {self.browser.label}{version_part}. "
            "Обновите выбранный браузер или проверьте интернет-доступ, чтобы приложение "
            "смогло скачать совместимый драйвер. Исходная ошибка Selenium: "
            f"{text}"
        )

    def _install_single_window_guard(self) -> None:
        """Запрещает сайту открывать авторизацию/переходы в новом окне."""
        if not self.driver:
            return
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _SINGLE_WINDOW_GUARD_JS},
            )
        except Exception:
            pass
        try:
            self.driver.execute_script(_SINGLE_WINDOW_GUARD_JS)
        except Exception:
            pass

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
                self.target_url,
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
                key=browser.key,
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
        self._ensure_devtools_page()
        if self.browser.key == "edge":
            edge_opts = EdgeOptions()
            edge_opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.port}")
            self.driver = EdgeWebDriver(options=edge_opts)
        else:
            opts = Options()
            opts.binary_location = str(self.browser.exe_path)
            opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{self.port}")
            service = self._matching_chromedriver_service()
            try:
                if service is not None:
                    self.driver = ChromeWebDriver(service=service, options=opts)
                else:
                    self.driver = ChromeWebDriver(options=opts)
            except SessionNotCreatedException as e:
                raise RuntimeError(self._driver_version_hint(e)) from e
        self.driver.set_script_timeout(30)
        self._install_single_window_guard()
        self._switch_to_etp_tab()

    def _switch_to_etp_tab(self) -> bool:
        """Ищет живую вкладку с etpgaz и переключается на неё.

        Если такой вкладки нет, переиспользует текущую живую вкладку. Это важно
        для поиска: повторный запуск не должен плодить новые окна браузера.
        """
        if not self.driver:
            return False
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            handles = []
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                if self.target_host in (self.driver.current_url or ""):
                    self._install_single_window_guard()
                    return True
            except Exception:
                continue
        # Нет живой вкладки с ЭТП — переходим в первой живой вкладке, не открывая
        # новую через window.open(). Так окно поиска остаётся единственным.
        for h in handles:
            try:
                self.driver.switch_to.window(h)
                self.driver.get(self.target_url)
                self._install_single_window_guard()
                return True
            except Exception:
                continue
        try:
            self.driver.get(self.target_url)
            self._install_single_window_guard()
            return True
        except Exception:
            pass
        if self._open_devtools_page(self.target_url):
            try:
                handles = list(self.driver.window_handles)
            except Exception:
                handles = []
            for h in handles:
                try:
                    self.driver.switch_to.window(h)
                    if self.target_host in (self.driver.current_url or ""):
                        self._install_single_window_guard()
                        return True
                except Exception:
                    continue
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
        """Пересоединяется к браузеру и переключается на живую вкладку площадки."""
        # Если сам драйвер умер — пересоздаём его.
        try:
            _ = self.driver and self.driver.window_handles
        except Exception:
            try:
                if self.driver is not None:
                    try:
                        self.driver.command_executor.close()
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

    def _prepare_fetch_payload(self, payload: dict[str, Any], client_filters: Any = None) -> None:
        """Hook for площадки с небольшими отличиями в Procedure.list payload."""
        return None

    def _filename_from_link(self, link: dict[str, Any], index: int) -> str:
        text = str(link.get("text") or "").strip()
        href = str(link.get("href") or "")
        for source in (text, href.rsplit("/", 1)[-1]):
            m = re.search(
                r"([^/?#]+\.(?:docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv))",
                source,
                re.I,
            )
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
        registry_digits = re.sub(r"\D+", "", registry)
        output_digits = re.sub(r"\D+", "", output_root.name)
        out_dir = output_root if registry_digits and registry_digits in output_digits else output_root / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        url = self._detail_url(proc_id)
        if progress:
            progress(f"Открываю подробную страницу {registry}: {url}")
        self.driver.get(url)
        links: list[dict[str, Any]] = []
        found: Any = None
        try:
            self.driver.set_script_timeout(120)
            found = self.driver.execute_async_script(_EXTRACT_PROCEDURE_VIEW_JS)
        except Exception:
            found = None
        finally:
            self.driver.set_script_timeout(30)
        if isinstance(found, dict) and found.get("ok") and isinstance(found.get("docLinks"), list):
            links = found.get("docLinks") or []
        if not links:
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
            payload_error = _download_payload_error(name, raw, str(res.get("contentType") or ""))
            if payload_error:
                errors.append(f"{name}: {payload_error}")
                continue
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

    def download_document_link(
        self,
        link: dict[str, Any],
        output_dir: Path,
        index: int = 1,
    ) -> Path:
        """Скачивает одну ссылку документации из текущей авторизованной вкладки."""
        assert self.driver is not None, "Сначала вызовите connect()"
        href = str((link or {}).get("href") or "")
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
        res = self.driver.execute_async_script(_DOWNLOAD_URL_JS, href)
        if not isinstance(res, dict) or not res.get("ok"):
            raise RuntimeError(f"Ошибка скачивания {name}: {res}")
        data_url = str(res.get("dataUrl") or "")
        if "," not in data_url:
            raise RuntimeError(f"Пустой ответ при скачивании {name}")
        raw = base64.b64decode(data_url.split(",", 1)[1])
        payload_error = _download_payload_error(name, raw, str(res.get("contentType") or ""))
        if payload_error:
            raise RuntimeError(f"Ошибка скачивания {name}: {payload_error}")
        target.write_bytes(raw)
        return target

    def extract_procedure_card_text(
        self,
        proc: dict[str, Any],
        progress: Optional[Callable[[str], None]] = None,
        max_page_chars: int = 280_000,
    ) -> dict[str, Any]:
        """Открывает карточку процедуры и собирает текст страницы + ссылки на файлы."""
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия подробной страницы.")

        registry = str(proc.get("registry_number") or proc.get("procedure_number") or proc_id)
        url = self._detail_url(proc_id)
        if progress:
            progress(f"Читаю карточку {registry}: {url}")
        self.driver.get(url)
        try:
            self.driver.set_script_timeout(120)
            raw = self.driver.execute_async_script(_EXTRACT_PROCEDURE_VIEW_JS)
        finally:
            self.driver.set_script_timeout(30)

        if not isinstance(raw, dict) or not raw.get("ok"):
            raise RuntimeError(f"Не удалось прочитать страницу: {raw}")

        page_text = str(raw.get("pageText") or "").strip()
        if len(page_text) > max_page_chars:
            page_text = page_text[:max_page_chars] + "\n\n[…текст обрезан…]"

        doc_links = raw.get("docLinks") or []
        primary_file = ""
        if isinstance(doc_links, list):
            for item in doc_links:
                if not isinstance(item, dict):
                    continue
                href = str(item.get("href") or "")
                if re.search(r"\.(zip|rar|7z)\b", href, re.I):
                    primary_file = href
                    break
            if not primary_file and doc_links:
                first = doc_links[0]
                if isinstance(first, dict):
                    primary_file = str(first.get("href") or "")

        return {
            "procedure": registry,
            "procedure_id": proc_id,
            "url": url,
            "page_text": page_text,
            "doc_links": doc_links if isinstance(doc_links, list) else [],
            "primary_doc_url": primary_file,
            "char_count": int(raw.get("charCount") or len(page_text)),
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
        client_filters: Any = None,
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
            "date_published_from": _date_to_etp_iso(date_from),
            "query": query or "",
            "tag_id": tag_id,
            "limit": limit,
            "procedure_number2_like": "",
            "procedure_number_like": "",
            "title_like": "",
            "lot_nomenclature": "",
            "lot_okved": "",
            "organizer": "",
            "customer": "",
            "lot_customer_region_okato": "",
            "agents": "",
            "coordination_resolved": False,
            "guarantee_application_from": None,
            "guarantee_application_till": None,
            "department_id": -1,
            "contact_person_like": "",
            "procedure_type": 0,
            "status": "",
            "private": -1,
            "lot_count_from": "",
            "lot_count_till": "",
            "applics_added_from": "",
            "applics_added_till": "",
            "experts": "",
            "asez_plan_position_id": "",
            "date_published_till": _date_to_etp_iso(date_to, end_of_day=True),
            "date_end_registration_from": "",
            "date_end_registration_till": "",
            "date_end_second_parts_review_from": "",
            "date_end_second_parts_review_till": "",
            "start_price_from": None,
            "start_price_till": None,
            "special_mark": "",
            "lot_units_search": "",
            "nm_types": "",
            "internal_registry_number": "",
            "managed_by_parent": False,
            "start": start,
            "__tid": int(time.time() * 1000) % 1_000_000,
        }
        if client_filters is not None:
            registry = str(getattr(client_filters, "registry_contains", "") or "")
            status_value = _server_status_value(
                tuple(getattr(client_filters, "step_ids", ()) or ())
            )
            payload.update(
                {
                    "procedure_number2_like": str(
                        getattr(client_filters, "unique_number_contains", "") or ""
                    ),
                    "procedure_number_like": registry,
                    "title_like": str(getattr(client_filters, "title_contains", "") or ""),
                    "lot_nomenclature": str(
                        getattr(client_filters, "okpd2_contains", "") or ""
                    ),
                    "lot_okved": str(getattr(client_filters, "okved2_contains", "") or ""),
                    "organizer": str(getattr(client_filters, "organizer_contains", "") or ""),
                    "customer": str(getattr(client_filters, "customer_contains", "") or ""),
                    "lot_customer_region_okato": str(
                        getattr(client_filters, "customer_region_contains", "") or ""
                    ),
                    "agents": str(
                        getattr(client_filters, "customer_agent_contains", "") or ""
                    ),
                    "contact_person_like": str(
                        getattr(client_filters, "responsible_contains", "") or ""
                    ),
                    "procedure_type": getattr(client_filters, "trend_pur", None) or 0,
                    "status": status_value if status_value is not None else "",
                    "private": _purchase_form_value(
                        str(getattr(client_filters, "purchase_form", "") or "")
                    ),
                    "lot_count_from": (
                        str(getattr(client_filters, "lots_min", "") or "")
                    ),
                    "lot_count_till": (
                        str(getattr(client_filters, "lots_max", "") or "")
                    ),
                    "applics_added_from": (
                        str(getattr(client_filters, "applics_min", "") or "")
                    ),
                    "applics_added_till": (
                        str(getattr(client_filters, "applics_max", "") or "")
                    ),
                    "start_price_from": getattr(client_filters, "price_min", None),
                    "start_price_till": getattr(client_filters, "price_max", None),
                    "date_end_registration_from": _date_to_etp_iso(
                        getattr(client_filters, "end_from", None)
                    ),
                    "date_end_registration_till": _date_to_etp_iso(
                        getattr(client_filters, "end_to", None), end_of_day=True
                    ),
                    "date_end_second_parts_review_from": _date_to_etp_iso(
                        getattr(client_filters, "results_from", None)
                    ),
                    "date_end_second_parts_review_till": _date_to_etp_iso(
                        getattr(client_filters, "results_to", None), end_of_day=True
                    ),
                    "special_mark": str(
                        getattr(client_filters, "special_features_contains", "") or ""
                    ),
                    "lot_units_search": str(
                        getattr(client_filters, "position_name_contains", "") or ""
                    ),
                    "nm_types": str(
                        getattr(client_filters, "national_regime_contains", "") or ""
                    ),
                }
            )
            guarantee_min = getattr(client_filters, "guarantee_min", None)
            guarantee_max = getattr(client_filters, "guarantee_max", None)
            if guarantee_min is not None:
                payload["guarantee_application_from"] = guarantee_min
            if guarantee_max is not None:
                payload["guarantee_application_till"] = guarantee_max

        self._prepare_fetch_payload(payload, client_filters)

        request_data = dict(payload)
        request_data.pop("__tid", None)
        request_body = {
            "action": "Procedure",
            "method": "list",
            "data": [request_data],
            "type": "rpc",
            "tid": payload.get("__tid") or 1,
            "token": self._token,
        }
        request_debug = {
            "platform": "gpb",
            "method": "POST",
            "url": RPC_ENDPOINT,
            "headers": {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            "body": request_body,
            "token": self._token,
            "request_payload": payload,
        }

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
                        client_filters=client_filters,
                        _recover_attempt=_recover_attempt + 1,
                    )
            return {
                "success": False,
                "error": str(e),
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "exception": str(e),
                },
            }
        if not isinstance(res, dict):
            return {
                "success": False,
                "error": "no_response",
                "procedures": [],
                "totalCount": None,
                "_debug": {
                    **request_debug,
                    "raw_response": res,
                },
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
                    client_filters=client_filters,
                    _recover_attempt=_recover_attempt + 1,
                )
        raw_res = dict(res)
        res["_debug"] = {
            **request_debug,
            "raw_response": raw_res,
        }
        return res

    def close(self) -> None:
        """Отсоединяется от Chrome, НЕ закрывая сам браузер."""
        if self.driver is not None:
            try:
                self.driver.command_executor.close()
            except Exception:
                pass
            self.driver = None


PROCEDURE_TYPE_ID_LABELS = {
    31: "Маркетинговые исследования",
    32: "Конкурентный отбор",
}

PROCEDURE_TYPE_OPTIONS = [
    ("Конкурентный отбор", "32"),
    ("Маркетинговые исследования", "31"),
]

PROCEDURE_TYPE_LABELS = [label for label, _ in PROCEDURE_TYPE_OPTIONS]

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

STATUS_OPTIONS = [
    (label, str(SERVER_STATUS_BY_LABEL[label.casefold().replace("ё", "е")]))
    for label in STATUS_LABELS
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


def procedure_type_label(code: Any) -> str:
    if code in (None, ""):
        return "—"
    try:
        numeric = int(str(code))
    except (TypeError, ValueError):
        return str(code)
    return PROCEDURE_TYPE_ID_LABELS.get(numeric, str(code))


def step_id_label(step: Any) -> str:
    if not step:
        return "—"
    return STEP_ID_LABELS.get(str(step), str(step))
