from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from etp_client import STEP_ID_LABELS, TREND_PUR_LABELS

from .params import ClientFilters, SearchParams

class Sidebar(QWidget):
    """Подробная форма фильтров, похожая на форму на сайте ЭТП."""

    searchRequested = Signal()
    resetRequested = Signal()
    clientFiltersChanged = Signal()
    loadMoreRequested = Signal()
    loadAllRequested = Signal()
    stopRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumHeight(88)
        self._build_ui()

    def _make_line(self, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumWidth(190)
        return edit

    def _make_money(self) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(0, 1e13)
        spin.setGroupSeparatorShown(True)
        spin.setSpecialValueText("—")
        spin.setMinimumWidth(115)
        return spin

    def _make_int(self) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(0, 1_000_000)
        spin.setSpecialValueText("—")
        spin.setMinimumWidth(90)
        return spin

    def _make_combo(self, items: Optional[list[tuple[str, str]]] = None) -> QComboBox:
        combo = QComboBox()
        combo.setMinimumWidth(190)
        combo.addItem("Все", "")
        for label, value in items or []:
            combo.addItem(label, value)
        return combo

    def _make_calendar(self) -> QCalendarWidget:
        calendar = QCalendarWidget()
        calendar.setGridVisible(True)
        calendar.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        calendar.setHorizontalHeaderFormat(
            QCalendarWidget.HorizontalHeaderFormat.ShortDayNames
        )
        calendar.setMinimumSize(360, 285)
        view = calendar.findChild(QTableView, "qt_calendar_calendarview")
        if view is not None:
            view.horizontalHeader().setMinimumSectionSize(42)
            view.horizontalHeader().setDefaultSectionSize(42)
            view.verticalHeader().setMinimumSectionSize(28)
            view.verticalHeader().setDefaultSectionSize(28)
            view.setMinimumSize(330, 210)
        calendar.setStyleSheet(
            """
            QCalendarWidget {
                background-color: #ffffff;
                color: #1a1d22;
            }
            QCalendarWidget QWidget {
                alternate-background-color: #ffffff;
            }
            QCalendarWidget QToolButton {
                color: #1a1d22;
                background-color: #eef4ff;
                border: 1px solid #bfd0ee;
                border-radius: 4px;
                padding: 4px 8px;
                min-width: 34px;
                min-height: 24px;
            }
            QCalendarWidget QMenu {
                background-color: #ffffff;
                color: #1a1d22;
            }
            QCalendarWidget QSpinBox {
                min-width: 72px;
                min-height: 24px;
                color: #1a1d22;
                background-color: #ffffff;
            }
            QCalendarWidget QAbstractItemView {
                min-width: 330px;
                min-height: 210px;
                font-size: 12px;
                color: #1a1d22;
                background-color: #ffffff;
                selection-background-color: #3572e0;
                selection-color: #ffffff;
                outline: 0;
            }
            """
        )
        return calendar

    def _make_date(self, date: Optional[QDate] = None) -> QDateEdit:
        edit = QDateEdit()
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("dd.MM.yyyy")
        edit.setDate(date or QDate.currentDate())
        edit.setMinimumWidth(112)
        edit.setCalendarWidget(self._make_calendar())
        return edit

    def _range_row(self, left: QWidget, right: QWidget, left_text: str = "с", right_text: str = "по") -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(QLabel(left_text))
        lay.addWidget(left)
        lay.addWidget(QLabel(right_text))
        lay.addWidget(right)
        return row

    def _add_row(self, grid: QGridLayout, row: int, col: int, label: str, widget: QWidget) -> None:
        lbl = QLabel(label)
        lbl.setObjectName("FilterLabel")
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(lbl, row, col * 2)
        grid.addWidget(widget, row, col * 2 + 1)

    def _build_ui(self) -> None:
        body_layout = QVBoxLayout(self)
        body_layout.setContentsMargins(10, 8, 10, 8)
        body_layout.setSpacing(6)

        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(0, 0, 0, 0)
        quick_row.setSpacing(8)
        quick_lbl = QLabel("Быстрый поиск:")
        quick_lbl.setObjectName("SidebarTitle")
        self.ed_quick_search = self._make_line("Введите текст для поиска по всем полям")
        self.ed_quick_search.setMinimumWidth(520)
        quick_row.addWidget(quick_lbl)
        quick_row.addWidget(self.ed_quick_search, 1)
        quick_row.addWidget(QLabel("Лимит в батче:"))
        self.sb_batch_limit = QSpinBox()
        self.sb_batch_limit.setRange(25, 1000)
        self.sb_batch_limit.setSingleStep(25)
        self.sb_batch_limit.setValue(100)
        self.sb_batch_limit.setMinimumWidth(90)
        quick_row.addWidget(self.sb_batch_limit)
        self.btn_search = QPushButton("Искать")
        self.btn_search.setObjectName("Primary")
        self.btn_search.setMinimumWidth(110)
        quick_row.addWidget(self.btn_search)
        self.btn_reset = QPushButton("Сбросить")
        quick_row.addWidget(self.btn_reset)
        body_layout.addLayout(quick_row)

        self.btn_toggle_extra = QToolButton()
        self.btn_toggle_extra.setText("Дополнительные фильтры")
        self.btn_toggle_extra.setCheckable(True)
        self.btn_toggle_extra.setChecked(False)
        self.btn_toggle_extra.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_toggle_extra.setArrowType(Qt.RightArrow)
        body_layout.addWidget(self.btn_toggle_extra)

        self.extra_filters = QWidget()
        self.extra_filters.setVisible(False)
        self.extra_filters.setMinimumHeight(300)
        extra_layout = QVBoxLayout(self.extra_filters)
        extra_layout.setContentsMargins(0, 0, 0, 0)
        extra_layout.setSpacing(6)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)
        for c in (1, 3, 5):
            grid.setColumnStretch(c, 1)

        self.ed_registry = self._make_line()
        self.ed_unique_number = self._make_line()
        self.ed_title_local = self._make_line()
        self.ed_okpd2 = self._make_line()
        self.ed_okved2 = self._make_line()
        self.ed_organizer = self._make_line()
        self.ed_customer = self._make_line()
        self.ed_customer_region = self._make_line("Введите регион/область")
        self.ed_customer_agent = self._make_line()

        self.sb_guarantee_min = self._make_money()
        self.sb_guarantee_max = self._make_money()
        self.ed_responsible = self._make_line()
        self.cb_trend = self._make_combo(
            [(lbl, code) for code, lbl in TREND_PUR_LABELS.items()]
        )
        self.cb_step = self._make_combo()
        added_labels: set[str] = set()
        for code, lbl in STEP_ID_LABELS.items():
            if lbl in added_labels:
                continue
            added_labels.add(lbl)
            self.cb_step.addItem(lbl, code)
        self.cb_purchase_form = self._make_combo(
            [("Любая", ""), ("Электронная", "электрон"), ("Бумажная", "бумаж")]
        )
        self.sb_lots_min = self._make_int()
        self.sb_lots_max = self._make_int()

        self.ed_date_from = self._make_date(QDate.currentDate().addYears(-1))
        self.ed_date_to = self._make_date(QDate.currentDate())
        self.de_end_from = self._make_date(QDate.currentDate())
        self.de_end_to = self._make_date(QDate.currentDate().addMonths(3))
        self.de_results_from = self._make_date(QDate.currentDate())
        self.de_results_to = self._make_date(QDate.currentDate().addMonths(3))
        self.cb_results_enabled = QCheckBox()
        self.cb_results_enabled.setToolTip("Включить фильтр по дате подведения итогов")

        self.sb_price_min = self._make_money()
        self.sb_price_max = self._make_money()
        self.ed_special_features = self._make_line()
        self.ed_position_name = self._make_line()
        self.ed_national_regime = self._make_line()

        self.ed_query = self.ed_quick_search
        self.ed_tag_id = QSpinBox()
        self.ed_tag_id.setRange(0, 1_000_000)
        self.ed_tag_id.setSpecialValueText("— любой —")

        self.sb_apc_min = self._make_int()
        self.sb_apc_max = self._make_int()

        self._add_row(grid, 0, 0, "Номер закупки:", self.ed_registry)
        self._add_row(grid, 1, 0, "Уникальный номер закупки:", self.ed_unique_number)
        self._add_row(grid, 2, 0, "Наименование закупки:", self.ed_title_local)
        self._add_row(grid, 3, 0, "ОКПД2:", self.ed_okpd2)
        self._add_row(grid, 4, 0, "ОКВЭД2:", self.ed_okved2)
        self._add_row(grid, 5, 0, "Организатор процедуры:", self.ed_organizer)
        self._add_row(grid, 6, 0, "Заказчик:", self.ed_customer)
        self._add_row(grid, 7, 0, "Регион заказчика:", self.ed_customer_region)
        self._add_row(grid, 8, 0, "Агент заказчика:", self.ed_customer_agent)

        self._add_row(
            grid,
            0,
            1,
            "Обеспечение заявки:",
            self._range_row(self.sb_guarantee_min, self.sb_guarantee_max),
        )
        self._add_row(grid, 1, 1, "Ответственное лицо:", self.ed_responsible)
        self._add_row(grid, 2, 1, "Тип процедуры:", self.cb_trend)
        self._add_row(grid, 3, 1, "Статус процедуры:", self.cb_step)
        self._add_row(grid, 4, 1, "Форма закупки:", self.cb_purchase_form)
        self._add_row(
            grid,
            5,
            1,
            "Лотов в закупке:",
            self._range_row(self.sb_lots_min, self.sb_lots_max),
        )
        self._add_row(
            grid,
            6,
            1,
            "Намерений:",
            self._range_row(self.sb_apc_min, self.sb_apc_max),
        )
        self._add_row(grid, 7, 1, "Тег:", self.ed_tag_id)

        self._add_row(
            grid,
            0,
            2,
            "Дата публикации:",
            self._range_row(self.ed_date_from, self.ed_date_to),
        )
        self._add_row(
            grid,
            1,
            2,
            "Окончание приема заявок:",
            self._range_row(self.de_end_from, self.de_end_to),
        )
        results_row = QWidget()
        results_lay = QHBoxLayout(results_row)
        results_lay.setContentsMargins(0, 0, 0, 0)
        results_lay.setSpacing(4)
        results_lay.addWidget(self.cb_results_enabled)
        results_lay.addWidget(QLabel("с"))
        results_lay.addWidget(self.de_results_from)
        results_lay.addWidget(QLabel("по"))
        results_lay.addWidget(self.de_results_to)
        self._add_row(grid, 2, 2, "Дата подведения итогов:", results_row)
        self._add_row(
            grid,
            3,
            2,
            "Начальная цена (с НДС):",
            self._range_row(self.sb_price_min, self.sb_price_max, "от", "до"),
        )
        self._add_row(grid, 4, 2, "Специальные признаки:", self.ed_special_features)
        self._add_row(grid, 5, 2, "Наименование позиции:", self.ed_position_name)
        self._add_row(grid, 6, 2, "Национальный режим закупок:", self.ed_national_regime)

        extra_layout.addLayout(grid)
        body_layout.addWidget(self.extra_filters)

        self.btn_toggle_extra.toggled.connect(self._set_extra_visible)

        # Подключение сигналов клиентских фильтров (мгновенные)
        text_widgets = (
            self.ed_quick_search,
            self.ed_registry,
            self.ed_unique_number,
            self.ed_organizer,
            self.ed_customer,
            self.ed_customer_region,
            self.ed_customer_agent,
            self.ed_title_local,
            self.ed_okpd2,
            self.ed_okved2,
            self.ed_responsible,
            self.ed_special_features,
            self.ed_position_name,
            self.ed_national_regime,
        )
        for w in text_widgets:
            w.returnPressed.connect(self.clientFiltersChanged)
            w.editingFinished.connect(self.clientFiltersChanged)
        for w in (self.cb_trend, self.cb_step, self.cb_purchase_form):
            w.currentIndexChanged.connect(lambda *_: self.clientFiltersChanged.emit())
        spin_widgets = (
            self.sb_apc_min,
            self.sb_apc_max,
            self.sb_lots_min,
            self.sb_lots_max,
            self.sb_guarantee_min,
            self.sb_guarantee_max,
            self.sb_price_min,
            self.sb_price_max,
        )
        for w in spin_widgets:
            w.editingFinished.connect(self.clientFiltersChanged)
        self.cb_results_enabled.toggled.connect(lambda *_: self.clientFiltersChanged.emit())
        self.sb_batch_limit.editingFinished.connect(self.clientFiltersChanged)
        for w in (
            self.ed_date_from,
            self.ed_date_to,
            self.de_end_from,
            self.de_end_to,
            self.de_results_from,
            self.de_results_to,
        ):
            w.dateChanged.connect(lambda *_: self.clientFiltersChanged.emit())

        self.btn_search.clicked.connect(self.searchRequested)
        self.btn_reset.clicked.connect(self.resetRequested)

    def _set_extra_visible(self, visible: bool) -> None:
        self.extra_filters.setVisible(visible)
        self.setMinimumHeight(390 if visible else 88)
        self.btn_toggle_extra.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SidebarSection")
        return lbl

    def search_params(self) -> SearchParams:
        use_extra = self.extra_filters.isVisible()
        return SearchParams(
            date_from=self.ed_date_from.date().toString("dd.MM.yyyy") if use_extra else "",
            date_to=self.ed_date_to.date().toString("dd.MM.yyyy") if use_extra else "",
            query="",
            tag_id=(self.ed_tag_id.value() or None) if use_extra else None,
            limit=self.sb_batch_limit.value(),
        )

    def client_filters(self) -> ClientFilters:
        if not self.extra_filters.isVisible():
            return ClientFilters(quick_search=self.ed_quick_search.text().strip())
        return ClientFilters(
            quick_search=self.ed_quick_search.text().strip(),
            registry_contains=self.ed_registry.text().strip(),
            unique_number_contains=self.ed_unique_number.text().strip(),
            organizer_contains=self.ed_organizer.text().strip(),
            customer_contains=self.ed_customer.text().strip(),
            customer_region_contains=self.ed_customer_region.text().strip(),
            customer_agent_contains=self.ed_customer_agent.text().strip(),
            title_contains=self.ed_title_local.text().strip(),
            okpd2_contains=self.ed_okpd2.text().strip(),
            okved2_contains=self.ed_okved2.text().strip(),
            guarantee_min=(self.sb_guarantee_min.value() or None),
            guarantee_max=(self.sb_guarantee_max.value() or None),
            responsible_contains=self.ed_responsible.text().strip(),
            trend_pur=self.cb_trend.currentData() or "",
            step_id=self.cb_step.currentData() or "",
            purchase_form=self.cb_purchase_form.currentData() or "",
            applics_min=(self.sb_apc_min.value() or None),
            applics_max=(self.sb_apc_max.value() or None),
            lots_min=(self.sb_lots_min.value() or None),
            lots_max=(self.sb_lots_max.value() or None),
            price_min=(self.sb_price_min.value() or None),
            price_max=(self.sb_price_max.value() or None),
            published_from=(
                datetime.combine(self.ed_date_from.date().toPython(), datetime.min.time())
            ),
            published_to=(
                datetime.combine(self.ed_date_to.date().toPython(), datetime.max.time())
            ),
            end_from=(
                datetime.combine(self.de_end_from.date().toPython(), datetime.min.time())
            ),
            end_to=(
                datetime.combine(self.de_end_to.date().toPython(), datetime.max.time())
            ),
            results_from=(
                datetime.combine(self.de_results_from.date().toPython(), datetime.min.time())
                if self.cb_results_enabled.isChecked() else None
            ),
            results_to=(
                datetime.combine(self.de_results_to.date().toPython(), datetime.max.time())
                if self.cb_results_enabled.isChecked() else None
            ),
            special_features_contains=self.ed_special_features.text().strip(),
            position_name_contains=self.ed_position_name.text().strip(),
            national_regime_contains=self.ed_national_regime.text().strip(),
        )

    def reset_client_filters(self) -> None:
        self.ed_quick_search.clear()
        self.sb_batch_limit.setValue(100)
        self.ed_registry.clear()
        self.ed_unique_number.clear()
        self.ed_organizer.clear()
        self.ed_customer.clear()
        self.ed_customer_region.clear()
        self.ed_customer_agent.clear()
        self.ed_title_local.clear()
        self.ed_okpd2.clear()
        self.ed_okved2.clear()
        self.ed_responsible.clear()
        self.ed_special_features.clear()
        self.ed_position_name.clear()
        self.ed_national_regime.clear()
        self.cb_trend.setCurrentIndex(0)
        self.cb_step.setCurrentIndex(0)
        self.cb_purchase_form.setCurrentIndex(0)
        self.sb_apc_min.setValue(0)
        self.sb_apc_max.setValue(0)
        self.sb_lots_min.setValue(0)
        self.sb_lots_max.setValue(0)
        self.sb_guarantee_min.setValue(0.0)
        self.sb_guarantee_max.setValue(0.0)
        self.sb_price_min.setValue(0.0)
        self.sb_price_max.setValue(0.0)
        self.ed_tag_id.setValue(0)
        self.ed_date_from.setDate(QDate.currentDate().addYears(-1))
        self.ed_date_to.setDate(QDate.currentDate())
        self.de_end_from.setDate(QDate.currentDate())
        self.de_end_to.setDate(QDate.currentDate().addMonths(3))
        self.de_results_from.setDate(QDate.currentDate())
        self.de_results_to.setDate(QDate.currentDate().addMonths(3))
        self.cb_results_enabled.setChecked(False)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_search.setEnabled(enabled)
