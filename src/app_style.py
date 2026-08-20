APP_STYLESHEET = """
QMainWindow {
    background: #f4f6f8;
}

QDialog#methodConfigDialog {
    background: #eef2f6;
}

QToolBar#mainToolbar {
    background: #ffffff;
    border: 0;
    border-bottom: 1px solid #d8dee6;
    spacing: 6px;
    padding: 8px 10px;
}

QToolBar#mainToolbar QToolButton,
QToolBar#graphToolbar QToolButton,
QPushButton {
    background: #ffffff;
    border: 1px solid #c7d0da;
    border-radius: 7px;
    color: #243241;
    padding: 6px 12px;
}

QToolBar#mainToolbar QToolButton:hover,
QToolBar#graphToolbar QToolButton:hover,
QPushButton:hover {
    background: #eef4fa;
    border-color: #8ca3ba;
}

QToolBar#mainToolbar QToolButton:pressed,
QToolBar#graphToolbar QToolButton:pressed,
QToolBar#graphToolbar QToolButton:checked,
QPushButton:pressed {
    background: #dce8f3;
}

QToolBar#graphToolbar QToolButton#channelRunButton[configured="true"] {
    background: #e5f4e9;
    border-color: #91c69d;
    color: #245b31;
}

QToolBar#graphToolbar QToolButton#channelRunButton[configured="true"]:hover {
    background: #d7eddd;
    border-color: #70af7e;
}

QToolBar#graphToolbar QToolButton#channelEditButton[configured="true"] {
    background: #e4f0fa;
    border-color: #8eb9da;
    color: #245779;
}

QToolBar#graphToolbar QToolButton#channelEditButton[configured="true"]:hover {
    background: #d7e9f7;
    border-color: #70a5cd;
}

QToolBar#graphToolbar QToolButton#channelStopButton:enabled {
    background: #f9e5e5;
    border-color: #d79a9a;
    color: #7a2929;
}

QToolBar#graphToolbar QToolButton#channelStopButton:enabled:hover {
    background: #f3d7d7;
    border-color: #c77878;
}

QToolBar#graphToolbar QToolButton#channelRunButton[configured="true"]:disabled,
QToolBar#graphToolbar QToolButton#channelEditButton[configured="true"]:disabled {
    color: #8a96a3;
    background: #f5f7f9;
    border-color: #d8dee6;
}

QToolButton:disabled,
QPushButton:disabled {
    color: #8a96a3;
    background: #f5f7f9;
    border-color: #d8dee6;
}

QWidget#panelContainer {
    background: #f4f6f8;
}

QFrame#graphPanel {
    background: #ffffff;
    border: 1px solid #d8dee6;
    border-radius: 8px;
}

QFrame#graphPanel[selected="true"] {
    border: 2px solid #8fb9dc;
}

QFrame#auroraOptionsCard {
    background: #ffffff;
    border: 1px solid #d8dee6;
    border-radius: 12px;
}

QFrame#customNamingCard {
    background: #f8fafc;
    border: 1px solid #e1e7ee;
    border-radius: 8px;
}

QLabel#graphPanelTitle {
    color: #1f2a36;
    font-size: 14px;
    font-weight: 700;
}

QLabel#graphHoverCoordinates {
    background: #ffffff;
    border: 1px solid #aeb9c5;
    border-radius: 4px;
    color: #243241;
    padding: 3px 6px;
}

QLabel#auroraCardTitle {
    color: #1f2a36;
    font-size: 13px;
    font-weight: 700;
}

QLabel#auroraHelpText {
    color: #52606d;
}

QLabel#filenamePreview {
    color: #52606d;
    font-family: Consolas, monospace;
}

QCheckBox {
    spacing: 6px;
}

QToolBar#graphToolbar {
    background: transparent;
    border: 0;
    spacing: 4px;
}

QWidget#expandedDataControls {
    background: #f8fafc;
    border: 1px solid #d8dee6;
    border-radius: 7px;
    padding: 5px 7px;
}

QToolButton#nyquistButton {
    background: #ffffff;
    border: 1px solid #c7d0da;
    border-radius: 7px;
    color: #243241;
    padding: 6px 12px;
}

QToolButton#nyquistButton:hover {
    background: #eef4fa;
    border-color: #8ca3ba;
}

QToolButton#nyquistButton:checked {
    background: #e8e2f7;
    border-color: #9b87cf;
    color: #4d3485;
}

QScrollArea#panelScrollArea {
    background: #f4f6f8;
    border: 0;
}

QLabel#connectionIndicator {
    font-weight: 600;
    padding: 2px 8px;
}

QLabel#channelStatus {
    color: #334155;
    padding: 2px 8px;
}

QListWidget,
QComboBox,
QLineEdit {
    background: #ffffff;
    border: 1px solid #c7d0da;
    border-radius: 7px;
    padding: 6px 8px;
    selection-background-color: #2f6f9f;
}

QComboBox QAbstractItemView,
QAbstractItemView,
QListView {
    background: #ffffff;
    border: 1px solid #c7d0da;
    color: #1f2a36;
    outline: 0;
    selection-background-color: #2f6f9f;
    selection-color: #ffffff;
}

QAbstractItemView::item,
QListView::item {
    background: #ffffff;
    color: #1f2a36;
    min-height: 24px;
    padding: 4px 8px;
}

QAbstractItemView::item:hover,
QListView::item:hover {
    background: #edf3f8;
    color: #1f2a36;
}

QStatusBar {
    background: #ffffff;
    border-top: 1px solid #d8dee6;
}
"""
