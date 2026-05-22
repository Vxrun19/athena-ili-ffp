"""
Fitness-for-purpose (FFP) assessment for metal-loss defects.

Five methods are implemented, all driven by primary references:

* **B31G Original** — ASME B31G-2012 Section 4 (Level 1, low-z branch) and
  Section 4 (high-z branch). Flow stress 1.1·SMYS. Used by most Indian
  liquid-pipeline operators by default.

* **B31G Modified** — ASME B31G-2012 Section 5. Sometimes called "Modified
  B31G" or "0.85 dL method". Flow stress SMYS + 69 MPa (≈ SMYS + 10 ksi).
  Folias factor branches at z = 50. Effective area = 0.85·d·L. Athena's
  HMEL-class projects use this as primary.

* **RSTRENG** — Pipeline Research Council's "effective-area" method
  (Kiefner & Vieth, 1989). Requires a measured river-bottom depth profile;
  the ILI data we ingest doesn't carry profiles, so we fall back to the
  0.85·d·L approximation, which is mathematically identical to B31G
  Modified when no profile is available. We still expose RSTRENG as a
  separate method so callers can run it as a cross-check (and so the
  field `using_approximate_profile=True` records the limitation).

* **DNV-RP-F101 Part B (ASD format)** — DNV-RP-F101 (2017) Section 4.
  Allowable Stress Design format matching India's design-factor approach.
  Uses UTS instead of flow stress; if only SMYS is known, UTS is estimated
  as SMYS + 110 MPa (standard line-pipe correlation) and a note is added
  to the result. Length-correction factor Q differs from B31G's M.

* **Kastner** — Originally Kastner (1986); the partial-depth circumferential
  variant we implement here is the **net-section approximation** documented
  in `project_v02_followups.md`. The 1986 paper's full equilibrium form is
  deferred to v0.2 once a CISL/CIGR feature with full-circumferential
  extent appears in real data and the published Psafe can validate. For
  Indian-scale defects (Kandla #125 etc.) the net-section form is well
  within the tool's ±2 % envelope and degrades correctly to σ_flow as
  d → 0 or W → 0.

The `ffp_assess(feature, pipeline, config)` coordinator looks up the
right MAOP zone by the feature's WT, runs the configured primary method,
runs any configured cross-checks, and for any feature whose
`dimension_class` is CISL or CIGR additionally runs Kastner and marks
whichever method gives the LOWER Psafe as the controlling case (via
`FFPResult.is_controlling`).

Every method works in SI internally (MPa, mm) and converts to kg/cm² at
the FFPResult boundary using `src.models.units.mpa_to_kgcm2`. ERF is
always MAOP/Psafe (high ERF = bad), per the locked project convention.

References:
- ASME B31G-2012, "Manual for Determining the Remaining Strength of
  Corroded Pipelines"
- DNV-RP-F101 (2017), "Corroded Pipelines"
- Kiefner, J. F. and Vieth, P. H. (1989), "A Modified Criterion for
  Evaluating the Remaining Strength of Corroded Pipe", AGA / PRCI
- Kastner, W. et al. (1986), "Critical Crack Sizes in Ductile Piping",
  International Journal of Pressure Vessels and Piping
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.models import (
    DimensionClass,
    FFPMethod,
    FFPResult,
    Feature,
    FeatureIdentification,
    Pipeline,
)
from src.models.units import mpa_to_kgcm2
from src.validation import QAFlagCode, make_flag


# Standard line-pipe correlation: UTS ≈ SMYS + 110 MPa for X-grade steels.
_UTS_OFFSET_MPA_DEFAULT = 110.0


# Feature-identification codes that B31G / RSTRENG / DNV / Kastner are
# NOT valid for. The reader (src/io/ili_reader.py) is supposed to drop
# these rows before they reach FFP, but a defense-in-depth guard here
# turns "silent garbage Psafe" (the Abu Road #1637 dent bug, ERF=8.57
# from 0.9%OD treated as 90%WT) into a noisy ValueError that surfaces
# the data leak instead of polluting the report. Keep this list in
# sync with `_NON_METAL_LOSS_FIDS` in src/io/ili_reader.py.
_NON_ASSESSABLE_FIDS: frozenset[FeatureIdentification] = frozenset({
    FeatureIdentification.DENT,
    FeatureIdentification.DENT_WITH_METAL_LOSS,
    FeatureIdentification.CRACK,
    FeatureIdentification.GIRTH_WELD_ANOMALY,
    FeatureIdentification.SPIRAL_WELD_ANOMALY,
    FeatureIdentification.LONG_WELD_ANOMALY,
    FeatureIdentification.RIPPLE,
})

# Default B31G-Original / Kastner flow-stress factor.
_SFLOW_FACTOR_B31G_ORIG = 1.1

# B31G-Modified / RSTRENG flow-stress offset.
_SFLOW_OFFSET_B31G_MOD_MPA = 69.0  # ≈ 10 ksi


DEFAULT_CONFIG: dict[str, Any] = {
    "primary_method": "B31G_Original",
    "cross_check_methods": [],                  # list of FFPMethod values (strings)
    "kastner_for_circumferential": True,
    "uts_offset_mpa": _UTS_OFFSET_MPA_DEFAULT,
}


# ---------------------------------------------------------------------------
# Kastner eligibility (v0.3.3)
# ---------------------------------------------------------------------------

def is_kastner_eligible(feature: Feature) -> bool:
    """Return True if this feature should be assessed by the Kastner approach.

    Kastner applies to defects oriented circumferentially (perpendicular to
    pipe axis), where axial tension is the controlling failure mode rather
    than hoop stress (which B31G / RSTRENG / DNV are calibrated against).

    Recognition uses three signals in priority order, with the POF enum
    treated as AUTHORITATIVE when present:

      1. **POF dimension_class enum** — the canonical POF-110 classification
         (``CIRCUMFERENTIAL_SLOTTING`` / ``CIRCUMFERENTIAL_GROOVING``). When
         the read pipeline produced a confident enum (anything other than
         ``UNDEFINED``), it wins outright: a feature classified as PITTING
         or AXIAL_SLOTTING is NOT eligible even if its width happens to
         exceed its length. This avoids the v0.3.3-iteration-1 overshoot
         where the geometric proxy added ~3000 PITTING/GENERAL/PINHOLE
         false positives on the BPCL Mathura-Piyala 1ZYT sample.

      2. **Raw-description substring match** — case-insensitive
         ``"circumferential"`` in ``feature.raw_description``. Only fires
         when the POF enum is ``UNDEFINED``. Catches pre-normalized
         stitched data and vendor formats whose POF column we couldn't map.

      3. **Geometric proxy** — ``width_mm > length_mm``. Only fires when
         the POF enum is ``UNDEFINED`` AND the description doesn't match.
         Fallback for unlabeled synthetic test packs. Requires both
         dimensions to be positive — does not false-positive on missing
         or zero data.

    Surfaced during the BPCL Mathura-Piyala 1ZYT validation cycle as the
    fix that took the estimated_erf_circ annexure count from 0 → 323. See
    docs/ENGINE_REFERENCE.md §9 (Annexure D / E Kastner).
    """
    # Signal 1: canonical POF dimension class — authoritative when known.
    if feature.dimension_class in (
        DimensionClass.CIRCUMFERENTIAL_SLOTTING,
        DimensionClass.CIRCUMFERENTIAL_GROOVING,
    ):
        return True
    # If POF has classified the feature as ANYTHING other than UNDEFINED,
    # trust the classification and reject. Don't second-guess the read
    # pipeline with a fuzzy substring or geometric heuristic.
    if feature.dimension_class is not DimensionClass.UNDEFINED:
        return False
    # POF unknown — fall back to softer signals.
    # Signal 2: free-text label substring (case-insensitive).
    label = (feature.raw_description or "").strip().lower()
    if "circumferential" in label:
        return True
    # Signal 3: geometric proxy.
    w = feature.width_mm
    L = feature.length_mm
    if w is not None and L is not None and w > 0 and L > 0:
        return w > L
    return False


# ---------------------------------------------------------------------------
# Result-flag attachment (shared by all methods)
# ---------------------------------------------------------------------------

# A defect length less than the wall thickness sits in the "pinhole" regime
# where B31G's parabolic-profile assumption is on the edge of its
# calibration. Flag (don't drop) so the report's QA section notes it.
_VERY_SHORT_DEFECT_RATIO = 1.0   # L < t

# z > 50 is outside B31G Original's calibration range (the Modified form
# extends it). We attach LONG_DEFECT_OUTSIDE_B31G to B31G Original results
# only.
_B31G_ORIG_LONG_DEFECT_Z = 50.0


def _attach_common_flags(
    result: FFPResult,
    *,
    check_very_short: bool = True,
) -> None:
    """Attach ERF / depth / very-short flags to a freshly-computed result.

    Method-specific flags (LONG_DEFECT_OUTSIDE_B31G) are added by the
    individual method functions before calling this. Pass
    `check_very_short=False` for Kastner, whose `result.length_mm` field
    stores the circumferential width — not the axial length the
    VERY_SHORT_DEFECT criterion is about.
    """
    if result.erf >= 1.0:
        result.qa_flags.append(make_flag(
            QAFlagCode.ERF_EXCEEDS_1,
            f"ERF {result.erf:.3f} ≥ 1.0 — operating pressure exceeds Psafe, "
            "immediate action required.",
            feature_id=result.feature_id,
            context={"erf": result.erf, "psafe_kgcm2": result.sop_kgcm2,
                     "maop_kgcm2": result.maop_kgcm2},
        ))
    if result.depth_pct_wt >= 80.0:
        result.qa_flags.append(make_flag(
            QAFlagCode.DEPTH_EXCEEDS_80,
            f"depth {result.depth_pct_wt:.1f} % WT ≥ 80 % — mandatory "
            "repair per the depth-only criterion.",
            feature_id=result.feature_id,
            context={"depth_pct_wt": result.depth_pct_wt,
                     "depth_mm": result.depth_mm, "wt_mm": result.wt_mm},
        ))
    if check_very_short and result.length_mm is not None \
            and result.wt_mm is not None \
            and result.length_mm < _VERY_SHORT_DEFECT_RATIO * result.wt_mm:
        result.qa_flags.append(make_flag(
            QAFlagCode.VERY_SHORT_DEFECT,
            f"axial length {result.length_mm:.2f} mm < wall thickness "
            f"{result.wt_mm:.2f} mm (pinhole regime — verify method is "
            "appropriate).",
            feature_id=result.feature_id,
            context={"length_mm": result.length_mm, "wt_mm": result.wt_mm},
        ))


# ---------------------------------------------------------------------------
# Method 1 — B31G Original (ASME B31G-2012 Section 4)
# ---------------------------------------------------------------------------

def b31g_original(
    *,
    d_mm: float,
    L_mm: float,
    t_mm: float,
    D_mm: float,
    smys_mpa: float,
    Fd: float,
    maop_kgcm2: float,
    feature_id: str = "",
    assessment_date: date | None = None,
) -> FFPResult:
    """ASME B31G-2012 Level 1 (z ≤ 20) and Level 2 (z > 20) branches.

    For z ≤ 20:
        M = √(1 + 0.8·z)
        A/A0 = (2/3)·(d/t)   (parabolic profile)
        Pf = (2·Sflow·t/D) · [(1 − Q) / (1 − Q/M)],   Q = (2/3)(d/t)
    For z > 20 (long-defect asymptote):
        Pf = (2·Sflow·t/D) · (1 − d/t)
    Psafe = Pf · Fd
    """
    z = (L_mm * L_mm) / (D_mm * t_mm)
    d_over_t = d_mm / t_mm
    sflow_mpa = _SFLOW_FACTOR_B31G_ORIG * smys_mpa
    p_intact_mpa = 2.0 * sflow_mpa * t_mm / D_mm    # at flow stress, no defect

    if z <= 20.0:
        M = math.sqrt(1.0 + 0.8 * z)
        Q = (2.0 / 3.0) * d_over_t                  # A/A0 (parabolic)
        denom = 1.0 - Q / M
        if denom <= 0:
            R = 0.0                                  # full collapse
        else:
            R = (1.0 - Q) / denom
        branch = "low_z"
        a_a0 = Q
    else:
        M = None
        R = 1.0 - d_over_t                          # rectangular profile
        branch = "high_z"
        a_a0 = d_over_t

    pf_mpa = max(0.0, p_intact_mpa * R)
    psafe_mpa = pf_mpa * Fd
    pf_kgcm2 = mpa_to_kgcm2(pf_mpa)
    sop_kgcm2 = mpa_to_kgcm2(psafe_mpa)
    erf = (maop_kgcm2 / sop_kgcm2) if sop_kgcm2 > 0 else float("inf")

    result = FFPResult(
        feature_id=feature_id,
        method=FFPMethod.B31G_ORIGINAL,
        depth_pct_wt=100.0 * d_over_t,
        depth_mm=d_mm,
        length_mm=L_mm,
        wt_mm=t_mm,
        pf_kgcm2=pf_kgcm2,
        sop_kgcm2=sop_kgcm2,
        maop_kgcm2=maop_kgcm2,
        erf=erf,
        folias_factor_M=M,
        z_value=z,
        flow_stress_mpa=sflow_mpa,
        area_metal_loss_ratio=a_a0,
        branch_used=branch,
        assessment_date=assessment_date or date.today(),
    )
    if z > _B31G_ORIG_LONG_DEFECT_Z:
        result.qa_flags.append(make_flag(
            QAFlagCode.LONG_DEFECT_OUTSIDE_B31G,
            f"z = {z:.1f} > {_B31G_ORIG_LONG_DEFECT_Z:.0f} — outside B31G "
            "Original's calibration; B31G Modified or RSTRENG is more "
            "appropriate.",
            feature_id=feature_id,
            context={"z": z, "method": "B31G_Original"},
        ))
    _attach_common_flags(result)
    return result


# ---------------------------------------------------------------------------
# Method 2 — B31G Modified (ASME B31G-2012 Section 5; 0.85·dL method)
# ---------------------------------------------------------------------------

def b31g_modified(
    *,
    d_mm: float,
    L_mm: float,
    t_mm: float,
    D_mm: float,
    smys_mpa: float,
    Fd: float,
    maop_kgcm2: float,
    feature_id: str = "",
    assessment_date: date | None = None,
) -> FFPResult:
    """ASME B31G-2012 Section 5 (Modified B31G), the 0.85·dL form.

        For z ≤ 50:  M = √(1 + 0.6275·z − 0.003375·z²)
        For z > 50:  M = 0.032·z + 3.3
        SF = Sflow · [(1 − 0.85·d/t) / (1 − 0.85·d/t / M)]
        PF = 2 · SF · t / D
        Psafe = PF · Fd

    Sflow = SMYS + 69 MPa (≈ SMYS + 10 ksi).
    """
    z = (L_mm * L_mm) / (D_mm * t_mm)
    d_over_t = d_mm / t_mm
    sflow_mpa = smys_mpa + _SFLOW_OFFSET_B31G_MOD_MPA

    if z <= 50.0:
        M = math.sqrt(max(0.0, 1.0 + 0.6275 * z - 0.003375 * z * z))
        branch = "low_z"
    else:
        M = 0.032 * z + 3.3
        branch = "high_z"

    Q = 0.85 * d_over_t                              # A/A0 (0.85·dL)
    denom = 1.0 - Q / M
    if denom <= 0:
        sf_mpa = 0.0
    else:
        sf_mpa = sflow_mpa * (1.0 - Q) / denom
    pf_mpa = max(0.0, 2.0 * sf_mpa * t_mm / D_mm)
    psafe_mpa = pf_mpa * Fd

    pf_kgcm2 = mpa_to_kgcm2(pf_mpa)
    sop_kgcm2 = mpa_to_kgcm2(psafe_mpa)
    erf = (maop_kgcm2 / sop_kgcm2) if sop_kgcm2 > 0 else float("inf")

    result = FFPResult(
        feature_id=feature_id,
        method=FFPMethod.B31G_MODIFIED,
        depth_pct_wt=100.0 * d_over_t,
        depth_mm=d_mm,
        length_mm=L_mm,
        wt_mm=t_mm,
        pf_kgcm2=pf_kgcm2,
        sop_kgcm2=sop_kgcm2,
        maop_kgcm2=maop_kgcm2,
        erf=erf,
        folias_factor_M=M,
        z_value=z,
        flow_stress_mpa=sflow_mpa,
        area_metal_loss_ratio=Q,
        branch_used=branch,
        assessment_date=assessment_date or date.today(),
    )
    _attach_common_flags(result)
    return result


# ---------------------------------------------------------------------------
# Method 3 — RSTRENG (effective-area, 0.85·dL fallback)
# ---------------------------------------------------------------------------

def rstreng(
    *,
    d_mm: float,
    L_mm: float,
    t_mm: float,
    D_mm: float,
    smys_mpa: float,
    Fd: float,
    maop_kgcm2: float,
    depth_profile_mm: list[float] | None = None,    # river-bottom profile, future
    feature_id: str = "",
    assessment_date: date | None = None,
) -> FFPResult:
    """PRCI RSTRENG (Kiefner & Vieth, 1989).

    With a measured river-bottom depth profile, RSTRENG computes A
    explicitly along the defect's length and finds the worst-case
    sub-profile that maximises predicted failure pressure. Most ILI data
    (POF 110 etc.) doesn't carry profiles, so this implementation falls
    back to the 0.85·d·L effective-area approximation — *which is what
    B31G Modified Section 5 uses*. The result is mathematically identical
    to `b31g_modified(...)`, but we expose it as a distinct method so
    cross-check tables can report both, and we set
    `using_approximate_profile=True` so the report makes the limitation
    explicit.
    """
    using_approx = depth_profile_mm is None
    if not using_approx:
        # Future: river-bottom integration. Not in scope until ILI vendors
        # ship per-feature profile data.
        raise NotImplementedError(
            "RSTRENG with depth_profile_mm not yet implemented — feed a None "
            "profile to use the 0.85·dL approximation."
        )

    result = b31g_modified(
        d_mm=d_mm, L_mm=L_mm, t_mm=t_mm, D_mm=D_mm,
        smys_mpa=smys_mpa, Fd=Fd, maop_kgcm2=maop_kgcm2,
        feature_id=feature_id, assessment_date=assessment_date,
    )
    result.method = FFPMethod.RSTRENG
    result.using_approximate_profile = True
    result.notes.append(
        "RSTRENG: no river-bottom profile available; using 0.85·dL "
        "approximation (mathematically identical to B31G Modified)."
    )
    return result


# ---------------------------------------------------------------------------
# Method 4 — DNV-RP-F101 Part B (ASD format)
# ---------------------------------------------------------------------------

def dnv_rp_f101(
    *,
    d_mm: float,
    L_mm: float,
    t_mm: float,
    D_mm: float,
    smys_mpa: float,
    Fd: float,
    maop_kgcm2: float,
    uts_mpa: float | None = None,
    uts_offset_mpa: float = _UTS_OFFSET_MPA_DEFAULT,
    feature_id: str = "",
    assessment_date: date | None = None,
) -> FFPResult:
    """DNV-RP-F101 (2017) Part B, ASD format.

        Q     = √(1 + 0.31 · (L² / (D·t)))        (length correction)
        Psafe = (2·UTS·t / (D − t)) · [(1 − d/t) / (1 − (d/t)/Q)] · Fd

    UTS, not flow stress. If the caller doesn't supply UTS, the standard
    line-pipe correlation UTS ≈ SMYS + 110 MPa is used and a note is added.
    """
    notes: list[str] = []
    if uts_mpa is None:
        uts_mpa = smys_mpa + uts_offset_mpa
        notes.append(
            f"UTS estimated as SMYS + {uts_offset_mpa:.0f} MPa = {uts_mpa:.1f} MPa "
            f"(standard line-pipe correlation; provide uts_mpa for tighter result)."
        )

    z = (L_mm * L_mm) / (D_mm * t_mm)
    Q = math.sqrt(1.0 + 0.31 * z)
    d_over_t = d_mm / t_mm

    denom = 1.0 - (d_over_t / Q)
    if denom <= 0:
        psafe_mpa = 0.0
        pf_mpa = 0.0
    else:
        # In DNV's ASD form, the design factor (Fd here) IS the usage factor
        # — applied directly to the failure pressure to give Psafe.
        pf_mpa = (2.0 * uts_mpa * t_mm / (D_mm - t_mm)) * ((1.0 - d_over_t) / denom)
        psafe_mpa = pf_mpa * Fd

    pf_kgcm2 = mpa_to_kgcm2(pf_mpa)
    sop_kgcm2 = mpa_to_kgcm2(psafe_mpa)
    erf = (maop_kgcm2 / sop_kgcm2) if sop_kgcm2 > 0 else float("inf")

    result = FFPResult(
        feature_id=feature_id,
        method=FFPMethod.DNV_RP_F101,
        depth_pct_wt=100.0 * d_over_t,
        depth_mm=d_mm,
        length_mm=L_mm,
        wt_mm=t_mm,
        pf_kgcm2=pf_kgcm2,
        sop_kgcm2=sop_kgcm2,
        maop_kgcm2=maop_kgcm2,
        erf=erf,
        folias_factor_M=Q,                              # DNV's Q in the M slot
        z_value=z,
        flow_stress_mpa=uts_mpa,                        # UTS not flow stress
        area_metal_loss_ratio=d_over_t,                 # full-depth treatment
        branch_used="",
        notes=notes,
        assessment_date=assessment_date or date.today(),
    )
    _attach_common_flags(result)
    return result


# ---------------------------------------------------------------------------
# Method 5 — Kastner (net-section approximation for circumferential defects)
# ---------------------------------------------------------------------------

def kastner(
    *,
    d_mm: float,
    W_mm: float,
    t_mm: float,
    D_mm: float,
    smys_mpa: float,
    Fd: float,
    maop_kgcm2: float,
    feature_id: str = "",
    assessment_date: date | None = None,
) -> FFPResult:
    """Net-section approximation for partial-depth circumferential defects.

    For circumferential defects, axial stress (P·D / 4t) governs, not hoop
    stress. The reduced-section criterion is:

        σ_axial,fail = σ_flow · [1 − (W / (π·D)) · (d/t)]
        Pf_axial     = 4 · σ_axial,fail · t / D
        Psafe        = Pf_axial · Fd

    For W → 0 or d → 0 this returns σ_flow (intact pipe), and for the
    pathological case W = π·D, d = t (defect wraps entire circumference,
    full depth) it returns σ_flow · 0 = 0. Sflow = 1.1·SMYS (same as B31G
    Original).

    This is documented as a deliberate simplification of Kastner 1986 in
    `project_v02_followups.md`; the full equilibrium form is deferred
    until real CISL/CIGR features with full-width extent + published
    Psafe come through.
    """
    sflow_mpa = _SFLOW_FACTOR_B31G_ORIG * smys_mpa
    d_over_t = d_mm / t_mm
    circumference_mm = math.pi * D_mm
    area_reduction = (W_mm / circumference_mm) * d_over_t
    area_reduction = max(0.0, min(1.0, area_reduction))    # clamp

    pf_mpa = max(0.0, (4.0 * sflow_mpa * t_mm / D_mm) * (1.0 - area_reduction))
    psafe_mpa = pf_mpa * Fd
    pf_kgcm2 = mpa_to_kgcm2(pf_mpa)
    sop_kgcm2 = mpa_to_kgcm2(psafe_mpa)
    erf = (maop_kgcm2 / sop_kgcm2) if sop_kgcm2 > 0 else float("inf")

    result = FFPResult(
        feature_id=feature_id,
        method=FFPMethod.KASTNER,
        depth_pct_wt=100.0 * d_over_t,
        depth_mm=d_mm,
        length_mm=W_mm,                                 # circumferential extent
        wt_mm=t_mm,
        pf_kgcm2=pf_kgcm2,
        sop_kgcm2=sop_kgcm2,
        maop_kgcm2=maop_kgcm2,
        erf=erf,
        folias_factor_M=None,
        z_value=None,
        flow_stress_mpa=sflow_mpa,
        area_metal_loss_ratio=area_reduction,
        branch_used="",
        notes=["Kastner: net-section approximation (see docs/FFP_VALIDATION.md)."],
        assessment_date=assessment_date or date.today(),
    )
    # Kastner stores W as length_mm; skip the VERY_SHORT_DEFECT check
    # (it's about axial length, not circumferential extent).
    _attach_common_flags(result, check_very_short=False)
    return result


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

_METHOD_DISPATCH = {
    FFPMethod.B31G_ORIGINAL: b31g_original,
    FFPMethod.B31G_MODIFIED: b31g_modified,
    FFPMethod.RSTRENG: rstreng,
    FFPMethod.DNV_RP_F101: dnv_rp_f101,
    FFPMethod.KASTNER: kastner,
}


def ffp_assess(
    feature: Feature,
    pipeline: Pipeline,
    config: dict | None = None,
    *,
    assessment_date: date | None = None,
) -> list[FFPResult]:
    """Run primary + cross-check methods (+ Kastner for circ defects) on a feature.

    Returns a list of FFPResult ordered:
      [0]              primary method
      [1..k]           cross-check methods (in config order, skipping duplicates)
      [k+1, if circ]   Kastner

    For circumferential defects (dimension_class CISL or CIGR) the
    coordinator additionally runs Kastner and marks whichever of (primary,
    Kastner) has the LOWER Psafe as `is_controlling=True`.

    Raises ValueError if the feature has no WT or if no MAOP zone matches
    the feature's WT — both are caller-fixable: provide a WT (or use the
    full joint list) and ensure project YAML covers the WT range.
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)

    # Defense-in-depth: reject non-metal-loss features (dents, welds,
    # cracks) before computing anything. The reader is supposed to
    # filter these out via _NON_METAL_LOSS_FIDS, but a stray dent that
    # slips past — e.g. a vendor variant not yet in
    # column_synonyms.yaml — would otherwise have its 0.9 %OD value
    # treated as 90 %WT and produce a wildly wrong ERF. Raise loudly
    # instead.
    if feature.feature_identification in _NON_ASSESSABLE_FIDS:
        raise ValueError(
            f"feature {feature.anomaly_id!r}: FFP methods (B31G / "
            f"RSTRENG / DNV / Kastner) don't apply to "
            f"feature_identification={feature.feature_identification.value!r}. "
            "Dents, welds, and cracks require separate assessment "
            "standards. If this row reached ffp_assess(), the reader's "
            "non-metal-loss filter missed it — check the "
            "value_normalisations.feature_identification map in "
            "config/column_synonyms.yaml."
        )

    if feature.wt_mm is None or feature.wt_mm <= 0:
        raise ValueError(
            f"feature {feature.anomaly_id!r}: wt_mm is required for FFP "
            f"(got {feature.wt_mm!r})"
        )
    if feature.depth_pct_wt is None:
        raise ValueError(
            f"feature {feature.anomaly_id!r}: depth_pct_wt is required for FFP"
        )

    # v0.3.0: mode-aware MAOP-zone lookup. WT-mode pipelines (the
    # default) route through `maop_for_wt`; chainage-mode pipelines
    # (`maop_zoning_mode: chainage` in the YAML) route through
    # `maop_for_chainage`. `maop_for_feature` is the dispatcher.
    zone, _zone_idx, zone_fallback_used = pipeline.maop_for_feature(feature)
    if zone is None:
        if pipeline.maop_zoning_mode == "chainage":
            raise ValueError(
                f"feature {feature.anomaly_id!r}: no MAOP zone matches "
                f"chainage {feature.abs_distance_m} m. Configure "
                f"pipeline.maop_zones to cover this chainage range."
            )
        raise ValueError(
            f"feature {feature.anomaly_id!r}: no MAOP zone matches WT "
            f"{feature.wt_mm} mm. Configure pipeline.maop_zones to cover this WT."
        )

    primary_method = _coerce_method(cfg["primary_method"])
    cross_checks = [_coerce_method(m) for m in cfg.get("cross_check_methods", [])]

    base_kwargs = dict(
        d_mm=feature.depth_mm,
        t_mm=feature.wt_mm,
        D_mm=pipeline.diameter_mm,
        smys_mpa=pipeline.smys_mpa,
        Fd=zone.design_factor,
        maop_kgcm2=zone.maop_kgcm2,
        feature_id=feature.anomaly_id,
        assessment_date=assessment_date,
    )

    results: list[FFPResult] = []

    # Primary (and cross-checks)
    methods_to_run = [primary_method]
    for m in cross_checks:
        if m not in methods_to_run:
            methods_to_run.append(m)

    for m in methods_to_run:
        results.append(_dispatch(m, feature, base_kwargs))

    # Auto-Kastner for circumferential defects (recognized via the
    # multi-signal `is_kastner_eligible` helper — POF dimension_class
    # enum, free-text label substring, or geometric proxy W > L).
    is_circ = is_kastner_eligible(feature)
    if is_circ and bool(cfg.get("kastner_for_circumferential", True)) \
            and FFPMethod.KASTNER not in methods_to_run:
        results.append(_dispatch(FFPMethod.KASTNER, feature, base_kwargs))

    # If the MAOP zone was a nearest-zone fallback (feature's WT or
    # chainage outside all explicit ranges), tag every result so the
    # QA report shows it. v0.3.0: flag text + context now adapt to
    # the active zoning mode.
    if zone_fallback_used:
        for r in results:
            if pipeline.maop_zoning_mode == "chainage":
                msg = (
                    f"feature chainage {feature.abs_distance_m} m is "
                    f"outside the explicit chainage-zone ranges; used "
                    f"nearest zone (chainage {zone.chainage_m_min}-"
                    f"{zone.chainage_m_max} m, MAOP {zone.maop_kgcm2} kg/cm²)."
                )
                ctx = {
                    "feature_chainage_m": feature.abs_distance_m,
                    "zone_chainage_min": zone.chainage_m_min,
                    "zone_chainage_max": zone.chainage_m_max,
                    "zone_maop_kgcm2": zone.maop_kgcm2,
                    "zoning_mode": "chainage",
                }
            else:
                msg = (
                    f"feature WT {feature.wt_mm} mm is outside the "
                    f"explicit MAOP-zone ranges; used nearest zone "
                    f"(WT {zone.wt_mm_min}-{zone.wt_mm_max} mm, "
                    f"MAOP {zone.maop_kgcm2} kg/cm²)."
                )
                ctx = {
                    "feature_wt_mm": feature.wt_mm,
                    "zone_wt_min": zone.wt_mm_min,
                    "zone_wt_max": zone.wt_mm_max,
                    "zone_maop_kgcm2": zone.maop_kgcm2,
                    "zoning_mode": "wt",
                }
            r.qa_flags.append(make_flag(
                QAFlagCode.MAOP_ZONE_NOT_FOUND, msg,
                feature_id=feature.anomaly_id, context=ctx,
            ))

    # Mark controlling: for circ defects, lower Psafe of (primary, kastner) wins.
    if is_circ:
        primary_result = results[0]
        kastner_result = next(
            (r for r in results if r.method is FFPMethod.KASTNER), None
        )
        if kastner_result is not None:
            if kastner_result.sop_kgcm2 < primary_result.sop_kgcm2:
                primary_result.is_controlling = False
                kastner_result.is_controlling = True
            else:
                primary_result.is_controlling = True
                kastner_result.is_controlling = False

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_method(value: Any) -> FFPMethod:
    if isinstance(value, FFPMethod):
        return value
    if isinstance(value, str):
        try:
            return FFPMethod(value)
        except ValueError as e:
            valid = ", ".join(m.value for m in FFPMethod)
            raise ValueError(
                f"unknown FFP method {value!r}; expected one of: {valid}"
            ) from e
    raise TypeError(f"method must be FFPMethod or str; got {type(value).__name__}")


def _dispatch(method: FFPMethod, feature: Feature, base_kwargs: dict[str, Any]) -> FFPResult:
    """Call the right core function with method-specific argument shape."""
    fn = _METHOD_DISPATCH[method]
    if method is FFPMethod.KASTNER:
        # Kastner takes W_mm (circumferential width), not L_mm.
        if feature.width_mm is None:
            raise ValueError(
                f"feature {feature.anomaly_id!r}: width_mm required for Kastner"
            )
        return fn(W_mm=feature.width_mm, **base_kwargs)
    if feature.length_mm is None:
        raise ValueError(
            f"feature {feature.anomaly_id!r}: length_mm required for {method.value}"
        )
    return fn(L_mm=feature.length_mm, **base_kwargs)
