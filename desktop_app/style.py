from __future__ import annotations

APP_STYLE = """
QMainWindow { background-color: #060b1a; }
QWidget { color: #dce4ff; }
QScrollArea { background: #060b1a; border: 0; }
QScrollArea > QWidget > QWidget { background: #060b1a; }

#TopBar {
    background-color: #0b1024;
    border-bottom: 1px solid #1a2442;
}
#TopBarTitle {
    font-size: 17px;
    font-weight: 700;
    color: #f3f6ff;
}
#TopBarSubtitle {
    font-size: 11px;
    color: #8290bd;
}
#PlatformSwitcher {
    background-color: #0e1430;
    border: 1px solid #1d2b53;
    border-radius: 8px;
}
QPushButton#PlatformButton {
    padding: 10px 18px;
    border: 0;
    border-radius: 6px;
    background-color: transparent;
    color: #9ca8d7;
    font-weight: 600;
    min-height: 28px;
}
QPushButton#PlatformButton:hover {
    background-color: #171f43;
}
QPushButton#PlatformButton:checked {
    background-color: #3730a3;
    color: #ffffff;
}
QPushButton#PlatformButton:disabled {
    color: #4c567c;
}
#SessionBadge {
    padding: 7px 12px;
    border-radius: 8px;
    font-weight: 600;
    border: 1px solid #1e2b53;
}
#SessionBadge[ok="true"]  { background-color: #092618; color: #3ee78a; border-color: #164b31; }
#SessionBadge[ok="false"] { background-color: #301018; color: #ff7088; border-color: #5a2132; }
#SessionBadge[ok="idle"]  { background-color: #101735; color: #a7b3e5; }

#Sidebar {
    background-color: #0e1430;
    border: 1px solid #1b2850;
    border-radius: 8px;
}
#SidebarBody {
    background-color: #0e1430;
}
#SidebarTitle {
    font-size: 13px;
    font-weight: 700;
    color: #f3f6ff;
    padding: 2px 0;
}
#SidebarSection {
    color: #7f8ab7;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
    padding-top: 8px;
}
#ExtraFiltersPanel {
    background-color: #0b1024;
    border: 1px solid #1a2442;
    border-radius: 8px;
}
#ExtraFiltersBody {
    background-color: #0b1024;
}
#ExtraFiltersTitle {
    color: #f3f6ff;
    font-size: 13px;
    font-weight: 700;
}
QToolButton#ExtraFiltersClose {
    border: 0;
    color: #8f9bc9;
    background: transparent;
    font-size: 18px;
    padding: 2px 8px;
}
QToolButton#ExtraFiltersClose:hover {
    color: #ffffff;
    background-color: #182246;
    border-radius: 6px;
}
QToolButton#MoreFiltersButton {
    border: 1px solid #27365f;
    border-radius: 7px;
    background-color: #121936;
    color: #dfe6ff;
    padding: 7px 12px;
}
QToolButton#MoreFiltersButton:checked {
    background-color: #10213d;
    border-color: #3b82f6;
}

QLabel {
    color: #dce4ff;
}

QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {
    padding: 7px 9px;
    border: 1px solid #27365f;
    border-radius: 6px;
    background-color: #111827;
    color: #e9edff;
    selection-background-color: #1d4ed8;
    selection-color: #ffffff;
    min-height: 32px;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #3b82f6;
}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: #677196;
    background-color: #0a0f23;
    border-color: #1a2442;
}
QComboBox {
    padding-right: 28px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid #1e293b;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
    background: #0f172a;
}
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #94a3b8;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background-color: #0b1020;
    color: #dbeafe;
    border: 1px solid #24324f;
    border-radius: 8px;
    padding: 6px;
    outline: 0;
    selection-background-color: #1e3a8a;
    selection-color: #ffffff;
}
QComboBox QAbstractItemView::item {
    min-height: 26px;
    padding: 5px 8px;
    border-radius: 5px;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #172554;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #1d4ed8;
    color: #ffffff;
}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    width: 18px;
    background-color: #0f172a;
    border-left: 1px solid #1e293b;
}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {
    width: 8px;
    height: 8px;
}

QPushButton {
    padding: 8px 14px;
    border: 1px solid #27365f;
    border-radius: 7px;
    background-color: #121936;
    color: #dfe6ff;
    min-height: 30px;
}
QPushButton:hover { background-color: #19234b; border-color: #3b4d84; }
QPushButton:disabled { color: #5f688c; background-color: #0c1228; border-color: #182343; }

QPushButton#Primary {
    background-color: #4f46e5;
    border: 1px solid #5b55f6;
    color: white;
    font-weight: 600;
}
QPushButton#Primary:hover     { background-color: #6256ff; }
QPushButton#Primary:disabled  { background-color: #1d2552; border-color: #27315c; color: #667098; }

QPushButton#Danger {
    background-color: #22122b;
    border: 1px solid #6a2540;
    color: #ff6b83;
}
QPushButton#Danger:hover { background-color: #33172a; }

QTableView {
    background-color: #070d20;
    alternate-background-color: #0a1128;
    gridline-color: #141f3b;
    selection-background-color: #263168;
    selection-color: #ffffff;
    border: 1px solid #18264b;
    border-radius: 8px;
    outline: 0;
}
QHeaderView::section {
    background-color: #101735;
    padding: 10px 10px;
    border: 0px;
    border-right: 1px solid #1a2442;
    border-bottom: 1px solid #1a2442;
    font-weight: 600;
    color: #b9c4ee;
}
QTableView::item {
    padding: 8px 10px;
    border-bottom: 1px solid #111a32;
}
QTableView::item:selected {
    background-color: #263168;
    color: #ffffff;
}
QTableCornerButton::section {
    background-color: #101735;
    border: 0;
    border-right: 1px solid #1a2442;
    border-bottom: 1px solid #1a2442;
}

QMenu {
    background-color: #0e1430;
    color: #dce4ff;
    border: 1px solid #27365f;
}
QMenu::item {
    padding: 6px 24px;
    color: #dce4ff;
    background-color: transparent;
}
QMenu::item:selected {
    background-color: #263168;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #5f688c;
}
QMenu::separator {
    height: 1px;
    background-color: #1a2442;
    margin: 4px 8px;
}

QStatusBar { background: #070d20; border-top: 1px solid #1a2442; color: #9ca8d7; }
QProgressBar {
    border: 1px solid #27365f;
    border-radius: 5px;
    background-color: #0b1024;
    text-align: center;
    height: 14px;
    color: #dce4ff;
}
QProgressBar::chunk { background-color: #4f46e5; border-radius: 4px; }

QCheckBox { spacing: 7px; color: #dce4ff; }
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #33436e;
    border-radius: 3px;
    background: #0b1024;
}
QCheckBox::indicator:checked {
    background: #2563eb;
    border-color: #3b82f6;
}

QListWidget#StatusPopupList {
    background-color: #0b1020;
    color: #dbeafe;
    border: 0;
    outline: 0;
    padding: 4px;
}
QListWidget#StatusPopupList::item {
    min-height: 28px;
    padding: 5px 8px;
    border-radius: 6px;
}
QListWidget#StatusPopupList::item:hover {
    background-color: #172554;
}
QListWidget#StatusPopupList::item:selected {
    background-color: #1e3a8a;
    color: #ffffff;
}
#StatusPopup {
    background-color: #0b1020;
    border: 1px solid #24324f;
    border-radius: 10px;
}
QToolButton#StatusMultiSelectButton {
    text-align: left;
    border: 1px solid #27365f;
    border-radius: 6px;
    background-color: #111827;
    color: #e9edff;
    padding: 7px 10px;
    min-height: 34px;
}
QToolButton#StatusMultiSelectButton:hover {
    border-color: #3b82f6;
    background-color: #132033;
}
#StatusChips {
    color: #bfdbfe;
    background-color: transparent;
    font-size: 10px;
}

QScrollBar:vertical {
    background: #0b1020;
    width: 10px;
    margin: 3px 2px 3px 2px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #334155;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #475569;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
    width: 0;
}
QScrollBar:horizontal {
    background: #0b1020;
    height: 10px;
    margin: 2px 3px 2px 3px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #334155;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background: #475569;
}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    height: 0;
    width: 0;
}

QToolTip {
    background-color: #111934;
    color: #ffffff;
    border: 1px solid #27365f;
    padding: 4px 6px;
    border-radius: 4px;
}

#FilterLabel {
    color: #a5b0da;
    font-size: 11px;
    font-weight: 600;
    margin-top: 4px;
}

#BottomBar {
    background-color: #0b1024;
    border: 1px solid #1a2442;
    border-radius: 8px;
}

#ActionsBar {
    background-color: #0b1024;
    border: 1px solid #1a2442;
    border-radius: 8px;
}

#EmptyState {
    background: transparent;
}
#EmptyStateTitle {
    color: #dfe6ff;
    font-size: 13px;
    font-weight: 700;
}
#EmptyStateText {
    color: #7f8ab7;
    font-size: 11px;
}
"""
