"""Делегат для колонки «быстрые действия»: три разные системные иконки в одной ячейке."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem


class QuickActionsDelegate(QStyledItemDelegate):
    """Рисует три пиктограммы разного семейства (табличный вид / подтверждение / инфо)."""

    ICON_SIZE = 20
    GAP_RATIO = 0.08  # минимальный зазор относительно ширины ячейки

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
        px_specs = [
            QStyle.StandardPixmap.SP_FileDialogDetailedView,
            QStyle.StandardPixmap.SP_DialogApplyButton,
            QStyle.StandardPixmap.SP_MessageBoxInformation,
        ]
        raw = [
            style.standardPixmap(spx, None, widget)
            for spx in px_specs
        ]

        iz = max(14, min(self.ICON_SIZE, option.rect.height() - 4))
        scaled = [
            px.scaled(iz, iz, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            for px in raw
        ]

        r = option.rect
        gap = max(5, int(r.width() * self.GAP_RATIO))
        total_w = sum(p.width() for p in scaled) + gap * (len(scaled) - 1)
        x = r.left() + max(2, (r.width() - total_w) // 2)

        for pix in scaled:
            vy = r.top() + (r.height() - pix.height()) // 2
            painter.drawPixmap(x, vy, pix)
            x += pix.width() + gap

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        h = super().sizeHint(option, index).height()
        return QSize(132, max(h, self.ICON_SIZE + 8))
