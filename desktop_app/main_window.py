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
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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

from .constants import APP_TITLE, CACHE_FILE, COLUMNS, VIEW_URL
from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import ClientFilters, SearchParams
from .sidebar import Sidebar
from .utils import fmt_date, parse_dt, parse_price
from .worker import TaskRunner, make_search_task

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

        self.session_badge = QLabel("○  Chrome не запущен")
        self.session_badge.setObjectName("SessionBadge")
        self.session_badge.setProperty("ok", "idle")
        top_layout.addWidget(self.session_badge)

        # ---------- Фильтры и основная область ----------
        self.sidebar = Sidebar()
        self.sidebar.searchRequested.connect(self._on_search)
        self.sidebar.resetRequested.connect(self._on_reset_filters)
        self.sidebar.clientFiltersChanged.connect(self._on_filters_changed)

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
        self.table.sortByColumn(6, Qt.AscendingOrder)
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
        self.table.setColumnWidth(4, 80)
        self.table.setColumnWidth(5, 95)
        self.table.setColumnWidth(6, 145)
        self.table.setColumnWidth(7, 170)
        self.table.setColumnWidth(8, 200)
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
        self.btn_load_more.setToolTip("Загрузить следующий батч по выбранному лимиту")
        self.btn_load_more.clicked.connect(self._on_load_more)
        self.btn_load_more.setEnabled(False)
        bottom_layout.addWidget(self.btn_load_more)

        self.btn_load_all = QPushButton("Загрузить все батчи")
        self.btn_load_all.setToolTip("Подряд скачать все оставшиеся пачки")
        self.btn_load_all.clicked.connect(self._on_load_all)
        self.btn_load_all.setEnabled(False)
        bottom_layout.addWidget(self.btn_load_all)

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
    def _on_search(self) -> None:
        if self.runner.is_running():
            return

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
        self._start_task(self.sidebar.search_params(), start=0, batches=1)

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
            self._start_task(self.sidebar.search_params(), start=0, batches=1)
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

    def _start_task(self, params: SearchParams, start: int, batches: int) -> None:
        self.progress.show()
        self.btn_stop.setEnabled(batches > 1)
        self.sidebar.set_controls_enabled(False)
        self.btn_load_more.setEnabled(False)
        self.btn_load_all.setEnabled(False)
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
        self.status_msg.setText("Останавливаю…")

    # --------------- клиентские фильтры
    def _on_filters_changed(self) -> None:
        self.proxy.set_filters(self.sidebar.client_filters())
        self._refresh_counter()

    def _on_reset_filters(self) -> None:
        self.sidebar.reset_client_filters()
        self.proxy.set_filters(ClientFilters())
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
                    ", ".join(str(t) for t in (p.get("tags") or [])),
                    p.get("applics_count") or 0,
                    fmt_date(parse_dt(p.get("date_end_registration"))),
                    parse_price(p.get("total_price")) or "",
                    step_id_label(p.get("step_id")),
                    p.get("id"),
                    fmt_date(parse_dt(p.get("date_published"))),
                ]
            )
        widths = [18, 28, 28, 80, 12, 12, 20, 18, 28, 10, 20]
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
        has_more = total > 0 and loaded < total
        self.btn_load_more.setEnabled(not running and has_more)
        self.btn_load_all.setEnabled(not running and has_more)
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
