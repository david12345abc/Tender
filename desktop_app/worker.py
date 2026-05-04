from __future__ import annotations

import traceback
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from etp_client import HARD_SERVER_LIMIT, EtpClient

from .models import ProcedureFilterProxy, ProcedureTableModel
from .params import SearchParams

class Worker(QObject):
    """Универсальный работник: выполняет одну задачу за жизнь.

    Сигналы:
        progress(str)      — сообщения о прогрессе
        session(bool, str) — результат проверки сессии
        batch(list, int, int) — загружена пачка: procedures, start, total
        error(str)         — неперехваченное исключение
        finished()         — всегда вызывается после run()
    """

    progress = Signal(str)
    session = Signal(bool, str)
    batch = Signal(list, int, int)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn: Callable[["Worker"], None]) -> None:
        super().__init__()
        self._fn = fn
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def is_stop_requested(self) -> bool:
        return self._stop

    @Slot()
    def run(self) -> None:
        try:
            self._fn(self)
        except Exception as e:
            tb = traceback.format_exc()
            self.error.emit(f"{type(e).__name__}: {e}\n{tb}")
        finally:
            self.finished.emit()


class TaskRunner(QObject):
    """Запускает `Worker` в отдельном QThread. Гарантирует корректное завершение."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._thread: Optional[QThread] = None
        self._worker: Optional[Worker] = None

    def is_running(self) -> bool:
        return self._thread is not None

    def start(
        self,
        fn: Callable[[Worker], None],
        on_progress: Optional[Callable[[str], None]] = None,
        on_session: Optional[Callable[[bool, str], None]] = None,
        on_batch: Optional[Callable[[list, int, int], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_done: Optional[Callable[[], None]] = None,
    ) -> Worker:
        if self._thread is not None:
            raise RuntimeError("Task already running")

        thread = QThread(self.parent())
        worker = Worker(fn)
        worker.moveToThread(thread)

        if on_progress:
            worker.progress.connect(on_progress)
        if on_session:
            worker.session.connect(on_session)
        if on_batch:
            worker.batch.connect(on_batch)
        if on_error:
            worker.error.connect(on_error)

        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        def _cleanup() -> None:
            self._thread = None
            self._worker = None
            if on_done:
                try:
                    on_done()
                except Exception:
                    traceback.print_exc()

        thread.finished.connect(_cleanup)
        thread.started.connect(worker.run)

        self._thread = thread
        self._worker = worker
        thread.start()
        return worker

    def request_stop(self) -> None:
        if self._worker:
            self._worker.request_stop()

    def shutdown(self, wait_ms: int = 3000) -> None:
        if self._worker:
            self._worker.request_stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(wait_ms)
        self._thread = None
        self._worker = None


# -----------------------------------------------------------------------------
# Задачи (запускаются внутри Worker)
# -----------------------------------------------------------------------------

def make_search_task(
    client: EtpClient,
    params: SearchParams,
    start: int,
    batches_left: int,
    client_filters=None,
) -> Callable[[Worker], None]:
    """Задача: запустить Chrome (если надо), подключиться, проверить сессию,
    скачать одну или несколько пачек.

    batches_left — сколько пачек подряд скачать. 1 = «одна». 9999 = «всё».
    """

    def _run(w: Worker) -> None:
        if w.is_stop_requested():
            return

        if not client.is_chrome_running():
            w.progress.emit(f"Запускаю {client.browser.label} с DevTools…")
            try:
                client.ensure_chrome(timeout=45)
            except Exception as e:
                w.error.emit(f"Не удалось запустить Chrome: {e}")
                return
        if w.is_stop_requested():
            return

        if client.driver is None:
            w.progress.emit(f"Подключаюсь к {client.browser.label} DevTools…")
            try:
                client.connect()
            except Exception as e:
                w.error.emit(f"Ошибка подключения к Chrome: {e}")
                return

        if w.is_stop_requested():
            return

        w.progress.emit("Получаю CSRF-токен…")
        try:
            client.pull_token()
        except Exception:
            pass

        if w.is_stop_requested():
            return

        cur_start = start
        loaded_this_task = 0
        accepted_this_task = 0
        total: Optional[int] = None
        pages_done = 0
        viewed_in_user_batch = 0
        last_next_start = cur_start
        last_emitted_start = cur_start
        probe_model = ProcedureTableModel()
        probe_proxy = ProcedureFilterProxy()
        probe_proxy.setSourceModel(probe_model)
        probe_filters = client_filters
        is_roseltorg = "roseltorg" in str(getattr(client, "target_host", ""))
        if client_filters is not None:
            # Для Росэлторга быстрый поиск выполняет сервер. Повторная локальная
            # фильтрация по подстроке ломает выдачу и счётчики.
            if is_roseltorg and getattr(client_filters, "quick_search", ""):
                probe_filters = replace(client_filters, quick_search="")
            probe_proxy.set_filters(probe_filters)
            set_client_filters = getattr(client, "set_client_filters", None)
            if callable(set_client_filters):
                set_client_filters(client_filters)

        while True:
            if w.is_stop_requested():
                return
            request_limit = max(1, int(params.limit or HARD_SERVER_LIMIT))
            w.progress.emit(
                f"Запрос Procedure.list: start={cur_start}, limit={request_limit}"
                + (f"  (найдено {accepted_this_task}, просмотрено {loaded_this_task}/{total})" if total else "")
            )
            res = client.fetch_page(
                start=cur_start,
                limit=request_limit,
                date_from=params.date_from or None,
                date_to=params.date_to or None,
                query=(
                    params.query
                    or (
                        getattr(client_filters, "quick_search", "")
                        if is_roseltorg and client_filters is not None
                        else ""
                    )
                    or None
                ),
                tag_id=params.tag_id,
                sort=params.sort,
                direction=params.direction,
            )
            if w.is_stop_requested():
                return
            if res.get("error"):
                err_text = str(res["error"])
                err_low = err_text.lower()
                if (
                    "no such window" in err_low
                    or "web view not found" in err_low
                    or "target window already closed" in err_low
                    or "target frame detached" in err_low
                    or "invalid session id" in err_low
                ):
                    short = (
                        "Вкладка ЭТП была закрыта в Chrome. "
                        "Открыл её заново — попробуйте ещё раз нажать «Поиск»."
                    )
                    w.error.emit(short)
                else:
                    w.error.emit(f"Сервер вернул ошибку: {err_text}")
                return
            if res.get("no_access") or res.get("no_session"):
                msg = res.get("message") or "Нет доступа / сессия не активна."
                host = str(getattr(client, "target_host", ""))
                login_hint = (
                    "В Chrome откройте Росэлторг, выполните вход через ЭЦП до конца, "
                    "затем снова нажмите «Поиск»."
                    if "roseltorg" in host
                    else "В Chrome: «Войти» → «ЕСИА + ЭП» → пройдите до конца, "
                    "затем снова нажмите «Поиск»."
                )
                w.session.emit(
                    False,
                    f"{msg}\n\n{login_hint}",
                )
                return
            procs = res.get("procedures") or []
            if total is None:
                total = int(res.get("totalCount") or 0)
            accepted = procs
            if probe_filters is not None:
                probe_model.set_rows(procs)
                accepted = []
                for i in range(probe_proxy.rowCount()):
                    src = probe_proxy.mapToSource(probe_proxy.index(i, 0))
                    row = probe_model.row_at(src.row())
                    if row is not None:
                        accepted.append(row)
            next_start = cur_start + len(procs)
            last_next_start = next_start
            if accepted:
                w.batch.emit(accepted, next_start, total or 0)
                last_emitted_start = next_start
                accepted_this_task += len(accepted)
            loaded_this_task += len(procs)
            viewed_in_user_batch += len(procs)
            pages_done += 1
            reached_user_batch = viewed_in_user_batch >= request_limit
            if not procs:
                break
            if total and next_start >= total:
                break
            if batches_left == 1 and accepted_this_task > 0 and reached_user_batch:
                # Один пользовательский батч = примерно request_limit просмотренных записей.
                # Найденные строки отдаём сразу, но продолжаем просмотр до границы батча.
                break
            if batches_left != 1 and reached_user_batch:
                viewed_in_user_batch = 0
            if batches_left != 1 and pages_done >= batches_left:
                break
            cur_start = next_start

        if last_emitted_start != last_next_start:
            w.batch.emit([], last_next_start, total or 0)
        w.session.emit(True, "Готово.")

    return _run


def make_download_documents_task(
    client: EtpClient,
    procedures: list[dict],
    output_dir: Path,
) -> Callable[[Worker], None]:
    """Задача: скачать документы по выбранным процедурам."""

    def _run(w: Worker) -> None:
        if not procedures:
            w.error.emit("Не выбраны процедуры для скачивания документов.")
            return

        if not client.is_chrome_running():
            w.progress.emit(f"Запускаю {client.browser.label} с DevTools…")
            try:
                client.ensure_chrome(timeout=45)
            except Exception as e:
                w.error.emit(f"Не удалось запустить Chrome: {e}")
                return

        if client.driver is None:
            w.progress.emit(f"Подключаюсь к {client.browser.label} DevTools…")
            try:
                client.connect()
            except Exception as e:
                w.error.emit(f"Ошибка подключения к Chrome: {e}")
                return

        results: list[dict] = []
        for index, proc in enumerate(procedures, start=1):
            if w.is_stop_requested():
                return
            registry = proc.get("registry_number") or proc.get("procedure_number") or proc.get("id")
            w.progress.emit(f"Скачиваю документы {index}/{len(procedures)}: {registry}")
            try:
                result = client.download_procedure_documents(
                    proc,
                    output_dir,
                    progress=w.progress.emit,
                )
                if w.is_stop_requested():
                    return
                results.append(result)
                w.progress.emit(
                    f"{registry}: скачано {len(result.get('saved') or [])} "
                    f"из {result.get('found') or 0} файлов"
                )
            except Exception as e:
                results.append({"procedure": registry, "saved": [], "errors": [str(e)]})
                w.progress.emit(f"{registry}: ошибка скачивания: {e}")

        saved_count = sum(len(r.get("saved") or []) for r in results)
        error_count = sum(len(r.get("errors") or []) for r in results)
        w.session.emit(
            True,
            f"Скачивание завершено. Файлов: {saved_count}, ошибок: {error_count}. "
            f"Папка: {output_dir}",
        )

    return _run
