"""Centralised colour + spacing tokens for the Athena ILI FFP Tool GUI.

Kept here (rather than scattered through Qt stylesheets) so a future
re-theme or dark/light toggle only touches one file.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                                # pragma: no cover
    from PyQt6.QtWidgets import QTableView, QTableWidget

# ---------------------------------------------------------------------------
# Brand / palette
# ---------------------------------------------------------------------------

# Sidebar (dark)
COLOR_SIDEBAR_BG = "#1F2D3D"
COLOR_SIDEBAR_FG = "#ECF0F1"
COLOR_SIDEBAR_FG_MUTED = "#90A4AE"
COLOR_SIDEBAR_HOVER = "#34495E"
COLOR_SIDEBAR_ACTIVE_BG = "#3498DB"
COLOR_SIDEBAR_ACTIVE_FG = "#FFFFFF"

# Content background + cards
COLOR_BG = "#F4F6F8"
COLOR_CARD_BG = "#FFFFFF"
COLOR_CARD_BORDER = "#E1E5EA"

# Text
COLOR_TEXT = "#2C3E50"
COLOR_TEXT_MUTED = "#7F8C8D"
COLOR_TEXT_INVERSE = "#FFFFFF"

# Status / semantic
COLOR_PRIMARY = "#2C7BE5"
COLOR_PRIMARY_HOVER = "#1A5FBF"
COLOR_SUCCESS = "#27AE60"
COLOR_WARNING = "#E67E22"
COLOR_ERROR = "#C0392B"
COLOR_INFO = "#3498DB"

# Inputs
COLOR_INPUT_BG = "#FFFFFF"
COLOR_INPUT_BORDER = "#CED4DA"
COLOR_INPUT_BORDER_FOCUS = "#2C7BE5"


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

SIDEBAR_WIDTH = 200
MAIN_WINDOW_MIN_W = 1100
MAIN_WINDOW_MIN_H = 750

PAD_S = 6
PAD_M = 12
PAD_L = 20
PAD_XL = 32

RADIUS_S = 4
RADIUS_M = 8


# ---------------------------------------------------------------------------
# Global QSS for the application
# ---------------------------------------------------------------------------

def apply_application_palette(app) -> None:
    """Set a known-good QApplication palette as a fallback for QSS gaps.

    Qt's Fusion style honours QPalette colours where QSS rules don't
    apply. Without setting an app-level palette explicitly, Fusion
    inherits from the OS theme — on Win11 light mode this is mostly
    fine, but on Win11 dark mode (which many engineers run) the
    inherited palette has WHITE text, which then bleeds into widgets
    we want to render dark-on-white (form labels, table cells,
    checkbox text).
    """
    from PyQt6.QtGui import QColor, QPalette

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,           QColor(COLOR_BG))
    pal.setColor(QPalette.ColorRole.WindowText,       QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Base,             QColor(COLOR_CARD_BG))
    pal.setColor(QPalette.ColorRole.AlternateBase,    QColor("#F8FAFC"))
    pal.setColor(QPalette.ColorRole.ToolTipBase,      QColor("#FFFFE0"))
    pal.setColor(QPalette.ColorRole.ToolTipText,      QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Text,             QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.PlaceholderText,  QColor(COLOR_TEXT_MUTED))
    pal.setColor(QPalette.ColorRole.Button,           QColor(COLOR_CARD_BG))
    pal.setColor(QPalette.ColorRole.ButtonText,       QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.BrightText,       QColor(COLOR_ERROR))
    pal.setColor(QPalette.ColorRole.Link,             QColor(COLOR_PRIMARY))
    pal.setColor(QPalette.ColorRole.Highlight,        QColor(COLOR_PRIMARY))
    pal.setColor(QPalette.ColorRole.HighlightedText,  QColor(COLOR_TEXT_INVERSE))
    # Disabled-state colours so greyed-out widgets are still readable.
    pal.setColor(QPalette.ColorGroup.Disabled,
                 QPalette.ColorRole.WindowText,       QColor(COLOR_TEXT_MUTED))
    pal.setColor(QPalette.ColorGroup.Disabled,
                 QPalette.ColorRole.Text,             QColor(COLOR_TEXT_MUTED))
    pal.setColor(QPalette.ColorGroup.Disabled,
                 QPalette.ColorRole.ButtonText,       QColor(COLOR_TEXT_MUTED))
    app.setPalette(pal)


def themed_item(text: str = ""):
    """Build a QTableWidgetItem with the theme's text colour pre-set.

    QTableWidgetItem ignores the table's QSS / QPalette colour rules
    in Fusion style — the only reliable way to ensure cells render
    with visible text is to set the foreground brush per-item.
    """
    from PyQt6.QtGui import QBrush, QColor
    from PyQt6.QtWidgets import QTableWidgetItem
    item = QTableWidgetItem(text)
    item.setForeground(QBrush(QColor(COLOR_TEXT)))
    return item


def apply_table_palette(table: "QTableWidget | QTableView") -> None:
    """Force a QTableWidget / QTableView's QPalette to use the theme colours.

    Workaround for a long-standing Qt-Fusion-style interaction: even
    when QSS includes ``QTableWidget::item { color: ... }`` rules, the
    Fusion style sometimes paints cell text using the underlying
    QPalette's ColorRole.Text instead — which on Win11 light-mode +
    Fusion ends up near-white, making MAOP cell values invisible.

    Setting the palette directly on the widget always wins, regardless
    of style. Call this on every table widget you create.
    """
    # Local imports — keep theme.py cheap to load on machines without Qt.
    from PyQt6.QtGui import QColor, QPalette

    pal = table.palette()
    pal.setColor(QPalette.ColorRole.Base,            QColor(COLOR_CARD_BG))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor("#F8FAFC"))
    pal.setColor(QPalette.ColorRole.Text,            QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(COLOR_TEXT_INVERSE))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(COLOR_PRIMARY))
    table.setPalette(pal)
    # The viewport (the inner widget the items are actually drawn on)
    # uses the same palette in most styles, but setting it explicitly
    # too is harmless and removes one more variable.
    vp = table.viewport()
    if vp is not None:
        vp.setPalette(pal)


def application_stylesheet() -> str:
    """Return the global QSS string applied to the QApplication.

    Qt QSS gotcha: unlike CSS in a browser, `color` does NOT cascade to
    child widgets. A rule on QMainWindow only colours QMainWindow's own
    text — child QLabels / QCheckBoxes / QTableWidget cells fall back
    to the system palette, which on Windows 11 with Fusion + the user's
    light theme renders as near-white-on-white (invisible).

    Every label-bearing widget therefore needs an explicit `color`
    rule below. More-specific [role=…] / objectName-targeted rules
    further down can still override these defaults for sidebars,
    section headers, status messages, etc.
    """
    return f"""
    QMainWindow, QWidget#contentRoot {{
        background-color: {COLOR_BG};
        color: {COLOR_TEXT};
    }}

    /* ----- Default text colour for label-bearing widgets ----------
     * These are the rules whose absence caused the Prompt 24 bug.
     * Keep them at TOP of the QSS so the more-specific rules below
     * (sidebar, sectionHeader, screenTitle, etc.) can override.
     */
    QLabel,
    QCheckBox,
    QRadioButton,
    QGroupBox {{
        color: {COLOR_TEXT};
        background-color: transparent;
    }}
    QGroupBox::title {{
        color: {COLOR_TEXT};
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }}
    QToolTip {{
        color: {COLOR_TEXT};
        background-color: #FFFFE0;
        border: 1px solid {COLOR_INPUT_BORDER};
        padding: 4px 6px;
    }}

    /* ----- Sidebar ----- */
    QWidget#sidebar {{
        background-color: {COLOR_SIDEBAR_BG};
    }}
    QLabel#sidebarBrand {{
        color: {COLOR_TEXT_INVERSE};
        font-size: 16px;
        font-weight: 600;
        padding: 18px 16px 4px 16px;
    }}
    QLabel#sidebarTagline {{
        color: {COLOR_SIDEBAR_FG_MUTED};
        font-size: 10px;
        padding: 0 16px 14px 16px;
    }}
    QPushButton[role="navButton"] {{
        background-color: transparent;
        color: {COLOR_SIDEBAR_FG};
        border: none;
        text-align: left;
        padding: 12px 18px;
        font-size: 13px;
    }}
    QPushButton[role="navButton"]:hover {{
        background-color: {COLOR_SIDEBAR_HOVER};
    }}
    QPushButton[role="navButton"][active="true"] {{
        background-color: {COLOR_SIDEBAR_ACTIVE_BG};
        color: {COLOR_SIDEBAR_ACTIVE_FG};
        font-weight: 600;
    }}
    QLabel#sidebarVersion {{
        color: {COLOR_SIDEBAR_FG_MUTED};
        font-size: 10px;
        padding: 8px 16px;
    }}

    /* ----- Content panes ----- */
    QLabel[role="screenTitle"] {{
        font-size: 22px;
        font-weight: 600;
        color: {COLOR_TEXT};
        padding-bottom: 4px;
    }}
    QLabel[role="screenSubtitle"] {{
        font-size: 12px;
        color: {COLOR_TEXT_MUTED};
        padding-bottom: 12px;
    }}
    QLabel[role="sectionHeader"] {{
        font-size: 13px;
        font-weight: 600;
        color: {COLOR_TEXT};
        padding-top: 8px;
    }}

    /* ----- Cards ----- */
    QFrame[role="card"] {{
        background-color: {COLOR_CARD_BG};
        border: 1px solid {COLOR_CARD_BORDER};
        border-radius: {RADIUS_M}px;
    }}

    /* ----- Inputs ----- */
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTextEdit {{
        background-color: {COLOR_INPUT_BG};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_INPUT_BORDER};
        border-radius: {RADIUS_S}px;
        padding: 4px 6px;
        selection-background-color: {COLOR_PRIMARY};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
    QDateEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
        border: 1px solid {COLOR_INPUT_BORDER_FOCUS};
    }}

    /* ----- Buttons ----- */
    QPushButton {{
        background-color: {COLOR_CARD_BG};
        color: {COLOR_TEXT};
        border: 1px solid {COLOR_INPUT_BORDER};
        border-radius: {RADIUS_S}px;
        padding: 6px 14px;
        font-size: 12px;
    }}
    QPushButton:hover {{
        background-color: #ECF0F1;
    }}
    QPushButton:disabled {{
        color: {COLOR_TEXT_MUTED};
        background-color: #F0F2F4;
    }}
    QPushButton[role="primary"] {{
        background-color: {COLOR_PRIMARY};
        color: {COLOR_TEXT_INVERSE};
        border: 1px solid {COLOR_PRIMARY};
        font-weight: 600;
        padding: 8px 20px;
    }}
    QPushButton[role="primary"]:hover {{
        background-color: {COLOR_PRIMARY_HOVER};
        border: 1px solid {COLOR_PRIMARY_HOVER};
    }}
    QPushButton[role="primary"]:disabled {{
        background-color: #9AA9B8;
        border: 1px solid #9AA9B8;
        color: #FFFFFF;
    }}

    /* ----- Progress bar ----- */
    QProgressBar {{
        background-color: #E9ECEF;
        border: none;
        border-radius: {RADIUS_S}px;
        text-align: center;
        color: {COLOR_TEXT};
        height: 18px;
    }}
    QProgressBar::chunk {{
        background-color: {COLOR_PRIMARY};
        border-radius: {RADIUS_S}px;
    }}

    /* ----- Status bar ----- */
    QStatusBar {{
        background-color: {COLOR_CARD_BG};
        color: {COLOR_TEXT_MUTED};
        border-top: 1px solid {COLOR_CARD_BORDER};
    }}

    /* ----- Tables -----
     * Without an explicit `color` on QTableWidget AND on ::item, cell
     * text renders in the system-palette default — which on Win11 +
     * Fusion + light mode is near-white-on-white. The ::item rule is
     * required because cell text is painted from item-data, not the
     * widget's own text channel.
     */
    QTableWidget, QTableView {{
        background-color: {COLOR_CARD_BG};
        color: {COLOR_TEXT};
        gridline-color: {COLOR_CARD_BORDER};
        border: 1px solid {COLOR_CARD_BORDER};
        border-radius: {RADIUS_S}px;
        alternate-background-color: #F8FAFC;
        selection-background-color: {COLOR_PRIMARY};
        selection-color: {COLOR_TEXT_INVERSE};
    }}
    QTableWidget::item, QTableView::item {{
        color: {COLOR_TEXT};
        padding: 4px 6px;
    }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background-color: {COLOR_PRIMARY};
        color: {COLOR_TEXT_INVERSE};
    }}
    QHeaderView::section {{
        background-color: #F0F2F4;
        color: {COLOR_TEXT};
        padding: 6px;
        border: none;
        border-right: 1px solid {COLOR_CARD_BORDER};
        border-bottom: 1px solid {COLOR_CARD_BORDER};
        font-weight: 600;
    }}

    /* ----- Tabs ----- */
    QTabWidget::pane {{
        border: 1px solid {COLOR_CARD_BORDER};
        border-radius: {RADIUS_S}px;
        background: {COLOR_CARD_BG};
    }}
    QTabBar::tab {{
        background: transparent;
        color: {COLOR_TEXT_MUTED};
        padding: 8px 16px;
        border: none;
    }}
    QTabBar::tab:selected {{
        color: {COLOR_PRIMARY};
        border-bottom: 2px solid {COLOR_PRIMARY};
        font-weight: 600;
    }}
    """
