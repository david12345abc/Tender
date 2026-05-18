from __future__ import annotations

from typing import Any

from etp_client import EtpClient


GPB_BUSINESS_ORGANIZATION_ID = "5ec50776-63f0-41ff-87a1-6cd125f38e78"
GPB_BUSINESS_URL = (
    "https://etp.gpb.ru/"
    f"?organizationId={GPB_BUSINESS_ORGANIZATION_ID}"
    "#com/procedure/index/223"
)


class GpbBusinessClient(EtpClient):
    """Клиент ГПБ Бизнес.

    Площадка использует тот же ExtJS/RPC-контур, что и ЭТП ГПБ, но другой
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
