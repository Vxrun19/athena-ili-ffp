"""ERF-distribution bucket classification.

Buckets are a **display-only** classification used by the Results
screen and any annexure-summary inset.

v0.3.2 convention (current default): bucket counts use the **raw
full-precision** ERF float, no rounding. This aligns the GUI Results
counter to the per-feature ERFs reported in Annexure D — re-bucketing
the annexure XLSX with raw float comparisons reproduces the GUI
counts exactly. The annexure XLSX is the engineering-ground-truth
deliverable; the GUI mirror should match it.

v0.2.6 mode (still supported via ``dp=3``): rounds each ERF to 3 dp
before comparing to the bucket boundaries. That matches the
convention used in some published Athena reports — a feature with
raw ERF ``0.8504`` displays as ``"0.850"`` and classifies as
``"≤ 0.85"`` rather than splitting at the underlying float boundary.
The 3-dp mode was found to match the BPCL Malarna PDF exactly but
diverges from the annexure XLSX by 10-20 features on customer-scale
runs (16 features on BPCL Malarna; 41 on BPCL Mathura-Piyala). Since
the XLSX is the file engineers actually re-process, the v0.3.2
default switches to raw-float comparison.

**Critical:** rounding (when used) is for COUNTS ONLY. Engineering
severity checks — ``ERF_EXCEEDS_1`` QA flag, repair triggers, etc. —
ALWAYS use the raw ERF float regardless of the bucket display mode.
A feature with raw ERF ``1.0001``:

  * triggers ``ERF_EXCEEDS_1`` (raw ≥ 1.0)
  * AND falls in the ``"> 1.00"`` display bucket under the default
    (raw) mode, since ``1.0001 > 1.00`` is True
  * Under the legacy ``dp=3`` mode it would round to ``1.000`` and
    fall in ``"0.90 < ERF ≤ 1.00"``. Engineers reading the bucket
    count should not interpret it as a safety floor — the QA flag is
    the authoritative severity signal.

The four canonical buckets — bracket boundaries chosen to match
the BPCL Malarna-Karwadi customer deliverable + the
``erf_bands`` definition in ``config/default_project.yaml``:

  * ``"≤ 0.85"``           — Acceptable
  * ``"0.85 < ERF ≤ 0.90"`` — Monitor
  * ``"0.90 < ERF ≤ 1.00"`` — Planned repair
  * ``"> 1.00"``           — Critical (also fires ``ERF_EXCEEDS_1``)

See ``docs/ENGINE_REFERENCE.md §11.2`` for the worked-example
discussion of why the default is raw-float.
"""
from __future__ import annotations

from typing import Iterable, Optional


# Bucket labels in display order. Tuple (label, (lo_open, hi_inclusive)).
# Comparison uses (erf > lo) AND (erf <= hi). The bottom bucket has
# lo = -inf so any value <= 0.85 lands here; the top bucket has
# hi = +inf so any value > 1.00 lands here.
_BUCKETS: list[tuple[str, float, float]] = [
    ("≤ 0.85",            float("-inf"), 0.85),
    ("0.85 < ERF ≤ 0.90", 0.85,          0.90),
    ("0.90 < ERF ≤ 1.00", 0.90,          1.00),
    ("> 1.00",            1.00,          float("inf")),
]

ERF_BUCKET_LABELS: tuple[str, ...] = tuple(label for label, _, _ in _BUCKETS)

# Optional display-precision rounding. The v0.2.6 default was 3 dp;
# v0.3.2 switched the production default to None (raw float). The
# constant is kept so callers can opt back into the rounded mode by
# passing ``dp=DEFAULT_BUCKET_DP`` explicitly.
DEFAULT_BUCKET_DP: int = 3


def erf_bucket_for(erf: float, *, dp: Optional[int] = None) -> str:
    """Return the display bucket for a single ERF.

    With ``dp=None`` (the v0.3.2 default) the raw float is compared
    directly to the bucket boundaries — this matches the per-feature
    ERFs reported in the annexure XLSX.

    With an integer ``dp`` (e.g. ``dp=3``) the value is rounded to
    that many decimal places before classifying — the v0.2.6
    display-precision mode, retained for callers that want PDF-style
    bucket counts.

    Boundary semantics (identical in both modes):

      * Lowest bucket is **inclusive of** its upper edge (``≤ 0.85``).
      * Mid buckets are open-low / closed-high (``> lo`` and ``≤ hi``).
      * Top bucket is open-low only (``> 1.00``).

    A value of exactly ``0.85`` lands in the lowest bucket; ``0.8501``
    lands in the middle bucket under the default raw mode.
    """
    e = float(erf)
    if dp is not None:
        e = round(e, dp)
    for label, lo, hi in _BUCKETS:
        if lo == float("-inf"):
            if e <= hi:
                return label
        elif hi == float("inf"):
            if e > lo:
                return label
        else:
            if e > lo and e <= hi:
                return label
    # Defensive fallback — should be unreachable.
    return _BUCKETS[-1][0]


def count_erf_buckets(
    ffps: Iterable,
    *,
    dp: Optional[int] = None,
) -> dict[str, int]:
    """Classify a sequence of :class:`FFPResult`-shaped objects into
    display buckets and return ``{label: count}``.

    Walks ``ffps`` once. Each item must expose an ``erf`` attribute.
    Items whose ``erf`` is ``None`` or non-finite are skipped (no
    bucket assigned). The returned dict always contains every bucket
    label as a key, even when the count is zero — so callers can
    iterate ``ERF_BUCKET_LABELS`` without worrying about KeyError.

    The default ``dp=None`` uses full-precision ERFs (v0.3.2 GUI
    default). Pass ``dp=3`` for the legacy display-precision mode.

    Use :func:`erf_bucket_for` for single-value classification.
    """
    out: dict[str, int] = {label: 0 for label in ERF_BUCKET_LABELS}
    import math
    for f in ffps:
        erf = getattr(f, "erf", None)
        if erf is None:
            continue
        try:
            ef = float(erf)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ef):
            continue
        out[erf_bucket_for(ef, dp=dp)] += 1
    return out


__all__ = [
    "ERF_BUCKET_LABELS",
    "DEFAULT_BUCKET_DP",
    "erf_bucket_for",
    "count_erf_buckets",
]
