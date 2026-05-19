"""Run Analysis screen — kicks off the AnalysisWorker and streams progress.

The user lands here from Project Setup with a pre-populated :class:`AnalysisJob`.
This screen lets them tweak the output directory + a couple of flags,
then runs the pipeline in a background QThread while showing per-stage
progress lines, an overall progress bar, and a live log view.

When the worker emits ``finished_ok``, the screen relays the
:class:`AnalysisResult` to the main window via the ``finished`` signal.
"""
from __future__ import annotations

from dataclasses import replace as dc_replace
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..analysis_worker import AnalysisJob, AnalysisResult, AnalysisWorker, STAGES
from ...io.paths import resolve_output_dir


class RunAnalysisScreen(QWidget):
    """Live progress + log for the running pipeline."""

    finished = pyqtSignal(object)         # emits AnalysisResult
    status_message = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._worker: AnalysisWorker | None = None
        self._job: AnalysisJob | None = None
        self._build_ui()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PAD_XL, theme.PAD_L, theme.PAD_XL, theme.PAD_L)
        root.setSpacing(theme.PAD_M)

        title = QLabel("Run Analysis")
        title.setProperty("role", "screenTitle")
        subtitle = QLabel(
            "Review the inputs, choose an output folder, and run the pipeline."
        )
        subtitle.setProperty("role", "screenSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        # ---- Summary card (filled from the incoming job) ----------------
        self.summary_card = QFrame()
        self.summary_card.setProperty("role", "card")
        sum_vbox = QVBoxLayout(self.summary_card)
        sum_vbox.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        sum_vbox.setSpacing(4)
        sum_header = QLabel("Job summary")
        sum_header.setProperty("role", "sectionHeader")
        self.lbl_project = QLabel("Project: —")
        self.lbl_config = QLabel("Config: —")
        self.lbl_run1 = QLabel("Run 1: —")
        self.lbl_run2 = QLabel("Run 2: —")
        for lbl in (self.lbl_project, self.lbl_config, self.lbl_run1, self.lbl_run2):
            lbl.setStyleSheet(f"color: {theme.COLOR_TEXT}; font-size: 12px;")
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setWordWrap(True)

        sum_vbox.addWidget(sum_header)
        sum_vbox.addWidget(self.lbl_project)
        sum_vbox.addWidget(self.lbl_config)
        sum_vbox.addWidget(self.lbl_run1)
        sum_vbox.addWidget(self.lbl_run2)
        root.addWidget(self.summary_card)

        # ---- Output options card ---------------------------------------
        self.options_card = QFrame()
        self.options_card.setProperty("role", "card")
        opt = QVBoxLayout(self.options_card)
        opt.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        opt.setSpacing(theme.PAD_S)
        opt_header = QLabel("Output options")
        opt_header.setProperty("role", "sectionHeader")
        opt.addWidget(opt_header)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output folder:"))
        # Left empty here on purpose. set_job() fills this in with the
        # resolver's choice (alongside YAML, or ~/Documents/Athena ILI
        # FFP/<project>/). Showing a stale Path("./output") placeholder
        # would mislead the user when running from a Start-Menu install.
        self.ed_output_dir = QLineEdit("")
        out_row.addWidget(self.ed_output_dir, stretch=1)
        self.btn_browse_out = QPushButton("Browse…")
        self.btn_browse_out.clicked.connect(self._on_browse_output)
        out_row.addWidget(self.btn_browse_out)
        opt.addLayout(out_row)

        # v0.2.5: the per-topic annexure selection lives on the
        # Project Setup screen now (single source of truth). This
        # screen retains the DOCX toggle as a per-run override.
        fmt_row = QHBoxLayout()
        self.cb_docx = QCheckBox("Write DOCX main report")
        self.cb_docx.setChecked(True)
        fmt_row.addWidget(self.cb_docx)
        fmt_row.addStretch(1)
        opt.addLayout(fmt_row)

        root.addWidget(self.options_card)

        # ---- Progress card --------------------------------------------
        prog_card = QFrame()
        prog_card.setProperty("role", "card")
        pv = QVBoxLayout(prog_card)
        pv.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        pv.setSpacing(theme.PAD_S)

        prog_header = QLabel("Progress")
        prog_header.setProperty("role", "sectionHeader")
        pv.addWidget(prog_header)

        self.lbl_stage = QLabel("Idle.")
        self.lbl_stage.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED}; font-size: 12px;"
        )
        pv.addWidget(self.lbl_stage)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(STAGES))
        self.progress_bar.setValue(0)
        pv.addWidget(self.progress_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)
        self.log_view.setFont(mono)
        self.log_view.setMinimumHeight(200)
        self.log_view.setStyleSheet(
            f"QPlainTextEdit {{ background-color: #1E1E1E; color: #DCDCDC;"
            f" border: 1px solid {theme.COLOR_CARD_BORDER}; }}"
        )
        pv.addWidget(self.log_view, stretch=1)

        root.addWidget(prog_card, stretch=1)

        # ---- Footer buttons --------------------------------------------
        footer = QHBoxLayout()
        self.btn_run = QPushButton("Run analysis")
        self.btn_run.setProperty("role", "primary")
        self.btn_run.clicked.connect(self._on_run_clicked)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        footer.addWidget(self.btn_run)
        footer.addWidget(self.btn_cancel)
        footer.addStretch(1)
        root.addLayout(footer)

    # -------------------------------------------------------- public hooks
    def set_job(self, job: AnalysisJob) -> None:
        """Called by the main window when the user proceeds from Project Setup."""
        self._job = job
        self.lbl_project.setText(f"Project YAML: {job.config_path}")
        # v0.2.5: summarize the topic selection instead of the legacy
        # E_F/B_C_D string.
        n_topics = len(job.report_annexures or [])
        self.lbl_config.setText(
            f"Annexures: {n_topics} topic(s)    "
            f"DOCX: {'yes' if job.write_docx else 'no'}    "
            f"Years override: "
            f"{job.years_override if job.years_override is not None else 'from dates'}"
        )
        self.lbl_run1.setText(f"Run 1: {job.run1_path}")
        self.lbl_run2.setText(f"Run 2: {job.run2_path}")
        self.cb_docx.setChecked(job.write_docx)
        self.ed_output_dir.setText(str(job.output_dir.resolve()))
        self.lbl_stage.setText("Ready. Click 'Run analysis' to start.")
        self.progress_bar.setValue(0)
        self.log_view.clear()
        self.btn_run.setEnabled(True)

    # ------------------------------------------------------------ handlers
    def _on_browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.ed_output_dir.text()
        )
        if path:
            self.ed_output_dir.setText(path)

    def _on_run_clicked(self) -> None:
        if not self._job:
            QMessageBox.warning(
                self, "No job", "Configure a project first (Project Setup).",
            )
            return
        if self._worker is not None and self._worker.isRunning():
            return
        # Merge in the screen's overrides. If the user manually
        # cleared the line edit, fall back to the resolver rather than
        # the broken Path("./output") that v0.2.1 used.
        text = self.ed_output_dir.text().strip()
        if text:
            out_dir = Path(text)
        else:
            out_dir = resolve_output_dir(
                Path(self._job.config_path) if self._job else None,
                Path(self._job.config_path).stem if self._job else "ffp_project",
            )
        # v0.2.5: annexure_format is no longer set here (topics come
        # from the project YAML via project_setup.py). DOCX stays as
        # a per-run override.
        job = dc_replace(
            self._job,
            output_dir=out_dir,
            write_docx=self.cb_docx.isChecked(),
        )
        self._job = job

        self.log_view.clear()
        self.progress_bar.setValue(0)
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.status_message.emit("Running analysis…")

        self._worker = AnalysisWorker(job, parent=self)
        self._worker.stage_started.connect(self._on_stage_started)
        self._worker.log_line.connect(self._on_log_line)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_cancel_clicked(self) -> None:
        if self._worker:
            self._worker.request_stop()
            self.lbl_stage.setText("Cancelling — waiting for current stage to finish…")

    # ------------------------------------------------------ worker signals
    def _on_stage_started(self, index: int, label: str) -> None:
        self.lbl_stage.setText(f"Stage {index} of {self.progress_bar.maximum()}: {label}")

    def _on_log_line(self, text: str) -> None:
        self.log_view.appendPlainText(text)
        # Keep scroll pinned to bottom
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_progress(self, current: int, total: int) -> None:
        if self.progress_bar.maximum() != total:
            self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)

    def _on_finished_ok(self, result: AnalysisResult) -> None:
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText(
            f"Done in {result.elapsed_seconds:.1f}s. "
            f"Outputs in {result.annexure_path.parent if result.annexure_path else '(?)'}."
        )
        self.status_message.emit("Analysis complete.")
        self.finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if message == "cancelled":
            self.lbl_stage.setText("Cancelled.")
            self.status_message.emit("Analysis cancelled.")
        else:
            self.lbl_stage.setText("Failed — see log for details.")
            self.status_message.emit("Analysis failed.")
            QMessageBox.critical(self, "Analysis failed", message)
