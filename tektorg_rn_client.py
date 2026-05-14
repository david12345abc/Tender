from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional

from desktop_app.commercial_extractor import CommercialTerms, extract_commercial_terms
from desktop_app.params import ClientFilters
from desktop_app.supplier_classifier import SupplierCharacteristic, classify_supplier_characteristic
from etp_client import HARD_SERVER_LIMIT, EtpClient


TEKTORG_RN_URL = "https://rn.tektorg.ru/#com/procedure/index/type/market_survey"
MARKET_SURVEY_TYPE = "market_survey"
RPC_ENDPOINT = "/index.php?rpctype=direct&module=default"


class ApplicationLetterManualInputRequired(RuntimeError):
    def __init__(self, fields: list[str], message: str = "") -> None:
        self.fields = fields
        super().__init__(message or "Требуется ручное заполнение обязательных полей письма заявки.")


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
  const fileExt = /\.(?:001|002|003|004|005|006|007|008|docx?|docm|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv|jpg|jpeg|gif|png|tiff?|sgn)(?:[?#]|\s|$)/i;
  const fileLike = (href, text) => {
    const value = `${href || ""} ${text || ""}`;
    return /\/file\/get\//i.test(value)
      || fileExt.test(value);
  };

  for (let i = 0; i < 80; i++) {
    const anchors = Array.from(document.querySelectorAll("a[href]"));
    if (anchors.some((a) => fileLike(a.href, a.innerText || a.textContent || ""))) {
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

  function nearestSection(anchor) {
    let node = anchor;
    for (let i = 0; node && i < 8; i++, node = node.parentElement) {
      const legend = node.querySelector && node.querySelector("legend");
      const legendText = (legend && (legend.innerText || legend.textContent) || "").trim();
      if (legendText) return legendText;
      const header = node.querySelector && node.querySelector(".x-panel-header-text, .x-fieldset-header-text, h1, h2, h3, h4");
      const headerText = (header && (header.innerText || header.textContent) || "").trim();
      if (headerText) return headerText;
    }
    return "Документы процедуры";
  }

  for (const anchor of Array.from(document.querySelectorAll("a[href]"))) {
    push(anchor, nearestSection(anchor));
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
        """Скачивает все файловые ссылки, найденные на странице процедуры."""
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

        timings: list[dict[str, Any]] = []
        workflow_started = time.perf_counter()

        def record_timing(label: str, started: float, ok: bool = True) -> None:
            timings.append(
                {
                    "label": label,
                    "seconds": round(time.perf_counter() - started, 3),
                    "ok": ok,
                }
            )

        def run_background(label: str, fn: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
            started = time.perf_counter()
            try:
                return fn(), {
                    "label": f"Фоново: {label}",
                    "seconds": round(time.perf_counter() - started, 3),
                    "ok": True,
                }
            except Exception as e:
                return e, {
                    "label": f"Фоново: {label}",
                    "seconds": round(time.perf_counter() - started, 3),
                    "ok": False,
                }

        analysis_executor: ThreadPoolExecutor | None = None
        supplier_future: Future[tuple[Any, dict[str, Any]]] | None = None
        commercial_terms_future: Future[tuple[Any, dict[str, Any]]] | None = None

        if progress:
            progress("Открываю вкладку подачи заявки для загрузки технических файлов...")
        step_started = time.perf_counter()
        self._switch_to_application_tab(application_url, progress=progress)
        record_timing(f"Переход на страницу подачи заявки: {application_url}", step_started)
        uploaded: list[str] = []
        errors: list[str] = []
        if progress:
            progress("Проверяю наличие вкладки технической части предложения...")
        step_started = time.perf_counter()
        has_technical_tab = self._has_technical_tab_button()
        record_timing("Проверка наличия технической части предложения", step_started)
        if progress:
            progress("Запускаю фоновое распознавание коммерческих условий...")
        step_started = time.perf_counter()
        analysis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tektorg-analysis")
        if has_technical_tab:
            supplier_future = analysis_executor.submit(
                run_background,
                "определение характеристики поставщика",
                lambda: self._classify_supplier_characteristic(technical_dir, progress=None),
            )
        commercial_terms_future = analysis_executor.submit(
            run_background,
            "распознавание итоговой стоимости и срока действия",
            lambda: self._extract_commercial_terms_for_application(technical_dir, progress=None),
        )
        record_timing("Запуск фонового распознавания документов", step_started)

        if has_technical_tab:
            if progress:
                progress("Ожидаю блок технической части заявки...")
            step_started = time.perf_counter()
            try:
                self._ensure_technical_tab_active()
                record_timing("Ожидание технической части предложения", step_started)
            except Exception:
                if self._has_technical_tab_button():
                    raise
                has_technical_tab = False
                if progress:
                    progress("Видимая вкладка «Техническая часть предложения» не найдена, пропускаю технический этап.")
                record_timing("Техническая часть предложения отсутствует, этап пропущен", step_started)

        if has_technical_tab:
            step_started = time.perf_counter()
            self._clear_uploaded_files(progress=progress)
            record_timing("Очистка ранее загруженных технических файлов", step_started)
            for index, path in enumerate(files, start=1):
                if progress:
                    progress(f"Загружаю технический файл {index}/{len(files)}: {path.name}")
                step_started = time.perf_counter()
                try:
                    self._upload_one_file(path, progress=progress)
                    self._remove_duplicate_uploaded_files(progress=progress)
                    if not self._is_uploaded_file_listed(path):
                        raise RuntimeError("файл не появился в списке загруженных документов")
                    uploaded.append(str(path))
                    record_timing(f"Загрузка технического файла {index}: {path.name}", step_started)
                except Exception as e:
                    record_timing(f"Загрузка технического файла {index}: {path.name}", step_started, ok=False)
                    errors.append(f"{path.name}: {e}")
                    break
            step_started = time.perf_counter()
            self._remove_duplicate_uploaded_files(progress=progress)
            record_timing("Финальная проверка дублей технических файлов", step_started)
        else:
            if progress:
                progress("Вкладка «Техническая часть предложения» отсутствует, пропускаю загрузку технических файлов.")
            record_timing("Техническая часть предложения отсутствует, этап пропущен", time.perf_counter())
        commercial_terms: CommercialTerms | None = None
        supplier_characteristic: SupplierCharacteristic | None = None
        commercial_upload: dict[str, Any] = {}
        manual_letter_required_fields: list[str] = []
        if not errors and has_technical_tab:
            step_started = time.perf_counter()
            try:
                if supplier_future is not None:
                    supplier_value, supplier_timing = supplier_future.result()
                    timings.append(supplier_timing)
                    if isinstance(supplier_value, Exception):
                        raise supplier_value
                    supplier_characteristic = supplier_value
                else:
                    supplier_characteristic = self._classify_supplier_characteristic(technical_dir, progress=progress)
                if supplier_characteristic.label:
                    if progress:
                        progress(f"Выбираю характеристику поставщика: {supplier_characteristic.label}")
                    self._select_supplier_characteristic(supplier_characteristic.label)
                    record_timing(
                        f"Определение и выбор характеристики поставщика: {supplier_characteristic.label}",
                        step_started,
                    )
                else:
                    record_timing("Определение характеристики поставщика", step_started, ok=False)
                    errors.append("Характеристика поставщика: не удалось определить подходящий пункт.")
            except Exception as e:
                record_timing("Определение и выбор характеристики поставщика", step_started, ok=False)
                errors.append(f"Характеристика поставщика: {e}")
        elif not has_technical_tab:
            record_timing("Характеристика поставщика пропущена вместе с технической частью", time.perf_counter())
        if not errors:
            if progress:
                progress("Перехожу на вкладку коммерческой части предложения...")
            step_started = time.perf_counter()
            self._ensure_commercial_tab_active()
            record_timing("Переход на вкладку коммерческой части предложения", step_started)
            try:
                step_started = time.perf_counter()
                commercial_upload = self._upload_commercial_documents(technical_dir, progress=progress)
                for timing in commercial_upload.get("timings") or []:
                    if isinstance(timing, dict):
                        timings.append(timing)
                if commercial_upload.get("errors"):
                    record_timing("Загрузка коммерческих документов", step_started, ok=False)
                    errors.extend(str(error) for error in commercial_upload.get("errors") or [])
                else:
                    commercial_uploaded = len(commercial_upload.get("uploaded") or [])
                    record_timing(f"Загрузка коммерческих документов: {commercial_uploaded}", step_started)
            except Exception as e:
                record_timing("Загрузка коммерческих документов", step_started, ok=False)
                errors.append(f"Коммерческие документы: {e}")
        if not errors:
            if progress:
                progress("Открываю окно формирования письма о подаче заявки...")
            step_started = time.perf_counter()
            self._open_application_letter_modal()
            record_timing("Открытие окна формирования письма", step_started)
            step_started = time.perf_counter()
            self._fill_application_letter_defaults()
            record_timing("Заполнение константных значений письма", step_started)
            try:
                step_started = time.perf_counter()
                if commercial_terms_future is not None:
                    commercial_value, commercial_timing = commercial_terms_future.result()
                    timings.append(commercial_timing)
                    if isinstance(commercial_value, Exception):
                        raise commercial_value
                    commercial_terms = commercial_value
                else:
                    commercial_terms = self._extract_commercial_terms_for_application(technical_dir, progress=progress)
                if not commercial_terms.validity_date:
                    commercial_terms.validity_date = self._default_offer_validity_date()
                record_timing("Получение результата распознавания итоговой стоимости и срока действия", step_started)
                if commercial_terms.price_with_vat or commercial_terms.price_without_vat or commercial_terms.validity_date:
                    step_started = time.perf_counter()
                    self._fill_application_letter_commercial_terms(commercial_terms)
                    record_timing("Заполнение распознанных коммерческих условий", step_started)
                    step_started = time.perf_counter()
                    letter_status = self.application_letter_required_fields_status()
                    manual_letter_required_fields = self._normalize_manual_letter_fields(
                        letter_status.get("missing") or []
                    )
                    if manual_letter_required_fields:
                        if progress:
                            progress("Ожидаю ручное заполнение обязательных полей письма заявки.")
                        record_timing("Письмо ожидает ручное заполнение обязательных полей", step_started)
                    else:
                        self._save_application_letter_modal()
                        record_timing("Сохранение письма о подаче заявки", step_started)
                else:
                    errors.append("Коммерческая часть: не удалось уверенно найти итоговую стоимость или срок действия предложения.")
            except ApplicationLetterManualInputRequired as e:
                manual_letter_required_fields = self._normalize_manual_letter_fields(e.fields, str(e))
                if progress:
                    progress("Ожидаю ручное заполнение обязательных полей письма заявки.")
                record_timing("Письмо ожидает ручное заполнение обязательных полей", step_started)
            except Exception as e:
                record_timing("Распознавание/заполнение коммерческих условий", step_started, ok=False)
                errors.append(f"Коммерческая часть: {e}")
        if analysis_executor is not None:
            analysis_executor.shutdown(wait=False, cancel_futures=True)
        record_timing("Весь сценарий после распределения файлов", workflow_started, ok=not errors)
        return {
            "uploaded": uploaded,
            "errors": errors,
            "commercial_terms": commercial_terms.as_dict() if commercial_terms else {},
            "supplier_characteristic": supplier_characteristic.as_dict() if supplier_characteristic else {},
            "commercial_upload": commercial_upload,
            "manual_letter_required_fields": manual_letter_required_fields,
            "timings": timings,
        }

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

    def _application_offer_tabs_state(self) -> dict[str, Any]:
        assert self.driver is not None
        script = r"""
const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").replace(/\s+/g, " ").trim();
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const sameText = (text, pattern) => pattern.test(String(text || "").replace(/\s+/g, " ").trim());
const tabContainers = () => {
  const selectors = [
    ".x-tab-panel-header ul.x-tab-strip li:not(.x-tab-edge)",
    "ul.x-tab-strip li:not(.x-tab-edge)",
    "[role='tab']",
    ".x-tab-strip-text"
  ];
  const items = [];
  for (const selector of selectors) {
    for (const el of Array.from(document.querySelectorAll(selector))) {
      if (!visible(el)) continue;
      const container = el.closest && el.closest("li:not(.x-tab-edge), [role='tab']");
      const node = container && visible(container) ? container : el;
      const text = textOf(node);
      if (!text) continue;
      items.push({ node, text });
    }
  }
  const seen = new Set();
  return items.filter((item) => {
    const rect = item.node.getBoundingClientRect();
    const key = `${item.text}:${Math.round(rect.left)}:${Math.round(rect.top)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
};
const tabs = tabContainers();
const labels = tabs.map((item) => item.text);
const hasTechnicalDom = labels.some((text) => sameText(text, /^Техническая\s+часть\s+предложения$/i));
const hasCommercialDom = labels.some((text) => sameText(text, /^Коммерческая\s+часть\s+предложения$/i));
return {
  hasTechnical: hasTechnicalDom,
  hasCommercial: hasCommercialDom,
  labels,
};
"""
        result = self.driver.execute_script(script)
        return result if isinstance(result, dict) else {"hasTechnical": False, "hasCommercial": False, "labels": []}

    def _has_technical_tab_button(self) -> bool:
        deadline = time.time() + 2.0
        last_result: Any = None
        while time.time() < deadline:
            last_result = self._application_offer_tabs_state()
            if isinstance(last_result, dict):
                if last_result.get("hasTechnical"):
                    return True
                if last_result.get("hasCommercial"):
                    return False
            time.sleep(0.25)
        if isinstance(last_result, dict) and not last_result.get("hasTechnical"):
            return False
        return False

    def _ensure_technical_tab_active(self) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").replace(/\s+/g, " ").trim();
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  };
  const findVisibleTechnicalTab = () => Array.from(document.querySelectorAll(
    ".x-tab-panel-header ul.x-tab-strip li:not(.x-tab-edge), ul.x-tab-strip li:not(.x-tab-edge), .x-tab-strip-text, [role='tab']"
  )).find((el) => {
    if (!visible(el) || textOf(el) !== "Техническая часть предложения") return false;
    const tab = el.closest && el.closest("li:not(.x-tab-edge), [role='tab']");
    return !!(tab && visible(tab));
  });
  const visibleOfferTabLabels = () => Array.from(document.querySelectorAll(
    ".x-tab-panel-header ul.x-tab-strip li:not(.x-tab-edge), ul.x-tab-strip li:not(.x-tab-edge), .x-tab-strip-text, [role='tab']"
  ))
    .filter(visible)
    .map((el) => textOf((el.closest && el.closest("li:not(.x-tab-edge), [role='tab']")) || el))
    .filter(Boolean);
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
  for (let i = 0; i < 12; i++) {
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
    const tab = findVisibleTechnicalTab();
    if (tab) {
      clickBest(tab);
    } else {
      const labels = visibleOfferTabLabels();
      const hasCommercial = labels.some((text) => /^Коммерческая\s+часть\s+предложения$/i.test(text));
      const hasTechnical = labels.some((text) => /^Техническая\s+часть\s+предложения$/i.test(text));
      if (hasCommercial && !hasTechnical) {
        callback(false);
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
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
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

    def _fill_application_letter_defaults(self) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const values = {
    number: "1",
    nds_name: "22%",
    other_price_text: "Доставка, упаковка, таможенные платежи, другие обязательные расходы.",
  };
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const modalOpened = () => Array.from(document.querySelectorAll(".x-window, .x-window-body"))
    .some((el) => visible(el) && /Письмо\s+о\s+подаче\s+заявки/i.test(String(el.innerText || el.textContent || "")));
  const inLetterWindow = (cmp) => {
    const el = cmp && cmp.el && cmp.el.dom;
    const win = el && el.closest && el.closest(".x-window");
    return !!(win && visible(win) && /Письмо\s+о\s+подаче\s+заявки/i.test(String(win.innerText || win.textContent || "")));
  };
  const findField = (name) => {
    if (window.Ext && Ext.ComponentMgr && Ext.ComponentMgr.all) {
      let found = null;
      Ext.ComponentMgr.all.each(function(cmp) {
        if (!found && cmp.name === name && !cmp.hidden && !cmp.disabled && inLetterWindow(cmp)) found = cmp;
      });
      if (found) return found;
    }
    return null;
  };
  const setComboByDisplayName = (name, displayValue) => {
    const cmp = findField(name);
    if (!cmp || !cmp.store) return false;
    let record = null;
    try {
      cmp.store.each(function(item) {
        const value = String(item.get ? item.get(cmp.displayField || "name") : item.data && item.data.name || "");
        if (!record && value === displayValue) record = item;
      });
    } catch (e) {}
    if (!record) return false;
    const value = record.get ? record.get(cmp.valueField || "id") : record.data && record.data.id;
    if (cmp.setValue) cmp.setValue(value);
    if (cmp.setRawValue) cmp.setRawValue(displayValue);
    if (cmp.fireEvent) {
      cmp.fireEvent("select", cmp, record, 0);
      cmp.fireEvent("change", cmp, value);
      cmp.fireEvent("blur", cmp);
    }
    if (cmp.el && cmp.el.dom) {
      cmp.el.dom.dispatchEvent(new Event("input", { bubbles: true }));
      cmp.el.dom.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return String(cmp.getRawValue ? cmp.getRawValue() : displayValue) === displayValue;
  };
  const setField = (name, value) => {
    const cmp = findField(name);
    if (cmp) {
      if (cmp.setValue) cmp.setValue(value);
      if (cmp.fireEvent) {
        cmp.fireEvent("change", cmp, value);
        cmp.fireEvent("blur", cmp);
      }
      if (cmp.el && cmp.el.dom) {
        cmp.el.dom.dispatchEvent(new Event("input", { bubbles: true }));
        cmp.el.dom.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return true;
    }
    const input = document.querySelector(`[name="${name}"]`);
    if (!input) return false;
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  };
  for (let i = 0; i < 40; i++) {
    if (modalOpened()) {
      const numberOk = setField("number", values.number);
      const ndsOk = setComboByDisplayName("nds_id", values.nds_name);
      const expensesOk = setField("other_price_text", values.other_price_text);
      if (numberOk && ndsOk && expensesOk) {
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
            raise RuntimeError("Не удалось заполнить поля письма о подаче заявки.")

    def _extract_commercial_terms_for_application(
        self,
        technical_dir: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> CommercialTerms:
        commercial_dir = technical_dir.parent / "Коммерческие"
        if progress:
            progress(f"Анализирую коммерческие документы: {commercial_dir}")
        return extract_commercial_terms(commercial_dir, progress=progress)

    def _classify_supplier_characteristic(
        self,
        technical_dir: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> SupplierCharacteristic:
        return classify_supplier_characteristic(technical_dir, progress=progress)

    def _upload_commercial_documents(
        self,
        technical_dir: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        commercial_dir = technical_dir.parent / "Коммерческие"
        files = [
            path for path in sorted(commercial_dir.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and not path.name.startswith("~$")
        ] if commercial_dir.exists() else []
        if not files:
            return {"uploaded": [], "errors": [f"В папке нет коммерческих файлов: {commercial_dir}"]}

        panel_id = "application_docs_com_1"
        self._clear_uploaded_files(
            progress=progress,
            upload_panel_id=panel_id,
            description="коммерческий",
        )

        uploaded: list[str] = []
        errors: list[str] = []
        timings: list[dict[str, Any]] = []

        def record_timing(label: str, started: float, ok: bool = True) -> None:
            timings.append(
                {
                    "label": label,
                    "seconds": round(time.perf_counter() - started, 3),
                    "ok": ok,
                }
            )

        for index, path in enumerate(files, start=1):
            if progress:
                progress(f"Загружаю коммерческий файл {index}/{len(files)}: {path.name}")
            step_started = time.perf_counter()
            try:
                self._upload_one_file(
                    path,
                    progress=progress,
                    upload_panel_id=panel_id,
                    area_name="коммерческой части заявки",
                )
                self._remove_duplicate_uploaded_files(
                    progress=progress,
                    upload_panel_id=panel_id,
                    description="коммерческих документов",
                )
                if not self._is_uploaded_file_listed(path, upload_panel_id=panel_id):
                    raise RuntimeError("файл не появился в списке коммерческих документов")
                uploaded.append(str(path))
                record_timing(f"Загрузка коммерческого файла {index}: {path.name}", step_started)
            except Exception as e:
                record_timing(f"Загрузка коммерческого файла {index}: {path.name}", step_started, ok=False)
                errors.append(f"{path.name}: {e}")
                break
        self._remove_duplicate_uploaded_files(
            progress=progress,
            upload_panel_id=panel_id,
            description="коммерческих документов",
        )
        return {"uploaded": uploaded, "errors": errors, "timings": timings}

    def _select_supplier_characteristic(self, label: str) -> None:
        assert self.driver is not None
        script = r"""
const callback = arguments[arguments.length - 1];
const targetLabel = String(arguments[0] || "");
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const norm = (value) => String(value || "")
    .toLowerCase()
    .replace(/mtr|mtp/g, "мтр")
    .replace(/\s+/g, " ")
    .trim();
  const target = norm(targetLabel);
  const targetCode = target.startsWith("06") ? "06" : "01";
  const targetText = targetCode === "06" ? "исполнитель услуг" : "производитель";
  const eachCmp = (fn) => {
    if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return;
    const all = Ext.ComponentMgr.all;
    if (typeof all.each === "function") {
      all.each(fn);
      return;
    }
    const items = all.items || all.getRange && all.getRange() || all.map && Object.values(all.map) || Object.values(all);
    for (const cmp of items) {
      if (cmp && typeof cmp === "object") fn(cmp);
    }
  };
  const labelOf = (cmp) => norm([
    cmp.fieldLabel,
    cmp.boxLabel,
    cmp.name,
    cmp.id,
    cmp.el && cmp.el.dom && (cmp.el.dom.innerText || cmp.el.dom.textContent || cmp.el.dom.value),
  ].filter(Boolean).join(" "));
  const findCombo = () => {
    let found = null;
    eachCmp((cmp) => {
      if (found || cmp.hidden || cmp.disabled) return;
      const label = labelOf(cmp);
      if (/характеристика поставщика/i.test(label) || /supplier/i.test(label)) found = cmp;
    });
    if (found) return found;
    const nodes = Array.from(document.querySelectorAll("label, td, div, span"));
    const node = nodes.find((el) => /характеристика\s+поставщика/i.test(norm(el.innerText || el.textContent || "")));
    if (!node) return null;
    const input = findInputNearNode(node);
    if (!input) return null;
    eachCmp((cmp) => {
      if (found) return;
      const dom = cmp.el && cmp.el.dom;
      const inputEl = cmp.inputEl && cmp.inputEl.dom;
      if ((inputEl && inputEl === input) || (dom && dom.contains(input))) found = cmp;
    });
    return found;
  };
  const findInputNearNode = (node) => {
    const containers = [
      node.closest("tr"),
      node.parentElement,
      node.parentElement && node.parentElement.parentElement,
      node.parentElement && node.parentElement.parentElement && node.parentElement.parentElement.parentElement,
    ].filter(Boolean);
    for (const container of containers) {
      const input = container.querySelector("input:not([type='hidden']):not([disabled]), textarea:not([disabled])");
      if (input) return input;
    }
    const labelRect = node.getBoundingClientRect();
    const candidates = Array.from(document.querySelectorAll("input:not([type='hidden']):not([disabled])"))
      .filter((input) => visible(input))
      .map((input) => {
        const rect = input.getBoundingClientRect();
        const sameLine = Math.abs((rect.top + rect.height / 2) - (labelRect.top + labelRect.height / 2));
        const toRight = rect.left >= labelRect.left;
        return { input, score: sameLine + (toRight ? 0 : 1000) + Math.max(0, labelRect.right - rect.left) };
      })
      .sort((a, b) => a.score - b.score);
    return candidates.length ? candidates[0].input : null;
  };
  const recordText = (record, combo) => {
    try {
      const fields = ["full_name", combo.displayField, "name", "title", "text", "value", "label"].filter(Boolean);
      for (const field of fields) {
        const value = record.get ? record.get(field) : record.data && record.data[field];
        if (value) return String(value);
      }
      return JSON.stringify(record.data || {});
    } catch (e) {
      return "";
    }
  };
  const recordValue = (record, combo) => {
    const fields = [combo.valueField, "id", "value", "code"].filter(Boolean);
    for (const field of fields) {
      const value = record.get ? record.get(field) : record.data && record.data[field];
      if (value !== undefined && value !== null && value !== "") return value;
    }
    return recordText(record, combo);
  };
  const findRecord = (combo) => {
    if (!combo || !combo.store) return null;
    let found = null;
    try {
      combo.store.each((record) => {
        const data = record.data || {};
        const code = String(record.get ? record.get("code") || "" : data.code || "");
        const fullName = norm(record.get ? record.get("full_name") || "" : data.full_name || "");
        const name = norm(record.get ? record.get("name") || "" : data.name || "");
        const text = norm(recordText(record, combo));
        if (found) return;
        if (
          code === targetCode
          || fullName.startsWith(targetCode)
          || text.startsWith(targetCode)
          || name.includes(targetText)
          || text.includes(targetText)
        ) found = record;
      });
    } catch (e) {}
    return found;
  };
  const setComboRecord = (combo, record) => {
    if (!combo || !record) return false;
    const value = recordValue(record, combo);
    const text = recordText(record, combo);
    const oldValue = combo.getValue ? combo.getValue() : undefined;
    if (combo.setValue) combo.setValue(value);
    if (combo.setRawValue) combo.setRawValue(text);
    if (combo.validate) combo.validate();
    if (combo.clearInvalid) combo.clearInvalid();
    if (combo.fireEvent) {
      combo.fireEvent("select", combo, record, 0);
      combo.fireEvent("change", combo, combo.getValue ? combo.getValue() : value, oldValue);
      combo.fireEvent("blur", combo);
    }
    const input = combo.inputEl && combo.inputEl.dom || combo.el && combo.el.dom && combo.el.dom.querySelector("input");
    if (input) {
      input.value = text;
      input.setAttribute("value", text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("blur", { bubbles: true }));
    }
    const raw = norm(combo.getRawValue ? combo.getRawValue() : input && input.value || "");
    const currentValue = String(combo.getValue ? combo.getValue() || "" : "");
    return raw.startsWith(targetCode) || raw.includes(targetText) || currentValue === String(value);
  };
  const setSupplierComboByName = () => {
    let combo = null;
    eachCmp((cmp) => {
      if (!combo && cmp.name === "supplier_type_srm_id" && !cmp.hidden && !cmp.disabled) combo = cmp;
    });
    if (!combo || !combo.store) return false;
    let record = null;
    try {
      combo.store.each((item) => {
        if (record) return;
        const data = item.data || {};
        const code = String(item.get ? item.get("code") || "" : data.code || "");
        const fullName = norm(item.get ? item.get("full_name") || "" : data.full_name || "");
        const name = norm(item.get ? item.get("name") || "" : data.name || "");
        if (code === targetCode || fullName.startsWith(targetCode) || name.includes(targetText)) record = item;
      });
    } catch (e) {}
    if (!record) return false;
    const value = recordValue(record, combo);
    const text = record.get ? (record.get("full_name") || record.get(combo.displayField || "name") || record.get("name")) : recordText(record, combo);
    const oldValue = combo.getValue ? combo.getValue() : undefined;
    if (combo.setValue) combo.setValue(value);
    if (combo.setRawValue) combo.setRawValue(text);
    if (combo.validate) combo.validate();
    if (combo.clearInvalid) combo.clearInvalid();
    if (combo.fireEvent) {
      combo.fireEvent("select", combo, record, 0);
      combo.fireEvent("change", combo, combo.getValue ? combo.getValue() : value, oldValue);
      combo.fireEvent("blur", combo);
    }
    const input = combo.inputEl && combo.inputEl.dom || combo.el && combo.el.dom && combo.el.dom.querySelector("input");
    if (input) {
      input.value = text;
      input.setAttribute("value", text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("blur", { bubbles: true }));
    }
    const raw = norm(combo.getRawValue ? combo.getRawValue() : input && input.value || "");
    return raw.startsWith(targetCode) || raw.includes(targetText) || String(combo.getValue ? combo.getValue() || "" : "") === String(value);
  };
  if (setSupplierComboByName()) {
    callback(true);
    return;
  }
  for (let i = 0; i < 50; i++) {
    const combo = findCombo();
    if (combo) {
      try {
        if (combo.store && combo.store.getCount && combo.store.getCount() === 0 && combo.store.load) {
          combo.store.load();
          await wait(600);
        }
      } catch (e) {}
      const record = findRecord(combo);
      if (setComboRecord(combo, record)) {
        callback(true);
        return;
      }
    }
    await wait(250);
  }
  callback(false);
})();
"""
        ok = self.driver.execute_async_script(script, label)
        if not ok:
            raise RuntimeError(f"Не удалось выбрать характеристику поставщика: {label}")

    def _fill_application_letter_commercial_terms(self, terms: CommercialTerms) -> None:
        assert self.driver is not None

        def ui_price(value: str) -> str:
            return value.replace(".", ",") if value else ""

        values = {
            "price_with_vat": ui_price(terms.as_dict().get("price_with_vat", "")),
            "price_without_vat": ui_price(terms.as_dict().get("price_without_vat", "")),
            "validity_date": terms.validity_date,
        }
        script = r"""
const callback = arguments[arguments.length - 1];
const values = arguments[0] || {};
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const letterText = /Письмо\s+о\s+подаче\s+заявки/i;
  const inLetterWindow = (cmp) => {
    const el = cmp && cmp.el && cmp.el.dom;
    const win = el && el.closest && el.closest(".x-window");
    return !!(win && visible(win) && letterText.test(String(win.innerText || win.textContent || "")));
  };
  const eachCmp = (fn) => {
    if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return;
    const all = Ext.ComponentMgr.all;
    if (typeof all.each === "function") {
      all.each(fn);
      return;
    }
    const items = all.items || all.getRange && all.getRange() || all.map && Object.values(all.map) || Object.values(all);
    for (const cmp of items) {
      if (cmp && typeof cmp === "object") fn(cmp);
    }
  };
  const norm = (value) => String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
  const labelOf = (cmp) => norm([
    cmp.fieldLabel,
    cmp.boxLabel,
    cmp.name,
    cmp.id,
    cmp.el && cmp.el.dom && (cmp.el.dom.innerText || cmp.el.dom.textContent || cmp.el.dom.value),
  ].filter(Boolean).join(" "));
  const findField = (names, labelPatterns) => {
    let found = null;
    eachCmp((cmp) => {
      if (found || cmp.hidden || cmp.disabled || !inLetterWindow(cmp)) return;
      const name = norm(cmp.name);
      const label = labelOf(cmp);
      if (names.some((item) => name === norm(item))) {
        found = cmp;
        return;
      }
      if (labelPatterns.some((pattern) => pattern.test(label))) found = cmp;
    });
    return found;
  };
  const letterWindow = () => Array.from(document.querySelectorAll(".x-window"))
    .find((el) => visible(el) && letterText.test(String(el.innerText || el.textContent || "")));
  const setDomValue = (input, value) => {
    if (!input || !value) return false;
    input.scrollIntoView({ block: "center", inline: "nearest" });
    input.focus && input.focus();
    input.value = value;
    input.setAttribute("value", value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new Event("blur", { bubbles: true }));
    let cmp = null;
    eachCmp((item) => {
      if (cmp || !inLetterWindow(item)) return;
      const dom = item.el && item.el.dom;
      if (dom && (dom === input || dom.contains(input))) cmp = item;
    });
    if (cmp) {
      if (cmp.setValue) cmp.setValue(value);
      if (cmp.setRawValue) cmp.setRawValue(value);
      fire(cmp, value);
    }
    const actual = String(input.value || "").replace(/\s+/g, "").replace(".", ",");
    const expected = String(value || "").replace(/\s+/g, "").replace(".", ",");
    return actual === expected;
  };
  const normalizeMoney = (value) => String(value || "")
    .replace(/\s+/g, "")
    .replace(/\u00a0/g, "")
    .replace(",", ".")
    .replace(/[^\d.]/g, "");
  const normalizeText = (value) => String(value || "").replace(/\s+/g, "").trim();
  const setByInputName = (name, value, isMoney = false) => {
    if (!value) return false;
    let changed = false;
    eachCmp((cmp) => {
      try {
        if (cmp.name !== name) return;
        if (cmp.setValue) cmp.setValue(value);
        if (cmp.setRawValue) cmp.setRawValue(value);
        if (cmp.validate) cmp.validate();
        fire(cmp, value);
      } catch (e) {}
    });
    const inputs = Array.from(document.querySelectorAll(`input[name="${name}"], textarea[name="${name}"]`))
      .filter((input) => !input.disabled && visible(input));
    for (const input of inputs) {
      input.classList.remove("x-form-empty-field");
      input.value = value;
      input.setAttribute("value", value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.dispatchEvent(new Event("blur", { bubbles: true }));
      changed = true;
    }
    const expected = isMoney ? normalizeMoney(value) : normalizeText(value);
    return changed && inputs.some((input) => {
      const actual = isMoney ? normalizeMoney(input.value) : normalizeText(input.value);
      return actual === expected;
    });
  };
  const findInputNearLabel = (labelPattern) => {
    const win = letterWindow();
    if (!win) return null;
    const nodes = Array.from(win.querySelectorAll("label, td, div, span"));
    for (const node of nodes) {
      const text = norm(node.innerText || node.textContent || "");
      if (!labelPattern.test(text)) continue;
      const containers = [
        node.closest("tr"),
        node.parentElement,
        node.parentElement && node.parentElement.parentElement,
        node.parentElement && node.parentElement.parentElement && node.parentElement.parentElement.parentElement,
      ].filter(Boolean);
      for (const container of containers) {
        const input = container.querySelector("input:not([type='hidden']):not([disabled]), textarea:not([disabled])");
        if (input) return input;
      }
      let cursor = node;
      for (let i = 0; i < 8 && cursor; i++) {
        cursor = cursor.nextElementSibling;
        const input = cursor && cursor.querySelector && cursor.querySelector("input:not([type='hidden']):not([disabled]), textarea:not([disabled])");
        if (input) return input;
      }
      const labelRect = node.getBoundingClientRect();
      const candidates = Array.from(win.querySelectorAll("input:not([type='hidden']):not([disabled]), textarea:not([disabled])"))
        .filter((input) => visible(input))
        .map((input) => {
          const rect = input.getBoundingClientRect();
          const sameLine = Math.abs((rect.top + rect.height / 2) - (labelRect.top + labelRect.height / 2));
          const toRight = rect.left >= labelRect.left;
          return { input, score: sameLine + (toRight ? 0 : 1000) + Math.max(0, labelRect.right - rect.left) };
        })
        .sort((a, b) => a.score - b.score);
      if (candidates.length) return candidates[0].input;
    }
    return null;
  };
  const fire = (cmp, value) => {
    if (cmp.fireEvent) {
      cmp.fireEvent("change", cmp, value);
      cmp.fireEvent("blur", cmp);
    }
    const dom = cmp.el && cmp.el.dom;
    if (dom) {
      dom.dispatchEvent(new Event("input", { bubbles: true }));
      dom.dispatchEvent(new Event("change", { bubbles: true }));
      dom.dispatchEvent(new Event("blur", { bubbles: true }));
    }
  };
  const setField = (cmp, value) => {
    if (!cmp || !value) return false;
    let usedSetter = false;
    if (cmp.setValue) cmp.setValue(value);
    if (cmp.setValue) usedSetter = true;
    if (cmp.setRawValue && /date|дата|срок|действ/i.test(labelOf(cmp))) {
      cmp.setRawValue(value);
      usedSetter = true;
    }
    if (!usedSetter) return false;
    fire(cmp, value);
    const actual = String(cmp.getRawValue ? cmp.getRawValue() : cmp.getValue ? cmp.getValue() : "").replace(/\s+/g, "").replace(".", ",");
    const expected = String(value || "").replace(/\s+/g, "").replace(".", ",");
    return actual === expected;
  };
  const setByCmpOrLabel = (cmp, labelPattern, value) => {
    if (!value) return false;
    if (setDomValue(findInputNearLabel(labelPattern), value)) return true;
    return setField(cmp, value);
  };
  const result = { price_with_vat: false, price_without_vat: false, validity_date: false };
  for (let i = 0; i < 40; i++) {
    if (!Array.from(document.querySelectorAll(".x-window, .x-window-body")).some((el) => visible(el) && letterText.test(String(el.innerText || el.textContent || "")))) {
      await wait(250);
      continue;
    }
    if (values.price_with_vat) {
      const field = findField(
        ["price", "price_nds", "price_with_nds", "price_with_vat", "total_price", "amount"],
        [
          /итогов.*цен.*с\s+ндс/i,
          /стоимост.*с\s+ндс/i,
          /цен.*предлож.*с\s+ндс/i,
          /^price$|price_with/i,
        ]
      );
      result.price_with_vat = setByInputName("price", values.price_with_vat, true)
        || setByCmpOrLabel(field, /итоговая\s+цена,\s*с\s+ндс/i, values.price_with_vat);
    }
    if (values.price_without_vat) {
      const field = findField(
        ["price_no_nds", "price_without_nds", "price_without_vat", "price_no_vat"],
        [
          /цен.*без\s+ндс/i,
          /стоимост.*без\s+ндс/i,
          /price_no_nds|without/i,
        ]
      );
      result.price_without_vat = setByInputName("price_no_nds", values.price_without_vat, true)
        || setByCmpOrLabel(field, /цен.*без\s+ндс/i, values.price_without_vat);
    }
    if (values.validity_date) {
      const field = findField(
        ["validity_date", "valid_until", "date_valid", "date_end", "offer_valid_until", "offer_date"],
        [
          /настоящая\s+заявка.*действует\s+до/i,
          /срок.*действ/i,
          /действ.*до/i,
          /оферт.*до/i,
          /valid/i,
        ]
      );
      result.validity_date = setByInputName("offer_valid", values.validity_date, false)
        || setByCmpOrLabel(field, /настоящая\s+заявка\s+на\s+участие\s+в\s+опросе\s+действует\s+до/i, values.validity_date);
    }
    if ((!values.price_with_vat || result.price_with_vat)
      && (!values.price_without_vat || result.price_without_vat)
      && (!values.validity_date || result.validity_date)) {
      callback({ ok: true, result });
      return;
    }
    await wait(250);
  }
  callback({ ok: false, result });
})();
"""
        result = self.driver.execute_async_script(script, values)
        if not isinstance(result, dict):
            raise RuntimeError(f"Не удалось заполнить коммерческие поля письма: {result}")

    def _default_offer_validity_date(self) -> str:
        return (date.today() + timedelta(days=200)).strftime("%d.%m.%Y")

    def _manual_letter_fields_from_text(self, text: str) -> list[str]:
        value = str(text or "").lower()
        fields: list[str] = []
        if ("без" in value and "ндс" in value) or "price_no" in value or "without" in value:
            fields.append("price_without_vat")
        if (
            ("с ндс" in value or "с  ндс" in value)
            and "без ндс" not in value
        ) or "price_with" in value:
            fields.append("price_with_vat")
        if "действ" in value or "дат" in value or "valid" in value:
            fields.append("validity_date")
        if ("стоим" in value or "цен" in value or "price" in value) and not any(
            field.startswith("price_") for field in fields
        ):
            fields.append("price_with_vat")
        return list(dict.fromkeys(fields))

    def _normalize_manual_letter_fields(self, fields: Any, text: str = "") -> list[str]:
        normalized: list[str] = []
        source = fields if isinstance(fields, list) else [fields]
        for field in source:
            if isinstance(field, dict):
                candidates = [str(key) for key in field.keys()]
                candidates.extend(str(value) for value in field.values())
            else:
                candidates = [str(field)]
            for candidate in candidates:
                value = candidate.strip()
                if not value or value in {"{}", "[]", "None", "null", "undefined"}:
                    continue
                if value in {"price_with_vat", "price_without_vat", "validity_date"}:
                    normalized.append(value)
                    continue
                normalized.extend(self._manual_letter_fields_from_text(value))
        if not normalized:
            normalized = self._manual_letter_fields_from_text(text)
        return list(dict.fromkeys(normalized))

    def application_letter_required_fields_status(self) -> dict[str, Any]:
        assert self.driver is not None
        script = r"""
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const norm = (value) => String(value || "").toLowerCase().replace(/\s+/g, " ").trim();
const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "");
const letterText = /Письмо\s+о\s+подаче\s+заявки/i;
const result = {
  letterOpen: false,
  missing: [],
  fields: {
    price_with_vat: { found: false, value: "" },
    price_without_vat: { found: false, value: "" },
    validity_date: { found: false, value: "" },
  },
};
const eachCmp = (fn) => {
  if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return;
  const all = Ext.ComponentMgr.all;
  if (typeof all.each === "function") {
    all.each(fn);
    return;
  }
  const items = all.items || all.getRange && all.getRange() || all.map && Object.values(all.map) || Object.values(all);
  for (const cmp of items) {
    if (cmp && typeof cmp === "object") fn(cmp);
  }
};
const letterWindow = () => Array.from(document.querySelectorAll(".x-window"))
  .find((el) => visible(el) && letterText.test(textOf(el)));
const win = letterWindow();
result.letterOpen = !!win;
if (!win) return result;
const inLetterWindow = (cmp) => {
  const el = cmp && cmp.el && cmp.el.dom;
  const cmpWin = el && el.closest && el.closest(".x-window");
  return !!(cmpWin && cmpWin === win);
};
const labelOf = (cmp) => norm([
  cmp.fieldLabel,
  cmp.boxLabel,
  cmp.name,
  cmp.id,
  cmp.el && cmp.el.dom && textOf(cmp.el.dom),
].filter(Boolean).join(" "));
const cmpValue = (cmp) => {
  try {
    if (cmp.getRawValue) return String(cmp.getRawValue() || "");
  } catch (e) {}
  try {
    if (cmp.getValue) return String(cmp.getValue() || "");
  } catch (e) {}
  const el = cmp && cmp.el && cmp.el.dom;
  const input = el && el.querySelector && el.querySelector("input:not([type='hidden']), textarea");
  return input ? String(input.value || "") : "";
};
const findCmp = (names, patterns) => {
  let found = null;
  eachCmp((cmp) => {
    if (found || cmp.hidden || cmp.disabled || !inLetterWindow(cmp)) return;
    const name = norm(cmp.name);
    const label = labelOf(cmp);
    if (names.some((item) => name === norm(item)) || patterns.some((pattern) => pattern.test(label))) {
      found = cmp;
    }
  });
  return found;
};
const findInputNearLabel = (labelPattern) => {
  const nodes = Array.from(win.querySelectorAll("label, td, div, span"));
  for (const node of nodes) {
    const text = norm(node.innerText || node.textContent || "");
    if (!labelPattern.test(text)) continue;
    const containers = [
      node.closest("tr"),
      node.parentElement,
      node.parentElement && node.parentElement.parentElement,
      node.parentElement && node.parentElement.parentElement && node.parentElement.parentElement.parentElement,
    ].filter(Boolean);
    for (const container of containers) {
      const input = container.querySelector("input:not([type='hidden']):not([disabled]), textarea:not([disabled])");
      if (input && visible(input)) return input;
    }
  }
  return null;
};
const readField = (key, names, patterns, labelPattern) => {
  const cmp = findCmp(names, patterns);
  const cmpRaw = cmp ? cmpValue(cmp) : "";
  const input = findInputNearLabel(labelPattern)
    || Array.from(win.querySelectorAll(names.map((name) => `input[name="${name}"], textarea[name="${name}"]`).join(",")))
      .find((el) => visible(el));
  const domRaw = input ? String(input.value || input.getAttribute("value") || "") : "";
  const value = String(cmpRaw || domRaw || "").replace(/\s+/g, " ").trim();
  result.fields[key] = { found: !!(cmp || input), value };
};
readField(
  "price_with_vat",
  ["price", "price_nds", "price_with_nds", "price_with_vat", "total_price", "amount"],
  [/итогов.*цен.*с\s+ндс/i, /стоимост.*с\s+ндс/i, /цен.*предлож.*с\s+ндс/i, /^price$|price_with/i],
  /итоговая\s+цена,\s*с\s+ндс/i
);
readField(
  "price_without_vat",
  ["price_no_nds", "price_without_nds", "price_without_vat", "price_no_vat"],
  [/цен.*без\s+ндс/i, /стоимост.*без\s+ндс/i, /price_no_nds|without/i],
  /итоговая\s+цена,\s*без\s+ндс|цен.*без\s+ндс/i
);
readField(
  "validity_date",
  ["offer_valid", "validity_date", "valid_until", "date_valid", "date_end", "offer_valid_until", "offer_date"],
  [/настоящая\s+заявка.*действует\s+до/i, /срок.*действ/i, /действ.*до/i, /оферт.*до/i, /valid/i],
  /настоящая\s+заявка\s+на\s+участие\s+в\s+опросе\s+действует\s+до/i
);
const hasMoney = /[1-9]/.test(result.fields.price_with_vat.value || "");
const hasMoneyWithoutVat = /[1-9]/.test(result.fields.price_without_vat.value || "");
const hasDate = /\d/.test(result.fields.validity_date.value || "");
if (!hasMoney) result.missing.push("price_with_vat");
if (result.fields.price_without_vat.found && !hasMoneyWithoutVat) result.missing.push("price_without_vat");
if (!hasDate) result.missing.push("validity_date");
return result;
"""
        result = self.driver.execute_script(script)
        return result if isinstance(result, dict) else {"letterOpen": False, "missing": []}

    def save_application_letter_after_manual_input(self) -> None:
        status = self.application_letter_required_fields_status()
        missing = self._normalize_manual_letter_fields(status.get("missing") or [])
        if missing:
            labels = {
                "price_with_vat": "итоговая цена с НДС",
                "price_without_vat": "итоговая цена без НДС",
                "validity_date": "дата действия заявки",
            }
            readable = ", ".join(labels.get(str(item), str(item)) for item in missing)
            raise RuntimeError(f"Обязательные поля письма всё ещё не заполнены: {readable}.")
        try:
            self._save_application_letter_modal()
        except ApplicationLetterManualInputRequired as e:
            labels = {
                "price_with_vat": "итоговая цена с НДС",
                "price_without_vat": "итоговая цена без НДС",
                "validity_date": "дата действия заявки",
            }
            missing_fields = self._normalize_manual_letter_fields(e.fields)
            readable = ", ".join(labels.get(str(item), str(item)) for item in missing_fields)
            raise RuntimeError(f"Обязательные поля письма всё ещё не заполнены: {readable}.") from e

    def _save_application_letter_modal(self) -> None:
        assert self.driver is not None
        script = r"""
const result = {
  letterOpen: false,
  blockerClosed: false,
  clicked: false,
  reason: "",
  validationRequired: false,
  validationFields: [],
  validationText: "",
};
  const visible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
  };
  const letterText = /Письмо\s+о\s+подаче\s+заявки/i;
  const saveText = /^Сохранить$/i;
  const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").trim();
  const eachCmp = (fn) => {
    if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return;
    const all = Ext.ComponentMgr.all;
    if (typeof all.each === "function") {
      all.each(fn);
      return;
    }
    const items = all.items || all.getRange && all.getRange() || all.map && Object.values(all.map) || Object.values(all);
    for (const cmp of items) {
      if (cmp && typeof cmp === "object") fn(cmp);
    }
  };
  const letterWindow = () => Array.from(document.querySelectorAll(".x-window"))
    .find((el) => visible(el) && letterText.test(String(el.innerText || el.textContent || "")));
  const fieldsFromValidationText = (text) => {
    const fields = [];
    const normalized = String(text || "").replace(/\s+/g, " ");
    if (/итоговая\s+цена\s+без\s+НДС|итоговая\s+цена,\s+без\s+НДС|цена\s+без\s+НДС/i.test(normalized)) {
      fields.push("price_without_vat");
    }
    if (/итоговая\s+цена\s*,?\s+с\s+НДС|цена\s+с\s+НДС/i.test(normalized)) {
      fields.push("price_with_vat");
    }
    if (/действует\s+до|дата\s+действ|срок\s+действ/i.test(normalized)) {
      fields.push("validity_date");
    }
    return Array.from(new Set(fields));
  };
  const validationDialog = () => {
    const windows = Array.from(document.querySelectorAll(".x-window"))
      .filter((win) => visible(win) && !letterText.test(String(win.innerText || win.textContent || "")));
    for (const win of windows) {
      const text = String(win.innerText || win.textContent || "");
      if (!/Не\s+заполнено\s+поле/i.test(text)) continue;
      return { win, text, fields: fieldsFromValidationText(text) };
    }
    return null;
  };
  const closeBlockingDialogs = () => {
    let closed = false;
    const windows = Array.from(document.querySelectorAll(".x-window"))
      .filter((win) => visible(win) && !letterText.test(String(win.innerText || win.textContent || "")));
    for (const win of windows) {
      const text = String(win.innerText || win.textContent || "");
      if (!/(Непредвиденн|Ошибка|Предупреждение|Error|Warning|Неверный\s+формат\s+файла)/i.test(text)) continue;
      let extClosed = false;
      eachCmp((cmp) => {
        if (extClosed || cmp.hidden || cmp.disabled) return;
        const el = cmp && cmp.el && cmp.el.dom;
        const cmpWin = el && el.closest && el.closest(".x-window");
        if (cmpWin !== win) return;
        const text = String(cmp.text || "").trim();
        if (!/^(OK|ОК)$/i.test(text)) return;
        try {
          if (typeof cmp.onClick === "function") {
            cmp.onClick({ button: 0, preventDefault() {}, stopEvent() {}, getTarget() { return el; } });
            extClosed = true;
            return;
          }
        } catch (e) {}
        try {
          if (cmp.handler) {
            cmp.handler.call(cmp.scope || cmp, cmp);
            extClosed = true;
            return;
          }
        } catch (e) {}
        try {
          if (cmp.fireEvent) {
            cmp.fireEvent("click", cmp);
            extClosed = true;
          }
        } catch (e) {}
      });
      if (extClosed) {
        closed = true;
        continue;
      }
      const buttons = Array.from(win.querySelectorAll("button, input[type='button'], a, span, div, td, em"))
        .filter((el) => visible(el) && /^(OK|ОК)$/i.test(textOf(el)));
      const button = buttons.find((el) => (el.tagName || "").toLowerCase() === "button") || buttons[0];
      if (!button) continue;
      const extButton = button.closest && button.closest(".x-btn, .x-btn-wrap, table, button, a");
      const chain = [extButton, button, button.parentElement, button.parentElement && button.parentElement.parentElement];
      for (const node of chain) {
        if (dispatchRealClick(node)) {
          closed = true;
          break;
        }
      }
    }
    return closed;
  };
  const hasBlockingDialogs = () => Array.from(document.querySelectorAll(".x-window"))
    .some((win) => visible(win)
      && !letterText.test(String(win.innerText || win.textContent || ""))
      && /(Непредвиденн|Ошибка|Предупреждение|Error|Warning|Неверный\s+формат\s+файла)/i.test(String(win.innerText || win.textContent || "")));
  const inLetterWindow = (cmp) => {
    const el = cmp && cmp.el && cmp.el.dom;
    const win = el && el.closest && el.closest(".x-window");
    return !!(win && visible(win) && letterText.test(String(win.innerText || win.textContent || "")));
  };
  const findSaveCmp = () => {
    let found = null;
    eachCmp((cmp) => {
      if (found || cmp.hidden || cmp.disabled || !inLetterWindow(cmp)) return;
      if (saveText.test(String(cmp.text || "").trim())) found = cmp;
    });
    return found;
  };
  const dispatchRealClick = (node) => {
    if (!node || !visible(node)) return false;
    try {
      node.scrollIntoView({ block: "center", inline: "center" });
      node.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, view: window }));
      node.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, cancelable: true, view: window }));
      node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
      node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
      node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      if (typeof node.click === "function") node.click();
      return true;
    } catch (e) {
      return false;
    }
  };
  const clickDom = () => {
    const win = letterWindow();
    if (!win) return false;
    const nodes = Array.from(win.querySelectorAll("button, input[type='button'], a, span, div, td, em"));
    const button = nodes.find((el) => visible(el) && saveText.test(textOf(el)));
    if (!button) return false;
    const extButton = button.closest && button.closest(".x-btn, .x-btn-wrap, table, button, a");
    const chain = [extButton, button, button.parentElement, button.parentElement && button.parentElement.parentElement];
    for (const node of chain) {
      if (dispatchRealClick(node)) return true;
    }
    return false;
  };
  const clickCmpDom = () => {
    const cmp = findSaveCmp();
    if (!cmp) return false;
    const candidates = [
      cmp.el && cmp.el.dom,
      cmp.btnEl && cmp.btnEl.dom,
      cmp.buttonEl && cmp.buttonEl.dom,
      cmp.wrap && cmp.wrap.dom,
    ].filter(Boolean);
    for (const node of candidates) {
      if (dispatchRealClick(node)) return true;
    }
    return false;
  };
  const clickCmpHandler = () => {
    const cmp = findSaveCmp();
    if (!cmp) return false;
    try {
      if (typeof cmp.onClick === "function") {
        cmp.onClick({ button: 0, preventDefault() {}, stopEvent() {}, getTarget() { return cmp.el && cmp.el.dom; } });
        return true;
      }
    } catch (e) {}
    try {
      if (cmp.handler) {
        cmp.handler.call(cmp.scope || cmp, cmp);
        return true;
      }
    } catch (e) {}
    try {
      if (cmp.fireEvent) {
        cmp.fireEvent("click", cmp);
        return true;
      }
    } catch (e) {}
    return false;
  };
  const validation = validationDialog();
  if (validation) {
    result.validationRequired = true;
    result.validationFields = validation.fields;
    result.validationText = validation.text;
    result.blockerClosed = closeBlockingDialogs();
    result.reason = "required fields validation dialog";
    return result;
  }
  result.blockerClosed = closeBlockingDialogs();
  result.blockersOpen = hasBlockingDialogs();
  if (result.blockerClosed) {
    result.reason = "blocking dialog closed";
    return result;
  }
  if (result.blockersOpen) {
    result.reason = "blocking dialog still open";
    return result;
  }
  result.letterOpen = !!letterWindow();
  if (!result.letterOpen) {
    result.reason = "letter window is already closed";
    return result;
  }
  const domClicked = clickDom();
  const cmpDomClicked = clickCmpDom();
  const cmpHandlerClicked = clickCmpHandler();
  result.clicked = domClicked || cmpDomClicked || cmpHandlerClicked;
  result.method = { domClicked, cmpDomClicked, cmpHandlerClicked };
  result.letterOpen = !!letterWindow();
  result.reason = result.clicked ? "save clicked" : "save button not found";
  return result;
"""
        last_result: Any = None
        for _ in range(30):
            last_result = self.driver.execute_script(script)
            if isinstance(last_result, dict) and last_result.get("validationRequired"):
                validation_text = str(last_result.get("validationText") or "")
                fields = self._normalize_manual_letter_fields(
                    last_result.get("validationFields") or [],
                    validation_text,
                )
                if not fields:
                    fields = ["price_with_vat"]
                raise ApplicationLetterManualInputRequired(fields, validation_text)
            if isinstance(last_result, dict) and last_result.get("blockerClosed"):
                time.sleep(1.0)
                continue
            if isinstance(last_result, dict) and last_result.get("blockersOpen"):
                time.sleep(0.5)
                continue
            if isinstance(last_result, dict) and not last_result.get("letterOpen"):
                return
            time.sleep(0.5)
            still_open = self.driver.execute_script(
                r"""
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width >= 0 && rect.height >= 0;
};
return Array.from(document.querySelectorAll(".x-window")).some((el) =>
  visible(el) && /Письмо\s+о\s+подаче\s+заявки/i.test(String(el.innerText || el.textContent || ""))
);
"""
            )
            if not still_open:
                return
        raise RuntimeError(f"Не удалось сохранить письмо о подаче заявки: {last_result}")

    def save_application_draft(self) -> None:
        assert self.driver is not None
        script = r"""
const result = { clicked: false, done: false, error: "", message: "", method: {} };
const draftText = /^Сохранить\s+черновик$/i;
const okText = /^(OK|ОК)$/i;
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const textOf = (el) => String(el && (el.innerText || el.textContent || el.value) || "").trim();
const eachCmp = (fn) => {
  if (!window.Ext || !Ext.ComponentMgr || !Ext.ComponentMgr.all) return;
  const all = Ext.ComponentMgr.all;
  if (typeof all.each === "function") {
    all.each(fn);
    return;
  }
  const items = all.items || all.getRange && all.getRange() || all.map && Object.values(all.map) || Object.values(all);
  for (const cmp of items) {
    if (cmp && typeof cmp === "object") fn(cmp);
  }
};
const dispatchRealClick = (node) => {
  if (!node || !visible(node)) return false;
  try {
    node.scrollIntoView({ block: "center", inline: "center" });
    node.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, view: window }));
    node.dispatchEvent(new MouseEvent("mousemove", { bubbles: true, cancelable: true, view: window }));
    node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
    node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    if (typeof node.click === "function") node.click();
    return true;
  } catch (e) {
    return false;
  }
};
const findDraftCmp = () => {
  let found = null;
  eachCmp((cmp) => {
    if (found || cmp.hidden || cmp.disabled) return;
    if (draftText.test(String(cmp.text || "").trim())) found = cmp;
  });
  return found;
};
const clickCmpDom = () => {
  const cmp = findDraftCmp();
  if (!cmp) return false;
  const candidates = [
    cmp.btnEl && cmp.btnEl.dom,
    cmp.el && cmp.el.dom,
    cmp.buttonEl && cmp.buttonEl.dom,
    cmp.wrap && cmp.wrap.dom,
  ].filter(Boolean);
  for (const node of candidates) {
    if (dispatchRealClick(node)) return true;
  }
  return false;
};
const clickDom = () => {
  const nodes = Array.from(document.querySelectorAll("button, input[type='button'], a, span, div, td, em"));
  const button = nodes.find((el) => visible(el) && draftText.test(textOf(el)));
  if (!button) return false;
  const extButton = button.closest && button.closest(".x-btn, .x-btn-wrap, table, button, a");
  const chain = [extButton, button, button.parentElement, button.parentElement && button.parentElement.parentElement];
  for (const node of chain) {
    if (dispatchRealClick(node)) return true;
  }
  return false;
};
const clickCmpHandler = () => {
  const cmp = findDraftCmp();
  if (!cmp) return false;
  try {
    if (typeof cmp.onClick === "function") {
      cmp.onClick({ button: 0, preventDefault() {}, stopEvent() {}, getTarget() { return cmp.el && cmp.el.dom; } });
      return true;
    }
  } catch (e) {}
  try {
    if (cmp.handler) {
      cmp.handler.call(cmp.scope || cmp, cmp);
      return true;
    }
  } catch (e) {}
  try {
    if (cmp.fireEvent) {
      cmp.fireEvent("click", cmp);
      return true;
    }
  } catch (e) {}
  return false;
};
const inspectDialogs = () => {
  const windows = Array.from(document.querySelectorAll(".x-window")).filter(visible);
  for (const win of windows) {
    const text = textOf(win);
    if (!text) continue;
    if (/(Ошибка|Error|Неверн|не\s+удалось|заполните|обязательн)/i.test(text)) {
      result.error = text.slice(0, 500);
      return;
    }
    if (/(сохранен|сохранён|сохранена|сохранено|черновик)/i.test(text)) {
      result.done = true;
      result.message = text.slice(0, 500);
      const buttons = Array.from(win.querySelectorAll("button, input[type='button'], a, span, div, td, em"))
        .filter((el) => visible(el) && okText.test(textOf(el)));
      const button = buttons.find((el) => (el.tagName || "").toLowerCase() === "button") || buttons[0];
      if (button) {
        const extButton = button.closest && button.closest(".x-btn, .x-btn-wrap, table, button, a");
        dispatchRealClick(extButton || button);
      }
      return;
    }
  }
};
inspectDialogs();
if (result.done || result.error) return result;
const cmpDomClicked = clickCmpDom();
const domClicked = clickDom();
const cmpHandlerClicked = clickCmpHandler();
result.clicked = cmpDomClicked || domClicked || cmpHandlerClicked;
result.method = { cmpDomClicked, domClicked, cmpHandlerClicked };
result.message = result.clicked ? "draft save clicked" : "draft save button not found";
return result;
"""
        last_result: Any = None
        for attempt in range(30):
            last_result = self.driver.execute_script(script)
            if isinstance(last_result, dict):
                if last_result.get("error"):
                    raise RuntimeError(str(last_result.get("error")))
                if last_result.get("done"):
                    return
                if not last_result.get("clicked") and attempt == 0:
                    raise RuntimeError(f"Не удалось нажать кнопку «Сохранить черновик»: {last_result}")
            time.sleep(0.5)
            page_state = self.driver.execute_script(
                r"""
const visible = (el) => {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
};
const text = String(document.body && document.body.innerText || "");
const windows = Array.from(document.querySelectorAll(".x-window")).filter(visible)
  .map((el) => String(el.innerText || el.textContent || "").trim());
return {
  hasDraftButton: /Сохранить\s+черновик/i.test(text),
  successWindow: windows.some((value) => /(сохранен|сохранён|сохранена|сохранено|черновик)/i.test(value)),
  errorWindow: windows.find((value) => /(Ошибка|Error|Неверн|не\s+удалось|заполните|обязательн)/i.test(value)) || "",
};
"""
            )
            if isinstance(page_state, dict):
                if page_state.get("errorWindow"):
                    raise RuntimeError(str(page_state.get("errorWindow")))
                if page_state.get("successWindow") or not page_state.get("hasDraftButton"):
                    return
        raise RuntimeError(f"Не удалось подтвердить сохранение черновика: {last_result}")

    def _upload_one_file(
        self,
        path: Path,
        progress: Optional[Callable[[str], None]] = None,
        upload_panel_id: str = "application_docs_tech_1",
        area_name: str = "технической части заявки",
    ) -> None:
        assert self.driver is not None

        dialog_error: Exception | None = None
        if progress:
            progress(f"Открываю штатный выбор файла: {path.name}")
        try:
            before_count = len(self._uploaded_file_names(upload_panel_id=upload_panel_id))
            self._upload_one_file_via_dialog(path, upload_panel_id=upload_panel_id)
            if self._wait_after_file_selection(
                path,
                upload_panel_id=upload_panel_id,
                previous_count=before_count,
            ):
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
            before_count = len(self._uploaded_file_names(upload_panel_id=upload_panel_id))
            input_element = self._find_tektorg_file_input(upload_panel_id=upload_panel_id, area_name=area_name)
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
            if self._wait_after_file_selection(
                path,
                upload_panel_id=upload_panel_id,
                previous_count=before_count,
            ):
                return
            raise RuntimeError("Сайт не показал файл в списке после выбора через input.")
        except Exception as e:
            direct_error = e
            if progress:
                progress(f"Прямая загрузка через input не сработала для {path.name}: {e}")

        if progress:
            progress(f"Проверяю, не появился ли файл с задержкой: {path.name}")
        if self._wait_after_file_selection(
            path,
            upload_panel_id=upload_panel_id,
            previous_count=None,
            timeout_seconds=45.0,
        ):
            return

        raise RuntimeError(f"Файл не загрузился. Ошибка штатной загрузки: {dialog_error}. Ошибка input: {direct_error}")

    def _find_tektorg_file_input(
        self,
        upload_panel_id: str = "application_docs_tech_1",
        area_name: str = "технической части заявки",
    ):
        assert self.driver is not None

        deadline = time.time() + 25
        while time.time() < deadline:
            input_element = self.driver.execute_script(
                r"""
const uploadPanelId = arguments[0];
const inputs = Array.from(document.querySelectorAll("input.x-form-file[type='file'], input[type='file']"));
if (!inputs.length) return null;
if (window.Ext && Ext.getCmp) {
  const upload = Ext.getCmp(uploadPanelId);
  const uploadPanel = upload && upload.ids ? Ext.getCmp(upload.ids.upload_panel_id) : null;
  const uploadDom = uploadPanel && uploadPanel.el && uploadPanel.el.dom;
  const direct = uploadDom && uploadDom.querySelector("input.x-form-file[type='file'], input[type='file']");
  if (direct && !direct.disabled) return direct;
}
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
  if (/Документы\s+технической\s+части\s+заявки/i.test(text) && uploadPanelId.includes("tech")) score += 100;
  if (/Документы\s+коммерческой\s+части\s+заявки/i.test(text) && uploadPanelId.includes("com")) score += 100;
  if (/Техническая\s+часть/i.test(text) && uploadPanelId.includes("tech")) score += 60;
  if (/Коммерческая\s+часть/i.test(text) && uploadPanelId.includes("com")) score += 60;
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
""",
                upload_panel_id,
            )
            if input_element is not None:
                return input_element
            time.sleep(0.3)
        raise RuntimeError(f"На странице не найдено поле выбора файла для {area_name}.")

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

    def _wait_after_file_selection(
        self,
        path: Path,
        upload_panel_id: str = "application_docs_tech_1",
        previous_count: int | None = None,
        timeout_seconds: float = 60.0,
    ) -> bool:
        assert self.driver is not None
        marker = self._normalize_uploaded_filename(path.name)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                names = self._uploaded_file_names(upload_panel_id=upload_panel_id)
                normalized_names = {
                    self._normalize_uploaded_filename(name)
                    for name in names
                }
                if marker and marker in normalized_names:
                    return True
                if previous_count is not None and len(names) > previous_count:
                    return True
                info = self.driver.execute_script(
                    r"""
const uploadPanelId = arguments[0];
let root = document.body;
if (window.Ext && Ext.getCmp) {
  const upload = Ext.getCmp(uploadPanelId);
  const panel = upload && upload.ids ? Ext.getCmp(upload.ids.uploaded_files_id) : null;
  if (panel && panel.el && panel.el.dom) root = panel.el.dom;
}
const bodyText = String(root && root.innerText || root && root.textContent || "");
const masks = Array.from(document.querySelectorAll(".x-mask-loading, .x-mask-msg, .x-mask"))
  .filter((el) => {
    const style = window.getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  })
  .map((el) => String(el.innerText || el.textContent || ""));
return { bodyText, masks };
""",
                    upload_panel_id,
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
        return self._is_uploaded_file_listed(path, upload_panel_id=upload_panel_id)

    def _is_uploaded_file_listed(
        self,
        path: Path,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> bool:
        marker = self._normalize_uploaded_filename(path.name)
        if not marker:
            return False
        return marker in {
            self._normalize_uploaded_filename(name)
            for name in self._uploaded_file_names(upload_panel_id=upload_panel_id)
        }

    def _uploaded_file_names(
        self,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> list[str]:
        items = self._uploaded_file_items(upload_panel_id=upload_panel_id)
        if items:
            return [str(item.get("name") or "") for item in items if str(item.get("name") or "").strip()]
        assert self.driver is not None
        try:
            names = self.driver.execute_script(
                r"""
const uploadPanelId = arguments[0];
let root = document;
if (window.Ext && Ext.getCmp) {
  const upload = Ext.getCmp(uploadPanelId);
  const panel = upload && upload.ids ? Ext.getCmp(upload.ids.uploaded_files_id) : null;
  if (panel && panel.el && panel.el.dom) root = panel.el.dom;
}
const anchors = Array.from(root.querySelectorAll("a"));
return anchors
  .map((a) => String(a.innerText || a.textContent || "").trim())
  .filter((text) => /\.(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv)$/i.test(text));
""",
                upload_panel_id,
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

    def _uploaded_file_items(
        self,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> list[dict[str, Any]]:
        assert self.driver is not None
        try:
            items = self.driver.execute_script(
                r"""
const uploadPanelId = arguments[0];
const upload = Ext && Ext.getCmp ? Ext.getCmp(uploadPanelId) : null;
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
""",
                upload_panel_id,
            )
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _remove_duplicate_uploaded_files(
        self,
        progress: Optional[Callable[[str], None]] = None,
        upload_panel_id: str = "application_docs_tech_1",
        description: str = "технических документов",
    ) -> int:
        assert self.driver is not None
        removed = 0
        while True:
            duplicate = self._next_duplicate_uploaded_file(upload_panel_id=upload_panel_id)
            if not duplicate:
                break
            name = str(duplicate.get("name") or "файл")
            component_id = str(duplicate.get("componentId") or "")
            if not component_id:
                break
            if progress:
                progress(f"Удаляю дубль загруженного файла: {name}")
            before_count = len(self._uploaded_file_names(upload_panel_id=upload_panel_id))
            deleted = self._delete_uploaded_file_component(component_id, upload_panel_id=upload_panel_id)
            if not deleted:
                break
            if not self._wait_uploaded_count_less_than(before_count, upload_panel_id=upload_panel_id):
                raise RuntimeError(f"Сайт не удалил дубль файла: {name}")
            removed += 1
        if removed and progress:
            progress(f"Удалено дублей {description}: {removed}")
        return removed

    def _clear_uploaded_files(
        self,
        progress: Optional[Callable[[str], None]] = None,
        upload_panel_id: str = "application_docs_tech_1",
        description: str = "технический",
    ) -> int:
        assert self.driver is not None
        removed = 0
        while True:
            item = self._first_uploaded_file(upload_panel_id=upload_panel_id)
            if not item:
                break
            name = str(item.get("name") or "файл")
            component_id = str(item.get("componentId") or "")
            if not component_id:
                break
            if progress:
                progress(f"Очищаю ранее загруженный {description} файл: {name}")
            before_count = len(self._uploaded_file_names(upload_panel_id=upload_panel_id))
            deleted = self._delete_uploaded_file_component(component_id, upload_panel_id=upload_panel_id)
            if not deleted:
                break
            if not self._wait_uploaded_count_less_than(before_count, upload_panel_id=upload_panel_id):
                raise RuntimeError(f"Сайт не удалил ранее загруженный файл: {name}")
            removed += 1
        if removed and progress:
            progress(f"Очищено ранее загруженных файлов ({description}): {removed}")
        return removed

    def _next_duplicate_uploaded_file(
        self,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> Optional[dict[str, str]]:
        items = self._uploaded_file_items(upload_panel_id=upload_panel_id)
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

    def _first_uploaded_file(
        self,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> Optional[dict[str, str]]:
        items = self._uploaded_file_items(upload_panel_id=upload_panel_id)
        if not items:
            return None
        first = items[0]
        if not isinstance(first, dict):
            return None
        return {
            "name": str(first.get("name") or ""),
            "componentId": str(first.get("componentId") or ""),
        }

    def _delete_uploaded_file_component(
        self,
        component_id: str,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> bool:
        assert self.driver is not None
        try:
            return bool(
                self.driver.execute_script(
                    r"""
const componentId = arguments[0];
const uploadPanelId = arguments[1];
const upload = Ext && Ext.getCmp ? Ext.getCmp(uploadPanelId) : null;
const item = Ext && Ext.getCmp ? Ext.getCmp(componentId) : null;
if (!upload || !item || !item.file || !upload.deleteFile) return false;
upload.deleteFile(item.file);
return true;
""",
                    component_id,
                    upload_panel_id,
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

    def _wait_uploaded_count_less_than(
        self,
        before_count: int,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> bool:
        deadline = time.time() + 12
        while time.time() < deadline:
            if len(self._uploaded_file_names(upload_panel_id=upload_panel_id)) < before_count:
                return True
            time.sleep(0.3)
        return False

    def _upload_one_file_via_dialog(
        self,
        path: Path,
        upload_panel_id: str = "application_docs_tech_1",
    ) -> None:
        assert self.driver is not None
        clicked = self.driver.execute_async_script(
            r"""
const callback = arguments[arguments.length - 1];
const uploadPanelId = arguments[0];
(async () => {
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const upload = window.Ext && Ext.getCmp ? Ext.getCmp(uploadPanelId) : null;
  const uploadPanel = upload && upload.ids ? Ext.getCmp(upload.ids.upload_panel_id) : null;
  const uploadDom = uploadPanel && uploadPanel.el && uploadPanel.el.dom;
  if (uploadDom) {
    const button = Array.from(uploadDom.querySelectorAll("button, input[type=button], a, span, div"))
      .find((el) => /Выбрать\s+и\s+загрузить\s+файл/i.test(String(el.innerText || el.value || el.textContent || "")));
    if (button) {
      try {
        button.scrollIntoView({ block: "center", inline: "center" });
        button.click();
        await wait(700);
        callback(true);
        return;
      } catch (e) {}
    }
  }
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
""",
            upload_panel_id,
        )
        if not clicked:
            raise RuntimeError("Не найдена кнопка «Выбрать и загрузить файл».")

        time.sleep(0.7)
        self._set_windows_clipboard_text(str(path))
        from pywinauto.keyboard import send_keys

        send_keys("^v")
        time.sleep(0.2)
        send_keys("{ENTER}")
        time.sleep(0.6)

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
