"""Main application window for the Athena ILI FFP Tool GUI.

Layout:
    +------------+--------------------------------------------+
    | Sidebar    |  Screen content (QStackedWidget)           |
    |  - Project |                                            |
    |  - Run     |                                            |
    |  - Results |                                            |
    |  - Output  |                                            |
    |            |                                            |
    | v0.1.0     |                                            |
    +------------+--------------------------------------------+
    |  Status bar (one-line current-state message)            |
    +-------------------------------------------------------- +

Sidebar navigation buttons are enabled progressively:
  * "Project Setup" is always enabled.
  * "Run Analysis" enables once a valid project is loaded.
  * "Results" / "Output" enable once an analysis finishes successfully.

Closing the window while a worker is running prompts to confirm.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .analysis_worker import AnalysisJob, AnalysisResult
from .icon import build_app_icon
from .screens import (
    FormatConverterScreen,
    OutputScreen,
    ProjectSetupScreen,
    ResultsScreen,
    RunAnalysisScreen,
)


# Sidebar navigation entries (label, screen-index).
#
# Project Setup is the DEFAULT entry point — most files Athena
# processes are already in NGP/Athena format, so the typical flow is
# Browse → file accepted silently → fill in metadata → Proceed. The
# Convert Format screen sits below it as a tool for the minority case
# where Run-1 is from a foreign vendor (Rosen / Baker Hughes / NDT
# Global / Onstream).
_NAV_ITEMS = (
    ("Project Setup",  0),
    ("Convert Format", 1),
    ("Run Analysis",   2),
    ("Results",        3),
    ("Output",         4),
)

# Stack indices (kept as constants so the gating logic reads clearly).
IDX_PROJECT = 0
IDX_FORMAT = 1
IDX_RUN = 2
IDX_RESULTS = 3
IDX_OUTPUT = 4


class MainWindow(QMainWindow):
    """Top-level window orchestrating the four screens."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Athena ILI FFP Tool")
        self.setWindowIcon(build_app_icon())
        self.setMinimumSize(theme.MAIN_WINDOW_MIN_W, theme.MAIN_WINDOW_MIN_H)

        # Open a touch larger than the minimum so users see breathing room.
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            w = max(theme.MAIN_WINDOW_MIN_W, min(1280, int(geo.width() * 0.80)))
            h = max(theme.MAIN_WINDOW_MIN_H, min(860, int(geo.height() * 0.80)))
            self.resize(w, h)

        self._build_ui()
        self._connect_signals()
        self._update_nav_state()
        self.statusBar().showMessage("Ready — load a project to get started.")

    # ----------------------------------------------------------------- build
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("contentRoot")
        self.setCentralWidget(central)

        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # ----- Sidebar -------------------------------------------------
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        brand = QLabel("Athena ILI FFP")
        brand.setObjectName("sidebarBrand")
        tag = QLabel("Pipeline FFP / CGR")
        tag.setObjectName("sidebarTagline")
        sv.addWidget(brand)
        sv.addWidget(tag)

        # Nav buttons
        self._nav_buttons: list[QPushButton] = []
        for label, idx in _NAV_ITEMS:
            btn = QPushButton(label)
            btn.setProperty("role", "navButton")
            btn.setCheckable(False)
            btn.clicked.connect(lambda _checked=False, i=idx: self.switch_to(i))
            sv.addWidget(btn)
            self._nav_buttons.append(btn)

        sv.addStretch(1)

        ver = QLabel(_sidebar_version_text())
        ver.setObjectName("sidebarVersion")
        ver.setWordWrap(True)
        sv.addWidget(ver)

        h.addWidget(sidebar)

        # ----- Content stack -------------------------------------------
        self.stack = QStackedWidget()
        self.screen_format = FormatConverterScreen()
        self.screen_project = ProjectSetupScreen()
        self.screen_run = RunAnalysisScreen()
        self.screen_results = ResultsScreen()
        self.screen_output = OutputScreen()

        # Order matches IDX_* constants above. Project Setup is index 0
        # and therefore the default landing screen on launch.
        self.stack.addWidget(self.screen_project)
        self.stack.addWidget(self.screen_format)
        self.stack.addWidget(self.screen_run)
        self.stack.addWidget(self.screen_results)
        self.stack.addWidget(self.screen_output)
        h.addWidget(self.stack, stretch=1)

        # ----- Status bar ----------------------------------------------
        sb = QStatusBar()
        self.setStatusBar(sb)

        # Initial nav state — only Project Setup enabled.
        self._project_loaded = False
        self._analysis_done = False

    def _connect_signals(self) -> None:
        # Format Converter → Project Setup
        self.screen_format.status_message.connect(self.statusBar().showMessage)
        self.screen_format.use_as_run1.connect(self._on_use_converted_as_run1)
        self.screen_format.export_complete.connect(self._on_export_complete)
        # Scope-banner "Go to Project Setup" button on the Convert
        # Format screen — switches back to Project Setup so users who
        # land here by accident have a one-click escape.
        self.screen_format.go_to_project_setup_requested.connect(
            lambda: self.switch_to(IDX_PROJECT)
        )

        # Project Setup → Run Analysis
        self.screen_project.ready.connect(self._on_project_ready)
        self.screen_project.status_message.connect(self.statusBar().showMessage)
        # When the user picks a Run-1 that doesn't parse, the project
        # setup screen offers to send them to the converter.
        if hasattr(self.screen_project, "convert_run1_requested"):
            self.screen_project.convert_run1_requested.connect(
                self._on_convert_run1_requested
            )

        # Run Analysis → Results + Output
        self.screen_run.finished.connect(self._on_analysis_finished)
        self.screen_run.status_message.connect(self.statusBar().showMessage)

        # Output → restart
        self.screen_output.run_again_requested.connect(self._on_run_again)
        self.screen_output.status_message.connect(self.statusBar().showMessage)

    # ---------------------------------------------------------------- handlers
    def _on_project_ready(self, job: AnalysisJob) -> None:
        self._project_loaded = True
        self.screen_run.set_job(job)
        self._update_nav_state()
        self.switch_to(IDX_RUN)
        self.statusBar().showMessage("Project configured — ready to run.")

    def _on_analysis_finished(self, result: AnalysisResult) -> None:
        self._analysis_done = True
        self.screen_results.set_result(result)
        self.screen_output.set_result(result)
        self._update_nav_state()
        self.switch_to(IDX_RESULTS)
        out_dir = (
            result.annexure_path.parent
            if result.annexure_path else Path(".")
        )
        self.statusBar().showMessage(
            f"Analysis complete — outputs in {out_dir}"
        )

    def _on_run_again(self) -> None:
        self.switch_to(IDX_PROJECT)
        self.statusBar().showMessage(
            "Edit the project, then proceed to run a new analysis."
        )

    def _on_export_complete(self, output_path: str) -> None:
        self.statusBar().showMessage(
            f"Converted file written: {output_path}"
        )

    def _on_use_converted_as_run1(self, output_path: str) -> None:
        """Route the freshly-converted file into the Project Setup screen."""
        if hasattr(self.screen_project, "set_run1_file"):
            self.screen_project.set_run1_file(output_path)
        self.switch_to(IDX_PROJECT)
        self.statusBar().showMessage(
            "Converted file loaded as Run-1 — fill in pipeline details next."
        )

    def _on_convert_run1_requested(self, file_path: str) -> None:
        """Project Setup → Format Converter handoff."""
        try:
            self.screen_format._load_source(Path(file_path))     # noqa: SLF001
        except Exception:                                        # noqa: BLE001
            pass
        self.switch_to(IDX_FORMAT)
        self.statusBar().showMessage(
            "Loaded the file in the converter — map its columns and export."
        )

    # ------------------------------------------------------------- navigation
    def switch_to(self, index: int) -> None:
        # Re-check gating in case a nav button was clicked while disabled
        # somehow (e.g. keyboard).
        if index == IDX_RUN and not self._project_loaded:
            return
        if index in (IDX_RESULTS, IDX_OUTPUT) and not self._analysis_done:
            return
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self._nav_buttons):
            btn.setProperty("active", "true" if i == index else "false")
            # Force QSS re-evaluation after a property change.
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _update_nav_state(self) -> None:
        # Convert Format and Project Setup are always available.
        self._nav_buttons[IDX_FORMAT].setEnabled(True)
        self._nav_buttons[IDX_PROJECT].setEnabled(True)
        self._nav_buttons[IDX_RUN].setEnabled(self._project_loaded)
        self._nav_buttons[IDX_RESULTS].setEnabled(self._analysis_done)
        self._nav_buttons[IDX_OUTPUT].setEnabled(self._analysis_done)
        # Refresh the active-button styling.
        self.switch_to(self.stack.currentIndex())

    # ------------------------------------------------------------ close logic
    def closeEvent(self, event) -> None:                 # noqa: N802 (Qt method)
        worker = getattr(self.screen_run, "_worker", None)
        if worker is not None and worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Analysis running",
                "An analysis is still running. Cancel it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            worker.request_stop()
            worker.wait(3000)
        event.accept()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sidebar_version_text() -> str:
    """Compact version banner for the sidebar footer."""
    try:
        from src._version import __version__, __build_date__
    except Exception:                                            # pragma: no cover
        return "v?"
    if __build_date__ and __build_date__ != "auto":
        # Trim seconds for a more compact label.
        date_part = __build_date__.split("T", 1)[0]
        return f"v{__version__}\n{date_part}"
    return f"v{__version__}"


# ---------------------------------------------------------------------------
# Launch helper
# ---------------------------------------------------------------------------

def launch(argv: list[str] | None = None) -> int:
    """Create the QApplication, show the window, and run the event loop.

    Returns the exit code so callers (``bin/run_gui.py``) can pass it to
    ``sys.exit``.
    """
    if argv is None:
        argv = sys.argv

    # Make sure the project root is on sys.path when launched as a script
    # so 'from src...' imports work.
    project_root = Path(__file__).resolve().parents[2]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    app = QApplication.instance() or QApplication(argv)
    app.setStyle("Fusion")
    app.setApplicationName("Athena ILI FFP Tool")
    app.setOrganizationName("Athena PowerTech LLP")
    app.setWindowIcon(build_app_icon())
    app.setStyleSheet(theme.application_stylesheet())

    # Set a known-good application palette as the ultimate fallback for
    # any widget Qt-Fusion doesn't fully style via QSS. The MAOP-zones
    # table cell text in particular falls through to QPalette.Text on
    # some Win11 + Fusion combinations; making the app palette match
    # the theme guarantees the cells render in COLOR_TEXT regardless.
    theme.apply_application_palette(app)

    window = MainWindow()
    window.show()
    return app.exec()


__all__ = ["MainWindow", "launch"]
