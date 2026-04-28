from __future__ import annotations

import sys
import traceback

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from .constants import APP_TITLE
from .main_window import MainWindow
from .style import APP_STYLE


def _install_global_excepthook() -> None:
    """Глобальный перехват исключений: UI не должен закрываться из-за них."""

    def hook(exc_type, exc, tb) -> None:
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        print(msg, file=sys.stderr)
        try:
            QMessageBox.critical(None, "Необработанная ошибка", msg)
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    _install_global_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(True)

    font = QFont()
    font.setFamily("Segoe UI")
    font.setPointSize(9)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f7f8fa"))
    palette.setColor(QPalette.WindowText, QColor("#1a1d22"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f8f9fb"))
    palette.setColor(QPalette.Text, QColor("#1a1d22"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#1a1d22"))
    palette.setColor(QPalette.Highlight, QColor("#cfe0ff"))
    palette.setColor(QPalette.HighlightedText, QColor("#1a1d22"))
    palette.setColor(QPalette.ToolTipBase, QColor("#2c2f36"))
    palette.setColor(QPalette.ToolTipText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(APP_STYLE)

    win = MainWindow()
    win.show()
    return app.exec()
