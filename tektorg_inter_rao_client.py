from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

from tektorg_rosneft_client import TektorgRosneftClient, _format_date_filter, _safe_text


TEKTORG_INTER_RAO_URL = "https://www.tektorg.ru/inter_rao/procedures"

TEKTORG_INTER_RAO_STATUS_OPTIONS = [
    ("Архив", "Архив"),
    ("Отменён", "Отменён"),
    ("Приём заявок", "Приём заявок"),
    ("Работа комиссии", "Работа комиссии"),
]

TEKTORG_INTER_RAO_TYPE_OPTIONS = [
    ("Аукцион", "Аукцион"),
    ("Закупка у единственного поставщика", "Закупка у единственного поставщика"),
    ("Запрос котировок", "Запрос котировок"),
    ("Запрос оферт", "Запрос оферт"),
    ("Запрос предложений", "Запрос предложений"),
    ("Запрос цен", "Запрос цен"),
    ("Конкурс", "Конкурс"),
]


class TektorgInterRaoClient(TektorgRosneftClient):
    """Клиент отдельной секции ТЭК-Торг «ПАО Интер РАО»."""

    platform_key = "tektorg_inter_rao"

    def __init__(self, port: int = 9222) -> None:
        super().__init__(port=port)
        self.target_url = TEKTORG_INTER_RAO_URL
        self.target_host = "www.tektorg.ru"

    def _detail_url(self, proc_id: Any) -> str:
        return f"{TEKTORG_INTER_RAO_URL}/{proc_id}"

    def _build_query(self, start: int, client_filters: Any = None, query: Optional[str] = None) -> str:
        params: list[tuple[str, str]] = []
        page = max(1, start // 15 + 1)
        if page > 1:
            params.append(("page", str(page)))
        if client_filters is not None:
            quick_search = _safe_text(query or getattr(client_filters, "quick_search", ""))
            registry = _safe_text(getattr(client_filters, "registry_contains", ""))
            if registry:
                params.append(("registryNumber", registry))
            elif quick_search:
                # На странице Интер РАО быстрый поиск формы называется именно `name`.
                params.append(("name", quick_search))

            for status in tuple(getattr(client_filters, "step_ids", ()) or ()):
                status_text = _safe_text(status)
                if status_text:
                    # Серверная форма Интер РАО принимает статусы как массив `status[]`.
                    params.append(("status[]", status_text))

            price_min = getattr(client_filters, "price_min", None)
            price_max = getattr(client_filters, "price_max", None)
            if price_min is not None:
                params.append(("sumPrice_start", str(price_min)))
            if price_max is not None:
                params.append(("sumPrice_end", str(price_max)))
            published = _format_date_filter(getattr(client_filters, "published_from", None))
            if published:
                params.append(("datePublished", published))
            end = _format_date_filter(getattr(client_filters, "end_to", None))
            if end:
                params.append(("dateEndRegistration", end))
        elif query:
            params.append(("name", query))
        return ("?" + urlencode(params, doseq=True)) if params else ""
