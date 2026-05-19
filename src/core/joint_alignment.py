"""
Joint-sequence alignment between two ILI runs.

Pipelines have thousands of joints. Run 1 (e.g. 2018) and Run 2 (e.g. 2023)
of the same line may list different counts (joints get replaced, vendors
renumber, the tool's joint detection drifts). To compare anything between
runs (corrosion growth, feature matching) the joints must first be aligned.

Algorithm: banded Needleman-Wunsch global sequence alignment on the joint
length signature, anchored by absolute distance and wall thickness.

    match score:   similarity = 1 - |L1 - L2| / max(L1, L2)
                   match if similarity >= 0.85, else mismatch penalty
                   + WT bonus if |WT1 - WT2| <= 0.5 mm
                   + distance bonus if |abs_d1 - abs_d2| <= 20 m
    gap penalty:   -0.5
    mismatch:      -1.0

The DP table is banded — only cells within `band_width` of the diagonal are
filled. Pipelines are monotonic chainage sequences; alignment drift is
bounded, so a band of ~5 % of the joint count is more than enough and
keeps memory + time linear in joints.

Fall back to nearest-distance matching when joint length is missing in
either run (e.g. some files only ship abs_distance per joint with no
length column). Without lengths NW has no signal — distance still works as
a coarse cue.

Output: a `JointAlignment` whose `.matches` is the requested list of
`JointMatch`. The aligner also runs two post-validations and parks any
diagnostic notes on the result rather than mutating the input runs.

    1. Monotonicity: matched pairs must increase in chainage in BOTH runs.
       Local reversals (joint i+1 maps to a smaller distance than joint i)
       are recorded as suspicious.
    2. Total length parity: sum of matched joint lengths in each run must
       agree within 1 %. Pipelines don't shrink.

If the match rate falls below the project's threshold (default 90 %), a
warning is added; the result is still returned. Downstream code decides
whether that's fatal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.models import ILIRun, Joint, JointMatch
from src.validation import QAFlagCode, make_flag


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class JointAlignment:
    """Result of aligning two runs' joint sequences."""
    matches: list[JointMatch] = field(default_factory=list)
    unmatched_run1: list[Joint] = field(default_factory=list)
    unmatched_run2: list[Joint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    match_rate: float = 0.0
    method: str = ""                              # "needleman_wunsch" | "nearest_distance"

    total_length_run1: float = 0.0
    total_length_run2: float = 0.0

    # (run1_joint_number, run2_joint_number) pairs where chainage went
    # backwards relative to the preceding matched pair.
    monotonicity_violations: list[tuple[int, int]] = field(default_factory=list)

    # Diagnostic counts useful in QA reports.
    band_width: int = 0

    # Structured QAFlag objects emitted during finalisation
    # (LOW_JOINT_MATCH_RATE, REVERSAL_DETECTED, LENGTH_MISMATCH_RUN).
    qa_flags: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Defaults — every value overridable via the `config` argument to `.align()`
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "min_similarity": 0.85,                 # below this counts as mismatch
    "gap_penalty": -0.5,
    "mismatch_penalty": -1.0,
    "wt_bonus": 0.2,
    "wt_tolerance_mm": 0.5,
    "distance_bonus": 0.3,
    # Tolerance for the chainage-agreement bonus. Wide enough to let pairs
    # across modest chainage drift (e.g. odometer recalibration between
    # runs, replacement-pipe insertions) still qualify for the bonus — at
    # which point length-signature similarity decides who wins. Critical
    # for real pipelines like Kandla-Samakhiali where the +30 joint-number
    # offset has 26 m chainage drift but length signatures match to 0.025 %.
    # Empirically chosen: <=200 m loses the canonical Kandla row5↔#125 pair;
    # >=500 m gets both Kandla joint 6380→6410 and HMEL to ≥12 000 matches
    # end-to-end. Net effect on cleanly-aligned pipelines is nil (their
    # candidates are still cheapest within 20 m, the bonus doesn't change
    # who wins, only widens who qualifies).
    "distance_tolerance_m": 500.0,
    "min_match_rate_warning": 0.90,         # warn if alignment falls under this
    "total_length_tolerance_pct": 0.01,     # 1 % length agreement target
    "band_width": None,                     # None -> auto-size from joint counts
    "min_length_coverage": 0.5,             # below this -> fallback method
}


# ---------------------------------------------------------------------------
# Aligner
# ---------------------------------------------------------------------------

class JointAligner:
    """Builds joint correspondences between two ILI runs.

    Usage:
        aligner = JointAligner()
        result  = aligner.align(run1, run2)
        for m in result.matches: ...
    """

    def __init__(self, config: dict | None = None):
        self.cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)

    # ------------------------------------------------------------------

    def align(
        self,
        run1: ILIRun,
        run2: ILIRun,
        config: dict | None = None,
    ) -> JointAlignment:
        cfg = dict(self.cfg)
        if config:
            cfg.update(config)

        joints1 = run1.joints
        joints2 = run2.joints
        result = JointAlignment(method="needleman_wunsch")

        if not joints1 or not joints2:
            result.warnings.append("one or both runs have no joints; nothing to align")
            result.unmatched_run1 = list(joints1)
            result.unmatched_run2 = list(joints2)
            return result

        # Fallback decision — if either run is mostly length-less, NW has
        # nothing to optimise on.
        if (
            _length_coverage(joints1) < cfg["min_length_coverage"]
            or _length_coverage(joints2) < cfg["min_length_coverage"]
        ):
            result.method = "nearest_distance"
            result.warnings.append(
                "joint lengths missing from >50% of joints in one or both runs; "
                "falling back to nearest-distance matching (NW disabled)"
            )
            return self._align_by_distance(joints1, joints2, result, cfg)

        return self._align_nw(joints1, joints2, result, cfg)

    # ------------------------------------------------------------------
    # Needleman-Wunsch (banded)
    # ------------------------------------------------------------------

    def _align_nw(
        self,
        joints1: list[Joint],
        joints2: list[Joint],
        result: JointAlignment,
        cfg: dict[str, Any],
    ) -> JointAlignment:
        n1, n2 = len(joints1), len(joints2)
        band = cfg["band_width"] or max(20, int(0.05 * max(n1, n2)))
        result.band_width = band
        gap = float(cfg["gap_penalty"])
        NEG = -1e18

        # Score matrix — vectorised numpy, O(n1 * n2) in numpy ops then
        # consumed row-by-row in Python below.
        S = _score_matrix(joints1, joints2, cfg)

        # Traceback table: 0=diag (match), 1=up (gap in run2), 2=left (gap in run1).
        T = np.zeros((n1 + 1, n2 + 1), dtype=np.int8)

        # F as two rolling rows of Python floats — single-cell numpy access
        # is too slow for the inner loop on 4900x4904 = 24M cells.
        Fprev: list[float] = [NEG] * (n2 + 1)
        Fcurr: list[float] = [NEG] * (n2 + 1)
        Fprev[0] = 0.0
        for j in range(1, min(n2, band) + 1):
            Fprev[j] = Fprev[j - 1] + gap
            T[0, j] = 2

        for i in range(1, n1 + 1):
            j_lo = max(1, i - band)
            j_hi = min(n2, i + band)

            # Column 0 boundary
            if i <= band:
                Fcurr[0] = Fprev[0] + gap
                T[i, 0] = 1
            else:
                Fcurr[0] = NEG

            # Clear left cells outside band (no info)
            if j_lo > 1:
                for j in range(1, j_lo):
                    Fcurr[j] = NEG

            S_row = S[i - 1].tolist()       # n2 floats
            Ti = T[i]                       # numpy row, indexed by j

            for j in range(j_lo, j_hi + 1):
                diag = Fprev[j - 1] + S_row[j - 1]
                up = Fprev[j] + gap
                left = Fcurr[j - 1] + gap
                if diag >= up:
                    if diag >= left:
                        Fcurr[j] = diag
                        Ti[j] = 0
                    else:
                        Fcurr[j] = left
                        Ti[j] = 2
                else:
                    if up >= left:
                        Fcurr[j] = up
                        Ti[j] = 1
                    else:
                        Fcurr[j] = left
                        Ti[j] = 2

            # Clear right cells outside band
            if j_hi < n2:
                for j in range(j_hi + 1, n2 + 1):
                    Fcurr[j] = NEG

            Fprev, Fcurr = Fcurr, Fprev

        # Traceback ----------------------------------------------------
        i, j = n1, n2
        matches: list[JointMatch] = []
        unm1: list[Joint] = []
        unm2: list[Joint] = []
        min_sim = float(cfg["min_similarity"])

        while i > 0 or j > 0:
            if i > 0 and j > 0:
                t = T[i, j]
            elif j > 0:
                t = 2
            else:
                t = 1

            if t == 0:
                j1, j2 = joints1[i - 1], joints2[j - 1]
                sim = _length_similarity(j1.length_m, j2.length_m)
                if sim >= min_sim:
                    matches.append(
                        JointMatch(
                            joint_old=j1,
                            joint_new=j2,
                            length_diff_m=float((j2.length_m or 0.0) - (j1.length_m or 0.0)),
                            confidence=float(sim),
                            matched_via="needleman_wunsch",
                        )
                    )
                else:
                    # The DP forced a diagonal step but the pair fails the
                    # similarity threshold — treat as unmatched both ways.
                    unm1.append(j1)
                    unm2.append(j2)
                i -= 1
                j -= 1
            elif t == 1:
                unm1.append(joints1[i - 1])
                i -= 1
            else:
                unm2.append(joints2[j - 1])
                j -= 1

        matches.reverse()
        unm1.reverse()
        unm2.reverse()

        return self._finalise(matches, unm1, unm2, joints1, joints2, result, cfg)

    # ------------------------------------------------------------------
    # Nearest-distance fallback
    # ------------------------------------------------------------------

    def _align_by_distance(
        self,
        joints1: list[Joint],
        joints2: list[Joint],
        result: JointAlignment,
        cfg: dict[str, Any],
    ) -> JointAlignment:
        """Greedy monotonic matching by abs_distance.

        Walks both sequences sorted by chainage. For each j1 picks the
        closest unconsumed j2 within `distance_tolerance_m`; otherwise
        the joint goes unmatched. Linear-time.
        """
        tol = float(cfg["distance_tolerance_m"])
        s1 = sorted(joints1, key=lambda j: j.abs_distance_start_m)
        s2 = sorted(joints2, key=lambda j: j.abs_distance_start_m)
        i, j = 0, 0
        matches: list[JointMatch] = []
        unm1: list[Joint] = []
        unm2: list[Joint] = []

        while i < len(s1) and j < len(s2):
            j1, j2 = s1[i], s2[j]
            d = j2.abs_distance_start_m - j1.abs_distance_start_m
            if abs(d) <= tol:
                matches.append(
                    JointMatch(
                        joint_old=j1,
                        joint_new=j2,
                        length_diff_m=float((j2.length_m or 0.0) - (j1.length_m or 0.0)),
                        confidence=1.0 - abs(d) / max(tol, 1e-6),
                        matched_via="nearest_distance",
                    )
                )
                i += 1
                j += 1
            elif d > 0:
                # j1 has no near partner before j2 — drop it as unmatched
                unm1.append(j1)
                i += 1
            else:
                unm2.append(j2)
                j += 1

        unm1.extend(s1[i:])
        unm2.extend(s2[j:])

        return self._finalise(matches, unm1, unm2, joints1, joints2, result, cfg)

    # ------------------------------------------------------------------
    # Post-alignment validation
    # ------------------------------------------------------------------

    def _finalise(
        self,
        matches: list[JointMatch],
        unm1: list[Joint],
        unm2: list[Joint],
        joints1: list[Joint],
        joints2: list[Joint],
        result: JointAlignment,
        cfg: dict[str, Any],
    ) -> JointAlignment:
        result.matches = matches
        result.unmatched_run1 = unm1
        result.unmatched_run2 = unm2
        # Match rate against the larger run — more conservative than averaging.
        denom = max(len(joints1), len(joints2))
        result.match_rate = len(matches) / denom if denom else 0.0
        result.total_length_run1 = float(sum((j.length_m or 0.0) for j in joints1))
        result.total_length_run2 = float(sum((j.length_m or 0.0) for j in joints2))

        # Monotonicity: chainage on both sides must be non-decreasing across
        # successive matches.
        result.monotonicity_violations = _monotonicity_violations(matches)

        # Total length parity (matched joints only — unmatched ones are by
        # definition not common to both runs).
        m_len1 = float(sum((m.joint_old.length_m or 0.0) for m in matches))
        m_len2 = float(sum((m.joint_new.length_m or 0.0) for m in matches))
        if m_len1 > 0 and m_len2 > 0:
            disagreement = abs(m_len1 - m_len2) / max(m_len1, m_len2)
            tol = float(cfg["total_length_tolerance_pct"])
            if disagreement > tol:
                result.warnings.append(
                    f"matched joint length disagrees by "
                    f"{disagreement * 100:.2f}% (run1 {m_len1:.1f} m, "
                    f"run2 {m_len2:.1f} m; tolerance {tol * 100:.1f}%)"
                )

        if result.match_rate < float(cfg["min_match_rate_warning"]):
            result.warnings.append(
                f"match rate {result.match_rate:.1%} is below the target "
                f"{float(cfg['min_match_rate_warning']):.0%}; "
                f"{len(unm1)} run1 joints and {len(unm2)} run2 joints went unmatched"
            )
            result.qa_flags.append(make_flag(
                QAFlagCode.LOW_JOINT_MATCH_RATE,
                f"joint match rate {result.match_rate:.1%} below target "
                f"{float(cfg['min_match_rate_warning']):.0%}; "
                f"{len(unm1)} run-1 / {len(unm2)} run-2 joints unmatched.",
                context={"match_rate": result.match_rate,
                         "target": float(cfg["min_match_rate_warning"]),
                         "unmatched_run1": len(unm1),
                         "unmatched_run2": len(unm2)},
            ))

        if result.monotonicity_violations:
            result.qa_flags.append(make_flag(
                QAFlagCode.REVERSAL_DETECTED,
                f"{len(result.monotonicity_violations)} matched joint pair(s) "
                "have chainage going backwards relative to the previous match; "
                "alignment may be locally wrong.",
                context={"n_violations": len(result.monotonicity_violations),
                         "first_pairs": result.monotonicity_violations[:5]},
            ))

        # Total matched length parity (matched joints only).
        m_len1 = float(sum((m.joint_old.length_m or 0.0) for m in matches))
        m_len2 = float(sum((m.joint_new.length_m or 0.0) for m in matches))
        if m_len1 > 0 and m_len2 > 0:
            disagreement = abs(m_len1 - m_len2) / max(m_len1, m_len2)
            tol = float(cfg["total_length_tolerance_pct"])
            if disagreement > tol:
                result.qa_flags.append(make_flag(
                    QAFlagCode.LENGTH_MISMATCH_RUN,
                    f"total matched joint length disagrees by "
                    f"{disagreement * 100:.2f} % "
                    f"(run1 {m_len1:.1f} m, run2 {m_len2:.1f} m; cap {tol * 100:.1f} %).",
                    context={"disagreement_pct": disagreement * 100,
                             "run1_m": m_len1, "run2_m": m_len2,
                             "tolerance_pct": tol * 100},
                ))

        return result


# ---------------------------------------------------------------------------
# Helpers (module-level so they're picklable / testable in isolation)
# ---------------------------------------------------------------------------

def _length_similarity(len1: float | None, len2: float | None) -> float:
    """Joint-length similarity in [0.0, 1.0]; 0 when either length is missing."""
    if not len1 or not len2 or len1 <= 0 or len2 <= 0:
        return 0.0
    return 1.0 - abs(len1 - len2) / max(len1, len2)


def _length_coverage(joints: list[Joint]) -> float:
    if not joints:
        return 0.0
    n = sum(1 for j in joints if j.length_m and j.length_m > 0)
    return n / len(joints)


def _score_matrix(
    joints1: list[Joint],
    joints2: list[Joint],
    cfg: dict[str, Any],
) -> np.ndarray:
    """NW pairwise scores S[i, j] = score(joints1[i], joints2[j])."""
    n1, n2 = len(joints1), len(joints2)
    lens1 = np.array([float(j.length_m or 0.0) for j in joints1], dtype=np.float32)
    lens2 = np.array([float(j.length_m or 0.0) for j in joints2], dtype=np.float32)
    wts1 = np.array([float(j.wt_mm or 0.0) for j in joints1], dtype=np.float32)
    wts2 = np.array([float(j.wt_mm or 0.0) for j in joints2], dtype=np.float32)
    d1 = np.array([float(j.abs_distance_start_m) for j in joints1], dtype=np.float32)
    d2 = np.array([float(j.abs_distance_start_m) for j in joints2], dtype=np.float32)

    L1 = lens1[:, None]
    L2 = lens2[None, :]
    maxL = np.maximum(L1, L2)
    # similarity = 1 - |L1 - L2| / max(L1, L2); 0 when either length missing
    with np.errstate(divide="ignore", invalid="ignore"):
        sim = 1.0 - np.abs(L1 - L2) / np.where(maxL > 0, maxL, 1.0)
    sim = np.where(maxL > 0, sim, 0.0).astype(np.float32)

    min_sim = float(cfg["min_similarity"])
    mismatch = np.float32(cfg["mismatch_penalty"])
    is_match = sim >= min_sim

    score = np.where(is_match, sim, mismatch).astype(np.float32)

    # WT bonus: applies only when matched AND both WTs present AND close
    has_wt = (wts1[:, None] > 0) & (wts2[None, :] > 0)
    wt_close = np.abs(wts1[:, None] - wts2[None, :]) <= float(cfg["wt_tolerance_mm"])
    score += np.where(
        is_match & has_wt & wt_close, np.float32(cfg["wt_bonus"]), np.float32(0.0)
    )

    # Distance bonus
    d_close = np.abs(d1[:, None] - d2[None, :]) <= float(cfg["distance_tolerance_m"])
    score += np.where(
        is_match & d_close, np.float32(cfg["distance_bonus"]), np.float32(0.0)
    )

    return score


def _monotonicity_violations(
    matches: list[JointMatch],
) -> list[tuple[int, int]]:
    """Indices where chainage went backwards relative to the previous match."""
    out: list[tuple[int, int]] = []
    prev_d1 = -float("inf")
    prev_d2 = -float("inf")
    for m in matches:
        d1 = m.joint_old.abs_distance_start_m
        d2 = m.joint_new.abs_distance_start_m
        if d1 < prev_d1 or d2 < prev_d2:
            out.append((m.joint_old.joint_number, m.joint_new.joint_number))
        prev_d1 = d1
        prev_d2 = d2
    return out
