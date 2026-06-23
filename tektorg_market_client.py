from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from tektorg_223_client import (
    TEKTORG_223_STATUS_OPTIONS,
    TEKTORG_223_TYPE_OPTIONS,
    Tektorg223Client,
)
from tektorg_rosneft_client import _safe_filename, _safe_text


TEKTORG_MARKET_URL = "https://www.tektorg.ru/market/procedures"

TEKTORG_MARKET_STATUS_OPTIONS = TEKTORG_223_STATUS_OPTIONS
TEKTORG_MARKET_TYPE_OPTIONS = TEKTORG_223_TYPE_OPTIONS


class TektorgMarketClient(Tektorg223Client):
    """Клиент отдельной секции ТЭК-Торг «Интернет-магазин»."""

    platform_key = "tektorg_market"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_MARKET_URL
        self.target_host = "www.tektorg.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return f"{TEKTORG_MARKET_URL}/{proc_id}"

    def _looks_like_card_id_search(self, client_filters: Any = None, query: Optional[str] = None) -> str:
        value = super()._looks_like_card_id_search(client_filters, query)
        # В Интернет-магазине реестровые номера тоже числовые (например 921648),
        # поэтому прямое открытие карточки включаем только для длинных id процедуры.
        return value if re.fullmatch(r"\d{8,}", value) else ""

    def _row_from_card_id(self, proc_id: str) -> dict[str, Any]:
        try:
            return super()._row_from_card_id(proc_id)
        except RuntimeError as exc:
            message = str(exc).replace("223-ФЗ", "Интернет-магазин")
            raise RuntimeError(message) from exc

    def download_procedure_documents(
        self,
        proc: dict[str, Any],
        output_root: Path,
        progress: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        registry = _safe_text(proc.get("registry_number") or proc.get("procedure_number") or proc.get("id"))
        title = _safe_text(proc.get("title"))
        out_dir = output_root / _safe_filename(f"{registry}_{title[:80]}", registry or "tektorg_market")
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
