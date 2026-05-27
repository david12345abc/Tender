from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .constants import (
    APP_ROOT,
    APP_VERSION,
    UPDATE_MANIFEST_ENV,
    UPDATE_MANIFEST_FILE_NAME,
    user_writable_root,
)


@dataclass
class UpdateInfo:
    current_version: str
    latest_version: str
    installer_url: str
    release_notes: str = ""
    silent_args: str = "/SILENT /NORESTART"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in str(value or "").strip().split("."):
        number = ""
        for ch in part:
            if ch.isdigit():
                number += ch
            else:
                break
        parts.append(int(number or "0"))
    return tuple(parts or [0])


def is_newer_version(latest: str, current: str = APP_VERSION) -> bool:
    latest_tuple = _version_tuple(latest)
    current_tuple = _version_tuple(current)
    width = max(len(latest_tuple), len(current_tuple))
    latest_tuple += (0,) * (width - len(latest_tuple))
    current_tuple += (0,) * (width - len(current_tuple))
    return latest_tuple > current_tuple


def _read_text_url(url: str, timeout: int = 15) -> str:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8-sig")
    if parsed.scheme == "file":
        path = Path(url2pathname(unquote(parsed.path)))
        return path.read_text(encoding="utf-8-sig")
    return Path(url).read_text(encoding="utf-8-sig")


def configured_manifest_url() -> str:
    env_value = os.environ.get(UPDATE_MANIFEST_ENV, "").strip()
    if env_value:
        return env_value

    for candidate in (
        user_writable_root() / UPDATE_MANIFEST_FILE_NAME,
        APP_ROOT / UPDATE_MANIFEST_FILE_NAME,
    ):
        try:
            if candidate.is_file():
                value = candidate.read_text(encoding="utf-8-sig").strip()
                if value:
                    return value
        except Exception:
            pass

    for candidate in (APP_ROOT / "latest_release.json", user_writable_root() / "latest_release.json"):
        if candidate.is_file():
            return str(candidate)
    return ""


def check_for_update(manifest_url: str | None = None) -> UpdateInfo | None:
    url = (manifest_url or configured_manifest_url()).strip()
    if not url:
        return None
    data = json.loads(_read_text_url(url))
    latest_version = str(data.get("version") or "").strip()
    installer_url = str(data.get("installer_url") or data.get("url") or "").strip()
    if not latest_version or not installer_url:
        return None
    if not is_newer_version(latest_version):
        return None
    return UpdateInfo(
        current_version=APP_VERSION,
        latest_version=latest_version,
        installer_url=installer_url,
        release_notes=str(data.get("release_notes") or ""),
        silent_args=str(data.get("silent_args") or "/SILENT /NORESTART"),
    )


def _download_or_copy_installer(installer_url: str) -> Path:
    parsed = urlparse(installer_url)
    target_dir = Path(tempfile.gettempdir()) / "ETP_GPB_Search_Update"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_name = Path(url2pathname(unquote(parsed.path))).name or "ETP_GPB_Search_Setup.exe"
    target = target_dir / file_name
    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(installer_url, timeout=120) as response:
            target.write_bytes(response.read())
        return target
    source = Path(url2pathname(unquote(parsed.path))) if parsed.scheme == "file" else Path(installer_url)
    shutil.copy2(source, target)
    return target


def install_update(update: UpdateInfo) -> Path:
    installer = _download_or_copy_installer(update.installer_url)
    args = [str(installer), *update.silent_args.split()]
    subprocess.Popen(args, close_fds=True)
    return installer
