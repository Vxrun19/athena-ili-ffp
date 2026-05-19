"""
Full QA-flag taxonomy and the canonical severity mapping.

A flag is a structured QA finding emitted by any pipeline stage (reader,
aligner, matcher, CGR, FFP, predictor, aggregator). Every stage attaches
flags to its result object — `ILIRun.qa_flags`, `JointAlignment.qa_flags`,
`MatchResult.qa_flags`, `CGRResult.qa_flags`, `FFPResult.qa_flags`,
`RepairPrediction.qa_flags` — and the `FlagAggregator` in
`src/validation/flag_aggregator.py` collects them into a single
`FlagReport` for the final DOCX / Excel deliverable.

Severities:

  * **ERROR** — immediate human review required (a critical condition is
    already true: a defect's ERF exceeds 1.0, a required column couldn't
    be parsed, etc.).
  * **WARN**  — worth reviewing before finalising the report; the tool
    produced output but a value is at the edge of its calibration or a
    methodology assumption was loosened.
  * **INFO**  — expected behaviour, documented for the audit trail.

Every flag's severity is centrally fixed via `CANONICAL_SEVERITY`. Modules
use `make_flag(code, message, ...)` instead of constructing `QAFlag`
directly so the severity stays consistent across emitters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class QASeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Full flag taxonomy
# ---------------------------------------------------------------------------

class QAFlagCode(str, Enum):
    """Stable identifiers for every kind of QA finding the tool produces.

    Codes are grouped by emitting stage in this file. Membership of a
    group is a comment, not enforced by the enum — same code can in
    principle be raised by multiple stages (the aggregator dedupes by
    (code, feature_id)).
    """
    # --- Reader -----------------------------------------------------------
    COORDINATES_SWAPPED = "COORDINATES_SWAPPED"
    LAT_LON_OUT_OF_BOUNDS = "LAT_LON_OUT_OF_BOUNDS"
    SHEET_NOT_DETECTED = "SHEET_NOT_DETECTED"
    HEADER_ROW_AMBIGUOUS = "HEADER_ROW_AMBIGUOUS"
    MISSING_COLUMN = "MISSING_COLUMN"
    SURFACE_VALUE_UNKNOWN = "SURFACE_VALUE_UNKNOWN"
    CLOCK_VALUE_UNKNOWN = "CLOCK_VALUE_UNKNOWN"
    RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE = "RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE"

    # --- Joint alignment + defect matching --------------------------------
    LOW_JOINT_MATCH_RATE = "LOW_JOINT_MATCH_RATE"
    REVERSAL_DETECTED = "REVERSAL_DETECTED"
    LENGTH_MISMATCH_RUN = "LENGTH_MISMATCH_RUN"
    LOW_DEFECT_MATCH_RATE = "LOW_DEFECT_MATCH_RATE"
    NO_CLUSTERS_IN_EITHER_RUN = "NO_CLUSTERS_IN_EITHER_RUN"

    # --- CGR --------------------------------------------------------------
    NEGATIVE_GROWTH = "NEGATIVE_GROWTH"
    EXTREME_CGR = "EXTREME_CGR"
    POPULATION_FLOOR_APPLIED = "POPULATION_FLOOR_APPLIED"
    UNMATCHED_RUN2 = "UNMATCHED_RUN2"
    DEPTH_BELOW_TOL = "DEPTH_BELOW_TOL"

    # --- FFP --------------------------------------------------------------
    ERF_EXCEEDS_1 = "ERF_EXCEEDS_1"
    DEPTH_EXCEEDS_80 = "DEPTH_EXCEEDS_80"
    LONG_DEFECT_OUTSIDE_B31G = "LONG_DEFECT_OUTSIDE_B31G"
    VERY_SHORT_DEFECT = "VERY_SHORT_DEFECT"
    MAOP_ZONE_NOT_FOUND = "MAOP_ZONE_NOT_FOUND"

    # --- Repair predictor + pipeline-level --------------------------------
    REPAIR_PREDICTED_WITHIN_HORIZON = "REPAIR_PREDICTED_WITHIN_HORIZON"
    HIGH_CGR_POPULATION = "HIGH_CGR_POPULATION"


# ---------------------------------------------------------------------------
# Canonical severity per code
# ---------------------------------------------------------------------------

# Set once here so the aggregator buckets correctly regardless of which
# stage emitted the flag. Anything not listed defaults to INFO.
CANONICAL_SEVERITY: dict[QAFlagCode, QASeverity] = {
    # ERROR — human review required immediately
    QAFlagCode.ERF_EXCEEDS_1: QASeverity.ERROR,
    QAFlagCode.DEPTH_EXCEEDS_80: QASeverity.ERROR,
    QAFlagCode.MISSING_COLUMN: QASeverity.ERROR,
    QAFlagCode.SHEET_NOT_DETECTED: QASeverity.ERROR,
    QAFlagCode.LAT_LON_OUT_OF_BOUNDS: QASeverity.ERROR,

    # WARN — review before finalising the report
    QAFlagCode.EXTREME_CGR: QASeverity.WARN,
    QAFlagCode.LOW_JOINT_MATCH_RATE: QASeverity.WARN,
    QAFlagCode.LOW_DEFECT_MATCH_RATE: QASeverity.WARN,
    QAFlagCode.LONG_DEFECT_OUTSIDE_B31G: QASeverity.WARN,
    QAFlagCode.REPAIR_PREDICTED_WITHIN_HORIZON: QASeverity.WARN,
    QAFlagCode.REVERSAL_DETECTED: QASeverity.WARN,
    QAFlagCode.LENGTH_MISMATCH_RUN: QASeverity.WARN,
    QAFlagCode.HEADER_ROW_AMBIGUOUS: QASeverity.WARN,
    QAFlagCode.VERY_SHORT_DEFECT: QASeverity.WARN,
    QAFlagCode.MAOP_ZONE_NOT_FOUND: QASeverity.WARN,
    QAFlagCode.HIGH_CGR_POPULATION: QASeverity.WARN,

    # INFO — expected behaviour, documented for traceability
    QAFlagCode.NEGATIVE_GROWTH: QASeverity.INFO,
    QAFlagCode.POPULATION_FLOOR_APPLIED: QASeverity.INFO,
    QAFlagCode.UNMATCHED_RUN2: QASeverity.INFO,
    QAFlagCode.DEPTH_BELOW_TOL: QASeverity.INFO,
    QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN: QASeverity.INFO,
    QAFlagCode.COORDINATES_SWAPPED: QASeverity.INFO,
    QAFlagCode.RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE: QASeverity.INFO,
    QAFlagCode.SURFACE_VALUE_UNKNOWN: QASeverity.INFO,
    QAFlagCode.CLOCK_VALUE_UNKNOWN: QASeverity.INFO,
}


def severity_for(code: QAFlagCode) -> QASeverity:
    """Single source of truth for a flag's severity."""
    return CANONICAL_SEVERITY.get(code, QASeverity.INFO)


# ---------------------------------------------------------------------------
# Flag dataclass + constructor
# ---------------------------------------------------------------------------

@dataclass
class QAFlag:
    code: QAFlagCode
    message: str
    severity: QASeverity = QASeverity.WARN        # overridden by make_flag()
    source_row: int | None = None
    feature_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        loc = f" [row {self.source_row}]" if self.source_row is not None else ""
        fid = f" feature={self.feature_id}" if self.feature_id else ""
        return f"{self.severity.value.upper()} {self.code.value}{loc}{fid}: {self.message}"

    @property
    def dedup_key(self) -> tuple[QAFlagCode, str | None]:
        """The key used by `FlagAggregator` to deduplicate flags emitted
        by multiple stages for the same (code, feature_id)."""
        return (self.code, self.feature_id)


def make_flag(
    code: QAFlagCode,
    message: str,
    *,
    feature_id: str | None = None,
    source_row: int | None = None,
    context: dict[str, Any] | None = None,
    severity: QASeverity | None = None,
) -> QAFlag:
    """Construct a flag with the canonical severity for its code.

    Pass `severity=...` to override the canonical mapping in unusual
    cases — but the aggregator still buckets by canonical severity. Use
    sparingly.
    """
    return QAFlag(
        code=code,
        message=message,
        severity=severity if severity is not None else severity_for(code),
        source_row=source_row,
        feature_id=feature_id,
        context=context or {},
    )
