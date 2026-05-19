"""
QA validation — flag taxonomy + aggregator.

The full system is in two files:
  - `qa_flags.py`         — the flag types (`QAFlag`, `QAFlagCode`,
                             `QASeverity`) and the canonical severity mapping
  - `flag_aggregator.py`  — `FlagReport` + `FlagAggregator`, which collect
                             flags from every pipeline stage into a single
                             report for the DOCX / Excel deliverable

Everything that was previously importable from `src.validation` still is:
`QAFlag`, `QAFlagCode`, `QASeverity`. The new `make_flag(...)` helper is
the recommended way to construct flags so severity stays consistent with
the canonical mapping.
"""
from .qa_flags import (
    CANONICAL_SEVERITY,
    QAFlag,
    QAFlagCode,
    QASeverity,
    make_flag,
    severity_for,
)

__all__ = [
    "QAFlag",
    "QAFlagCode",
    "QASeverity",
    "CANONICAL_SEVERITY",
    "make_flag",
    "severity_for",
]
