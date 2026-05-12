from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from PySide6.QtCore import QDate, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter
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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from etp_client import PROCEDURE_TYPE_OPTIONS, STATUS_OPTIONS

from .assets import asset_path
from .browsers import BrowserConfig, available_browsers
from .keywords import load_keyword_items, load_keywords
from .params import ClientFilters, SearchParams

DEFAULT_REQUEST_LIMIT = 500


class ChevronComboBox(QComboBox):
    """QComboBox with a plain text v marker instead of the platform arrow."""

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(
            self.rect().adjusted(0, 0, -10, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "v",
        )


class NoWheelSpinBox(QSpinBox):
    """Numeric filter input that does not change value on accidental scroll."""

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Money filter input that does not change value on accidental scroll."""

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class StatusMultiSelect(QWidget):
    """Compact multi-select control that opens the status list above the form."""

    def __init__(
        self,
        options: Sequence[str | tuple[str, str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(190)
        self.setObjectName("StatusMultiSelect")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.button = QToolButton()
        self.button.setObjectName("StatusMultiSelectButton")
        self.button.setText("Все")
        self.button.setMinimumHeight(36)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.DownArrow)
        self.button.clicked.connect(self._show_popup)
        layout.addWidget(self.button)

        self.chips = QLabel("")
        self.chips.setObjectName("StatusChips")
        self.chips.setWordWrap(True)
        self.chips.setVisible(False)
        layout.addWidget(self.chips)

        self.popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        self.popup.setObjectName("StatusPopup")
        popup_layout = QVBoxLayout(self.popup)
        popup_layout.setContentsMargins(8, 8, 8, 8)
        popup_layout.setSpacing(6)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("StatusPopupList")
        self.list_widget.setMinimumHeight(180)
        self.list_widget.setMaximumHeight(280)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setAlternatingRowColors(False)
        self.set_options(options)
        self.list_widget.itemChanged.connect(self._update_button_text)
        popup_layout.addWidget(self.list_widget)

    def set_options(self, options: Sequence[str | tuple[str, str]]) -> None:
        selected_values = {
            str(self.list_widget.item(i).data(Qt.UserRole) or "")
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.Checked
        }
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for option in options:
            if isinstance(option, tuple):
                label, value = option
            else:
                label = option
                value = option
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, value)
            item.setCheckState(Qt.Checked if str(value) in selected_values else Qt.Unchecked)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        self._update_button_text()

    def _show_popup(self) -> None:
        self._update_button_text()
        width = max(self.width(), 300)
        row_h = max(28, self.list_widget.sizeHintForRow(0))
        height = min(300, max(210, row_h * min(self.list_widget.count(), 8) + 24))
        self.popup.resize(width, height)
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()
        self.list_widget.setFocus()

    def _update_button_text(self) -> None:
        selected = [
            self.list_widget.item(i).text()
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.Checked
        ]
        if not selected:
            self.button.setText("Все")
            self.chips.setText("")
            self.chips.setVisible(False)
        elif len(selected) == 1:
            self.button.setText(selected[0])
            self.chips.setText(f"[{selected[0]} ×]")
            self.chips.setVisible(True)
        else:
            self.button.setText(f"Выбрано: {len(selected)}")
            shown = selected[:3]
            text = "  ".join(f"[{s} ×]" for s in shown)
            if len(selected) > len(shown):
                text += f"  +{len(selected) - len(shown)}"
            self.chips.setText(text)
            self.chips.setVisible(True)


class QuickMultiSelect(QWidget):
    """Compact quick-filter multiselect with a dark popup checklist."""

    selectionChanged = Signal()

    def __init__(
        self,
        options: Sequence[str | tuple[str, str]],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("QuickMultiSelect")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.button = QToolButton()
        self.button.setObjectName("QuickMultiSelectButton")
        self.button.setText("Все")
        self.button.setMinimumHeight(38)
        self.button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.button.clicked.connect(self._show_popup)
        layout.addWidget(self.button)

        self.popup = QWidget(self, Qt.Popup | Qt.FramelessWindowHint)
        self.popup.setObjectName("StatusPopup")
        popup_layout = QVBoxLayout(self.popup)
        popup_layout.setContentsMargins(8, 8, 8, 8)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("StatusPopupList")
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setMinimumHeight(170)
        self.list_widget.setMaximumHeight(300)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        popup_layout.addWidget(self.list_widget)
        self.set_options(options)

    def set_options(self, options: Sequence[str | tuple[str, str]]) -> None:
        selected = set(self.selected_values())
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for option in options:
            if isinstance(option, tuple):
                label, value = option
            else:
                label = option
                value = option
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, str(value))
            item.setCheckState(Qt.Checked if str(value) in selected else Qt.Unchecked)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        self._update_button_text()

    def selected_values(self) -> tuple[str, ...]:
        return tuple(
            str(self.list_widget.item(i).data(Qt.UserRole) or "")
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.Checked
        )

    def selected_label_values(self) -> list[tuple[str, str]]:
        return [
            (
                self.list_widget.item(i).text(),
                str(self.list_widget.item(i).data(Qt.UserRole) or ""),
            )
            for i in range(self.list_widget.count())
            if self.list_widget.item(i).checkState() == Qt.Checked
        ]

    def clear_selection(self) -> None:
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            self.list_widget.item(i).setCheckState(Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self._update_button_text()
        self.selectionChanged.emit()

    def unselect_value(self, value: str) -> None:
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if str(item.data(Qt.UserRole) or "") == str(value):
                item.setCheckState(Qt.Unchecked)
        self.list_widget.blockSignals(False)
        self._update_button_text()
        self.selectionChanged.emit()

    def _show_popup(self) -> None:
        width = max(self.width(), 240)
        row_h = max(28, self.list_widget.sizeHintForRow(0))
        height = min(320, max(190, row_h * min(self.list_widget.count(), 9) + 24))
        self.popup.resize(width, height)
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()
        self.list_widget.setFocus()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        self._update_button_text()
        self.selectionChanged.emit()

    def _update_button_text(self) -> None:
        count = len(self.selected_values())
        if count == 0:
            self.button.setText("Все  v")
        elif count == 1:
            self.button.setText(f"{self.selected_label_values()[0][0]}  v")
        else:
            self.button.setText(f"Выбрано: {count}  v")

class Sidebar(QWidget):
    """Подробная форма фильтров, похожая на форму на сайте ЭТП."""

    searchRequested = Signal()
    resetRequested = Signal()
    clientFiltersChanged = Signal()
    loadMoreRequested = Signal()
    loadAllRequested = Signal()
    stopRequested = Signal()
    editKeywordsRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumHeight(88)
        self._filter_rows: dict[str, tuple[QLabel, QWidget, int, int]] = {}
        self._filter_controls: dict[str, QWidget] = {}
        self._platform_key = "gpb"
        self._build_ui()

    def _make_line(self, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumWidth(220)
        edit.setMinimumHeight(42)
        edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return edit

    def _make_money(self) -> QDoubleSpinBox:
        spin = NoWheelDoubleSpinBox()
        spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        spin.setDecimals(2)
        spin.setRange(0, 1e13)
        spin.setGroupSeparatorShown(True)
        spin.setSpecialValueText("—")
        spin.setMinimumWidth(88)
        spin.setMinimumHeight(42)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return spin

    def _make_int(self) -> QSpinBox:
        spin = NoWheelSpinBox()
        spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        spin.setRange(0, 1_000_000)
        spin.setSpecialValueText("—")
        spin.setMinimumWidth(76)
        spin.setMinimumHeight(42)
        spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return spin

    def _make_combo(self, items: Optional[list[tuple[str, str]]] = None) -> QComboBox:
        combo = ChevronComboBox()
        combo.setMinimumWidth(220)
        combo.setMinimumHeight(42)
        combo.setMaxVisibleItems(8)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
                background-color: #0b1020;
                color: #dbeafe;
            }
            QCalendarWidget QWidget {
                alternate-background-color: #111827;
            }
            QCalendarWidget QToolButton {
                color: #dbeafe;
                background-color: #111827;
                border: 1px solid #24324f;
                border-radius: 6px;
                padding: 4px 8px;
                min-width: 34px;
                min-height: 24px;
            }
            QCalendarWidget QMenu {
                background-color: #0b1020;
                color: #dbeafe;
                border: 1px solid #24324f;
            }
            QCalendarWidget QSpinBox {
                min-width: 72px;
                min-height: 24px;
                color: #dbeafe;
                background-color: #111827;
                border: 1px solid #24324f;
            }
            QCalendarWidget QAbstractItemView {
                min-width: 330px;
                min-height: 210px;
                font-size: 12px;
                color: #dbeafe;
                background-color: #0b1020;
                selection-background-color: #1d4ed8;
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
        edit.setMinimumWidth(108)
        edit.setMinimumHeight(42)
        edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        edit.setCalendarWidget(self._make_calendar())
        return edit

    def _range_row(self, left: QWidget, right: QWidget, left_text: str = "с", right_text: str = "по") -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        left_label = QLabel(left_text)
        right_label = QLabel(right_text)
        left_label.setFixedWidth(14)
        right_label.setFixedWidth(22)
        left.setMinimumHeight(max(left.minimumHeight(), 42))
        right.setMinimumHeight(max(right.minimumHeight(), 42))
        lay.addWidget(left_label)
        lay.addWidget(left, 1)
        lay.addWidget(right_label)
        lay.addWidget(right, 1)
        row.setMinimumHeight(50)
        row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return row

    def _add_row(
        self,
        grid: QGridLayout,
        row: int,
        col: int,
        label: str,
        widget: QWidget,
        key: str = "",
    ) -> None:
        lbl = QLabel(label)
        lbl.setObjectName("FilterLabel")
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl.setMinimumHeight(20)
        widget.setMinimumHeight(max(widget.minimumHeight(), 42))
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        field = QWidget()
        field.setObjectName("FilterField")
        field_lay = QVBoxLayout(field)
        field_lay.setContentsMargins(0, 18, 0, 28)
        field_lay.setSpacing(12)
        field_lay.addWidget(lbl)
        field_lay.addWidget(widget)
        field.setMinimumHeight(max(124, lbl.minimumHeight() + widget.minimumHeight() + 62))
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        actual_row = getattr(self, "_next_filter_row", 0)
        self._next_filter_row = actual_row + 1
        grid.addWidget(field, actual_row, 0, 1, 2)
        if key:
            self._filter_rows[key] = (lbl, field, actual_row, 0)
            self._filter_controls[key] = widget

    def _quick_filter_box(self, label: str, widget: QWidget, min_width: int = 120) -> QWidget:
        box = QWidget()
        box.setObjectName("QuickFilterBox")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lbl = QLabel(label)
        lbl.setObjectName("QuickFilterLabel")
        widget.setMinimumWidth(min_width)
        widget.setMinimumHeight(38)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay.addWidget(lbl)
        lay.addWidget(widget)
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return box

    def _build_ui(self) -> None:
        body_layout = QVBoxLayout(self)
        body_layout.setContentsMargins(14, 12, 14, 12)
        body_layout.setSpacing(10)

        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(0, 0, 0, 0)
        quick_row.setSpacing(8)
        quick_lbl = QLabel("Быстрый поиск:")
        quick_lbl.setObjectName("SidebarTitle")
        self.ed_quick_search = self._make_line("Введите текст для поиска по всем полям")
        self.ed_quick_search.setMinimumWidth(520)
        self.ed_quick_search.setMinimumHeight(38)
        quick_row.addWidget(quick_lbl)
        quick_row.addWidget(self.ed_quick_search, 1)
        self.btn_toggle_extra = QToolButton()
        self.btn_toggle_extra.setObjectName("MoreFiltersButton")
        self.btn_toggle_extra.setText("Еще фильтры ▸")
        self.btn_toggle_extra.setCheckable(True)
        self.btn_toggle_extra.setChecked(False)
        self.btn_toggle_extra.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.btn_toggle_extra.setArrowType(Qt.NoArrow)
        quick_row.addWidget(self.btn_toggle_extra)
        self.btn_search = QPushButton("Искать")
        self.btn_search.setObjectName("Primary")
        self.btn_search.setMinimumWidth(110)
        self.btn_search.setMinimumHeight(38)
        quick_row.addWidget(self.btn_search)
        self.btn_reset = QPushButton("Сбросить")
        self.btn_reset.setMinimumHeight(38)
        quick_row.addWidget(self.btn_reset)
        body_layout.addLayout(quick_row)

        filters_row = QHBoxLayout()
        filters_row.setContentsMargins(0, 0, 0, 0)
        filters_row.setSpacing(8)
        self.cb_quick_trend = QuickMultiSelect(list(PROCEDURE_TYPE_OPTIONS))
        filters_row.addWidget(self._quick_filter_box("Тип процедуры", self.cb_quick_trend, 150))
        self.cb_quick_status = QuickMultiSelect(list(STATUS_OPTIONS))
        filters_row.addWidget(self._quick_filter_box("Статус", self.cb_quick_status, 130))
        self.cb_quick_law = QuickMultiSelect([("44-ФЗ", "44"), ("223-ФЗ", "223")])
        filters_row.addWidget(self._quick_filter_box("Закон", self.cb_quick_law, 110))
        self.cb_quick_published = self._make_combo([("Любая", ""), ("Сегодня", "today"), ("За неделю", "week")])
        self.cb_quick_published.setObjectName("QuickFilterCombo")
        filters_row.addWidget(self._quick_filter_box("Дата публикации", self.cb_quick_published, 140))
        self.cb_browser = ChevronComboBox()
        self.cb_browser.setObjectName("QuickFilterCombo")
        self.cb_browser.setMinimumWidth(180)
        self.cb_browser.setMinimumHeight(38)
        self.cb_browser.setIconSize(QSize(20, 20))
        self._browsers = available_browsers()
        for browser in self._browsers:
            icon = QIcon(str(asset_path(f"{browser.key}.png")))
            self.cb_browser.addItem(icon, browser.label, browser)
        filters_row.addWidget(self._quick_filter_box("Браузер", self.cb_browser, 180))
        filters_row.addStretch(1)
        body_layout.addLayout(filters_row)

        keyword_row = QHBoxLayout()
        keyword_row.setContentsMargins(0, 0, 0, 0)
        keyword_row.setSpacing(8)
        self.cb_keyword_search = QCheckBox("Поиск по ключевым словам")
        self.cb_keyword_search.setToolTip(
            "Искать процедуры, где встречается хотя бы одно слово из списка"
        )
        keyword_row.addWidget(self.cb_keyword_search)
        self.btn_edit_keywords = QPushButton("Редактировать список")
        keyword_row.addWidget(self.btn_edit_keywords)
        self.lbl_keywords_count = QLabel()
        keyword_row.addWidget(self.lbl_keywords_count)
        keyword_row.addStretch(1)
        body_layout.addLayout(keyword_row)
        self.refresh_keywords_count()

        self.active_filters_row = QHBoxLayout()
        self.active_filters_row.setContentsMargins(0, 0, 0, 0)
        self.active_filters_row.setSpacing(6)
        self.active_filters_label = QLabel("Активные фильтры:")
        self.active_filters_label.setObjectName("QuickFilterLabel")
        self.active_filters_row.addWidget(self.active_filters_label)
        self.active_filters_row.addStretch(1)
        body_layout.addLayout(self.active_filters_row)
        self._refresh_quick_filter_chips()

        self.extra_scroll = QScrollArea()
        self.extra_scroll.setObjectName("ExtraFiltersPanel")
        self.extra_scroll.setVisible(False)
        self.extra_scroll.setWidgetResizable(True)
        self.extra_scroll.setFrameShape(QScrollArea.NoFrame)
        self.extra_scroll.setMinimumWidth(390)
        self.extra_scroll.setMaximumWidth(430)

        self.extra_filters = QWidget()
        self.extra_filters.setObjectName("ExtraFiltersBody")
        self.extra_filters.setMinimumHeight(680)
        extra_layout = QVBoxLayout(self.extra_filters)
        extra_layout.setContentsMargins(14, 12, 14, 14)
        extra_layout.setSpacing(12)

        extra_header = QHBoxLayout()
        extra_header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Дополнительные фильтры")
        title.setObjectName("ExtraFiltersTitle")
        extra_header.addWidget(title)
        extra_header.addStretch(1)
        btn_close_extra = QToolButton()
        btn_close_extra.setObjectName("ExtraFiltersClose")
        btn_close_extra.setText("×")
        btn_close_extra.clicked.connect(lambda: self.btn_toggle_extra.setChecked(False))
        extra_header.addWidget(btn_close_extra)
        extra_layout.addLayout(extra_header)

        grid = QGridLayout()
        self._filter_grid = grid
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(18)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for r in range(40):
            grid.setRowMinimumHeight(r, 0)

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
            list(PROCEDURE_TYPE_OPTIONS)
        )
        self.status_selector = StatusMultiSelect(STATUS_OPTIONS)
        self.lst_steps = self.status_selector.list_widget
        self.cb_purchase_form = self._make_combo(
            [("Любая", ""), ("Электронная", "электрон"), ("Бумажная", "бумаж")]
        )
        self.sb_lots_min = self._make_int()
        self.sb_lots_max = self._make_int()

        self.ed_date_from = self._make_date(QDate.currentDate().addYears(-1))
        self.ed_date_to = self._make_date(QDate.currentDate())
        self.de_end_from = self._make_date(QDate.currentDate())
        self.de_end_to = self._make_date(QDate.currentDate().addMonths(3))
        self.cb_published_enabled = QCheckBox()
        self.cb_published_enabled.setToolTip("Включить фильтр по дате публикации")
        self.cb_end_enabled = QCheckBox()
        self.cb_end_enabled.setToolTip("Включить фильтр по окончанию приёма заявок")
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
        self.ed_tag_id = NoWheelSpinBox()
        self.ed_tag_id.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.ed_tag_id.setRange(0, 1_000_000)
        self.ed_tag_id.setSpecialValueText("— любой —")
        self.ed_tag_id.setMinimumHeight(36)
        self.ed_tag_id.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.sb_apc_min = self._make_int()
        self.sb_apc_max = self._make_int()

        self._add_row(grid, 0, 0, "Номер закупки:", self.ed_registry, "registry")
        self._add_row(grid, 1, 0, "Уникальный номер закупки:", self.ed_unique_number, "unique_number")
        self._add_row(grid, 2, 0, "Наименование закупки:", self.ed_title_local, "title")
        self._add_row(grid, 3, 0, "ОКПД2:", self.ed_okpd2, "okpd2")
        self._add_row(grid, 4, 0, "ОКВЭД2:", self.ed_okved2, "okved2")
        self._add_row(grid, 5, 0, "Организатор процедуры:", self.ed_organizer, "organizer")
        self._add_row(grid, 6, 0, "Заказчик:", self.ed_customer, "customer")
        self._add_row(grid, 7, 0, "Регион заказчика:", self.ed_customer_region, "customer_region")
        self._add_row(grid, 8, 0, "Агент заказчика:", self.ed_customer_agent, "customer_agent")

        self._add_row(
            grid,
            0,
            1,
            "Обеспечение заявки:",
            self._range_row(self.sb_guarantee_min, self.sb_guarantee_max),
            "guarantee",
        )
        self._add_row(grid, 1, 1, "Ответственное лицо:", self.ed_responsible, "responsible")
        self._add_row(grid, 2, 1, "Тип процедуры:", self.cb_trend, "trend")
        self._add_row(grid, 3, 1, "Статус процедуры:", self.status_selector, "status")
        self._add_row(grid, 4, 1, "Форма закупки:", self.cb_purchase_form, "purchase_form")
        self._add_row(
            grid,
            5,
            1,
            "Лотов в закупке:",
            self._range_row(self.sb_lots_min, self.sb_lots_max),
            "lots",
        )
        self._add_row(
            grid,
            6,
            1,
            "Намерений:",
            self._range_row(self.sb_apc_min, self.sb_apc_max),
            "applics",
        )
        self._add_row(grid, 7, 1, "Тег:", self.ed_tag_id, "tag")

        published_row = QWidget()
        published_lay = QHBoxLayout(published_row)
        published_lay.setContentsMargins(0, 2, 0, 2)
        published_lay.setSpacing(8)
        published_lay.addWidget(self.cb_published_enabled)
        published_from_label = QLabel("с")
        published_to_label = QLabel("по")
        published_from_label.setFixedWidth(14)
        published_to_label.setFixedWidth(22)
        published_lay.addWidget(published_from_label)
        published_lay.addWidget(self.ed_date_from, 1)
        published_lay.addWidget(published_to_label)
        published_lay.addWidget(self.ed_date_to, 1)
        published_row.setMinimumHeight(50)
        published_row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._add_row(grid, 9, 0, "Дата публикации:", published_row, "published")

        end_row = QWidget()
        end_lay = QHBoxLayout(end_row)
        end_lay.setContentsMargins(0, 2, 0, 2)
        end_lay.setSpacing(8)
        end_lay.addWidget(self.cb_end_enabled)
        end_from_label = QLabel("с")
        end_to_label = QLabel("по")
        end_from_label.setFixedWidth(14)
        end_to_label.setFixedWidth(22)
        end_lay.addWidget(end_from_label)
        end_lay.addWidget(self.de_end_from, 1)
        end_lay.addWidget(end_to_label)
        end_lay.addWidget(self.de_end_to, 1)
        end_row.setMinimumHeight(50)
        end_row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._add_row(grid, 10, 0, "Окончание приема заявок:", end_row, "end")
        results_row = QWidget()
        results_lay = QHBoxLayout(results_row)
        results_lay.setContentsMargins(0, 0, 0, 0)
        results_lay.setSpacing(8)
        results_lay.addWidget(self.cb_results_enabled)
        results_from_label = QLabel("с")
        results_to_label = QLabel("по")
        results_from_label.setFixedWidth(14)
        results_to_label.setFixedWidth(22)
        results_lay.addWidget(results_from_label)
        results_lay.addWidget(self.de_results_from, 1)
        results_lay.addWidget(results_to_label)
        results_lay.addWidget(self.de_results_to, 1)
        results_row.setMinimumHeight(50)
        results_row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._add_row(grid, 8, 1, "Дата подведения итогов:", results_row, "results")
        self._add_row(
            grid,
            9,
            1,
            "Начальная цена (с НДС):",
            self._range_row(self.sb_price_min, self.sb_price_max, "от", "до"),
            "price",
        )
        self._add_row(grid, 10, 1, "Специальные признаки:", self.ed_special_features, "special_features")
        self._add_row(grid, 11, 0, "Наименование позиции:", self.ed_position_name, "position_name")
        self._add_row(grid, 11, 1, "Национальный режим закупок:", self.ed_national_regime, "national_regime")

        extra_layout.addLayout(grid)
        self.extra_scroll.setWidget(self.extra_filters)
        body_layout.addWidget(self.extra_scroll)

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
        for w in (self.cb_trend, self.cb_purchase_form):
            w.currentIndexChanged.connect(lambda *_: self.clientFiltersChanged.emit())
        self.cb_quick_trend.selectionChanged.connect(self._sync_quick_trend)
        self.cb_quick_status.selectionChanged.connect(self._sync_quick_status)
        self.cb_quick_law.selectionChanged.connect(self._sync_quick_law)
        self.cb_quick_published.currentIndexChanged.connect(self._sync_quick_published)
        self.lst_steps.itemChanged.connect(lambda *_: self.clientFiltersChanged.emit())
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
        for w in (self.cb_published_enabled, self.cb_end_enabled, self.cb_results_enabled):
            w.toggled.connect(lambda *_: self.clientFiltersChanged.emit())
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
        self.cb_keyword_search.toggled.connect(lambda *_: self.clientFiltersChanged.emit())
        self.btn_edit_keywords.clicked.connect(self.editKeywordsRequested)

    def _set_extra_visible(self, visible: bool) -> None:
        self.extra_scroll.setVisible(visible)
        self.setMinimumHeight(88)
        self.btn_toggle_extra.setArrowType(Qt.NoArrow)
        self.btn_toggle_extra.setText("Скрыть фильтры ▴" if visible else "Еще фильтры ▸")
        self.updateGeometry()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def _sync_quick_trend(self) -> None:
        values = self.cb_quick_trend.selected_values()
        idx = self.cb_trend.findData(values[0]) if len(values) == 1 else -1
        self.cb_trend.setCurrentIndex(idx if idx >= 0 else 0)
        self._refresh_quick_filter_chips()
        self.clientFiltersChanged.emit()

    def _sync_quick_status(self) -> None:
        values = set(self.cb_quick_status.selected_values())
        self.lst_steps.blockSignals(True)
        for i in range(self.lst_steps.count()):
            item = self.lst_steps.item(i)
            item.setCheckState(Qt.Checked if str(item.data(Qt.UserRole) or "") in values else Qt.Unchecked)
        self.lst_steps.blockSignals(False)
        self.status_selector._update_button_text()
        self._refresh_quick_filter_chips()
        self.clientFiltersChanged.emit()

    def _sync_quick_law(self) -> None:
        self._refresh_quick_filter_chips()
        self.clientFiltersChanged.emit()

    def _sync_quick_published(self) -> None:
        mode = str(self.cb_quick_published.currentData() or "")
        today = QDate.currentDate()
        if mode == "today":
            self.cb_published_enabled.setChecked(True)
            self.ed_date_from.setDate(today)
            self.ed_date_to.setDate(today)
        elif mode == "week":
            self.cb_published_enabled.setChecked(True)
            self.ed_date_from.setDate(today.addDays(-7))
            self.ed_date_to.setDate(today)
        else:
            self.cb_published_enabled.setChecked(False)
        self.clientFiltersChanged.emit()

    def _refresh_quick_filter_chips(self) -> None:
        if not hasattr(self, "active_filters_row"):
            return
        while self.active_filters_row.count() > 1:
            item = self.active_filters_row.takeAt(1)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        chips: list[tuple[str, str, str]] = []
        chips.extend(("Тип", label, value) for label, value in self.cb_quick_trend.selected_label_values())
        chips.extend(("Статус", label, value) for label, value in self.cb_quick_status.selected_label_values())
        chips.extend(("Закон", label, value) for label, value in self.cb_quick_law.selected_label_values())

        self.active_filters_label.setVisible(bool(chips))
        insert_index = 1
        for group, label, value in chips:
            chip = QToolButton()
            chip.setObjectName("ActiveFilterChip")
            chip.setText(f"{group}: {label} ×")
            chip.setToolButtonStyle(Qt.ToolButtonTextOnly)
            if group == "Тип":
                chip.clicked.connect(lambda checked=False, v=value: self.cb_quick_trend.unselect_value(v))
            elif group == "Статус":
                chip.clicked.connect(lambda checked=False, v=value: self.cb_quick_status.unselect_value(v))
            else:
                chip.clicked.connect(lambda checked=False, v=value: self.cb_quick_law.unselect_value(v))
            self.active_filters_row.insertWidget(insert_index, chip)
            insert_index += 1
        self.active_filters_row.addStretch(1)

    def _expanded_min_height(self) -> int:
        return 360 if self._platform_key == "roseltorg" else 560

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("SidebarSection")
        return lbl

    def set_platform_filter_options(
        self,
        procedure_type_options: Sequence[tuple[str, str]],
        status_options: Sequence[str | tuple[str, str]],
        search_by_options: Optional[Sequence[tuple[str, str]]] = None,
        platform_key: str = "gpb",
    ) -> None:
        self._platform_key = platform_key
        current_trend = self.cb_trend.currentData()
        self.cb_trend.blockSignals(True)
        self.cb_trend.clear()
        self.cb_trend.addItem("Все", "")
        for label, value in procedure_type_options:
            self.cb_trend.addItem(label, value)
        trend_idx = self.cb_trend.findData(current_trend)
        self.cb_trend.setCurrentIndex(trend_idx if trend_idx >= 0 else 0)
        self.cb_trend.blockSignals(False)
        self.cb_quick_trend.blockSignals(True)
        self.cb_quick_trend.set_options(procedure_type_options)
        self.cb_quick_trend.blockSignals(False)

        self.status_selector.set_options(status_options)
        self.cb_quick_status.blockSignals(True)
        self.cb_quick_status.set_options(status_options)
        self.cb_quick_status.blockSignals(False)
        self._sync_quick_trend()
        self._sync_quick_status()

        current_search_by = self.cb_purchase_form.currentData()
        self.cb_purchase_form.blockSignals(True)
        self.cb_purchase_form.clear()
        if search_by_options:
            for label, value in search_by_options:
                self.cb_purchase_form.addItem(label, value)
        else:
            self.cb_purchase_form.addItem("Любая", "")
            self.cb_purchase_form.addItem("Электронная", "электрон")
            self.cb_purchase_form.addItem("Бумажная", "бумаж")
        search_by_idx = self.cb_purchase_form.findData(current_search_by)
        self.cb_purchase_form.setCurrentIndex(search_by_idx if search_by_idx >= 0 else 0)
        self.cb_purchase_form.blockSignals(False)
        self._apply_platform_filter_visibility()

    def _set_row_visible(self, key: str, visible: bool) -> None:
        row = self._filter_rows.get(key)
        if row is None:
            return
        label, field, _, _ = row
        label.setVisible(visible)
        field.setVisible(visible)

    def _set_row_label(self, key: str, text: str) -> None:
        row = self._filter_rows.get(key)
        if row is None:
            return
        row[0].setText(text)

    def _place_row(self, key: str, row: int, col: int, visible: bool = True) -> None:
        stored = self._filter_rows.get(key)
        if stored is None:
            return
        label, field, _, _ = stored
        self._filter_grid.removeWidget(field)
        actual_row = row * 2 + col
        self._filter_grid.addWidget(field, actual_row, 0, 1, 2)
        label.setVisible(visible)
        field.setVisible(visible)

    def _restore_row_position(self, key: str) -> None:
        stored = self._filter_rows.get(key)
        if stored is None:
            return
        label, field, row, _ = stored
        self._filter_grid.removeWidget(field)
        self._filter_grid.addWidget(field, row, 0, 1, 2)
        label.setVisible(True)
        field.setVisible(True)

    def _apply_platform_filter_visibility(self) -> None:
        if self._platform_key == "roseltorg":
            visible_keys = {"trend", "purchase_form", "status", "organizer", "price", "published", "end", "results"}
            self._set_row_label("trend", "Процедуры по:")
            self._set_row_label("purchase_form", "Отображать по:")
            self._set_row_label("organizer", "Организатор:")
            self._set_row_label("price", "Цена:")
            self._set_row_label("published", "Дата публикации:")
            self._set_row_label("end", "Дата окончания приема предложений:")
            self._set_row_label("results", "Дата выбора победителя:")
            for row in range(40):
                self._filter_grid.setRowMinimumHeight(row, 0)
            for key in self._filter_rows:
                self._set_row_visible(key, False)
            for key, row, col in (
                ("trend", 0, 0),
                ("purchase_form", 0, 1),
                ("status", 1, 0),
                ("organizer", 1, 1),
                ("published", 2, 0),
                ("results", 2, 1),
                ("end", 3, 0),
                ("price", 3, 1),
            ):
                self._place_row(key, row, col, key in visible_keys)
            self.extra_filters.setMinimumHeight(980)
            self.extra_scroll.setMinimumHeight(0)
            self.extra_scroll.setMaximumHeight(16777215)
        else:
            visible_keys = set(self._filter_rows)
            self._set_row_label("trend", "Тип процедуры:")
            self._set_row_label("purchase_form", "Форма закупки:")
            self._set_row_label("organizer", "Организатор процедуры:")
            self._set_row_label("price", "Начальная цена (с НДС):")
            self._set_row_label("published", "Дата публикации:")
            self._set_row_label("end", "Окончание приема заявок:")
            self._set_row_label("results", "Дата подведения итогов:")
            for row in range(40):
                self._filter_grid.setRowMinimumHeight(row, 0)
            for key in self._filter_rows:
                self._restore_row_position(key)
            self.extra_filters.setMinimumHeight(2600)
            self.extra_scroll.setMinimumHeight(0)
            self.extra_scroll.setMaximumHeight(16777215)
        for key in self._filter_rows:
            self._set_row_visible(key, key in visible_keys)
        if self.extra_scroll.isVisible():
            self.setMinimumHeight(self._expanded_min_height())
        self.extra_filters.updateGeometry()
        self.extra_scroll.updateGeometry()
        self.updateGeometry()

    def search_params(self) -> SearchParams:
        use_extra = self.extra_scroll.isVisible()
        return SearchParams(
            date_from=(
                self.ed_date_from.date().toString("dd.MM.yyyy")
                if use_extra and self.cb_published_enabled.isChecked() else ""
            ),
            date_to=(
                self.ed_date_to.date().toString("dd.MM.yyyy")
                if use_extra and self.cb_published_enabled.isChecked() else ""
            ),
            query="",
            tag_id=(self.ed_tag_id.value() or None) if use_extra else None,
            limit=DEFAULT_REQUEST_LIMIT,
        )

    def client_filters(self) -> ClientFilters:
        keywords = tuple(load_keywords()) if self.cb_keyword_search.isChecked() else ()
        quick_trends = self.cb_quick_trend.selected_values()
        quick_laws = self.cb_quick_law.selected_values()
        if self._platform_key == "roseltorg":
            return ClientFilters(
                quick_search=self.ed_quick_search.text().strip(),
                keyword_search_enabled=self.cb_keyword_search.isChecked(),
                keywords=keywords,
                organizer_contains=self.ed_organizer.text().strip(),
                trend_pur=quick_trends[0] if len(quick_trends) == 1 else (self.cb_trend.currentData() or ""),
                trend_pur_values=quick_trends,
                step_ids=tuple(
                    str(self.lst_steps.item(i).data(Qt.UserRole) or "")
                    for i in range(self.lst_steps.count())
                    if self.lst_steps.item(i).checkState() == Qt.Checked
                ),
                law_values=quick_laws,
                purchase_form=self.cb_purchase_form.currentData() or "",
                price_min=(self.sb_price_min.value() or None),
                price_max=(self.sb_price_max.value() or None),
                published_from=(
                    datetime.combine(self.ed_date_from.date().toPython(), datetime.min.time())
                    if self.cb_published_enabled.isChecked() else None
                ),
                published_to=(
                    datetime.combine(self.ed_date_to.date().toPython(), datetime.max.time())
                    if self.cb_published_enabled.isChecked() else None
                ),
                end_from=(
                    datetime.combine(self.de_end_from.date().toPython(), datetime.min.time())
                    if self.cb_end_enabled.isChecked() else None
                ),
                end_to=(
                    datetime.combine(self.de_end_to.date().toPython(), datetime.max.time())
                    if self.cb_end_enabled.isChecked() else None
                ),
                results_from=(
                    datetime.combine(self.de_results_from.date().toPython(), datetime.min.time())
                    if self.cb_results_enabled.isChecked() else None
                ),
                results_to=(
                    datetime.combine(self.de_results_to.date().toPython(), datetime.max.time())
                    if self.cb_results_enabled.isChecked() else None
                ),
            )
        return ClientFilters(
            quick_search=self.ed_quick_search.text().strip(),
            keyword_search_enabled=self.cb_keyword_search.isChecked(),
            keywords=keywords,
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
            trend_pur=quick_trends[0] if len(quick_trends) == 1 else (self.cb_trend.currentData() or ""),
            trend_pur_values=quick_trends,
            step_ids=tuple(
                str(self.lst_steps.item(i).data(Qt.UserRole) or "")
                for i in range(self.lst_steps.count())
                if self.lst_steps.item(i).checkState() == Qt.Checked
            ),
            law_values=quick_laws,
            purchase_form=self.cb_purchase_form.currentData() or "",
            applics_min=(self.sb_apc_min.value() or None),
            applics_max=(self.sb_apc_max.value() or None),
            lots_min=(self.sb_lots_min.value() or None),
            lots_max=(self.sb_lots_max.value() or None),
            price_min=(self.sb_price_min.value() or None),
            price_max=(self.sb_price_max.value() or None),
            published_from=(
                datetime.combine(self.ed_date_from.date().toPython(), datetime.min.time())
                if self.cb_published_enabled.isChecked() else None
            ),
            published_to=(
                datetime.combine(self.ed_date_to.date().toPython(), datetime.max.time())
                if self.cb_published_enabled.isChecked() else None
            ),
            end_from=(
                datetime.combine(self.de_end_from.date().toPython(), datetime.min.time())
                if self.cb_end_enabled.isChecked() else None
            ),
            end_to=(
                datetime.combine(self.de_end_to.date().toPython(), datetime.max.time())
                if self.cb_end_enabled.isChecked() else None
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
        self.cb_keyword_search.setChecked(False)
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
        self.cb_quick_trend.clear_selection()
        self.cb_quick_status.clear_selection()
        self.cb_quick_law.clear_selection()
        self.cb_quick_published.setCurrentIndex(0)
        self.cb_trend.setCurrentIndex(0)
        for i in range(self.lst_steps.count()):
            self.lst_steps.item(i).setCheckState(Qt.Unchecked)
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
        self.cb_published_enabled.setChecked(False)
        self.cb_end_enabled.setChecked(False)
        self.ed_date_from.setDate(QDate.currentDate().addYears(-1))
        self.ed_date_to.setDate(QDate.currentDate())
        self.de_end_from.setDate(QDate.currentDate())
        self.de_end_to.setDate(QDate.currentDate().addMonths(3))
        self.de_results_from.setDate(QDate.currentDate())
        self.de_results_to.setDate(QDate.currentDate().addMonths(3))
        self.cb_results_enabled.setChecked(False)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.btn_search.setEnabled(enabled)

    def refresh_keywords_count(self) -> None:
        items = load_keyword_items()
        active = sum(1 for enabled, _ in items if enabled)
        self.lbl_keywords_count.setText(f"Активных слов: {active}/{len(items)}")

    def selected_browser(self) -> BrowserConfig:
        browser = self.cb_browser.currentData()
        if isinstance(browser, BrowserConfig):
            return browser
        return self._browsers[0]
