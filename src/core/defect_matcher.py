"""
Defect-to-defect matching within aligned joint pairs.

Inputs are two ILIRuns and the joint correspondence already produced by
`src.core.joint_alignment.JointAligner`. For each matched joint pair the
matcher finds the optimal one-to-one assignment between run-1 and run-2
defects using the Hungarian algorithm (scipy.optimize.linear_sum_assignment),
with iterative tolerance relaxation across three passes — easy matches lock
in first, then progressively looser tolerances pick up the harder ones.

Cost between two features (in the same joint pair) is a weighted sum of:

    axial_weight * |upstream_weld_dist_run1 - upstream_weld_dist_run2|   [metres]
  + clock_weight * clock_wrap_distance(c1, c2)                            [hours]
  + surface_mismatch_penalty   if both surfaces are known and differ      [10.0]
  + depth_shrinkage_penalty    if depth_run2 < depth_run1 * 0.5           [1.0]
  + cluster_type_mismatch_penalty if one is a cluster parent and the other [0.2]
                                  is a standalone — keeps clusters
                                  preferentially paired to clusters
                                  without forbidding cross-type matches

Per-pass tolerances act as hard filters: a candidate pair whose axial or
clock distance exceeds the pass tolerance has its cost set to +infinity
(forbidden in that pass). After Hungarian, any accepted pair whose cost
exceeds the pass's `max_cost` ceiling is rejected and rolled forward to
the next pass.

Cluster awareness:
- For files with explicit cluster_parent_id / is_cluster_parent (typical NGP
  multi-sheet), `ILIRun.features_for_assessment()` already excludes children;
  the matcher operates on parents + standalones.
- If neither run carries any cluster parents at all, the matcher emits a
  warning recommending the caller either accept feature-level matching or
  pre-cluster the runs using B31G 3t proximity rules — it never silently
  auto-clusters.

Output is a `MatchResult` carrying every accepted FeatureMatch, separate
lists of unmatched features for run1 and run2, the per-pass match counts
(diagnostic), and any warnings raised during matching.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment

from src.models import (
    Feature,
    FeatureMatch,
    ILIRun,
    JointMatch,
    MatchResult,
    Surface,
)
from src.validation import QAFlagCode, make_flag


# Effective +infinity for forbidden Hungarian cells. Must be large enough
# that no legitimate cost can rival it but finite (scipy can't handle inf).
_FORBIDDEN_COST = 1.0e6


DEFAULT_CONFIG: dict[str, Any] = {
    # Distance-metric weights and penalties
    "axial_weight": 1.0,
    "clock_weight": 0.3,
    "surface_mismatch_penalty": 10.0,
    "depth_shrinkage_penalty": 1.0,
    "depth_shrinkage_ratio": 0.5,
    "cluster_type_mismatch_penalty": 0.2,

    # Iterative-relaxation pass list (executed in order). Pass-3 axial
    # tolerance (1.0 m) is wider than the user spec's initial 0.5 m — this
    # is the relaxation that captures Kandla's row77↔#1038 last-defect-in-
    # joint pair (uw diff 0.607 m, still well within a 12 m joint). Tighter
    # values lose ~1 of the 23 published Kandla matches.
    "passes": [
        {"axial_tolerance_m": 0.10, "clock_tolerance_h": 0.5, "max_cost": 0.5},
        {"axial_tolerance_m": 0.25, "clock_tolerance_h": 1.0, "max_cost": 1.0},
        {"axial_tolerance_m": 1.00, "clock_tolerance_h": 1.5, "max_cost": 1.5},
    ],
}


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

class DefectMatcher:
    """Within-joint feature matcher.

    Usage:
        matcher = DefectMatcher()
        joint_alignment = JointAligner().align(run1, run2)
        result = matcher.match(run1, run2, joint_alignment.matches)
        for fm in result.feature_matches: ...
    """

    def __init__(self, config: dict | None = None):
        self.cfg: dict[str, Any] = _merge_cfg(DEFAULT_CONFIG, config)

    # ------------------------------------------------------------------

    def match(
        self,
        run1: ILIRun,
        run2: ILIRun,
        joint_matches: list[JointMatch],
        config: dict | None = None,
    ) -> MatchResult:
        cfg = _merge_cfg(self.cfg, config)

        result = MatchResult()
        passes_matched: dict[int, int] = {}

        # Use features_for_assessment so cluster children don't get matched
        # individually — their parent COCL row carries them.
        f1_pool = run1.features_for_assessment()
        f2_pool = run2.features_for_assessment()

        f1_by_joint = _index_by_joint(f1_pool)
        f2_by_joint = _index_by_joint(f2_pool)

        # Walk every aligned joint pair, accumulate matches. Track matched
        # feature ids by Python object identity; "unmatched" is then
        # whatever's in the pool minus what got matched (single pass over
        # the pool, O(n) — not O(n²) like trying to maintain unmatched
        # lists as you go).
        for jm in joint_matches:
            feats1 = f1_by_joint.get(jm.joint_old.joint_number, [])
            feats2 = f2_by_joint.get(jm.joint_new.joint_number, [])
            if not feats1 or not feats2:
                # No pair-up possible in this joint; both sides' features
                # (if any) stay unmatched — handled by the pool-difference
                # pass below.
                continue
            pair_matches, _unm1, _unm2 = self._match_joint_pair(
                feats1, feats2, cfg, passes_matched
            )
            result.feature_matches.extend(pair_matches)

        matched_f1_ids = {id(m.feature_old) for m in result.feature_matches}
        matched_f2_ids = {id(m.feature_new) for m in result.feature_matches}
        result.unmatched_features_old = [f for f in f1_pool if id(f) not in matched_f1_ids]
        result.unmatched_features_new = [f for f in f2_pool if id(f) not in matched_f2_ids]

        result.matches_per_pass = passes_matched
        result.match_rate = _compute_match_rate(result, f1_pool, f2_pool)
        result.final_tolerances = {
            "axial_m": cfg["passes"][-1]["axial_tolerance_m"],
            "clock_h": cfg["passes"][-1]["clock_tolerance_h"],
            "max_cost": cfg["passes"][-1]["max_cost"],
        }

        # Cluster-awareness advisory: no clusters in EITHER run -> warn.
        n_clusters_1 = sum(1 for f in f1_pool if f.is_cluster_parent)
        n_clusters_2 = sum(1 for f in f2_pool if f.is_cluster_parent)
        if n_clusters_1 == 0 and n_clusters_2 == 0:
            msg = (
                "No cluster parents present in either run. Defect matching "
                "ran at the individual-feature level. If clusters are "
                "expected for this pipeline, either pre-cluster the runs "
                "using B31G 3t proximity rules before re-running the matcher, "
                "or accept feature-level pairing for this report."
            )
            result.warnings.append(msg)
            result.qa_flags.append(make_flag(
                QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN, msg,
                context={"n_clusters_run1": 0, "n_clusters_run2": 0},
            ))

        # LOW_DEFECT_MATCH_RATE: <90 % of the smaller pool got paired.
        # match_rate already uses min(|run1|, |run2|) as denominator
        # (see _compute_match_rate) so this is a direct comparison.
        if f1_pool and f2_pool and result.match_rate < 0.90:
            result.qa_flags.append(make_flag(
                QAFlagCode.LOW_DEFECT_MATCH_RATE,
                f"defect match rate {result.match_rate:.1%} below the 90 % target "
                f"({len(result.feature_matches)} matched of "
                f"{min(len(f1_pool), len(f2_pool))} (smaller pool); "
                f"{len(result.unmatched_features_old)} run-1 and "
                f"{len(result.unmatched_features_new)} run-2 features unmatched).",
                context={
                    "match_rate": result.match_rate,
                    "matched": len(result.feature_matches),
                    "unmatched_run1": len(result.unmatched_features_old),
                    "unmatched_run2": len(result.unmatched_features_new),
                },
            ))

        return result

    # ------------------------------------------------------------------

    def _match_joint_pair(
        self,
        feats1: list[Feature],
        feats2: list[Feature],
        cfg: dict[str, Any],
        passes_matched: dict[int, int],
    ) -> tuple[list[FeatureMatch], list[Feature], list[Feature]]:
        """Apply each relaxation pass in order, removing accepted matches
        from the remaining pool between passes.
        """
        remaining1 = list(feats1)
        remaining2 = list(feats2)
        all_matches: list[FeatureMatch] = []

        for pass_idx, pass_cfg in enumerate(cfg["passes"], start=1):
            if not remaining1 or not remaining2:
                break
            new_matches, unm1, unm2 = _hungarian_one_pass(
                remaining1, remaining2, pass_cfg, cfg, pass_idx
            )
            if new_matches:
                passes_matched[pass_idx] = passes_matched.get(pass_idx, 0) + len(new_matches)
            all_matches.extend(new_matches)
            remaining1 = unm1
            remaining2 = unm2

        return all_matches, remaining1, remaining2


# ---------------------------------------------------------------------------
# Hungarian inside one pass
# ---------------------------------------------------------------------------

def _hungarian_one_pass(
    feats1: list[Feature],
    feats2: list[Feature],
    pass_cfg: dict[str, Any],
    cfg: dict[str, Any],
    pass_idx: int,
) -> tuple[list[FeatureMatch], list[Feature], list[Feature]]:
    """Single Hungarian pass with absolute tolerance filtering.

    Returns (matches accepted in this pass, unmatched run1, unmatched run2).
    """
    n1, n2 = len(feats1), len(feats2)
    axial_tol = float(pass_cfg["axial_tolerance_m"])
    clock_tol = float(pass_cfg["clock_tolerance_h"])
    max_cost = float(pass_cfg["max_cost"])

    cost = _vectorised_cost_matrix(feats1, feats2, axial_tol, clock_tol, cfg)

    row_ind, col_ind = linear_sum_assignment(cost)

    matches: list[FeatureMatch] = []
    matched_rows: set[int] = set()
    matched_cols: set[int] = set()
    cap = max(max_cost, 1e-9)

    for i, j in zip(row_ind, col_ind):
        c = float(cost[i, j])
        if c >= _FORBIDDEN_COST / 2.0:
            # Forbidden by the tolerance filter — not a real match.
            continue
        if c > max_cost:
            # Hungarian picked the cheapest under forbidden, but it's still
            # above the pass's acceptance ceiling — defer to next pass.
            continue
        confidence = max(0.0, min(1.0, 1.0 - c / cap))
        matches.append(
            FeatureMatch(
                feature_old=feats1[i],
                feature_new=feats2[j],
                match_score=c,
                confidence=confidence,
                relaxation_level=pass_idx,
            )
        )
        matched_rows.add(i)
        matched_cols.add(j)

    unm1 = [feats1[i] for i in range(n1) if i not in matched_rows]
    unm2 = [feats2[j] for j in range(n2) if j not in matched_cols]
    return matches, unm1, unm2


# ---------------------------------------------------------------------------
# Cost function
# ---------------------------------------------------------------------------

def _vectorised_cost_matrix(
    feats1: list[Feature],
    feats2: list[Feature],
    axial_tol: float,
    clock_tol: float,
    cfg: dict[str, Any],
) -> np.ndarray:
    """Build the [n1 × n2] cost matrix in numpy. Equivalent to calling
    `_pair_cost` per cell but ~100× faster on large joint pairs.

    Forbidden cells (missing data, beyond tolerances) keep the +infinity
    sentinel; valid cells get a finite weighted-cost score plus any
    applicable penalties.
    """
    n1, n2 = len(feats1), len(feats2)
    if n1 == 0 or n2 == 0:
        return np.zeros((n1, n2), dtype=np.float64)

    # Pull arrays out of the feature lists once.
    NAN = np.nan
    uw1 = np.array(
        [f.upstream_weld_dist_m if f.upstream_weld_dist_m is not None else NAN
         for f in feats1], dtype=np.float64
    )
    uw2 = np.array(
        [f.upstream_weld_dist_m if f.upstream_weld_dist_m is not None else NAN
         for f in feats2], dtype=np.float64
    )
    cl1 = np.array(
        [f.clock_decimal_hours if f.clock_decimal_hours is not None else NAN
         for f in feats1], dtype=np.float64
    )
    cl2 = np.array(
        [f.clock_decimal_hours if f.clock_decimal_hours is not None else NAN
         for f in feats2], dtype=np.float64
    )
    d1 = np.array(
        [f.depth_pct_wt if f.depth_pct_wt is not None else NAN for f in feats1],
        dtype=np.float64,
    )
    d2 = np.array(
        [f.depth_pct_wt if f.depth_pct_wt is not None else NAN for f in feats2],
        dtype=np.float64,
    )
    # Encode surface as int — same int means "compatible" or "either is UNKNOWN".
    SURF_UNK = -1
    surf_codes = {
        Surface.UNKNOWN: SURF_UNK,
        Surface.INTERNAL: 0,
        Surface.EXTERNAL: 1,
        Surface.MIDWALL: 2,
    }
    s1 = np.array([surf_codes.get(f.surface, SURF_UNK) for f in feats1], dtype=np.int8)
    s2 = np.array([surf_codes.get(f.surface, SURF_UNK) for f in feats2], dtype=np.int8)
    cp1 = np.array([1 if f.is_cluster_parent else 0 for f in feats1], dtype=np.int8)
    cp2 = np.array([1 if f.is_cluster_parent else 0 for f in feats2], dtype=np.int8)

    # Axial diff & forbidden mask (missing-data or out-of-tolerance).
    axial = np.abs(uw1[:, None] - uw2[None, :])
    axial_forbidden = np.isnan(axial) | (axial > axial_tol)

    # Clock wrap distance. Missing clock on either side -> 0 contribution
    # (don't penalise but don't reward either).
    raw_clock = np.abs(cl1[:, None] - cl2[None, :])
    clock = np.where(raw_clock > 6.0, 12.0 - raw_clock, raw_clock)
    # NaN clock on either side -> treat as 0 for cost but never as "forbidden"
    clock_known = ~(np.isnan(cl1[:, None]) | np.isnan(cl2[None, :]))
    clock_forbidden = clock_known & (clock > clock_tol)
    clock = np.where(clock_known, clock, 0.0)

    # Base weighted cost
    cost = (
        np.nan_to_num(axial, nan=0.0) * float(cfg["axial_weight"])
        + clock * float(cfg["clock_weight"])
    )

    # Surface mismatch — both sides known and different.
    both_known = (s1[:, None] != SURF_UNK) & (s2[None, :] != SURF_UNK)
    surface_mismatch = both_known & (s1[:, None] != s2[None, :])
    cost = np.where(surface_mismatch, cost + float(cfg["surface_mismatch_penalty"]), cost)

    # Depth shrinkage — d1 known & positive, d2 known, d2 < ratio*d1.
    shrinkage_ratio = float(cfg["depth_shrinkage_ratio"])
    d1_known = ~np.isnan(d1)
    d2_known = ~np.isnan(d2)
    depth_known = d1_known[:, None] & d2_known[None, :]
    d1_pos = d1_known[:, None] & (np.nan_to_num(d1[:, None], nan=0.0) > 0)
    shrunk = (
        depth_known
        & d1_pos
        & (np.nan_to_num(d2[None, :], nan=0.0)
           < shrinkage_ratio * np.nan_to_num(d1[:, None], nan=0.0))
    )
    cost = np.where(shrunk, cost + float(cfg["depth_shrinkage_penalty"]), cost)

    # Cluster-type mismatch.
    cluster_mismatch = cp1[:, None] != cp2[None, :]
    cost = np.where(
        cluster_mismatch, cost + float(cfg["cluster_type_mismatch_penalty"]), cost
    )

    # Forbidden cells -> sentinel.
    forbidden = axial_forbidden | clock_forbidden
    cost = np.where(forbidden, _FORBIDDEN_COST, cost)
    return cost.astype(np.float64)


def _pair_cost(
    f1: Feature,
    f2: Feature,
    axial_tol: float,
    clock_tol: float,
    cfg: dict[str, Any],
) -> float:
    u1 = f1.upstream_weld_dist_m
    u2 = f2.upstream_weld_dist_m
    if u1 is None or u2 is None:
        return _FORBIDDEN_COST
    axial = abs(u1 - u2)
    if axial > axial_tol:
        return _FORBIDDEN_COST

    c1 = f1.clock_decimal_hours
    c2 = f2.clock_decimal_hours
    if c1 is not None and c2 is not None:
        d = abs(c1 - c2)
        clock = d if d <= 6.0 else 12.0 - d
    else:
        # Missing clock on either side — don't reject, but don't reward.
        clock = 0.0
    if clock > clock_tol:
        return _FORBIDDEN_COST

    cost = axial * float(cfg["axial_weight"]) + clock * float(cfg["clock_weight"])

    if (
        f1.surface is not f2.surface
        and f1.surface is not Surface.UNKNOWN
        and f2.surface is not Surface.UNKNOWN
    ):
        cost += float(cfg["surface_mismatch_penalty"])

    d1 = f1.depth_pct_wt
    d2 = f2.depth_pct_wt
    if (
        d1 is not None
        and d2 is not None
        and d1 > 0
        and d2 < float(cfg["depth_shrinkage_ratio"]) * d1
    ):
        cost += float(cfg["depth_shrinkage_penalty"])

    if f1.is_cluster_parent != f2.is_cluster_parent:
        cost += float(cfg["cluster_type_mismatch_penalty"])

    return cost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _index_by_joint(features: list[Feature]) -> dict[int, list[Feature]]:
    out: dict[int, list[Feature]] = {}
    for f in features:
        if f.joint_number is None:
            continue
        out.setdefault(f.joint_number, []).append(f)
    return out


def _merge_cfg(base: dict[str, Any], override: dict | None) -> dict[str, Any]:
    """Shallow merge: top-level keys from override replace base wholesale.

    The `passes` list is opaque — if the caller supplies it, theirs wins
    entirely (rather than element-wise merging).
    """
    out = dict(base)
    if override:
        for k, v in override.items():
            out[k] = v
    return out


def _compute_match_rate(
    result: MatchResult,
    f1_pool: list[Feature],
    f2_pool: list[Feature],
) -> float:
    """Conservative match rate: fraction of features paired, against the
    smaller of the two pools.

    The choice of denominator matters: pipelines like HMEL where run-2
    sees ~8× more defects than run-1 (better tool) would look unmatched
    by either-side normalisation. Using `min` here reports "what fraction
    of the side with fewer features got paired", which is the figure
    operators actually care about.
    """
    denom = max(min(len(f1_pool), len(f2_pool)), 1)
    return len(result.feature_matches) / denom
