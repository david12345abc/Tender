from __future__ import annotations

from typing import Any

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
            f"#com/procedure/view/procedure/{proc_id}"
        )

    def fetch_page(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        res = super().fetch_page(*args, **kwargs)
        procedures = res.get("procedures")
        if isinstance(procedures, list):
            for proc in procedures:
                if not isinstance(proc, dict):
                    continue
                proc["source"] = "gpb_business"
                try:
                    type_id = int(str(proc.get("procedure_type") or "").strip())
                except (TypeError, ValueError):
                    continue
                label = GPB_BUSINESS_PROCEDURE_TYPE_ID_LABELS.get(type_id)
                if label:
                    proc["procedure_type_name"] = label
        return res

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
