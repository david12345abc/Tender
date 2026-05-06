from __future__ import annotations

APP_STYLE = """
QMainWindow { background-color: #f7f8fa; }

#TopBar {
    background-color: #ffffff;
    border-bottom: 1px solid #e3e5e8;
}
#TopBarTitle {
    font-size: 16px;
    font-weight: 700;
    color: #1a1d22;
}
#TopBarSubtitle {
    font-size: 11px;
    color: #6a717a;
}
#PlatformSwitcher {
    background-color: #eef0f4;
    border: 1px solid #d9dde3;
    border-radius: 8px;
}
QPushButton#PlatformButton {
    padding: 6px 12px;
    border: 0;
    border-radius: 6px;
    background-color: transparent;
    color: #4a515a;
    font-weight: 600;
}
QPushButton#PlatformButton:hover {
    background-color: #e2e6ec;
}
QPushButton#PlatformButton:checked {
    background-color: #3572e0;
    color: #ffffff;
}
QPushButton#PlatformButton:disabled {
    color: #9aa1aa;
}
#SessionBadge {
    padding: 4px 10px;
    border-radius: 10px;
    font-weight: 600;
}
#SessionBadge[ok="true"]  { background-color: #e6f6ea; color: #1f7a3a; }
#SessionBadge[ok="false"] { background-color: #fbe9e9; color: #a61b1b; }
#SessionBadge[ok="idle"]  { background-color: #eef0f4; color: #4a515a; }

#Sidebar {
    background-color: #ffffff;
    border-right: 1px solid #e3e5e8;
}
#SidebarBody {
    background-color: #ffffff;
}
#SidebarTitle {
    font-size: 13px;
    font-weight: 700;
    color: #1a1d22;
    padding: 2px 0;
}
#SidebarSection {
    color: #6a717a;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    padding-top: 8px;
}

QLabel {
    color: #1a1d22;
}

QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {
    padding: 5px 6px;
    border: 1px solid #d4d8dd;
    border-radius: 6px;
    background-color: #ffffff;
    color: #1a1d22;
    selection-background-color: #cfe0ff;
    selection-color: #1a1d22;
    min-height: 30px;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #3572e0;
}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #7b818a;
    background-color: #f3f5f7;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    width: 18px;
    background-color: #f7f8fa;
}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {
    width: 8px;
    height: 8px;
}

QPushButton {
    padding: 7px 14px;
    border: 1px solid #d4d8dd;
    border-radius: 6px;
    background-color: #ffffff;
    color: #1a1d22;
}
QPushButton:hover { background-color: #f0f3f7; }
QPushButton:disabled { color: #a2a8b0; background-color: #f5f6f8; }

QPushButton#Primary {
    background-color: #3572e0;
    border: 1px solid #2a5bc0;
    color: white;
    font-weight: 600;
}
QPushButton#Primary:hover     { background-color: #2a5bc0; }
QPushButton#Primary:disabled  { background-color: #9fbaed; border-color: #9fbaed; }

QPushButton#Danger {
    background-color: #ffffff;
    border: 1px solid #e0b4b4;
    color: #a61b1b;
}
QPushButton#Danger:hover { background-color: #fbe9e9; }

QTableView {
    background-color: #ffffff;
    alternate-background-color: #f8f9fb;
    gridline-color: #eceff3;
    selection-background-color: #cfe0ff;
    selection-color: #1a1d22;
    border: 1px solid #e3e5e8;
}
QHeaderView::section {
    background-color: #f0f3f7;
    padding: 6px 8px;
    border: 0px;
    border-right: 1px solid #e3e5e8;
    border-bottom: 1px solid #e3e5e8;
    font-weight: 600;
    color: #3a4048;
}
QTableView::item { padding: 4px 8px; }

QMenu {
    background-color: #ffffff;
    color: #1a1d22;
    border: 1px solid #d4d8dd;
}
QMenu::item {
    padding: 6px 24px;
    color: #1a1d22;
    background-color: transparent;
}
QMenu::item:selected {
    background-color: #cfe0ff;
    color: #1a1d22;
}
QMenu::item:disabled {
    color: #9aa1aa;
}
QMenu::separator {
    height: 1px;
    background-color: #e3e5e8;
    margin: 4px 8px;
}

QStatusBar { background: #ffffff; border-top: 1px solid #e3e5e8; }
QProgressBar {
    border: 1px solid #d4d8dd;
    border-radius: 5px;
    background-color: #ffffff;
    text-align: center;
    height: 14px;
}
QProgressBar::chunk { background-color: #3572e0; border-radius: 4px; }

QCheckBox { spacing: 5px; }

QToolTip {
    background-color: #2c2f36;
    color: #ffffff;
    border: 1px solid #2c2f36;
    padding: 4px 6px;
    border-radius: 4px;
}

#FilterLabel {
    color: #3a4048;
    font-size: 11px;
    font-weight: 600;
    margin-top: 4px;
}

#BottomBar {
    background-color: #ffffff;
    border: 1px solid #e3e5e8;
    border-radius: 8px;
}
"""
