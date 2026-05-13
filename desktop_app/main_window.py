from __future__ import annotations

import json
import re
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QEvent, QModelIndex, QObject, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from etp_client import EtpClient
from tektorg_rn_client import TektorgRnClient

from .constants import (
    ANALYSIS_DIR,
    APP_TITLE,
    CACHE_FILE,
    COLUMNS,
    DOCUMENTS_DIR,
    KEYWORDS_FILE,
    LM_STUDIO_BASE_URL,
    LM_STUDIO_MODEL,
    TENDER_DOCUMENTS_ROOT,
    VIEW_URL,
)
from .document_sorter import sort_filled_documents
from .lm_table_analysis import ANALYSIS_TABLE_HEADERS_RU
from .keywords import load_keyword_items, parse_keyword_items, save_keyword_items
from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import ClientFilters, SearchParams
from .sidebar import Sidebar
from .utils import fmt_date, parse_dt
from .worker import (
    TaskRunner,
    make_analyze_procedure_task,
    make_download_documents_task,
    make_search_task,
    make_tektorg_row_documents_task,
    make_tektorg_technical_upload_task,
)

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)

        self.client = TektorgRnClient()
        self.runner = TaskRunner(self)
        self.row_download_runner = TaskRunner(self)
        self.technical_upload_runner = TaskRunner(self)
        self.model = ProcedureTableModel(self)
        self.proxy = ProcedureFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self._last_total: int = 0
        self._current_start: int = 0
        self._last_user: Optional[str] = None
        self._cache_dirty: bool = False
        self._cache_save_enabled: bool = True
        self._platform_key: str = "tektorg_rn"
        self._auth_dialog_open: bool = False

        self._cache_save_timer = QTimer(self)
        self._cache_save_timer.setSingleShot(True)
        self._cache_save_timer.timeout.connect(self._save_cache_now)
        self._analysis_sink: dict[str, Any] = {}
        self._row_download_sink: dict[str, Any] = {}
        self._upload_timing_sink: dict[str, Any] = {}

        self._build_ui()
        self._set_platform_buttons()
        self._announce_cache_on_start()
        self._update_controls()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # ---------- Верхняя панель ----------
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(64)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(16, 8, 16, 8)
        top_layout.setSpacing(12)

        platform_switcher = QFrame()
        platform_switcher.setObjectName("PlatformSwitcher")
        platform_layout = QHBoxLayout(platform_switcher)
        platform_layout.setContentsMargins(2, 2, 2, 2)
        platform_layout.setSpacing(2)
        self.platform_group = QButtonGroup(self)
        self.platform_group.setExclusive(True)
        self.btn_platform_gpb = QPushButton("ЭТП ГПБ")
        self.btn_platform_gpb.setObjectName("PlatformButton")
        self.btn_platform_gpb.setCheckable(True)
        self.btn_platform_gpb.setChecked(False)
        self.btn_platform_roseltorg = QPushButton("ТЭК-Торг РН")
        self.btn_platform_roseltorg.setObjectName("PlatformButton")
        self.btn_platform_roseltorg.setCheckable(True)
        self.btn_platform_roseltorg.setChecked(True)
        self.platform_group.addButton(self.btn_platform_gpb)
        self.platform_group.addButton(self.btn_platform_roseltorg)
        platform_layout.addWidget(self.btn_platform_gpb)
        platform_layout.addWidget(self.btn_platform_roseltorg)
        self.btn_platform_gpb.clicked.connect(lambda: self._select_platform("gpb"))
        self.btn_platform_roseltorg.clicked.connect(lambda: self._select_platform("tektorg_rn"))
        platform_switcher.setVisible(False)
        top_layout.addWidget(platform_switcher)

        t_title_box = QVBoxLayout()
        t_title_box.setSpacing(0)
        self.title_label = QLabel("Подача заявок на тендер")
        self.title_label.setObjectName("TopBarTitle")
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("TopBarSubtitle")
        self.subtitle_label.setVisible(False)
        t_title_box.addWidget(self.title_label)
        t_title_box.addWidget(self.subtitle_label)
        top_layout.addLayout(t_title_box)
        top_layout.addStretch(1)

        self.user_label = QLabel("Пользователь: —")
        self.user_label.setStyleSheet("color: #4a515a;")
        top_layout.addWidget(self.user_label)

        self.session_badge = QLabel("○  Браузер не запущен")
        self.session_badge.setObjectName("SessionBadge")
        self.session_badge.setProperty("ok", "idle")
        top_layout.addWidget(self.session_badge)

        # ---------- Фильтры и основная область ----------
        self.sidebar = Sidebar()
        self.sidebar.searchRequested.connect(self._on_search)
        self.sidebar.resetRequested.connect(self._on_reset_filters)
        self.sidebar.clientFiltersChanged.connect(self._on_filters_changed)
        self.sidebar.editKeywordsRequested.connect(self._on_edit_keywords)

        main_area = QWidget()
        main_area.setMinimumHeight(360)
        main_area_layout = QVBoxLayout(main_area)
        main_area_layout.setContentsMargins(12, 10, 12, 8)
        main_area_layout.setSpacing(8)

        # Верхняя полоска со счётчиком
        actions = QFrame()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)

        self.lbl_counter = QLabel("Данных нет. Нажмите «Поиск».")
        self.lbl_counter.setStyleSheet("color: #3a4048; font-weight: 600;")
        actions_layout.addWidget(self.lbl_counter)
        actions_layout.addStretch(1)

        self.btn_export = QPushButton("Экспорт в XLSX…")
        self.btn_export.clicked.connect(self._on_export)
        actions_layout.addWidget(self.btn_export)

        main_area_layout.addWidget(actions)

        # Таблица
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(3, Qt.AscendingOrder)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # Иначе layout раздувает окно по широкому sizeHint таблицы; скролл — только внутри QTableView.
        self.table.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Expanding,
        )
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setCascadingSectionResizes(False)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.clicked.connect(self._on_row_clicked)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        self.proxy.modelReset.connect(self._apply_table_column_widths)
        self.table.viewport().installEventFilter(self)
        self._apply_table_column_widths()
        main_area_layout.addWidget(self.table, 1)

        bottom_bar = QFrame()
        bottom_bar.setObjectName("BottomBar")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(10, 8, 10, 8)
        bottom_layout.setSpacing(8)

        self.btn_load_more = QPushButton("Следующий батч")
        self.btn_load_more.setToolTip("Загрузить следующий пакет данных")
        self.btn_load_more.clicked.connect(self._on_load_more)
        self.btn_load_more.setEnabled(False)
        bottom_layout.addWidget(self.btn_load_more)

        self.btn_load_all = QPushButton("Загрузить все батчи")
        self.btn_load_all.setToolTip("Подряд скачать все оставшиеся пачки")
        self.btn_load_all.clicked.connect(self._on_load_all)
        self.btn_load_all.setEnabled(False)
        bottom_layout.addWidget(self.btn_load_all)

        self.btn_download_docs = QPushButton("Скачать документы")
        self.btn_download_docs.setToolTip("Скачать документацию выбранных процедур")
        self.btn_download_docs.clicked.connect(self._on_download_documents)
        self.btn_download_docs.setEnabled(False)
        bottom_layout.addWidget(self.btn_download_docs)

        self.btn_analyze = QPushButton("Проанализировать")
        self.btn_analyze.setToolTip(
            "Собрать текст карточки с ЭТП ГПБ и отправить в LM Studio для заполнения таблицы анализа"
        )
        self.btn_analyze.clicked.connect(self._on_analyze_procedures)
        self.btn_analyze.setEnabled(False)
        bottom_layout.addWidget(self.btn_analyze)

        self.btn_stop = QPushButton("Стоп")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_stop.setEnabled(False)
        bottom_layout.addWidget(self.btn_stop)

        bottom_layout.addStretch(1)
        main_area_layout.addWidget(bottom_bar)

        # Центральный виджет: вся страница прокручивается при раскрытых фильтрах.
        page = QWidget()
        cl = QVBoxLayout(page)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(top)
        cl.addWidget(self.sidebar, 0)
        cl.addWidget(main_area, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.setCentralWidget(scroll)

        # Status bar
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status_msg = QLabel("Готов. Нажмите «Поиск» и войдите через ЭЦП при необходимости.")
        sb.addWidget(self.status_msg, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setRange(0, 0)
        self.progress.hide()
        sb.addPermanentWidget(self.progress)

        # Горячие клавиши
        act_search = QAction(self)
        act_search.setShortcut(QKeySequence("Ctrl+Return"))
        act_search.triggered.connect(self._on_search)
        self.addAction(act_search)

        act_focus_query = QAction(self)
        act_focus_query.setShortcut(QKeySequence("Ctrl+F"))
        act_focus_query.triggered.connect(lambda: self.sidebar.ed_query.setFocus())
        self.addAction(act_focus_query)

        QTimer.singleShot(0, self._apply_table_column_widths)

    def _apply_table_column_widths(self) -> None:
        """Колонки имеют базовую ширину, последняя заполняет свободное место."""
        self._apply_search_mode_column_visibility()
        hh = self.table.horizontalHeader()
        widths_by_key = {
            "registry_number": 150,
            "title": 300,
            "organizer": 150,
            "lots_count": 170,
            "date_end_registration": 170,
            "total_price": 170,
            "total_price_with_vat": 170,
            "step_label": 220,
        }
        visible_columns = [
            i for i in range(self.proxy.columnCount())
            if not self.table.isColumnHidden(i)
        ]
        last_visible = visible_columns[-1] if visible_columns else -1
        for i, (key, _) in enumerate(COLUMNS[: self.proxy.columnCount()]):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if i == last_visible
                else QHeaderView.ResizeMode.Interactive
            )
            hh.setSectionResizeMode(i, mode)
            hh.resizeSection(i, widths_by_key.get(key, 160))

    def _apply_search_mode_column_visibility(self) -> None:
        hidden_keys = (
            {"lots_count", "total_price_with_vat"}
            if self.sidebar.btn_search_by_number.isChecked()
            else set()
        )
        for i, (key, _) in enumerate(COLUMNS):
            self.table.setColumnHidden(i, key in hidden_keys)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.table.viewport() and event.type() == QEvent.Type.Wheel:
            wheel = event
            if wheel.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                bar = self.table.horizontalScrollBar()
                dy = wheel.angleDelta().y()
                dx = wheel.angleDelta().x()
                step = dx if dx != 0 else dy
                if step != 0:
                    bar.setValue(bar.value() - step)
                    return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------ задачи
    def _is_platform_ready(self) -> bool:
        return self._platform_key in {"gpb", "tektorg_rn"}

    def _platform_title(self) -> str:
        return "ТЭК-Торг РН" if self._platform_key == "tektorg_rn" else "ЭТП ГПБ"

    def _set_platform_buttons(self) -> None:
        self.btn_platform_gpb.setChecked(self._platform_key == "gpb")
        self.btn_platform_roseltorg.setChecked(self._platform_key == "tektorg_rn")

    def _apply_platform_ui(self) -> None:
        if self._platform_key == "tektorg_rn":
            self.title_label.setText("Подача заявок на тендер")
            self.subtitle_label.setText("")
            self.subtitle_label.setVisible(False)
            self.lbl_counter.setText("Данных нет. Нажмите «Поиск». Если сессии нет, войдите через ЭЦП.")
            self.user_label.setText("Пользователь: —")
            self._set_badge("idle", "○  ТЭК-Торг РН")
            self.status_msg.setText("Готов. Нажмите «Поиск» и войдите через ЭЦП при необходимости.")
        else:
            self.title_label.setText("ЭТП ГПБ — Актуальные процедуры")
            self.subtitle_label.setText("Поиск, фильтры и экспорт")
            if not self.model.rowCount():
                self.lbl_counter.setText("Данных нет. Нажмите «Поиск».")
            self._set_badge("idle", "○  Браузер не запущен")
            self.status_msg.setText("Готов. Нажмите «Поиск».")

    def _select_platform(self, key: str) -> None:
        if key not in {"gpb", "tektorg_rn"}:
            return
        if self.runner.is_running():
            self._set_platform_buttons()
            QMessageBox.information(
                self,
                "Идёт операция",
                "Дождитесь завершения текущей операции перед сменой площадки.",
            )
            return
        if key == self._platform_key:
            self._apply_platform_ui()
            self._update_controls()
            return

        if self._cache_dirty:
            self._save_cache_now()
        self._cache_save_timer.stop()
        self._cache_dirty = False
        try:
            self.client.close()
        except Exception:
            traceback.print_exc()

        self._platform_key = key
        self.client = TektorgRnClient() if key == "tektorg_rn" else EtpClient()
        self._set_platform_buttons()
        self.model.clear()
        self._last_total = 0
        self._current_start = 0
        self._last_user = None
        self._refresh_counter()
        self._apply_platform_ui()
        self._update_controls()

    def _ensure_platform_ready(self) -> bool:
        if self._is_platform_ready():
            return True
        QMessageBox.information(
            self,
            "Площадка в разработке",
            f"{self._platform_title()} пока добавлена только как переключатель. "
            "Поиск и загрузка документов будут подключены следующим этапом.",
        )
        return False

    def _apply_selected_browser(self) -> None:
        browser = self.sidebar.selected_browser()
        self.client.configure_browser(
            key=browser.key,
            label=browser.label,
            exe_path=browser.exe_path,
            user_data_dir=browser.user_data_dir,
            profile_dir=browser.profile_dir,
            port=browser.port,
        )

    def _on_search(self) -> None:
        if self.runner.is_running():
            return
        if not self._ensure_platform_ready():
            return

        self._apply_selected_browser()
        filters = self.sidebar.client_filters()
        if filters.keyword_search_enabled and not filters.keywords:
            QMessageBox.information(
                self,
                "Нет ключевых слов",
                "Список ключевых слов пуст. Добавьте слова через «Редактировать список».",
            )
            return
        self.proxy.set_filters(filters)
        self.model.set_keywords(filters.keywords)

        has_active_filters = self._has_active_filters(filters)
        if self._platform_key == "gpb" and CACHE_FILE.exists() and not has_active_filters:
            choice = self._ask_cache_choice()
            if choice == "cancel":
                return
            if choice == "cache":
                self._use_cache()
                return
            # choice == "refresh" → удаляем кэш и идём парсить заново
            self._delete_cache()

        self.model.clear()
        self._current_start = 0
        self._last_total = 0
        self._refresh_counter()
        self._start_task(
            self.sidebar.search_params(),
            start=0,
            batches=self._search_batches(filters),
            filters=filters,
        )

    def _on_edit_keywords(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Ключевые слова")
        dialog.resize(720, 560)

        layout = QVBoxLayout(dialog)
        hint = QLabel(
            "Отметьте галочками ключевые слова, по которым нужно искать. "
            "Поиск найдёт процедуры, где встречается хотя бы одно активное слово."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        keyword_list = QListWidget()
        keyword_list.setAlternatingRowColors(True)
        keyword_list.setSelectionMode(QListWidget.ExtendedSelection)
        try:
            keyword_items = load_keyword_items()
        except OSError as e:
            QMessageBox.critical(
                self,
                "Не удалось открыть список",
                "Не удалось открыть файл ключевых слов. "
                "Проверьте доступ к локальной папке приложения.\n\n"
                f"Путь: {KEYWORDS_FILE}\n\n"
                f"Подробности: {e}",
            )
            return
        for enabled, keyword in keyword_items:
            item = QListWidgetItem(keyword)
            item.setFlags(
                item.flags()
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsEditable
                | Qt.ItemIsEnabled
                | Qt.ItemIsSelectable
            )
            item.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
            keyword_list.addItem(item)
        layout.addWidget(keyword_list, 1)

        actions = QHBoxLayout()
        btn_all = QPushButton("Все")
        btn_none = QPushButton("Снять все")
        btn_add = QPushButton("Добавить")
        btn_remove = QPushButton("Удалить выбранные")
        actions.addWidget(btn_all)
        actions.addWidget(btn_none)
        actions.addStretch(1)
        actions.addWidget(btn_add)
        actions.addWidget(btn_remove)
        layout.addLayout(actions)

        def set_all(state: Qt.CheckState) -> None:
            for i in range(keyword_list.count()):
                keyword_list.item(i).setCheckState(state)

        def add_keyword() -> None:
            text, ok = QInputDialog.getText(
                dialog,
                "Добавить ключевое слово",
                "Ключевое слово или фраза:",
            )
            if not ok:
                return
            # parse_keywords() отбрасывает строки с [ ] (выключено) — для «Добавить»
            # нужна любая распознанная фраза; активность задаётся галочкой в списке.
            rows = parse_keyword_items(text.strip())
            if not rows:
                QMessageBox.information(
                    dialog,
                    "Не добавлено",
                    "Текст не принят: пустая строка, служебный фрагмент или слишком короткая фраза "
                    "(до 2 символов, если это не аббревиатура заглавными буквами).",
                )
                return
            _, keyword = rows[0]
            item = QListWidgetItem(keyword)
            item.setFlags(
                item.flags()
                | Qt.ItemIsUserCheckable
                | Qt.ItemIsEditable
                | Qt.ItemIsEnabled
                | Qt.ItemIsSelectable
            )
            item.setCheckState(Qt.Checked)
            keyword_list.addItem(item)

        def remove_selected() -> None:
            for item in keyword_list.selectedItems():
                keyword_list.takeItem(keyword_list.row(item))

        btn_all.clicked.connect(lambda: set_all(Qt.Checked))
        btn_none.clicked.connect(lambda: set_all(Qt.Unchecked))
        btn_add.clicked.connect(add_keyword)
        btn_remove.clicked.connect(remove_selected)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.Accepted:
            return

        items: list[tuple[bool, str]] = []
        for i in range(keyword_list.count()):
            item = keyword_list.item(i)
            rows = parse_keyword_items(item.text())
            if not rows:
                continue
            _, keyword = rows[0]
            items.append((item.checkState() == Qt.Checked, keyword))
        try:
            save_keyword_items(items)
        except OSError as e:
            QMessageBox.critical(
                self,
                "Не удалось сохранить",
                "Не удалось записать файл ключевых слов (нет прав или диск недоступен).\n\n"
                f"Путь: {KEYWORDS_FILE}\n\n"
                f"Подробности: {e}",
            )
            return
        active_keywords = tuple(keyword for enabled, keyword in items if enabled)
        self.model.set_keywords(active_keywords)
        self.sidebar.refresh_keywords_count()
        self._on_filters_changed()
        QMessageBox.information(
            self,
            "Список сохранён",
            f"Активных ключевых слов/фраз: {len(active_keywords)} из {len(items)}.",
        )

    def _ask_cache_choice(self) -> str:
        """Диалог «Показать из кэша / Загрузить заново / Отмена».

        Возвращает одно из: 'cache', 'refresh', 'cancel'.
        """
        meta = self._read_cache_meta()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Найден сохранённый результат")
        if meta:
            saved_at = (meta.get("saved_at") or "")[:16].replace("T", " ")
            count = meta.get("count") or 0
            box.setText(
                f"В кэше уже есть <b>{count}</b> процедур, сохранённых "
                f"<b>{saved_at}</b>."
            )
        else:
            box.setText("В кэше уже есть сохранённый результат поиска.")
        box.setInformativeText(
            "Что делать?\n\n"
            "• Показать из кэша — мгновенно вывести сохранённые данные.\n"
            "• Загрузить заново — очистить кэш и спарсить с сайта."
        )
        btn_cache = box.addButton("Показать из кэша", QMessageBox.AcceptRole)
        btn_refresh = box.addButton("Загрузить заново", QMessageBox.DestructiveRole)
        btn_cancel = box.addButton("Отмена", QMessageBox.RejectRole)
        box.setDefaultButton(btn_cache)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_cache:
            return "cache"
        if clicked is btn_refresh:
            return "refresh"
        return "cancel"

    def _read_cache_meta(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return {
                "saved_at": data.get("saved_at"),
                "count": len(data.get("procedures") or []),
                "total": data.get("total"),
            }
        except Exception:
            return None

    def _use_cache(self) -> None:
        """Загрузить результат из кэша в таблицу, без обращения к сайту."""
        if not CACHE_FILE.exists():
            return
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(
                self, "Кэш повреждён",
                f"Не удалось прочитать кэш:\n{e}\n\nБудет выполнен новый поиск.",
            )
            self._delete_cache()
            self.model.clear()
            self._current_start = 0
            self._last_total = 0
            self._start_task(
                self.sidebar.search_params(),
                start=0,
                batches=self._search_batches(self.sidebar.client_filters()),
                filters=self.sidebar.client_filters(),
            )
            return
        procs = data.get("procedures") or []
        self.model.set_rows(procs)
        self._last_total = int(data.get("total") or len(procs))
        self._current_start = len(procs)
        saved_at = (data.get("saved_at") or "")[:16].replace("T", " ")
        self.status_msg.setText(
            f"Показано из кэша: {len(procs)} процедур (сохранено {saved_at})."
        )
        self._refresh_counter()
        self._update_controls()

    def _delete_cache(self) -> None:
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
        except Exception:
            traceback.print_exc()
        self._cache_dirty = False

    def _on_load_more(self) -> None:
        if self.runner.is_running():
            return
        if not self._ensure_platform_ready():
            return
        filters = self.sidebar.client_filters()
        self._start_task(
            self.sidebar.search_params(),
            start=self._current_start,
            batches=1,
            filters=filters,
        )

    def _on_load_all(self) -> None:
        if self.runner.is_running():
            return
        if not self._ensure_platform_ready():
            return
        filters = self.sidebar.client_filters()
        self._start_task(
            self.sidebar.search_params(),
            start=self._current_start,
            batches=10_000,
            filters=filters,
        )

    def _selected_procedures(self) -> list[dict[str, Any]]:
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentIndex().isValid():
            rows = [self.table.currentIndex().row()]
        selected: list[dict[str, Any]] = []
        for row in rows:
            src = self.proxy.mapToSource(self.proxy.index(row, 0))
            proc = self.model.row_at(src.row())
            if proc is not None:
                selected.append(proc)
        return selected

    def _on_download_documents(self) -> None:
        if self.runner.is_running():
            return
        if not self._ensure_platform_ready():
            return
        if self._platform_key == "tektorg_rn":
            QMessageBox.information(
                self,
                "Скачивание документов",
                "Для ТЭК-Торг РН сейчас реализован поиск процедур. "
                "Скачивание документов подключим отдельным этапом.",
            )
            return
        procedures = self._selected_procedures()
        if not procedures:
            QMessageBox.information(
                self,
                "Ничего не выбрано",
                "Выберите одну или несколько строк в таблице.",
            )
            return
        default_download_dir = DOCUMENTS_DIR
        try:
            default_download_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            default_download_dir = Path.home() / "Documents"
        output_dir_str = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку для загрузки документов",
            str(default_download_dir),
        )
        if not output_dir_str:
            return
        output_dir = Path(output_dir_str)
        self._apply_selected_browser()

        self.progress.show()
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_load_more.setEnabled(False)
        self.btn_load_all.setEnabled(False)
        self.btn_download_docs.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self._set_badge("idle", "● Скачиваю документы…")

        fn = make_download_documents_task(self.client, procedures, output_dir)
        try:
            self.runner.start(
                fn,
                on_progress=self._on_progress,
                on_session=self._on_documents_status,
                on_error=self._on_error,
                on_done=self._on_task_done,
            )
        except Exception as e:
            self._on_error(f"Не удалось запустить скачивание: {e}")
            self._on_task_done()

    def _on_analyze_procedures(self) -> None:
        if self.runner.is_running():
            return
        if not self._ensure_platform_ready():
            return
        if self._platform_key == "tektorg_rn":
            QMessageBox.information(
                self,
                "Анализ",
                "Анализ карточки через LM Studio сейчас доступен только для ЭТП ГПБ.",
            )
            return
        procedures = self._selected_procedures()
        if not procedures:
            QMessageBox.information(
                self,
                "Ничего не выбрано",
                "Выберите одну или несколько строк в таблице.",
            )
            return
        self._apply_selected_browser()
        self._analysis_sink.clear()

        self.progress.show()
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_load_more.setEnabled(False)
        self.btn_load_all.setEnabled(False)
        self.btn_download_docs.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self._set_badge("idle", "● Анализ карточки и LM Studio…")

        fn = make_analyze_procedure_task(
            self.client,
            procedures,
            LM_STUDIO_BASE_URL,
            LM_STUDIO_MODEL,
            self._analysis_sink,
        )
        try:
            self.runner.start(
                fn,
                on_progress=self._on_progress,
                on_session=self._on_analyze_session,
                on_error=self._on_error,
                on_done=self._on_analyze_task_done,
            )
        except Exception as e:
            self._on_error(f"Не удалось запустить анализ: {e}")
            self._on_task_done()

    @Slot(bool, str)
    def _on_analyze_session(self, ok: bool, message: str) -> None:
        if ok:
            self._set_badge("true", "● Анализ выполнен")
            self.status_msg.setText(message)
        else:
            self._set_badge("false", "⚠ Ошибка анализа")
            self.status_msg.setText(message)

    @Slot()
    def _on_analyze_task_done(self) -> None:
        rows = self._analysis_sink.get("rows") or []
        self._on_task_done()
        if rows:
            try:
                summary_rows = self._save_analysis_tables(rows)
            except Exception as e:
                self._on_error(f"Не удалось сохранить файлы анализа: {e}")
                return
            self._show_analysis_table_dialog(summary_rows)

    def _safe_analysis_filename(self, name: str, default: str = "analysis") -> str:
        clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
        return (clean[:160] or default) + ".docx"

    def _save_analysis_tables(self, rows: list[list[str]]) -> list[list[str]]:
        from docx import Document

        ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        title_by_registry = self._analysis_sink.get("title_by_registry") or {}
        unpacked_by_registry = self._analysis_sink.get("unpacked_docs_by_registry") or {}
        summary_rows: list[list[str]] = []

        for row in rows:
            registry = str(row[0] if len(row) > 0 else "").strip() or "unknown"
            parsed_title = str(row[4] if len(row) > 4 else "").strip()
            source_title = str(title_by_registry.get(registry) or "").strip()
            title = parsed_title if parsed_title and parsed_title not in {"—", "не указано"} else source_title
            filename = self._safe_analysis_filename(f"{registry}_{title[:80]}", registry)
            path = ANALYSIS_DIR / filename
            n = 2
            while path.exists():
                path = ANALYSIS_DIR / self._safe_analysis_filename(f"{registry}_{title[:70]}_{n}", registry)
                n += 1

            doc = Document()
            doc.add_heading(f"Анализ закупки {registry}", level=1)
            if title:
                doc.add_paragraph(title)
            table = doc.add_table(rows=1, cols=2)
            table.style = "Table Grid"
            hdr = table.rows[0].cells
            hdr[0].text = "Поле"
            hdr[1].text = "Значение"
            for cell in hdr:
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
            for header, value in zip(ANALYSIS_TABLE_HEADERS_RU, row):
                cells = table.add_row().cells
                cells[0].text = str(header)
                cells[1].text = str(value or "—")

            doc.save(path)
            summary_rows.append([registry, title or "—", str(path), str(unpacked_by_registry.get(registry) or "")])

        self._analysis_sink["summary_rows"] = summary_rows
        return summary_rows

    def _show_analysis_table_dialog(self, rows: list[list[str]]) -> None:
        dlg = QDialog(self)
        n = len(rows)
        dlg.setWindowTitle("Результат анализа карточки ЭТП ГПБ" + (f" ({n} процедур)" if n != 1 else ""))
        dlg.resize(min(1100, self.width() + 80), min(520, self.height()))
        layout = QVBoxLayout(dlg)
        hint = QLabel(
            "Полная таблица анализа сохранена в Word-файлы. "
            "Нажмите «ссылка» в третьей колонке, чтобы выделить Word-файл, "
            "или в четвёртой колонке, чтобы открыть папку с разархивированными документами."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        headers = ["Реестровый номер", "Наименование", "Файл с таблицей", "Разархивированные документы"]
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 120)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(val)
                item.setToolTip(val[:2000] if val else "")
                if c in {2, 3}:
                    item.setText("ссылка" if val else "—")
                    item.setToolTip(val)
                    if val:
                        font = QFont(item.font())
                        font.setUnderline(True)
                        item.setFont(font)
                        item.setForeground(QColor("#0645ad"))
                table.setItem(r, c, item)

        def open_analysis_file(row: int, col: int) -> None:
            if col not in {2, 3}:
                return
            source = rows[row][col] if 0 <= row < len(rows) and len(rows[row]) > col else ""
            if source:
                try:
                    import subprocess

                    resolved = Path(source).resolve()
                    if col == 2:
                        subprocess.Popen(["explorer", "/select,", str(resolved)])
                    else:
                        subprocess.Popen(["explorer", str(resolved)])
                except Exception:
                    webbrowser.open(Path(source).resolve().as_uri())

        table.cellClicked.connect(open_analysis_file)
        table.cellDoubleClicked.connect(open_analysis_file)
        layout.addWidget(table, 1)

        issues = self._analysis_sink.get("document_issues") or []
        issues_label = QLabel("Ошибки обработки документов")
        issues_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
        layout.addWidget(issues_label)

        if issues:
            issue_table = QTableWidget(len(issues), 4)
            issue_table.setHorizontalHeaderLabels(["!", "Важность", "Реестровый номер", "Описание"])
            issue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            issue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            issue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            issue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            issue_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            issue_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            issue_table.setColumnWidth(0, 28)
            issue_table.setMaximumHeight(150)
            for r, issue in enumerate(issues):
                severity = str(issue.get("severity") or "important")
                is_critical = severity == "critical"
                color = QColor("#c00000" if is_critical else "#b8860b")
                level_text = "Критичная" if is_critical else "Важная"
                file_text = str(issue.get("file") or "").strip()
                message = str(issue.get("message") or "").strip()
                if file_text:
                    message = f"{file_text}: {message}"
                values = ["!", level_text, str(issue.get("registry") or ""), message]
                for c, val in enumerate(values):
                    item = QTableWidgetItem(val)
                    item.setToolTip(val)
                    if c in {0, 1}:
                        font = QFont(item.font())
                        font.setBold(True)
                        item.setFont(font)
                        item.setForeground(color)
                    issue_table.setItem(r, c, item)
            layout.addWidget(issue_table)
        else:
            no_issues = QLabel("Ошибок обработки документов нет.")
            no_issues.setStyleSheet("color: #4a515a;")
            layout.addWidget(no_issues)

        raw_map = self._analysis_sink.get("raw_by_registry") or {}
        if raw_map:

            def show_raw() -> None:
                raw_dlg = QDialog(dlg)
                raw_dlg.setWindowTitle("Сырой ответ модели")
                raw_dlg.resize(900, 600)
                rl = QVBoxLayout(raw_dlg)
                te = QTextEdit()
                te.setReadOnly(True)
                te.setPlainText(json.dumps(raw_map, ensure_ascii=False, indent=2))
                rl.addWidget(te)
                bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
                bb.rejected.connect(raw_dlg.reject)
                rl.addWidget(bb)
                raw_dlg.exec()

            btn_raw = QPushButton("Сырой ответ модели…")
            btn_raw.clicked.connect(show_raw)
            layout.addWidget(btn_raw)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _search_batches(self, filters: ClientFilters) -> int:
        if self._platform_key == "tektorg_rn" and (filters.quick_search or filters.title_contains):
            return 1
        if filters.keyword_search_enabled:
            return 10_000
        if len(filters.step_ids) > 1:
            # Сайт принимает один status за запрос. При мультивыборе добираем
            # выдачу и применяем объединение статусов локально.
            return 10_000
        return 1

    def _has_active_filters(self, filters: ClientFilters) -> bool:
        return any(
            (
                bool(filters.quick_search),
                bool(filters.keyword_search_enabled),
                bool(filters.registry_contains),
                bool(filters.unique_number_contains),
                bool(filters.organizer_contains),
                bool(filters.customer_contains),
                bool(filters.customer_region_contains),
                bool(filters.customer_agent_contains),
                bool(filters.title_contains),
                bool(filters.okpd2_contains),
                bool(filters.okved2_contains),
                filters.guarantee_min is not None,
                filters.guarantee_max is not None,
                bool(filters.responsible_contains),
                bool(filters.trend_pur),
                bool(filters.step_ids),
                bool(filters.purchase_form),
                filters.applics_min is not None,
                filters.applics_max is not None,
                filters.lots_min is not None,
                filters.lots_max is not None,
                filters.price_min is not None,
                filters.price_max is not None,
                filters.published_from is not None,
                filters.published_to is not None,
                filters.end_from is not None,
                filters.end_to is not None,
                filters.results_from is not None,
                filters.results_to is not None,
                bool(filters.special_features_contains),
                bool(filters.position_name_contains),
                bool(filters.national_regime_contains),
            )
        )

    def _start_task(
        self,
        params: SearchParams,
        start: int,
        batches: int,
        filters: Optional[ClientFilters] = None,
    ) -> None:
        filters = filters if filters is not None else self.sidebar.client_filters()
        self._cache_save_enabled = not self._has_active_filters(filters)
        self.progress.show()
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_load_more.setEnabled(False)
        self.btn_load_all.setEnabled(False)
        self.btn_download_docs.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self._set_badge("idle", "● Работаю…")

        fn = make_search_task(
            self.client,
            params,
            start,
            batches,
            client_filters=filters,
        )
        try:
            self.runner.start(
                fn,
                on_progress=self._on_progress,
                on_session=self._on_session_status,
                on_batch=self._on_batch_loaded,
                on_error=self._on_error,
                on_done=self._on_task_done,
            )
        except Exception as e:
            self._on_error(f"Не удалось запустить задачу: {e}")
            self._on_task_done()

    # --------------- слоты от Worker
    @Slot(str)
    def _on_progress(self, text: str) -> None:
        self.status_msg.setText(text)

    @Slot(bool, str)
    def _on_session_status(self, ok: bool, message: str) -> None:
        if ok:
            self._set_badge("true", "● Сессия активна")
            # Подхватим логин пользователя
            try:
                login = self.client.current_user_login()
            except Exception:
                login = None
            if login:
                self._last_user = login
                self.user_label.setText(f"Пользователь: {login}")
        else:
            if self._auth_dialog_open:
                return
            self._auth_dialog_open = True
            self._set_badge("false", "○ Нужен вход")
            # Диалог с подсказкой + кнопкой «Повторить»
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Требуется авторизация")
            box.setText("Сессия не активна.")
            box.setInformativeText(message)
            btn_retry = box.addButton("Я вошёл — повторить", QMessageBox.AcceptRole)
            box.addButton("Отмена", QMessageBox.RejectRole)
            try:
                box.exec()
                if box.clickedButton() is btn_retry:
                    QTimer.singleShot(200, self._on_search)
            finally:
                self._auth_dialog_open = False

    @Slot(bool, str)
    def _on_documents_status(self, ok: bool, message: str) -> None:
        self._set_badge("true" if ok else "false", "● Документы скачаны" if ok else "⚠ Ошибка")
        self.status_msg.setText(message)
        QMessageBox.information(self, "Скачивание документов", message)

    @Slot(list, int, int)
    def _on_batch_loaded(self, procs: list, start: int, total: int) -> None:
        self._last_total = total or self._last_total
        if start == 0 and self.model.rowCount() == 0:
            self.model.set_rows(procs)
        else:
            self.model.append_rows(procs)
        self._current_start = start
        self._refresh_counter()
        if self._cache_save_enabled:
            self._schedule_cache_save()

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        # Не даём приложению упасть — только сообщаем пользователю.
        self._set_badge("false", "⚠ Ошибка")
        self.status_msg.setText(msg.splitlines()[0] if msg else "Ошибка")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Ошибка")
        box.setText("При выполнении операции возникла ошибка:")
        box.setDetailedText(msg)
        box.exec()

    @Slot()
    def _on_task_done(self) -> None:
        self.progress.hide()
        self.btn_stop.setEnabled(False)
        self.sidebar.set_controls_enabled(True)
        self._update_controls()
        if self.model.rowCount() > 0:
            self.status_msg.setText(
                f"Загружено {self.model.rowCount()} / {self._last_total or self.model.rowCount()} процедур."
            )

    def _on_stop(self) -> None:
        self.runner.request_stop()
        self.btn_stop.setEnabled(False)
        self.status_msg.setText("Останавливаю…")
        try:
            self.client.close()
        except Exception:
            traceback.print_exc()

    # --------------- клиентские фильтры
    def _on_filters_changed(self) -> None:
        filters = self.sidebar.client_filters()
        self.proxy.set_filters(filters)
        self.model.set_keywords(filters.keywords)
        self._apply_table_column_widths()
        self._refresh_counter()

    def _on_reset_filters(self) -> None:
        self.sidebar.reset_client_filters()
        self.proxy.set_filters(ClientFilters())
        self.model.set_keywords(())
        self._apply_table_column_widths()
        self._refresh_counter()

    # --------------- таблица
    def _proc_from_index(self, idx: QModelIndex) -> Optional[dict[str, Any]]:
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        return self.model.row_at(src.row())

    def _on_row_clicked(self, idx: QModelIndex) -> None:
        if not idx.isValid() or not self.sidebar.btn_search_by_number.isChecked():
            return
        proc = self._proc_from_index(idx)
        self._open_in_browser(proc)
        self._start_tektorg_row_download(proc)

    def _on_row_double_clicked(self, idx: QModelIndex) -> None:
        proc = self._proc_from_index(idx)
        if self.sidebar.btn_search_by_number.isChecked():
            self._open_in_browser(proc)
            self._start_tektorg_row_download(proc)
            return
        self._open_in_browser(proc)

    def _ensure_procedure_folder(self, proc: Optional[dict[str, Any]]) -> Optional[Path]:
        if not proc:
            return None
        registry = str(
            proc.get("registry_number")
            or proc.get("procedure_number")
            or proc.get("number")
            or proc.get("id")
            or ""
        ).strip()
        if not registry:
            return None
        folder_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", registry).strip(" .")
        if not folder_name:
            return None
        folder = TENDER_DOCUMENTS_ROOT / folder_name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.status_msg.setText(f"Создана папка закупки: {folder}")
        except OSError as e:
            QMessageBox.warning(
                self,
                "Не удалось создать папку",
                f"Папка закупки не создана:\n{folder}\n\n{e}",
            )
            return None
        return folder

    def _download_tektorg_row_documents(
        self,
        proc: Optional[dict[str, Any]],
        folder: Path,
    ) -> bool:
        if not proc or self._platform_key != "tektorg_rn":
            return False
        download_fn = getattr(self.client, "download_visible_procedure_documents", None)
        if not callable(download_fn):
            return False
        try:
            result = download_fn(proc, folder, progress=self.status_msg.setText)
        except Exception as e:
            QMessageBox.warning(
                self,
                "Не удалось скачать документы",
                f"Карточка открыта не была или файлы не скачались:\n{e}",
            )
            return False

        saved = len(result.get("saved") or [])
        found = int(result.get("found") or 0)
        errors = len(result.get("errors") or [])
        self.status_msg.setText(
            f"Папка: {folder}. Найдено файлов: {found}, скачано: {saved}, ошибок: {errors}."
        )
        if found == 0:
            QMessageBox.information(
                self,
                "Документы не найдены",
                "В блоках «Документация процедуры» и «Извещение» ссылки на файлы не найдены.",
            )
        elif errors:
            QMessageBox.warning(
                self,
                "Скачивание завершено с ошибками",
                f"Скачано {saved} из {found} файлов.\nПапка:\n{folder}",
            )
        return True

    def _start_tektorg_row_download(self, proc: Optional[dict[str, Any]]) -> None:
        if not proc or self._platform_key != "tektorg_rn":
            return
        if self.row_download_runner.is_running():
            self.status_msg.setText("Документы уже скачиваются в фоне. Дождитесь завершения.")
            return
        self._row_download_sink.clear()
        self._row_download_sink["proc"] = dict(proc)
        fn = make_tektorg_row_documents_task(self.client, proc, sink=self._row_download_sink)
        try:
            self.row_download_runner.start(
                fn,
                on_progress=self._on_progress,
                on_session=self._on_row_documents_status,
                on_error=self._on_error,
            )
        except Exception as e:
            self._on_error(f"Не удалось запустить фоновое скачивание документов: {e}")

    @Slot(bool, str)
    def _on_row_documents_status(self, ok: bool, message: str) -> None:
        self.status_msg.setText(message)
        if not ok:
            QMessageBox.warning(self, "Скачивание документов", message)
            return
        self._bring_window_to_front()
        box = QMessageBox(self)
        box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Файлы скачаны")
        box.setText(
            "Документация по выбранной процедуре успешно загружена.\n\n"
            "Пожалуйста, заполните необходимые документы. "
            "После завершения заполнения нажмите «Готово» для продолжения работы."
        )
        btn_done = box.addButton("Готово", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is btn_done:
            self._sort_filled_documents_after_done()

    def _sort_filled_documents_after_done(self) -> None:
        folder_text = str(self._row_download_sink.get("folder") or "").strip()
        if not folder_text:
            QMessageBox.warning(
                self,
                "Папка закупки не найдена",
                "Не удалось определить папку с загруженной документацией.",
            )
            return
        folder = Path(folder_text)
        try:
            result = sort_filled_documents(folder)
        except Exception as e:
            QMessageBox.warning(
                self,
                "Ошибка распределения файлов",
                f"Не удалось распределить файлы по папкам:\n{e}",
            )
            return

        commercial_count = len(result.get("commercial") or [])
        technical_count = len(result.get("technical") or [])
        errors = result.get("errors") or []
        self.status_msg.setText(
            f"Файлы распределены. Коммерческие: {commercial_count}, технические: {technical_count}, ошибок: {len(errors)}."
        )
        if errors:
            QMessageBox.warning(
                self,
                "Распределение завершено с ошибками",
                "Часть файлов не удалось переместить:\n\n" + "\n".join(str(e) for e in errors[:10]),
            )
            return
        application_url = self._open_application_create_tab()
        if application_url:
            self._start_technical_upload(application_url, folder / "Технические")

    def _bring_window_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(1200, self._clear_temporary_topmost)

    def _clear_temporary_topmost(self) -> None:
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self.show()

    def _bring_browser_to_front(self) -> Optional[int]:
        try:
            driver = self.client.driver
            if driver is None:
                return None
            title = str(driver.title or "").strip()
            browser_hint = str(getattr(self.client.browser, "label", "") or "").casefold()
        except Exception:
            return None

        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            self.show()

            import win32con
            import win32gui

            candidates: list[int] = []

            def enum_handler(hwnd, _) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                window_title = win32gui.GetWindowText(hwnd)
                if not window_title:
                    return
                low = window_title.casefold()
                title_match = bool(title and title.casefold() in low)
                browser_match = bool(
                    ("chrome" in low or "edge" in low or browser_hint and browser_hint in low)
                    and "cursor" not in low
                )
                if title_match or browser_match:
                    candidates.append(hwnd)

            win32gui.EnumWindows(enum_handler, None)
            if not candidates:
                return None

            hwnd = candidates[0]
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOP,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            win32gui.SetForegroundWindow(hwnd)
            return hwnd
        except Exception:
            try:
                self.showMinimized()
            except Exception:
                pass
        return None

    def _application_create_url(self) -> str:
        proc = self._row_download_sink.get("proc") or {}
        proc_id = (
            proc.get("procedure_id")
            or proc.get("procedureId")
            or proc.get("id")
            or proc.get("procedure")
        )
        lot_id = (
            proc.get("lot_id")
            or proc.get("lotId")
            or proc.get("lot")
            or proc.get("active_lot_id")
        )
        if not proc_id or not lot_id:
            return ""
        return f"https://rn.tektorg.ru/index.php#com/applic/create/lot/{lot_id}/procedure/{proc_id}"

    def _open_application_create_tab(self) -> str:
        try:
            url = self._application_create_url()
            if not url:
                QMessageBox.warning(
                    self,
                    "Не удалось открыть заявку",
                    "Не найден id процедуры или id лота для формирования ссылки на подачу заявки.",
                )
                return ""
            if not self._open_url_in_managed_browser(url, new_tab=True):
                return ""
            return url
        except Exception:
            return ""

    def _start_technical_upload(self, application_url: str, technical_dir: Path) -> None:
        if self.technical_upload_runner.is_running():
            self.status_msg.setText("Технические файлы уже загружаются в форму заявки.")
            return
        fn = make_tektorg_technical_upload_task(
            self.client,
            application_url,
            technical_dir,
            timing_sink=self._upload_timing_sink,
        )
        try:
            self.technical_upload_runner.start(
                fn,
                on_progress=self._on_progress,
                on_session=self._on_technical_upload_status,
                on_error=self._on_error,
            )
        except Exception as e:
            self._on_error(f"Не удалось запустить загрузку технических файлов: {e}")

    @Slot(bool, str)
    def _on_technical_upload_status(self, ok: bool, message: str) -> None:
        self.status_msg.setText(message)
        self._show_upload_timing_dialog()
        if not ok:
            QMessageBox.warning(self, "Загрузка технических файлов", message)

    def _show_upload_timing_dialog(self) -> None:
        timings = self._upload_timing_sink.get("timings") or []
        if not timings:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Временный лог выполнения")
        dlg.resize(760, 520)
        layout = QVBoxLayout(dlg)
        text = QTextEdit(dlg)
        text.setReadOnly(True)
        lines = ["Детальный лог времени выполнения:", ""]
        for index, item in enumerate(timings, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "Шаг").strip()
            seconds = float(item.get("seconds") or 0)
            ok = bool(item.get("ok", True))
            status = "OK" if ok else "ОШИБКА"
            lines.append(f"{index}. [{status}] {label}: {seconds:.2f} сек.")
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _on_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        proc = self._proc_from_index(idx)
        menu = QMenu(self)
        menu.addAction("Открыть в Chrome", lambda: self._open_in_browser(proc))
        menu.addSeparator()
        menu.addAction(
            "Копировать реестровый №",
            lambda: QApplication.clipboard().setText(
                str((proc or {}).get("registry_number") or (proc or {}).get("procedure_number") or "")
            ),
        )
        menu.addAction(
            "Копировать наименование",
            lambda: QApplication.clipboard().setText(str((proc or {}).get("title") or "")),
        )
        menu.addAction(
            "Копировать ИНН организатора",
            lambda: QApplication.clipboard().setText(str((proc or {}).get("org_inn") or "")),
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _open_in_browser(self, proc: Optional[dict[str, Any]]) -> None:
        if not proc:
            return
        pid = proc.get("id")
        if not pid:
            return
        url = str(proc.get("url") or VIEW_URL.format(pid=pid))
        if self._open_url_in_managed_browser(url, new_tab=False):
            return
        webbrowser.open(url)

    def _open_url_in_managed_browser(self, url: str, new_tab: bool = True) -> bool:
        try:
            if not self.client.is_chrome_running():
                self.status_msg.setText(f"Запускаю {self.client.browser.label} для открытия страницы...")
                self.client.ensure_chrome(timeout=45)
            if self.client.driver is None:
                self.client.connect()
            driver = self.client.driver
            if driver is None:
                return False
            if new_tab:
                handles_before = set(driver.window_handles)
                driver.execute_script("window.open(arguments[0], '_blank');", url)
                deadline = time.time() + 8
                while time.time() < deadline:
                    new_handles = [handle for handle in driver.window_handles if handle not in handles_before]
                    if new_handles:
                        driver.switch_to.window(new_handles[-1])
                        break
                    time.sleep(0.2)
                else:
                    driver.get(url)
            else:
                driver.get(url)
            self._bring_browser_to_front()
            return True
        except Exception as e:
            self.status_msg.setText(f"Не удалось открыть страницу в управляемом браузере: {e}")
            return False

    # --------------- экспорт
    def _on_export(self) -> None:
        if self.model.rowCount() == 0:
            QMessageBox.information(self, "Нет данных", "Сначала выполните поиск.")
            return
        default_name = f"procedures_{datetime.now():%Y%m%d_%H%M}.xlsx"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Сохранить в XLSX",
            str(Path.cwd() / default_name),
            "Excel (*.xlsx)",
        )
        if not path_str:
            return
        visible: list[dict[str, Any]] = []
        for i in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(i, 0))
            r = self.model.row_at(src.row())
            if r is not None:
                visible.append(r)
        try:
            self._write_xlsx(Path(path_str), visible)
            QMessageBox.information(
                self, "Готово",
                f"Сохранено {len(visible)} строк в\n{path_str}",
            )
        except Exception as e:
            QMessageBox.critical(self, "Ошибка экспорта", str(e))

    def _write_xlsx(self, path: Path, procs: list[dict[str, Any]]) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        wb = Workbook()
        ws = wb.active
        ws.title = "Процедуры"
        titles = [c[1] for c in COLUMNS] + ["id", "Дата публикации"]
        ws.append(titles)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for p in procs:
            ws.append(
                [self.model._display(p, key) for key, _ in COLUMNS]
                + [p.get("id"), fmt_date(parse_dt(p.get("date_published")))]
            )
        widths = [18, 36, 18, 20, 20, 20, 28, 10, 20]
        for i, w in enumerate(widths, start=1):
            col_letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[col_letter].width = w
        ws.freeze_panes = "A2"
        wb.save(path)

    # --------------- счётчики / бейдж
    def _refresh_counter(self) -> None:
        loaded = self.model.rowCount()
        visible = self.proxy.rowCount()
        total = self._last_total
        if loaded == 0 and total == 0:
            self.lbl_counter.setText("Данных нет. Нажмите «Поиск».")
        elif total and loaded < total:
            self.lbl_counter.setText(
                f"Показано {visible} (загружено {loaded}) из {total} по фильтру поиска."
            )
        else:
            self.lbl_counter.setText(f"Показано {visible} из {loaded} процедур.")
        self._update_controls()

    def _set_badge(self, state: str, text: str) -> None:
        self.session_badge.setProperty("ok", state)
        self.session_badge.setText(text)
        self.session_badge.style().unpolish(self.session_badge)
        self.session_badge.style().polish(self.session_badge)

    def _update_controls(self) -> None:
        running = self.runner.is_running()
        platform_ready = self._is_platform_ready()
        loaded = self.model.rowCount()
        total = self._last_total
        has_more = total > 0 and self._current_start < total
        self.btn_platform_gpb.setEnabled(not running)
        self.btn_platform_roseltorg.setEnabled(not running)
        self.btn_load_more.setEnabled(platform_ready and not running and has_more)
        self.btn_load_all.setEnabled(platform_ready and not running and has_more)
        self.btn_download_docs.setEnabled(platform_ready and self._platform_key == "gpb" and not running and loaded > 0)
        self.btn_analyze.setEnabled(platform_ready and self._platform_key == "gpb" and not running and loaded > 0)
        self.btn_export.setEnabled(platform_ready and loaded > 0)
        self.sidebar.set_controls_enabled(platform_ready and not running)

    # --------------- кэш
    def _schedule_cache_save(self) -> None:
        self._cache_dirty = True
        self._cache_save_timer.start(1000)

    def _save_cache_now(self) -> None:
        if not self._cache_dirty:
            return
        self._cache_dirty = False
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now().isoformat(),
                "date_from": self.sidebar.ed_date_from.date().toString("dd.MM.yyyy"),
                "query": self.sidebar.ed_query.text().strip(),
                "total": self._last_total,
                "procedures": self.model.rows(),
            }
            CACHE_FILE.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            traceback.print_exc()

    def _announce_cache_on_start(self) -> None:
        """При старте только сообщаем, что есть кэш — не загружаем его автоматически.

        Сам выбор (использовать / перезагрузить) делается при клике «Поиск».
        """
        if self._platform_key != "gpb":
            self.status_msg.setText("Готов. Нажмите «Поиск» и войдите через ЭЦП при необходимости.")
            return
        meta = self._read_cache_meta()
        if meta and meta.get("count"):
            saved_at = (meta.get("saved_at") or "")[:16].replace("T", " ")
            self.lbl_counter.setText(
                f"Есть сохранённый результат: {meta['count']} процедур от {saved_at}. "
                "Нажмите «Поиск», чтобы выбрать действие."
            )
            self.status_msg.setText(
                "Готов. Найден кэш — выбор предложат при нажатии «Поиск»."
            )
        else:
            self.status_msg.setText("Готов. Нажмите «Поиск».")

    # --------------- закрытие
    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self._save_cache_now()
        except Exception:
            pass
        try:
            self.runner.shutdown(wait_ms=2000)
        except Exception:
            pass
        try:
            self.row_download_runner.shutdown(wait_ms=2000)
        except Exception:
            pass
        try:
            self.technical_upload_runner.shutdown(wait_ms=2000)
        except Exception:
            pass
        try:
            self.client.close()
        except Exception:
            pass
        event.accept()
