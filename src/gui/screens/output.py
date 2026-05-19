"""Output screen — file list, open buttons, and a 'run again' shortcut.

The user gets here after the worker finishes successfully. We list the
generated artefacts (annexure .xlsx, main report .docx, plus any
intermediate charts the report writer dropped to disk) and provide
shortcuts to open each file or the folder containing them.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..analysis_worker import AnalysisResult


class OutputScreen(QWidget):
    """Lists generated files + offers Open / Open folder / Run another."""

    run_again_requested = pyqtSignal()
    status_message = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result: AnalysisResult | None = None
        self._build_ui()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PAD_XL, theme.PAD_L, theme.PAD_XL, theme.PAD_L)
        root.setSpacing(theme.PAD_M)

        title = QLabel("Output files")
        title.setProperty("role", "screenTitle")
        self._subtitle = QLabel("Run an analysis to see generated outputs.")
        self._subtitle.setProperty("role", "screenSubtitle")
        self._subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(self._subtitle)

        # ----- File table card -----------------------------------------
        card = QFrame()
        card.setProperty("role", "card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        cv.setSpacing(theme.PAD_S)

        ch = QLabel("Generated files")
        ch.setProperty("role", "sectionHeader")
        cv.addWidget(ch)

        self.tbl = QTableWidget(0, 4)
        theme.apply_table_palette(self.tbl)
        self.tbl.setHorizontalHeaderLabels(
            ["File", "Type", "Size", "Path"]
        )
        self.tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.tbl.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.tbl.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.tbl.itemDoubleClicked.connect(self._on_open_selected)
        cv.addWidget(self.tbl, stretch=1)

        actions = QHBoxLayout()
        self.btn_open = QPushButton("Open selected file")
        self.btn_open.clicked.connect(self._on_open_selected)
        self.btn_open_folder = QPushButton("Open output folder")
        self.btn_open_folder.clicked.connect(self._on_open_folder)
        actions.addWidget(self.btn_open)
        actions.addWidget(self.btn_open_folder)
        actions.addStretch(1)

        self.btn_run_again = QPushButton("Run another analysis")
        self.btn_run_again.setProperty("role", "primary")
        self.btn_run_again.clicked.connect(self.run_again_requested.emit)
        actions.addWidget(self.btn_run_again)

        cv.addLayout(actions)
        root.addWidget(card, stretch=1)

        # ----- QA summary footer ---------------------------------------
        self._qa_label = QLabel("")
        self._qa_label.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;"
        )
        self._qa_label.setWordWrap(True)
        root.addWidget(self._qa_label)

    # ---------------------------------------------------------- public API
    def set_result(self, result: AnalysisResult) -> None:
        self._result = result

        proj_name = getattr(result.project, "project_name", "") or "(unnamed)"
        out_dir = (
            result.annexure_path.parent
            if result.annexure_path else Path(".")
        )
        self._subtitle.setText(
            f"{proj_name} — outputs written to "
            f"<a href='{out_dir.as_uri()}'>{out_dir}</a>"
        )
        self._subtitle.setTextFormat(Qt.TextFormat.RichText)
        self._subtitle.setOpenExternalLinks(True)
        self._subtitle.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )

        self.tbl.setRowCount(0)
        self._add_file_row(result.annexure_path, "Annexure (Excel)")
        if result.docx_path:
            self._add_file_row(result.docx_path, "Main report (Word)")

        # Look for extra files in the same folder (chart PNGs, etc).
        if result.annexure_path:
            for sib in sorted(out_dir.iterdir()):
                if sib.is_dir():
                    continue
                # Skip the two we've already added.
                if sib == result.annexure_path or sib == result.docx_path:
                    continue
                if sib.suffix.lower() not in (".png", ".jpg", ".jpeg", ".csv"):
                    continue
                self._add_file_row(sib, _file_kind(sib))

        # QA summary
        fr = result.flag_report
        if fr is not None:
            self._qa_label.setText(f"QA: {fr.summary}")

    def reset(self) -> None:
        self._result = None
        self.tbl.setRowCount(0)
        self._subtitle.setText("Run an analysis to see generated outputs.")
        self._qa_label.setText("")

    # ----------------------------------------------------------- internals
    def _add_file_row(self, path: Path | None, kind: str) -> None:
        if path is None:
            return
        if not path.exists():
            return
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        size_str = _human_size(path.stat().st_size)
        for c, text in enumerate(
            (path.name, kind, size_str, str(path.resolve()))
        ):
            item = theme.themed_item(text)
            if c == 3:
                item.setToolTip(text)
            self.tbl.setItem(r, c, item)

    def _on_open_selected(self) -> None:
        row = self.tbl.currentRow()
        if row < 0:
            self.status_message.emit("Pick a file first.")
            return
        path = Path(self.tbl.item(row, 3).text())
        _open_with_default(path)

    def _on_open_folder(self) -> None:
        if self._result and self._result.annexure_path:
            _open_with_default(self._result.annexure_path.parent)
        else:
            self.status_message.emit("No output folder yet.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    for unit in ("KB", "MB", "GB"):
        num_bytes /= 1024.0
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
    return f"{num_bytes:.1f} TB"


def _file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "Chart (PNG)",
        ".jpg": "Image (JPG)",
        ".jpeg": "Image (JPG)",
        ".csv": "Data (CSV)",
        ".xlsx": "Spreadsheet (Excel)",
        ".docx": "Document (Word)",
    }.get(ext, "File")


def _open_with_default(path: Path) -> None:
    """Open ``path`` (file or folder) with the OS-default application."""
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))                              # noqa: S606
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)     # noqa: S603, S607
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:                                            # noqa: BLE001
        # Fall back to Qt's url opener.
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
