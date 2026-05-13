from __future__ import annotations
APP_STYLE = """
QMainWindow {
    background-color: #060b1a;
}

QWidget {
    color: #dce4ff;
    font-family: "Segoe UI";
}

QScrollArea {
    background: transparent;
    border: 0;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

#TopBar {
    background-color: #0a1022;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    padding: 6px 0;
}

#TopBarTitle {
    font-size: 18px;
    font-weight: 700;
    color: #f8faff;
}

#TopBarSubtitle {
    font-size: 11px;
    color: #7f8cb7;
}

#PlatformSwitcher {
    background-color: #111936;
    border-radius: 14px;
    padding: 4px;
}

QPushButton#PlatformButton {
    background: transparent;
    border: 0;
    border-radius: 10px;
    padding: 10px 18px;
    color: #9ca8d7;
    font-weight: 600;
    min-height: 40px;
}

QPushButton#PlatformButton:hover {
    background-color: #18244a;
    color: #eef2ff;
}

QPushButton#PlatformButton:checked {
    background-color: #6366f1;
    color: white;
}

#FiltersToolbar {
    background-color: #10182f;
    border-radius: 16px;
    padding: 18px 20px;
}

#FilterSection {
    background: transparent;
}

QLabel#FilterSectionTitle {
    color: #7482b3;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.6px;
    text-transform: uppercase;
    padding-bottom: 6px;
}

QFrame#FilterSeparator {
    background-color: rgba(255,255,255,0.05);
    max-width: 1px;
    min-width: 1px;
    margin: 6px 10px;
}

QFrame#FilterHorizontalSeparator {
    background-color: rgba(255,255,255,0.05);
    border: 0;
    max-height: 1px;
    min-height: 1px;
    margin: 8px 0;
}

QWidget#QuickFilterBox {
    background: transparent;
    min-height: 74px;
    max-height: 74px;
}

QLabel#QuickFilterLabel {
    color: #7f8cb7;
    font-size: 11px;
    font-weight: 600;
    padding-left: 2px;
    margin-bottom: 4px;
}

QLineEdit,
QComboBox,
QDateEdit,
QSpinBox,
QDoubleSpinBox {

    background-color: #151f3f;
    border: 1px solid transparent;
    border-radius: 12px;

    padding: 0 14px;

    color: #eef2ff;

    min-height: 46px;
    max-height: 46px;

    selection-background-color: #6366f1;
    selection-color: white;
}

QLineEdit:hover,
QComboBox:hover,
QDateEdit:hover,
QSpinBox:hover,
QDoubleSpinBox:hover {

    background-color: #18254b;
    border-color: rgba(255,255,255,0.06);
}

QLineEdit:focus,
QComboBox:focus,
QDateEdit:focus,
QSpinBox:focus,
QDoubleSpinBox:focus {

    background-color: #1b2854;
    border: 1px solid #6366f1;
}

QLineEdit:disabled,
QComboBox:disabled,
QDateEdit:disabled {

    background-color: #0d1328;
    color: #677196;
}

QLineEdit#QuickSearchInput {
    min-width: 340px;
}

QComboBox {
    padding-right: 34px;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 32px;
    border: 0;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    border: 0;
    width: 0;
    height: 0;
}

QComboBox QAbstractItemView {

    background-color: #141d3d;

    border: 0;
    border-radius: 12px;

    padding: 8px;

    color: #eef2ff;

    outline: 0;

    selection-background-color: #263b78;
    selection-color: white;
}

QComboBox QAbstractItemView::item {

    min-height: 32px;
    padding: 6px 10px;

    border-radius: 8px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: #1c2955;
}

QComboBox QAbstractItemView::item:selected {
    background-color: #2d4bcf;
}

QPushButton {

    background-color: #18213f;

    border: 0;
    border-radius: 12px;

    color: #dfe6ff;

    padding: 0 20px;

    min-height: 46px;
    max-height: 46px;

    font-weight: 600;
}

QPushButton:hover {
    background-color: #202b52;
}

QPushButton:pressed {
    background-color: #162042;
}

QPushButton:disabled {

    background-color: #0d1328;
    color: #5f688c;
}

QPushButton#Primary {

    background-color: #6366f1;
    color: white;

    padding: 0 26px;

    font-weight: 700;
}

QPushButton#Primary:hover {
    background-color: #7377ff;
}

QPushButton#Primary:pressed {
    background-color: #5458d9;
}

QPushButton#Ghost {

    background-color: transparent;

    border: 1px solid rgba(255,255,255,0.06);

    color: #aeb9df;
}

QPushButton#Ghost:hover {

    background-color: #151f3f;
    border-color: rgba(255,255,255,0.10);

    color: white;
}

QPushButton#Ghost:checked {

    background-color: #263b78;
    border-color: rgba(99,102,241,0.45);

    color: white;
}

QPushButton#Danger {

    background-color: rgba(120,30,55,0.16);

    border: 1px solid rgba(255,80,120,0.20);

    color: #ff7f98;
}

QPushButton#Danger:hover {
    background-color: rgba(120,30,55,0.28);
}

QToolButton#MoreFiltersButton {

    background-color: #18213f;

    border: 0;
    border-radius: 12px;

    color: #eef2ff;

    padding: 0 18px;

    min-height: 46px;
    max-height: 46px;

    font-weight: 600;
}

QToolButton#MoreFiltersButton:hover {
    background-color: #202b52;
}

QToolButton#MoreFiltersButton:checked {

    background-color: #263b78;
    color: white;
}

QToolButton#QuickMultiSelectButton {

    background-color: #151f3f;

    border: 1px solid transparent;
    border-radius: 12px;

    color: #eef2ff;

    padding: 0 30px 0 14px;

    min-height: 46px;
    max-height: 46px;

    font-weight: 500;
    text-align: left;
}

QToolButton#QuickMultiSelectButton:hover {

    background-color: #18254b;
    border-color: rgba(255,255,255,0.06);
}

QToolButton#QuickMultiSelectButton:pressed,
QToolButton#QuickMultiSelectButton:checked {

    background-color: #1b2854;
    border: 1px solid #6366f1;
}

QToolButton#ActiveFilterChip {

    background-color: #18213f;

    border: 0;
    border-radius: 10px;

    color: #c8d2f5;

    padding: 4px 10px;

    min-height: 28px;

    font-size: 11px;
    font-weight: 500;
}

QToolButton#ActiveFilterChip:hover {

    background-color: #1f2b56;

    color: white;
}

#CacheBanner {

    background-color: #10182f;

    border: 0;

    border-left: 3px solid #6366f1;

    border-radius: 14px;

    padding: 6px;
}

QLabel#CacheBannerText {

    color: #aeb9df;
    font-size: 12px;
}

QToolButton#CacheBannerClose {

    background: transparent;

    border: 0;

    color: #7f8cb7;

    min-width: 28px;
}

QToolButton#CacheBannerClose:hover {

    background-color: #18254b;

    border-radius: 8px;

    color: white;
}

QTableView {

    background-color: #0f1733;

    alternate-background-color: #121d3f;

    border: 1px solid rgba(255,255,255,0.04);

    border-radius: 14px;

    gridline-color: rgba(255,255,255,0.03);

    selection-background-color: #263b78;
    selection-color: white;

    outline: 0;
}

QHeaderView::section {

    background-color: #141d3d;

    border: 0;
    border-right: 1px solid rgba(255,255,255,0.04);

    padding: 12px 10px;

    color: #c3cff5;

    font-weight: 700;
}

QTableView::item {

    padding: 10px;

    border-bottom: 1px solid rgba(255,255,255,0.03);
}

QTableView::item:hover {
    background-color: #18244a;
}

QTableView::item:selected {
    background-color: #263b78;
}

QMenu {

    background-color: #141d3d;

    border: 0;

    border-radius: 12px;

    padding: 6px;

    color: #eef2ff;
}

QMenu::item {

    padding: 8px 14px;

    border-radius: 8px;
}

QMenu::item:selected {
    background-color: #263b78;
}

QStatusBar {

    background-color: #0a1022;

    border-top: 1px solid rgba(255,255,255,0.04);

    color: #8f9bc9;
}

QScrollBar:vertical {

    background: transparent;

    width: 10px;

    margin: 2px;
}

QScrollBar::handle:vertical {

    background: rgba(255,255,255,0.14);

    border-radius: 5px;

    min-height: 40px;
}

QScrollBar::handle:vertical:hover {
    background: rgba(255,255,255,0.22);
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {

    background: transparent;

    height: 10px;

    margin: 2px;
}

QScrollBar::handle:horizontal {

    background: rgba(255,255,255,0.14);

    border-radius: 5px;

    min-width: 40px;
}

QScrollBar::handle:horizontal:hover {
    background: rgba(255,255,255,0.22);
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

#EmptyState {
    background: transparent;
}

#EmptyStateTitle {

    color: #eef2ff;

    font-size: 18px;

    font-weight: 700;
}

#EmptyStateText {

    color: #8b97c5;

    font-size: 12px;
}

QToolTip {

    background-color: #141d3d;

    border: 0;

    border-radius: 10px;

    padding: 6px 10px;

    color: white;
}
"""