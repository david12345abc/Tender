from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class SearchParams:
    date_from: str = ""
    date_to: str = ""
    query: str = ""
    tag_id: Optional[int] = None
    limit: int = 500
    sort: str = "id"
    direction: str = "DESC"


@dataclass
class ClientFilters:
    quick_search: str = ""
    keyword_search_enabled: bool = False
    keywords: tuple[str, ...] = ()
    registry_contains: str = ""
    unique_number_contains: str = ""
    organizer_contains: str = ""
    customer_contains: str = ""
    customer_region_contains: str = ""
    customer_agent_contains: str = ""
    title_contains: str = ""
    okpd2_contains: str = ""
    okved2_contains: str = ""
    guarantee_min: Optional[float] = None
    guarantee_max: Optional[float] = None
    responsible_contains: str = ""
    trend_pur: str = ""
    trend_pur_values: tuple[str, ...] = ()
    step_ids: tuple[str, ...] = ()
    law_values: tuple[str, ...] = ()
    purchase_form: str = ""
    applics_min: Optional[int] = None
    applics_max: Optional[int] = None
    lots_min: Optional[int] = None
    lots_max: Optional[int] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    published_from: Optional[datetime] = None
    published_to: Optional[datetime] = None
    end_from: Optional[datetime] = None
    end_to: Optional[datetime] = None
    results_from: Optional[datetime] = None
    results_to: Optional[datetime] = None
    special_features_contains: str = ""
    position_name_contains: str = ""
    national_regime_contains: str = ""
