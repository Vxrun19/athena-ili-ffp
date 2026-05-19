"""
Repair-date prediction: project each defect forward in time and find when
either of the two repair triggers fires first.

Triggers (whichever fires earlier wins):

  * `DEPTH_80`  — `depth ≥ 80 % WT`
  * `ERF_1.0`   — `MAOP / Psafe ≥ 1.0`

If neither trigger fires within the configured horizon (default 10 years),
the prediction is `NONE_WITHIN_HORIZON` and the final depth + ERF at the
end of the horizon are recorded.

Algorithm — year-by-year, not continuous:

    year_offset = 0:   evaluate the current_ffp passed in (year-0 state).
                        if already triggered, return immediately.
    year_offset = 1..H: grow depth by cgr_mm_yr · 1.0, recompute FFP at
                        the new depth, check triggers.

For circumferential defects the year-0 FFP is whichever method was
controlling in `ffp_assess`'s output (B31G or Kastner); the predictor
projects that same method each year. If the controlling method would
flip mid-projection (rare in practice), the report will miss it —
documented limitation, OK for v0.1.

For unmatched run-2 features (where the CGR used the 10 %-WT depth
assumption), the result carries `UNMATCHED_RUN2` so report rendering
can mark the row.

Performance: each year of projection costs one FFP-method call
(~5-10 µs in pure Python). HMEL's 106 k features × ~10 years ≈ 1 M
FFP calls ≈ 20 s end-to-end — well inside the 60 s budget. Kandla's
333 features completes in well under 1 s.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from src.core.cgr import CGRResult
from src.core.ffp import (
    b31g_modified,
    b31g_original,
    dnv_rp_f101,
    kastner,
    rstreng,
)
from src.models import (
    DimensionClass,
    FFPMethod,
    FFPResult,
    Feature,
    MAOPZone,
    Pipeline,
    RepairPrediction,
)
from src.validation import QAFlag, QAFlagCode, QASeverity


# Trigger string constants — mirrors the user spec wording verbatim so
# report code and test asserts can grep for them.
TRIGGER_DEPTH_80 = "DEPTH_80"
TRIGGER_ERF_1 = "ERF_1.0"
TRIGGER_NONE = "NONE_WITHIN_HORIZON"

# Approximate days/year — used to translate year_offset into a calendar
# date. Tracks the rest of the toolchain (CGR `years_between_runs`).
_DAYS_PER_YEAR = 365.25

DEFAULT_CONFIG: dict[str, Any] = {
    "horizon_years": 10,
    "depth_trigger_pct_wt": 80.0,
    "erf_trigger": 1.0,
}


# Method dispatch — bypasses the coordinator's MAOP-zone lookup and
# auto-Kastner branching, since for the projection we already know the
# zone (resolved once at year 0) and the controlling method (told to us
# via current_ffp).
_METHOD_FN = {
    FFPMethod.B31G_ORIGINAL: b31g_original,
    FFPMethod.B31G_MODIFIED: b31g_modified,
    FFPMethod.RSTRENG: rstreng,
    FFPMethod.DNV_RP_F101: dnv_rp_f101,
    FFPMethod.KASTNER: kastner,
}


# ---------------------------------------------------------------------------
# Predictor
# ---------------------------------------------------------------------------

class RepairPredictor:
    """Year-by-year projection. See module docstring for algorithm and
    performance characteristics.
    """

    def __init__(self, config: dict | None = None):
        self.cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)

    # ------------------------------------------------------------------

    def predict_one(
        self,
        cgr_result: CGRResult,
        current_ffp: FFPResult,
        pipeline: Pipeline,
        *,
        run2_inspection_date: date | None = None,
        config: dict | None = None,
    ) -> RepairPrediction:
        cfg = {**self.cfg, **(config or {})}
        horizon = int(cfg["horizon_years"])
        depth_trigger_pct = float(cfg["depth_trigger_pct_wt"])
        erf_trigger = float(cfg["erf_trigger"])

        feature = cgr_result.feature
        if feature.wt_mm is None or feature.wt_mm <= 0:
            raise ValueError(
                f"feature {feature.anomaly_id!r}: wt_mm required for repair prediction"
            )

        cgr_mm_yr = float(cgr_result.cgr_mm_yr)
        wt = float(feature.wt_mm)
        method = current_ffp.method
        method_fn = _METHOD_FN[method]

        # Resolve the MAOP zone once. Use the same zone the year-0 FFP
        # used (carried on current_ffp.maop_kgcm2) — bypass zone-lookup
        # so tests can pass an explicit MAOP without a Pipeline
        # scaffold. v0.3.0: mode-aware via maop_for_feature.
        if pipeline.maop_zones:
            zone, _idx, _fb = pipeline.maop_for_feature(feature)
        else:
            zone = None
        if zone is not None and zone.maop_kgcm2 != current_ffp.maop_kgcm2:
            # Caller might be projecting with an override MAOP — trust
            # current_ffp's MAOP over the pipeline's zone lookup.
            maop_kgcm2 = current_ffp.maop_kgcm2
            Fd = zone.design_factor
        elif zone is not None:
            maop_kgcm2 = zone.maop_kgcm2
            Fd = zone.design_factor
        else:
            maop_kgcm2 = current_ffp.maop_kgcm2
            # Without a zone we can't read Fd — fall back to a typical 0.72.
            Fd = 0.72

        base_kwargs = _build_base_kwargs(
            method, feature, pipeline, maop_kgcm2, Fd
        )

        # Carry across any QA flags the CGR step already raised for this
        # feature (UNMATCHED_RUN2 in particular).
        qa_flags: list[QAFlag] = list(cgr_result.qa_flags or [])

        # Year 0: use the FFP we were given.
        d_mm = feature.depth_mm or 0.0
        d_pct = 100.0 * d_mm / wt if wt > 0 else 0.0
        yearly_results: list[FFPResult] = [current_ffp]

        if d_pct >= depth_trigger_pct:
            return _build_prediction(
                cgr_result=cgr_result,
                method=method,
                yearly=yearly_results,
                horizon=horizon,
                triggered=True,
                trigger=TRIGGER_DEPTH_80,
                year_offset=0,
                final_d_mm=d_mm,
                final_pct=d_pct,
                final_erf=current_ffp.erf,
                final_psafe=current_ffp.sop_kgcm2,
                run2_date=run2_inspection_date,
                qa_flags=qa_flags,
            )
        if current_ffp.erf >= erf_trigger:
            return _build_prediction(
                cgr_result=cgr_result,
                method=method,
                yearly=yearly_results,
                horizon=horizon,
                triggered=True,
                trigger=TRIGGER_ERF_1,
                year_offset=0,
                final_d_mm=d_mm,
                final_pct=d_pct,
                final_erf=current_ffp.erf,
                final_psafe=current_ffp.sop_kgcm2,
                run2_date=run2_inspection_date,
                qa_flags=qa_flags,
            )

        # Year 1 .. horizon: grow depth, recompute FFP, check triggers.
        for year_offset in range(1, horizon + 1):
            d_mm += cgr_mm_yr
            d_pct = 100.0 * d_mm / wt

            if d_pct >= depth_trigger_pct:
                return _build_prediction(
                    cgr_result=cgr_result,
                    method=method,
                    yearly=yearly_results,
                    horizon=horizon,
                    triggered=True,
                    trigger=TRIGGER_DEPTH_80,
                    year_offset=year_offset,
                    final_d_mm=d_mm,
                    final_pct=d_pct,
                    # ERF at the moment depth hit 80 % — we ran FFP at the
                    # PREVIOUS year, so use that as the last-known ERF.
                    final_erf=yearly_results[-1].erf if yearly_results else current_ffp.erf,
                    final_psafe=yearly_results[-1].sop_kgcm2 if yearly_results else current_ffp.sop_kgcm2,
                    run2_date=run2_inspection_date,
                    qa_flags=qa_flags,
                )

            new_ffp = _ffp_at_depth(method_fn, method, d_mm, d_pct, wt, base_kwargs)
            yearly_results.append(new_ffp)

            if new_ffp.erf >= erf_trigger:
                return _build_prediction(
                    cgr_result=cgr_result,
                    method=method,
                    yearly=yearly_results,
                    horizon=horizon,
                    triggered=True,
                    trigger=TRIGGER_ERF_1,
                    year_offset=year_offset,
                    final_d_mm=d_mm,
                    final_pct=d_pct,
                    final_erf=new_ffp.erf,
                    final_psafe=new_ffp.sop_kgcm2,
                    run2_date=run2_inspection_date,
                    qa_flags=qa_flags,
                )

        # No trigger fired — feature stays below both triggers for the horizon.
        last = yearly_results[-1]
        return _build_prediction(
            cgr_result=cgr_result,
            method=method,
            yearly=yearly_results,
            horizon=horizon,
            triggered=False,
            trigger=TRIGGER_NONE,
            year_offset=None,
            final_d_mm=d_mm,
            final_pct=d_pct,
            final_erf=last.erf,
            final_psafe=last.sop_kgcm2,
            run2_date=run2_inspection_date,
            qa_flags=qa_flags,
        )

    # ------------------------------------------------------------------

    def predict(
        self,
        cgr_results: list[CGRResult],
        current_ffp_by_feature_id: dict[str, FFPResult],
        pipeline: Pipeline,
        *,
        run2_inspection_date: date | None = None,
        config: dict | None = None,
    ) -> list[RepairPrediction]:
        """Run `predict_one` for every CGR result.

        `current_ffp_by_feature_id` maps `Feature.anomaly_id` to its year-0
        FFPResult (typically the controlling result from `ffp_assess`).
        Features missing from the dict are skipped with no error — the
        caller is expected to do its own bookkeeping if it cares.
        """
        out: list[RepairPrediction] = []
        for cgr in cgr_results:
            ffp = current_ffp_by_feature_id.get(cgr.feature.anomaly_id)
            if ffp is None:
                continue
            out.append(
                self.predict_one(
                    cgr, ffp, pipeline,
                    run2_inspection_date=run2_inspection_date,
                    config=config,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_base_kwargs(
    method: FFPMethod,
    feature: Feature,
    pipeline: Pipeline,
    maop_kgcm2: float,
    Fd: float,
) -> dict[str, Any]:
    base = dict(
        t_mm=feature.wt_mm,
        D_mm=pipeline.diameter_mm,
        smys_mpa=pipeline.smys_mpa,
        Fd=Fd,
        maop_kgcm2=maop_kgcm2,
        feature_id=feature.anomaly_id,
    )
    if method is FFPMethod.KASTNER:
        if feature.width_mm is None:
            raise ValueError(
                f"feature {feature.anomaly_id!r}: width_mm required to project Kastner"
            )
        base["W_mm"] = feature.width_mm
    else:
        if feature.length_mm is None:
            raise ValueError(
                f"feature {feature.anomaly_id!r}: length_mm required to project {method.value}"
            )
        base["L_mm"] = feature.length_mm
    return base


def _ffp_at_depth(
    method_fn,
    method: FFPMethod,
    d_mm: float,
    d_pct: float,
    wt: float,
    base_kwargs: dict[str, Any],
) -> FFPResult:
    """One projected FFP call. We bypass the coordinator (no MAOP-zone
    lookup, no Kastner auto-run) — the base_kwargs were resolved once at
    year 0 and the controlling method is fixed for the projection.

    If the projected depth has exceeded WT (d/t > 1.0), we cap at 100 %
    to avoid the Feature dataclass's validation tripping if the caller
    later constructs a Feature. The FFP method itself handles d ≥ t
    sensibly (Psafe → 0).
    """
    capped_d_mm = min(d_mm, wt * 0.999)            # numerical safety
    return method_fn(d_mm=capped_d_mm, **base_kwargs)


def _build_prediction(
    *,
    cgr_result: CGRResult,
    method: FFPMethod,
    yearly: list[FFPResult],
    horizon: int,
    triggered: bool,
    trigger: str,
    year_offset: int | None,
    final_d_mm: float,
    final_pct: float,
    final_erf: float,
    final_psafe: float,
    run2_date: date | None,
    qa_flags: list[QAFlag],
) -> RepairPrediction:
    repair_date: date | None = None
    if triggered and year_offset is not None and run2_date is not None:
        repair_date = run2_date + timedelta(days=int(year_offset * _DAYS_PER_YEAR))

    return RepairPrediction(
        feature_id=cgr_result.feature.anomaly_id,
        feature=cgr_result.feature,
        cgr_mm_per_year=cgr_result.cgr_mm_yr,
        yearly_assessments=yearly,
        predicted_repair_date=repair_date,
        repair_trigger=trigger,
        repair_year_offset=year_offset,
        final_depth_pct_wt=final_pct,
        final_depth_mm=final_d_mm,
        final_erf=final_erf,
        final_psafe_kgcm2=final_psafe,
        method_used=method,
        horizon_years=horizon,
        qa_flags=qa_flags,
    )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def horizon_end_date(run2_inspection_date: date, horizon_years: int) -> date:
    """Calendar date at the end of the projection horizon. Useful for
    report rendering ('After March 2033')."""
    return run2_inspection_date + timedelta(days=int(horizon_years * _DAYS_PER_YEAR))
