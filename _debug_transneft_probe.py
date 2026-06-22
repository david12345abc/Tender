from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from desktop_app.browsers import available_browsers
from etp_client import EtpClient


TRANSNEFT_BASE = "https://transneft.etpgpb.ru/?organizationId=5ec50776-63f0-41ff-87a1-6cd125f38e78"
TRANSNEFT_URL = f"{TRANSNEFT_BASE}#com/procedure/index/223"
TRANSNEFT_CARD = f"{TRANSNEFT_BASE}#com/procedure/view/procedure/1264275/223"


def configure_chromium(client: EtpClient) -> None:
    for browser in available_browsers():
        if browser.key == "chromium":
            client.configure_browser(
                key=browser.key,
                label=browser.label,
                exe_path=browser.exe_path,
                user_data_dir=browser.user_data_dir,
                profile_dir=browser.profile_dir,
                port=browser.port,
            )
            return


def base_payload() -> dict[str, Any]:
    return {
        "sort": "id",
        "dir": "DESC",
        "with_affiliates": True,
        "date_published_from": "",
        "query": "",
        "tag_id": None,
        "limit": 25,
        "procedure_number2_like": "",
        "procedure_number_like": "",
        "title_like": "",
        "lot_nomenclature": "",
        "lot_okved": "",
        "organizer": "",
        "customer": "",
        "lot_customer_region_okato": "",
        "agents": "",
        "coordination_resolved": False,
        "guarantee_application_from": None,
        "guarantee_application_till": None,
        "department_id": -1,
        "contact_person_like": "",
        "procedure_type": 0,
        "status": "",
        "private": -1,
        "lot_count_from": "",
        "lot_count_till": "",
        "applics_added_from": "",
        "applics_added_till": "",
        "experts": "",
        "asez_plan_position_id": "",
        "date_published_till": "",
        "date_end_registration_from": "",
        "date_end_registration_till": "",
        "date_end_second_parts_review_from": "",
        "date_end_second_parts_review_till": "",
        "start_price_from": None,
        "start_price_till": None,
        "special_mark": "",
        "lot_units_search": "",
        "nm_types": "",
        "internal_registry_number": "",
        "managed_by_parent": False,
        "start": 0,
    }


def rpc(driver: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return driver.execute_async_script(
        """
        const callback = arguments[arguments.length - 1];
        const payload = arguments[0];
        (async () => {
          const token = (window.Main && (window.Main.requestToken || window.Main.token)) || '';
          const data = Object.assign({}, payload);
          delete data.__tid;
          const body = {action:'Procedure', method:'list', data:[data], type:'rpc', tid:Date.now()%1000000, token};
          try {
            const resp = await fetch('/index.php?rpctype=direct&module=default&client=etp', {
              method:'POST',
              credentials:'include',
              headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest'},
              body: JSON.stringify(body),
            });
            const text = await resp.text();
            let j = null;
            try { j = JSON.parse(text); } catch (e) {}
            const r = j && j.result || {};
            callback({
              ok: resp.ok,
              statusCode: resp.status,
              contentType: resp.headers.get('content-type') || '',
              success: r.success !== false,
              totalCount: r.totalCount,
              no_access: !!r.no_access,
              no_session: !!r.no_session,
              message: r.message || '',
              procedures: (r.procedures || []).slice(0, 8),
              token: token ? 'present' : '',
              payload: data,
              preview: text.slice(0, 700),
              url: location.href,
              title: document.title,
            });
          } catch (e) {
            callback({error:String(e && e.message || e), payload, url: location.href, title: document.title});
          }
        })();
        """,
        payload,
    )


def snapshot(driver: Any) -> dict[str, Any]:
    return driver.execute_script(
        """
        return {
          url: location.href,
          title: document.title,
          text: String(document.body && document.body.innerText || '').slice(0, 60000),
          links: Array.from(document.querySelectorAll('a[href]')).map((a) => ({
            href: a.href,
            text: String(a.innerText || a.textContent || '').trim(),
            download: a.getAttribute('download') || '',
          })).slice(0, 500),
          inputs: Array.from(document.querySelectorAll('input, button, select, textarea')).map((el) => ({
            tag: el.tagName,
            type: el.getAttribute('type'),
            name: el.getAttribute('name'),
            value: el.value || el.getAttribute('value') || '',
            placeholder: el.getAttribute('placeholder') || '',
            text: String(el.innerText || el.textContent || '').trim(),
          })).slice(0, 500),
          token: (window.Main && (window.Main.requestToken || window.Main.token)) ? 'present' : '',
        };
        """
    )


def main() -> int:
    client = EtpClient()
    configure_chromium(client)
    client.target_url = TRANSNEFT_URL
    client.target_host = "transneft.etpgpb.ru"
    client.ensure_chrome(timeout=60)
    client.connect()
    driver = client.driver
    assert driver is not None
    driver.get(TRANSNEFT_URL)
    time.sleep(10)
    list_snapshot = snapshot(driver)

    status_results = []
    for status in [-2, -1, *range(0, 80), 101, 102, 103, 104, 201, 202, 203, 301, 302]:
        payload = base_payload()
        payload["status"] = status
        result = rpc(driver, payload)
        procedures = result.get("procedures") or []
        total = result.get("totalCount")
        if total or procedures:
            sample = procedures[0] if procedures else {}
            lot = next((x for x in sample.get("lots", []) if isinstance(x, dict) and x.get("actual")), None)
            if not isinstance(lot, dict) and isinstance(sample.get("lots"), list) and sample.get("lots"):
                lot = sample["lots"][0]
            status_results.append(
                {
                    "status": status,
                    "totalCount": total,
                    "sample": {
                        "id": sample.get("id"),
                        "registry": sample.get("registry_number") or sample.get("procedure_number"),
                        "title": sample.get("title"),
                        "procedure_type": sample.get("procedure_type"),
                        "procedure_type_name": sample.get("procedure_type_name"),
                        "lot_status": (lot or {}).get("status"),
                        "lot_step": (lot or {}).get("lot_step"),
                    },
                }
            )

    queries = {}
    for field, value in (
        ("query_id", "1264275"),
        ("procedure_number_like", "1264275"),
        ("procedure_number2_like", "1264275"),
    ):
        payload = base_payload()
        if field == "query_id":
            payload["query"] = value
        else:
            payload[field] = value
        queries[field] = rpc(driver, payload)

    driver.get(TRANSNEFT_CARD)
    time.sleep(10)
    card_snapshot = snapshot(driver)
    out = {
        "browser": {
            "key": client.browser.key,
            "label": client.browser.label,
            "port": client.port,
            "exe": str(client.browser.exe_path),
        },
        "list": list_snapshot,
        "status_results": status_results,
        "queries": queries,
        "card": card_snapshot,
    }
    Path("_debug_transneft_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("_debug_transneft_probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
