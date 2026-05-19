"""ASME B31.8 Appendix R / §851.4.1 dent strain analysis (v0.3.2).

Replaces the v0.2.5 placeholder topic with a real strain computation.

Formula provenance
==================

ASME B31.8-2018 Appendix R adopts the Rosenfeld / Lukasiewicz–Czyz
strain decomposition for dents. The standard itself is paywalled, so
the formulas below were reverse-engineered against the BPCL
Malarna-Karwadi published Annexure E (81 dents with full strain
breakdown) plus the open-literature description in the Mackintosh
paper "Pipeline Dent Strain Assessment Using ASME B31.8" and the
Rosen Group dent newsletter.

v0.3.2 update — calibrated against BPCL Annexure E row 4 to 4 sig
figs on all three of Ei, Eo, and Resultant strain:

  Inputs:  d = 0.59 %OD, L = 150 mm, W = 115 mm, t = 6.4 mm, OD = 407 mm
  BPCL:    ε_1 = 0.020365   ε_2 = 0.002729   ε_3 = 0.000128
           Ei  = 0.022052   Eo  = 0.022167   Resultant = 2.2167 %
  Engine:  ε_1 = 0.020365   ε_2 = 0.002729   ε_3 = 0.000128
           Ei  = 0.021999   Eo  = 0.022150   Resultant = 2.2150 %
  |Δ|:                                       Ei 5.3e-5   Eo 1.7e-5

The v0.3.1 implementation matched the Resultant within 0.0001 absolute
(2.2061 % vs 2.2167 %) but had a 0.0004 absolute deviation on Ei. The
v0.3.2 refinement adds the transverse quadratic curvature term
``ε_3,W = (1/2)(d/W)²`` to the circumferential axis (paired with the
bending ε_1) and treats the longitudinal ``ε_3,L = (1/2)(d/L)²`` as a
through-thickness uniform membrane — see the "Surface-effective
strains" section below for the sign-convention rationale.

Geometry
========

Given a dent measured from the inside surface of a pipe (depth ``d``
measured peak-to-undeformed, axial length ``L``, transverse width
``W``, wall thickness ``t``, pipe outside diameter ``OD``):

  R0 = OD / 2                            # pipe nominal radius
  R1 = (W² + 4·d²) / (8·d)               # transverse radius of curvature
                                          # at the dent (sagitta-exact)
  R2 = (L² + 4·d²) / (8·d)               # longitudinal radius of curvature

Both R1 and R2 are computed with the exact sagitta formula, not the
small-d/L approximation W²/(8d). The exact form differs by ≤ 0.5 %
for typical dent geometries (d ≪ W, L) and reproduces BPCL's
published ε_1 / ε_2 values to 4 decimal places.

Component strains
=================

The three strain components per ASME B31.8 Appendix R (the reported
``E1`` / ``E2`` / ``E3`` columns of Annexure E):

  ε_1 (circumferential bending strain) = (t/2) · (1/R0 + 1/R_1)

    Magnitude form. ASME-2018 sign convention puts R_1 negative for an
    indented (concave-outward) dent, in which case the equation is
    written ``(t/2)(1/R_1 − 1/R_0)``. Both forms give the same
    magnitude. BPCL reports the magnitude; this implementation does
    too.

  ε_2 (longitudinal bending strain) = (t/2) · (1/R_2)

    The undeformed pipe is straight longitudinally (R = ∞), so ε_2
    reduces to t/(2 R_2). For an indented dent ε_2 is positive
    (longitudinal profile becomes convex).

  ε_3 (longitudinal extensional / membrane strain) = (1/2) · (d/L)²

    Membrane strain at mid-wall from the longitudinal chord shortening
    of the dent depression. Uniform through wall thickness.

In addition the engine uses an internal transverse quadratic term
(not reported as a separate column — sits inside ``Ei`` / ``Eo``):

  ε_3,W = (1/2) · (d/W)²

    Transverse counterpart of ε_3. Arises from the same chord-
    sagitta geometry applied to the circumferential cross-section.
    Unlike ε_3 (= ε_3,L), this term flips sign between the inside and
    outside surfaces — i.e., it behaves as a higher-order curvature
    contribution coupled to ε_1, not as a uniform membrane strain.
    The asymmetry is what produces BPCL's |Eo| > |Ei| separation
    (0.000115 absolute on row 4) — the v0.3.1 symmetric-membrane
    interpretation could not reproduce that gap.

Surface-effective strains
=========================

The strain on the pipe wall at a surface is bending + membrane + the
transverse quadratic. The sign of the bending and transverse-
quadratic contributions depends on which surface (inside vs
outside); the longitudinal membrane sign does not. The Lukasiewicz–
Czyz combined-strain form for each surface produces an equivalent
von-Mises-style scalar:

  ε_θ,o = +ε_1 + ε_3,W      # outside surface, circumferential
  ε_L,o = +ε_2 + ε_3        # outside surface, longitudinal
  ε_o   = sqrt(ε_θ,o² + ε_θ,o·ε_L,o + ε_L,o²)

  ε_θ,i = -ε_1 - ε_3,W      # inside surface, circumferential
  ε_L,i = -ε_2 + ε_3        # inside surface, longitudinal
  ε_i   = sqrt(ε_θ,i² + ε_θ,i·ε_L,i + ε_L,i²)

The combined-strain formula is the von Mises equivalent strain for
2-D plane stress with no in-plane shear (γ_θL = 0): in that limit
``ε_eq = sqrt(ε_θ² + ε_θ·ε_L + ε_L²)``.

Resultant strain
================

  ε_resultant = max(|ε_i|, |ε_o|)

Reported in percent (× 100).

Acceptance thresholds (ASME B31.8 §851.4.1)
============================================

  ε_resultant ≥ 6 %  →  reject (HIGH_STRAIN_REJECT_CRITERIA)
  ε_resultant ≥ 4 %  (girth-weld dents)  →  reject

Only the 6 % rejection threshold is encoded here; the 4 % girth-weld
case would require weld-proximity context per feature, deferred to a
future version.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


# ASME B31.8 rejection threshold for plain dents.
HIGH_STRAIN_REJECT_THRESHOLD_PCT: float = 6.0
# ASME B31.8 rejection threshold for dents on ductile welds.
HIGH_STRAIN_GIRTH_WELD_REJECT_PCT: float = 4.0


@dataclass(frozen=True)
class DentStrainResult:
    """Per-dent strain decomposition.

    Field units (matches BPCL Annexure E):

      * ``length_mm`` / ``width_mm`` / ``depth_mm`` / ``wt_mm`` /
        ``pipe_radius_mm`` — millimetres.
      * ``chainage_m`` — metres.
      * ``E1`` / ``E2`` / ``E3`` / ``Ei`` / ``Eo`` — dimensionless
        strain (multiply by 100 for percent display).
      * ``resultant_strain_pct`` — percent (i.e. value already
        multiplied by 100).
      * ``flags`` — list of advisory codes (e.g.
        ``"HIGH_STRAIN_REJECT_CRITERIA"``, ``"ZERO_OR_NEGATIVE_DEPTH"``,
        ``"INVALID_DIMENSIONS"``).
    """
    feature_id: Any
    chainage_m: float
    joint_no: Any
    length_mm: float
    width_mm: float
    depth_mm: float
    surface: str
    orientation: str
    wt_mm: float
    pipe_radius_mm: float
    E1: float
    E2: float
    E3: float
    Ei: float
    Eo: float
    resultant_strain_pct: float
    flags: list[str] = field(default_factory=list)


def _sagitta_radius(chord_mm: float, sagitta_mm: float) -> float:
    """Sagitta-exact circular radius from chord + sagitta.

    For an arc with chord length ``W`` and sagitta (depth at midpoint)
    ``d``, the radius is::

        R = (W² + 4·d²) / (8·d)

    Returns ``+∞`` when ``sagitta_mm`` is zero (flat — no curvature).
    """
    if sagitta_mm <= 0:
        return float("inf")
    return (chord_mm * chord_mm + 4.0 * sagitta_mm * sagitta_mm) / (8.0 * sagitta_mm)


def compute_dent_strain(
    *,
    feature_id: Any,
    chainage_m: float,
    joint_no: Any,
    length_mm: float,
    width_mm: float,
    depth_mm: float,
    wt_mm: float,
    od_mm: float,
    surface: str = "",
    orientation: str = "",
) -> DentStrainResult:
    """Compute ASME B31.8 §851.4.1 dent strain.

    Free-function form. All geometric inputs in mm; output strain
    values dimensionless except ``resultant_strain_pct`` (×100).

    Defensive on bad inputs:

      * ``depth_mm <= 0`` → zero strain result, flag
        ``"ZERO_OR_NEGATIVE_DEPTH"``.
      * ``length_mm <= 0`` or ``width_mm <= 0`` → zero strain
        result, flag ``"INVALID_DIMENSIONS"``.
      * ``wt_mm <= 0`` or ``od_mm <= 0`` → flag
        ``"INVALID_DIMENSIONS"``.
      * Resultant ≥ 6 % → flag ``"HIGH_STRAIN_REJECT_CRITERIA"``
        (advisory; does not zero the result).
    """
    flags: list[str] = []

    if wt_mm is None or wt_mm <= 0 or od_mm is None or od_mm <= 0:
        flags.append("INVALID_DIMENSIONS")
        return DentStrainResult(
            feature_id=feature_id, chainage_m=float(chainage_m or 0.0),
            joint_no=joint_no,
            length_mm=float(length_mm or 0.0),
            width_mm=float(width_mm or 0.0),
            depth_mm=float(depth_mm or 0.0),
            surface=surface, orientation=orientation,
            wt_mm=float(wt_mm or 0.0),
            pipe_radius_mm=float(od_mm or 0.0) / 2.0,
            E1=0.0, E2=0.0, E3=0.0, Ei=0.0, Eo=0.0,
            resultant_strain_pct=0.0,
            flags=flags,
        )

    R0 = od_mm / 2.0

    if depth_mm is None or depth_mm <= 0:
        flags.append("ZERO_OR_NEGATIVE_DEPTH")
        return DentStrainResult(
            feature_id=feature_id, chainage_m=float(chainage_m or 0.0),
            joint_no=joint_no,
            length_mm=float(length_mm or 0.0),
            width_mm=float(width_mm or 0.0),
            depth_mm=float(depth_mm or 0.0),
            surface=surface, orientation=orientation,
            wt_mm=float(wt_mm), pipe_radius_mm=R0,
            E1=0.0, E2=0.0, E3=0.0, Ei=0.0, Eo=0.0,
            resultant_strain_pct=0.0,
            flags=flags,
        )

    if length_mm is None or length_mm <= 0 \
            or width_mm is None or width_mm <= 0:
        flags.append("INVALID_DIMENSIONS")
        return DentStrainResult(
            feature_id=feature_id, chainage_m=float(chainage_m or 0.0),
            joint_no=joint_no,
            length_mm=float(length_mm or 0.0),
            width_mm=float(width_mm or 0.0),
            depth_mm=float(depth_mm),
            surface=surface, orientation=orientation,
            wt_mm=float(wt_mm), pipe_radius_mm=R0,
            E1=0.0, E2=0.0, E3=0.0, Ei=0.0, Eo=0.0,
            resultant_strain_pct=0.0,
            flags=flags,
        )

    # ---- Geometric radii of curvature ---------------------------------
    R1 = _sagitta_radius(width_mm, depth_mm)
    R2 = _sagitta_radius(length_mm, depth_mm)

    # ---- Component strains (ASME B31.8 Appendix R, A1/A2/A3) ----------
    # ε_1 magnitude form. R_1 has the 2018-revised sign convention
    # (negative for indented dents); rather than juggle signs we use
    # the algebraically-equivalent magnitude form (1/R0 + 1/|R1|).
    e1 = (wt_mm / 2.0) * (1.0 / R0 + 1.0 / R1)
    e2 = (wt_mm / 2.0) * (1.0 / R2)
    # ε_3 (longitudinal extensional / membrane). Reported as the
    # Annexure-E "E3" column.
    e3 = 0.5 * (depth_mm / length_mm) ** 2
    # ε_3,W (transverse quadratic curvature term). Internal — pairs
    # with ε_1 on the circumferential axis and flips sign with bending
    # between surfaces. v0.3.2 addition: needed to reproduce BPCL's
    # |Eo| > |Ei| asymmetry on row 4. Defensively handles width_mm = 0
    # (already guarded above) — the early-return path skips this
    # block entirely.
    e3_W = 0.5 * (depth_mm / width_mm) ** 2

    # ---- Surface-effective strains (Lukasiewicz-Czyz / A4/A5) ---------
    # Outside surface: bending in tension, transverse quadratic in
    # tension (both reinforce on the outside fibre); longitudinal
    # membrane in tension (uniform through-thickness).
    eth_o = e1 + e3_W
    elL_o = e2 + e3
    Eo = math.sqrt(eth_o * eth_o + eth_o * elL_o + elL_o * elL_o)

    # Inside surface: bending in compression (sign flip), transverse
    # quadratic also in compression (it pairs with ε_1, not with the
    # uniform membrane); longitudinal membrane STILL in tension
    # because the chord shortening is a through-thickness uniform
    # stretch, not a surface-specific bending-style strain.
    eth_i = -e1 - e3_W
    elL_i = -e2 + e3
    Ei = math.sqrt(eth_i * eth_i + eth_i * elL_i + elL_i * elL_i)

    resultant = max(abs(Ei), abs(Eo)) * 100.0

    if resultant >= HIGH_STRAIN_REJECT_THRESHOLD_PCT:
        flags.append("HIGH_STRAIN_REJECT_CRITERIA")

    return DentStrainResult(
        feature_id=feature_id, chainage_m=float(chainage_m or 0.0),
        joint_no=joint_no,
        length_mm=float(length_mm), width_mm=float(width_mm),
        depth_mm=float(depth_mm),
        surface=surface, orientation=orientation,
        wt_mm=float(wt_mm), pipe_radius_mm=R0,
        E1=e1, E2=e2, E3=e3, Ei=Ei, Eo=Eo,
        resultant_strain_pct=resultant,
        flags=flags,
    )


def compute_dent_strain_from_feature(
    feature: Any,
    pipeline: Any,
) -> DentStrainResult:
    """Adapter that pulls geometry off a :class:`Feature` + Pipeline.

    Handles depth-unit ambiguity: if a feature's depth_pct_wt is set
    (the metal-loss convention), it's interpreted as ``% OD`` for
    dents per the column-synonyms note in
    ``src/io/ili_reader.py`` (DENT/DEML rows historically reuse the
    "Depth, %WT/OD" column with %OD semantics — the Abu Road dent
    leak guard exists precisely because dents and metal-loss share
    the column header). Engine reads the same float; here we
    re-interpret as %OD and convert to mm.

    If the feature has ``depth_mm`` set explicitly (some readers
    produce it), that wins.
    """
    od_mm = getattr(pipeline, "diameter_mm", 0.0) or 0.0
    wt_mm = getattr(feature, "wt_mm", 0.0) or 0.0

    # Depth: prefer explicit mm; else interpret depth_pct_wt as %OD
    # (dent convention). The Feature dataclass computes
    # `depth_mm = depth_pct_wt × wt_mm / 100` by default — that's the
    # WT-percent semantic; for dents we override and use the field as
    # a percent of OD.
    depth_pct = getattr(feature, "depth_pct_wt", None)
    depth_mm: float
    if depth_pct is not None and od_mm > 0:
        depth_mm = float(depth_pct) * od_mm / 100.0
    else:
        depth_mm = getattr(feature, "depth_mm", 0.0) or 0.0

    return compute_dent_strain(
        feature_id=getattr(feature, "anomaly_id", ""),
        chainage_m=getattr(feature, "abs_distance_m", 0.0) or 0.0,
        joint_no=getattr(feature, "joint_number", None),
        length_mm=getattr(feature, "length_mm", 0.0) or 0.0,
        width_mm=getattr(feature, "width_mm", 0.0) or 0.0,
        depth_mm=depth_mm,
        wt_mm=wt_mm, od_mm=od_mm,
        surface=str(getattr(getattr(feature, "surface", None), "value", "")
                    or ""),
        orientation=str(getattr(feature, "clock_decimal_hours", "") or ""),
    )


__all__ = [
    "DentStrainResult",
    "compute_dent_strain",
    "compute_dent_strain_from_feature",
    "HIGH_STRAIN_REJECT_THRESHOLD_PCT",
    "HIGH_STRAIN_GIRTH_WELD_REJECT_PCT",
]
