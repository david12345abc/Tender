"""Ищем процедуру ГП531972 в выдаче Бизнес.223 и печатаем все её поля,
чтобы понять, почему она появляется в нашем приложении, но не находится на сайте.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import desktop_search  # noqa: F401

from gpb_business_client import GpbBusinessClient


TARGET = "ГП531972"


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
        time.sleep(4)

    found = None
    for start in range(0, 4000, 25):
        try:
            page = client.fetch_page(start=start, limit=25, query=TARGET)
        except Exception as e:
            print(f"[fetch] start={start} err={e}")
            break
        procs = page.get("procedures") or []
        if not procs:
            print(f"[stop] start={start} empty page")
            break
        for p in procs:
            reg = str(p.get("registry_number") or p.get("procedure_number") or "")
            if reg == TARGET:
                found = p
                break
        if found:
            break

    if not found:
        print(f"[done] {TARGET} не найден через query")
        return 1

    print(f"[found] {TARGET} id={found.get('id')}")
    print(f"  step / status fields:")
    interesting = [
        "stage", "step_id", "step_second_parts", "is_hide_purchase",
        "private", "draft", "cancel_basis", "is_competitive", "managed_by_parent",
        "oos_publish_status", "oos_cancel_status", "oos_changes_status",
        "coordination_status", "outside_coordination_status",
        "date_published", "date_start", "date_end_registration",
        "date_end_tender_proposals", "procedure_type", "procedure_type_name",
        "contragent_purchasemethod", "law_regulation", "tags",
        "registry_number", "procedure_number", "url", "title",
    ]
    for k in interesting:
        if k in found:
            print(f"    {k}: {found.get(k)!r}")

    Path("_debug_business_phantom.json").write_text(
        json.dumps(found, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
