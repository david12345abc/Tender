from __future__ import annotations

import os
import winreg
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BrowserConfig:
    key: str
    label: str
    exe_path: Path
    user_data_dir: Path
    profile_dir: str = "Default"
    port: int = 9222


def _env_path(name: str, default: str = "") -> Path:
    return Path(os.environ.get(name) or default)


def _first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _app_path(exe_name: str) -> Path | None:
    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "")
                path = Path(str(value).strip('"'))
                if path.exists():
                    return path
        except OSError:
            pass
    return None


def available_browsers() -> list[BrowserConfig]:
    local = _env_path("LOCALAPPDATA")
    program_files = _env_path("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = Path(
        os.environ.get("PROGRAMFILES(X86)")
        or os.environ.get("ProgramFiles(x86)")
        or r"C:\Program Files (x86)"
    )
    appdata = _env_path("APPDATA")

    chrome_app_path = _app_path("chrome.exe")
    chromium_app_path = _app_path("chromium.exe")
    edge_app_path = _app_path("msedge.exe")
    yandex_app_path = _app_path("browser.exe")
    opera_app_path = _app_path("opera.exe")

    candidates = [
        BrowserConfig(
            key="chrome",
            label="Google Chrome",
            exe_path=_first_existing(
                [
                    *( [chrome_app_path] if chrome_app_path else [] ),
                    program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
                    program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
                    local / "Google" / "Chrome" / "Application" / "chrome.exe",
                ]
            ),
            user_data_dir=local / "Google" / "Chrome" / "User Data",
            port=9222,
        ),
        BrowserConfig(
            key="edge",
            label="Microsoft Edge",
            exe_path=_first_existing(
                [
                    *( [edge_app_path] if edge_app_path else [] ),
                    program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                ]
            ),
            user_data_dir=local / "Microsoft" / "Edge" / "User Data",
            port=9223,
        ),
        BrowserConfig(
            key="yandex",
            label="Yandex Browser",
            exe_path=_first_existing(
                [
                    *( [yandex_app_path] if yandex_app_path else [] ),
                    local / "Yandex" / "YandexBrowser" / "Application" / "browser.exe",
                    program_files / "Yandex" / "YandexBrowser" / "Application" / "browser.exe",
                    program_files_x86 / "Yandex" / "YandexBrowser" / "Application" / "browser.exe",
                ]
            ),
            user_data_dir=local / "Yandex" / "YandexBrowser" / "User Data",
            port=9224,
        ),
        BrowserConfig(
            key="opera",
            label="Opera",
            exe_path=_first_existing(
                [
                    *( [opera_app_path] if opera_app_path else [] ),
                    local / "Programs" / "Opera" / "opera.exe",
                    program_files / "Opera" / "opera.exe",
                    program_files_x86 / "Opera" / "opera.exe",
                ]
            ),
            user_data_dir=appdata / "Opera Software" / "Opera Stable",
            port=9225,
        ),
        BrowserConfig(
            key="chromium",
            label="Chromium",
            exe_path=_first_existing(
                [
                    *([chromium_app_path] if chromium_app_path else []),
                    program_files / "Chromium" / "Application" / "chrome.exe",
                    program_files_x86 / "Chromium" / "Application" / "chrome.exe",
                    local / "Chromium" / "Application" / "chrome.exe",
                    local / "Chromium" / "chrome.exe",
                    local / "Programs" / "Chromium" / "Application" / "chrome.exe",
                    local / "Programs" / "Chromium" / "chromium.exe",
                ]
            ),
            user_data_dir=local / "Chromium" / "User Data",
            port=9226,
        ),
    ]

    found = [browser for browser in candidates if browser.exe_path.exists()]
    return found or [candidates[0]]
