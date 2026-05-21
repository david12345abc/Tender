"""Проверяем результат правок: тип ГП629912 и отсутствие ГП531972 в выдаче."""
from __future__ import annotations

import sys
import time

import desktop_search  # noqa: F401

from gpb_business_client import GpbBusinessClient


def main() -> int:
    client = GpbBusinessClient()
    if not client.is_chrome_running():
        client.ensure_chrome(timeout=45)
    client.connect()

    if "etp.gpb.ru" not in (client.driver.current_url or ""):
        client.driver.get(client.target_url)
        time.sleep(4)

    # Проверка типа для ГП629912
    page = client.fetch_page(start=0, limit=25, query="ГП629912")
    procs = page.get("procedures") or []
    target = next(
        (p for p in procs if str(p.get("registry_number") or "") == "ГП629912"),
        None,
    )
    if target:
        print(f"[type] ГП629912: procedure_type_name={target.get('procedure_type_name')!r}")
        print(f"       contragent_purchasemethod={target.get('contragent_purchasemethod')!r}")
        print(f"       procedure_type={target.get('procedure_type')!r}")
    else:
        print("[type] ГП629912 не вернулся фетчером (это плохо)")
        return 1

    # Проверка отсутствия ГП531972
    page2 = client.fetch_page(start=0, limit=25, query="ГП531972")
    procs2 = page2.get("procedures") or []
    regs = [str(p.get("registry_number") or "") for p in procs2]
    print(f"[phantom] поиск 'ГП531972' вернул: {regs}")
    if "ГП531972" in regs:
        print("[phantom] ОШИБКА: ГП531972 всё ещё в выдаче")
        return 2
    print("[phantom] ОК: ГП531972 отфильтрован как имущественная процедура")
    return 0


if __name__ == "__main__":
    sys.exit(main())
