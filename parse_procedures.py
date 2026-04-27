"""
Финальный парсер актуальных процедур ЭТП ГПБ (Газпромбанк).

Этап 2 проекта: после разведки (explore_etp.py) собираем реальные данные
с https://etpgaz.gazprombank.ru/#com/procedure/index через Ext.Direct RPC
метод Procedure.list.

Стратегия:
 1) Запускаем Google Chrome пользователя (обычный профиль, все его расширения,
    включая IFCPlugin/ГосПлагин и CAdES Browser Plug-in) с
    --remote-debugging-port.
 2) Selenium подключается к запущенному Chrome (debuggerAddress).
 3) Открываем целевую страницу. Если сессия жива — сразу попадаем на реестр.
    Если сессия истекла — нас редиректит на ESIA (esia.gosuslugi.ru), там
    автоматически кликаем «Электронная подпись» (если ещё не выбрана) и
    «Продолжить». Плагин Госуслуг подписывает «невидимо» (сертификат в
    реестре без PIN — см. вопрос пользователю), ЕСИА валидирует подпись и
    возвращает нас на ЭТП.
 4) На странице реестра вызываем Procedure.list через fetch() прямо в
    контексте страницы (execute_async_script). Браузер сам подставит cookies
    (democom_etpsid) и TLS, поэтому прямые requests из Python тут не нужны
    (они и не работают — сервер фильтрует по JA3/SNI).
 5) Пагинация: берём по N процедур за запрос, пока не соберём все
    result.totalCount.
 6) Сохраняем в output/: procedures.json, procedures.csv (BOM UTF-8 для
    Excel) и procedures.xlsx.
 7) В консоль печатаем сводку: всего / топ по дате / топ по сумме /
    распределение по trend_pur.

Использование:

    python parse_procedures.py                       # полный прогон
    python parse_procedures.py --limit 100           # только первые 100
    python parse_procedures.py --since 2026-01-01    # с этой даты публикации
    python parse_procedures.py --output custom_dir   # другая папка вывода
    python parse_procedures.py --chrome-port 9333    # другой порт DevTools
    python parse_procedures.py --kill-chrome         # убить Chrome перед
                                                       запуском (если висит)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------------

TARGET_URL = "https://etpgaz.gazprombank.ru/#com/procedure/index"
RPC_URL = "/index.php?rpctype=direct&module=default&client=etp"
DEFAULT_PORT = 9222
DEFAULT_BATCH = 200  # за сколько штук просить пачку (Procedure.list)
DEFAULT_OUTPUT = "output"

# Последовательность CSS/JS-селекторов внутри ESIA для автоклика.
# На момент разведки (21.04.2026) кнопки «Электронная подпись» и «Продолжить»
# имеют определённые классы.
ESIA_EDS_TILE_SELECTORS = [
    'esia-eds button.plain-button',
    'button.plain-button.plain-button_wide',
]


# ----------------------------------------------------------------------------
# Логирование
# ----------------------------------------------------------------------------

_LEVEL_PREFIX = {
    "INFO": "[i]",
    "OK": "[+]",
    "WARN": "[!]",
    "ERR": "[x]",
    "STEP": "==>",
}


def log(msg: str, level: str = "INFO") -> None:
    try:
        sys.stdout.write(f"{_LEVEL_PREFIX.get(level, '[i]')} {msg}\n")
    except UnicodeEncodeError:
        sys.stdout.write(
            f"{_LEVEL_PREFIX.get(level, '[i]')} "
            f"{msg.encode('ascii', 'replace').decode('ascii')}\n"
        )
    sys.stdout.flush()


# ----------------------------------------------------------------------------
# Chrome launcher (перенесено из explore_etp.py, отдельный модуль не делаем)
# ----------------------------------------------------------------------------

def _find_chrome_exe() -> Path | None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _chrome_user_data_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def _is_port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def _kill_chrome_processes() -> int:
    killed = 0
    try:
        import psutil  # type: ignore
    except ImportError:
        log("psutil не установлен — не могу убить процессы Chrome", level="WARN")
        return 0
    for proc in psutil.process_iter(["name", "pid"]):
        name = (proc.info.get("name") or "").lower()
        if name in ("chrome.exe", "chromedriver.exe"):
            try:
                proc.kill()
                killed += 1
            except Exception:
                pass
    return killed


def _cleanup_profile_singletons(user_data_dir: Path) -> None:
    for root in (user_data_dir, user_data_dir / "Default"):
        if not root.exists():
            continue
        for name in ("Singleton", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = root / name
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass


def _launch_chrome_with_debug(
    user_data_dir: Path,
    profile_name: str,
    port: int,
    start_url: str,
) -> subprocess.Popen:
    chrome = _find_chrome_exe()
    if not chrome:
        raise RuntimeError("Не нашёл chrome.exe в стандартных местах.")
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        start_url,
    ]
    log(f"Запускаю Chrome: {chrome}")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        )
        kwargs["close_fds"] = True
    return subprocess.Popen(args, **kwargs)


def _wait_for_devtools(port: int, timeout: int = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _is_port_open(port):
            return True
        time.sleep(0.5)
    return False


def connect_or_launch_chrome(port: int, profile: str = "Default"):
    """Вернёт Selenium-драйвер, подключённый к Chrome с DevTools на `port`.

    Если Chrome на этом порту уже есть — просто подключимся. Иначе запустим
    нативный Chrome с нужным профилем и подождём.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    if _is_port_open(port):
        log(f"Chrome с DevTools уже слушает порт {port} — подключаюсь.")
    else:
        user_data_dir = _chrome_user_data_dir()
        _cleanup_profile_singletons(user_data_dir)
        _launch_chrome_with_debug(user_data_dir, profile, port, TARGET_URL)
        log(f"Жду DevTools на порту {port}…")
        if not _wait_for_devtools(port, timeout=60):
            raise RuntimeError(
                f"Chrome не поднял DevTools на {port} за 60 сек.\n"
                "Попробуйте закрыть все окна Chrome и запустить ещё раз "
                "с флагом --kill-chrome."
            )
        log("DevTools поднялся, подключаю Selenium.", level="OK")

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = webdriver.Chrome(options=options)
    # Критично: без таймаута execute_async_script может висеть бесконечно,
    # если fetch() не резолвит (сеть/сервер молчат).
    try:
        driver.set_script_timeout(20)
        driver.set_page_load_timeout(30)
    except Exception:
        pass
    return driver


# ----------------------------------------------------------------------------
# Авторизация через ЕСИА+ЭП (автоклики)
# ----------------------------------------------------------------------------

def _wait_until(driver, predicate, timeout: int, poll: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def _switch_to_etp_tab(driver) -> None:
    """Переключается на вкладку etpgaz.gazprombank.ru, если она открыта."""
    for handle in driver.window_handles:
        try:
            driver.switch_to.window(handle)
            if "etpgaz.gazprombank.ru" in (driver.current_url or ""):
                return
        except Exception:
            continue


_LIST_CONTRAGENTS_JS = r"""
return (() => {
  const isVisible = (el) => {
    if (!el) return false;
    if (el.offsetParent === null) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10) return false;
    return true;
  };

  // Быстрый старт: ищем radio-кнопки. Их на странице обычно МАЛО
  // (отличие от кучи div-ов). От radio идём вверх к контейнеру.
  const radios = Array.from(document.querySelectorAll('input[type="radio"]'))
    .filter((r) => isVisible(r) && !r.disabled);
  if (radios.length === 0) return null;

  const markerRe = /Выберите\s+контрагента|Авторизация\s+через\s+ЕСИА/i;
  const userRe = /Пользователь:/i;
  const btnRe = /^Выбрать\b/i;

  // Группируем radio по общему предку, содержащему маркерные фразы.
  const containerFor = (r) => {
    let p = r.parentElement;
    for (let d = 0; p && d < 10; d++, p = p.parentElement) {
      const t = (p.innerText || '');
      if (markerRe.test(t) && userRe.test(t)) {
        // нашли потенциальный контейнер — поднимемся ещё чуть-чуть,
        // если кнопка «Выбрать» лежит выше.
        let holder = p;
        for (let k = 0; k < 4 && holder.parentElement; k++) {
          const ht = (holder.parentElement.innerText || '');
          if (btnRe.test(ht)) {
            const btnHere = Array.from(
              holder.parentElement.querySelectorAll(
                'button, input[type="button"], input[type="submit"], .x-btn, .x-btn-text'
              )
            ).some((b) => {
              if (!isVisible(b)) return false;
              const bt = (b.innerText || b.value || b.textContent || '').trim();
              return btnRe.test(bt);
            });
            if (btnHere) {
              holder = holder.parentElement;
              break;
            }
          }
          holder = holder.parentElement;
        }
        return holder;
      }
    }
    return null;
  };

  const containers = new Map(); // container → array of radios
  for (const r of radios) {
    const c = containerFor(r);
    if (!c) continue;
    if (!containers.has(c)) containers.set(c, []);
    containers.get(c).push(r);
  }
  if (containers.size === 0) return null;

  // Берём контейнер с наибольшим числом валидных radio (наш модал).
  let modal = null;
  let maxCount = -1;
  for (const [c, rs] of containers.entries()) {
    if (rs.length > maxCount) {
      modal = c;
      maxCount = rs.length;
    }
  }
  if (!modal) return null;

  // Убедимся, что кнопка «Выбрать» реально есть в выбранном контейнере.
  const hasSelect = Array.from(modal.querySelectorAll(
    'button, input[type="button"], input[type="submit"], .x-btn, .x-btn-text'
  )).some((b) => {
    if (!isVisible(b)) return false;
    const bt = (b.innerText || b.value || b.textContent || '').trim();
    return btnRe.test(bt);
  });
  if (!hasSelect) return null;

  // Собираем все активные радио-кнопки конкретно этого модала
  const modalRadios = Array.from(modal.querySelectorAll('input[type="radio"]'))
    .filter((r) => r.offsetParent !== null && !r.disabled);
  // Сохраняем ссылки для дальнейшего клика по индексу
  window.__contragent_radios = modalRadios;

  const parseLine = (text) => {
    const out = { user: '', contragent: '', inn: '', kpp: '' };
    const userReL = /Пользователь:\s*([^\n\r|]+)/;
    const contrRe = /Контрагент:\s*([^\n\r|]+)/;
    const innRe = /ИНН[\/\\]?КПП?\s*[:\s]*([\d\-\/]+)/i;
    const u = text.match(userReL);
    if (u) out.user = u[1].trim();
    const c = text.match(contrRe);
    if (c) out.contragent = c[1].trim();
    const inn = text.match(innRe);
    if (inn) out.inn = inn[1].trim();
    return out;
  };

  const items = modalRadios.map((r, i) => {
    // Ищем родительскую строку с «Пользователь: …» и «Контрагент: …»
    let node = r.parentElement;
    let bestText = '';
    let depth = 0;
    while (node && node !== modal && depth < 10) {
      const t = (node.innerText || '').trim();
      if (t && /Пользователь:|Контрагент:/i.test(t) && t.length < 800) {
        bestText = t;
        break;
      }
      node = node.parentElement;
      depth++;
    }
    if (!bestText) {
      // fallback — ближайший текстовый узел
      let p = r.parentElement;
      while (p && p !== modal) {
        const tt = (p.innerText || '').trim();
        if (tt && tt.length < 500) { bestText = tt; break; }
        p = p.parentElement;
      }
    }
    return { index: i, text: bestText, ...parseLine(bestText) };
  });
  return items;
})();
"""


_CLICK_CONTRAGENT_JS = r"""
const idx = arguments[0];
const radios = window.__contragent_radios;
if (!radios || !radios[idx]) return { ok: false, reason: 'no-radio' };
const radio = radios[idx];
try { radio.focus(); } catch(e) {}
try { radio.click(); } catch (e) {}
// Убеждаемся что radio помечен (иногда ExtJS требует дополнительно change)
try {
  if (!radio.checked) radio.checked = true;
  const ev = new Event('change', { bubbles: true });
  radio.dispatchEvent(ev);
} catch (e) {}

// Ищем кнопку "Выбрать" в любой видимой области
const buttons = Array.from(document.querySelectorAll(
  'button, input[type="button"], input[type="submit"], .x-btn, .x-btn-text'
));
let btnClicked = false;
for (const b of buttons) {
  const t = (b.innerText || b.value || b.textContent || '').trim();
  if (!/^Выбрать\b/i.test(t)) continue;
  if (b.offsetParent === null) continue;
  if (b.disabled) continue;
  try { b.click(); } catch (e) {}
  btnClicked = true;
  break;
}
return { ok: btnClicked, reason: btnClicked ? 'ok' : 'no-button' };
"""


_DEBUG_WINDOWS_JS = r"""
return (() => {
  const out = [];
  const nodes = document.querySelectorAll(
    '.x-window, .x-form-field-set, .x-panel-header, .x-window-header, ' +
    '.x-window-header-text, fieldset legend, .x-toolbar'
  );
  for (const n of nodes) {
    if (n.offsetParent === null) continue;
    const r = n.getBoundingClientRect();
    if (r.width < 5 || r.height < 5) continue;
    const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    out.push(t.slice(0, 120));
  }
  return Array.from(new Set(out)).slice(0, 20);
})();
"""


def _debug_visible_windows(driver) -> list[str]:
    try:
        res = driver.execute_script(_DEBUG_WINDOWS_JS)
        return list(res) if isinstance(res, list) else []
    except Exception:
        return []


def _list_contragents_modal(driver) -> list[dict[str, Any]] | None:
    """Возвращает список контрагентов в открытом модале, или None если модала нет."""
    try:
        items = driver.execute_script(_LIST_CONTRAGENTS_JS)
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    # Дополнительный фильтр: модал валиден только если все элементы имеют
    # хотя бы одно из полей user/contragent/text (иначе это могло быть
    # ложное срабатывание).
    valid = [
        it for it in items
        if (it.get("user") or it.get("contragent") or (it.get("text") or "").strip())
    ]
    return valid if valid else None


def _click_contragent_modal(driver, index: int) -> bool:
    """Кликает «Выбрать» и проверяет, что модал реально закрылся.

    Если после клика модал всё ещё на экране — считаем это фантомным кликом
    и возвращаем False (не спамим в цикле).
    """
    try:
        res = driver.execute_script(_CLICK_CONTRAGENT_JS, index)
    except Exception:
        return False
    if not (isinstance(res, dict) and res.get("ok")):
        return False
    # Проверяем, что модал исчез — иначе клик был куда-то не туда.
    time.sleep(0.8)
    after = _list_contragents_modal(driver)
    if after is None:
        return True
    # Модал всё ещё открыт — значит «Выбрать» не сработал
    log(
        "Клик «Выбрать» сработал визуально, но модал остался открыт. "
        "Возможно нужна ручная реакция в Chrome.",
        level="WARN",
    )
    return False


_CONTRAGENT_PROMPTED_ONCE: dict[str, bool] = {}


def _print_contragent_list(items: list[dict[str, Any]]) -> None:
    print()
    print("=" * 70)
    print("Выберите контрагента для авторизации на ЭТП ГПБ:")
    print("-" * 70)
    for i, it in enumerate(items, 1):
        user = it.get("user") or "—"
        contr = it.get("contragent") or "—"
        inn = it.get("inn") or ""
        line = f"  [{i}] Пользователь: {user} | Контрагент: {contr}"
        if inn:
            line += f" | ИНН/КПП: {inn}"
        print(line)
    print("=" * 70)


def _ask_contragent_via_tk(items: list[dict[str, Any]]) -> int | None:
    """Рисует tkinter-диалог с radio-кнопками: по одной на контрагента.

    Возвращает 1-based индекс выбранного контрагента или None,
    если пользователь закрыл окно без выбора.
    """
    try:
        import tkinter as _tk
        from tkinter import ttk as _ttk
    except Exception as e:
        log(f"tkinter недоступен: {e}", level="WARN")
        return None

    choice: dict[str, int | None] = {"value": None}
    root = _tk.Tk()
    root.title("Выбор контрагента для авторизации")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.geometry("640x360+400+250")
    except Exception:
        pass

    header = _ttk.Label(
        root,
        text="Выберите контрагента для авторизации на ЭТП ГПБ:",
        font=("Segoe UI", 11, "bold"),
    )
    header.pack(padx=16, pady=(14, 8), anchor="w")

    var = _tk.IntVar(value=1)
    frame = _ttk.Frame(root)
    frame.pack(fill="both", expand=True, padx=16, pady=4)

    for i, it in enumerate(items, 1):
        user = it.get("user") or "—"
        contr = it.get("contragent") or "—"
        inn = it.get("inn") or ""
        text = f"[{i}]  Пользователь: {user}\n       Контрагент: {contr}"
        if inn:
            text += f"\n       ИНН/КПП: {inn}"
        rb = _ttk.Radiobutton(
            frame, text=text, variable=var, value=i,
        )
        rb.pack(anchor="w", pady=4)

    def on_ok() -> None:
        choice["value"] = int(var.get())
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    btns = _ttk.Frame(root)
    btns.pack(fill="x", padx=16, pady=(8, 14))
    ok = _ttk.Button(btns, text="Выбрать", command=on_ok)
    ok.pack(side="right")
    cancel = _ttk.Button(btns, text="Отмена", command=on_cancel)
    cancel.pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    try:
        root.mainloop()
    except Exception as e:
        log(f"Ошибка tkinter: {e}", level="WARN")
        return None
    return choice["value"]


def _resolve_contragent_modal(
    driver,
    contragent_index: int | None = None,
    contragent_match: str | None = None,
) -> bool:
    """Если показан модал «Выберите контрагента» — спросить пользователя и
    кликнуть «Выбрать». После этого SPA должна сама активировать сессию и
    перейти на главную страницу.

    Приоритет:
      1. contragent_index (1-based) / contragent_match — неинтерактивно.
      2. TTY в консоли — спрашиваем номер.
      3. Иначе — tkinter-диалог с radio-кнопками.
    """
    items = _list_contragents_modal(driver)
    if not items:
        return False

    # 1) Явный index через CLI
    if contragent_index is not None:
        if 1 <= contragent_index <= len(items):
            chosen = items[contragent_index - 1]
            log(
                f"--contragent-index={contragent_index} → выбираю: "
                f"{chosen.get('user','')} / {chosen.get('contragent','')}",
                level="OK",
            )
            return _click_contragent_modal(driver, contragent_index - 1)
        log(
            f"--contragent-index={contragent_index} вне диапазона 1..{len(items)}.",
            level="WARN",
        )

    # 2) Подстрока через CLI
    if contragent_match:
        needle = contragent_match.lower()
        for i, it in enumerate(items):
            hay = " ".join(
                [
                    it.get("user") or "",
                    it.get("contragent") or "",
                    it.get("inn") or "",
                    it.get("text") or "",
                ]
            ).lower()
            if needle in hay:
                log(
                    f"--contragent-match='{contragent_match}' → выбираю #{i+1}: "
                    f"{it.get('user','')} / {it.get('contragent','')}",
                    level="OK",
                )
                return _click_contragent_modal(driver, i)
        log(
            f"--contragent-match='{contragent_match}' не найден среди {len(items)}.",
            level="WARN",
        )

    # 3) Показываем пользователю ВСЕГДА — он должен подтвердить выбор
    if not _CONTRAGENT_PROMPTED_ONCE.get("shown"):
        _print_contragent_list(items)
        _CONTRAGENT_PROMPTED_ONCE["shown"] = True

    try:
        is_tty = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        is_tty = False

    idx: int | None = None

    if is_tty:
        suffix = ""
        if len(items) == 1:
            suffix = " (по умолчанию 1, просто Enter)"
        print(
            f"\nВведите НОМЕР контрагента 1..{len(items)} и нажмите Enter{suffix}:"
        )
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if not raw and len(items) == 1:
            idx = 1
        elif raw:
            try:
                idx = int(raw)
            except ValueError:
                log(f"Не понял число: {raw!r}.", level="WARN")
                return False
    else:
        log(
            "Консоль без TTY — открываю окно выбора контрагента.",
            level="INFO",
        )
        idx = _ask_contragent_via_tk(items)

    if idx is None:
        log("Пользователь не выбрал контрагента.", level="WARN")
        return False
    if not (1 <= idx <= len(items)):
        log(f"Номер {idx} вне диапазона 1..{len(items)}.", level="WARN")
        return False
    chosen = items[idx - 1]
    log(
        f"Выбираю контрагента #{idx}: {chosen.get('user','')} / "
        f"{chosen.get('contragent','')}. Нажимаю «Выбрать»…",
        level="OK",
    )
    return _click_contragent_modal(driver, idx - 1)


def _accept_contragent_modal(
    driver,
    contragent_index: int | None = None,
    contragent_match: str | None = None,
) -> bool:
    """Обёртка для обратной совместимости: вызывает `_resolve_contragent_modal`."""
    return _resolve_contragent_modal(
        driver,
        contragent_index=contragent_index,
        contragent_match=contragent_match,
    )


_FIND_CSRF_JS = r"""
// CSRF / session token SPA может хранить в разных местах. Перебираем.
return (() => {
  try {
    const guesses = [
      () => window.Main && window.Main.token,
      () => window.App && window.App.token,
      () => window.token,
      () => window._token,
      () => window.Ext && Ext.Ajax && Ext.Ajax.defaultHeaders
        && (Ext.Ajax.defaultHeaders['X-CSRF-Token']
            || Ext.Ajax.defaultHeaders['X-Requested-Token']),
      () => {
        const m = document.querySelector(
          'meta[name="csrf-token"], meta[name="X-CSRF-TOKEN"]'
        );
        return m ? m.getAttribute('content') : null;
      },
      () => {
        // Иногда SPA кладёт токен в data-атрибут на body
        return document.body ? document.body.getAttribute('data-token') : null;
      },
      () => {
        // Или в cookie XSRF-TOKEN
        const m = document.cookie.match(/(?:^|;\s*)XSRF-TOKEN=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : null;
      },
      () => {
        // Пытаемся вытащить из Ext.direct.Manager
        if (!window.Ext || !Ext.direct || !Ext.direct.Manager) return null;
        const providers = Ext.direct.Manager.providers || {};
        for (const k in providers) {
          const p = providers[k];
          if (p && p.headers) {
            const h = p.headers['X-CSRF-Token']
              || p.headers['X-Requested-Token']
              || p.headers['token'];
            if (h) return h;
          }
        }
        return null;
      },
    ];
    for (const g of guesses) {
      try {
        const v = g();
        if (v) return String(v);
      } catch (e) {}
    }
  } catch (e) {}
  return '';
})();
"""


_AUTH_GETCONTRAGENTS_JS = r"""
const callback = arguments[arguments.length - 1];
const hash = arguments[0];
const token = arguments[1] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 12000);
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
        action: 'Authentication',
        method: 'esiagetcontragents',
        data: [{ hash: hash }],
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: token,
      }),
    });
    clearTimeout(to);
    const data = await resp.json();
    callback(data);
  } catch (e) {
    clearTimeout(to);
    callback({error: String(e)});
  }
})();
"""

_AUTH_ESIALOGIN_JS = r"""
const callback = arguments[arguments.length - 1];
const payload = arguments[0];
const token = arguments[1] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 12000);
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
        action: 'Authentication',
        method: 'esialogin',
        data: [payload],
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: token,
      }),
    });
    clearTimeout(to);
    const data = await resp.json();
    callback(data);
  } catch (e) {
    clearTimeout(to);
    callback({error: String(e)});
  }
})();
"""


_INDEX_INDEX_JS = r"""
const callback = arguments[arguments.length - 1];
const token = arguments[0] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 12000);
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
        action: 'Index',
        method: 'index',
        data: null,
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: token,
      }),
    });
    clearTimeout(to);
    const data = await resp.json();
    callback(data);
  } catch (e) {
    clearTimeout(to);
    callback({error: String(e)});
  }
})();
"""


def _activate_session_via_index(driver, token: str = "") -> dict[str, Any] | None:
    """Вызывает Index.index — это завершающий шаг login-flow, который
    активирует сессию на сервере. Без него Procedure.list вернёт no_session,
    даже если esialogin=success.
    """
    try:
        data = driver.execute_async_script(_INDEX_INDEX_JS, token)
    except Exception as e:
        log(f"Index.index упал: {e}", level="ERR")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _get_csrf_token(driver) -> str:
    try:
        v = driver.execute_script(_FIND_CSRF_JS)
        return v or ""
    except Exception:
        return ""


def _summarize_cookies(driver) -> str:
    """Коротко резюмирует cookies на текущем домене (для диагностики).

    Печатаем все имена с укороченными значениями, чтобы видеть реальный
    набор (имя PHPSESSID может оказаться любым — например, `s` или
    `sessid`).
    """
    try:
        cookies = driver.get_cookies() or []
    except Exception:
        return "<get_cookies error>"
    if not cookies:
        return "<нет cookies>"
    parts: list[str] = []
    for c in cookies:
        name = c.get("name") or ""
        val = str(c.get("value") or "")
        short_val = val[:6] + "…" if len(val) > 6 else val
        parts.append(f"{name}={short_val}")
    return f"[{len(cookies)}] " + ", ".join(parts)


def _extract_contragent_candidates(result: Any) -> list[dict[str, Any]]:
    """Достаёт список контрагентов из ответа esiagetcontragents.

    Из разведки структура ответа такая:
      result.contragents = [
        {first_name, last_name, username, id, short_name, inn, kpp, ogrn},
        ...
      ]
    Нам важны `username` (используется как `login` при esialogin) и `id`
    (user_id, пригодится как контекст).
    """
    if not isinstance(result, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for key in ("contragents", "users", "rows", "data", "list"):
        v = result.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and (item.get("username") or item.get("login")):
                    candidates.append(item)
            if candidates:
                return candidates
    if result.get("username") or result.get("login"):
        candidates.append({k: v for k, v in result.items() if not isinstance(v, (dict, list))})
    return candidates


def _complete_esia_login(driver, current_url: str) -> bool:
    """Извлекает hash из SPA URL и вызывает ЕСИА-авторизацию через RPC.

    Алгоритм:
    1. Извлекаем hash из `/#auth/esialogin/hash/<HASH>/`.
    2. Вызываем `Authentication.esiagetcontragents({hash})` — получаем
       список доступных пар (login, контрагент) для этого hash.
    3. Берём первый контрагент и вызываем `Authentication.esialogin` с
       `{login, hash, cookie_notice_accepted: false}`. Если доступно
       несколько контрагентов, но у пользователя всего один — берём его.
    """
    import re

    m = re.search(r"#auth/esialogin/hash/([A-Za-z0-9_-]+)/?", current_url)
    if not m:
        log(f"Не нашёл hash в URL: {current_url}", level="WARN")
        return False
    hash_value = m.group(1)

    token = _get_csrf_token(driver)
    log(
        f"Cookies перед esiagetcontragents: {_summarize_cookies(driver)}"
        + (f" | token={token[:10]}…" if token else " | token=<пусто>")
    )

    # Шаг 1: получаем список контрагентов
    try:
        cdata = driver.execute_async_script(
            _AUTH_GETCONTRAGENTS_JS, hash_value, token
        )
    except Exception as e:
        log(f"esiagetcontragents упал: {e}", level="ERR")
        return False

    if _DEBUG_DIR is not None:
        try:
            (_DEBUG_DIR / "debug_esiagetcontragents.json").write_text(
                json.dumps(cdata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    if not isinstance(cdata, dict) or cdata.get("error") or cdata.get("type") == "exception":
        log(f"esiagetcontragents ошибка: {cdata}", level="WARN")
        return False
    cresult = cdata.get("result") or {}
    if cresult.get("no_session"):
        log("esiagetcontragents → no_session (hash протух?).", level="WARN")
        return False

    candidates = _extract_contragent_candidates(cresult)
    if not candidates:
        log(
            f"Не нашёл контрагентов в ответе esiagetcontragents. "
            f"Ключи result: {list(cresult.keys())}",
            level="WARN",
        )
        return False

    chosen = candidates[0]
    # Сервер называет поле `username`, а в esialogin его отправляют как `login`.
    login = chosen.get("username") or chosen.get("login") or ""
    user_id = chosen.get("id")
    log(
        f"esialogin: hash={hash_value}, login={login!r}"
        + (f", user_id={user_id}" if user_id else "")
    )

    # Шаг 2: вызываем esialogin ровно с той структурой payload, которую
    # использует реальный SPA (из разведки network_log):
    #   {login, hash, cookie_notice_accepted: false}
    payload = {
        "login": login,
        "hash": hash_value,
        "cookie_notice_accepted": False,
    }

    try:
        ldata = driver.execute_async_script(_AUTH_ESIALOGIN_JS, payload, token)
    except Exception as e:
        log(f"esialogin execute_async_script упал: {e}", level="ERR")
        return False

    log(f"Cookies после esialogin: {_summarize_cookies(driver)}")

    if _DEBUG_DIR is not None:
        try:
            (_DEBUG_DIR / "debug_esialogin.json").write_text(
                json.dumps(ldata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    if not isinstance(ldata, dict) or ldata.get("error") or ldata.get("type") == "exception":
        log(f"esialogin ошибка: {ldata}", level="WARN")
        return False
    lresult = ldata.get("result") or {}
    if lresult.get("success"):
        # Запоминаем успешный token — он понадобится для Procedure.list
        # (window.Main.token в SPA может быть пустым, если SPA не
        # перезагружалась после esialogin).
        _remember_token(token)

        # КРИТИЧНО: после успешного esialogin SPA вызывает Index.index —
        # именно этот вызов активирует серверную сессию. Без него
        # Procedure.list будет возвращать no_session.
        log("esialogin=success. Вызываю Index.index для активации сессии…", level="INFO")
        idx_data = _activate_session_via_index(driver, token)
        if isinstance(idx_data, dict):
            # Сохраняем ВСЕГДА — нужно для разбора роли/permissions.
            if _DEBUG_DIR is not None:
                try:
                    (_DEBUG_DIR / "debug_index_after_esialogin.json").write_text(
                        json.dumps(idx_data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            idx_result = idx_data.get("result") or {}
            if idx_result.get("success"):
                contragent = idx_result.get("contragent") or {}
                user = idx_result.get("user") or {}
                role = user.get("role") if isinstance(user, dict) else None
                log(
                    f"Index.index OK. contragent.id={contragent.get('id')} "
                    f"role={role!r}",
                    level="OK",
                )
            else:
                log(
                    f"Index.index вернул success=false: "
                    f"{idx_result.get('message','<без сообщения>')}",
                    level="WARN",
                )
        return True
    if lresult.get("no_session"):
        log("esialogin → no_session — hash/сессия протухли.", level="WARN")
        return False
    msg = lresult.get("message") or "<без message>"
    log(f"esialogin success=false. Сервер: {msg}", level="WARN")
    return False


_AUTH_ESIAGETURL_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  try {
    const resp = await fetch('/index.php?rpctype=direct&module=default&client=etp', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: JSON.stringify({
        action: 'Authentication',
        method: 'esiageturl',
        data: null,
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: '',
      }),
    });
    const data = await resp.json();
    callback(data);
  } catch (e) {
    callback({error: String(e)});
  }
})();
"""


def _fetch_esia_login_url(driver) -> str | None:
    """Вызывает RPC Authentication.esiageturl → возвращает URL на esia.gosuslugi.ru.

    Этот метод не требует авторизации (его вызывают анонимные пользователи для
    старта логина). В ответе в result обычно лежит {url: "https://esia...."}.
    """
    try:
        data = driver.execute_async_script(_AUTH_ESIAGETURL_JS)
    except Exception as e:
        log(f"Не смог вызвать Authentication.esiageturl: {e}", level="WARN")
        return None
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    for key in ("url", "login_url", "esia_url", "href", "link"):
        v = result.get(key)
        if isinstance(v, str) and "gosuslugi.ru" in v:
            return v
    # Иногда URL лежит на верхнем уровне
    v = data.get("url")
    if isinstance(v, str) and "gosuslugi.ru" in v:
        return v
    # Диагностика: сохраним, что вернулось
    if _DEBUG_DIR is not None:
        try:
            (_DEBUG_DIR / "debug_esia_geturl.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
    return None


def _initiate_esia_login(driver) -> None:
    """Инициирует логин через ЕСИА.

    Стратегия:
    1) Сначала пробуем получить login URL через RPC Authentication.esiageturl
       и навигировать напрямую на esia.gosuslugi.ru — это самый надёжный путь.
    2) Фолбэк: навигируем на SPA-маршрут #auth/esialogin — страница сама
       вызовет нужный RPC и перенаправит.
    3) Фолбэк-2: полная перезагрузка на корень домена.
    """
    log("Запрашиваю URL для логина через ЕСИА…")
    esia_url = _fetch_esia_login_url(driver)
    if esia_url:
        log(f"Навигирую на ЕСИА: {esia_url[:120]}…")
        try:
            driver.get(esia_url)
            return
        except Exception as e:
            log(f"driver.get(esia_url) упал: {e}", level="WARN")

    log("Фолбэк: пытаюсь SPA-маршрут #auth/esialogin")
    try:
        driver.get("https://etpgaz.gazprombank.ru/#auth/esialogin")
    except Exception:
        pass
    time.sleep(2)
    if "esia.gosuslugi.ru" in (driver.current_url or ""):
        return

    log("Фолбэк-2: hard-reload на корень домена")
    try:
        driver.get("about:blank")
        time.sleep(0.3)
        driver.get("https://etpgaz.gazprombank.ru/")
    except Exception:
        pass


def ensure_authenticated(driver, esia_timeout: int = 600) -> None:
    """Гарантирует, что в Chrome есть живая сессия на ЭТП.

    Подход упрощённый: НИКАКОГО автологина. Просто пробуем Procedure.list.
    Если сервер отвечает — идём качать. Если нет — просим пользователя
    войти вручную и ждём, периодически пробуя Procedure.list.

    Важный нюанс: даже если пользователь УЖЕ залогинен в другой вкладке
    Chrome и сервер ответил `no_access`, это значит что в нашей вкладке
    SPA не успела подхватить свежий `window.Main.token`. Делаем refresh.
    """
    _switch_to_etp_tab(driver)

    cur = driver.current_url or ""
    if "etpgaz.gazprombank.ru" not in cur and "esia.gosuslugi.ru" not in cur:
        log(f"Сейчас на {cur}, открываю ЭТП…")
        try:
            driver.get(TARGET_URL)
        except Exception:
            pass
        time.sleep(3)

    log("Проверяю сессию через тестовый вызов Procedure.list (limit=1)…")
    _pull_and_remember_main_token(driver)
    if _session_alive(driver):
        log("Сессия активна — сразу парсю.", level="OK")
        _goto_registry_if_needed(driver)
        return

    # Может быть: пользователь залогинен, но в нашей вкладке SPA не
    # переинициализировалась (window.Main.token пустой). Один раз обновим
    # вкладку — если cookie сессии уже есть, SPA загрузит пользователя и
    # проставит свежий token. Подождём и снова пробуем.
    log("Сессия не подхватилась. Обновляю вкладку (F5), чтобы SPA взяла свежий токен…", level="INFO")
    try:
        driver.refresh()
    except Exception as e:
        log(f"driver.refresh() упал: {e}", level="WARN")
    time.sleep(4)
    _wait_until(driver, lambda: _is_rpc_ready(driver), timeout=15)
    _pull_and_remember_main_token(driver)
    if _session_alive(driver):
        log("Сессия активна после refresh — продолжаю.", level="OK")
        _goto_registry_if_needed(driver)
        return

    # Всё ещё не работает — разберёмся, что именно вернул сервер.
    probe = _session_alive_probe(driver)
    result = (probe or {}).get("result") or {}
    if result.get("no_access"):
        msg = (result.get("message") or "").split("<br>")[0][:200]
        log(f"Сервер: {msg}", level="WARN")
        log(
            "Текущий пользователь не имеет прав на Procedure.list. "
            "Авторизуйтесь под сертификатом сотрудника с доступом к реестру.",
            level="WARN",
        )
    elif result.get("no_session"):
        log("Сервер: нет активной сессии. Нужен вход через ЕСИА + ЭП.", level="WARN")

    print()
    print("=" * 70)
    print("Нужна АВТОРИЗАЦИЯ. В открытом окне Chrome:")
    print("  1) Переключитесь на вкладку ЭТП ГПБ (или любую другую — неважно)")
    print("  2) Нажмите «Войти» → «ЕСИА + Электронная подпись»")
    print("  3) Пройдите ЕСИА, выберите сертификат СОТРУДНИКА (не организации)")
    print("  4) Подтвердите контрагента в модале «Выберите контрагента»")
    print("  5) Дождитесь загрузки реестра процедур")
    print(f"  URL: {TARGET_URL}")
    print("=" * 70)
    log(
        f"Жду активной сессии до {esia_timeout} сек. Пока вы авторизуетесь, "
        "периодически проверяю Procedure.list и обновляю вкладку…",
        level="INFO",
    )

    deadline = time.time() + esia_timeout
    last_msg = 0.0
    last_refresh = time.time()
    while time.time() < deadline:
        _pull_and_remember_main_token(driver)
        if _session_alive(driver):
            log("Сессия стала активной — продолжаю.", level="OK")
            _goto_registry_if_needed(driver)
            return
        now = time.time()
        # Раз в ~30 сек обновляем нашу вкладку, чтобы SPA подхватила
        # сессию, если пользователь логинился в другой вкладке/окне.
        if now - last_refresh > 30:
            last_refresh = now
            try:
                cur_url = driver.current_url or ""
            except Exception:
                cur_url = ""
            if "etpgaz.gazprombank.ru" in cur_url and "esia" not in cur_url:
                log("Периодический refresh вкладки для обновления токена…", level="INFO")
                try:
                    driver.refresh()
                except Exception:
                    pass
                time.sleep(3)
        if now - last_msg > 15:
            last_msg = now
            try:
                cur_url = driver.current_url or ""
            except Exception:
                cur_url = ""
            remain = int(deadline - now)
            log(f"Осталось {remain} сек. Текущий URL: {cur_url}", level="INFO")
        time.sleep(2)

    raise RuntimeError(
        "Не дождался ручной авторизации. "
        "Войдите в Chrome под сертификатом с правами на реестр процедур "
        "и запустите парсер ещё раз."
    )


_INDEX_INDEX_FETCH_JS = r"""
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
        action: 'Index',
        method: 'index',
        data: null,
        type: 'rpc',
        tid: 1,
        token: '',
      }),
    });
    clearTimeout(to);
    const data = await resp.json();
    const r = data.result || {};
    callback({
      success: !!r.success,
      authToken: r.auth_token || null,
      mainModule: r.main_module || null,
      userLogin: (r.user && (r.user.login || r.user.username)) || null,
    });
  } catch (e) {
    clearTimeout(to);
    callback({error: String(e)});
  }
})();
"""


def _pull_and_remember_main_token(driver) -> str:
    """Забирает CSRF-токен из SPA и кладёт его в кэш `_LAST_CSRF_TOKEN`.

    У ЭТП ГПБ токен хранится в `window.Main.requestToken` (заполняется SPA
    после ответа Index.index), а `window.Main.token` для обычных
    поставщиков равен null. Если ни там, ни там токена нет — мы сами
    дёрнем Index.index и возьмём `result.auth_token` из ответа.
    """
    try:
        token = driver.execute_script(
            """
            const m = window.Main || {};
            return String(m.requestToken || m.token || '');
            """
        )
    except Exception:
        token = ""

    if not token:
        # Пытаемся разбудить SPA через Index.index. Сервер отвечает всем,
        # включая анонимам — и внутри ответа есть auth_token для этой сессии.
        try:
            resp = driver.execute_async_script(_INDEX_INDEX_FETCH_JS)
        except Exception:
            resp = None
        if isinstance(resp, dict) and resp.get("authToken"):
            token = str(resp["authToken"])
            log(f"Получил CSRF-токен через Index.index (…{token[-8:]}).", level="OK")

    if isinstance(token, str) and token and token != _get_remembered_token():
        _remember_token(token)
        log(f"Токен в кэше (…{token[-8:]}).", level="OK")
    return token or ""


def _goto_registry_if_needed(driver) -> None:
    """Если URL не #com/procedure/index — перейдём. Нужно, чтобы SPA
    инициализировала Ext.Direct для дальнейших fetch-ов (token и пр.)."""
    try:
        cur = driver.current_url or ""
    except Exception:
        cur = ""
    if "#com/procedure/index" not in cur:
        log(f"Навигирую на {TARGET_URL}…")
        try:
            driver.get(TARGET_URL)
        except Exception:
            pass
        time.sleep(3)
    log("Жду инициализации Ext.Direct…")
    _wait_until(driver, lambda: _is_rpc_ready(driver), timeout=15)
    time.sleep(1)
    _pull_and_remember_main_token(driver)


def _await_etp_session(
    driver,
    esia_timeout: int = 180,
    cert_index: int | None = None,
    cert_match: str | None = None,
    contragent_index: int | None = None,
    contragent_match: str | None = None,
) -> None:
    """После инициации логина ЕСИА ожидает, когда ЭТП активирует сессию.

    Сценарии:
    - На ЕСИА открыт экран выбора способа входа → автоклик «Продолжить».
    - Мы на промежуточном SPA-роуте `/#auth/esialogin/hash/XXX/` — значит
      ЕСИА уже отдала hash, но SPA ЭТП не обработала его. Перезагружаем
      страницу, SPA сама вызовет Authentication.esialogin.
    - Открылся нативный диалог выбора сертификата КриптоПро — ждём, пока
      пользователь выберет (если настроено без диалога — ничего не будет).
    """
    deadline = time.time() + esia_timeout
    last_stage = ""
    spa_hash_wait_started: float | None = None
    manual_login_tried_for_hash: set[str] = set()

    while time.time() < deadline:
        cur = driver.current_url or ""

        if "esia.gosuslugi.ru" in cur:
            stage = "esia"
            if stage != last_stage:
                log("Мы на ЕСИА.")
                last_stage = stage

            # Сначала проверяем, не открыт ли экран «Выберите сертификат
            # для входа» — тут нужен выбор (--cert-index/--cert-match или
            # ручной клик).
            if _esia_prompt_certificate_choice(
                driver, cert_index=cert_index, cert_match=cert_match
            ):
                log(
                    "Сертификат выбран, жду следующий экран ЕСИА…",
                    level="OK",
                )
                time.sleep(3)
                continue

            # Иначе (один сертификат по умолчанию или экран «Способ входа»)
            # делаем автоклик «Электронная подпись» / «Продолжить».
            _esia_auto_login(driver)
            time.sleep(2)
            continue

        # Критично: если SPA показала модал «Выберите контрагента» — выбираем
        # нужного. Именно этот клик заставляет SPA вызвать esialogin от её
        # имени (с правильным CSRF), после чего сессия активируется.
        if "etpgaz.gazprombank.ru" in cur and _accept_contragent_modal(
            driver,
            contragent_index=contragent_index,
            contragent_match=contragent_match,
        ):
            log(
                "Подтвердил модал «Выберите контрагента» (SPA вызовет esialogin).",
                level="OK",
            )
            time.sleep(4)
            continue

        if "#auth/esialogin/hash/" in cur and "etpgaz.gazprombank.ru" in cur:
            stage = "spa-hash"
            if stage != last_stage:
                log(
                    "Попали на SPA-hash. Даю SPA до 45 сек показать модал "
                    "«Выберите контрагента»…"
                )
                last_stage = stage
                spa_hash_wait_started = time.time()
                # Диагностика — какие окна вообще видны SPA сейчас.
                vis = _debug_visible_windows(driver)
                if vis:
                    log(f"Видимые блоки SPA: {vis[:5]}…", level="INFO")

            if spa_hash_wait_started is not None:
                waited = time.time() - spa_hash_wait_started

                # Уже активна?
                if _session_alive(driver):
                    log("SPA сама активировала сессию.", level="OK")
                    return

                # Модал появился? (_accept_contragent_modal уже обрабатывается
                # блоком выше в начале итерации, но на hash-URL проверяем
                # прицельно и сразу.)
                if _accept_contragent_modal(
                    driver,
                    contragent_index=contragent_index,
                    contragent_match=contragent_match,
                ):
                    log(
                        "Поймал модал «Выберите контрагента» на hash-URL, "
                        "SPA должна сама вызвать esialogin.",
                        level="OK",
                    )
                    time.sleep(4)
                    continue

                if waited < 45:
                    if int(waited) % 10 == 0 and int(waited) > 0:
                        vis = _debug_visible_windows(driver)
                        log(
                            f"Ждём SPA, прошло {int(waited)} сек. "
                            f"Видимо: {vis[:5]}",
                            level="INFO",
                        )
                    time.sleep(1.5)
                    continue

            # SPA не справилась за 45 сек → fallback: вручную вызываем
            # esialogin через RPC. Делаем это только ОДИН раз на каждый hash.
            import re as _re

            m = _re.search(r"#auth/esialogin/hash/([A-Za-z0-9_-]+)", cur)
            this_hash = m.group(1) if m else ""
            if this_hash and this_hash not in manual_login_tried_for_hash:
                manual_login_tried_for_hash.add(this_hash)
                log(
                    "SPA не справилась сама, вызываю Authentication.esialogin вручную…",
                    level="WARN",
                )
                if _complete_esia_login(driver, cur):
                    log(
                        "Authentication.esialogin прошёл. Проверяю сессию без перезагрузки страницы…",
                        level="OK",
                    )
                    # Первый раз — с подробным логом что ответил сервер.
                    time.sleep(1)
                    if _session_alive(driver, debug=True):
                        log("Сессия активна после ручного esialogin.", level="OK")
                        return
                    # Проверяем не no_access ли это (нет прав у текущего
                    # пользователя) — в этом случае auto-login бесполезен,
                    # нужно входить под другим сертификатом.
                    probe_data = _session_alive_probe(driver)
                    probe_result = (probe_data or {}).get("result") or {}
                    if probe_result.get("no_access"):
                        msg = probe_result.get("message") or ""
                        log(
                            "Через fetch() сервер вернул no_access. "
                            "Пробую тот же вызов через родной Ext.Direct "
                            "(возможно, сервер фильтрует по инициатору)…",
                            level="INFO",
                        )
                        native = _session_alive_native(driver)
                        if native is not None:
                            log(
                                f"native probe: {str(native)[:300]}",
                                level="INFO",
                            )
                            nres = (native or {}).get("result") or {}
                            if (
                                isinstance(nres, dict)
                                and (
                                    "totalCount" in nres or "procedures" in nres
                                )
                            ):
                                log(
                                    "Родной Ext.Direct сработал — "
                                    "переключаю парсер на SPA-канал.",
                                    level="OK",
                                )
                                _USE_NATIVE_EXT["value"] = True
                                return
                        log(
                            f"У текущего пользователя НЕТ прав на "
                            f"Procedure.list. Сервер: {msg[:200]!r}",
                            level="ERR",
                        )
                        log(
                            "Выход: войдите в Chrome ВРУЧНУЮ под другим "
                            "сертификатом (с правами). Парсер будет ждать "
                            "активной сессии и продолжит автоматически.",
                            level="WARN",
                        )
                        # Остановим автологин и будем ждать сессию пассивно.
                        return _wait_session_passive(driver, max(30.0, deadline - time.time()))
                    for _ in range(5):
                        time.sleep(2)
                        if _session_alive(driver):
                            log("Сессия активна после ручного esialogin.", level="OK")
                            return
                    log(
                        "esialogin=true, но сессия мертва. Вероятно PHPSESSID "
                        "разошёлся. Запрашиваю новый hash через ЕСИА…",
                        level="WARN",
                    )
                    _initiate_esia_login(driver)
                    spa_hash_wait_started = None
                    time.sleep(3)
                    continue
                else:
                    log("Ручной esialogin не удался. Запрашиваю новый hash…", level="WARN")
                    _initiate_esia_login(driver)
                    spa_hash_wait_started = None
                    time.sleep(3)
                    continue
            else:
                log(
                    f"Hash {this_hash} уже пробовали вручную без результата. "
                    "Запрашиваю новый через ЕСИА…",
                    level="WARN",
                )
                _initiate_esia_login(driver)
                manual_login_tried_for_hash.clear()
                spa_hash_wait_started = None
                time.sleep(3)
                continue

        if "etpgaz.gazprombank.ru" in cur and _session_alive(driver):
            log("Сессия на ЭТП активна.", level="OK")
            return

        time.sleep(2)

    raise RuntimeError(
        f"Не дождался активной сессии за {esia_timeout} сек. "
        "Возможные причины: открыт диалог выбора сертификата КриптоПро, "
        "истёк срок действия сертификата, нет сети. "
        "Завершите ручной вход в Chrome и запустите парсер повторно."
    )


_SESSION_PROBE_JS = r"""
const callback = arguments[arguments.length - 1];
const explicitToken = arguments[0] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 10000);
  try {
    // У ЭТП ГПБ CSRF-токен лежит в Main.requestToken (а не в Main.token,
    // который для обычных поставщиков = null). Его заполняет сам SPA после
    // ответа Index.index → result.auth_token.
    const token = explicitToken
      || (window.Main && (Main.requestToken || Main.token))
      || '';
    const resp = await fetch('/index.php?rpctype=direct&module=default&client=etp', {
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
        data: [{
          sort: 'id',
          dir: 'DESC',
          with_affiliates: true,
          date_published_from: '01.01.2026',
          query: null,
          tag_id: null,
          limit: 1,
        }],
        type: 'rpc',
        tid: Math.floor(Math.random() * 1000000),
        token: token,
      }),
    });
    clearTimeout(to);
    const data = await resp.json();
    callback(data);
  } catch (e) {
    clearTimeout(to);
    callback({error: String(e)});
  }
})();
"""


_LAST_CSRF_TOKEN: dict[str, str] = {}
_USE_NATIVE_EXT: dict[str, bool] = {"value": False}


def _remember_token(token: str) -> None:
    """Сохраняет последний валидный CSRF-токен для Procedure.list."""
    if token:
        _LAST_CSRF_TOKEN["value"] = token


def _get_remembered_token() -> str:
    return _LAST_CSRF_TOKEN.get("value", "")


def _session_alive_probe(driver, token: str | None = None) -> dict[str, Any] | None:
    """Сырой ответ от Procedure.list — нужен чтобы отличить no_session от
    no_access (нет прав у текущего пользователя)."""
    use_token = token if token is not None else _get_remembered_token()
    try:
        data = driver.execute_async_script(_SESSION_PROBE_JS, use_token)
    except Exception:
        return None
    if isinstance(data, dict):
        return data
    return None


_NATIVE_PROBE_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  // Пробуем родной Ext.Direct — если namespace готов, он не требует нам знать
  // token/URL, сам подставит. Если SPA «не загружена» — падаем.
  try {
    if (typeof Procedure === 'undefined' ||
        typeof Procedure.list !== 'function') {
      callback({_native_ready: false});
      return;
    }
  } catch (e) {
    callback({_native_ready: false, _err: String(e)});
    return;
  }
  let done = false;
  const to = setTimeout(() => {
    if (!done) callback({_native_ready: true, _timeout: true});
  }, 10000);
  try {
    Procedure.list({
      sort: 'id',
      dir: 'DESC',
      with_affiliates: true,
      date_published_from: '01.01.2026',
      query: null,
      tag_id: null,
      limit: 1,
    }, function(result, event) {
      if (done) return;
      done = true;
      clearTimeout(to);
      callback({_native_ready: true, result: result, _ok: !!(event && event.status)});
    });
  } catch (e) {
    done = true;
    clearTimeout(to);
    callback({_native_ready: true, _err: String(e)});
  }
})();
"""


def _session_alive_native(driver) -> dict[str, Any] | None:
    """Пробуем вызвать Procedure.list через родной Ext.Direct API.
    Нужно чтобы отличить: «сервер блокирует только наш fetch» от «роль
    реально не имеет прав»."""
    try:
        data = driver.execute_async_script(_NATIVE_PROBE_JS)
    except Exception:
        return None
    if isinstance(data, dict):
        return data
    return None


def _wait_session_passive(driver, timeout: float) -> None:
    """Ждём живую сессию без попыток автологина.

    Используется после того, как автологин определил, что текущий
    сертификат/пользователь не имеет прав — требуется ручной вход в Chrome
    под другим аккаунтом. Мы просто поллим Procedure.list, а когда он
    вернёт нормальный ответ (totalCount/procedures) — выходим.
    """
    import time as _time

    deadline = _time.time() + max(timeout, 30)
    log(
        f"Жду активной сессии (ручной вход в Chrome) до "
        f"{int(timeout)} сек…",
        level="INFO",
    )
    last_url = ""
    last_print = 0.0
    while _time.time() < deadline:
        cur = driver.current_url or ""
        if cur != last_url:
            log(f"URL: {cur}", level="INFO")
            last_url = cur
        # Сначала пробуем запомненный token, потом свежий window.Main.token.
        if _session_alive(driver):
            log("Сессия стала активной — продолжаю парсинг.", level="OK")
            return
        # Если SPA загрузилась и window.Main.token появился, запомним его.
        try:
            main_token = driver.execute_script(
                "return (window.Main && Main.token) ? Main.token : '';"
            )
        except Exception:
            main_token = ""
        if main_token and main_token != _get_remembered_token():
            _remember_token(main_token)
            log("Подхватил свежий window.Main.token из SPA.", level="OK")
        now = _time.time()
        if now - last_print > 15:
            last_print = now
            log(
                f"Сессия всё ещё не активна, осталось "
                f"{int(deadline - now)} сек…",
                level="INFO",
            )
        _time.sleep(2)
    raise RuntimeError(
        "Не дождался ручного входа в Chrome. Войдите под сертификатом с "
        "правами на Procedure.list и запустите парсер повторно."
    )


def _session_alive(driver, token: str | None = None, debug: bool = False) -> bool:
    """True, если Procedure.list с limit=1 возвращает непустой ответ.

    Принимает опциональный `token` — CSRF-токен, полученный при ручном
    Authentication.esialogin. Нужен потому что `window.Main.token` в SPA
    может быть пустым (если SPA не перезагружалась после esialogin).
    """
    use_token = token if token is not None else _get_remembered_token()
    try:
        data = driver.execute_async_script(_SESSION_PROBE_JS, use_token)
    except Exception as e:
        if debug:
            log(f"_session_alive execute_async_script упал: {e}", level="WARN")
        return False
    if not isinstance(data, dict):
        if debug:
            log(f"_session_alive: странный ответ не-dict: {type(data).__name__}",
                level="WARN")
        return False
    if data.get("error") or data.get("type") == "exception":
        if debug:
            log(
                f"_session_alive: exception/error: {str(data)[:200]}",
                level="WARN",
            )
        return False
    result = data.get("result")
    if not isinstance(result, dict):
        if debug:
            log(
                f"_session_alive: result не dict: {str(data)[:200]}",
                level="WARN",
            )
        return False
    if result.get("no_session") or result.get("no_access"):
        if debug:
            msg = result.get("message") or ""
            log(
                f"_session_alive: no_session/no_access. msg={msg[:120]!r}",
                level="WARN",
            )
        return False
    if "totalCount" in result or "procedures" in result:
        return True
    if debug:
        log(
            f"_session_alive: неожиданный result-ответ: {str(result)[:200]}",
            level="WARN",
        )
    return bool(result.get("success") is True)


def _is_no_session_result(result: dict[str, Any]) -> bool:
    """Сервер вернул типичный ответ 'нужно логиниться'."""
    if not isinstance(result, dict):
        return False
    return bool(result.get("no_session") or result.get("no_access"))


def _is_on_target_registry(driver) -> bool:
    """True, если мы на странице реестра ЭТП (не на ЕСИА)."""
    url = driver.current_url or ""
    if "esia.gosuslugi.ru" in url:
        return False
    if "etpgaz.gazprombank.ru" not in url:
        return False
    # После ЕСИА SPA может быть либо на #com/procedure/index,
    # либо на #auth/esialogin/hash/... (промежуточный URL, пока SPA
    # обменивает хэш на сессию). В обоих случаях — уже на ЭТП.
    return True


def _is_rpc_ready(driver) -> bool:
    """Быстрая проверка: возможен ли прямой fetch к Procedure.list.

    Идём по нарастающей: сперва проверяем, что Ext.Direct.Manager
    зарегистрировал Procedure.list; если да — точно готовы. Если нет, но URL
    уже на реестре — отдаём True спустя небольшую задержку (SPA может ещё
    дожёвывать ext-all.js).
    """
    try:
        ready = driver.execute_script(
            """
            if (window.Ext && window.Ext.Direct && Ext.Direct.Manager) {
              // ExtJS 3.x: Ext.Direct.Manager.getProvider()
              try {
                const prov = Ext.Direct.Manager.getProvider
                  ? Ext.Direct.Manager.getProvider()
                  : null;
                if (prov && prov.isConnected && prov.isConnected()) return true;
              } catch (e) {}
            }
            if (window.Procedure && typeof Procedure.list === 'function') return true;
            return false;
            """
        )
        return bool(ready)
    except Exception:
        return False


_ESIA_LIST_CERTS_JS = r"""
return (() => {
  // Ищем заголовок «Выберите сертификат для входа» на странице ЕСИА.
  const headerRe = /Выберите\s+сертификат\s+для\s+входа/i;
  const allText = Array.from(
    document.querySelectorAll('h1, h2, h3, h4, div, span, p, section')
  );
  const hasHeader = allText.some((e) => headerRe.test((e.innerText || '').trim()));
  if (!hasHeader) return null;

  // Ищем потенциальные карточки сертификатов: элементы, содержащие
  // «Физическое лицо» или «Юридическое лицо» и компактный текст.
  const roleRe = /(Физическое лицо|Юридическое лицо|Индивидуальный предприниматель)/;
  const nodes = Array.from(
    document.querySelectorAll('div, a, li, article, section, button')
  );
  let cards = [];
  for (const n of nodes) {
    const t = (n.innerText || '').trim();
    if (!t) continue;
    if (!roleRe.test(t)) continue;
    if (t.length > 500) continue;
    cards.push(n);
  }

  // Убираем вложенные: если карточка содержит другую карточку — оставляем внутреннюю.
  cards = cards.filter(
    (c) => !cards.some((o) => o !== c && c.contains(o))
  );

  // Сохраним узлы глобально, чтобы можно было кликнуть по index.
  window.__esia_cert_nodes = cards;

  // Парсим каждую карточку.
  const parseCard = (text) => {
    const lines = text
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean);
    // Обычно порядок:
    //   [0] "Физическое лицо" / "Юридическое лицо"
    //   [1] ФИО
    //   [2] Организация
    //   [3] Срок (ДД.ММ.ГГГГ—ДД.ММ.ГГГГ)
    const roleLine = lines.find((l) => roleRe.test(l)) || '';
    const role = (roleLine.match(roleRe) || [''])[0];
    const validityLine =
      lines.find((l) =>
        /\d{1,2}\.\d{1,2}\.\d{2,4}\s*[—\-–]\s*\d{1,2}\.\d{1,2}\.\d{2,4}/.test(l)
      ) || '';
    // Имя — чаще всего строка непосредственно после роли
    const roleIdx = lines.indexOf(roleLine);
    const name = roleIdx >= 0 ? lines[roleIdx + 1] || '' : lines[1] || '';
    // Организация — строка между именем и сроком действия
    let org = '';
    if (roleIdx >= 0) {
      for (let i = roleIdx + 2; i < lines.length; i++) {
        if (lines[i] === validityLine) break;
        if (!org) org = lines[i];
      }
    }
    return { role, name, org, validity: validityLine };
  };

  return cards.map((node, i) => {
    const fields = parseCard(node.innerText || '');
    return {
      index: i,
      role: fields.role,
      name: fields.name,
      org: fields.org,
      validity: fields.validity,
    };
  });
})();
"""

_ESIA_CLICK_CERT_JS = r"""
const idx = arguments[0];
const nodes = window.__esia_cert_nodes;
if (!nodes || !nodes[idx]) return { ok: false, reason: 'no-nodes' };
const target = nodes[idx];
// Сперва пробуем клик по самой карточке (часто она clickable целиком).
try { target.click(); } catch (e) {}
// Плюс кликнем любой <a>/<button>/[role=button] внутри — на случай,
// если сама карточка не реагирует.
const inner = target.querySelector('a, button, [role="button"]');
if (inner) {
  try { inner.click(); } catch (e) {}
}
return { ok: true };
"""


def _esia_list_certificates(driver) -> list[dict[str, Any]] | None:
    """Возвращает список сертификатов, если на ЕСИА открыт экран выбора.

    Иначе `None`. Каждый элемент содержит поля: `role`, `name`, `org`,
    `validity`, `index`.
    """
    try:
        result = driver.execute_script(_ESIA_LIST_CERTS_JS)
    except Exception as e:
        log(f"Ошибка чтения сертификатов: {e}", level="WARN")
        return None
    if not isinstance(result, list) or not result:
        return None
    return result


def _esia_click_certificate(driver, index: int) -> bool:
    try:
        res = driver.execute_script(_ESIA_CLICK_CERT_JS, index)
    except Exception as e:
        log(f"Ошибка клика по сертификату: {e}", level="WARN")
        return False
    return bool(isinstance(res, dict) and res.get("ok"))


_CERT_PROMPTED_ONCE: dict[str, bool] = {}


def _print_cert_list(certs: list[dict[str, Any]]) -> None:
    print()
    print("=" * 70)
    print("ЕСИА просит выбрать сертификат для входа:")
    print("-" * 70)
    for i, c in enumerate(certs, 1):
        role = c.get("role") or "—"
        name = c.get("name") or "<имя не распознано>"
        org = c.get("org") or ""
        validity = c.get("validity") or ""
        line = f"  [{i}] {role} — {name}"
        if org:
            line += f" | {org}"
        if validity:
            line += f" | срок: {validity}"
        print(line)
    print("=" * 70)


def _esia_prompt_certificate_choice(
    driver,
    cert_index: int | None = None,
    cert_match: str | None = None,
) -> bool:
    """Если ЕСИА предлагает выбрать сертификат — выбрать нужный.

    Приоритет выбора:
      1. `cert_index` (1-based) — кликаем этот номер.
      2. `cert_match` — ищем сертификат по подстроке в ФИО/организации.
      3. Иначе — пробуем спросить пользователя в консоли через input().
         Если stdin не-TTY (запуск из IDE без интерактивного терминала),
         просто выводим список один раз и возвращаем False, ожидая ручной клик.

    Возвращает True, если клик по карточке выполнен.
    """
    certs = _esia_list_certificates(driver)
    if not certs:
        return False

    # 1) Явный cert_index
    if cert_index is not None:
        if 1 <= cert_index <= len(certs):
            chosen = certs[cert_index - 1]
            log(
                f"--cert-index={cert_index} → кликаю: {chosen.get('name','')} "
                f"({chosen.get('org','')}, {chosen.get('validity','')})",
                level="OK",
            )
            return _esia_click_certificate(driver, cert_index - 1)
        log(
            f"--cert-index={cert_index} вне диапазона 1..{len(certs)}. "
            "Сертификат не выбран.",
            level="WARN",
        )

    # 2) cert_match по подстроке
    if cert_match:
        needle = cert_match.lower()
        for i, c in enumerate(certs):
            hay = " ".join(
                [c.get("name") or "", c.get("org") or "", c.get("role") or ""]
            ).lower()
            if needle in hay:
                log(
                    f"--cert-match='{cert_match}' → кликаю #{i+1}: "
                    f"{c.get('name','')} ({c.get('org','')})",
                    level="OK",
                )
                return _esia_click_certificate(driver, i)
        log(
            f"--cert-match='{cert_match}' не найден среди {len(certs)} сертификатов.",
            level="WARN",
        )

    # 3) Интерактив: пытаемся input(). В Cursor-IDE это не сработает, но хотя
    # бы покажем список пользователю один раз, чтобы он мог выбрать мышкой.
    if not _CERT_PROMPTED_ONCE.get("shown"):
        _print_cert_list(certs)
        _CERT_PROMPTED_ONCE["shown"] = True

    import sys as _sys

    is_tty = False
    try:
        is_tty = bool(_sys.stdin and _sys.stdin.isatty())
    except Exception:
        is_tty = False

    if not is_tty:
        # stdin не-TTY — откроем tkinter-диалог с radio-кнопками.
        log(
            "Консоль без TTY — открываю диалог выбора сертификата ЕСИА.",
            level="INFO",
        )
        idx = _ask_cert_via_tk(certs)
        if idx is None:
            log(
                "Сертификат не выбран в диалоге. "
                "Кликните сертификат вручную в окне Chrome.",
                level="WARN",
            )
            return False
        if not (1 <= idx <= len(certs)):
            log(f"Номер {idx} вне диапазона 1..{len(certs)}.", level="WARN")
            return False
        chosen = certs[idx - 1]
        log(
            f"Кликаю сертификат #{idx}: {chosen.get('name', '')} "
            f"({chosen.get('org', '')}, {chosen.get('validity', '')})",
            level="OK",
        )
        return _esia_click_certificate(driver, idx - 1)

    print(
        "Введите НОМЕР сертификата и нажмите Enter "
        "(пустое значение = выбрать вручную в Chrome):"
    )
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("Ввод отменён — кликните сертификат вручную в окне Chrome.")
        return False

    if not raw:
        return False

    try:
        idx = int(raw)
    except ValueError:
        log(f"Не понял число: {raw!r}.", level="WARN")
        return False

    if not (1 <= idx <= len(certs)):
        log(f"Номер {idx} вне диапазона 1..{len(certs)}.", level="WARN")
        return False

    chosen = certs[idx - 1]
    log(
        f"Кликаю сертификат #{idx}: {chosen.get('name', '')} "
        f"({chosen.get('org', '')}, {chosen.get('validity', '')})",
        level="OK",
    )
    return _esia_click_certificate(driver, idx - 1)


def _ask_cert_via_tk(certs: list[dict[str, Any]]) -> int | None:
    """tkinter-диалог с radio-кнопками по каждому сертификату."""
    try:
        import tkinter as _tk
        from tkinter import ttk as _ttk
    except Exception as e:
        log(f"tkinter недоступен: {e}", level="WARN")
        return None

    choice: dict[str, int | None] = {"value": None}
    root = _tk.Tk()
    root.title("Выбор сертификата для входа (ЕСИА)")
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.geometry("720x460+320+200")
    except Exception:
        pass

    header = _ttk.Label(
        root,
        text="Выберите сертификат для входа через ЕСИА:",
        font=("Segoe UI", 11, "bold"),
    )
    header.pack(padx=16, pady=(14, 4), anchor="w")

    tip = _ttk.Label(
        root,
        text="Для доступа к реестру процедур нужен сертификат сотрудника "
             "(физлица) с правами на ЭТП ГПБ.",
        foreground="#555",
    )
    tip.pack(padx=16, pady=(0, 10), anchor="w")

    var = _tk.IntVar(value=1)
    frame = _ttk.Frame(root)
    frame.pack(fill="both", expand=True, padx=16, pady=4)

    for i, c in enumerate(certs, 1):
        role = c.get("role") or "—"
        name = c.get("name") or "—"
        org = c.get("org") or ""
        validity = c.get("validity") or ""
        lines = [f"[{i}]  {role}: {name}"]
        if org:
            lines.append(f"      {org}")
        if validity:
            lines.append(f"      срок: {validity}")
        rb = _ttk.Radiobutton(
            frame, text="\n".join(lines), variable=var, value=i,
        )
        rb.pack(anchor="w", pady=4)

    def on_ok() -> None:
        choice["value"] = int(var.get())
        root.destroy()

    def on_cancel() -> None:
        root.destroy()

    btns = _ttk.Frame(root)
    btns.pack(fill="x", padx=16, pady=(10, 14))
    ok = _ttk.Button(btns, text="Выбрать", command=on_ok)
    ok.pack(side="right")
    cancel = _ttk.Button(btns, text="Отмена", command=on_cancel)
    cancel.pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", on_cancel)
    try:
        root.mainloop()
    except Exception as e:
        log(f"Ошибка tkinter: {e}", level="WARN")
        return None
    return choice["value"]


# ----------------------------------------------------------------------------
# Работа с нативным окном Госплагина / КриптоПро (ввод пароля к контейнеру)
# ----------------------------------------------------------------------------

_NATIVE_PWD_DIALOG_TITLES = (
    "Госплагин",
    "КриптоПро CSP",
    "CryptoPro CSP",
    "КриптоПро",
    "Ввод пароля",
    "Pin-код",
    "PIN-код",
)


def _find_crypto_password_window():
    """Возвращает handle первого подходящего нативного окна с запросом пароля.

    Подходят окна с заголовком, содержащим один из известных маркеров
    (`Госплагин`, `КриптоПро`, `Ввод пароля`), И содержащие поле ввода типа
    Edit. Если ничего не найдено — возвращает None.
    """
    try:
        from pywinauto import findwindows  # type: ignore
    except Exception:
        return None

    for marker in _NATIVE_PWD_DIALOG_TITLES:
        try:
            handles = findwindows.find_windows(title_re=f".*{marker}.*", visible_only=True)
        except Exception:
            handles = []
        for h in handles:
            return h
    return None


def _fill_crypto_password_window(
    password: str,
    remember: bool = False,
    verbose: bool = False,
) -> bool:
    """Если сейчас на экране нативный диалог Госплагина/КриптоПро — заполнить пароль.

    Returns True, если пароль введён и OK нажат. False — окна нет либо
    что-то пошло не так.
    """
    try:
        from pywinauto import Application  # type: ignore
    except Exception:
        if verbose:
            log("pywinauto недоступен — пропускаю автозаполнение.", level="WARN")
        return False

    handle = _find_crypto_password_window()
    if not handle:
        return False

    try:
        app = Application(backend="uia").connect(handle=handle)
        w = app.window(handle=handle)
    except Exception:
        try:
            app = Application().connect(handle=handle)
            w = app.window(handle=handle)
        except Exception as e:
            if verbose:
                log(f"Не смог подключиться к окну Госплагина: {e}", level="WARN")
            return False

    try:
        w.set_focus()
    except Exception:
        pass

    # Ищем поле ввода пароля
    edit_ctrl = None
    try:
        edits = w.descendants(control_type="Edit")
        if edits:
            edit_ctrl = edits[0]
    except Exception:
        pass

    if edit_ctrl is None:
        if verbose:
            log("В окне Госплагина нет поля Edit — пропускаю.", level="WARN")
        return False

    try:
        edit_ctrl.set_edit_text(password)
    except Exception:
        try:
            edit_ctrl.set_text(password)
        except Exception:
            try:
                edit_ctrl.type_keys(password, with_spaces=True, set_foreground=True)
            except Exception as e:
                if verbose:
                    log(f"Не удалось вбить пароль: {e}", level="ERR")
                return False

    # Опционально — поставить галочку «Запомнить на сеанс работы»
    if remember:
        try:
            checkboxes = w.descendants(control_type="CheckBox")
            for cb in checkboxes:
                try:
                    state = cb.get_toggle_state()
                except Exception:
                    state = None
                if state == 0:
                    try:
                        cb.click_input()
                    except Exception:
                        try:
                            cb.toggle()
                        except Exception:
                            pass
                    break
        except Exception:
            pass

    # Нажимаем OK / ОК
    clicked = False
    try:
        for b in w.descendants(control_type="Button"):
            try:
                name = (b.window_text() or "").strip().lower()
            except Exception:
                continue
            if name in ("ok", "ок"):
                try:
                    b.click()
                except Exception:
                    try:
                        b.click_input()
                    except Exception:
                        continue
                clicked = True
                break
    except Exception:
        pass

    if not clicked:
        # Fallback: Enter
        try:
            edit_ctrl.type_keys("{ENTER}", set_foreground=True)
            clicked = True
        except Exception:
            pass

    return clicked


def _crypto_password_watcher(
    password: str,
    stop_event: threading.Event,
    remember: bool = False,
) -> None:
    """Фоновый цикл: следит за появлением окна Госплагина и заполняет пароль."""
    log("Watcher окна Госплагина запущен.", level="OK")
    last_fill = 0.0
    while not stop_event.is_set():
        try:
            # Не спамим: между заполнениями минимум 5 сек
            if time.time() - last_fill > 5:
                if _fill_crypto_password_window(password, remember=remember):
                    log("Автоввёл пароль к ключевому контейнеру.", level="OK")
                    last_fill = time.time()
        except Exception as e:
            log(f"Watcher Госплагина: {e}", level="WARN")
        stop_event.wait(timeout=0.5)
    log("Watcher окна Госплагина остановлен.")


def _start_crypto_password_watcher(
    password: str, remember: bool = False
) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    t = threading.Thread(
        target=_crypto_password_watcher,
        args=(password, stop_event),
        kwargs={"remember": remember},
        daemon=True,
    )
    t.start()
    return t, stop_event


def _get_cert_password(args) -> str | None:
    """Получает пароль к ключевому контейнеру ЭЦП.

    Приоритет:
      1. --cert-password <pass> (строго для локальной отладки).
      2. ENV var (по умолчанию TENDERS_CERT_PASSWORD).
      3. Если stdin — TTY → getpass.getpass() в консоли.
      4. Иначе → tkinter-диалог (работает даже в неинтерактивной IDE-оболочке).
      5. None — пароль не задан, пользователь будет вводить в окне вручную.
    """
    if getattr(args, "cert_password", None):
        return args.cert_password

    env_name = getattr(args, "cert_password_env", None) or "TENDERS_CERT_PASSWORD"
    env_val = os.environ.get(env_name)
    if env_val:
        log(f"Использую пароль из переменной окружения {env_name}.")
        return env_val

    try:
        is_tty = bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        is_tty = False

    if is_tty:
        try:
            import getpass as _gp

            raw = _gp.getpass(
                "Пароль к ключевому контейнеру ЭЦП "
                "(Enter — пропустить и вводить вручную в окне): "
            )
            return raw or None
        except Exception as e:
            log(f"getpass упал: {e}. Пробую tkinter-диалог…", level="WARN")

    # Fallback: tkinter password dialog
    try:
        import tkinter as _tk
        from tkinter import simpledialog as _sd

        root = _tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        pw = _sd.askstring(
            "Пароль ЭЦП",
            "Введите пароль к ключевому контейнеру ЭЦП (Госплагин/КриптоПро).\n"
            "Можно оставить пустым — тогда введёте вручную в окне Госплагина.",
            show="*",
        )
        try:
            root.destroy()
        except Exception:
            pass
        return (pw or "").strip() or None
    except Exception as e:
        log(
            f"tkinter-диалог недоступен: {e}. "
            "Пароль не получен — вводите вручную в окне Госплагина.",
            level="WARN",
        )
        return None


def _esia_auto_login(driver) -> None:
    """Кликает «Электронная подпись» (если предложено) и «Продолжить»."""
    end = time.time() + 60
    clicked_continue = False

    while time.time() < end and not clicked_continue:
        try:
            clicked_continue = driver.execute_script(
                """
                function findButtonByText(texts) {
                  const nodes = document.querySelectorAll('button');
                  for (const n of nodes) {
                    const t = (n.innerText || '').trim();
                    for (const target of texts) {
                      if (t === target || t.includes(target)) return n;
                    }
                  }
                  return null;
                }

                // Шаг 1: если на странице несколько способов входа — выберем ЭП.
                const btnEds = findButtonByText(['Электронная подпись']);
                if (btnEds) {
                  btnEds.click();
                }

                // Шаг 2: нажать «Продолжить» (основная кнопка подписи).
                const btnContinue = findButtonByText(['Продолжить']);
                if (btnContinue && !btnContinue.disabled) {
                  btnContinue.click();
                  return true;
                }
                return false;
                """
            )
        except Exception as e:
            log(f"Ошибка автоклика на ЕСИА: {e}", level="WARN")
        if not clicked_continue:
            time.sleep(1)

    if not clicked_continue:
        log(
            "Не нашёл кнопку «Продолжить» на странице ЕСИА за 60 сек. "
            "Возможно, нужно нажать вручную в открытом Chrome.",
            level="WARN",
        )


# ----------------------------------------------------------------------------
# Вызов Procedure.list через fetch() в контексте страницы
# ----------------------------------------------------------------------------

_FETCH_PROC_LIST_JS = r"""
const callback = arguments[arguments.length - 1];
const payload = arguments[0];
const rpcUrl = arguments[1];
const explicitToken = arguments[2] || '';
(async () => {
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 15000);
  try {
    const tokenGuess = explicitToken ||
      (window.Main && (window.Main.requestToken || window.Main.token)) ||
      (window.App && window.App.token) ||
      (window.Ext && Ext.Ajax && Ext.Ajax.defaultHeaders
        ? Ext.Ajax.defaultHeaders['X-CSRF-Token']
        : null) ||
      '';
    const body = {
      action: 'Procedure',
      method: 'list',
      data: [payload],
      type: 'rpc',
      tid: Math.floor(Math.random() * 1000000),
      token: tokenGuess,
    };
    const resp = await fetch(rpcUrl, {
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
    const text = await resp.text();
    callback({ ok: resp.ok, status: resp.status, text });
  } catch (e) {
    clearTimeout(to);
    callback({ ok: false, error: String(e) });
  }
})();
"""


_DEBUG_DIR: Path | None = None


def fetch_procedure_page(
    driver,
    start: int,
    limit: int,
    date_from: str,
    query: str | None = None,
    sort: str = "id",
    direction: str = "DESC",
    with_affiliates: bool = True,
    tag_id: int | None = None,
) -> dict[str, Any]:
    """Вызов Procedure.list с заданными параметрами. Возвращает result из ответа."""

    # Набор параметров, который реально отправлял браузер при просмотре реестра
    # (точный снимок из network_log.json, полученного в разведке). Пагинация
    # в Ext.direct Store передаётся дополнительными полями start/page, но на
    # серверной стороне ЭТП ГПБ они не поддерживаются — реальная «пагинация»
    # достигается сдвигом `limit` и сортировкой id DESC. Поэтому «листание»
    # делаем через фильтр по id_max (см. fetch_all_procedures).
    payload: dict[str, Any] = {
        "sort": sort,
        "dir": direction,
        "with_affiliates": with_affiliates,
        "date_published_from": date_from,
        "query": query,
        "tag_id": tag_id,
        "limit": limit,
        "start": start,
    }

    result = driver.execute_async_script(
        _FETCH_PROC_LIST_JS, payload, RPC_URL, _get_remembered_token()
    )

    if not result or not result.get("ok"):
        raise RuntimeError(
            f"fetch Procedure.list start={start} не удался: {result!r}"
        )

    text = result.get("text", "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        if _DEBUG_DIR is not None:
            (_DEBUG_DIR / "debug_last_response.txt").write_text(
                text, encoding="utf-8"
            )
        raise RuntimeError(
            f"Невалидный JSON от Procedure.list (start={start}): {e}\n"
            f"Первые 500 символов ответа: {text[:500]}"
        ) from e

    if _DEBUG_DIR is not None and start == 0:
        (_DEBUG_DIR / "debug_first_response.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2)[:20000],
            encoding="utf-8",
        )

    if data.get("type") == "exception":
        raise RuntimeError(
            f"Ext.Direct exception (start={start}): "
            f"{data.get('message') or data!r}"
        )
    return data.get("result") or {}


def fetch_all_procedures(
    driver,
    date_from: str,
    batch: int = DEFAULT_BATCH,
    hard_limit: int | None = None,
    query: str | None = None,
    esia_timeout: int = 600,
) -> list[dict[str, Any]]:
    """Пагинирует Procedure.list до исчерпания totalCount.

    Если на какой-то итерации сервер отвечает `no_session`/`no_access` —
    снова просим пользователя авторизоваться в Chrome и продолжаем.
    """
    all_procs: list[dict[str, Any]] = []
    start = 0
    total: int | None = None

    while True:
        limit = batch
        if hard_limit is not None:
            remaining = hard_limit - len(all_procs)
            if remaining <= 0:
                break
            limit = min(batch, remaining)

        t0 = time.time()
        result = fetch_procedure_page(
            driver, start=start, limit=limit, date_from=date_from, query=query
        )
        took = time.time() - t0

        if _is_no_session_result(result):
            log(
                "Сервер отверг запрос (no_session/no_access). "
                "Прошу пользователя снова авторизоваться…",
                level="WARN",
            )
            ensure_authenticated(driver, esia_timeout=esia_timeout)
            continue  # повторяем тот же start/limit

        procs = result.get("procedures") or []
        total = result.get("totalCount") if total is None else total
        all_procs.extend(procs)

        log(
            f"Пачка start={start} limit={limit}: получено {len(procs)} "
            f"(накоплено {len(all_procs)}"
            + (f"/{total}" if total is not None else "")
            + f", {took:.1f}с)",
            level="OK",
        )

        if not procs:
            break
        if total is not None and len(all_procs) >= total:
            break
        if hard_limit is not None and len(all_procs) >= hard_limit:
            break

        start += len(procs)

    if total is not None:
        log(
            f"Итого собрано {len(all_procs)} процедур из {total} "
            f"(по фильтру date_published_from={date_from}).",
            level="OK",
        )
    return all_procs


# ----------------------------------------------------------------------------
# Сохранение результатов
# ----------------------------------------------------------------------------

# Главные поля для CSV/XLSX + вывода в консоль. Остальные 100+ полей
# тоже сохраняются — в CSV и XLSX они идут дополнительными столбцами,
# в JSON сохраняется всё целиком.
KEY_FIELDS = [
    "id",
    "procedure_number",
    "procedure_number2",
    "registry_number",
    "title",
    "trend_pur",
    "procedure_type",
    "stage",
    "step_id",
    "short_name",
    "full_name",
    "org_inn",
    "org_kpp",
    "org_ogrn",
    "contact_person",
    "total_price",
    "total_price_no_nds",
    "currency_name",
    "date_published",
    "date_end_registration",
    "date_end_second_parts_review",
    "date_last_update",
    "sanction_access",
    "under_sanctions",
    "oos_publish_status",
    "lot_id",
    "application_stages",
    "we_are_parent",
    "is_from_eis",
]


def _flatten_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    # dict/list — сериализуем JSON-ом, чтобы CSV/XLSX не ломались
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def save_json(procs: list[dict[str, Any]], path: Path, meta: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            {"meta": meta, "procedures": procs},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"JSON сохранён: {path} ({path.stat().st_size // 1024} КБ)", level="OK")


def _all_columns(procs: list[dict[str, Any]]) -> list[str]:
    extra = set()
    for p in procs:
        extra.update(p.keys())
    for k in KEY_FIELDS:
        extra.discard(k)
    return KEY_FIELDS + sorted(extra)


def save_csv(procs: list[dict[str, Any]], path: Path) -> None:
    columns = _all_columns(procs)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(columns)
        for p in procs:
            writer.writerow([_flatten_value(p.get(k)) for k in columns])
    log(f"CSV сохранён: {path} ({path.stat().st_size // 1024} КБ)", level="OK")


def save_xlsx(procs: list[dict[str, Any]], path: Path) -> None:
    try:
        from openpyxl import Workbook  # type: ignore
    except ImportError:
        log("openpyxl не установлен — XLSX пропускаю.", level="WARN")
        return

    columns = _all_columns(procs)
    wb = Workbook()
    ws = wb.active
    ws.title = "procedures"
    ws.append(columns)
    for p in procs:
        ws.append([_flatten_value(p.get(k)) for k in columns])

    for i, c in enumerate(columns[:30], start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(
            40, max(10, len(c) + 2)
        )
    ws.freeze_panes = "A2"

    wb.save(path)
    log(f"XLSX сохранён: {path} ({path.stat().st_size // 1024} КБ)", level="OK")


# ----------------------------------------------------------------------------
# Консольная сводка
# ----------------------------------------------------------------------------

# Справочник кодов trend_pur, встречающихся на ЭТП ГПБ (базовые значения).
TREND_PUR_NAMES = {
    "001": "Открытый конкурс",
    "002": "Открытый аукцион / редукцион",
    "003": "Запрос предложений",
    "004": "Запрос котировок",
    "005": "Закупка у единственного поставщика",
    "006": "Конкурс с ограниченным участием",
    "007": "Двухэтапный конкурс",
    "008": "Предквалификационный отбор",
}


def _parse_date(s: Any) -> datetime | None:
    if not isinstance(s, str):
        return None
    s = s.split("+")[0].replace("T", " ").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_price(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def print_summary(procs: list[dict[str, Any]]) -> None:
    if not procs:
        log("Ничего не собрано — сводка пуста.", level="WARN")
        return

    print()
    print("=" * 72)
    print(f"СВОДКА: собрано {len(procs)} актуальных процедур")
    print("=" * 72)

    # 1. Распределение по trend_pur
    by_trend = Counter(p.get("trend_pur") or "—" for p in procs)
    print("\nПо типу закупки (trend_pur):")
    for code, cnt in by_trend.most_common():
        name = TREND_PUR_NAMES.get(code, "код без описания")
        print(f"  {code:<5} {cnt:>5}  {name}")

    # 2. Топ-10 по сумме
    with_price = [
        (p, _parse_price(p.get("total_price"))) for p in procs
    ]
    with_price = [(p, v) for p, v in with_price if v]
    with_price.sort(key=lambda t: t[1], reverse=True)

    print("\nТоп-10 по начальной цене:")
    for p, price in with_price[:10]:
        title = (p.get("title") or "").replace("\n", " ")[:70]
        cust = p.get("short_name") or ""
        print(
            f"  id={p.get('id'):<7} {price:>15,.2f} RUB  "
            f"{cust[:30]:<30}  {title}"
        )

    # 3. Топ-10 по дате публикации (свежие)
    dated = [(p, _parse_date(p.get("date_published"))) for p in procs]
    dated = [(p, d) for p, d in dated if d]
    dated.sort(key=lambda t: t[1], reverse=True)

    print("\nСвежие 10 по дате публикации:")
    for p, d in dated[:10]:
        title = (p.get("title") or "").replace("\n", " ")[:70]
        cust = p.get("short_name") or ""
        print(
            f"  id={p.get('id'):<7} {d.strftime('%Y-%m-%d %H:%M')}  "
            f"{cust[:30]:<30}  {title}"
        )

    # 4. Общие суммы
    total_sum = sum(v for _, v in with_price)
    print(f"\nСуммарная НМЦ по известным {len(with_price)} "
          f"процедурам: {total_sum:,.2f} RUB")

    print("=" * 72)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="parse_procedures.py",
        description=(
            "Парсер актуальных процедур ЭТП Газпромбанка "
            "(https://etpgaz.gazprombank.ru). "
            "Работает через Chrome с включённым DevTools Remote Debugging. "
            "Авторизацию (ЕСИА + ЭЦП, выбор сертификата и контрагента) "
            "делает ПОЛЬЗОВАТЕЛЬ вручную в открытом Chrome. Парсер сам только "
            "проверяет сессию и качает данные через Ext.Direct RPC."
        ),
    )
    p.add_argument(
        "--chrome-port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Порт Chrome DevTools Protocol (default {DEFAULT_PORT}).",
    )
    p.add_argument(
        "--profile",
        default="Default",
        help="Имя профиля Chrome (папка User Data/<profile>, default 'Default').",
    )
    p.add_argument(
        "--kill-chrome",
        action="store_true",
        help="Перед запуском убить все процессы chrome.exe/chromedriver.exe.",
    )
    p.add_argument(
        "--since",
        default=None,
        help=(
            "Дата начала периода публикации (формат ДД.ММ.ГГГГ или ГГГГ-ММ-ДД). "
            "Default — 365 дней назад."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Жёсткий верхний лимит количества собираемых процедур.",
    )
    p.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"Размер одной пачки Procedure.list (default {DEFAULT_BATCH}).",
    )
    p.add_argument(
        "--query",
        default=None,
        help="Поисковая строка (поле query в Procedure.list). Default — нет фильтра.",
    )
    p.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Папка вывода (default '{DEFAULT_OUTPUT}').",
    )
    p.add_argument(
        "--esia-timeout",
        type=int,
        default=600,
        help=(
            "Сколько секунд ждать ручной авторизации в Chrome, "
            "если сессия не активна (default 600)."
        ),
    )
    return p.parse_args(argv)


def _normalize_since(since: str | None) -> str:
    """Возвращает дату в формате ДД.ММ.ГГГГ (как ждёт сервер)."""
    if not since:
        d = datetime.now() - timedelta(days=365)
        return d.strftime("%d.%m.%Y")
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(since, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise SystemExit(
        f"Не смог разобрать --since={since}. Ожидаются форматы ДД.ММ.ГГГГ или ГГГГ-ММ-ДД."
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    date_from = _normalize_since(args.since)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    global _DEBUG_DIR
    _DEBUG_DIR = output_dir

    if args.kill_chrome:
        log("Убиваю процессы Chrome/chromedriver…")
        killed = _kill_chrome_processes()
        log(f"Убито: {killed}", level="OK")
        time.sleep(1.5)

    log("Подключаюсь к Chrome…", level="STEP")
    driver = connect_or_launch_chrome(args.chrome_port, profile=args.profile)

    try:
        log("Проверяю сессию…", level="STEP")
        ensure_authenticated(driver, esia_timeout=args.esia_timeout)

        log(
            f"Запрашиваю реестр процедур (date_published_from={date_from}, "
            f"batch={args.batch}"
            + (f", hard_limit={args.limit}" if args.limit else "")
            + ")…",
            level="STEP",
        )
        procs = fetch_all_procedures(
            driver,
            date_from=date_from,
            batch=args.batch,
            hard_limit=args.limit,
            query=args.query,
            esia_timeout=args.esia_timeout,
        )

        meta = {
            "source_url": TARGET_URL,
            "rpc_endpoint": RPC_URL,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "date_published_from": date_from,
            "batch_size": args.batch,
            "hard_limit": args.limit,
            "query": args.query,
            "collected_count": len(procs),
        }

        log("Сохраняю результаты…", level="STEP")
        save_json(procs, output_dir / "procedures.json", meta)
        save_csv(procs, output_dir / "procedures.csv")
        save_xlsx(procs, output_dir / "procedures.xlsx")

        print_summary(procs)

        return 0
    finally:
        # Chrome оставляем открытым — пользователь просил не выходить с профиля.
        # Selenium-сессию просто отвязываем.
        try:
            driver.command_executor.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
