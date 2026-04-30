from __future__ import annotations

import json
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
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
    QStatusBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from etp_client import EtpClient, step_id_label, trend_pur_label

from .constants import APP_TITLE, CACHE_FILE, COLUMNS, DOCUMENTS_DIR, VIEW_URL
from .keywords import load_keyword_items, parse_keywords, save_keyword_items
from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import ClientFilters, SearchParams
from .sidebar import Sidebar
from .utils import fmt_date, parse_dt, parse_price
from .worker import TaskRunner, make_download_documents_task, make_search_task

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

        self._cache_save_timer = QTimer(self)
        self._cache_save_timer.setSingleShot(True)
        self._cache_save_timer.timeout.connect(self._save_cache_now)

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

        main_area_layout.addWidget(actions)

        # Таблица
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(8, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(26)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 230)
        self.table.setColumnWidth(4, 220)
        self.table.setColumnWidth(5, 80)
        self.table.setColumnWidth(6, 95)
        self.table.setColumnWidth(7, 145)
        self.table.setColumnWidth(8, 145)
        self.table.setColumnWidth(9, 170)
        self.table.setColumnWidth(10, 200)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
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

    # ------------------------------------------------------------------ задачи
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

        if CACHE_FILE.exists():
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
        for enabled, keyword in load_keyword_items():
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
            parsed = parse_keywords(text)
            if not parsed:
                return
            item = QListWidgetItem(parsed[0])
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
            parsed = parse_keywords(item.text())
            if not parsed:
                continue
            items.append((item.checkState() == Qt.Checked, parsed[0]))
        save_keyword_items(items)
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
        self._start_task(self.sidebar.search_params(), start=self._current_start, batches=1)

    def _on_load_all(self) -> None:
        if self.runner.is_running():
            return
        self._start_task(self.sidebar.search_params(), start=self._current_start, batches=10_000)

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
        procedures = self._selected_procedures()
        if not procedures:
            QMessageBox.information(
                self,
                "Ничего не выбрано",
                "Выберите одну или несколько строк в таблице.",
            )
            return
        DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
        output_dir_str = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку для загрузки документов",
            str(DOCUMENTS_DIR),
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

    def _search_batches(self, filters: ClientFilters) -> int:
        return 10_000 if filters.keyword_search_enabled else 1

    def _start_task(self, params: SearchParams, start: int, batches: int) -> None:
        self.progress.show()
        self.btn_stop.setEnabled(True)
        self.sidebar.set_controls_enabled(False)
        self.btn_load_more.setEnabled(False)
        self.btn_load_all.setEnabled(False)
        self.btn_download_docs.setEnabled(False)
        self._set_badge("idle", "● Работаю…")

        fn = make_search_task(
            self.client,
            params,
            start,
            batches,
            client_filters=self.sidebar.client_filters(),
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
        if start == 0 and self.model.rowCount() == 0:
            self.model.set_rows(procs)
        else:
            self.model.append_rows(procs)
        self._current_start = start
        self._refresh_counter()
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
        self._refresh_counter()

    def _on_reset_filters(self) -> None:
        self.sidebar.reset_client_filters()
        self.proxy.set_filters(ClientFilters())
        self.model.set_keywords(())
        self._refresh_counter()

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
                    trend_pur_label(p.get("trend_pur")),
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
                    step_id_label(p.get("step_id")),
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
        loaded = self.model.rowCount()
        total = self._last_total
        has_more = total > 0 and self._current_start < total
        self.btn_load_more.setEnabled(not running and has_more)
        self.btn_load_all.setEnabled(not running and has_more)
        self.btn_download_docs.setEnabled(not running and loaded > 0)
        self.btn_export.setEnabled(loaded > 0)
        self.sidebar.set_controls_enabled(not running)

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
