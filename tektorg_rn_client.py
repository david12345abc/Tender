from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional

from desktop_app.params import ClientFilters
from etp_client import HARD_SERVER_LIMIT, EtpClient


TEKTORG_RN_URL = "https://rn.tektorg.ru/#com/procedure/index/type/market_survey"
MARKET_SURVEY_TYPE = "market_survey"
RPC_ENDPOINT = "/index.php?rpctype=direct&module=default"


_INDEX_INDEX_JS = r"""
const callback = arguments[arguments.length - 1];
const endpoint = arguments[0];
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 8000);
  const mainToken = () => {
    try {
      const m = window.Main || {};
      return String(m.requestToken || m.token || '');
    } catch (e) {
      return '';
    }
  };
  const readToken = () => {
    const raw = localStorage.getItem('elk_token')
      || localStorage.getItem('access_token')
      || localStorage.getItem('auth_token')
      || sessionStorage.getItem('elk_token')
      || sessionStorage.getItem('access_token')
      || sessionStorage.getItem('auth_token')
      || '';
    if (!raw) return '';
    try {
      return JSON.parse(raw) || raw;
    } catch (e) {
      return raw;
    }
  };
  const findToken = (value) => {
    if (!value || typeof value !== 'object') return '';
    for (const key of ['auth_token', 'token', 'csrf_token', 'request_token', 'requestToken']) {
      if (value[key]) return String(value[key]);
    }
    for (const nested of Object.values(value)) {
      if (nested && typeof nested === 'object') {
        const found = findToken(nested);
        if (found) return found;
      }
    }
    return '';
  };
  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      credentials: 'include',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action: 'Index',
        method: 'index',
        data: null,
        type: 'rpc',
        tid: 1,
        token: '',
      }),
    });
    clearTimeout(to);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) {}
    const result = data && data.result ? data.result : {};
    const user = result.user || {};
    callback({
      ok: resp.ok,
      status: resp.status,
      data,
      text: data ? '' : text.slice(0, 3000),
      success: !!result.success,
      token: mainToken() || findToken(result) || findToken(data) || readToken() || '',
      userLogin: user.login || user.full_name || user.user_email || null,
    });
  } catch (e) {
    clearTimeout(to);
    callback({ ok: false, error: String(e), token: mainToken() || readToken() || '' });
  }
})();
"""

_FETCH_PROCEDURES_JS = r"""
const callback = arguments[arguments.length - 1];
const endpoint = arguments[0];
const payload = arguments[1];
const explicitToken = arguments[2] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 45000);
  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      credentials: 'include',
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action: 'Procedure',
        method: 'list',
        data: [payload],
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: explicitToken,
      }),
    });
    clearTimeout(to);
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) {}
    const result = data && data.result ? data.result : {};
    callback({
      ok: resp.ok,
      status: resp.status,
      data,
      text: data ? '' : text.slice(0, 3000),
      result,
      no_session: resp.status === 401 || resp.status === 403 || !!result.no_session || !!result.no_access,
    });
  } catch (e) {
    clearTimeout(to);
    callback({ ok: false, error: String(e) });
  }
})();
"""

_READ_TOKEN_JS = r"""
const readToken = () => {
  const raw = localStorage.getItem('elk_token')
    || localStorage.getItem('access_token')
    || localStorage.getItem('auth_token')
    || sessionStorage.getItem('elk_token')
    || sessionStorage.getItem('access_token')
    || sessionStorage.getItem('auth_token')
    || '';
  if (!raw) return '';
  try {
    return JSON.parse(raw) || raw;
  } catch (e) {
    return raw;
  }
};
return readToken();
"""


_CURRENT_USER_JS = r"""
function decodeJwt(token) {
  try {
    const payload = token.split('.')[1] || '';
    return JSON.parse(decodeURIComponent(escape(atob(payload))));
  } catch (e) {
    return {};
  }
}
try {
  const raw = localStorage.getItem('elk_token')
    || localStorage.getItem('access_token')
    || sessionStorage.getItem('elk_token')
    || sessionStorage.getItem('access_token')
    || '';
  let token = '';
  if (raw) {
    try {
      token = JSON.parse(raw) || raw;
    } catch (e) {
      token = raw;
    }
  }
  const payload = decodeJwt(String(token || ''));
  const user = payload.user || payload || {};
  return [user.surname, user.name, user.patronymic].filter(Boolean).join(' ')
    || user.login
    || user.email
    || payload.sub
    || null;
} catch (e) {
  return null;
}
"""

_COLLECT_RN_DOCUMENT_LINKS_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const wanted = /Документация процедуры|Извещение/i;
  const fileLike = (href, text) => {
    const value = `${href || ""} ${text || ""}`;
    return /\/file\/get\//i.test(value)
      || /\.(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv)(?:[?#]|\s|$)/i.test(value);
  };

  for (let i = 0; i < 80; i++) {
    const pageText = String(document.body && document.body.innerText || "");
    const anchors = Array.from(document.querySelectorAll("a[href]"));
    if (
      wanted.test(pageText)
      && anchors.some((a) => fileLike(a.href, a.innerText || a.textContent || ""))
    ) {
      break;
    }
    await wait(300);
  }

  const links = [];
  const seen = new Set();

  function push(anchor, section) {
    let href = anchor && anchor.href;
    if (!href || seen.has(href)) return;
    const text = (anchor.innerText || anchor.textContent || "").trim();
    if (!fileLike(href, text)) return;
    href = href.replace("/file/get/#/", "/file/get/");
    seen.add(href);
    links.push({ href, text, section });
  }

  function collectFrom(root, section) {
    for (const anchor of Array.from(root.querySelectorAll("a[href]"))) {
      push(anchor, section);
    }
  }

  for (const fieldset of Array.from(document.querySelectorAll("fieldset"))) {
    const legend = fieldset.querySelector("legend");
    const title = (legend && (legend.innerText || legend.textContent) || "").trim();
    if (wanted.test(title)) collectFrom(fieldset, title);
  }

  if (!links.length) {
    const blocks = Array.from(document.querySelectorAll("body, div, section, table, tbody, tr, td"));
    for (const block of blocks) {
      const text = String(block.innerText || block.textContent || "").trim();
      if (!wanted.test(text)) continue;
      const section = /Документация процедуры/i.test(text)
        ? "Документация процедуры"
        : "Извещение";
      collectFrom(block, section);
    }
  }

  if (!links.length) {
    for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
      const href = anchor.href || "";
      const text = (anchor.innerText || anchor.textContent || "").trim();
      if (!fileLike(href, text)) continue;
      const before = String(document.body && document.body.innerText || "");
      const section = /Извещение/i.test(before) && /извещ/i.test(text)
        ? "Извещение"
        : "Документация процедуры";
      push(anchor, section);
    }
  }

  callback(links);
})();
"""

class TektorgRnClient(EtpClient):
    """Клиент ТЭК-Торг РН через авторизованную вкладку браузера."""

    server_side_search = True
    max_page_limit = 5
    request_delay_seconds = 0.8

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_RN_URL
        self.target_host = "rn.tektorg.ru"
        self._filters = ClientFilters()
        self._keyword_cache_key: tuple[Any, ...] | None = None
        self._keyword_cache_rows: list[dict[str, Any]] = []

    def set_client_filters(self, filters: ClientFilters) -> None:
        if filters != self._filters:
            self._keyword_cache_key = None
            self._keyword_cache_rows = []
        self._filters = filters

    def _detail_url(self, proc_id: Any) -> str:
        if not proc_id:
            return TEKTORG_RN_URL
        return f"https://rn.tektorg.ru/#com/procedure/view/procedure/{proc_id}"

    def download_visible_procedure_documents(
        self,
        proc: dict[str, Any],
        output_dir: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Скачивает файлы из блоков «Документация процедуры» и «Извещение»."""
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия подробной страницы.")

        registry = str(proc.get("registry_number") or proc.get("procedure_number") or proc_id)
        url = self._detail_url(proc_id)
        if progress:
            progress(f"Открываю карточку {registry}: {url}")
        self.driver.get(url)

        links = self.driver.execute_async_script(_COLLECT_RN_DOCUMENT_LINKS_JS)
        if not isinstance(links, list):
            links = []

        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            if not isinstance(link, dict):
                continue
            try:
                target = self.download_document_link(link, output_dir, index=index)
                saved.append(str(target))
                if progress:
                    progress(f"{registry}: скачан файл {target.name}")
            except Exception as e:
                errors.append(str(e))
                if progress:
                    progress(f"{registry}: ошибка скачивания файла: {e}")

        return {
            "procedure": registry,
            "url": url,
            "folder": str(output_dir),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }

    def upload_technical_documents(
        self,
        application_url: str,
        technical_dir: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Загружает файлы технической части на странице создания заявки."""
        assert self.driver is not None, "Сначала вызовите connect()"
        files = [
            path for path in sorted(technical_dir.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.name.startswith("~$")
        ]
        if not files:
            return {"uploaded": [], "errors": [f"В папке нет файлов: {technical_dir}"]}

        if progress:
            progress("Открываю вкладку подачи заявки для загрузки технических файлов...")
        self._switch_to_application_tab(application_url, progress=progress)
        if progress:
            progress("Ожидаю блок технической части заявки...")
        self._ensure_technical_tab_active()
        self._clear_uploaded_files(progress=progress)

        uploaded: list[str] = []
        errors: list[str] = []
        for index, path in enumerate(files, start=1):
            if progress:
                progress(f"Загружаю технический файл {index}/{len(files)}: {path.name}")
            try:
                self._upload_one_file(path, progress=progress)
                self._remove_duplicate_uploaded_files(progress=progress)
                if not self._is_uploaded_file_listed(path):
                    raise RuntimeError("файл не появился в списке загруженных документов")
                uploaded.append(str(path))
            except Exception as e:
                errors.append(f"{path.name}: {e}")
                break
        self._remove_duplicate_uploaded_files(progress=progress)
        if not errors:
            if progress:
                progress("Перехожу на вкладку коммерческой части предложения...")
            self._ensure_commercial_tab_active()
            if progress:
                progress("Открываю окно формирования письма о подаче заявки...")
            self._open_application_letter_modal()
        return {"uploaded": uploaded, "errors": errors}

    def _switch_to_application_tab(
        self,
        application_url: str,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        assert self.driver is not None
        normalized = self._normalize_tektorg_url(application_url)

        def is_target_url(url: str) -> bool:
            current = self._normalize_tektorg_url(url)
            return (
                bool(current)
                and (
                    current == normalized
                    or normalized in current
                    or "/#com/applic/create/" in current
                    or "#com/applic/create/" in current
                )
            )

        deadline = time.time() + 18
        while time.time() < deadline:
            for handle in reversed(list(self.driver.window_handles)):
                try:
                    self.driver.switch_to.window(handle)
                    current = self.driver.current_url or ""
                    if is_target_url(current):
                        if progress:
                            progress(f"Найдена вкладка заявки: {current}")
                        return
                except Exception:
                    continue
            time.sleep(0.3)

        if progress:
            progress("Вкладка заявки не найдена Selenium, открываю её через WebDriver...")
        handles_before = set(self.driver.window_handles)
        try:
            self.driver.execute_script("window.open(arguments[0], '_blank');", application_url)
            deadline = time.time() + 10
            while time.time() < deadline:
                handles_after = set(self.driver.window_handles)
                new_handles = [handle for handle in handles_after - handles_before]
                if new_handles:
                    self.driver.switch_to.window(new_handles[-1])
                    break
                time.sleep(0.2)
            else:
                self.driver.get(application_url)
        except Exception:
            self.driver.get(application_url)

        if not is_target_url(self.driver.current_url or ""):
            self.driver.get(application_url)
        self._wait_for_application_url()

    def _normalize_tektorg_url(self, url: str) -> str:
        value = str(url or "").strip()
        value = value.replace("https://rn.tektorg.ru/index.php", "https://rn.tektorg.ru")
        value = value.replace("https://rn.tektorg.ru/#", "https://rn.tektorg.ru/#")
        return value.rstrip("/")

    def _wait_for_application_url(self) -> None:
        assert self.driver is not None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                current = self.driver.current_url or ""
                ready = self.driver.execute_script("return document.readyState") in {"interactive", "complete"}
                if ready and "applic/create" in current:
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise RuntimeError("Страница подачи заявки не открылась в браузере.")

    def _ensure_technical_tab_active(self) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const clickBest = (node) => {
    const chain = [node, node && node.parentElement, node && node.parentElement && node.parentElement.parentElement];
    for (const el of chain) {
      if (!el) continue;
      try {
        el.scrollIntoView({ block: "center", inline: "center" });
        el.click();
        return true;
      } catch (e) {}
    }
    return false;
  };
  for (let i = 0; i < 90; i++) {
    const bodyText = String(document.body && document.body.innerText || "");
    const hasTechnicalBlock = /Документы\s+технической\s+части\s+заявки/i.test(bodyText);
    const hasFileInput = Array.from(document.querySelectorAll("input[type='file']")).some(visible);
    if (hasTechnicalBlock && hasFileInput) {
      callback(true);
      return;
    }
    if (hasTechnicalBlock && document.querySelector("input[type='file']")) {
      callback(true);
      return;
    }
    const nodes = Array.from(document.querySelectorAll("a, button, span, div, td, em"));
    const tab = nodes.find((el) => /Техническая\s+часть\s+предложения/i.test(textOf(el)));
    if (tab) {
      clickBest(tab);
    }
    await wait(400);
  }
  callback(false);
})();
"""
        ok = self.driver.execute_async_script(script)
        if not ok:
            raise RuntimeError("Не удалось открыть блок технической части заявки.")

    def _ensure_commercial_tab_active(self) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").trim();
  const commercialTitle = "Коммерческая часть предложения";
  const isCommercialCmp = (cmp) => String(cmp && cmp.title || "").trim() === commercialTitle;
  const findCommercialCmp = () => {
    if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return null;
    let found = null;
    Ext.ComponentMgr.all.each(function(cmp) {
      if (!found && isCommercialCmp(cmp)) found = cmp;
    });
    return found;
  };
  const activateByExt = () => {
    const cmp = findCommercialCmp();
    if (!cmp) return false;
    try {
      if (cmp.ownerCt && cmp.ownerCt.setActiveTab) {
        cmp.ownerCt.setActiveTab(cmp);
        return true;
      }
      if (cmp.show) {
        cmp.show();
        return true;
      }
    } catch (e) {}
    return false;
  };
  const isCommercialActive = () => {
    const cmp = findCommercialCmp();
    if (cmp && cmp.ownerCt && cmp.ownerCt.activeTab === cmp) return true;
    const nodes = Array.from(document.querySelectorAll("a, button, span, div, td, em, li"));
    return nodes.some((el) => {
      const text = textOf(el);
      if (!/Коммерческая\s+часть\s+предложения/i.test(text)) return false;
      const cls = String(el.className || "");
      const parentCls = String(el.parentElement && el.parentElement.className || "");
      return /active|x-tab-strip-active|selected|current/i.test(`${cls} ${parentCls}`);
    });
  };
  const clickBest = (node) => {
    const chain = [node, node && node.parentElement, node && node.parentElement && node.parentElement.parentElement];
    for (const el of chain) {
      if (!el) continue;
      try {
        el.scrollIntoView({ block: "center", inline: "center" });
        el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
        el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
        el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        return true;
      } catch (e) {}
    }
    return false;
  };
  for (let i = 0; i < 50; i++) {
    if (isCommercialActive()) {
      callback(true);
      return;
    }
    if (activateByExt()) {
      await wait(350);
      if (isCommercialActive()) {
        callback(true);
        return;
      }
    }
    const nodes = Array.from(document.querySelectorAll("a, button, span, div, td, em, li"));
    const tab = nodes.find((el) => /Коммерческая\s+часть\s+предложения/i.test(textOf(el)));
    if (tab) {
      clickBest(tab);
      await wait(350);
      if (isCommercialActive()) {
        callback(true);
        return;
      }
    }
    await wait(250);
  }
  callback(false);
})();
"""
        ok = self.driver.execute_async_script(script)
        if not ok:
            raise RuntimeError("Не удалось открыть вкладку коммерческой части предложения.")

    def _open_application_letter_modal(self) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const target = /Сформировать\s+письмо\s+о\s+подаче\s+заявки/i;
  const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const findLetterButtonCmp = () => {
    if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return null;
    let found = null;
    Ext.ComponentMgr.all.each(function(cmp) {
      if (found || cmp.hidden || cmp.disabled) return;
      const text = String(cmp.text || "");
      if (cmp.xtype === "button" && target.test(text)) {
        const el = cmp.el && cmp.el.dom;
        if (!el || visible(el)) found = cmp;
      }
    });
    return found;
  };
  const clickByExt = () => {
    const cmp = findLetterButtonCmp();
    if (!cmp) return false;
    try {
      if (cmp.handler) {
        cmp.handler.call(cmp.scope || cmp, cmp);
        return true;
      }
      if (cmp.fireEvent) {
        cmp.fireEvent("click", cmp);
        return true;
      }
    } catch (e) {}
    return false;
  };
  const modalOpened = () => {
    const windows = Array.from(document.querySelectorAll(".x-window, .x-window-dlg, .x-window-body"));
    return windows.some((el) => {
      if (!visible(el)) return false;
      const text = textOf(el);
      return /письм|подач|заявк/i.test(text) && !target.test(text);
    });
  };
  const clickElement = (el) => {
    const chain = [el, el && el.parentElement, el && el.parentElement && el.parentElement.parentElement];
    for (const node of chain) {
      if (!node || !visible(node)) continue;
      try {
        node.scrollIntoView({ block: "center", inline: "center" });
        node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
        node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
        node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        return true;
      } catch (e) {}
    }
    return false;
  };
  for (let i = 0; i < 60; i++) {
    if (modalOpened()) {
      callback(true);
      return;
    }
    if (clickByExt()) {
      await wait(700);
      if (modalOpened()) {
        callback(true);
        return;
      }
    }
    const nodes = Array.from(document.querySelectorAll("button, input[type='button'], a, span, div, td, em"));
    const button = nodes.find((el) => visible(el) && target.test(textOf(el)));
    if (button) {
      clickElement(button);
      await wait(600);
      if (modalOpened()) {
        callback(true);
        return;
      }
    }
    await wait(250);
  }
  callback(false);
})();
"""
        ok = self.driver.execute_async_script(script)
        if not ok:
            raise RuntimeError("Не удалось открыть окно формирования письма о подаче заявки.")

    def _upload_one_file(
        self,
        path: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        assert self.driver is not None

        dialog_error: Exception | None = None
        if progress:
            progress(f"Открываю штатный выбор файла: {path.name}")
        try:
            self._upload_one_file_via_dialog(path)
            if self._wait_after_file_selection(path):
                return
            raise RuntimeError("файл не появился в списке после штатной загрузки")
        except Exception as e:
            dialog_error = e
            if progress:
                progress(f"Штатная загрузка не подтвердилась для {path.name}: {e}")

        direct_error: Exception | None = None
        try:
            if progress:
                progress(f"Пробую резервную загрузку через input: {path.name}")
            input_element = self._find_tektorg_file_input()
            self.driver.execute_script(
                """
const input = arguments[0];
let node = input;
while (node && node !== document.body) {
  node.style.display = node.style.display === 'none' ? 'block' : node.style.display;
  node.style.visibility = 'visible';
  node = node.parentElement;
}
input.style.display = 'block';
input.style.visibility = 'visible';
input.style.opacity = 1;
input.style.position = 'relative';
input.style.zIndex = 999999;
input.style.width = '520px';
input.style.height = '30px';
input.removeAttribute('disabled');
input.scrollIntoView({ block: 'center', inline: 'center' });
""",
                input_element,
            )
            if progress:
                progress(f"Передаю файл в поле загрузки через DevTools: {path.name}")
            try:
                self._set_file_input_files_with_cdp(input_element, path)
            except Exception as cdp_error:
                if progress:
                    progress(f"DevTools-загрузка не сработала, пробую Selenium send_keys: {cdp_error}")
                input_element.send_keys(str(path))
            self._dispatch_file_input_events(input_element)
            if self._wait_after_file_selection(path):
                return
            raise RuntimeError("Сайт не показал файл в списке после выбора через input.")
        except Exception as e:
            direct_error = e
            if progress:
                progress(f"Прямая загрузка через input не сработала для {path.name}: {e}")

        raise RuntimeError(f"Файл не загрузился. Ошибка штатной загрузки: {dialog_error}. Ошибка input: {direct_error}")

    def _find_tektorg_file_input(self):
        assert self.driver is not None

        deadline = time.time() + 25
        while time.time() < deadline:
            input_element = self.driver.execute_script(
                r"""
const inputs = Array.from(document.querySelectorAll("input.x-form-file[type='file'], input[type='file']"));
if (!inputs.length) return null;
const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "");
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
};
const ancestorText = (el) => {
  const chunks = [];
  let node = el;
  for (let i = 0; node && i < 8; i++, node = node.parentElement) {
    chunks.push(textOf(node));
  }
  return chunks.join("\n");
};
let best = null;
let bestScore = -1;
for (const input of inputs) {
  const text = ancestorText(input);
  let score = 0;
  if (/Документы\s+технической\s+части\s+заявки/i.test(text)) score += 100;
  if (/Техническая\s+часть/i.test(text)) score += 60;
  if (/Выбрать\s+и\s+загрузить\s+файл/i.test(text)) score += 40;
  if ((input.className || "").toString().includes("x-form-file")) score += 20;
  if (visible(input)) score += 10;
  if (!input.disabled) score += 5;
  if (score > bestScore) {
    best = input;
    bestScore = score;
  }
}
return best;
"""
            )
            if input_element is not None:
                return input_element
            time.sleep(0.3)
        raise RuntimeError("На странице не найдено поле выбора файла input.x-form-file.")

    def _set_file_input_files_with_cdp(self, input_element, path: Path) -> None:
        assert self.driver is not None
        marker = f"auto-upload-{time.time_ns()}"
        self.driver.execute_script(
            "arguments[0].setAttribute('data-auto-upload-id', arguments[1]);",
            input_element,
            marker,
        )
        document = self.driver.execute_cdp_cmd(
            "DOM.getDocument",
            {"depth": -1, "pierce": True},
        )
        root = document.get("root") if isinstance(document, dict) else {}
        root_id = root.get("nodeId") if isinstance(root, dict) else None
        if not root_id:
            raise RuntimeError("CDP не вернул корневой DOM nodeId.")
        found = self.driver.execute_cdp_cmd(
            "DOM.querySelector",
            {"nodeId": root_id, "selector": f'input[data-auto-upload-id="{marker}"]'},
        )
        node_id = found.get("nodeId") if isinstance(found, dict) else None
        if not node_id:
            raise RuntimeError("CDP не нашёл file input в DOM.")
        self.driver.execute_cdp_cmd(
            "DOM.setFileInputFiles",
            {"nodeId": node_id, "files": [str(path)]},
        )

    def _dispatch_file_input_events(self, input_element) -> None:
        assert self.driver is not None
        self.driver.execute_script(
            """
const input = arguments[0];
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
if (window.Ext) {
  try {
    const fileFields = Ext.ComponentQuery ? Ext.ComponentQuery.query('filefield, fileuploadfield') : [];
    const cmp = fileFields.find((item) => {
      try {
        return item.fileInputEl && item.fileInputEl.dom === input;
      } catch (e) {
        return false;
      }
    });
    if (cmp) {
      if (cmp.fireEvent) cmp.fireEvent('change', cmp, input.value, '');
      if (cmp.onFileChange) cmp.onFileChange(input, { target: input });
      if (cmp.button && cmp.button.fireEvent) cmp.button.fireEvent('change', cmp.button, input.value);
    }
  } catch (e) {}
}
""",
            input_element,
        )

    def _wait_after_file_selection(self, path: Path) -> bool:
        assert self.driver is not None
        marker = self._normalize_uploaded_filename(path.name)
        deadline = time.time() + 18
        while time.time() < deadline:
            try:
                info = self.driver.execute_script(
                    r"""
const bodyText = String(document.body && document.body.innerText || "");
const masks = Array.from(document.querySelectorAll(".x-mask-loading, .x-mask-msg, .x-mask"))
  .filter((el) => {
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  })
  .map((el) => String(el.innerText || el.textContent || ""));
return { bodyText, masks };
"""
                )
                if isinstance(info, dict):
                    body_text = str(info.get("bodyText") or "")
                    text_marker = self._normalize_uploaded_filename(body_text)
                    if marker and marker in text_marker:
                        return True
                    masks = info.get("masks") or []
                    if masks:
                        time.sleep(0.5)
                        continue
            except Exception:
                pass
            time.sleep(0.4)
        return self._is_uploaded_file_listed(path)

    def _is_uploaded_file_listed(self, path: Path) -> bool:
        marker = self._normalize_uploaded_filename(path.name)
        if not marker:
            return False
        return marker in {self._normalize_uploaded_filename(name) for name in self._uploaded_file_names()}

    def _uploaded_file_names(self) -> list[str]:
        items = self._uploaded_file_items()
        if items:
            return [str(item.get("name") or "") for item in items if str(item.get("name") or "").strip()]
        assert self.driver is not None
        try:
            names = self.driver.execute_script(
                r"""
const anchors = Array.from(document.querySelectorAll("a"));
return anchors
  .map((a) => String(a.innerText || a.textContent || "").trim())
  .filter((text) => /\.(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv)$/i.test(text));
"""
            )
        except Exception:
            return []
        if not isinstance(names, list):
            return []
        return [str(name).strip() for name in names if str(name or "").strip()]

    def _normalize_uploaded_filename(self, name: str) -> str:
        value = str(name or "").casefold().replace("ё", "е")
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"[_\W]+", "", value, flags=re.UNICODE)
        return value

    def _uploaded_file_items(self) -> list[dict[str, Any]]:
        assert self.driver is not None
        try:
            items = self.driver.execute_script(
                r"""
const upload = Ext && Ext.getCmp ? Ext.getCmp("application_docs_tech_1") : null;
const panel = upload && upload.ids ? Ext.getCmp(upload.ids.uploaded_files_id) : null;
if (!panel || !panel.items) return [];
const result = [];
panel.items.each(function(item) {
  const file = item.file || {};
  const name = String(file.original_name || file.name || "").trim();
  if (!name) return;
  result.push({
    componentId: item.id,
    fileId: file.id || null,
    name: name,
    date: file.date || "",
    size: file.size || null
  });
});
return result;
"""
            )
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _remove_duplicate_uploaded_files(
        self,
        progress: Optional[Callable[[str], None]] = None,
    ) -> int:
        assert self.driver is not None
        removed = 0
        while True:
            duplicate = self._next_duplicate_uploaded_file()
            if not duplicate:
                break
            name = str(duplicate.get("name") or "файл")
            component_id = str(duplicate.get("componentId") or "")
            if not component_id:
                break
            if progress:
                progress(f"Удаляю дубль загруженного файла: {name}")
            before_count = len(self._uploaded_file_names())
            deleted = self._delete_uploaded_file_component(component_id)
            if not deleted:
                break
            if not self._wait_uploaded_count_less_than(before_count):
                raise RuntimeError(f"Сайт не удалил дубль файла: {name}")
            removed += 1
        if removed and progress:
            progress(f"Удалено дублей технических документов: {removed}")
        return removed

    def _clear_uploaded_files(
        self,
        progress: Optional[Callable[[str], None]] = None,
    ) -> int:
        assert self.driver is not None
        removed = 0
        while True:
            item = self._first_uploaded_file()
            if not item:
                break
            name = str(item.get("name") or "файл")
            component_id = str(item.get("componentId") or "")
            if not component_id:
                break
            if progress:
                progress(f"Очищаю ранее загруженный технический файл: {name}")
            before_count = len(self._uploaded_file_names())
            deleted = self._delete_uploaded_file_component(component_id)
            if not deleted:
                break
            if not self._wait_uploaded_count_less_than(before_count):
                raise RuntimeError(f"Сайт не удалил ранее загруженный файл: {name}")
            removed += 1
        if removed and progress:
            progress(f"Очищено ранее загруженных технических файлов: {removed}")
        return removed

    def _next_duplicate_uploaded_file(self) -> Optional[dict[str, str]]:
        items = self._uploaded_file_items()
        grouped: dict[str, list[dict[str, str]]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            key = self._normalize_uploaded_filename(name)
            if not key:
                continue
            grouped.setdefault(key, []).append(
                {
                    "name": name,
                    "componentId": str(item.get("componentId") or ""),
                }
            )
        for duplicates in grouped.values():
            if len(duplicates) > 1:
                # Оставляем последнюю запись: обычно это самый свежий загруженный файл.
                return duplicates[0]
        return None

    def _first_uploaded_file(self) -> Optional[dict[str, str]]:
        items = self._uploaded_file_items()
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        return {
            "name": str(first.get("name") or ""),
            "componentId": str(first.get("componentId") or ""),
        }

    def _delete_uploaded_file_component(self, component_id: str) -> bool:
        assert self.driver is not None
        try:
            return bool(
                self.driver.execute_script(
                    r"""
const componentId = arguments[0];
const upload = Ext && Ext.getCmp ? Ext.getCmp("application_docs_tech_1") : null;
const item = Ext && Ext.getCmp ? Ext.getCmp(componentId) : null;
if (!upload || !item || !item.file || !upload.deleteFile) return false;
upload.deleteFile(item.file);
return true;
""",
                    component_id,
                )
            )
        except Exception:
            return False

    def _confirm_delete_dialog(self) -> None:
        assert self.driver is not None
        time.sleep(0.3)
        try:
            alert = self.driver.switch_to.alert
            alert.accept()
            return
        except Exception:
            pass
        try:
            self.driver.execute_async_script(
                r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let i = 0; i < 20; i++) {
    const buttons = Array.from(document.querySelectorAll("button, .x-btn-text, td.x-btn-mc"));
    const yes = buttons.find((el) => /^(да|yes|ok|удалить)$/i.test(String(el.innerText || el.textContent || el.value || "").trim()));
    if (yes) {
      try {
        yes.click();
        callback(true);
        return;
      } catch (e) {}
    }
    await wait(200);
  }
  callback(false);
})();
"""
            )
        except Exception:
            pass

    def _wait_uploaded_count_less_than(self, before_count: int) -> bool:
        deadline = time.time() + 12
        while time.time() < deadline:
            if len(self._uploaded_file_names()) < before_count:
                return True
            time.sleep(0.3)
        return False

    def _upload_one_file_via_dialog(self, path: Path) -> None:
        assert self.driver is not None
        clicked = self.driver.execute_async_script(
            r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let i = 0; i < 40; i++) {
    const nodes = Array.from(document.querySelectorAll("button, input[type=button], a, span, div"));
    const button = nodes.find((el) => /Выбрать\s+и\s+загрузить\s+файл/i.test(String(el.innerText || el.value || el.textContent || "")));
    if (button) {
      try {
        button.scrollIntoView({ block: "center", inline: "center" });
        button.click();
        callback(true);
        return;
      } catch (e) {
        callback(false);
        return;
      }
    }
    await wait(250);
  }
  callback(false);
})();
"""
        )
        if not clicked:
            raise RuntimeError("Не найдена кнопка «Выбрать и загрузить файл».")

        time.sleep(0.7)
        self._set_windows_clipboard_text(str(path))
        from pywinauto.keyboard import send_keys

        send_keys("^v")
        time.sleep(0.2)
        send_keys("{ENTER}")
        time.sleep(2.5)

    def _set_windows_clipboard_text(self, text: str) -> None:
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        finally:
            win32clipboard.CloseClipboard()

    def _switch_to_etp_tab(self) -> bool:
        switched = super()._switch_to_etp_tab()
        if not self.driver:
            return switched
        try:
            current_url = self.driver.current_url or ""
            if self.target_host not in current_url or MARKET_SURVEY_TYPE not in current_url:
                self.driver.get(self.target_url)
                return True
        except Exception:
            return switched
        return switched

    def connect(self) -> None:
        super().connect()
        if self.driver is not None:
            self.driver.set_script_timeout(55)

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
            result = self.driver.execute_async_script(_INDEX_INDEX_JS, RPC_ENDPOINT)
            token = result.get("token") if isinstance(result, dict) else ""
        except Exception:
            token = ""
        self._token = str(token or "")
        return self._token

    def is_session_alive(self) -> bool:
        if self.pull_token():
            return True
        try:
            return bool(self.driver and self.driver.execute_script(_READ_TOKEN_JS))
        except Exception:
            return False

    def _list_payload(
        self,
        start: int,
        limit: int,
        query: Optional[str],
        ignore_number_filter: bool = False,
    ) -> dict[str, Any]:
        f = self._filters
        title_search = str(query or f.quick_search or f.title_contains or "").strip()
        number_search = "" if ignore_number_filter else str(f.registry_contains or "").strip()
        payload: dict[str, Any] = {
            "today": False,
            "limit": limit,
            "start": start,
            "page": max(1, start // max(1, limit) + 1),
            "query": title_search or number_search,
            "procedure_type": 19,
            "checkType": MARKET_SURVEY_TYPE,
            "status": [],
            "sort": [{"property": "id", "direction": "DESC"}],
        }
        if f.published_from:
            payload["date_published_from"] = f.published_from.strftime("%d.%m.%Y")
        if f.published_to:
            payload["date_published_till"] = f.published_to.strftime("%d.%m.%Y")
        if f.end_from:
            payload["date_end_registration_from"] = f.end_from.strftime("%d.%m.%Y")
        if f.end_to:
            payload["date_end_registration_till"] = f.end_to.strftime("%d.%m.%Y")
        if f.results_from:
            payload["date_end_second_parts_review_from"] = f.results_from.strftime("%d.%m.%Y")
        if f.results_to:
            payload["date_end_second_parts_review_till"] = f.results_to.strftime("%d.%m.%Y")
        if f.price_min is not None:
            payload["start_price_from"] = f.price_min
        if f.price_max is not None:
            payload["start_price_till"] = f.price_max
        return payload

    def _request_page(self, payload: dict[str, Any], _recover_attempt: int = 0) -> dict[str, Any]:
        assert self.driver is not None, "Сначала вызовите connect()"
        if not self._token:
            self.pull_token()
        if not self._token:
            return {
                "ok": False,
                "no_session": True,
                "error": "Не удалось получить RPC-токен ТЭК-Торг для запроса списка.",
            }
        try:
            result = self.driver.execute_async_script(_FETCH_PROCEDURES_JS, RPC_ENDPOINT, payload, self._token)
        except Exception as e:
            if self._is_window_lost(e) and _recover_attempt < 2:
                if self._recover_tab():
                    self._token = ""
                    self.pull_token()
                    return self._request_page(payload, _recover_attempt + 1)
            return {"ok": False, "error": str(e)}
        if not isinstance(result, dict):
            return {"ok": False, "error": "no_response"}
        if (
            not result.get("ok")
            and "abort" in str(result.get("error") or "").lower()
        ):
            query_text = str(payload.get("query") or payload.get("title_like") or "").strip()
            return {
                "ok": False,
                "error": (
                    "ТЭК-Торг не ответил на запрос списка за 45 секунд "
                    f"(limit={payload.get('limit')}, start={payload.get('start')}, "
                    f"query={'есть' if query_text else 'пусто'}). "
                    "Попробуйте повторить поиск или ввести номер/название для более узкого поиска."
                ),
            }
        return result

    def _extract_items_total(self, data: Any) -> tuple[list[dict[str, Any]], int]:
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)], len(data)
        if not isinstance(data, dict):
            return [], 0

        for key in ("items", "procedures", "data", "content", "records", "results"):
            value = data.get(key)
            if isinstance(value, list):
                total = (
                    data.get("totalCount")
                    or data.get("total")
                    or data.get("count")
                    or data.get("recordsTotal")
                    or len(value)
                )
                return [x for x in value if isinstance(x, dict)], int(total or 0)
            if isinstance(value, dict):
                nested_items, nested_total = self._extract_items_total(value)
                if nested_items:
                    return nested_items, nested_total
        return [], int(data.get("totalCount") or data.get("total") or 0)

    def _title_text(self, proc: dict[str, Any]) -> str:
        return " ".join(
            str(proc.get(key) or "")
            for key in ("title", "name", "procedure_name", "lot_name")
        ).casefold()

    def _number_text(self, proc: dict[str, Any]) -> str:
        return " ".join(
            str(proc.get(key) or "")
            for key in ("registry_number", "procedure_number", "procedure_number2", "number")
        ).casefold()

    def _procedure_key(self, proc: dict[str, Any]) -> str:
        for key in ("id", "guid", "procedureId", "procedure_id", "number", "registry_number"):
            value = proc.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        return f"title:{self._title_text(proc)}"

    def _first_value(self, source: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
        return None

    def _active_lot(self, lots: list[Any]) -> dict[str, Any]:
        for lot in lots:
            if isinstance(lot, dict) and lot.get("actual"):
                return lot
        for lot in lots:
            if isinstance(lot, dict):
                return lot
        return {}

    def _rn_status_label(self, item: dict[str, Any], lot: dict[str, Any]) -> str:
        if item.get("date_archived") or lot.get("date_lot_archived"):
            return "В архиве"
        lot_step = str(lot.get("lot_step") or "").casefold()
        if lot_step == "registration":
            return (
                "Прием технико-коммерческого предложения"
                if item.get("force_tech_part")
                else "Прием коммерческого предложения"
            )
        if lot_step == "joint_applic_opened":
            return "Вскрытие заявок"
        if lot_step in {"second_parts", "second_parts_review"}:
            return "Рассмотрение заявок"
        if lot_step in {"summing_up", "summarizing", "results"}:
            return "Подведение итогов"

        status = lot.get("status")
        if status == 2:
            return (
                "Прием технико-коммерческого предложения"
                if item.get("force_tech_part")
                else "Прием коммерческого предложения"
            )
        state = str(
            self._first_value(
                item,
                "state",
                "status",
                "stage",
                "step_id",
                "procedure_status",
            )
            or ""
        )
        return {
            "Published": "Приём предложений",
            "ReviewOffers": "Подведение итогов",
            "procedureArchive": "В архиве",
            "ProcedureArchive": "В архиве",
            "Archived": "В архиве",
            "Canceled": "Процедура отменена",
            "Cancelled": "Процедура отменена",
            "1": "Прием коммерческого предложения",
        }.get(state, state)

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        org = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        if not org:
            org = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
        lots = (
            item.get("LotsList")
            if isinstance(item.get("LotsList"), list)
            else item.get("lots")
            if isinstance(item.get("lots"), list)
            else item.get("lot_list")
        )
        lots = lots if isinstance(lots, list) else []
        active_lot = self._active_lot(lots)
        regions = item.get("region") if isinstance(item.get("region"), list) else item.get("regions")
        regions = regions if isinstance(regions, list) else []
        region_names = [
            str(r.get("name") or r.get("title"))
            for r in regions
            if isinstance(r, dict) and (r.get("name") or r.get("title"))
        ]
        lot_items: list[str] = []
        for lot in lots:
            if not isinstance(lot, dict):
                continue
            if lot.get("name"):
                lot_items.append(str(lot["name"]))
            for pos in lot.get("items") or lot.get("positions") or []:
                if isinstance(pos, dict) and (pos.get("name") or pos.get("title")):
                    lot_items.append(str(pos.get("name") or pos.get("title")))

        state = str(active_lot.get("lot_step") or item.get("stage") or "")
        status_label = self._rn_status_label(item, active_lot)
        status_label = str(
            self._first_value(
                item,
                "step_name",
                "step_label",
                "status_name",
                "status_label",
                "state_name",
                "stage_name",
            )
            or status_label
            or ""
        )
        proc_id = self._first_value(item, "id", "procedureId", "procedure_id", "procedure")
        lot_id = self._first_value(
            active_lot,
            "id",
            "lotId",
            "lot_id",
            "lot",
            "lotNumber",
            "lot_number",
        )
        total_price = self._first_value(
            item,
            "total_price",
            "publish_total_price",
            "unpublish_total_price",
            "start_price",
            "initialSum",
            "initial_sum",
            "sum",
            "price",
            "nmck",
        )
        total_price_with_vat = (
            self._first_value(
                item,
                "total_price_with_vat",
                "total_price_with_nds",
                "publish_total_price",
                "unpublish_total_price",
                "start_price_with_vat",
                "start_price_with_nds",
                "initialSumWithVat",
                "initialSumWithVAT",
                "initialSumWithNds",
                "initialSumWithNDS",
                "initial_sum_with_vat",
                "initial_sum_with_nds",
                "sumWithVat",
                "sumWithVAT",
                "sumWithNds",
                "sumWithNDS",
                "priceWithVat",
                "priceWithVAT",
                "priceWithNds",
                "priceWithNDS",
            )
            or total_price
        )
        if str(total_price or "").strip() in {"-", "—"} and str(total_price_with_vat).strip() in {"0", "0.0", "0.00"}:
            total_price_with_vat = total_price
        registry_number = self._first_value(
            item,
            "registry_number",
            "registryNumber",
            "procedure_number",
            "procedureNumber",
            "number",
        )
        title = self._first_value(item, "title", "name", "procedure_name", "procedureName", "subject")
        short_name = self._first_value(
            org,
            "shortName",
            "short_name",
            "name",
            "fullName",
            "full_name",
        ) or self._first_value(
            item,
            "short_name",
            "organizer_short_name",
            "organizerShortName",
            "organizer_name",
            "organizerName",
            "customer_name",
            "customerName",
        )
        full_name = self._first_value(org, "fullName", "full_name", "name", "shortName", "short_name") or self._first_value(
            item,
            "full_name",
            "organizer_full_name",
            "organizerFullName",
            "organizer_name",
            "organizerName",
            "customer_full_name",
            "customerFullName",
            "customer_name",
            "customerName",
        )
        return {
            **item,
            "source": "tektorg_rn",
            "procedure_id": proc_id,
            "lot_id": lot_id,
            "registry_number": registry_number or "",
            "procedure_number": registry_number or "",
            "title": title or "",
            "trend_pur_label": "Маркетинговое исследование",
            "trend_pur_name": "Маркетинговое исследование",
            "step_id": state,
            "step_label": status_label,
            "status_label": status_label,
            "short_name": short_name or "",
            "full_name": full_name or short_name or "",
            "org_inn": self._first_value(org, "inn", "INN") or self._first_value(item, "org_inn", "organizer_inn", "customer_inn") or "",
            "org_kpp": self._first_value(org, "kpp", "KPP") or self._first_value(item, "org_kpp", "organizer_kpp", "customer_kpp") or "",
            "date_published": self._first_value(item, "date_published", "datePublication", "date_publication", "createdAt", "created_at"),
            "date_start_registration": self._first_value(
                item,
                "date_start_registration",
                "dateStartRegistration",
                "date_begin_registration",
                "createdAt",
                "created_at",
                "date_published",
                "datePublication",
            ),
            "date_end_registration": self._first_value(
                item,
                "date_end_registration",
                "dateEndRegistration",
                "date_finish_registration",
                "applications_deadline",
                "replyUntil",
                "reply_until",
                "endDate",
                "end_date",
            )
            or self._first_value(active_lot, "date_end_registration", "cur_step_end"),
            "date_results": self._first_value(item, "date_results", "date_result", "dateSummingUp", "acceptAt", "accept_at"),
            "total_price": total_price,
            "total_price_with_vat": total_price_with_vat,
            "currency_name": "RUB" if str(item.get("currency") or "") == "643" else str(item.get("currency") or item.get("currency_name") or "RUB"),
            "lots_count": self._first_value(item, "lots_count", "lot_count", "countActualLotItems", "count_actual_lot_items") or len(lots) or 1,
            "positions_count": sum(
                len(lot.get("items") or lot.get("positions") or [])
                for lot in lots
                if isinstance(lot, dict)
            ),
            "applics_count": item.get("countActualApplications") or item.get("countSubmittedApplications"),
            "region_name": ", ".join(region_names),
            "position_name": ", ".join(lot_items),
            "url": self._detail_url(proc_id),
            "tags": [
                "ТЭК-Торг РН",
                "Маркетинговое исследование",
                status_label,
            ],
        }

    def _fetch_normalized_page(
        self,
        start: int,
        limit: int,
        query: Optional[str],
        ignore_number_filter: bool = False,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        payload = self._list_payload(
            start=start,
            limit=limit,
            query=query,
            ignore_number_filter=ignore_number_filter,
        )
        result = self._request_page(payload)
        if result.get("no_session"):
            return [], 0, {"no_session": True}
        if not result.get("ok"):
            return [], 0, {
                "error": result.get("error") or result.get("text") or f"HTTP {result.get('status')}"
            }
        rpc_result = result.get("result")
        if not isinstance(rpc_result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            rpc_result = data.get("result") if isinstance(data.get("result"), dict) else data
        if isinstance(rpc_result, dict) and (rpc_result.get("no_session") or rpc_result.get("no_access")):
            return [], 0, {"no_session": True}
        items, total = self._extract_items_total(rpc_result)
        return [self._normalize_item(item) for item in items], total, {}

    def _number_search_rows(self, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        needle = str(self._filters.registry_contains or "").strip().casefold()
        if not needle:
            return [], {}

        rows, _, error = self._fetch_normalized_page(
            0,
            limit,
            query=None,
            ignore_number_filter=False,
        )
        if error:
            return [], error
        found = [row for row in rows if needle in self._number_text(row)]
        if found:
            return found, {}

        return [], {}

    def _keyword_cache_signature(self, limit: int) -> tuple[Any, ...]:
        f = self._filters
        return (
            tuple(k.casefold() for k in f.keywords if k.strip()),
            f.quick_search.casefold(),
            f.registry_contains.casefold(),
            f.title_contains.casefold(),
            f.published_from,
            f.published_to,
            f.end_from,
            f.end_to,
            f.results_from,
            f.results_to,
            f.price_min,
            f.price_max,
            f.applics_min,
            f.applics_max,
            limit,
        )

    def _keyword_search_rows(self, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        signature = self._keyword_cache_signature(limit)
        if signature == self._keyword_cache_key:
            return list(self._keyword_cache_rows), {}

        keywords = tuple(k for k in self._filters.keywords if k.strip())
        by_key: dict[str, dict[str, Any]] = {}
        for keyword in keywords:
            start = 0
            while True:
                rows, total, error = self._fetch_normalized_page(start, limit, keyword)
                if error:
                    return [], error
                keyword_cf = keyword.casefold()
                for row in rows:
                    if keyword_cf not in self._title_text(row):
                        continue
                    key = self._procedure_key(row)
                    existing = by_key.get(key)
                    if existing is None:
                        by_key[key] = row
                    else:
                        tags = list(existing.get("tags") or [])
                        for tag in row.get("tags") or []:
                            if tag not in tags:
                                tags.append(tag)
                        existing["tags"] = tags
                next_start = start + len(rows)
                if not rows or (total and next_start >= total):
                    break
                time.sleep(self.request_delay_seconds)
                start = next_start

        rows = list(by_key.values())
        self._keyword_cache_key = signature
        self._keyword_cache_rows = rows
        return list(rows), {}

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
        request_limit = min(
            self.max_page_limit,
            max(1, int(limit or HARD_SERVER_LIMIT)),
        )
        title_query = str(query or self._filters.quick_search or self._filters.title_contains or "").strip()
        if title_query:
            rows, total, error = self._fetch_normalized_page(start, request_limit, title_query)
            if error.get("no_session"):
                return {
                    "success": False,
                    "no_session": True,
                    "message": "Нет активной сессии ТЭК-Торг РН.",
                    "procedures": [],
                    "totalCount": None,
                }
            if error:
                return {
                    "success": False,
                    "error": error.get("error") or "Не удалось выполнить поиск по названию.",
                    "procedures": [],
                    "totalCount": None,
                }
            return {
                "success": True,
                "procedures": rows,
                "totalCount": total,
            }

        if self._filters.registry_contains and not query:
            rows, error = self._number_search_rows(request_limit)
            if error.get("no_session"):
                return {
                    "success": False,
                    "no_session": True,
                    "message": "Нет активной сессии ТЭК-Торг РН.",
                    "procedures": [],
                    "totalCount": None,
                }
            if error:
                return {
                    "success": False,
                    "error": error.get("error") or "Не удалось выполнить поиск по номеру.",
                    "procedures": [],
                    "totalCount": None,
                }
            return {
                "success": True,
                "procedures": rows[start : start + request_limit],
                "totalCount": len(rows),
            }

        if self._filters.keyword_search_enabled and self._filters.keywords and not query:
            rows, error = self._keyword_search_rows(request_limit)
            if error.get("no_session"):
                return {
                    "success": False,
                    "no_session": True,
                    "message": "Нет активной сессии ТЭК-Торг РН.",
                    "procedures": [],
                    "totalCount": None,
                }
            if error:
                return {
                    "success": False,
                    "error": error.get("error") or "Не удалось выполнить поиск по ключевым словам.",
                    "procedures": [],
                    "totalCount": None,
                }
            return {
                "success": True,
                "procedures": rows[start : start + request_limit],
                "totalCount": len(rows),
            }

        rows, total, error = self._fetch_normalized_page(start, request_limit, query)
        if error.get("no_session"):
            return {
                "success": False,
                "no_session": True,
                "message": "Нет активной сессии ТЭК-Торг РН.",
                "procedures": [],
                "totalCount": None,
            }
        if error:
            return {
                "success": False,
                "error": error.get("error") or "Не удалось получить список процедур.",
                "procedures": [],
                "totalCount": None,
            }
        return {
            "success": True,
            "procedures": rows,
            "totalCount": total,
        }
