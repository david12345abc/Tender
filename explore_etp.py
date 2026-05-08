"""
Разведочный скрипт для ЭТП Газпромбанка (etpgaz.gazprombank.ru).

Назначение:
  1. Проверяет окружение:
       - КриптоПро CSP
       - КриптоПро ЭЦП Browser plug-in ("Госплагин" / CAdES)
       - Личные сертификаты (в том числе на токенах Рутокен/JaCarta)
       - Google Chrome и расширение "CryptoPro Extension for CAdES Browser"
       - Не запущен ли Chrome (основной профиль должен быть свободен)
  2. Запускает Chrome через Selenium с ВАШИМ основным профилем
     (где уже установлены плагин и расширение CAdES) и собирает данные о сайте:
       - все сетевые запросы (CDP Network.*)
       - HTML и скриншоты до/после Ваших действий
       - cookies
       - кнопки и ссылки (чтобы найти "Вход по ЭЦП")
       - верхнеуровневая структура DOM
  3. Сохраняет всё в папку exploration_results/ + формирует report.md.

Запуск:
    pip install -r requirements.txt
    # Перед запуском обязательно закройте ВСЕ окна Chrome.
    python explore_etp.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

TARGET_URL = "https://etpgaz.gazprombank.ru/#com/procedure/index"

CRYPTOPRO_EXTENSION_IDS = [
    "iifchhfnnmpdbibifmljnfjhpififfog",
    "epebfcehmdedogndhlcacafjaacknbcm",
]

SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "exploration_results"


def log(msg: str, level: str = "INFO") -> None:
    prefix = {
        "INFO": "[i]",
        "OK": "[+]",
        "WARN": "[!]",
        "ERR": "[x]",
        "STEP": "==>",
    }.get(level, "[i]")
    print(f"{prefix} {msg}", flush=True)


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "fail" | "skip"
    details: dict = field(default_factory=dict)
    message: str = ""


def _read_registry_value(hive, subkey: str, value_name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except OSError:
        return None


def _registry_key_exists(hive, subkey: str) -> bool:
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(hive, subkey):
            return True
    except OSError:
        return False


def _list_registry_subkeys(hive, subkey: str) -> list[str]:
    try:
        import winreg
    except ImportError:
        return []
    names: list[str] = []
    try:
        with winreg.OpenKey(hive, subkey) as key:
            i = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        pass
    return names


def check_cryptopro_csp() -> CheckResult:
    if sys.platform != "win32":
        return CheckResult("КриптоПро CSP", "skip", message="Не Windows — пропущено")

    try:
        import winreg
    except ImportError:
        return CheckResult(
            "КриптоПро CSP",
            "fail",
            message="Нет модуля winreg — неподдерживаемая платформа",
        )

    candidate_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Crypto Pro\Settings"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Crypto Pro\Settings"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Crypto Pro\CSP"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Crypto Pro\CSP"),
    ]

    install_paths_candidates = [
        r"C:\Program Files\Crypto Pro\CSP",
        r"C:\Program Files (x86)\Crypto Pro\CSP",
    ]

    found_key = None
    for hive, subkey in candidate_keys:
        if _registry_key_exists(hive, subkey):
            found_key = subkey
            break

    install_path = None
    for path in install_paths_candidates:
        if Path(path).exists():
            install_path = path
            break

    version = None
    for hive, subkey in candidate_keys:
        v = _read_registry_value(hive, subkey, "Version") or _read_registry_value(
            hive, subkey, "ProductVersion"
        )
        if v:
            version = v
            break

    if not found_key and not install_path:
        return CheckResult(
            "КриптоПро CSP",
            "fail",
            message="КриптоПро CSP не обнаружен. Скачайте на https://cryptopro.ru/products/csp",
        )

    csptest_output = None
    csptest_path = None
    if install_path:
        for name in ("csptest.exe", "cpverify.exe"):
            candidate = Path(install_path) / name
            if candidate.exists():
                csptest_path = str(candidate)
                break

    if csptest_path:
        try:
            proc = subprocess.run(
                [csptest_path, "-keyset", "-enum_cont", "-fqcn", "-verifycontext"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="cp866",
                errors="replace",
            )
            csptest_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            csptest_output = csptest_output.strip()[:4000]
        except Exception as e:
            csptest_output = f"<не удалось выполнить csptest: {e}>"

    return CheckResult(
        "КриптоПро CSP",
        "ok",
        details={
            "registry_key": found_key,
            "install_path": install_path,
            "version": version,
            "csptest_path": csptest_path,
            "csptest_enum_containers": csptest_output,
        },
        message=f"Установлен (версия: {version or 'неизвестна'}, путь: {install_path})",
    )


def check_cades_plugin() -> CheckResult:
    if sys.platform != "win32":
        return CheckResult("КриптоПро CAdES Plug-in", "skip", message="Не Windows — пропущено")

    try:
        import winreg
    except ImportError:
        return CheckResult(
            "КриптоПро CAdES Plug-in", "fail", message="Нет модуля winreg"
        )

    candidate_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Crypto Pro\CAdES"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Crypto Pro\CAdES"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Crypto Pro\CAdES Browser Plug-in"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Crypto Pro\CAdES Browser Plug-in",
        ),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Crypto Pro\CAdES"),
    ]

    candidate_paths = [
        r"C:\Program Files\Crypto Pro\CAdES Browser Plug-in",
        r"C:\Program Files (x86)\Crypto Pro\CAdES Browser Plug-in",
        r"C:\Program Files\Crypto Pro\CAdES",
        r"C:\Program Files (x86)\Crypto Pro\CAdES",
    ]

    found_key = None
    for hive, subkey in candidate_keys:
        if _registry_key_exists(hive, subkey):
            found_key = subkey
            break

    install_path = None
    for path in candidate_paths:
        if Path(path).exists():
            install_path = path
            break

    dll_found: list[str] = []
    if install_path:
        for dll in ("nmcades.dll", "npcades.dll", "CAdESCOM.dll"):
            for p in Path(install_path).rglob(dll):
                dll_found.append(str(p))

    version = None
    if found_key:
        for hive, subkey in candidate_keys:
            v = _read_registry_value(hive, subkey, "Version") or _read_registry_value(
                hive, subkey, "DisplayVersion"
            )
            if v:
                version = v
                break

    com_available = False
    com_error: str | None = None
    try:
        import win32com.client

        obj = win32com.client.Dispatch("CAdESCOM.About")
        com_available = True
        version = version or getattr(obj, "Version", None)
    except Exception as e:
        com_error = str(e)

    if not found_key and not install_path and not com_available:
        return CheckResult(
            "КриптоПро CAdES Plug-in",
            "fail",
            message=(
                "КриптоПро ЭЦП Browser plug-in не найден. "
                "Скачайте на https://www.cryptopro.ru/products/cades/plugin"
            ),
            details={"com_error": com_error},
        )

    status = "ok" if com_available else "warn"
    return CheckResult(
        "КриптоПро CAdES Plug-in",
        status,
        details={
            "registry_key": found_key,
            "install_path": install_path,
            "dlls": dll_found,
            "version": version,
            "com_available": com_available,
            "com_error": com_error,
        },
        message=(
            f"Установлен (версия: {version or 'неизвестна'}, COM: "
            f"{'доступен' if com_available else f'недоступен — {com_error}'})"
        ),
    )


def check_certificates() -> CheckResult:
    if sys.platform != "win32":
        return CheckResult("Сертификаты (хранилище MY)", "skip", message="Не Windows")

    try:
        import win32com.client  # type: ignore
    except ImportError as e:
        return CheckResult(
            "Сертификаты (хранилище MY)",
            "fail",
            message=f"pywin32 не установлен: {e}. pip install pywin32",
        )

    CAPICOM_CURRENT_USER_STORE = 2
    CAPICOM_MY_STORE = "My"
    CAPICOM_STORE_OPEN_READ_ONLY = 0
    CAPICOM_STORE_OPEN_EXISTING_ONLY = 128
    open_mode = CAPICOM_STORE_OPEN_READ_ONLY | CAPICOM_STORE_OPEN_EXISTING_ONLY

    try:
        store = win32com.client.Dispatch("CAdESCOM.Store")
        store.Open(CAPICOM_CURRENT_USER_STORE, CAPICOM_MY_STORE, open_mode)
    except Exception as e:
        return CheckResult(
            "Сертификаты (хранилище MY)",
            "fail",
            message=(
                "Не удалось открыть хранилище 'Мои' через CAdESCOM. "
                "Убедитесь, что КриптоПро CAdES plug-in установлен."
            ),
            details={"error": repr(e)},
        )

    certs_info: list[dict[str, Any]] = []
    try:
        certs = store.Certificates
        count = certs.Count
        for i in range(1, count + 1):
            cert = certs.Item(i)
            has_pk = False
            try:
                has_pk = bool(cert.HasPrivateKey())
            except Exception:
                pass

            valid_from: Any = None
            valid_to: Any = None
            try:
                valid_from = str(cert.ValidFromDate)
                valid_to = str(cert.ValidToDate)
            except Exception:
                pass

            now = datetime.now()
            is_expired: bool | None = None
            try:
                is_expired = cert.IsValid().Result is False
            except Exception:
                try:
                    is_expired = (
                        datetime.strptime(valid_to, "%m/%d/%y %H:%M:%S") < now
                        if valid_to
                        else None
                    )
                except Exception:
                    is_expired = None

            certs_info.append(
                {
                    "index": i,
                    "subject": getattr(cert, "SubjectName", None),
                    "issuer": getattr(cert, "IssuerName", None),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "serial_number": getattr(cert, "SerialNumber", None),
                    "thumbprint": getattr(cert, "Thumbprint", None),
                    "version": getattr(cert, "Version", None),
                    "has_private_key": has_pk,
                    "is_expired": is_expired,
                }
            )
    except Exception as e:
        return CheckResult(
            "Сертификаты (хранилище MY)",
            "fail",
            message=f"Ошибка перечисления сертификатов: {e}",
        )
    finally:
        try:
            store.Close()
        except Exception:
            pass

    usable = [c for c in certs_info if c.get("has_private_key") and not c.get("is_expired")]
    status = "ok" if usable else ("warn" if certs_info else "fail")

    return CheckResult(
        "Сертификаты (хранилище MY)",
        status,
        details={"count": len(certs_info), "certificates": certs_info},
        message=(
            f"Найдено сертификатов: {len(certs_info)} (с закрытым ключом и действующих: {len(usable)})"
        ),
    )


def _chrome_user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA", "")
    return Path(local) / "Google" / "Chrome" / "User Data"


def _chrome_executable_paths() -> list[str]:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
        ),
    ]
    return [c for c in candidates if Path(c).exists()]


def check_chrome_extension() -> CheckResult:
    user_data = _chrome_user_data_dir()
    if not user_data.exists():
        return CheckResult(
            "Chrome + расширение CAdES",
            "fail",
            message=f"Не найдена папка профиля Chrome: {user_data}",
        )

    chrome_paths = _chrome_executable_paths()
    if not chrome_paths:
        return CheckResult(
            "Chrome + расширение CAdES",
            "fail",
            message="Chrome не установлен (не найден chrome.exe)",
        )

    profiles: list[dict[str, Any]] = []
    for profile_dir in user_data.iterdir():
        if not profile_dir.is_dir():
            continue
        ext_dir = profile_dir / "Extensions"
        if not ext_dir.exists():
            continue

        found_extensions: list[dict[str, Any]] = []
        for ext_id in CRYPTOPRO_EXTENSION_IDS:
            ext_path = ext_dir / ext_id
            if ext_path.exists():
                versions = [v for v in ext_path.iterdir() if v.is_dir()]
                ver_info: list[dict[str, Any]] = []
                for v in versions:
                    manifest = v / "manifest.json"
                    ver = {"path": str(v), "version_folder": v.name}
                    if manifest.exists():
                        try:
                            m = json.loads(manifest.read_text(encoding="utf-8"))
                            ver["name"] = m.get("name")
                            ver["version"] = m.get("version")
                            ver["description"] = m.get("description")
                        except Exception as e:
                            ver["manifest_error"] = str(e)
                    ver_info.append(ver)
                found_extensions.append({"id": ext_id, "versions": ver_info})

        profiles.append(
            {
                "profile": profile_dir.name,
                "extensions_cryptopro": found_extensions,
            }
        )

    default_profile = next((p for p in profiles if p["profile"] == "Default"), None)
    has_cades_in_default = bool(
        default_profile and default_profile["extensions_cryptopro"]
    )
    has_cades_anywhere = any(p["extensions_cryptopro"] for p in profiles)

    if has_cades_in_default:
        status = "ok"
        message = "Расширение CryptoPro CAdES найдено в профиле Default"
    elif has_cades_anywhere:
        status = "warn"
        message = (
            "Расширение CryptoPro CAdES найдено, но НЕ в профиле Default "
            "(скрипт будет использовать Default)"
        )
    else:
        status = "fail"
        message = (
            "Расширение CryptoPro Extension for CAdES Browser не найдено ни в одном профиле Chrome. "
            "Установите из Chrome Web Store."
        )

    return CheckResult(
        "Chrome + расширение CAdES",
        status,
        details={
            "user_data_dir": str(user_data),
            "chrome_executables": chrome_paths,
            "profiles": profiles,
        },
        message=message,
    )


def check_chrome_running() -> CheckResult:
    try:
        import psutil
    except ImportError:
        return CheckResult(
            "Процессы Chrome",
            "warn",
            message="psutil не установлен — пропуск проверки. pip install psutil",
        )

    chrome_procs = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name == "chrome.exe":
                chrome_procs.append(
                    {"pid": p.info["pid"], "exe": p.info.get("exe")}
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if chrome_procs:
        return CheckResult(
            "Процессы Chrome",
            "warn",
            details={"count": len(chrome_procs), "processes": chrome_procs[:10]},
            message=(
                f"Chrome уже запущен ({len(chrome_procs)} процессов). "
                "Закройте ВСЕ окна Chrome перед продолжением, иначе Selenium не сможет подключиться к профилю."
            ),
        )

    return CheckResult(
        "Процессы Chrome",
        "ok",
        message="Chrome не запущен — можно продолжать",
    )


def run_all_checks() -> list[CheckResult]:
    log("Проверка окружения", level="STEP")
    results: list[CheckResult] = []
    for fn in (
        check_cryptopro_csp,
        check_cades_plugin,
        check_certificates,
        check_chrome_extension,
        check_chrome_running,
    ):
        try:
            res = fn()
        except Exception as e:
            res = CheckResult(
                name=fn.__name__,
                status="fail",
                message=f"Исключение: {e}",
                details={"traceback": traceback.format_exc()},
            )
        icon = {"ok": "OK", "warn": "WARN", "fail": "ERR", "skip": "INFO"}.get(
            res.status, "INFO"
        )
        log(f"{res.name}: {res.status.upper()} — {res.message}", level=icon)
        results.append(res)
    return results


def _save_state(driver, results_dir: Path, suffix: str) -> dict[str, Any]:
    from selenium.webdriver.common.by import By

    state: dict[str, Any] = {
        "suffix": suffix,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    try:
        state["current_url"] = driver.current_url
    except Exception as e:
        state["current_url_error"] = str(e)
    try:
        state["title"] = driver.title
    except Exception as e:
        state["title_error"] = str(e)

    try:
        driver.save_screenshot(str(results_dir / f"screenshot_{suffix}.png"))
    except Exception as e:
        state["screenshot_error"] = str(e)

    try:
        (results_dir / f"page_source_{suffix}.html").write_text(
            driver.page_source or "", encoding="utf-8"
        )
    except Exception as e:
        state["page_source_error"] = str(e)

    try:
        cookies = driver.get_cookies()
        (results_dir / f"cookies_{suffix}.json").write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        state["cookies_count"] = len(cookies)
    except Exception as e:
        state["cookies_error"] = str(e)

    try:
        buttons = []
        for btn in driver.find_elements(By.TAG_NAME, "button"):
            try:
                buttons.append(
                    {
                        "text": (btn.text or "").strip()[:200],
                        "id": btn.get_attribute("id"),
                        "class": btn.get_attribute("class"),
                        "type": btn.get_attribute("type"),
                        "name": btn.get_attribute("name"),
                        "title": btn.get_attribute("title"),
                        "aria_label": btn.get_attribute("aria-label"),
                        "disabled": btn.get_attribute("disabled"),
                        "displayed": btn.is_displayed(),
                    }
                )
            except Exception:
                continue

        links = []
        for link in driver.find_elements(By.TAG_NAME, "a"):
            try:
                links.append(
                    {
                        "text": (link.text or "").strip()[:200],
                        "href": link.get_attribute("href"),
                        "id": link.get_attribute("id"),
                        "class": link.get_attribute("class"),
                        "title": link.get_attribute("title"),
                    }
                )
            except Exception:
                continue

        inputs = []
        for inp in driver.find_elements(By.TAG_NAME, "input"):
            try:
                inputs.append(
                    {
                        "type": inp.get_attribute("type"),
                        "name": inp.get_attribute("name"),
                        "id": inp.get_attribute("id"),
                        "placeholder": inp.get_attribute("placeholder"),
                        "value_preview": (inp.get_attribute("value") or "")[:80],
                    }
                )
            except Exception:
                continue

        (results_dir / f"buttons_and_links_{suffix}.json").write_text(
            json.dumps(
                {"buttons": buttons, "links": links, "inputs": inputs},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        state["buttons_count"] = len(buttons)
        state["links_count"] = len(links)
        state["inputs_count"] = len(inputs)
    except Exception as e:
        state["buttons_links_error"] = str(e)

    dom_script = r"""
    function summarize(el, depth) {
        if (!el || depth > 4) return null;
        var children = [];
        for (var i = 0; i < el.children.length && i < 30; i++) {
            var sub = summarize(el.children[i], depth + 1);
            if (sub) children.push(sub);
        }
        var text = el.innerText ? el.innerText.trim().slice(0, 200) : '';
        return {
            tag: el.tagName ? el.tagName.toLowerCase() : null,
            id: el.id || null,
            classes: el.className && typeof el.className === 'string' ? el.className : null,
            role: el.getAttribute ? el.getAttribute('role') : null,
            textSample: text,
            childCount: el.children.length,
            children: children
        };
    }
    return summarize(document.body, 0);
    """
    try:
        dom = driver.execute_script(dom_script)
        (results_dir / f"dom_structure_{suffix}.json").write_text(
            json.dumps(dom, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        state["dom_error"] = str(e)

    try:
        storages_script = r"""
        var out = {localStorage: {}, sessionStorage: {}};
        try {
            for (var i = 0; i < localStorage.length; i++) {
                var k = localStorage.key(i);
                var v = localStorage.getItem(k);
                out.localStorage[k] = (v && v.length > 2000) ? v.slice(0, 2000) + '...[truncated]' : v;
            }
        } catch(e) { out.localStorageError = String(e); }
        try {
            for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                var v = sessionStorage.getItem(k);
                out.sessionStorage[k] = (v && v.length > 2000) ? v.slice(0, 2000) + '...[truncated]' : v;
            }
        } catch(e) { out.sessionStorageError = String(e); }
        return out;
        """
        storages = driver.execute_script(storages_script)
        (results_dir / f"storages_{suffix}.json").write_text(
            json.dumps(storages, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        state["storages_error"] = str(e)

    return state


RPC_PROBE_CASES: list[dict[str, Any]] = [
    {"action": "Api", "method": "publicProcedures", "data": None},
    {"action": "Api", "method": "publicProcedures", "data": []},
    {"action": "Api", "method": "publicProcedures", "data": [{"page": 1, "start": 0, "limit": 500}]},
    {"action": "Api", "method": "procedures", "data": None},
    {"action": "Api", "method": "procedures", "data": [{"page": 1, "start": 0, "limit": 5}]},
    {"action": "Api", "method": "privateProcedures", "data": None},
    {"action": "Api", "method": "company", "data": None},
    {"action": "Api", "method": "customerslist", "data": None},
    {"action": "Api", "method": "supplierslist", "data": None},
    {"action": "Api", "method": "protocolslist", "data": None},
]


_RPC_JS = r"""
var done = arguments[arguments.length - 1];
var cases = arguments[0];
var url = '/index.php?rpctype=direct&module=default&client=etp';
var tid = 9000;
var results = [];
function next(i) {
    if (i >= cases.length) { done(results); return; }
    tid += 1;
    var c = cases[i];
    var body = {action: c.action, method: c.method, data: c.data, type: 'rpc', tid: tid, token: ''};
    var started = Date.now();
    fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
        body: JSON.stringify(body),
        credentials: 'same-origin'
    }).then(function (r) {
        return r.text().then(function (t) { return {status: r.status, text: t}; });
    }).then(function (r) {
        var parsed = null; var parseErr = null;
        try { parsed = JSON.parse(r.text); } catch (e) { parseErr = String(e); }
        results.push({
            case: c,
            status: r.status,
            elapsed_ms: Date.now() - started,
            parsed: parsed,
            parse_error: parseErr,
            text_preview: r.text.slice(0, 6000),
            text_length: r.text.length
        });
        next(i + 1);
    }).catch(function (e) {
        results.push({case: c, error: String(e)});
        next(i + 1);
    });
}
next(0);
"""


def _probe_rpc_api(driver, results_dir: Path) -> dict[str, Any]:
    try:
        driver.set_script_timeout(60)
        raw = driver.execute_async_script(_RPC_JS, RPC_PROBE_CASES)
    except Exception as e:
        return {"error": str(e)}

    successful = 0
    for r in raw:
        try:
            parsed = r.get("parsed")
            if isinstance(parsed, dict):
                res = parsed.get("result")
                if isinstance(res, dict) and res.get("success"):
                    successful += 1
        except Exception:
            continue

    try:
        (results_dir / "rpc_probe.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {"successful": successful, "total": len(raw), "file": "rpc_probe.json"}


def _collect_network_log(driver, results_dir: Path) -> dict[str, Any]:
    try:
        raw_logs = driver.get_log("performance")
    except Exception as e:
        return {"error": f"get_log('performance') failed: {e}"}

    events: list[dict[str, Any]] = []
    for entry in raw_logs:
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        if not msg.get("method", "").startswith("Network."):
            continue
        events.append(
            {
                "method": msg["method"],
                "timestamp": entry.get("timestamp"),
                "params": msg.get("params", {}),
            }
        )

    requests_by_id: dict[str, dict[str, Any]] = {}
    for ev in events:
        params = ev["params"]
        rid = params.get("requestId")
        if not rid:
            continue
        requests_by_id.setdefault(rid, {})[ev["method"]] = params

    summary: list[dict[str, Any]] = []
    for rid, by_method in requests_by_id.items():
        req_will = by_method.get("Network.requestWillBeSent", {})
        response = by_method.get("Network.responseReceived", {})
        request = req_will.get("request", {}) if req_will else {}
        resp = response.get("response", {}) if response else {}
        resource_type = response.get("type") or req_will.get("type") or ""

        body_snippet: str | None = None
        body_full_file: str | None = None
        if resource_type in ("XHR", "Fetch", "Document") and resp:
            try:
                body_result = driver.execute_cdp_cmd(
                    "Network.getResponseBody", {"requestId": rid}
                )
                body = body_result.get("body", "") or ""
                is_b64 = body_result.get("base64Encoded", False)
                if is_b64:
                    body_snippet = f"<base64 length={len(body)}>"
                else:
                    if len(body) > 3000:
                        body_snippet = body[:3000] + "...[truncated]"
                        safe_name = f"body_{rid}.txt"
                        try:
                            (results_dir / "bodies").mkdir(exist_ok=True)
                            (results_dir / "bodies" / safe_name).write_text(
                                body, encoding="utf-8", errors="replace"
                            )
                            body_full_file = f"bodies/{safe_name}"
                        except Exception:
                            pass
                    else:
                        body_snippet = body
            except Exception as e:
                body_snippet = f"<body unavailable: {e}>"

        summary.append(
            {
                "request_id": rid,
                "url": request.get("url"),
                "method": request.get("method"),
                "resource_type": resource_type,
                "status": resp.get("status"),
                "status_text": resp.get("statusText"),
                "mime_type": resp.get("mimeType"),
                "request_headers": request.get("headers", {}),
                "response_headers": resp.get("headers", {}),
                "post_data": request.get("postData"),
                "response_body_snippet": body_snippet,
                "response_body_file": body_full_file,
                "initiator": req_will.get("initiator"),
            }
        )

    summary.sort(key=lambda x: (x.get("url") or ""))

    try:
        (results_dir / "network_log.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as e:
        return {"error": f"не удалось сохранить network_log.json: {e}", "count": len(summary)}

    api_like = [
        e
        for e in summary
        if e.get("resource_type") in ("XHR", "Fetch")
        and "etpgaz.gazprombank.ru" in (e.get("url") or "")
    ]
    try:
        (results_dir / "api_candidates.json").write_text(
            json.dumps(api_like, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass

    return {
        "total_requests": len(summary),
        "api_candidates": len(api_like),
        "file": "network_log.json",
    }


def _cleanup_profile_singletons(user_data_dir: Path) -> list[str]:
    removed: list[str] = []
    for root in (user_data_dir, user_data_dir / "Default"):
        if not root.exists():
            continue
        for name in ("Singleton", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            p = root / name
            if p.exists():
                try:
                    p.unlink()
                    removed.append(str(p))
                except Exception:
                    pass
    return removed


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


def _is_port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except Exception:
        return False


def _launch_chrome_with_debug(
    user_data_dir: Path,
    profile_name: str,
    port: int,
    start_url: str,
) -> subprocess.Popen | None:
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
    log(f"Запуск Chrome: {chrome}", level="INFO")
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        )
        kwargs["close_fds"] = True
    proc = subprocess.Popen(args, **kwargs)
    return proc


def _wait_for_devtools(port: int, timeout: int = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if _is_port_open(port):
            return True
        time.sleep(0.5)
    return False


def setup_driver(
    fresh_profile: bool = False,
    remote_debug: bool = False,
    remote_port: int = 9222,
    start_url: str | None = None,
):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    if fresh_profile:
        import tempfile

        user_data_dir = Path(tempfile.gettempdir()) / f"chrome_etp_explore_profile_{int(time.time())}"
        user_data_dir.mkdir(exist_ok=True)
        profile_name = "Default"
        log(f"Используется временный профиль: {user_data_dir}", level="INFO")
    else:
        user_data_dir = _chrome_user_data_dir()
        profile_name = "Default"
        removed = _cleanup_profile_singletons(user_data_dir)
        if removed:
            log(f"Удалены lock-файлы: {len(removed)}", level="INFO")

    options = Options()
    if remote_debug:
        if _is_port_open(remote_port):
            log(
                f"Chrome с DevTools уже слушает порт {remote_port} — подключаюсь к нему.",
                level="INFO",
            )
        else:
            _launch_chrome_with_debug(
                user_data_dir, profile_name, remote_port, start_url or "about:blank"
            )
            log(f"Жду DevTools на порту {remote_port}...", level="INFO")
            if not _wait_for_devtools(remote_port, timeout=60):
                raise RuntimeError(
                    f"Chrome не поднял DevTools на порту {remote_port} за 60 сек. "
                    f"Попробуйте запустить Chrome вручную:\n"
                    f'  "{_find_chrome_exe()}" '
                    f"--remote-debugging-port={remote_port} "
                    f'--user-data-dir="{user_data_dir}" '
                    f"--profile-directory={profile_name}"
                )
        log("DevTools доступен. Подключаю Selenium.", level="OK")
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{remote_port}")
        options.set_capability(
            "goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"}
        )
        return webdriver.Chrome(options=options)

    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument(f"--profile-directory={profile_name}")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.set_capability(
        "goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"}
    )

    try:
        driver = webdriver.Chrome(options=options)
        return driver
    except Exception as e1:
        log(
            f"Selenium Manager не смог запустить Chrome ({e1}). "
            "Пробую webdriver-manager...",
            level="WARN",
        )
        try:
            from selenium.webdriver.chrome.service import Service  # type: ignore
            from webdriver_manager.chrome import ChromeDriverManager  # type: ignore

            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)
        except Exception as e2:
            raise RuntimeError(
                f"Не удалось запустить Chrome через Selenium.\n"
                f"  1) Selenium Manager: {e1}\n"
                f"  2) webdriver-manager: {e2}\n\n"
                "Убедитесь, что Chrome ЗАКРЫТ и что установлены корректные версии библиотек."
            ) from e2


def explore_site(
    results_dir: Path,
    auto_seconds: int | None = None,
    fresh_profile: bool = False,
    remote_debug: bool = False,
    remote_port: int = 9222,
) -> dict[str, Any]:
    log("Разведка сайта", level="STEP")
    summary: dict[str, Any] = {
        "target_url": TARGET_URL,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "mode": "auto" if auto_seconds is not None else "interactive",
        "fresh_profile": fresh_profile,
        "remote_debug": remote_debug,
        "steps": [],
    }

    try:
        driver = setup_driver(
            fresh_profile=fresh_profile,
            remote_debug=remote_debug,
            remote_port=remote_port,
            start_url=TARGET_URL if remote_debug else None,
        )
    except Exception as e:
        log(str(e), level="ERR")
        summary["driver_error"] = str(e)
        return summary

    try:
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Page.enable", {})
        except Exception as e:
            log(f"Не удалось включить CDP Network/Page: {e}", level="WARN")

        log(f"Открываю {TARGET_URL}", level="INFO")
        driver.get(TARGET_URL)

        log("Жду 20 секунд первичной загрузки SPA...", level="INFO")
        time.sleep(20)

        state_initial = _save_state(driver, results_dir, suffix="initial")
        summary["steps"].append({"stage": "initial", "state": state_initial})
        log("Слепок состояния 'initial' сохранён", level="OK")

        if auto_seconds is not None:
            done_signal = results_dir / "DONE.txt"
            if done_signal.exists():
                try:
                    done_signal.unlink()
                except Exception:
                    pass
            log(
                f"Автоматический режим: жду максимум {auto_seconds} сек "
                f"ИЛИ создания файла {done_signal} для досрочного завершения.",
                level="INFO",
            )
            slept = 0
            step = 5
            while slept < auto_seconds:
                time.sleep(step)
                slept += step
                if done_signal.exists():
                    log(
                        f"Найден {done_signal.name} — завершаю разведку досрочно "
                        f"(прошло {slept} сек).",
                        level="OK",
                    )
                    try:
                        done_signal.unlink()
                    except Exception:
                        pass
                    break
        else:
            print()
            print("=" * 70)
            print("СЕЙЧАС ОКНО CHROME ОТКРЫТО. Выполните в нём любые действия, которые")
            print("нужны для разведки:")
            print("  1) Оглядите страницу 'Актуальные процедуры', прокрутите вниз/вверх.")
            print("  2) Если есть кнопка 'Вход по ЭЦП' — нажмите и попробуйте войти.")
            print("     (Откроется окно выбора сертификата КриптоПро — выберите свой.)")
            print("  3) Перейдите на вторую страницу реестра процедур, смените фильтры.")
            print("  4) Откройте карточку любой процедуры, если интересно.")
            print()
            print("Когда закончите — вернитесь сюда и нажмите Enter.")
            print("=" * 70)
            try:
                input()
            except EOFError:
                log("stdin недоступен, жду ещё 60 секунд...", level="WARN")
                time.sleep(60)

        state_final = _save_state(driver, results_dir, suffix="final")
        summary["steps"].append({"stage": "final", "state": state_final})
        log("Слепок состояния 'final' сохранён", level="OK")

        rpc_results = _probe_rpc_api(driver, results_dir)
        summary["rpc_probe"] = rpc_results
        log(
            "Ext.Direct RPC-прощупывание завершено: "
            f"успешных вызовов {rpc_results.get('successful', 0)}",
            level="OK",
        )

        net_summary = _collect_network_log(driver, results_dir)
        summary["network"] = net_summary
        log(
            f"Сетевой лог: всего {net_summary.get('total_requests', '?')}, "
            f"API-кандидатов {net_summary.get('api_candidates', '?')}",
            level="OK",
        )

        try:
            browser_logs = driver.get_log("browser")
            (results_dir / "browser_console_log.json").write_text(
                json.dumps(browser_logs, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            summary["browser_console_entries"] = len(browser_logs)
        except Exception as e:
            summary["browser_console_error"] = str(e)

    finally:
        try:
            if remote_debug:
                log(
                    "Оставляю Chrome открытым (remote-debug). Закройте вкладку вручную при необходимости.",
                    level="INFO",
                )
            else:
                driver.quit()
        except Exception:
            pass

    summary["finished_at"] = datetime.utcnow().isoformat() + "Z"
    return summary


def _format_check_for_report(res: CheckResult) -> str:
    icon = {"ok": "[OK]", "warn": "[!]", "fail": "[X]", "skip": "[skip]"}.get(
        res.status, "[?]"
    )
    lines = [f"### {icon} {res.name}", "", f"**Статус:** `{res.status}`", ""]
    if res.message:
        lines += [f"**Сообщение:** {res.message}", ""]
    if res.details:
        details_json = json.dumps(
            res.details, ensure_ascii=False, indent=2, default=str
        )
        if len(details_json) > 6000:
            details_json = details_json[:6000] + "\n... [обрезано]"
        lines += ["<details><summary>Подробности</summary>", "", "```json"]
        lines += details_json.splitlines()
        lines += ["```", "", "</details>", ""]
    return "\n".join(lines)


def build_report(
    checks: list[CheckResult],
    exploration: dict[str, Any] | None,
    results_dir: Path,
) -> Path:
    lines: list[str] = []
    lines.append("# Отчёт разведки ЭТП Газпромбанка")
    lines.append("")
    lines.append(f"Дата генерации: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Целевая страница: {TARGET_URL}")
    lines.append("")

    overall_bad = [c for c in checks if c.status == "fail"]
    overall_warn = [c for c in checks if c.status == "warn"]
    if not overall_bad and not overall_warn:
        verdict = "Все проверки пройдены успешно."
    elif overall_bad:
        verdict = (
            f"Есть критические проблемы ({len(overall_bad)}). "
            "Разведка сайта может быть неполной или невозможной."
        )
    else:
        verdict = f"Есть предупреждения ({len(overall_warn)}), но критических проблем нет."
    lines.append(f"**Итог:** {verdict}")
    lines.append("")

    lines.append("## 1. Проверки окружения")
    lines.append("")
    for c in checks:
        lines.append(_format_check_for_report(c))

    lines.append("## 2. Разведка сайта")
    lines.append("")
    if not exploration:
        lines.append("Разведка не выполнялась (см. проверки выше).")
        lines.append("")
    elif exploration.get("driver_error"):
        lines.append("Не удалось запустить браузер:")
        lines.append("")
        lines.append("```")
        lines.append(str(exploration["driver_error"]))
        lines.append("```")
        lines.append("")
    else:
        lines.append(f"- Целевая страница: `{exploration.get('target_url')}`")
        lines.append(f"- Старт: {exploration.get('started_at')}")
        lines.append(f"- Финиш: {exploration.get('finished_at')}")
        if "network" in exploration:
            net = exploration["network"]
            lines.append(
                f"- Сетевой лог: всего {net.get('total_requests', '?')}, "
                f"API-кандидатов {net.get('api_candidates', '?')}"
            )
        lines.append("")
        for step in exploration.get("steps", []):
            state = step.get("state", {})
            lines.append(f"### Состояние `{step.get('stage')}`")
            lines.append("")
            lines.append(f"- URL: `{state.get('current_url')}`")
            lines.append(f"- Title: `{state.get('title')}`")
            lines.append(f"- Cookies: {state.get('cookies_count', '?')}")
            lines.append(f"- Buttons: {state.get('buttons_count', '?')}")
            lines.append(f"- Links: {state.get('links_count', '?')}")
            lines.append(f"- Inputs: {state.get('inputs_count', '?')}")
            if any(k.endswith("_error") for k in state):
                errs = {k: v for k, v in state.items() if k.endswith("_error")}
                lines.append("")
                lines.append("<details><summary>Ошибки</summary>")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(errs, ensure_ascii=False, indent=2))
                lines.append("```")
                lines.append("")
                lines.append("</details>")
            lines.append("")

    lines.append("## 3. Файлы в exploration_results/")
    lines.append("")
    for p in sorted(results_dir.iterdir()):
        if p.is_file():
            size_kb = p.stat().st_size / 1024
            lines.append(f"- `{p.name}` ({size_kb:.1f} KB)")
        elif p.is_dir():
            n = len(list(p.iterdir()))
            lines.append(f"- `{p.name}/` ({n} файлов)")
    lines.append("")

    lines.append("## 4. Что делать дальше")
    lines.append("")
    lines.append(
        "Пришлите содержимое всей папки `exploration_results/` разработчику — "
        "особенно `report.md`, `network_log.json`, `api_candidates.json`, "
        "`buttons_and_links_*.json`. На их основе будет собран финальный парсер."
    )
    lines.append("")

    report_path = results_dir / "report.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Разведочный скрипт для ЭТП Газпромбанка",
    )
    parser.add_argument(
        "--auto-seconds",
        type=int,
        default=None,
        help=(
            "Неинтерактивный режим: сколько секунд держать страницу открытой "
            "для сбора сетевых запросов (вместо ожидания Enter)."
        ),
    )
    parser.add_argument(
        "--kill-chrome",
        action="store_true",
        help="Принудительно завершить все процессы chrome.exe перед запуском Selenium.",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Не запускать проверку окружения (только разведка).",
    )
    parser.add_argument(
        "--checks-only",
        action="store_true",
        help="Только проверки окружения, без запуска Chrome.",
    )
    parser.add_argument(
        "--fresh-profile",
        action="store_true",
        help=(
            "Использовать временный пустой профиль Chrome (без расширений и cookies). "
            "Рекомендуется для публичной разведки без авторизации."
        ),
    )
    parser.add_argument(
        "--remote-debug",
        action="store_true",
        help=(
            "Запустить chrome.exe отдельно с --remote-debugging-port и подключить "
            "Selenium через DevTools. Более надёжный способ для корпоративных "
            "профилей Chrome, которые Selenium не может запустить напрямую."
        ),
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=9222,
        help="Порт для --remote-debug (по умолчанию 9222).",
    )
    return parser.parse_args(argv)


def _kill_chrome_processes() -> int:
    killed = 0
    try:
        import psutil
    except ImportError:
        return -1
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() in ("chrome.exe", "chromedriver.exe"):
                p.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(2)
    return killed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    RESULTS_DIR.mkdir(exist_ok=True)
    log(f"Рабочая папка результатов: {RESULTS_DIR}", level="INFO")

    if args.kill_chrome:
        log("Принудительное завершение процессов Chrome...", level="INFO")
        n = _kill_chrome_processes()
        if n < 0:
            log("psutil не доступен — пропуск kill-chrome", level="WARN")
        else:
            log(f"Убито процессов Chrome/chromedriver: {n}", level="OK")

    checks: list[CheckResult] = []
    if not args.skip_checks:
        checks = run_all_checks()
        checks_json = [asdict(c) for c in checks]
        (RESULTS_DIR / "environment_checks.json").write_text(
            json.dumps(checks_json, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    if args.checks_only:
        build_report(checks, None, RESULTS_DIR)
        log("Режим --checks-only: разведка сайта не выполнялась", level="INFO")
        return 0

    critical_names = {"КриптоПро CSP", "Chrome + расширение CAdES"}
    critical_fail = [
        c for c in checks if c.name in critical_names and c.status == "fail"
    ]
    chrome_running = next(
        (c for c in checks if c.name == "Процессы Chrome" and c.status == "warn"),
        None,
    )

    exploration: dict[str, Any] | None = None

    if critical_fail:
        log(
            "Критические проверки провалены — разведку сайта пропускаем. "
            "Устраните проблемы и запустите скрипт снова.",
            level="ERR",
        )
        for c in critical_fail:
            log(f"  - {c.name}: {c.message}", level="ERR")
    else:
        if chrome_running and args.auto_seconds is None:
            print()
            print("=" * 70)
            print("ВНИМАНИЕ: Chrome уже запущен. Selenium НЕ сможет открыть профиль,")
            print("пока он занят другим процессом.")
            print("Закройте ВСЕ окна Chrome и нажмите Enter, чтобы продолжить.")
            print("(или введите 's' + Enter чтобы пропустить разведку сайта)")
            print("=" * 70)
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = ""
            if answer == "s":
                log("Разведка сайта пропущена пользователем", level="WARN")
            else:
                exploration = explore_site(
                    RESULTS_DIR,
                    auto_seconds=args.auto_seconds,
                    fresh_profile=args.fresh_profile,
                    remote_debug=args.remote_debug,
                    remote_port=args.remote_port,
                )
        elif chrome_running and args.auto_seconds is not None:
            log(
                "Chrome ещё запущен, а мы в --auto режиме — пробую kill-chrome автоматически",
                level="WARN",
            )
            _kill_chrome_processes()
            exploration = explore_site(
                RESULTS_DIR,
                auto_seconds=args.auto_seconds,
                fresh_profile=args.fresh_profile,
                remote_debug=args.remote_debug,
                remote_port=args.remote_port,
            )
        else:
            exploration = explore_site(
                RESULTS_DIR,
                auto_seconds=args.auto_seconds,
                fresh_profile=args.fresh_profile,
                remote_debug=args.remote_debug,
                remote_port=args.remote_port,
            )

    if exploration is not None:
        (RESULTS_DIR / "exploration_summary.json").write_text(
            json.dumps(exploration, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    report_path = build_report(checks, exploration, RESULTS_DIR)
    log(f"Отчёт сохранён: {report_path}", level="OK")
    log("Готово. Пришлите содержимое папки exploration_results/", level="OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Прервано пользователем", level="WARN")
        sys.exit(130)
    except Exception as e:
        log(f"Непредвиденная ошибка: {e}", level="ERR")
        traceback.print_exc()
        sys.exit(1)
