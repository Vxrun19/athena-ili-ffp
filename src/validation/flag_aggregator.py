"""
Collect QA flags from every pipeline stage into a single, deduplicated,
report-ready `FlagReport`.

Sources the aggregator pulls from:

  * `ILIRun.qa_flags`             (reader: COORDINATES_SWAPPED,
                                    LAT_LON_OUT_OF_BOUNDS,
                                    RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE)
  * `JointAlignment.qa_flags`      (LOW_JOINT_MATCH_RATE, REVERSAL_DETECTED,
                                    LENGTH_MISMATCH_RUN)
  * `MatchResult.qa_flags`         (LOW_DEFECT_MATCH_RATE, NO_CLUSTERS_IN_EITHER_RUN)
  * `CGRResult.qa_flags`           (NEGATIVE_GROWTH, EXTREME_CGR,
                                    POPULATION_FLOOR_APPLIED, UNMATCHED_RUN2,
                                    DEPTH_BELOW_TOL)
  * `FFPResult.qa_flags`           (ERF_EXCEEDS_1, DEPTH_EXCEEDS_80,
                                    LONG_DEFECT_OUTSIDE_B31G, VERY_SHORT_DEFECT,
                                    MAOP_ZONE_NOT_FOUND)
  * `RepairPrediction.qa_flags`    (carries the CGR flags forward)
  * Aggregator-synthesised pipeline-level flags
    (REPAIR_PREDICTED_WITHIN_HORIZON, HIGH_CGR_POPULATION).

Deduplication: same `(code, feature_id)` only kept once. Run-level flags
(feature_id=None) dedupe by code alone. Severity is always re-normalised
through `CANONICAL_SEVERITY` so the bucketing matches the policy in
`qa_flags.py` regardless of what severity the emitting module set.

The `FlagReport` returned is suitable for downstream rendering:
  - the DOCX report's "QA findings" section can iterate
    `flags_by_severity` ERROR/WARN/INFO in order;
  - the Excel deliverable's "Issues" sheet can iterate `all_flags`;
  - the GUI dashboard reads `counts` for chip badges and `has_critical`
    for the red-flag indicator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable

from src.validation.qa_flags import (
    CANONICAL_SEVERITY,
    QAFlag,
    QAFlagCode,
    QASeverity,
    make_flag,
)


# Critical codes that flip `FlagReport.has_critical`.
_CRITICAL_CODES: frozenset[QAFlagCode] = frozenset({
    QAFlagCode.ERF_EXCEEDS_1,
    QAFlagCode.DEPTH_EXCEEDS_80,
})

# Median-CGR threshold (mm/yr) for the pipeline-level HIGH_CGR_POPULATION
# flag. Above this the line is "actively corroding" enough to call out.
_HIGH_CGR_MEDIAN_THRESHOLD_MM_YR = 0.2


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class FlagReport:
    """Aggregated QA output ready for rendering."""
    all_flags: list[QAFlag] = field(default_factory=list)
    flags_by_severity: dict[QASeverity, list[QAFlag]] = field(default_factory=dict)
    flags_by_feature: dict[str, list[QAFlag]] = field(default_factory=dict)
    counts: dict[QAFlagCode, int] = field(default_factory=dict)
    has_critical: bool = False
    summary: str = ""

    def __post_init__(self):
        # Pre-populate severity buckets so callers can iterate without
        # KeyError on stages that emitted no flags of a given level.
        for sev in QASeverity:
            self.flags_by_severity.setdefault(sev, [])


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

class FlagAggregator:
    """Build a `FlagReport` from the outputs of the full pipeline.

    Inputs are optional — pass whichever stage outputs you have. Anything
    None or empty contributes nothing.
    """

    def aggregate(
        self,
        *,
        run1: Any = None,
        run2: Any = None,
        joint_alignment: Any = None,
        match_result: Any = None,
        cgr_results: Iterable[Any] | None = None,
        ffp_results: Iterable[Any] | None = None,
        predictions: Iterable[Any] | None = None,
    ) -> FlagReport:
        seen: set[tuple[QAFlagCode, str | None]] = set()
        flags: list[QAFlag] = []

        def _add(flag_objs: Iterable[Any]) -> None:
            for f in flag_objs or []:
                if not isinstance(f, QAFlag):
                    continue
                key = (f.code, f.feature_id)
                if key in seen:
                    continue
                seen.add(key)
                # Re-normalise severity against the canonical map so
                # bucketing is consistent regardless of emitter.
                f.severity = CANONICAL_SEVERITY.get(f.code, f.severity)
                flags.append(f)

        # Reader (per run)
        for run in (run1, run2):
            if run is not None:
                _add(getattr(run, "qa_flags", []) or [])

        # Joint alignment
        if joint_alignment is not None:
            _add(getattr(joint_alignment, "qa_flags", []) or [])

        # Defect matcher
        if match_result is not None:
            _add(getattr(match_result, "qa_flags", []) or [])

        # CGR — one per feature
        cgr_list = list(cgr_results or [])
        for r in cgr_list:
            _add(getattr(r, "qa_flags", []) or [])

        # FFP — one per assessed feature
        for r in ffp_results or []:
            _add(getattr(r, "qa_flags", []) or [])

        # Repair predictions — one per projected feature
        pred_list = list(predictions or [])
        for p in pred_list:
            _add(getattr(p, "qa_flags", []) or [])

        # ----- Synthesised pipeline-level flags -----
        # REPAIR_PREDICTED_WITHIN_HORIZON: any prediction whose trigger
        # fires within the horizon. The trigger string lives on the
        # prediction itself; we look for anything other than the
        # "NONE_WITHIN_HORIZON" sentinel.
        repair_triggered = [
            p for p in pred_list
            if getattr(p, "repair_trigger", "") not in ("", "NONE_WITHIN_HORIZON")
        ]
        if repair_triggered:
            _add([
                make_flag(
                    QAFlagCode.REPAIR_PREDICTED_WITHIN_HORIZON,
                    f"{len(repair_triggered)} feature(s) predicted to need "
                    "repair within the projection horizon.",
                    context={"n_features": len(repair_triggered)},
                )
            ])

        # HIGH_CGR_POPULATION: pipeline-wide median CGR > threshold.
        cgr_values = [
            float(r.cgr_mm_yr)
            for r in cgr_list
            if getattr(r, "cgr_mm_yr", None) is not None
        ]
        if cgr_values:
            med = float(median(cgr_values))
            if med > _HIGH_CGR_MEDIAN_THRESHOLD_MM_YR:
                _add([
                    make_flag(
                        QAFlagCode.HIGH_CGR_POPULATION,
                        f"pipeline median CGR {med:.3f} mm/yr exceeds "
                        f"{_HIGH_CGR_MEDIAN_THRESHOLD_MM_YR:.2f} mm/yr — "
                        "active corrosion across the line, not just isolated defects.",
                        context={"median_cgr_mm_yr": med,
                                 "threshold_mm_yr": _HIGH_CGR_MEDIAN_THRESHOLD_MM_YR,
                                 "n_features": len(cgr_values)},
                    )
                ])

        return self._build_report(flags)

    # ------------------------------------------------------------------

    def _build_report(self, flags: list[QAFlag]) -> FlagReport:
        report = FlagReport(all_flags=list(flags))

        for f in flags:
            report.flags_by_severity.setdefault(f.severity, []).append(f)
            if f.feature_id:
                report.flags_by_feature.setdefault(f.feature_id, []).append(f)
            report.counts[f.code] = report.counts.get(f.code, 0) + 1

        report.has_critical = any(f.code in _CRITICAL_CODES for f in flags)
        report.summary = self._compose_summary(report)
        return report

    @staticmethod
    def _compose_summary(report: FlagReport) -> str:
        n_err = len(report.flags_by_severity.get(QASeverity.ERROR, []))
        n_warn = len(report.flags_by_severity.get(QASeverity.WARN, []))
        n_info = len(report.flags_by_severity.get(QASeverity.INFO, []))
        n_total = n_err + n_warn + n_info
        if n_total == 0:
            return "QA: clean — no findings raised."
        verdict = "REVIEW REQUIRED" if report.has_critical else "review recommended"
        return (
            f"QA: {n_total} finding(s) — "
            f"{n_err} error, {n_warn} warn, {n_info} info. "
            f"({verdict})"
        )
