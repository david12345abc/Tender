from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from desktop_app.browsers import available_browsers
from etp_client import EtpClient


TARGET_URL = "https://corp.roseltorg.ru/#procedures"
CARD_URL = "https://corp.roseltorg.ru/#msp_lotinfo/502239/513301"


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


def sync_js(driver: Any, source: str, *args: Any) -> Any:
    return driver.execute_script(source, *args)


def async_js(driver: Any, source: str, *args: Any) -> Any:
    return driver.execute_async_script(source, *args)


def fetch_json(driver: Any, path: str) -> dict[str, Any]:
    return async_js(
        driver,
        """
        const callback = arguments[arguments.length - 1];
        const path = arguments[0];
        (async () => {
          let token = '';
          for (const key of ['app-state-authtoken', 'elk_token', 'authtoken']) {
            try {
              const raw = localStorage.getItem(key) || '';
              if (raw) {
                token = raw.startsWith('"') ? JSON.parse(raw) : raw;
                break;
              }
            } catch (e) {}
          }
          try {
            const resp = await fetch(path, {
              method:'GET',
              credentials:'include',
              headers:{
                'Accept':'application/json, text/plain, */*',
                'Authorization': token ? `Bearer ${token}` : '',
                'X-Requested-With':'XMLHttpRequest',
              },
            });
            const text = await resp.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; } catch (e) {}
            callback({
              ok: resp.ok,
              status: resp.status,
              contentType: resp.headers.get('content-type') || '',
              data,
              text: data ? '' : text.slice(0, 3000),
              path,
              token: token ? 'present' : '',
              url: location.href,
              title: document.title,
            });
          } catch (e) {
            callback({ok:false, error:String(e && e.message || e), path, token: token ? 'present' : '', url:location.href, title:document.title});
          }
        })();
        """,
        path,
    )


def search(driver: Any, filters: dict[str, Any], limit: int = 10, visibility: str = "active") -> dict[str, Any]:
    sort = quote(json.dumps([{"property": "publicationDate", "direction": "DESC"}], ensure_ascii=False))
    encoded_filters = quote(json.dumps(filters, ensure_ascii=False, separators=(",", ":")))
    path = (
        "/api/v1/elastica/configurations/procedures/search"
        f"?sort={sort}&filters={encoded_filters}&visibility={visibility}"
        f"&page=1&offset=0&limit={limit}"
    )
    return fetch_json(driver, path)


def ext_state(driver: Any) -> dict[str, Any]:
    return sync_js(
        driver,
        """
        const result = {combos: [], stores: [], grids: [], snapshot: null};
        try {
          const ls = {};
          for (let i = 0; i < localStorage.length; i++) {
            const k = localStorage.key(i);
            ls[k] = String(localStorage.getItem(k) || '').slice(0, 1400);
          }
          result.snapshot = {
            url: location.href,
            title: document.title,
            text: String(document.body && document.body.innerText || '').slice(0, 90000),
            localStorage: ls,
            resources: performance.getEntriesByType('resource').map((e) => ({
              name:e.name, initiatorType:e.initiatorType, transferSize:e.transferSize, duration:e.duration
            })).filter((e) => /api|procedure|lot|msp|document|file|search|elastica|config/i.test(e.name)).slice(-800),
            links: Array.from(document.querySelectorAll('a[href]')).map((a) => ({
              href:a.href,
              text:String(a.innerText || a.textContent || '').trim(),
              download:a.getAttribute('download') || '',
            })).slice(0, 1000),
            inputs: Array.from(document.querySelectorAll('input, button, select, textarea')).map((el) => ({
              tag:el.tagName,
              type:el.getAttribute('type'),
              name:el.getAttribute('name'),
              value:el.value || el.getAttribute('value') || '',
              placeholder:el.getAttribute('placeholder') || '',
              text:String(el.innerText || el.textContent || '').trim(),
            })).slice(0, 1000),
          };
          if (!window.Ext) return result;
          result.combos = Ext.ComponentQuery.query('combo, tagfield, combobox, check-combo-field').map((cmp) => {
            const store = cmp.getStore && cmp.getStore();
            let rows = [];
            try { rows = store && store.getRange ? store.getRange().map((r) => Object.assign({}, r.data)).slice(0, 180) : []; } catch (e) {}
            return {
              xtype: cmp.xtype,
              id: cmp.getId && cmp.getId(),
              name: cmp.getName && cmp.getName(),
              fieldLabel: cmp.fieldLabel || '',
              emptyText: cmp.emptyText || '',
              value: cmp.getValue && cmp.getValue(),
              displayValue: cmp.getRawValue && cmp.getRawValue(),
              storeId: store && store.storeId,
              rows,
            };
          }).slice(0, 260);
          result.stores = Ext.StoreManager.items.map((s) => {
            let rows = [];
            try { rows = s.getRange().map((r) => Object.assign({}, r.data)).slice(0, 30); } catch (e) {}
            let proxy = {};
            try {
              const p = s.getProxy && s.getProxy();
              proxy = p ? {type:p.type, url:p.getUrl && p.getUrl(), extraParams:p.getExtraParams && p.getExtraParams()} : {};
            } catch (e) {}
            return {storeId:s.storeId, count:s.getCount && s.getCount(), proxy, rows};
          }).filter((s) => /procedure|elastica|state|status|search|lot|document|msp/i.test(String(s.storeId) + ' ' + JSON.stringify(s.proxy) + ' ' + JSON.stringify(s.rows))).slice(0, 260);
          result.grids = Ext.ComponentQuery.query('grid').map((grid) => {
            const st = grid.getStore && grid.getStore();
            const p = st && st.getProxy && st.getProxy();
            return {
              id: grid.getId && grid.getId(),
              title: grid.title || '',
              storeId: st && st.storeId,
              count: st && st.getCount && st.getCount(),
              proxy: p ? {type:p.type, url:p.getUrl && p.getUrl(), extraParams:p.getExtraParams && p.getExtraParams()} : {},
              rows: st && st.getRange ? st.getRange().map((r) => Object.assign({}, r.data)).slice(0, 15) : [],
            };
          }).slice(0, 80);
        } catch (e) {
          result.error = String(e && e.stack || e);
        }
        return result;
        """,
    )


def main() -> int:
    client = EtpClient()
    configure_chromium(client)
    client.target_url = TARGET_URL
    client.target_host = "corp.roseltorg.ru"
    client.ensure_chrome(timeout=60)
    client.connect()
    driver = client.driver
    assert driver is not None
    driver.get(TARGET_URL)
    time.sleep(12)
    list_state = ext_state(driver)

    base = search(driver, {}, limit=10)
    candidate_filters = {
        "registrationNumber": search(driver, {"registrationNumber": "502239"}, limit=5),
        "lotId": search(driver, {"lotId": "513301"}, limit=5),
        "registrationNumberFull": search(driver, {"registrationNumber": "32312723977"}, limit=5),
        "procedureName": search(driver, {"procedureName": "поставка"}, limit=5),
        "organizerName": search(driver, {"organizerName": "газ"}, limit=5),
        "procedureType": search(driver, {"procedureType": "1"}, limit=5),
    }

    status_values: list[Any] = []
    for combo in list_state.get("combos", []):
        if not isinstance(combo, dict):
            continue
        label = str(combo.get("fieldLabel") or "").lower()
        name = str(combo.get("name") or "").lower()
        if "статус" in label or name in {"state", "status"}:
            for row in combo.get("rows") or []:
                if isinstance(row, dict):
                    status_values.append(row.get("id") or row.get("value") or row.get("code") or row.get("name"))
    status_values = [x for x in status_values if x not in (None, "")]
    status_tests = [{"value": value, "result": search(driver, {"state": [value]}, limit=3)} for value in status_values[:100]]

    driver.get(CARD_URL)
    time.sleep(12)
    card_state = ext_state(driver)
    card_fetches = {
        "procedure_info": fetch_json(driver, "/api/v1/procedure/502239/info"),
        "lot": fetch_json(driver, "/api/v1/lots/513301"),
        "msp_procedure_info": fetch_json(driver, "/api/v1/msp/procedure/502239/info"),
        "msp_lot": fetch_json(driver, "/api/v1/msp/lots/513301"),
        "docs_proc": fetch_json(driver, "/api/v1/procedure/502239/documents"),
        "docs_lot": fetch_json(driver, "/api/v1/lots/513301/documents"),
    }

    out = {
        "list": list_state,
        "base": base,
        "candidate_filters": candidate_filters,
        "status_values": status_values,
        "status_tests": status_tests,
        "card": card_state,
        "card_fetches": card_fetches,
    }
    Path("_debug_roseltorg_223_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print("_debug_roseltorg_223_probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
