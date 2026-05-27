"""Запуск анализа точно через worker-код приложения, без GUI.

Подключается к уже запущенному Chrome на порту 9222, ищет процедуру по
реестровому номеру, и прогоняет make_analyze_procedure_task — тот же
код, который выполняется при нажатии кнопки «Анализ» в приложении.
"""
from __future__ import annotations

# ВАЖНО: тот же preload, что и в desktop_search.py — иначе torch/paddle DLL ломается.
import desktop_search  # noqa: F401  (только для side-effect _preload_rag_runtime)

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from etp_client import EtpClient
from desktop_app.constants import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
from desktop_app.worker import make_analyze_procedure_task


class _FakeWorker:
    def __init__(self) -> None:
        self.progress = SimpleNamespace(emit=lambda s: print(f"[progress] {s}", flush=True))
        self.error = SimpleNamespace(emit=lambda s: print(f"[ERROR] {s}", flush=True))
        self.session = SimpleNamespace(emit=lambda ok, msg: print(f"[session ok={ok}] {msg}", flush=True))
        self._stop = False

    def is_stop_requested(self) -> bool:
        return self._stop


def _find_procedure(client: EtpClient, registry: str) -> dict | None:
    page = client.fetch_page(start=0, limit=25, query=registry)
    for p in page.get("procedures") or []:
        if str(p.get("registry_number") or p.get("procedure_number") or "") == registry:
            return p
    procs = page.get("procedures") or []
    return procs[0] if procs else None


def _random_procedures(client: EtpClient, n: int) -> list[dict]:
    page = client.fetch_page(start=0, limit=25)
    procs = page.get("procedures") or []
    return procs[:n]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="32616025062")
    parser.add_argument("--random", type=int, default=0,
                        help="Дополнительно прогнать N случайных процедур")
    args = parser.parse_args()

    print(f"[init] LM={LM_STUDIO_BASE_URL} model={LM_STUDIO_MODEL}")
    client = EtpClient()
    if not client.is_chrome_running():
        print("[init] Запускаю Chrome с DevTools…")
        client.ensure_chrome(timeout=45)
    print("[init] Подключаюсь к DevTools…")
    client.connect()

    target_proc = _find_procedure(client, args.registry)
    if not target_proc:
        print(f"[fatal] Не найдено процедуры по реестровому {args.registry}")
        return 2
    print(f"[found] {target_proc.get('registry_number')} id={target_proc.get('id')}"
          f" title={(target_proc.get('title') or '')[:80]!r}")

    procedures = [target_proc]
    if args.random > 0:
        extra = _random_procedures(client, args.random + 5)
        seen = {str(target_proc.get("id"))}
        for p in extra:
            if len(procedures) >= 1 + args.random:
                break
            if str(p.get("id")) in seen:
                continue
            procedures.append(p)
            seen.add(str(p.get("id")))
        print(f"[plan] Анализирую процедур: {len(procedures)} (1 целевая + случайные)")

    sink: dict = {}
    task = make_analyze_procedure_task(
        client=client,
        procedures=procedures,
        lm_base_url=LM_STUDIO_BASE_URL,
        lm_model=LM_STUDIO_MODEL,
        sink=sink,
    )
    worker = _FakeWorker()
    t0 = time.time()
    task(worker)
    dt = time.time() - t0
    print(f"\n[done] Анализ занял {dt:.1f}s")

    rows = sink.get("rows") or []
    out_path = Path("_debug_analysis_result.json")
    out_path.write_text(
        json.dumps(
            {
                "rows": rows,
                "raw_by_registry": {k: v[:4000] for k, v in (sink.get("raw_by_registry") or {}).items()},
                "technical_by_registry": sink.get("technical_by_registry") or {},
                "document_issues": sink.get("document_issues") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[saved] Полный результат → {out_path.resolve()}")

    print("\n=== Итоговые строки таблицы ===")
    for row in rows:
        registry = row[0] if row else "?"
        filled = sum(1 for c in row[3:] if c and c != "—")
        print(f"  {registry}: заполнено {filled}/{len(row)-3} полей")

    target = next((r for r in rows if r and str(r[0]).startswith(args.registry)), None)
    if not target:
        print("[warn] Целевой процедуры нет в результатах")
        return 1
    filled = sum(1 for c in target[3:] if c and c != "—")
    print(f"\n[target {args.registry}] filled={filled}/{len(target)-3}")
    return 0 if filled >= 3 else 1


if __name__ == "__main__":
    sys.exit(main())
