"""Делегат для колонки «быстрые действия»: две иконки в одной ячейке."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem


class QuickActionsDelegate(QStyledItemDelegate):
    """Рисует две стандартные пиктограммы по центру строки."""

    ICON_SIZE = 20

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        painter.save()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        painter.restore()

        style = widget.style() if widget is not None else QApplication.style()
        px1 = style.standardPixmap(
            QStyle.StandardPixmap.SP_ArrowForward,
            None,
            widget,
        )
        px2 = style.standardPixmap(
            QStyle.StandardPixmap.SP_CommandLink,
            None,
            widget,
        )

        iz = max(14, min(self.ICON_SIZE, option.rect.height() - 4))
        p1 = px1.scaled(iz, iz, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        p2 = px2.scaled(iz, iz, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

        r = option.rect
        gap = max(6, r.width() // 12)
        total_w = p1.width() + gap + p2.width()
        x0 = r.left() + max(4, (r.width() - total_w) // 2)
        y0 = r.top() + (r.height() - p1.height()) // 2

        painter.drawPixmap(x0, y0, p1)
        painter.drawPixmap(x0 + p1.width() + gap, y0, p2)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        h = super().sizeHint(option, index).height()
        return QSize(88, max(h, self.ICON_SIZE + 8))
