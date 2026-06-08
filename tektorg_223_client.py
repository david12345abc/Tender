from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from tektorg_inter_rao_client import TektorgInterRaoClient
from tektorg_rosneft_client import _safe_filename, _safe_text


TEKTORG_223_URL = "https://www.tektorg.ru/223-fz/procedures"

TEKTORG_223_STATUS_OPTIONS = [
    ("Архив", "Архив"),
    ("Отменён", "Отменён"),
    ("Приём заявок", "Приём заявок"),
    ("Работа комиссии", "Работа комиссии"),
]

TEKTORG_223_TYPE_OPTIONS = [
    ("Аукцион", "Аукцион"),
    ("Закупка у единственного поставщика", "Закупка у единственного поставщика"),
    ("Запрос котировок", "Запрос котировок"),
    ("Запрос оферт", "Запрос оферт"),
    ("Запрос предложений", "Запрос предложений"),
    ("Запрос предоставления ценовой информации", "Запрос предоставления ценовой информации"),
    ("Запрос цен", "Запрос цен"),
    ("Конкурс", "Конкурс"),
]


class Tektorg223Client(TektorgInterRaoClient):
    """Клиент отдельной секции ТЭК-Торг «223-ФЗ и коммерческие закупки»."""

    platform_key = "tektorg_223"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_223_URL
        self.target_host = "www.tektorg.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return f"{TEKTORG_223_URL}/{proc_id}"

    def _normalize_row(self, raw: Any, index: int = 0) -> dict[str, Any]:
        row = super()._normalize_row(raw, index)
        subsection = _safe_text(row.get("subsectionAlias") or row.get("subsection_alias"))
        proc_id = row.get("id") or row.get("procedure_id") or index + 1
        if subsection:
            card_url = f"https://www.tektorg.ru/org/{subsection}/procedures/{proc_id}"
        else:
            card_url = self._detail_url(proc_id)
        row["url"] = card_url
        row["card_url"] = card_url
        return row

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id"))
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry or "tektorg_223")
        out_dir.mkdir(parents=True, exist_ok=True)

        card = self.extract_procedure_card_text(proc, progress=progress)
        links = [link for link in (card.get("document_links") or []) if isinstance(link, dict)]

        saved: list[str] = []
        errors: list[str] = []
        for index, link in enumerate(links, start=1):
            try:
                if progress:
                    progress(f"Скачиваю файл {index}/{len(links)}")
                saved.append(str(self.download_document_link(link, out_dir, index)))
            except Exception as exc:
                errors.append(f"{link.get('text') or link.get('href')}: {exc}")
        return {
            "procedure": registry,
            "url": card.get("url"),
            "folder": str(out_dir),
            "found": len(links),
            "saved": saved,
            "errors": errors,
        }
