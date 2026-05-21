"""Дамп полей процедур секции Бизнес.223 для анализа источника типа.

Подключаемся к уже работающему Chrome (DevTools 9222), переходим на etp.gpb.ru
(если ещё не там), запрашиваем Procedure.list и печатаем все ключи у нескольких
процедур, в первую очередь у ГП629912, чтобы понять по какому именно полю сайт
показывает «Сбор коммерческих предложений».
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import desktop_search  # noqa: F401  preload torch/faiss

from gpb_business_client import GpbBusinessClient


TARGET_REGISTRIES = {"ГП629912", "ГП531972"}


def _dump_proc(proc: dict) -> dict:
    return {
        k: (v if not isinstance(v, (dict, list)) else json.loads(json.dumps(v, ensure_ascii=False, default=str)))
        for k, v in sorted(proc.items())
    }


def main() -> int:
    client = GpbBusinessClient()
    if not client.is_chrome_running():
        client.ensure_chrome(timeout=45)
    client.connect()

    try:
        cur_url = client.driver.current_url
    except Exception:
        cur_url = ""
    if "etp.gpb.ru" not in cur_url:
        client.driver.get(client.target_url)
        import time
        time.sleep(4)

    out: list[dict] = []
    seen_targets: set[str] = set()
    for start in range(0, 200, 25):
        try:
            page = client.fetch_page(start=start, limit=25)
        except Exception as e:
            print(f"[fetch] start={start} error: {e}")
            break
        procs = page.get("procedures") or []
        if not procs:
            break
        for p in procs:
            reg = str(p.get("registry_number") or p.get("procedure_number") or "")
            if reg in TARGET_REGISTRIES:
                seen_targets.add(reg)
                out.append({"registry": reg, "fields": _dump_proc(p)})
            elif len(out) - len(seen_targets) < 3:
                out.append({"registry": reg, "fields": _dump_proc(p)})
        if seen_targets >= TARGET_REGISTRIES:
            break

    print(f"\n[summary] Найдено целевых: {sorted(seen_targets)}")

    Path("_debug_business_types.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sample_keys: set[str] = set()
    for item in out:
        sample_keys.update(item["fields"].keys())
    print(f"\n[keys union] {sorted(sample_keys)}")

    type_like = ("procedure_type", "trend_pur", "trend_pur_id", "trend_pur_name",
                 "purchase_method", "purchase_method_name", "method", "method_name",
                 "purchase_type", "purchase_type_name", "procurement_method",
                 "tender_type", "lot_type", "type_id", "type_name",
                 "order_type", "request_type", "kind", "kind_id", "kind_name")
    for item in out:
        f = item["fields"]
        small = {k: f.get(k) for k in type_like if k in f}
        print(f"\n[{item['registry']}]")
        print(f"  type-like fields: {small}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
