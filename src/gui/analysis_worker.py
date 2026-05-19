"""Background QThread that runs the full FFP pipeline.

This is the GUI's equivalent of ``bin/run_pipeline.py:run()`` — it walks
the same stages (read → align → match → CGR → FFP → predict → QA →
reports) but emits Qt signals so the Run Analysis screen can stream
per-stage status, log lines, and a progress bar without blocking the UI
thread.

Threading note: matplotlib uses its Agg-derived QtAgg backend, but the
pipeline itself only writes Excel + DOCX via openpyxl / python-docx,
neither of which touches Qt objects. We never read or modify any GUI
widget from inside this thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..io.paths import resolve_output_dir, resolve_relative_to_yaml


# Stage labels — kept in sync with the progress bar in run_analysis.py.
STAGES = (
    "Reading run-1 pipe tally",
    "Reading run-2 pipe tally",
    "Aligning joints",
    "Matching defects",
    "Computing CGR",
    "FFP assessment",
    "Predicting repair dates",
    "Aggregating QA flags",
    "Writing annexure (Excel)",
    "Writing main report (DOCX)",
)


@dataclass
class AnalysisJob:
    """All inputs the worker needs to run the pipeline once."""
    config_path: Path
    run1_path: Path | None = None
    run2_path: Path | None = None
    years_override: float | None = None
    # Resolved by callers via src.io.paths.resolve_output_dir() before
    # the job ships. None is a defensive default — the worker re-runs
    # the resolver inside run() if it ever sees None, so a hand-rolled
    # AnalysisJob from a test or REPL still works without crashing.
    output_dir: Path | None = None
    # v0.2.5: per-topic annexure selection. List of (topic_id, letter)
    # tuples; None means "fall back to project.report_annexures or the
    # legacy E_F preset".
    report_annexures: list[tuple[str, str]] | None = None
    annexure_format: str = "E_F"
    write_docx: bool = True


@dataclass
class AnalysisResult:
    """Bag of artifacts produced by a successful run.

    Both the Results screen (charts + KPI cards) and the Output screen
    (file list + open buttons) consume this; keeping it in one place
    means the worker fires a single ``finished`` signal at the end.
    """
    project: Any
    pipeline: Any
    joint_alignment: Any
    match_result: Any
    cgr_results: list[Any] = field(default_factory=list)
    ffp_results: list[Any] = field(default_factory=list)
    repair_predictions: list[Any] = field(default_factory=list)
    flag_report: Any | None = None
    years_between: float = 0.0
    annexure_path: Path | None = None
    docx_path: Path | None = None
    elapsed_seconds: float = 0.0


class AnalysisWorker(QThread):
    """Runs an :class:`AnalysisJob` and emits progress signals.

    Signals:
        stage_started(index, name)  — fires when a new stage begins
        log_line(text)              — for the live log view
        progress(current, total)    — integers for the progress bar
        finished_ok(result)         — fires once on success; carries the
                                      :class:`AnalysisResult`
        failed(message)             — fires once on error; carries a
                                      human-readable message
    """

    stage_started = pyqtSignal(int, str)
    log_line = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, job: AnalysisJob, parent=None) -> None:
        super().__init__(parent)
        self._job = job
        self._stopped = False

    # ------------------------------------------------------------------ API
    def request_stop(self) -> None:
        """Cooperative cancellation. The worker checks this between stages."""
        self._stopped = True

    # ------------------------------------------------------------------ run()
    def run(self) -> None:                                       # noqa: C901
        import time

        try:
            # Defer heavy imports until the worker thread starts. Keeps the
            # GUI startup snappy and prevents import-time failures from
            # crashing the whole app.
            from src.core.cgr import CGRCalculator
            from src.core.defect_matcher import DefectMatcher
            from src.core.ffp import ffp_assess
            from src.core.joint_alignment import JointAligner
            from src.core.repair_predictor import RepairPredictor
            from src.io.ili_reader import ILIReader
            from src.models import Project
            from src.reports.annexure_writer import AnnexureWriter
            from src.reports.main_report_writer import MainReportWriter
            from src.validation.flag_aggregator import FlagAggregator
        except Exception as e:                                   # pragma: no cover
            self.failed.emit(f"Failed to import pipeline modules: {e}")
            return

        job = self._job
        total_stages = (
            len(STAGES) if job.write_docx else len(STAGES) - 1
        )
        t_overall = time.time()

        def emit_stage(idx: int, label: str) -> None:
            self.stage_started.emit(idx, label)
            self.progress.emit(idx, total_stages)
            self.log_line.emit(f"[{idx}/{total_stages}] {label}")

        try:
            # ---------- Load project config
            self.log_line.emit(f"Loading project: {job.config_path}")
            project = Project.from_yaml(str(job.config_path))

            runs_cfg = (project.config.get("runs") or {})
            run1_raw = runs_cfg.get("run_1", {}).get("file_path")
            run2_raw = runs_cfg.get("run_2", {}).get("file_path")
            run1_path = job.run1_path or resolve_relative_to_yaml(
                job.config_path, run1_raw,
            )
            run2_path = job.run2_path or resolve_relative_to_yaml(
                job.config_path, run2_raw,
            )
            # v0.2.3: error mentions BOTH the YAML location and the
            # resolved path — relative paths make the resolved value
            # non-obvious from the raw YAML text.
            if not run1_path or not Path(run1_path).exists():
                raise FileNotFoundError(
                    f"Run-1 file not found.\n"
                    f"  YAML:     {job.config_path}\n"
                    f"  Resolved: {run1_path}\n"
                    f"  Raw YAML value: {run1_raw!r}"
                )
            if not run2_path or not Path(run2_path).exists():
                raise FileNotFoundError(
                    f"Run-2 file not found.\n"
                    f"  YAML:     {job.config_path}\n"
                    f"  Resolved: {run2_path}\n"
                    f"  Raw YAML value: {run2_raw!r}"
                )

            pipeline = project.pipeline
            reader = ILIReader()

            # ---------- 1: read run-1
            if self._stopped: return self._emit_cancelled()
            emit_stage(1, STAGES[0])
            t0 = time.time()
            run1 = reader.read(str(run1_path), run_id="run_1")
            if project.run_1.inspection_date:
                run1.inspection_date = project.run_1.inspection_date
            self.log_line.emit(
                f"    {len(run1.features_for_assessment())} assessable features "
                f"({time.time() - t0:.1f}s)"
            )

            # ---------- 2: read run-2
            if self._stopped: return self._emit_cancelled()
            emit_stage(2, STAGES[1])
            t0 = time.time()
            run2 = reader.read(str(run2_path), run_id="run_2")
            if project.run_2.inspection_date:
                run2.inspection_date = project.run_2.inspection_date
            project.run_1 = run1
            project.run_2 = run2
            self.log_line.emit(
                f"    {len(run2.features_for_assessment())} assessable features "
                f"({time.time() - t0:.1f}s)"
            )

            # ----- years
            if job.years_override is not None:
                years_between = float(job.years_override)
                self.log_line.emit(f"    years between: {years_between:.3f} (override)")
            elif run1.inspection_date and run2.inspection_date:
                years_between = (run2.inspection_date - run1.inspection_date).days / 365.25
                self.log_line.emit(f"    years between: {years_between:.3f}")
            else:
                raise ValueError(
                    "No inspection dates in config and no --years override supplied"
                )

            # ---------- 3: joint alignment
            if self._stopped: return self._emit_cancelled()
            emit_stage(3, STAGES[2])
            t0 = time.time()
            ja = JointAligner().align(run1, run2)
            self.log_line.emit(
                f"    {len(ja.matches)} joint pairs, "
                f"match_rate={ja.match_rate:.1%} ({time.time() - t0:.1f}s)"
            )

            # ---------- 4: defect matching
            if self._stopped: return self._emit_cancelled()
            emit_stage(4, STAGES[3])
            t0 = time.time()
            mr = DefectMatcher().match(run1, run2, ja.matches)
            self.log_line.emit(
                f"    {len(mr.feature_matches)} matched, "
                f"{len(mr.unmatched_features_new)} new-only "
                f"({time.time() - t0:.1f}s)"
            )

            # ---------- 5: CGR
            if self._stopped: return self._emit_cancelled()
            emit_stage(5, STAGES[4])
            t0 = time.time()
            cgr_mode = (project.config.get("cgr") or {}).get("mode", "hybrid")
            cgrs = CGRCalculator({"mode": cgr_mode}).compute(
                mr, years_between=years_between
            )
            self.log_line.emit(
                f"    mode={cgr_mode}, {len(cgrs)} growth results "
                f"({time.time() - t0:.1f}s)"
            )

            # ---------- 6: FFP
            if self._stopped: return self._emit_cancelled()
            emit_stage(6, STAGES[5])
            t0 = time.time()
            primary = (project.config.get("ffp") or {}).get(
                "primary_method", "B31G_Original"
            )
            ffps_by_id: dict[str, Any] = {}
            for c in cgrs:
                try:
                    fl = ffp_assess(
                        c.feature, pipeline, config={"primary_method": primary}
                    )
                    ffps_by_id[c.feature.anomaly_id] = next(
                        (f for f in fl if f.is_controlling), fl[0]
                    )
                except ValueError:
                    continue
            self.log_line.emit(
                f"    primary={primary}, {len(ffps_by_id)} features assessed "
                f"({time.time() - t0:.1f}s)"
            )

            # ---------- 7: repair predictor
            if self._stopped: return self._emit_cancelled()
            emit_stage(7, STAGES[6])
            t0 = time.time()
            horizon = int(
                (project.config.get("repair_prediction") or {}).get(
                    "horizon_years", 10
                )
            )
            preds = RepairPredictor({"horizon_years": horizon}).predict(
                cgrs, ffps_by_id, pipeline,
                run2_inspection_date=run2.inspection_date,
            )
            n_repair = sum(
                1 for p in preds if p.repair_trigger != "NONE_WITHIN_HORIZON"
            )
            self.log_line.emit(
                f"    horizon={horizon}y, {n_repair} repair within horizon "
                f"({time.time() - t0:.1f}s)"
            )

            # ---------- 8: QA aggregation
            if self._stopped: return self._emit_cancelled()
            emit_stage(8, STAGES[7])
            t0 = time.time()
            flag_report = FlagAggregator().aggregate(
                run1=run1, run2=run2, joint_alignment=ja, match_result=mr,
                cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
                predictions=preds,
            )
            self.log_line.emit(
                f"    {flag_report.summary} ({time.time() - t0:.1f}s)"
            )

            # ---------- 9: annexure
            if job.output_dir is not None:
                out_dir = Path(job.output_dir)
            else:
                # Defensive: every GUI / CLI caller populates
                # job.output_dir via resolve_output_dir() up front, but
                # if a hand-built AnalysisJob slipped through without
                # one, resolve here instead of inheriting Path("./output").
                out_dir = resolve_output_dir(
                    Path(job.config_path),
                    project.project_name
                    or Path(job.config_path).stem
                    or "ffp_project",
                )
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = project.project_name or "ffp_project"
            annex_path = out_dir / f"{stem}_annexure.xlsx"

            if self._stopped: return self._emit_cancelled()
            emit_stage(9, STAGES[8])
            t0 = time.time()
            # v0.2.5: prefer the new topic-list mode (from the project
            # YAML's `report.annexures` block, parsed at load time). Fall
            # back to the legacy `format` preset if the AnalysisJob has
            # no topic list — keeps the path open for older callers that
            # haven't been migrated to the new selector yet.
            AnnexureWriter().write(
                cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
                repair_predictions=preds, flag_report=flag_report,
                project=project, pipeline=pipeline,
                output_path=str(annex_path),
                topics=getattr(job, "report_annexures", None) or project.report_annexures,
                years_between=years_between,
                format=job.annexure_format,
            )
            self.log_line.emit(
                f"    -> {annex_path.name} ({time.time() - t0:.1f}s)"
            )

            # ---------- 10: DOCX (optional)
            docx_path: Path | None = None
            if job.write_docx:
                if self._stopped: return self._emit_cancelled()
                emit_stage(10, STAGES[9])
                t0 = time.time()
                docx_path = out_dir / f"{stem}_report.docx"
                MainReportWriter().write(
                    project=project, match_result=mr, joint_alignment=ja,
                    cgr_results=cgrs, ffp_results=list(ffps_by_id.values()),
                    repair_predictions=preds, flag_report=flag_report,
                    output_path=str(docx_path),
                )
                self.log_line.emit(
                    f"    -> {docx_path.name} ({time.time() - t0:.1f}s)"
                )

            elapsed = time.time() - t_overall
            self.log_line.emit("")
            self.log_line.emit(f"Pipeline complete in {elapsed:.1f}s.")

            result = AnalysisResult(
                project=project,
                pipeline=pipeline,
                joint_alignment=ja,
                match_result=mr,
                cgr_results=list(cgrs),
                ffp_results=list(ffps_by_id.values()),
                repair_predictions=list(preds),
                flag_report=flag_report,
                years_between=years_between,
                annexure_path=annex_path,
                docx_path=docx_path,
                elapsed_seconds=elapsed,
            )
            self.progress.emit(total_stages, total_stages)
            self.finished_ok.emit(result)

        except Exception as e:                                   # noqa: BLE001
            import traceback
            tb = traceback.format_exc()
            self.log_line.emit("")
            self.log_line.emit(f"ERROR: {type(e).__name__}: {e}")
            self.log_line.emit(tb)
            self.failed.emit(f"{type(e).__name__}: {e}")

    # ----------------------------------------------------------------- helpers
    def _emit_cancelled(self) -> None:
        self.log_line.emit("Analysis cancelled by user.")
        self.failed.emit("cancelled")


# NOTE: v0.2.3 removed `_resolve_path` in favour of
# `src.io.paths.resolve_relative_to_yaml`. The same rationale as in
# `bin/run_pipeline.py` applies — the old "project root first" fallback
# meant nothing inside a PyInstaller bundle, and the new helper
# resolves against the YAML's own parent only.


__all__ = ["AnalysisJob", "AnalysisResult", "AnalysisWorker", "STAGES"]
