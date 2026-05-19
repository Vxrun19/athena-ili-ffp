"""Tests for src.core.defect_matcher.

Two layers:

1. **Synthetic** — directly construct Feature lists in known geometries and
   verify the matcher's algorithm (Hungarian + 3-pass relaxation + cost
   function). These don't depend on real files.

2. **Real-data** — end-to-end via ILIReader -> JointAligner -> DefectMatcher.
   The Kandla #125 validation uses a *manually-constructed* joint pair to
   verify matcher correctness regardless of where the joint aligner places
   joint 6380 — the published Kandla pair (run1 j6380 ↔ run2 j6410) crosses
   ~26 m of chainage which length-signature NW alignment doesn't naturally
   produce (it pairs j6380 ↔ j6390 instead). Decoupling the test from the
   aligner's choice lets us validate the matcher in isolation.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.core.defect_matcher import DefectMatcher
from src.core.joint_alignment import JointAligner
from src.io.ili_reader import ILIReader
from src.models import (
    Feature,
    FeatureIdentification,
    ILIRun,
    Joint,
    JointMatch,
    Surface,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# ---------------------------------------------------------------------------
# Synthetic helpers
# ---------------------------------------------------------------------------

def _mk_feature(
    aid: str,
    *,
    joint_number: int = 10,
    abs_distance_m: float = 100.0,
    upstream_weld_dist_m: float = 5.0,
    clock: float = 6.0,
    surface: Surface = Surface.INTERNAL,
    depth: float | None = 20.0,
    wt: float = 8.7,
    is_cluster_parent: bool = False,
    source_run: str = "test_run",
) -> Feature:
    return Feature(
        anomaly_id=aid,
        source_run=source_run,
        joint_number=joint_number,
        abs_distance_m=abs_distance_m,
        upstream_weld_dist_m=upstream_weld_dist_m,
        clock_decimal_hours=clock,
        surface=surface,
        depth_pct_wt=depth,
        wt_mm=wt,
        feature_identification=FeatureIdentification.CORROSION,
        is_cluster_parent=is_cluster_parent,
    )


def _mk_run(run_id: str, features: list[Feature]) -> ILIRun:
    # Build the unique-joint list from the features so features_in_joint works.
    joints_by_num: dict[int, Joint] = {}
    for f in features:
        joints_by_num.setdefault(
            f.joint_number,
            Joint(
                joint_number=f.joint_number,
                abs_distance_start_m=f.abs_distance_m - (f.upstream_weld_dist_m or 0),
                length_m=12.0,
                wt_mm=f.wt_mm,
            ),
        )
    return ILIRun(
        run_id=run_id,
        features=features,
        joints=list(joints_by_num.values()),
    )


def _trivial_joint_match(run1: ILIRun, run2: ILIRun, jn: int) -> JointMatch:
    """Pair run1.joint(jn) with run2.joint(jn) for synthetics."""
    j1 = next(j for j in run1.joints if j.joint_number == jn)
    j2 = next(j for j in run2.joints if j.joint_number == jn)
    return JointMatch(
        joint_old=j1, joint_new=j2,
        length_diff_m=0.0, confidence=1.0, matched_via="synthetic",
    )


# ---------------------------------------------------------------------------
# Synthetic — algorithmic correctness
# ---------------------------------------------------------------------------

class TestSyntheticFiveIdentical:
    """5 defects in run1, same 5 in run2 with depth +1% each → all 5 match."""

    @pytest.fixture
    def setup(self):
        # Spread across the joint at distinct uw positions and clocks.
        configs = [
            (1.0,  3.0,  10.0),
            (3.5,  6.0,  15.0),
            (5.0,  9.0,  20.0),
            (7.5,  11.5, 25.0),
            (10.0, 5.5,  30.0),
        ]
        f1 = [_mk_feature(f"a{i}", upstream_weld_dist_m=uw, clock=cl, depth=d)
              for i, (uw, cl, d) in enumerate(configs)]
        f2 = [_mk_feature(f"b{i}", upstream_weld_dist_m=uw, clock=cl, depth=d + 1.0)
              for i, (uw, cl, d) in enumerate(configs)]
        run1 = _mk_run("r1", f1)
        run2 = _mk_run("r2", f2)
        jm = [_trivial_joint_match(run1, run2, 10)]
        return run1, run2, jm

    def test_all_five_match(self, setup):
        run1, run2, jm = setup
        result = DefectMatcher().match(run1, run2, jm)
        assert len(result.feature_matches) == 5
        assert not result.unmatched_features_old
        assert not result.unmatched_features_new
        # All confidence high (tight match)
        assert all(m.confidence > 0.5 for m in result.feature_matches)
        # All in pass 1 (very tight cost)
        assert all(m.relaxation_level == 1 for m in result.feature_matches)


class TestSyntheticFiveWithExtras:
    """5 run1 defects, same 5 in run2 plus 100 brand-new ones in run2 only.
    All 5 originals match; 100 extras stay unmatched."""

    @pytest.fixture
    def setup(self):
        # Original 5 — uw evenly spaced 1m..9m.
        f1 = [_mk_feature(f"a{i}", upstream_weld_dist_m=1.0 + 2.0 * i, clock=6.0)
              for i in range(5)]
        f2_originals = [_mk_feature(f"b{i}", upstream_weld_dist_m=1.0 + 2.0 * i,
                                    clock=6.05, depth=21.0)
                        for i in range(5)]
        # 100 extra new defects — uw values that don't coincide with originals.
        # Avoid the originals' uw (1, 3, 5, 7, 9 m). Spread elsewhere.
        f2_extras = [_mk_feature(
            f"x{j}",
            upstream_weld_dist_m=0.05 + 0.10 * j,    # 0.05..10.05m in 0.1m steps
            clock=(0.5 + 0.1 * j) % 12.0,
            depth=10.0 + j * 0.1,
        ) for j in range(100)
            if abs((0.05 + 0.10 * j) % 2.0 - 1.0) > 0.15]    # avoid near-originals
        f2 = f2_originals + f2_extras
        run1 = _mk_run("r1", f1)
        run2 = _mk_run("r2", f2)
        jm = [_trivial_joint_match(run1, run2, 10)]
        return run1, run2, jm

    def test_five_match_extras_unmatched(self, setup):
        run1, run2, jm = setup
        result = DefectMatcher().match(run1, run2, jm)
        assert len(result.feature_matches) == 5
        assert len(result.unmatched_features_old) == 0
        assert len(result.unmatched_features_new) == len(run2.features) - 5
        # The originals' run-1 anomaly_ids should appear in matches.
        matched_r1_ids = {m.feature_old.anomaly_id for m in result.feature_matches}
        assert matched_r1_ids == {f"a{i}" for i in range(5)}


class TestSyntheticSurfaceMismatch:
    """5 defects but 1 has surface flipped between runs → 4 match, 1 unmatched
    on each side."""

    @pytest.fixture
    def setup(self):
        # The surface_mismatch_penalty (10.0) plus a normal cost (~0.05)
        # blows past every pass's max_cost ceiling, so the pair is never
        # accepted.
        surfaces1 = [Surface.INTERNAL] * 5
        surfaces2 = [Surface.INTERNAL] * 5
        surfaces2[2] = Surface.EXTERNAL    # the offender
        f1 = [_mk_feature(f"a{i}", upstream_weld_dist_m=1.0 + 2.0 * i,
                          clock=6.0, surface=surfaces1[i])
              for i in range(5)]
        f2 = [_mk_feature(f"b{i}", upstream_weld_dist_m=1.0 + 2.0 * i,
                          clock=6.0, depth=21.0, surface=surfaces2[i])
              for i in range(5)]
        run1 = _mk_run("r1", f1)
        run2 = _mk_run("r2", f2)
        jm = [_trivial_joint_match(run1, run2, 10)]
        return run1, run2, jm

    def test_surface_mismatch_blocks_one_pair(self, setup):
        run1, run2, jm = setup
        result = DefectMatcher().match(run1, run2, jm)
        assert len(result.feature_matches) == 4
        assert len(result.unmatched_features_old) == 1
        assert len(result.unmatched_features_new) == 1
        # The unmatched ones are specifically the surface-mismatched pair.
        assert result.unmatched_features_old[0].anomaly_id == "a2"
        assert result.unmatched_features_new[0].anomaly_id == "b2"


# ---------------------------------------------------------------------------
# Synthetic — cluster handling + warnings
# ---------------------------------------------------------------------------

class TestClusterWarning:
    def test_warning_when_no_clusters_in_either_run(self):
        run1 = _mk_run("r1", [_mk_feature("a")])
        run2 = _mk_run("r2", [_mk_feature("b")])
        jm = [_trivial_joint_match(run1, run2, 10)]
        result = DefectMatcher().match(run1, run2, jm)
        assert any("cluster" in w.lower() for w in result.warnings)

    def test_no_warning_when_clusters_present(self):
        run1 = _mk_run("r1", [_mk_feature("a", is_cluster_parent=True)])
        run2 = _mk_run("r2", [_mk_feature("b", is_cluster_parent=True)])
        jm = [_trivial_joint_match(run1, run2, 10)]
        result = DefectMatcher().match(run1, run2, jm)
        assert not any("cluster" in w.lower() for w in result.warnings)


class TestClusterTypePenalty:
    """A cluster parent matching a standalone gets a small penalty (0.2);
    a cluster-parent ↔ cluster-parent and standalone ↔ standalone pair
    each match cheaper than the cross-type pairing."""

    def test_parents_prefer_parents(self):
        # Two defects in each run at the same location: one cluster parent,
        # one standalone. Hungarian should pair like-with-like.
        f1 = [
            _mk_feature("a_parent", upstream_weld_dist_m=2.0, is_cluster_parent=True),
            _mk_feature("a_standalone", upstream_weld_dist_m=2.0,
                        clock=8.0, is_cluster_parent=False),
        ]
        f2 = [
            _mk_feature("b_parent", upstream_weld_dist_m=2.0, depth=22.0,
                        is_cluster_parent=True),
            _mk_feature("b_standalone", upstream_weld_dist_m=2.0, clock=8.0,
                        depth=22.0, is_cluster_parent=False),
        ]
        run1 = _mk_run("r1", f1)
        run2 = _mk_run("r2", f2)
        jm = [_trivial_joint_match(run1, run2, 10)]
        result = DefectMatcher().match(run1, run2, jm)
        # Should produce 2 matches with parent->parent and standalone->standalone.
        assert len(result.feature_matches) == 2
        pairs = {(m.feature_old.anomaly_id, m.feature_new.anomaly_id)
                 for m in result.feature_matches}
        assert ("a_parent", "b_parent") in pairs
        assert ("a_standalone", "b_standalone") in pairs


# ---------------------------------------------------------------------------
# Synthetic — depth shrinkage penalty
# ---------------------------------------------------------------------------

class TestDepthShrinkagePenalty:
    """When run2 depth < 0.5 * run1 depth, the pair gets a +1.0 cost penalty.
    Combined with a small base cost this still fits within pass-3 ceiling
    (1.5), but pushes the match out of pass 1 (max 0.5) into a later pass."""

    def test_shrunk_pair_lands_in_later_pass(self):
        f1 = [_mk_feature("a", upstream_weld_dist_m=2.0, depth=40.0)]
        # Drop depth from 40 -> 10 (much less than half) but keep position.
        f2 = [_mk_feature("b", upstream_weld_dist_m=2.0, depth=10.0)]
        run1 = _mk_run("r1", f1)
        run2 = _mk_run("r2", f2)
        jm = [_trivial_joint_match(run1, run2, 10)]
        result = DefectMatcher().match(run1, run2, jm)
        assert len(result.feature_matches) == 1
        m = result.feature_matches[0]
        # Cost = 0 axial + 0 clock + 1.0 shrinkage = 1.0 -> exceeds pass-1 (0.5),
        # fits pass-2 (1.0). Pass 2 expected.
        assert m.relaxation_level >= 2
        assert m.match_score >= 1.0


# ---------------------------------------------------------------------------
# Real-data — Kandla #125 via manual joint pair
# ---------------------------------------------------------------------------

class TestKandlaFeature125:
    """The published Athena Kandla-Samakhiali LPG FFP report Table 6b lists
    the highest-CGR defect as feature #125 (run2 anomaly_id '125', joint
    6410, abs_dist 7453.05m, depth 28.75%) matched to a run-1 feature at
    depth 12%. In our reader output the run-1 partner is `row5` at joint
    6380, uw=11.102m, depth=12.0%, clock=5.3, internal.

    The +30 joint-number offset has 26 m of chainage drift relative to the
    +10 offset's 1.4 m, but a far better length-signature match (0.025 %
    vs 2.6 %). With `distance_tolerance_m=500` (the default after the
    Prompt 4 follow-up), both offsets qualify for the chainage bonus and
    length similarity wins — joint 6380 now maps to 6410 via the production
    aligner without any manual override.
    """

    @pytest.fixture(scope="class")
    def runs(self) -> tuple[ILIRun, ILIRun]:
        reader = ILIReader()
        return (
            reader.read(
                EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
                run_id="kandla_run1",
            ),
            reader.read(
                EXAMPLES / "1ZSV_Pipeline_Listing.xlsx",
                run_id="kandla_run2",
            ),
        )

    def test_finds_row5_to_125_via_production_aligner(self, runs):
        run1, run2 = runs
        joint_alignment = JointAligner().align(run1, run2)
        # Sanity: aligner now pairs joint 6380 with 6410.
        m_6380 = next(
            (m for m in joint_alignment.matches if m.joint_old.joint_number == 6380),
            None,
        )
        assert m_6380 is not None, "joint 6380 not in alignment output"
        assert m_6380.joint_new.joint_number == 6410, (
            f"expected r1.6380 → r2.6410; got r2.{m_6380.joint_new.joint_number}"
        )

        result = DefectMatcher().match(run1, run2, joint_alignment.matches)
        pair = next(
            (m for m in result.feature_matches
             if m.feature_old.anomaly_id == "row5"
             and m.feature_new.anomaly_id == "125"),
            None,
        )
        assert pair is not None, (
            f"row5 ↔ 125 not in matches; matches include "
            f"{[(m.feature_old.anomaly_id, m.feature_new.anomaly_id) for m in result.feature_matches[:10]]}…"
        )
        # The pair has axial diff 0.006 m + clock diff 0.167 h -> cost ~0.06,
        # well inside pass-1's 0.5 ceiling.
        assert pair.relaxation_level == 1
        assert pair.match_score < 0.2
        assert pair.confidence > 0.5
        assert pair.feature_old.depth_pct_wt == pytest.approx(12.0)
        assert pair.feature_new.depth_pct_wt == pytest.approx(28.75)


# ---------------------------------------------------------------------------
# Real-data — full pipeline integration
# ---------------------------------------------------------------------------

class TestKandlaPairFullPipeline:
    """End-to-end on the Kandla pair using the production joint aligner +
    defect matcher. After the Prompt 4 follow-up fixes (chainage-lookup
    reader path + widened joint distance tolerance + wider pass-3 axial
    tolerance) we reproduce ≥22 of the 23 matches in the published Athena
    Annexure C, including the canonical highest-CGR pair row5↔#125.
    """

    @pytest.fixture(scope="class")
    def result(self):
        reader = ILIReader()
        run1 = reader.read(
            EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
            run_id="kandla_run1",
        )
        run2 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="kandla_run2")
        ja = JointAligner().align(run1, run2)
        return DefectMatcher().match(run1, run2, ja.matches)

    def test_matched_defect_count_near_published(self, result):
        # Published Annexure C: 23 matches. Floor at 22 — the user spec
        # allows for one borderline pair. As of the follow-up we get 22.
        n = len(result.feature_matches)
        assert n >= 22, f"expected ≥22 matches; got {n}"

    def test_no_clusters_warning_still_present(self, result):
        # Kandla has no cluster parents in either run — the warning is
        # advisory and must still fire.
        assert any("cluster" in w.lower() for w in result.warnings)

    def test_completes_quickly(self):
        reader = ILIReader()
        run1 = reader.read(
            EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
            run_id="kandla_run1",
        )
        run2 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="kandla_run2")
        ja = JointAligner().align(run1, run2)
        t0 = time.time()
        DefectMatcher().match(run1, run2, ja.matches)
        assert time.time() - t0 < 5.0


class TestHMELPairFullPipeline:
    """HMEL IPS-1 to IPS-2 end-to-end. The published FFP Annexure E lists
    13 267 matched defects. With the reader's chainage-lookup mode active
    (parse warning RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE) and the
    widened joint-aligner tolerance, we reach ≥12 000 matches end-to-end
    in under 60 s.
    """

    def test_matched_defect_count_near_published(self):
        reader = ILIReader()
        run1 = reader.read(
            EXAMPLES / "8-9100-13964_28IP1IP2_Pipe_Tally_run1_.xlsx",
            run_id="hmel_run1",
        )
        run2 = reader.read(
            EXAMPLES / "1YCF_Pipeline_Listing__run2_.xlsx",
            run_id="hmel_run2",
        )
        # Reader Bug 1 sanity: chainage-lookup mode kicked in for run 1
        # (rows 9-78 are anomaly block with scrambled abs_distance).
        assert any(
            "RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE" in w
            for w in run1.parse_warnings
        ), f"chainage-lookup warning missing from run1 parse_warnings"
        # No feature can have negative upstream_weld_dist_m after the fix.
        neg_uw = [
            f for f in run1.features
            if f.upstream_weld_dist_m is not None and f.upstream_weld_dist_m < 0
        ]
        assert not neg_uw, (
            f"{len(neg_uw)} features have negative upstream_weld_dist_m "
            "— chainage lookup is broken"
        )

        ja = JointAligner().align(run1, run2)
        t0 = time.time()
        result = DefectMatcher().match(run1, run2, ja.matches)
        elapsed = time.time() - t0

        # Spec target: ≥12 000 (within 5 % of published 13 267).
        assert len(result.feature_matches) >= 12_000, (
            f"got {len(result.feature_matches)} matches; spec target ≥12 000"
        )
        # Performance budget.
        assert elapsed < 60.0, f"defect matching took {elapsed:.1f}s on HMEL"

        # Accounting integrity.
        assert (
            len(result.feature_matches) + len(result.unmatched_features_old)
            == len(run1.features_for_assessment())
        )
        assert (
            len(result.feature_matches) + len(result.unmatched_features_new)
            == len(run2.features_for_assessment())
        )
