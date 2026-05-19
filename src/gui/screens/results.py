"""Results screen — KPI cards + charts derived from an :class:`AnalysisResult`.

This screen is purely a viewer: it consumes the data the worker produced
on the previous screen and renders summaries. No pipeline calls happen
here. Charts use the matplotlib QtAgg backend via :class:`ChartCanvas`.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..analysis_worker import AnalysisResult
from ..widgets.chart_canvas import ChartCanvas
from ..widgets.summary_card import SummaryCard


class ResultsScreen(QWidget):
    """Summary cards + tabbed charts for the most recent analysis."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._result: AnalysisResult | None = None
        self._build_ui()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(theme.PAD_XL, theme.PAD_L, theme.PAD_XL, theme.PAD_L)
        root.setSpacing(theme.PAD_M)

        title = QLabel("Results")
        title.setProperty("role", "screenTitle")
        self._subtitle = QLabel("Run an analysis to see results.")
        self._subtitle.setProperty("role", "screenSubtitle")
        self._subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(self._subtitle)

        # ----- Scrollable body so KPI grid + charts both fit on smaller screens
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("contentRoot")
        scroll.setWidget(body)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(theme.PAD_M)

        # ----- KPI grid (3 columns × 2 rows)
        kpi_grid = QGridLayout()
        kpi_grid.setSpacing(theme.PAD_M)
        self.card_joints = SummaryCard("Joints aligned")
        self.card_matches = SummaryCard("Defects matched")
        self.card_erf = SummaryCard("ERF ≥ 1.0",
                                    accent=theme.COLOR_SUCCESS)
        self.card_depth = SummaryCard("Depth ≥ 80% WT",
                                      accent=theme.COLOR_SUCCESS)
        self.card_repairs = SummaryCard("Repairs within horizon",
                                        accent=theme.COLOR_SUCCESS)
        self.card_qa = SummaryCard("QA verdict", value="—")

        for r, c, card in (
            (0, 0, self.card_joints),
            (0, 1, self.card_matches),
            (0, 2, self.card_erf),
            (1, 0, self.card_depth),
            (1, 1, self.card_repairs),
            (1, 2, self.card_qa),
        ):
            kpi_grid.addWidget(card, r, c)
        body_layout.addLayout(kpi_grid)

        # ----- ERF-distribution strip (v0.3.2).
        # Single horizontal label showing the 4-bucket breakdown,
        # classified via `src.reports.erf_buckets`. Default dp=None
        # (raw float comparison) since v0.3.2 — matches the per-feature
        # ERFs reported in Annexure D. Engineering severity flags
        # (ERF_EXCEEDS_1) continue to use raw ERF — the bucket counts
        # are display-only.
        self.lbl_erf_distribution = QLabel(
            "ERF distribution: (run an analysis to populate)"
        )
        self.lbl_erf_distribution.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED};"
            f" font-size: 12px; padding: 6px 2px;"
        )
        self.lbl_erf_distribution.setWordWrap(True)
        body_layout.addWidget(self.lbl_erf_distribution)

        # Italic footnote clarifying the convention vs published PDFs.
        self.lbl_erf_distribution_note = QLabel(
            "<i>Bucket counts use full-precision ERFs as reported in "
            "Annexure D. Display-precision rounding in published "
            "reports may produce slightly different counts; see "
            "ENGINE_REFERENCE.md §11.2.</i>"
        )
        self.lbl_erf_distribution_note.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_erf_distribution_note.setStyleSheet(
            f"color: {theme.COLOR_TEXT_MUTED};"
            f" font-size: 11px; padding: 0px 2px 6px 2px;"
        )
        self.lbl_erf_distribution_note.setWordWrap(True)
        body_layout.addWidget(self.lbl_erf_distribution_note)

        # ----- Charts in tabs
        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(380)

        self.chart_depth = ChartCanvas(width=6, height=3.5)
        self.chart_length = ChartCanvas(width=6, height=3.5)
        self.chart_erf_scatter = ChartCanvas(width=6, height=3.5)
        self.chart_repair_timeline = ChartCanvas(width=6, height=3.5)
        self.chart_cgr = ChartCanvas(width=6, height=3.5)

        self.tabs.addTab(self._wrap_chart(self.chart_depth), "Depth distribution")
        self.tabs.addTab(self._wrap_chart(self.chart_length), "Length distribution")
        self.tabs.addTab(self._wrap_chart(self.chart_erf_scatter), "ERF vs depth")
        self.tabs.addTab(self._wrap_chart(self.chart_repair_timeline), "Repair timeline")
        self.tabs.addTab(self._wrap_chart(self.chart_cgr), "CGR distribution")
        body_layout.addWidget(self.tabs)

        body_layout.addStretch(1)
        root.addWidget(scroll, stretch=1)

        # ----- Pipeline meta footer
        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet(f"color: {theme.COLOR_TEXT_MUTED}; font-size: 11px;")
        self._meta_label.setWordWrap(True)
        root.addWidget(self._meta_label)

    def _wrap_chart(self, canvas: ChartCanvas) -> QWidget:
        wrap = QFrame()
        wrap.setProperty("role", "card")
        v = QVBoxLayout(wrap)
        v.setContentsMargins(theme.PAD_M, theme.PAD_M, theme.PAD_M, theme.PAD_M)
        v.addWidget(canvas)
        return wrap

    # ---------------------------------------------------------- update API
    def set_result(self, result: AnalysisResult) -> None:
        self._result = result

        proj_name = (
            getattr(result.project, "project_name", "")
            or "(unnamed project)"
        )
        pipe = result.pipeline
        self._subtitle.setText(
            f"{proj_name} — {pipe.pipeline_name} "
            f"({pipe.diameter_mm:.0f} mm OD, {pipe.length_km:.1f} km, "
            f"{pipe.material_grade}). Δt = {result.years_between:.2f} yr."
        )

        # ----- KPI values --------------------------------------------------
        ja = result.joint_alignment
        mr = result.match_result
        ffps = result.ffp_results
        preds = result.repair_predictions
        flag_report = result.flag_report

        self.card_joints.set_value(f"{len(ja.matches)}")
        self.card_joints.set_subtitle(
            f"match rate {ja.match_rate:.1%}"
            + (f", {len(ja.monotonicity_violations)} reversal(s)"
               if getattr(ja, "monotonicity_violations", None) else "")
        )

        n_run2 = len(mr.feature_matches) + len(mr.unmatched_features_new)
        self.card_matches.set_value(f"{len(mr.feature_matches)}")
        self.card_matches.set_subtitle(f"of {n_run2} run-2 features assessed")

        # ERF ≥ 1.0 card uses RAW float comparison — this is the
        # engineering severity threshold and must NOT round.
        n_erf = sum(1 for f in ffps if f.erf >= 1.0)
        self.card_erf.set_value(f"{n_erf}")
        self.card_erf.set_subtitle(f"of {len(ffps)} assessed features")
        self.card_erf.set_accent(SummaryCard.threshold_colour(n_erf))

        # v0.3.2: 4-bucket ERF distribution strip using raw float
        # comparison (no rounding) — aligns to Annexure D per-feature
        # ERFs. See src/reports/erf_buckets.py and the italic footnote
        # below the strip.
        from src.reports.erf_buckets import count_erf_buckets, ERF_BUCKET_LABELS
        bucket_counts = count_erf_buckets(ffps)
        strip = "   ·   ".join(
            f"{label}: {bucket_counts[label]}"
            for label in ERF_BUCKET_LABELS
        )
        self.lbl_erf_distribution.setText(
            f"ERF distribution:   {strip}"
        )

        n_depth = sum(1 for f in ffps if f.depth_pct_wt >= 80.0)
        self.card_depth.set_value(f"{n_depth}")
        self.card_depth.set_subtitle(f"of {len(ffps)} assessed features")
        self.card_depth.set_accent(SummaryCard.threshold_colour(n_depth))

        horizon = preds[0].horizon_years if preds else 0
        n_repair = sum(1 for p in preds if p.repair_trigger != "NONE_WITHIN_HORIZON")
        self.card_repairs.set_value(f"{n_repair}")
        self.card_repairs.set_subtitle(
            f"within {horizon} yr (of {len(preds)} tracked)"
        )
        self.card_repairs.set_accent(SummaryCard.threshold_colour(n_repair))

        n_err = self._count_severity(flag_report, "ERROR")
        n_warn = self._count_severity(flag_report, "WARN")
        n_info = self._count_severity(flag_report, "INFO")
        verdict = (
            "REVIEW REQUIRED" if flag_report and flag_report.has_critical
            else ("REVIEW" if n_err else "OK")
        )
        self.card_qa.set_value(verdict)
        self.card_qa.set_subtitle(
            f"{n_err} error, {n_warn} warn, {n_info} info"
        )
        self.card_qa.set_accent(SummaryCard.severity_colour(
            critical=int(bool(flag_report and flag_report.has_critical)),
            errors=n_err, warnings=n_warn,
        ))

        # ----- Charts ------------------------------------------------------
        self._refresh_charts(result)

        # ----- Footer ------------------------------------------------------
        meta_bits = []
        if result.years_between:
            meta_bits.append(f"Δt = {result.years_between:.3f} yr")
        if flag_report is not None:
            meta_bits.append(f"QA: {flag_report.summary}")
        if result.elapsed_seconds:
            meta_bits.append(f"pipeline ran in {result.elapsed_seconds:.1f}s")
        self._meta_label.setText("  •  ".join(meta_bits))

    def reset(self) -> None:
        self._result = None
        self._subtitle.setText("Run an analysis to see results.")
        for card in (self.card_joints, self.card_matches, self.card_erf,
                     self.card_depth, self.card_repairs, self.card_qa):
            card.set_value("—")
            card.set_subtitle("")
        # v0.2.6: clear the ERF-distribution strip.
        self.lbl_erf_distribution.setText(
            "ERF distribution: (run an analysis to populate)"
        )
        for canvas in (self.chart_depth, self.chart_length,
                       self.chart_erf_scatter, self.chart_repair_timeline,
                       self.chart_cgr):
            canvas.message("No data — run an analysis to populate this chart.")
        self._meta_label.setText("")

    # ------------------------------------------------------------ internals
    @staticmethod
    def _count_severity(flag_report, name: str) -> int:
        if not flag_report:
            return 0
        # Severity dict is keyed by QASeverity enum; match by .name to
        # avoid importing the enum from here.
        for sev, flags in flag_report.flags_by_severity.items():
            sev_name = getattr(sev, "name", str(sev))
            if sev_name == name:
                return len(flags)
        return 0

    def _refresh_charts(self, result: AnalysisResult) -> None:
        ffps = result.ffp_results
        cgrs = result.cgr_results
        preds = result.repair_predictions

        depths = [f.depth_pct_wt for f in ffps if f.depth_pct_wt is not None]
        lengths = [f.length_mm for f in ffps if f.length_mm is not None and f.length_mm > 0]

        self.chart_depth.plot_histogram(
            depths, bins=20,
            title="Depth distribution (run-2 assessed features)",
            xlabel="Depth (% WT)", ylabel="Count",
        )

        self.chart_length.plot_histogram(
            lengths, bins=20,
            title="Length distribution (run-2 assessed features)",
            xlabel="Length (mm)", ylabel="Count",
            color=theme.COLOR_INFO,
        )

        # ERF vs depth scatter; horizontal line at ERF=1.0
        xs = depths
        ys: list[float] = []
        if ffps:
            # Align by index — depths list and ffps order match because
            # `depths` was built by iterating ffps.
            ys = [f.erf for f in ffps if f.depth_pct_wt is not None]
        self.chart_erf_scatter.plot_scatter(
            xs, ys,
            title="ERF vs depth (assessed features)",
            xlabel="Depth (% WT)", ylabel="ERF (= MAOP / Psafe)",
            hline=1.0, hline_label="ERF = 1.0",
        )

        # Repair timeline — bucket by year offset
        offsets = [
            p.repair_year_offset for p in preds
            if p.repair_year_offset is not None
            and p.repair_trigger != "NONE_WITHIN_HORIZON"
        ]
        if offsets:
            horizon = preds[0].horizon_years if preds else 10
            counts: Counter[int] = Counter()
            for o in offsets:
                counts[int(o)] += 1
            xs_l = [str(i) for i in range(0, horizon + 1)]
            ys_l = [counts.get(i, 0) for i in range(0, horizon + 1)]
            self.chart_repair_timeline.plot_bar(
                xs_l, ys_l,
                title="Predicted repair count by year offset",
                xlabel="Years from run-2 inspection date",
                ylabel="Features predicted to need repair",
                color=theme.COLOR_WARNING,
            )
        else:
            self.chart_repair_timeline.message(
                "No features predicted to need repair within the horizon."
            )

        # CGR distribution
        cgr_values = [c.cgr_mm_yr for c in cgrs if c.cgr_mm_yr is not None]
        self.chart_cgr.plot_histogram(
            cgr_values, bins=20,
            title="CGR distribution",
            xlabel="Corrosion growth rate (mm/yr)", ylabel="Count",
            color=theme.COLOR_SUCCESS,
        )
