from __future__ import annotations

from typing import Any

from gpb_business_client import GPB_BUSINESS_PROCEDURE_TYPE_OPTIONS, GpbBusinessClient


GPB_INTER_RAO_ORGANIZATION_ID = "5ec50776-63f0-41ff-87a1-6cd125f38e78"
GPB_INTER_RAO_URL = (
    "https://inter-rao.etpgpb.ru/"
    f"?organizationId={GPB_INTER_RAO_ORGANIZATION_ID}"
    "#com/procedure/index/223"
)

# Значения проверены на inter-rao.etpgpb.ru через Procedure.list: поле `status` в теле RPC.
GPB_INTER_RAO_STATUS_OPTIONS = [
    ("Активные", "-2"),
    ("Прием заявок", "2"),
    ("Вскрытие заявок", "3"),
    ("Рассмотрение заявок", "4"),
    ("Подведение итогов", "6"),
    ("Заключение договора", "7"),
    ("Архив", "8"),
    ("Отменен", "10"),
    ("Подача дополнительных ценовых предложений", "20"),
]

GPB_INTER_RAO_STATUS_LABEL_BY_CODE = {
    -2: "Активные",
    2: "Прием заявок",
    3: "Вскрытие заявок",
    4: "Рассмотрение заявок",
    6: "Подведение итогов",
    7: "Заключение договора",
    8: "Архив",
    10: "Отменен",
    20: "Подача дополнительных ценовых предложений",
}

GPB_INTER_RAO_PROCEDURE_TYPE_OPTIONS = GPB_BUSINESS_PROCEDURE_TYPE_OPTIONS


class GpbInterRaoClient(GpbBusinessClient):
    """Клиент секции «Газпром Интер РАО» на inter-rao.etpgpb.ru."""

    platform_key = "gpb_inter_rao"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = GPB_INTER_RAO_URL
        self.target_host = "inter-rao.etpgpb.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return (
            "https://inter-rao.etpgpb.ru/"
            f"?organizationId={GPB_INTER_RAO_ORGANIZATION_ID}"
            f"#com/procedure/view/procedure/{proc_id}/223"
        )

    def fetch_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        res = super().fetch_page(*args, **kwargs)
        procedures = res.get("procedures")
        if isinstance(procedures, list):
            for proc in procedures:
                if not isinstance(proc, dict):
                    continue
                proc["source"] = self.platform_key
                proc_id = proc.get("id") or proc.get("procedure_id")
                if proc_id:
                    url = self._detail_url(proc_id)
                    proc["url"] = url
                    proc["card_url"] = url
                status_code = self._status_code(proc)
                if status_code is not None:
                    label = GPB_INTER_RAO_STATUS_LABEL_BY_CODE.get(status_code)
                    if label:
                        proc["status_label"] = label
                        proc["status_name"] = label
                        proc["step_label"] = label
        return res

    def _status_code(self, proc: dict[str, Any]) -> int | None:
        for key in ("status", "status_id"):
            parsed = self._parse_status_code(proc.get(key))
            if parsed is not None:
                return parsed
        lots = proc.get("lots")
        if isinstance(lots, list):
            lot = next((item for item in lots if isinstance(item, dict) and item.get("actual")), None)
            if not isinstance(lot, dict):
                lot = next((item for item in lots if isinstance(item, dict)), None)
            if isinstance(lot, dict):
                for key in ("status", "status_id"):
                    parsed = self._parse_status_code(lot.get(key))
                    if parsed is not None:
                        return parsed
        return None

    @staticmethod
    def _parse_status_code(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
