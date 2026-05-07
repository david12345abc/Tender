from __future__ import annotations

import json
import re
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QEvent, QModelIndex, QObject, QSize, QTimer, Qt, Slot
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
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from etp_client import EtpClient, PROCEDURE_TYPE_OPTIONS, STATUS_OPTIONS, step_id_label, trend_pur_label
from roseltorg_client import (
    ROSELTORG_PROCEDURE_TYPE_OPTIONS,
    ROSELTORG_SEARCH_BY_OPTIONS,
    ROSELTORG_STATUS_OPTIONS,
    RoseltorgClient,
)

from .constants import (
    ANALYSIS_DIR,
    APP_TITLE,
    CACHE_FILE,
    COLUMNS,
    DOCUMENTS_DIR,
    KEYWORDS_FILE,
    LM_STUDIO_BASE_URL,
    LM_STUDIO_MODEL,
    VIEW_URL,
)
from .lm_table_analysis import ANALYSIS_TABLE_HEADERS_RU
from .keywords import load_keyword_items, parse_keyword_items, save_keyword_items
from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import ClientFilters, SearchParams
from .sidebar import Sidebar
from .utils import fmt_date, parse_dt, parse_price
from .worker import (
    TaskRunner,
    make_analyze_procedure_task,
    make_download_documents_task,
    make_search_task,
)


class LimitedWrapDelegate(QStyledItemDelegate):
    """Переносит длинный текст в таблице, но не даёт строкам занять весь экран."""

    MAX_ROW_HEIGHT = 96
    MIN_ROW_HEIGHT = 26
    PADDING = 12

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        option.features |= QStyleOptionViewItem.ViewItemFeature.WrapText
        option.textElideMode = Qt.TextElideMode.ElideRight

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        base = super().sizeHint(option, index)
        text = str(index.data(Qt.DisplayRole) or "")
        if not text:
            return QSize(base.width(), self.MIN_ROW_HEIGHT)

        width = option.rect.width()
        view = self.parent()
        if width <= 0 and isinstance(view, QTableView):
            width = view.columnWidth(index.column())
        text_width = max(40, width - self.PADDING)
        rect = option.fontMetrics.boundingRect(
            0,
            0,
            text_width,
            10_000,
            int(Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextExpandTabs),
            text,
        )
        height = min(max(self.MIN_ROW_HEIGHT, rect.height() + self.PADDING), self.MAX_ROW_HEIGHT)
        return QSize(base.width(), height)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 900)

        self.client = EtpClient()
        self.runner = TaskRunner(self)
        self.model = ProcedureTableModel(self)
        self.proxy = ProcedureFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self._last_total: int = 0
        self._current_start: int = 0
        self._last_user: Optional[str] = None
        self._cache_dirty: bool = False
        self._cache_save_enabled: bool = True
        self._platform_key: str = "gpb"
        self._api_debug_chunks: list[str] = []

        self._cache_save_timer = QTimer(self)
        self._cache_save_timer.setSingleShot(True)
        self._cache_save_timer.timeout.connect(self._save_cache_now)
        self._analysis_sink: dict[str, Any] = {}

        self._build_ui()
        self._announce_cache_on_start()
        self._update_controls()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # ---------- Верхняя панель ----------
        top = QFrame()
        top.setObjectName("TopBar")
        top.setFixedHeight(56)
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
        self.btn_platform_gpb.setChecked(True)
        self.btn_platform_roseltorg = QPushButton("Росэлторг")
        self.btn_platform_roseltorg.setObjectName("PlatformButton")
        self.btn_platform_roseltorg.setCheckable(True)
        self.platform_group.addButton(self.btn_platform_gpb)
        self.platform_group.addButton(self.btn_platform_roseltorg)
        platform_layout.addWidget(self.btn_platform_gpb)
        platform_layout.addWidget(self.btn_platform_roseltorg)
        self.btn_platform_gpb.clicked.connect(lambda: self._select_platform("gpb"))
        self.btn_platform_roseltorg.clicked.connect(lambda: self._select_platform("roseltorg"))
        top_layout.addWidget(platform_switcher)

        t_title_box = QVBoxLayout()
        t_title_box.setSpacing(0)
        self.title_label = QLabel("ЭТП ГПБ — Актуальные процедуры")
        self.title_label.setObjectName("TopBarTitle")
        self.subtitle_label = QLabel("Поиск, фильтры и экспорт")
        self.subtitle_label.setObjectName("TopBarSubtitle")
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

        self.btn_save_api_debug = QPushButton("Сохранить API-логи…")
        self.btn_save_api_debug.setToolTip("Сохранить запросы, headers, body, token и ответы API в файл")
        self.btn_save_api_debug.clicked.connect(self._save_api_debug)
        self.btn_save_api_debug.setEnabled(False)
        actions_layout.addWidget(self.btn_save_api_debug)

        main_area_layout.addWidget(actions)

        # Таблица
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(8, Qt.AscendingOrder)
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
        self.table.setWordWrap(True)
        self.table.setItemDelegate(LimitedWrapDelegate(self.table))
        hh.sectionResized.connect(lambda *_: self._schedule_table_row_resize())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        self.proxy.modelReset.connect(self._apply_table_column_widths)
        self.proxy.modelReset.connect(self._schedule_table_row_resize)
        self.proxy.rowsInserted.connect(self._schedule_table_row_resize)
        self.proxy.layoutChanged.connect(self._schedule_table_row_resize)
        self.table.viewport().installEventFilter(self)
        self._apply_table_column_widths()
        main_area_layout.addWidget(self.table, 1)

        bottom_bar = QFrame()
        bottom_bar.setObjectName("BottomBar")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(10, 8, 10, 8)
        bottom_layout.setSpacing(8)

        self.btn_prev_page = QPushButton("←")
        self.btn_prev_page.setToolTip("Предыдущая страница")
        self.btn_prev_page.clicked.connect(self._on_prev_page)
        self.btn_prev_page.setEnabled(False)
        bottom_layout.addWidget(self.btn_prev_page)

        self.lbl_page = QLabel("Страница 0 из 0")
        self.lbl_page.setMinimumWidth(120)
        self.lbl_page.setAlignment(Qt.AlignCenter)
        bottom_layout.addWidget(self.lbl_page)

        self.btn_next_page = QPushButton("→")
        self.btn_next_page.setToolTip("Следующая страница")
        self.btn_next_page.clicked.connect(self._on_next_page)
        self.btn_next_page.setEnabled(False)
        bottom_layout.addWidget(self.btn_next_page)

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
        self.status_msg = QLabel("Готов.")
        sb.addWidget(self.status_msg, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Обработано: %v из %m")
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
        """Фиксированные ширины колонок — иначе заголовок сжимает их под вьюпорт и скролл не появляется."""
        hh = self.table.horizontalHeader()
        widths = [170, 180, 230, 280, 220, 80, 95, 145, 145, 170, 200]
        n = min(len(widths), self.proxy.columnCount())
        for i in range(n):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            hh.resizeSection(i, widths[i])
        self._schedule_table_row_resize()

    def _schedule_table_row_resize(self) -> None:
        QTimer.singleShot(0, self._resize_table_rows_to_contents)

    def _resize_table_rows_to_contents(self) -> None:
        self.table.resizeRowsToContents()

    def _clear_api_debug(self) -> None:
        self._api_debug_chunks.clear()
        self.btn_save_api_debug.setEnabled(False)

    @Slot(str)
    def _on_api_debug(self, text: str) -> None:
        if not text:
            return
        self._api_debug_chunks.append(text.strip())
        self.btn_save_api_debug.setEnabled(True)

    def _save_api_debug(self) -> None:
        if not self._api_debug_chunks:
            QMessageBox.information(
                self,
                "Логов пока нет",
                "Запустите поиск. После первых ответов API здесь можно будет сохранить лог.",
            )
            return

        default_name = f"api_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Куда сохранить API-логи",
            default_name,
            "JSON (*.json);;Text (*.txt);;Все файлы (*.*)",
        )
        if not path:
            return

        entries: list[Any] = []
        for chunk in self._api_debug_chunks:
            try:
                entries.append(json.loads(chunk))
            except json.JSONDecodeError:
                entries.append({"raw": chunk})
        content = json.dumps(
            {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(
                self,
                "Не удалось сохранить лог",
                "Файл не удалось записать. Выберите другую папку или имя файла.\n\n"
                f"Путь: {path}\n\n"
                f"Подробности: {e}",
            )
            return

        QMessageBox.information(self, "API-логи сохранены", f"Файл сохранён:\n{path}")

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
        return self._platform_key in {"gpb", "roseltorg"}

    def _platform_title(self) -> str:
        return "Росэлторг" if self._platform_key == "roseltorg" else "ЭТП ГПБ"

    def _set_platform_buttons(self) -> None:
        self.btn_platform_gpb.setChecked(self._platform_key == "gpb")
        self.btn_platform_roseltorg.setChecked(self._platform_key == "roseltorg")

    def _apply_platform_ui(self) -> None:
        if self._platform_key == "roseltorg":
            self.sidebar.set_platform_filter_options(
                ROSELTORG_PROCEDURE_TYPE_OPTIONS,
                ROSELTORG_STATUS_OPTIONS,
                ROSELTORG_SEARCH_BY_OPTIONS,
                platform_key="roseltorg",
            )
            self.title_label.setText("Росэлторг — Актуальные процедуры")
            self.subtitle_label.setText("Поиск, фильтры и ключевые слова")
            self.lbl_counter.setText("Данных нет. Нажмите «Поиск». Если сессии нет, войдите через ЭЦП.")
            self.user_label.setText("Пользователь: —")
            self._set_badge("idle", "○  Росэлторг")
            self.status_msg.setText("Готов. Нажмите «Поиск» и войдите через ЭЦП при необходимости.")
        else:
            self.sidebar.set_platform_filter_options(
                PROCEDURE_TYPE_OPTIONS,
                STATUS_OPTIONS,
                None,
                platform_key="gpb",
            )
            self.title_label.setText("ЭТП ГПБ — Актуальные процедуры")
            self.subtitle_label.setText("Поиск, фильтры и экспорт")
            if not self.model.rowCount():
                self.lbl_counter.setText("Данных нет. Нажмите «Поиск».")
            self._set_badge("idle", "○  Браузер не запущен")
            self.status_msg.setText("Готов. Нажмите «Поиск».")

    def _select_platform(self, key: str) -> None:
        if key not in {"gpb", "roseltorg"}:
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
        self.client = RoseltorgClient() if key == "roseltorg" else EtpClient()
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
        self._schedule_table_row_resize()
        self._update_controls()

    def _delete_cache(self) -> None:
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
        except Exception:
            traceback.print_exc()
        self._cache_dirty = False

    def _on_prev_page(self) -> None:
        self.proxy.previous_page()
        self._refresh_counter()
        self.table.scrollToTop()
        self._schedule_table_row_resize()

    def _on_next_page(self) -> None:
        self.proxy.next_page()
        self._refresh_counter()
        self.table.scrollToTop()
        self._schedule_table_row_resize()

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
        if self._platform_key == "roseltorg":
            QMessageBox.information(
                self,
                "Скачивание документов",
                "Для Росэлторга сейчас реализован поиск процедур. "
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
        self.progress.setRange(0, 0)
        self.progress.setFormat("Скачиваю документы...")
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
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
        if self._platform_key == "roseltorg":
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
        self.progress.setRange(0, 0)
        self.progress.setFormat("Анализирую...")
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
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
        return 10_000

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
        self.progress.setRange(0, 0)
        self.progress.setFormat("Ищу процедуры...")
        self._clear_api_debug()
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
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
                on_debug=self._on_api_debug,
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
            self._set_badge("false", "○ Нужен вход")
            # Диалог с подсказкой + кнопкой «Повторить»
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Information)
            box.setWindowTitle("Требуется авторизация")
            box.setText("Сессия не активна.")
            box.setInformativeText(message)
            btn_retry = box.addButton("Я вошёл — повторить", QMessageBox.AcceptRole)
            box.addButton("Отмена", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is btn_retry:
                QTimer.singleShot(200, self._on_search)

    @Slot(bool, str)
    def _on_documents_status(self, ok: bool, message: str) -> None:
        self._set_badge("true" if ok else "false", "● Документы скачаны" if ok else "⚠ Ошибка")
        self.status_msg.setText(message)
        QMessageBox.information(self, "Скачивание документов", message)

    @Slot(list, int, int)
    def _on_batch_loaded(self, procs: list, start: int, total: int) -> None:
        self._last_total = total or self._last_total
        if total:
            processed = min(max(start, 0), total)
            self.progress.setRange(0, total)
            self.progress.setValue(processed)
            self.progress.setFormat(f"Обработано: {processed} из {total}")
        if start == 0 and self.model.rowCount() == 0:
            self.model.set_rows(procs)
        else:
            self.model.append_rows(procs)
        self._current_start = start
        self.proxy.refresh_page()
        self._refresh_counter()
        self._schedule_table_row_resize()
        if procs and self._cache_save_enabled:
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
        self.status_msg.setText(f"Найдено {self.proxy.filtered_count()} процедур.")

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
        self._refresh_counter()
        self._schedule_table_row_resize()

    def _on_reset_filters(self) -> None:
        self.sidebar.reset_client_filters()
        self.proxy.set_filters(ClientFilters())
        self.model.set_keywords(())
        self._refresh_counter()
        self._schedule_table_row_resize()

    # --------------- таблица
    def _proc_from_index(self, idx: QModelIndex) -> Optional[dict[str, Any]]:
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        return self.model.row_at(src.row())

    def _on_row_double_clicked(self, idx: QModelIndex) -> None:
        self._open_in_browser(self._proc_from_index(idx))

    def _on_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        proc = self._proc_from_index(idx)
        if not proc:
            return
        cell_value = str(idx.data(Qt.DisplayRole) or "").strip()
        menu = QMenu(self)
        copy_cell_action = menu.addAction(
            "Копировать значение ячейки",
            lambda value=cell_value: QApplication.clipboard().setText(value),
        )
        copy_cell_action.setEnabled(bool(cell_value))
        menu.addSeparator()
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
        if proc.get("url"):
            webbrowser.open(str(proc["url"]))
            return
        webbrowser.open(VIEW_URL.format(pid=pid))

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
                [
                    p.get("registry_number") or p.get("procedure_number") or "",
                    self.model._display(p, "trend_pur_label"),
                    p.get("short_name") or p.get("full_name") or "",
                    p.get("title") or "",
                    ", ".join(self.model._keyword_matches(p)),
                    ", ".join(str(t) for t in (p.get("tags") or [])),
                    p.get("applics_count") or 0,
                    fmt_date(
                        self.model._first_date(
                            p,
                            (
                                "date_start_registration",
                                "date_begin_registration",
                                "date_registration_start",
                                "date_start_applic",
                                "date_begin_applic",
                                "date_published",
                            ),
                        )
                    ),
                    fmt_date(parse_dt(p.get("date_end_registration"))),
                    parse_price(p.get("total_price")) or "",
                    self.model._display(p, "step_label"),
                    p.get("id"),
                    fmt_date(parse_dt(p.get("date_published"))),
                ]
            )
        widths = [18, 28, 28, 80, 30, 12, 12, 20, 20, 18, 28, 10, 20]
        for i, w in enumerate(widths, start=1):
            col_letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[col_letter].width = w
        ws.freeze_panes = "A2"
        wb.save(path)

    # --------------- счётчики / бейдж
    def _refresh_counter(self) -> None:
        loaded = self.model.rowCount()
        visible = self.proxy.rowCount()
        found = self.proxy.filtered_count()
        page_count = self.proxy.page_count()
        current_page = self.proxy.current_page() + 1 if found else 0
        if loaded == 0:
            self.lbl_counter.setText("Данных нет. Нажмите «Поиск».")
            self.lbl_page.setText("Страница 0 из 0")
        elif found == 0:
            self.lbl_counter.setText("По текущим фильтрам ничего не найдено.")
            self.lbl_page.setText("Страница 0 из 0")
        else:
            self.lbl_counter.setText(
                f"Найдено {found}. Показано {visible} на странице."
            )
            self.lbl_page.setText(f"Страница {current_page} из {page_count}")
        self._update_controls()

    def _set_badge(self, state: str, text: str) -> None:
        self.session_badge.setProperty("ok", state)
        self.session_badge.setText(text)
        self.session_badge.style().unpolish(self.session_badge)
        self.session_badge.style().polish(self.session_badge)

    def _update_controls(self) -> None:
        running = self.runner.is_running()
        platform_ready = self._is_platform_ready()
        found = self.proxy.filtered_count()
        has_rows = self.model.rowCount() > 0
        has_visible_rows = found > 0
        current_page = self.proxy.current_page()
        page_count = self.proxy.page_count()
        self.btn_platform_gpb.setEnabled(not running)
        self.btn_platform_roseltorg.setEnabled(not running)
        self.btn_prev_page.setEnabled(platform_ready and current_page > 0)
        self.btn_next_page.setEnabled(platform_ready and current_page + 1 < page_count)
        self.btn_download_docs.setEnabled(platform_ready and self._platform_key == "gpb" and not running and has_visible_rows)
        self.btn_analyze.setEnabled(platform_ready and self._platform_key == "gpb" and not running and has_visible_rows)
        self.btn_export.setEnabled(platform_ready and has_rows)
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
            self.client.close()
        except Exception:
            pass
        event.accept()
