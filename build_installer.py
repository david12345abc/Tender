from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "dist"
INSTALLER_DIR = DIST_DIR / "installer"
SPEC_FILE = ROOT / "ETP_GPB_Search.spec"
ISS_FILE = ROOT / "ETP_GPB_Search.iss"


def _file_url(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except Exception:
        return "file:///" + quote(str(path.resolve()).replace("\\", "/"))


def _run(cmd: list[str], *, env: dict[str, str]) -> None:
    print(">", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=ROOT, env=env)


def _find_iscc(explicit: str = "") -> str:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        [
            Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
            Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
        ]
    )
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "Не найден Inno Setup Compiler (ISCC.exe). Установите Inno Setup 6 "
        "или передайте путь через --iscc."
    )


def _write_manifest(*, version: str, installer_path: Path, publish_url: str, release_notes: str) -> Path:
    installer_url = publish_url.rstrip("/") + "/" + installer_path.name if publish_url else _file_url(installer_path)
    payload = {
        "version": version,
        "installer_url": installer_url,
        "release_notes": release_notes,
        "silent_args": "/SILENT /NORESTART",
    }
    manifest_path = INSTALLER_DIR / "latest_release.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Собрать onedir-приложение и установщик обновления.")
    parser.add_argument("--version", required=True, help="Версия релиза, например 1.0.1")
    parser.add_argument(
        "--publish-url",
        default="",
        help="HTTP/UNC/file URL папки, где будет лежать установщик. Если пусто, в manifest пишется file:// на локальный installer.",
    )
    parser.add_argument("--release-notes", default="", help="Короткое описание релиза для окна обновления.")
    parser.add_argument("--iscc", default="", help="Путь к ISCC.exe, если его нет в PATH.")
    args = parser.parse_args()

    env = os.environ.copy()
    env["ETP_GPB_APP_VERSION"] = args.version
    (ROOT / "app_version.txt").write_text(args.version, encoding="utf-8")

    _run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC_FILE)], env=env)

    iscc = _find_iscc(args.iscc)
    _run([iscc, str(ISS_FILE)], env=env)

    installer_path = INSTALLER_DIR / f"ETP_GPB_Search_Setup_{args.version}.exe"
    if not installer_path.is_file():
        raise RuntimeError(f"Установщик не найден после сборки: {installer_path}")
    manifest_path = _write_manifest(
        version=args.version,
        installer_path=installer_path,
        publish_url=args.publish_url,
        release_notes=args.release_notes,
    )
    print(f"Готово: {installer_path}")
    print(f"Манифест обновления: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
