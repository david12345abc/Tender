from __future__ import annotations

from typing import Any

from roseltorg_223_client import (
    ROSELTORG_223_PROCEDURE_TYPE_OPTIONS,
    ROSELTORG_223_STATUS_OPTIONS,
    Roseltorg223Client,
    _safe_text,
)


ROSELTORG_INTERRAO_URL = "https://interrao.roseltorg.ru/#procedures/all"
ROSELTORG_INTERRAO_PROCEDURE_TYPE_OPTIONS = ROSELTORG_223_PROCEDURE_TYPE_OPTIONS
ROSELTORG_INTERRAO_STATUS_OPTIONS = ROSELTORG_223_STATUS_OPTIONS


class RoseltorgInterRaoClient(Roseltorg223Client):
    """Клиент секции Росэлторг «Корпоративные закупки» (`interrao.roseltorg.ru`)."""

    platform_key = "roseltorg_interrao"
    target_host_name = "interrao.roseltorg.ru"
    procedures_config = "interrao-procedures"
    lots_config = "interrao-lots"
    session_message = "Нет активной сессии Росэлторг Корпоративные закупки. Выполните авторизацию в Chromium."

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = ROSELTORG_INTERRAO_URL
        self.target_host = self.target_host_name

    def _detail_url(self, proc_id: Any, lot_id: Any = None) -> str:
        if lot_id:
            return f"https://{self.target_host_name}/#msp_lotinfo/{proc_id}/{lot_id}"
        return f"https://{self.target_host_name}/#msp_lotinfo/{proc_id}"

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        proc = super()._normalize_item(item)
        proc["source"] = self.platform_key
        proc["url"] = self._detail_url(proc.get("id"), proc.get("lot_id"))
        proc["card_url"] = proc["url"]
        return proc

    def _request_json(self, path: str) -> dict[str, Any]:
        result = super()._request_json(path)
        if isinstance(result, dict) and result.get("no_session"):
            result["message"] = self.session_message
        return result

    def download_document_link(self, link: dict[str, Any], output_dir, index: int = 1):
        href = _safe_text((link or {}).get("href"))
        if href.startswith("/"):
            link = {**(link or {}), "href": f"https://{self.target_host_name}{href}"}
        return super().download_document_link(link, output_dir, index=index)
