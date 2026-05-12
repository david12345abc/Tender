from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from etp_client import SERVER_STATUS_BY_LABEL, STATUS_LABELS, procedure_type_label, step_id_label, trend_pur_label

from .constants import COLUMNS
from .keywords import load_keywords
from .params import ClientFilters
from .utils import fmt_date, fmt_money, parse_dt, parse_price


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", text.casefold().replace("ё", "е"))


def _contains_keyword_as_words(text: str, keyword: str) -> bool:
    haystack = _word_tokens(text)
    needle = _word_tokens(keyword)
    if not haystack or not needle:
        return False
    if len(needle) == 1:
        return needle[0] in set(haystack)
    last_start = len(haystack) - len(needle)
    return any(haystack[i : i + len(needle)] == needle for i in range(last_start + 1))


def _normalize_status(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold().replace("ё", "е")).strip()


SERVER_STATUS_LABEL_BY_VALUE: dict[int, str] = {}
for _label in STATUS_LABELS:
    _value = SERVER_STATUS_BY_LABEL.get(_normalize_status(_label))
    if _value is not None and _value not in SERVER_STATUS_LABEL_BY_VALUE:
        SERVER_STATUS_LABEL_BY_VALUE[_value] = _label


def _server_status_label(proc: dict[str, Any]) -> Optional[str]:
    for key in ("status", "status_id"):
        value = proc.get(key)
        try:
            label = SERVER_STATUS_LABEL_BY_VALUE.get(int(str(value)))
        except (TypeError, ValueError):
            label = None
        if label:
            return label
    lots = proc.get("lots")
    if isinstance(lots, list):
        lot = next((item for item in lots if isinstance(item, dict) and item.get("actual")), None)
        if not isinstance(lot, dict):
            lot = next((item for item in lots if isinstance(item, dict)), None)
        if isinstance(lot, dict):
            for key in ("status", "status_id"):
                value = lot.get(key)
                try:
                    label = SERVER_STATUS_LABEL_BY_VALUE.get(int(str(value)))
                except (TypeError, ValueError):
                    label = None
                if label:
                    return label
    return None


def _server_status_values(proc: dict[str, Any]) -> tuple[int, ...]:
    values: list[int] = []

    def append_status(value: Any) -> None:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return
        if parsed not in values:
            values.append(parsed)

    for key in ("status", "status_id"):
        append_status(proc.get(key))
    lots = proc.get("lots")
    if isinstance(lots, list):
        lot = next((item for item in lots if isinstance(item, dict) and item.get("actual")), None)
        if not isinstance(lot, dict):
            lot = next((item for item in lots if isinstance(item, dict)), None)
        if isinstance(lot, dict):
            for key in ("status", "status_id"):
                append_status(lot.get(key))
    return tuple(values)


class ProcedureTableModel(QAbstractTableModel):
    COL_KEYS = [c[0] for c in COLUMNS]
    COL_TITLES = [c[1] for c in COLUMNS]

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._keywords: tuple[str, ...] = ()

    def set_keywords(self, keywords: tuple[str, ...]) -> None:
        self._keywords = keywords
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, len(self.COL_KEYS) - 1),
            )

    def _all_text(self, value: Any) -> str:
        values: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                for nested in v.values():
                    walk(nested)
            elif isinstance(v, (list, tuple, set)):
                for nested in v:
                    walk(nested)
            elif v is not None:
                values.append(str(v))

        walk(value)
        return " ".join(values).casefold()

    def _keyword_matches(self, proc: dict[str, Any]) -> list[str]:
        haystack = " ".join(
            str(proc.get(key) or "")
            for key in ("title", "name", "procedure_name")
        )
        keywords = self._keywords or tuple(load_keywords())
        return [
            keyword
            for keyword in keywords
            if _contains_keyword_as_words(haystack, keyword)
        ]

    def _first_date(self, proc: dict[str, Any], keys: tuple[str, ...]) -> Optional[datetime]:
        for key in keys:
            dt = parse_dt(proc.get(key))
            if dt is not None:
                return dt
        return None

    def _status_label(self, proc: dict[str, Any]) -> str:
        status_suffix = ""
        if proc.get("oos_changes_status") == 1:
            status_suffix = "\nОжидается публикация изменений на ЕИС"

        def with_suffix(label: str) -> str:
            return label + status_suffix if status_suffix and status_suffix not in label else label

        for status_key in (
            "step_name",
            "step_label",
            "status_name",
            "status_label",
            "state_name",
            "stage_name",
        ):
            if proc.get(status_key):
                return with_suffix(str(proc[status_key]))
        step = proc.get("step_id")
        status_blob = " ".join(
            str(proc.get(key) or "")
            for key in (
                "status",
                "status_name",
                "status_label",
                "state",
                "state_name",
                "stage",
                "stage_name",
                "step_name",
                "step_label",
            )
        ).casefold()
        if "архив" in status_blob:
            return with_suffix("В архиве")
        server_label = _server_status_label(proc)
        if server_label:
            return with_suffix(server_label)
        lots = proc.get("lots")
        if isinstance(lots, list) and lots:
            lot = next((item for item in lots if isinstance(item, dict) and item.get("actual")), None)
            if not isinstance(lot, dict):
                lot = next((item for item in lots if isinstance(item, dict)), None)
            if isinstance(lot, dict):
                lot_step = str(lot.get("lot_step") or "").casefold()
                if lot.get("date_archived"):
                    return with_suffix("В архиве")
                if lot_step in {"second_parts", "second_parts_review"}:
                    second_parts_dt = parse_dt(lot.get("date_end_second_parts_review"))
                    if (
                        second_parts_dt is not None
                        and second_parts_dt.replace(tzinfo=None) < datetime.now()
                    ):
                        return with_suffix("Подведение итогов")
                    return with_suffix("Рассмотрение заявок")
                if lot_step in {"registration", "applic_access"}:
                    end_dt = parse_dt(lot.get("date_end_registration"))
                    if end_dt is not None and end_dt.replace(tzinfo=None) < datetime.now():
                        return with_suffix("Подведение итогов")
                lot_status = lot.get("status")
                if lot_status == 6:
                    return with_suffix("Подведение итогов")
                if lot_status == 5:
                    return with_suffix("Рассмотрение заявок")
        # На ЭТП ГПБ у некоторых процедур технический step_id остаётся старым.
        # Фактическую стадию берём из дат блока «Этапы закупочной процедуры».
        if step in {"applic_access", "registration"}:
            results_dt = self._first_date(
                proc,
                (
                    "date_results",
                    "date_result",
                    "date_summingup",
                    "date_end_procedure",
                    "date_review",
                    "date_consideration",
                    "date_end_review",
                    "date_end_final",
                    "date_end_second_parts_review",
                    "date_end_final_offers",
                ),
            )
            if results_dt is not None and results_dt.replace(tzinfo=None) < datetime.now():
                return with_suffix("Подведение итогов")
            end_dt = self._first_date(
                proc,
                (
                    "date_end_registration",
                    "date_finish_registration",
                    "date_end_applic",
                    "date_finish_applic",
                    "date_end",
                ),
            )
            if end_dt is not None and end_dt.replace(tzinfo=None) < datetime.now():
                return with_suffix("Подведение итогов")
        return with_suffix(step_id_label(step))

    def _status_background_color(self, proc: dict[str, Any]) -> Optional[QColor]:
        status = self._status_label(proc).casefold()
        if "архив" in status or "заверш" in status:
            return QColor(235, 235, 235)
        if "отмен" in status:
            return QColor(248, 225, 225)
        if (
            "подвед" in status
            or "рассмотр" in status
            or "провер" in status
            or "вскры" in status
        ):
            return QColor(250, 240, 210)
        if "ожида" in status:
            return QColor(230, 240, 255)
        if (
            "прием" in status
            or "приём" in status
            or "регистрац" in status
            or "актив" in status
            or "повышение" in status
        ):
            return QColor(225, 245, 225)
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.COL_KEYS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COL_TITLES[section]
        if role == Qt.DisplayRole and orientation == Qt.Vertical:
            return "☆"
        return None

    def _display(self, proc: dict[str, Any], key: str) -> Any:
        if key == "trend_pur_label":
            procedure_type = proc.get("procedure_type")
            if procedure_type not in (None, ""):
                label = procedure_type_label(procedure_type)
                if label != str(procedure_type):
                    return label
            for type_key in (
                "trend_pur_name",
                "trend_pur_label",
                "procedure_type_name",
                "type_name",
                "procedure_type",
            ):
                if proc.get(type_key) and not str(proc[type_key]).isdigit():
                    return str(proc[type_key])
            return trend_pur_label(proc.get("trend_pur"))
        if key == "step_label":
            return self._status_label(proc)
        if key == "organizer":
            return proc.get("short_name") or proc.get("full_name") or ""
        if key == "tags_label":
            tags = proc.get("tags") or []
            return ", ".join(str(t) for t in tags) if tags else ""
        if key == "date_start_registration":
            return fmt_date(
                self._first_date(
                    proc,
                    (
                        "date_start_registration",
                        "date_begin_registration",
                        "date_registration_start",
                        "date_start_applic",
                        "date_begin_applic",
                        "date_published",
                    ),
                )
            )
        if key == "date_end_registration":
            return fmt_date(parse_dt(proc.get("date_end_registration")))
        if key == "total_price":
            p = parse_price(proc.get("total_price"))
            return fmt_money(p, proc.get("currency_name") or "RUB")
        if key == "applics_count":
            return proc.get("applics_count") if proc.get("applics_count") is not None else ""
        if key == "title":
            return proc.get("title") or ""
        if key == "registry_number":
            return proc.get("registry_number") or proc.get("procedure_number") or ""
        if key == "keyword_matches":
            return ", ".join(self._keyword_matches(proc))
        return proc.get(key, "")

    def _sort_value(self, proc: dict[str, Any], key: str) -> Any:
        if key == "total_price":
            return parse_price(proc.get("total_price")) or 0.0
        if key == "applics_count":
            return int(proc.get("applics_count") or 0)
        if key == "date_start_registration":
            return self._first_date(
                proc,
                (
                    "date_start_registration",
                    "date_begin_registration",
                    "date_registration_start",
                    "date_start_applic",
                    "date_begin_applic",
                    "date_published",
                ),
            ) or datetime.min
        if key == "date_end_registration":
            return parse_dt(proc.get("date_end_registration")) or datetime.min
        if key == "trend_pur_label":
            return str(proc.get("trend_pur") or "")
        if key == "step_label":
            return str(proc.get("step_id") or "")
        if key == "organizer":
            return str(proc.get("short_name") or proc.get("full_name") or "").lower()
        if key == "title":
            return str(proc.get("title") or "").lower()
        if key == "registry_number":
            return str(proc.get("registry_number") or proc.get("procedure_number") or "")
        if key == "keyword_matches":
            return self._display(proc, key)
        return str(proc.get(key) or "")

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._rows)):
            return None
        proc = self._rows[row]
        col_key = self.COL_KEYS[index.column()]

        if role == Qt.DisplayRole:
            return self._display(proc, col_key)
        if role == Qt.UserRole:
            return self._sort_value(proc, col_key)
        if role == Qt.TextAlignmentRole:
            if col_key in ("total_price", "applics_count"):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ToolTipRole:
            if col_key == "organizer":
                parts: list[str] = []
                if proc.get("full_name"):
                    parts.append(str(proc["full_name"]))
                if proc.get("org_inn"):
                    parts.append(f"ИНН {proc['org_inn']}")
                if proc.get("org_kpp"):
                    parts.append(f"КПП {proc['org_kpp']}")
                return "\n".join(parts) or None
            if col_key == "title":
                return str(proc.get("title") or "")
            if col_key == "keyword_matches":
                matches = self._keyword_matches(proc)
                return "\n".join(matches) if matches else "Совпадений по ключевым словам нет"
            if col_key == "date_start_registration":
                info = []
                for k in (
                    "date_start_registration",
                    "date_begin_registration",
                    "date_registration_start",
                    "date_start_applic",
                    "date_begin_applic",
                    "date_published",
                ):
                    v = proc.get(k)
                    if v:
                        info.append(f"{k}: {v}")
                return "\n".join(info) or None
            if col_key == "date_end_registration":
                return str(proc.get("date_end_registration") or "")
            if col_key == "registry_number":
                info = []
                for k in ("registry_number", "procedure_number", "procedure_number2"):
                    v = proc.get(k)
                    if v:
                        info.append(f"{k}: {v}")
                info.append(f"id: {proc.get('id')}")
                return "\n".join(info)
        if role == Qt.BackgroundRole:
            return None
        return None

    def set_rows(self, procs: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(procs)
        self.endResetModel()

    def append_rows(self, procs: list[dict[str, Any]]) -> None:
        if not procs:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(procs) - 1)
        self._rows.extend(procs)
        self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def rows(self) -> list[dict[str, Any]]:
        return self._rows

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


class ProcedureFilterProxy(QSortFilterProxyModel):
    PAGE_SIZE = 25

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._flt = ClientFilters()
        self._page = 0
        self._matching_rows_cache: Optional[list[int]] = None
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def setSourceModel(self, source_model: QAbstractTableModel) -> None:
        super().setSourceModel(source_model)
        source_model.modelReset.connect(self._clear_matching_cache)
        source_model.rowsInserted.connect(self._clear_matching_cache)
        source_model.rowsRemoved.connect(self._clear_matching_cache)
        source_model.dataChanged.connect(self._clear_matching_cache)

    def _clear_matching_cache(self, *args: Any) -> None:
        self._matching_rows_cache = None

    def set_filters(self, flt: ClientFilters) -> None:
        self._flt = flt
        self._page = 0
        self._clear_matching_cache()
        self.invalidateFilter()

    def refresh_page(self) -> None:
        self._clear_matching_cache()
        self._page = max(0, min(self._page, self.page_count() - 1))
        self.invalidateFilter()

    def page_size(self) -> int:
        return self.PAGE_SIZE

    def current_page(self) -> int:
        return self._page

    def filtered_count(self) -> int:
        return len(self._matching_source_rows())

    def filtered_source_rows(self) -> list[int]:
        return list(self._matching_source_rows())

    def page_count(self) -> int:
        total = self.filtered_count()
        return max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def set_page(self, page: int) -> None:
        new_page = max(0, min(page, self.page_count() - 1))
        if new_page == self._page:
            return
        self._page = new_page
        self.invalidateFilter()

    def next_page(self) -> None:
        self.set_page(self._page + 1)

    def previous_page(self) -> None:
        self.set_page(self._page - 1)

    def _matching_source_rows(self) -> list[int]:
        if self._matching_rows_cache is not None:
            return self._matching_rows_cache
        model = self.sourceModel()
        if not isinstance(model, ProcedureTableModel):
            self._matching_rows_cache = []
            return self._matching_rows_cache
        self._matching_rows_cache = [
            row
            for row in range(model.rowCount())
            if self._matches_source_row(row, QModelIndex())
        ]
        if self._page >= self.page_count():
            self._page = self.page_count() - 1
        return self._matching_rows_cache

    def _all_text(self, value: Any) -> str:
        values: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                for nested in v.values():
                    walk(nested)
            elif isinstance(v, (list, tuple, set)):
                for nested in v:
                    walk(nested)
            elif v is not None:
                values.append(str(v))

        walk(value)
        return " ".join(values).casefold()

    def _blob(
        self,
        proc: dict[str, Any],
        keys: tuple[str, ...] = (),
        key_contains: tuple[str, ...] = (),
    ) -> str:
        values: list[str] = []
        for key in keys:
            value = proc.get(key)
            if isinstance(value, (list, tuple, set)):
                values.extend(str(v) for v in value)
            elif isinstance(value, dict):
                values.extend(str(v) for v in value.values())
            elif value is not None:
                values.append(str(value))
        if key_contains:
            needles = tuple(s.lower() for s in key_contains)
            for key, value in proc.items():
                if any(n in str(key).lower() for n in needles):
                    if isinstance(value, (list, tuple, set)):
                        values.extend(str(v) for v in value)
                    elif isinstance(value, dict):
                        values.extend(str(v) for v in value.values())
                    elif value is not None:
                        values.append(str(value))
        return " ".join(values).lower()

    def _contains(
        self,
        proc: dict[str, Any],
        needle: str,
        keys: tuple[str, ...] = (),
        key_contains: tuple[str, ...] = (),
    ) -> bool:
        if not needle:
            return True
        return needle.lower() in self._blob(proc, keys, key_contains)

    def _date_in_range(
        self,
        proc: dict[str, Any],
        keys: tuple[str, ...],
        date_from: Optional[datetime],
        date_to: Optional[datetime],
    ) -> bool:
        if date_from is None and date_to is None:
            return True
        dt: Optional[datetime] = None
        for key in keys:
            dt = parse_dt(proc.get(key))
            if dt is not None:
                break
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        if date_from is not None and naive < date_from:
            return False
        if date_to is not None and naive > date_to:
            return False
        return True

    def _numeric_value(self, proc: dict[str, Any], keys: tuple[str, ...]) -> Optional[float]:
        for key in keys:
            value = parse_price(proc.get(key))
            if value is not None:
                return value
        return None

    def _lot_count(self, proc: dict[str, Any]) -> int:
        for key in ("lots_count", "lot_count", "lots_cnt", "positions_count"):
            value = proc.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        lots = proc.get("lots")
        if isinstance(lots, list):
            return len(lots)
        return 1

    def _status_matches(self, proc: dict[str, Any], selected: str, display_status: str) -> bool:
        selected_norm = _normalize_status(selected)
        if not selected_norm:
            return True
        try:
            selected_code = int(str(selected).strip())
        except (TypeError, ValueError):
            selected_code = None
        if selected_code is not None and selected_code > 0:
            raw_status_values = _server_status_values(proc)
            if raw_status_values:
                return selected_code in raw_status_values

        raw_selected = _normalize_status(selected)
        for key in ("state", "step_id", "status", "status_label", "step_label"):
            if _normalize_status(proc.get(key)) == raw_selected:
                return True

        try:
            selected_label = SERVER_STATUS_LABEL_BY_VALUE.get(int(str(selected).strip()))
        except (TypeError, ValueError):
            selected_label = None
        if selected_label:
            selected_norm = _normalize_status(selected_label)

        display_values = [
            display_status,
            str(display_status or "").splitlines()[0],
        ]
        if any(_normalize_status(value) == selected_norm for value in display_values):
            return True

        return False

    def _matches_source_row(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, ProcedureTableModel):
            return True
        proc = model.row_at(source_row)
        if proc is None:
            return False
        f = self._flt

        def selected_matches(
            selected: str,
            code_keys: tuple[str, ...],
            text_keys: tuple[str, ...],
        ) -> bool:
            needle = selected.casefold()
            for key in code_keys:
                value = str(proc.get(key) or "")
                if value and value.casefold() == needle:
                    return True
            for key in text_keys:
                value = str(proc.get(key) or "")
                if value and needle in value.casefold():
                    return True
            return needle in self._all_text({key: proc.get(key) for key in code_keys + text_keys})

        if f.quick_search:
            needle = f.quick_search.casefold()
            if needle not in self._all_text(proc):
                return False

        if f.keyword_search_enabled:
            haystack = self._blob(
                proc,
                ("title", "name", "procedure_name"),
            )
            keywords = tuple(k for k in f.keywords if k.strip())
            if not keywords or not any(
                _contains_keyword_as_words(haystack, keyword)
                for keyword in keywords
            ):
                return False

        if f.registry_contains:
            if not self._contains(
                proc,
                f.registry_contains,
                ("registry_number", "procedure_number", "procedure_number2", "number"),
            ):
                return False

        if f.unique_number_contains:
            if not self._contains(
                proc,
                f.unique_number_contains,
                ("unique_number", "purchase_number", "external_id", "procedure_guid", "guid"),
                ("unique",),
            ):
                return False

        if f.organizer_contains:
            if not self._contains(
                proc,
                f.organizer_contains,
                ("short_name", "full_name", "org_inn", "org_ogrn", "org_kpp"),
                ("organizer", "org_"),
            ):
                return False

        if f.customer_contains:
            if not self._contains(
                proc,
                f.customer_contains,
                ("customer", "customers", "customer_name", "customer_full_name", "customer_inn"),
                ("customer", "client"),
            ):
                return False

        if f.customer_region_contains:
            if not self._contains(
                proc,
                f.customer_region_contains,
                ("region", "region_name", "customer_region", "delivery_region"),
                ("region",),
            ):
                return False

        if f.customer_agent_contains:
            if not self._contains(
                proc,
                f.customer_agent_contains,
                ("agent", "customer_agent", "agent_name"),
                ("agent",),
            ):
                return False

        if f.title_contains:
            if not self._contains(
                proc,
                f.title_contains,
                ("title", "name", "procedure_name", "lot_name"),
            ):
                return False

        if f.okpd2_contains:
            if not self._contains(proc, f.okpd2_contains, key_contains=("okpd", "okpd2")):
                return False

        if f.okved2_contains:
            if not self._contains(proc, f.okved2_contains, key_contains=("okved", "okved2")):
                return False

        if f.responsible_contains:
            if not self._contains(
                proc,
                f.responsible_contains,
                ("contact_person", "responsible", "responsible_person", "contact_fio"),
                ("responsible", "contact"),
            ):
                return False

        trend_values = tuple(getattr(f, "trend_pur_values", ()) or ())
        if trend_values:
            if not any(
                selected_matches(
                    trend,
                    ("trend_pur", "procedure_type", "type"),
                    ("trend_pur_name", "trend_pur_label", "procedure_type_name", "type_name"),
                )
                for trend in trend_values
            ):
                return False
        elif f.trend_pur and not selected_matches(
            f.trend_pur,
            ("trend_pur", "procedure_type", "type"),
            ("trend_pur_name", "trend_pur_label", "procedure_type_name", "type_name"),
        ):
            return False
        if f.step_ids:
            def is_active_selection(step_id: Any) -> bool:
                normalized = _normalize_status(step_id)
                if normalized == "активные":
                    return True
                try:
                    return int(str(step_id).strip()) == -2
                except (TypeError, ValueError):
                    return False

            active_only = len(f.step_ids) == 1 and is_active_selection(f.step_ids[0])
            effective_step_ids = (
                ()
                if active_only
                else tuple(step_id for step_id in f.step_ids if not is_active_selection(step_id))
            )
            display_status = model._status_label(proc)
            if effective_step_ids and not any(
                self._status_matches(proc, step_id, display_status)
                for step_id in effective_step_ids
            ):
                return False
        law_values = tuple(getattr(f, "law_values", ()) or ())
        if law_values:
            if not any(
                self._contains(
                    proc,
                    law,
                    ("law", "law_name", "fz", "federal_law", "purchase_law"),
                    ("law", "fz", "фз", "закон"),
                )
                for law in law_values
            ):
                return False
        if f.purchase_form:
            if not self._contains(
                proc,
                f.purchase_form,
                ("purchase_form", "form", "trade_form", "procedure_form"),
                ("form",),
            ):
                return False

        apc = int(proc.get("applics_count") or 0)
        if f.applics_min is not None and apc < f.applics_min:
            return False
        if f.applics_max is not None and apc > f.applics_max:
            return False

        lot_count = self._lot_count(proc)
        if f.lots_min is not None and lot_count < f.lots_min:
            return False
        if f.lots_max is not None and lot_count > f.lots_max:
            return False

        guarantee = self._numeric_value(
            proc,
            (
                "guarantee",
                "guarantee_amount",
                "application_guarantee",
                "applic_guarantee",
                "security_sum",
                "customer_ignore_guarantee",
            ),
        )
        if f.guarantee_min is not None and (guarantee is None or guarantee < f.guarantee_min):
            return False
        if f.guarantee_max is not None and (guarantee is None or guarantee > f.guarantee_max):
            return False

        price = parse_price(proc.get("total_price"))
        if f.price_min is not None and (price is None or price < f.price_min):
            return False
        if f.price_max is not None and (price is None or price > f.price_max):
            return False

        if not self._date_in_range(
            proc, ("date_published", "date_publication"), f.published_from, f.published_to
        ):
            return False
        if not self._date_in_range(
            proc, ("date_end_registration", "date_finish_registration"), f.end_from, f.end_to
        ):
            return False
        if not self._date_in_range(
            proc,
            ("date_results", "date_result", "date_summingup", "date_end_procedure"),
            f.results_from,
            f.results_to,
        ):
            return False

        if f.special_features_contains:
            if not self._contains(
                proc,
                f.special_features_contains,
                ("special_features", "features", "tags"),
                ("feature", "special", "tag"),
            ):
                return False

        if f.position_name_contains:
            if not self._contains(
                proc,
                f.position_name_contains,
                ("position_name", "positions", "item_name", "product_name", "title"),
                ("position", "product", "item"),
            ):
                return False

        if f.national_regime_contains:
            if not self._contains(
                proc,
                f.national_regime_contains,
                ("national_regime", "national_mode", "national_treatment"),
                ("national", "regime"),
            ):
                return False
        return True

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not isinstance(self.sourceModel(), ProcedureTableModel):
            return True
        if not self._matches_source_row(source_row, source_parent):
            return False
        try:
            ordinal = self._matching_source_rows().index(source_row)
        except ValueError:
            return False
        page_start = self._page * self.PAGE_SIZE
        return page_start <= ordinal < page_start + self.PAGE_SIZE
