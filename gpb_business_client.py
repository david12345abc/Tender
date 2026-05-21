from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from etp_client import EtpClient


GPB_BUSINESS_ORGANIZATION_ID = "5ec50776-63f0-41ff-87a1-6cd125f38e78"
GPB_BUSINESS_URL = (
    "https://etp.gpb.ru/"
    f"?organizationId={GPB_BUSINESS_ORGANIZATION_ID}"
    "#com/procedure/index/223"
)

GPB_BUSINESS_PROCEDURE_TYPE_OPTIONS = [
    ("Аукцион на понижение", "2"),
    ("Конкурс", "3"),
    ("Запрос предложений", "4"),
    ("Запрос (ценовых) котировок", "5"),
    ("Предварительный отбор", "6"),
    ("Редукцион", "11"),
    ("Попозиционная", "13"),
    ("Маркетинговые исследования", "31"),
    ("Конкурентный отбор", "32"),
    ("Аукцион на понижение (конкурентный)", "34"),
    ("Запрос котировок (конкурентный)", "35"),
    ("Конкурс в электронной форме (конкурентный)", "36"),
    ("Закупка у единственного поставщика", "45"),
    ("Запрос предложений (конкурентный)", "48"),
    ("Запрос предложений в электронной форме для СМСП", "26"),
    ("Запрос котировок в электронной форме для СМСП", "27"),
    ("Конкурс в электронной форме для СМСП", "28"),
    ("Аукцион на понижение в электронной форме для СМСП", "29"),
]

GPB_BUSINESS_PROCEDURE_TYPE_ID_LABELS = {
    int(value): label
    for label, value in GPB_BUSINESS_PROCEDURE_TYPE_OPTIONS
}

_BUSINESS_223_COLLECT_DOCUMENT_LINKS_JS = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const bodyText = () => String(document.body && document.body.innerText || "");
  for (let i = 0; i < 70; i++) {
    const text = bodyText();
    if (
      String(location.href || "").includes("procedure/view")
      && (/Документация процедуры/i.test(text) || /Сведения о процедуре/i.test(text))
    ) {
      break;
    }
    await wait(300);
  }

  try {
    const tabs = Array.from(document.querySelectorAll(".x-tab-inner, .x-tab-right, .x-tab"));
    const seenTabs = new Set();
    for (const tab of tabs) {
      const label = String(tab.innerText || tab.textContent || "").trim();
      if (!label || seenTabs.has(label)) continue;
      seenTabs.add(label);
      if (/Сведения|Лоты|Документ|Извещение/i.test(label)) {
        try {
          tab.click();
          await wait(250);
        } catch (e) {}
      }
    }
  } catch (e) {}

  const fileRe = /([^\\/\n\r\t<>:"|?*]+?\.(?:docx?|xlsx?|xlsm|pdf|zip(?:\.\d{3})?|rar(?:\.\d{3})?|7z(?:\.\d{3})?|rtf|txt|xml|csv))/i;
  const docs = new Map();

  function clean(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function filenameNear(anchor) {
    const sources = [
      anchor.innerText,
      anchor.textContent,
      anchor.getAttribute("title"),
      anchor.getAttribute("download"),
    ];
    let node = anchor;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      sources.push(node.innerText, node.textContent);
    }
    for (const source of sources) {
      for (const line of String(source || "").split(/\r?\n/)) {
        const lineText = clean(line);
        const lineMatch = lineText.match(fileRe);
        if (lineMatch) return lineMatch[1];
      }
      const text = clean(source);
      const match = text.match(fileRe);
      if (match) return match[1];
    }
    return clean(anchor.innerText || anchor.textContent || anchor.href || "document");
  }

  function add(anchor) {
    const href = anchor.href || anchor.getAttribute("href") || "";
    if (!href || href === "javascript:;") return;
    const name = filenameNear(anchor);
    const ownText = clean([
      anchor.innerText,
      anchor.textContent,
      anchor.getAttribute("title"),
      anchor.getAttribute("download"),
    ].filter(Boolean).join(" "));
    const isDoc = /\/file\/get\/t\/(?:LotDocuments|ProcedureDocuments)\b/i.test(href)
      || fileRe.test(href)
      || fileRe.test(ownText);
    if (!isDoc) return;
    const current = docs.get(href);
    if (!current || (!fileRe.test(current.text) && fileRe.test(name))) {
      docs.set(href, { href, text: name });
    }
  }

  Array.from(document.querySelectorAll("a[href]")).forEach(add);
  callback(Array.from(docs.values()));
})();
"""


class GpbBusinessClient(EtpClient):
    """Клиент секции Бизнес.223.

    Площадка использует тот же ExtJS/RPC-контур, что и секция Газпром, но другой
    домен и стартовый маршрут с organizationId.
    """

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = GPB_BUSINESS_URL
        self.target_host = "etp.gpb.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return (
            "https://etp.gpb.ru/"
            f"?organizationId={GPB_BUSINESS_ORGANIZATION_ID}"
            f"#com/procedure/view/procedure/{proc_id}/223"
        )

    def fetch_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        res = super().fetch_page(*args, **kwargs)
        procedures = res.get("procedures")
        if isinstance(procedures, list):
            # На etp.gpb.ru имущественные торги (is_type_property=True) живут в
            # отдельном разделе и в стандартном реестре закупок не показываются.
            # У нас RPC отдаёт их вперемешку, поэтому отсеиваем тут — иначе в
            # выдаче появляются «фантомные» процедуры (например, ГП531972),
            # которые на сайте через обычный поиск не находятся.
            filtered: list[dict[str, Any]] = []
            for proc in procedures:
                if isinstance(proc, dict) and bool(proc.get("is_type_property")):
                    continue
                filtered.append(proc)
            procedures = filtered
            res["procedures"] = procedures

            total = res.get("totalCount")
            if isinstance(total, int) and total > len(procedures):
                # totalCount теперь рассинхронизирован, но это лучше, чем
                # показывать в реестре закупки, отфильтрованные сайтом.
                pass
            for proc in procedures:
                if not isinstance(proc, dict):
                    continue
                proc["source"] = "gpb_business"
                proc_id = proc.get("id") or proc.get("procedure_id")
                if proc_id:
                    proc["url"] = self._detail_url(proc_id)

                # На etp.gpb.ru тип в карточке/реестре строится так:
                #   1) если у процедуры заполнен contragent_purchasemethod —
                #      показывается именно он (например, «Сбор коммерческих
                #      предложений»), независимо от procedure_type;
                #   2) иначе — серверный procedure_type_name;
                #   3) иначе — маппинг по числовому procedure_type.
                # Это отличается от секции Газпром, поэтому правим только тут.
                method_label = str(proc.get("contragent_purchasemethod") or "").strip()
                server_type_name = str(proc.get("procedure_type_name") or "").strip()
                if method_label:
                    proc["procedure_type_name"] = method_label
                elif server_type_name:
                    proc["procedure_type_name"] = server_type_name
                else:
                    try:
                        type_id = int(str(proc.get("procedure_type") or "").strip())
                    except (TypeError, ValueError):
                        type_id = None
                    if type_id is not None:
                        label = GPB_BUSINESS_PROCEDURE_TYPE_ID_LABELS.get(type_id)
                        if label:
                            proc["procedure_type_name"] = label
        return res

    def _business_document_links(self) -> list[dict[str, Any]]:
        assert self.driver is not None, "Сначала вызовите connect()"
        try:
            self.driver.set_script_timeout(120)
            links = self.driver.execute_async_script(_BUSINESS_223_COLLECT_DOCUMENT_LINKS_JS)
        finally:
            self.driver.set_script_timeout(30)
        if not isinstance(links, list):
            return []
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in links:
            if not isinstance(item, dict):
                continue
            href = str(item.get("href") or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            text = str(item.get("text") or "").strip()
            ext_match = re.fullmatch(r"(?i)(docx?|xlsx?|xlsm|pdf|zip|rar|7z|rtf|txt|xml|csv)", text)
            if ext_match:
                text = f"document_{len(cleaned) + 1}.{ext_match.group(1).lower()}"
            cleaned.append({"href": href, "text": text})
        return cleaned

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Скачивает документы карточки секции Бизнес.223.

        В Бизнес.223 URL файла часто выглядит как /file/get/... без расширения,
        поэтому имя файла берём из соседнего DOM-блока документации.
        """
        assert self.driver is not None, "Сначала вызовите connect()"
        proc_id = proc.get("id") or proc.get("procedure_id")
        if not proc_id:
            raise RuntimeError("У процедуры нет id для открытия подробной страницы.")

        registry = str(proc.get("registry_number") or proc.get("procedure_number") or proc_id)
        title = str(proc.get("title") or "")
        folder_name = self._safe_filename(f"{registry}_{title[:80]}", str(proc_id))
        registry_digits = re.sub(r"\D+", "", registry)
        output_digits = re.sub(r"\D+", "", output_root.name)
        out_dir = output_root if registry_digits and registry_digits in output_digits else output_root / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)

        url = self._detail_url(proc_id)
        if progress:
            progress(f"Открываю подробную страницу {registry}: {url}")
        self.driver.get(url)

        links = self._business_document_links()
        saved: list[str] = []
        errors: list[str] = []

        for index, link in enumerate(links, start=1):
            if progress:
                progress(f"Скачиваю {registry}: {link.get('text') or index}")
            try:
                saved.append(str(self.download_document_link(link, out_dir, index=index)))
            except Exception as e:
                errors.append(f"{link.get('text') or link.get('href') or index}: {e}")

        return {
            "procedure": registry,
            "url": url,
            "folder": str(out_dir),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }

    def _prepare_fetch_payload(self, payload: dict[str, Any], client_filters: Any = None) -> None:
        # На etp.gpb.ru номер процедуры ищется через общий query. Поля
        # procedure_number_like/procedure_number2_like возвращают 0 результатов.
        search_parts = [
            str(payload.get("query") or "").strip(),
            str(payload.get("procedure_number_like") or "").strip(),
            str(payload.get("procedure_number2_like") or "").strip(),
        ]
        query = next((part for part in search_parts if part), "")
        if query:
            payload["query"] = query
        payload["procedure_number_like"] = ""
        payload["procedure_number2_like"] = ""
