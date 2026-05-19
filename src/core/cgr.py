"""
Corrosion-growth-rate (CGR) computation across matched + unmatched defects.

Three modes, project-selectable via config:

  * `FEATURE_SPECIFIC` — each defect uses its own growth rate. For unmatched
    run-2 features (new defects, or defects below run-1's POD threshold)
    the depth-at-run-1 is *assumed* equal to the tool's detection threshold
    (typically 10 % WT), so they still get a non-zero growth rate.

  * `POPULATION_ONLY` — pool feature-specific CGRs, take a configurable
    quantile (default 95th percentile) separately for internal and external
    populations, assign every defect its surface's P95. Useful where the
    matching coverage is too sparse to trust individual rates.

  * `HYBRID` (recommended for Indian projects) — compute feature-specific
    CGRs, then *floor* each defect at its surface's P95. A defect that
    happens to look slow doesn't escape the population's typical bound,
    but a fast defect keeps its actual rate.

The published Kandla-Samakhiali numbers (internal P95 0.0625 mm/yr,
external P95 0.0339 mm/yr) are reproduced from this module's HYBRID-mode
output when run on the matched + 10 %-assumed unmatched defect set, which
is what the report's analysts used. See tests/test_cgr.py.

QA flags emitted (codes in `src.validation.QAFlagCode`):

  * `NEGATIVE_GROWTH` — d_new < d_old; feature got shallower (re-measurement
    error). CGR clamped to 0.
  * `EXTREME_CGR` — feature-specific CGR > 1.0 mm/yr; deserves attention.
  * `POPULATION_FLOOR_APPLIED` — feature CGR was below the surface P95 and
    was uplifted. Hybrid mode only.
  * `UNMATCHED_RUN2` — depth_old was assumed = 10 % WT (POD threshold).
  * `DEPTH_BELOW_TOL` — measured depth delta is smaller than the tool's
    sizing tolerance (default ±10 % WT for general corrosion at 80 %
    confidence). The CGR is statistically indistinguishable from noise.

The CGRCalculator never reads inspection dates itself — the caller passes
`years_between` (typically `Project.years_between_runs`). If the caller
can't determine the interval, it should raise rather than guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.models import (
    CGRMode,
    Feature,
    MatchResult,
    Surface,
)
from src.validation import QAFlag, QAFlagCode, QASeverity, make_flag


DEFAULT_CONFIG: dict[str, Any] = {
    # Which of the three modes to apply
    "mode": "hybrid",

    # Population quantile when bucketing (HYBRID / POPULATION_ONLY)
    "population_quantile": 0.95,
    # Bucket the population by surface (internal vs external) — set False
    # to apply a single P95 across both
    "split_by_surface": True,

    # Pin negative growth at zero. Corrosion doesn't physically shrink;
    # apparent shrinkage is re-measurement noise.
    "floor_negative_at_zero": True,

    # Unmatched run-2 features (new defects OR below run-1 POD): assume
    # depth at run-1 = the tool's POD threshold. NOT zero — see module
    # docstring.
    "unmatched_depth_assumption_pct_wt": 10.0,

    # CGR > this -> EXTREME_CGR flag (informational)
    "extreme_cgr_threshold_mm_yr": 1.0,

    # Tool depth-sizing tolerance — typically ±10 % WT for general corr.
    # If the matched depth delta is smaller than this × WT, the growth
    # signal is below tool noise; we flag (don't drop).
    "tool_depth_tolerance_pct_wt": 10.0,
    "flag_below_tool_tolerance": True,
}


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class CGRResult:
    """The CGR-and-friends for a single run-2 feature.

    `cgr_mm_yr` is the value to use downstream (CGR-projection, repair
    prediction). `feature_cgr_mm_yr` is the raw individual rate before any
    population floor was applied — keep both so audit/report code can show
    'rate would have been X, raised to Y by the P95 floor'.
    """
    feature: Feature                            # the run-2 feature
    matched_to_run1: Feature | None             # run-1 partner; None if unmatched
    cgr_mm_yr: float                            # the value to use
    feature_cgr_mm_yr: float                    # individual rate before floor
    mode_used: str                              # see _MODE_USED_* below
    depth_old_used_mm: float                    # actual or assumed
    depth_new_mm: float
    years_between: float
    population_p95_mm_yr: float | None = None   # the surface's P95 (if applicable)
    qa_flags: list[QAFlag] = field(default_factory=list)


_MODE_USED_FEATURE_SPECIFIC = "feature_specific"
_MODE_USED_POPULATION_FLOOR = "population_floor"
_MODE_USED_POPULATION_ONLY = "population_only"


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

class CGRCalculator:
    """Produce a CGRResult for every run-2 defect (matched + unmatched).

    Inputs:
      - `match_result`: output of `src.core.defect_matcher.DefectMatcher`
      - `years_between`: interval between the two pig runs (caller's
         responsibility — typically from `Project.years_between_runs`)
      - `config`: optional overrides; merged shallow over `DEFAULT_CONFIG`

    Output: list of CGRResult, one per (matched run-2 feature ∪ unmatched
    run-2 feature). Unmatched *run-1* features are excluded — they don't
    appear in run-2 so they have no forward projection.
    """

    def __init__(self, config: dict | None = None):
        self.cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)

    # ------------------------------------------------------------------

    def compute(
        self,
        match_result: MatchResult,
        years_between: float,
        config: dict | None = None,
    ) -> list[CGRResult]:
        if years_between is None or years_between <= 0.0:
            raise ValueError(
                f"years_between must be a positive number; got {years_between!r}. "
                "Pull this from Project.years_between_runs (which raises if the "
                "inspection dates aren't set on the two ILIRuns)."
            )

        cfg = {**self.cfg, **(config or {})}
        mode = _coerce_mode(cfg["mode"])

        # ---- Step 1: feature-specific CGR for every run-2 defect.
        results: list[CGRResult] = []
        for fm in match_result.feature_matches:
            results.append(self._cgr_for_matched(fm.feature_old, fm.feature_new,
                                                  years_between, cfg))
        for f in match_result.unmatched_features_new:
            results.append(self._cgr_for_unmatched_new(f, years_between, cfg))

        if mode is CGRMode.FEATURE_SPECIFIC:
            return results

        # ---- Step 2: population P95 by surface (or unified if !split).
        p95_by_surface = self._compute_p95_by_surface(
            results,
            quantile=float(cfg["population_quantile"]),
            split_by_surface=bool(cfg["split_by_surface"]),
        )

        # ---- Step 3: apply floor / replacement.
        for r in results:
            surf_p95 = p95_by_surface.get(self._surface_key(r.feature, cfg))
            r.population_p95_mm_yr = surf_p95
            if surf_p95 is None or not (surf_p95 > 0):
                # No population data for this surface; leave feature-specific value.
                continue

            if mode is CGRMode.POPULATION_ONLY:
                r.cgr_mm_yr = surf_p95
                r.mode_used = _MODE_USED_POPULATION_ONLY
                continue

            # HYBRID
            if r.feature_cgr_mm_yr < surf_p95:
                r.cgr_mm_yr = surf_p95
                r.mode_used = _MODE_USED_POPULATION_FLOOR
                r.qa_flags.append(make_flag(
                    QAFlagCode.POPULATION_FLOOR_APPLIED,
                    f"feature CGR {r.feature_cgr_mm_yr:.4f} mm/yr below "
                    f"surface ({r.feature.surface.value}) P95 "
                    f"{surf_p95:.4f}; raised to floor",
                    feature_id=r.feature.anomaly_id,
                    context={
                        "feature_cgr_mm_yr": r.feature_cgr_mm_yr,
                        "p95_mm_yr": surf_p95,
                        "surface": r.feature.surface.value,
                    },
                ))

        return results

    # ------------------------------------------------------------------
    # Per-feature CGR
    # ------------------------------------------------------------------

    def _cgr_for_matched(
        self,
        f_old: Feature,
        f_new: Feature,
        years_between: float,
        cfg: dict[str, Any],
    ) -> CGRResult:
        d_old_mm = f_old.depth_mm or 0.0
        d_new_mm = f_new.depth_mm or 0.0
        return self._build_result(
            feature=f_new,
            matched_to_run1=f_old,
            d_old_mm=d_old_mm,
            d_new_mm=d_new_mm,
            years_between=years_between,
            unmatched=False,
            cfg=cfg,
        )

    def _cgr_for_unmatched_new(
        self,
        f: Feature,
        years_between: float,
        cfg: dict[str, Any],
    ) -> CGRResult:
        # Depth at run-1 was below the tool POD threshold; assume it was AT
        # the threshold (most conservative non-zero option).
        wt = f.wt_mm
        assumed_pct = float(cfg["unmatched_depth_assumption_pct_wt"])
        if wt is None or wt <= 0:
            d_old_mm = 0.0  # cannot compute without WT — falls back to 0
        else:
            d_old_mm = assumed_pct / 100.0 * wt
        d_new_mm = f.depth_mm or 0.0
        return self._build_result(
            feature=f,
            matched_to_run1=None,
            d_old_mm=d_old_mm,
            d_new_mm=d_new_mm,
            years_between=years_between,
            unmatched=True,
            cfg=cfg,
        )

    # ------------------------------------------------------------------

    def _build_result(
        self,
        *,
        feature: Feature,
        matched_to_run1: Feature | None,
        d_old_mm: float,
        d_new_mm: float,
        years_between: float,
        unmatched: bool,
        cfg: dict[str, Any],
    ) -> CGRResult:
        delta_mm = d_new_mm - d_old_mm
        raw_cgr = delta_mm / years_between

        flags: list[QAFlag] = []
        if unmatched:
            flags.append(make_flag(
                QAFlagCode.UNMATCHED_RUN2,
                f"depth_old assumed = "
                f"{cfg['unmatched_depth_assumption_pct_wt']:.1f}% WT "
                "(tool POD threshold) — feature was below run-1 detection.",
                feature_id=feature.anomaly_id,
                context={"assumed_pct_wt": cfg["unmatched_depth_assumption_pct_wt"]},
            ))

        if delta_mm < 0 and bool(cfg.get("floor_negative_at_zero", True)):
            flags.append(make_flag(
                QAFlagCode.NEGATIVE_GROWTH,
                f"apparent shrinkage Δd={delta_mm:.3f} mm "
                f"({d_old_mm:.3f} -> {d_new_mm:.3f}); clamped to 0",
                feature_id=feature.anomaly_id,
                context={"delta_mm": delta_mm},
            ))
            feature_cgr = 0.0
        else:
            feature_cgr = max(0.0, raw_cgr) if cfg.get("floor_negative_at_zero", True) else raw_cgr

        # DEPTH_BELOW_TOL — for matched features, when the measured delta
        # is smaller than the tool's depth-sizing tolerance.
        if (
            not unmatched
            and bool(cfg.get("flag_below_tool_tolerance", True))
            and feature.wt_mm
            and feature.wt_mm > 0
        ):
            tol_mm = float(cfg["tool_depth_tolerance_pct_wt"]) / 100.0 * float(feature.wt_mm)
            if abs(delta_mm) < tol_mm:
                flags.append(make_flag(
                    QAFlagCode.DEPTH_BELOW_TOL,
                    f"|Δdepth| {abs(delta_mm):.3f} mm < tool tolerance "
                    f"{tol_mm:.3f} mm ({cfg['tool_depth_tolerance_pct_wt']:.0f}% "
                    f"× WT {feature.wt_mm} mm); CGR is below tool noise",
                    feature_id=feature.anomaly_id,
                    context={"delta_mm": delta_mm, "tolerance_mm": tol_mm},
                ))

        # EXTREME_CGR — flag based on the per-feature rate, not on the
        # floored/replaced value (population floors aren't anomalies).
        if feature_cgr > float(cfg.get("extreme_cgr_threshold_mm_yr", 1.0)):
            flags.append(make_flag(
                QAFlagCode.EXTREME_CGR,
                f"feature-specific CGR {feature_cgr:.3f} mm/yr exceeds "
                f"{cfg['extreme_cgr_threshold_mm_yr']:.2f} mm/yr — fast growth",
                feature_id=feature.anomaly_id,
                context={"feature_cgr_mm_yr": feature_cgr},
            ))

        return CGRResult(
            feature=feature,
            matched_to_run1=matched_to_run1,
            cgr_mm_yr=feature_cgr,
            feature_cgr_mm_yr=feature_cgr,
            mode_used=_MODE_USED_FEATURE_SPECIFIC,
            depth_old_used_mm=d_old_mm,
            depth_new_mm=d_new_mm,
            years_between=years_between,
            qa_flags=flags,
        )

    # ------------------------------------------------------------------
    # Population P95 by surface
    # ------------------------------------------------------------------

    def _surface_key(self, f: Feature, cfg: dict[str, Any]) -> Any:
        """Key used to bucket the population. With split_by_surface=False,
        every feature shares the same bucket."""
        if bool(cfg["split_by_surface"]):
            return f.surface
        return "all"

    def _compute_p95_by_surface(
        self,
        results: list[CGRResult],
        quantile: float,
        split_by_surface: bool,
    ) -> dict[Any, float]:
        """Pool feature-specific CGRs (after negative-clamping) by surface,
        return {surface: P95}. Surfaces with too few samples (<2) get None
        and the floor doesn't apply.
        """
        if not results:
            return {}

        buckets: dict[Any, list[float]] = {}
        for r in results:
            key = r.feature.surface if split_by_surface else "all"
            buckets.setdefault(key, []).append(r.feature_cgr_mm_yr)

        q_pct = float(quantile) * 100.0
        out: dict[Any, float] = {}
        for key, vals in buckets.items():
            if len(vals) < 2:
                # Single-sample percentile is degenerate; skip so the floor
                # never gets applied to a barely-populated bucket.
                continue
            out[key] = float(np.percentile(np.asarray(vals, dtype=np.float64), q_pct))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_mode(value: Any) -> CGRMode:
    """Accept either CGRMode or a string. Raise on anything else."""
    if isinstance(value, CGRMode):
        return value
    if isinstance(value, str):
        try:
            return CGRMode(value)
        except ValueError as e:
            valid = ", ".join(m.value for m in CGRMode)
            raise ValueError(
                f"unknown CGR mode {value!r}; expected one of: {valid}"
            ) from e
    raise TypeError(f"mode must be CGRMode or str; got {type(value).__name__}")


def years_between_runs(
    inspection_date_run1, inspection_date_run2
) -> float:
    """Compute the interval in years (365.25-day) between two inspection
    dates. Raises a clear ValueError if either side is None — the spec
    explicitly says we don't guess.
    """
    if inspection_date_run1 is None or inspection_date_run2 is None:
        raise ValueError(
            "inspection dates must be set on both ILIRuns to compute "
            "years_between. Got "
            f"run1={inspection_date_run1!r}, run2={inspection_date_run2!r}."
        )
    delta = inspection_date_run2 - inspection_date_run1
    return abs(delta.days) / 365.25
