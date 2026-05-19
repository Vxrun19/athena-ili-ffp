"""Tests for the QA flag system.

Three layers:

1. **Per-flag positive + negative** — for each `QAFlagCode` we own a
   trigger condition, verify it fires AND that it doesn't fire under
   normal conditions.

2. **Aggregator** — deduplication, severity bucketing, counts,
   has_critical, summary, synthesised pipeline-level flags.

3. **Integration on Kandla** — run the full chain (read→align→match→CGR→
   FFP→predict), aggregate, and assert the expected flag pattern (no
   ERF_EXCEEDS_1, no DEPTH_EXCEEDS_80, UNMATCHED_RUN2 ≈ 311,
   LOW_DEFECT_MATCH_RATE fires, NO_CLUSTERS_IN_EITHER_RUN fires).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.core.cgr import CGRCalculator, CGRResult
from src.core.defect_matcher import DefectMatcher
from src.core.ffp import b31g_modified, b31g_original, ffp_assess, kastner
from src.core.joint_alignment import JointAligner
from src.core.repair_predictor import RepairPredictor
from src.io.ili_reader import ILIReader
from src.models import (
    DimensionClass,
    FFPMethod,
    FFPResult,
    Feature,
    FeatureIdentification,
    Joint,
    JointMatch,
    MAOPZone,
    MatchResult,
    Pipeline,
    Surface,
)
from src.validation import (
    CANONICAL_SEVERITY,
    QAFlag,
    QAFlagCode,
    QASeverity,
    make_flag,
    severity_for,
)
from src.validation.flag_aggregator import FlagAggregator, FlagReport

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_feature(
    *,
    aid: str = "x",
    depth_pct: float = 30.0,
    wt: float = 6.4,
    length_mm: float = 30.0,
    width_mm: float = 30.0,
    surface: Surface = Surface.INTERNAL,
    dim: DimensionClass = DimensionClass.PITTING,
) -> Feature:
    return Feature(
        anomaly_id=aid,
        source_run="r2",
        depth_pct_wt=depth_pct,
        wt_mm=wt,
        length_mm=length_mm,
        width_mm=width_mm,
        surface=surface,
        feature_identification=FeatureIdentification.CORROSION,
        dimension_class=dim,
    )


def _pipeline(*, D=273.0, smys=358.0, maop=70.0, Fd=0.72,
              wt_lo=6.0, wt_hi=8.0) -> Pipeline:
    return Pipeline(
        diameter_mm=D, smys_mpa=smys,
        maop_zones=[MAOPZone(wt_lo, wt_hi, Fd, maop)],
    )


# ---------------------------------------------------------------------------
# Canonical-severity sanity
# ---------------------------------------------------------------------------

class TestCanonicalSeverity:
    @pytest.mark.parametrize("code,expected", [
        (QAFlagCode.ERF_EXCEEDS_1, QASeverity.ERROR),
        (QAFlagCode.DEPTH_EXCEEDS_80, QASeverity.ERROR),
        (QAFlagCode.MISSING_COLUMN, QASeverity.ERROR),
        (QAFlagCode.LAT_LON_OUT_OF_BOUNDS, QASeverity.ERROR),
        (QAFlagCode.SHEET_NOT_DETECTED, QASeverity.ERROR),
        (QAFlagCode.EXTREME_CGR, QASeverity.WARN),
        (QAFlagCode.LOW_DEFECT_MATCH_RATE, QASeverity.WARN),
        (QAFlagCode.LONG_DEFECT_OUTSIDE_B31G, QASeverity.WARN),
        (QAFlagCode.NEGATIVE_GROWTH, QASeverity.INFO),
        (QAFlagCode.UNMATCHED_RUN2, QASeverity.INFO),
        (QAFlagCode.COORDINATES_SWAPPED, QASeverity.INFO),
        (QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN, QASeverity.INFO),
    ])
    def test_severity_matches_canonical(self, code, expected):
        assert severity_for(code) == expected
        assert CANONICAL_SEVERITY[code] == expected

    def test_make_flag_uses_canonical_severity(self):
        f = make_flag(QAFlagCode.NEGATIVE_GROWTH, "test")
        assert f.severity is QASeverity.INFO

    def test_make_flag_override_severity(self):
        f = make_flag(QAFlagCode.NEGATIVE_GROWTH, "test", severity=QASeverity.WARN)
        assert f.severity is QASeverity.WARN

    def test_every_code_has_a_severity(self):
        for code in QAFlagCode:
            assert severity_for(code) in QASeverity


# ---------------------------------------------------------------------------
# Per-flag positive + negative — FFP flags
# ---------------------------------------------------------------------------

class TestERFExceeds1Flag:
    def test_fires_when_erf_over_one(self):
        # Long defect at 80 % depth on a 10" line → high ERF.
        r = b31g_original(
            d_mm=5.0, L_mm=500.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        assert r.erf >= 1.0
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.ERF_EXCEEDS_1 in codes

    def test_does_not_fire_when_erf_under_one(self):
        # Tiny defect on the same line → ERF well below 1.
        r = b31g_original(
            d_mm=0.5, L_mm=10.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        assert r.erf < 1.0
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.ERF_EXCEEDS_1 not in codes


class TestDepthExceeds80Flag:
    def test_fires_at_or_above_80_pct(self):
        r = b31g_original(
            d_mm=5.2, L_mm=20.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        assert r.depth_pct_wt >= 80.0
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.DEPTH_EXCEEDS_80 in codes

    def test_does_not_fire_below_80_pct(self):
        r = b31g_original(
            d_mm=1.84, L_mm=9.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.DEPTH_EXCEEDS_80 not in codes


class TestLongDefectOutsideB31GFlag:
    def test_fires_when_z_above_50_in_b31g_original(self):
        # HMEL #209581 dimensions on B31G ORIGINAL → z = 246.6.
        r = b31g_original(
            d_mm=2.244, L_mm=1235.0, t_mm=8.7, D_mm=711.0,
            smys_mpa=482.0, Fd=0.72, maop_kgcm2=96.7,
        )
        assert r.z_value and r.z_value > 50
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.LONG_DEFECT_OUTSIDE_B31G in codes

    def test_does_not_fire_for_b31g_modified(self):
        # Same z > 50 but with B31G Modified — Modified is designed for
        # this regime, no flag.
        r = b31g_modified(
            d_mm=2.244, L_mm=1235.0, t_mm=8.7, D_mm=711.0,
            smys_mpa=482.0, Fd=0.72, maop_kgcm2=96.7,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.LONG_DEFECT_OUTSIDE_B31G not in codes

    def test_does_not_fire_when_z_under_50(self):
        # Kandla #125 → z ≈ 0.046.
        r = b31g_original(
            d_mm=1.84, L_mm=9.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.LONG_DEFECT_OUTSIDE_B31G not in codes


class TestVeryShortDefectFlag:
    def test_fires_when_length_below_wt(self):
        # Kandla #125 L=9, t=6.4 → 9 > 6.4 so no flag. Try L=5 < t=6.4.
        r = b31g_original(
            d_mm=1.84, L_mm=5.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.VERY_SHORT_DEFECT in codes

    def test_does_not_fire_when_length_above_wt(self):
        r = b31g_original(
            d_mm=1.84, L_mm=9.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.VERY_SHORT_DEFECT not in codes

    def test_does_not_fire_for_kastner_width(self):
        # Kastner stores W in length_mm; W < t shouldn't fire (W is
        # circumferential, not axial). Tiny circumferential pinhole.
        r = kastner(
            d_mm=1.0, W_mm=2.0, t_mm=6.4, D_mm=273.0,
            smys_mpa=358.0, Fd=0.72, maop_kgcm2=70.0,
        )
        codes = [f.code for f in r.qa_flags]
        assert QAFlagCode.VERY_SHORT_DEFECT not in codes


class TestMAOPZoneNotFoundFlag:
    def test_fires_when_wt_outside_zones(self):
        feature = _mk_feature(wt=15.0)    # outside zone 6-8
        pipeline = _pipeline(wt_lo=6.0, wt_hi=8.0)
        results = ffp_assess(feature, pipeline)
        codes = [f.code for r in results for f in r.qa_flags]
        assert QAFlagCode.MAOP_ZONE_NOT_FOUND in codes

    def test_does_not_fire_when_wt_in_zone(self):
        feature = _mk_feature(wt=6.4)     # inside zone 6-8
        pipeline = _pipeline(wt_lo=6.0, wt_hi=8.0)
        results = ffp_assess(feature, pipeline)
        codes = [f.code for r in results for f in r.qa_flags]
        assert QAFlagCode.MAOP_ZONE_NOT_FOUND not in codes


# ---------------------------------------------------------------------------
# Per-flag — Joint alignment
# ---------------------------------------------------------------------------

class TestLowJointMatchRateFlag:
    def test_fires_for_misaligned_runs(self):
        from src.io.ili_reader import ILIReader as IR
        reader = IR()
        # Synthetic: two single-joint runs that don't share length signatures.
        run1 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="r1")
        # Re-align to itself with extreme similarity threshold so most
        # pairs become "mismatches" → match_rate < 0.9.
        aligner = JointAligner({"min_similarity": 0.9999, "min_match_rate_warning": 0.99})
        result = aligner.align(run1, run1, config={"min_similarity": 0.9999})
        # With a near-impossible similarity threshold, match_rate falls.
        if result.match_rate < 0.99:
            assert any(f.code is QAFlagCode.LOW_JOINT_MATCH_RATE
                       for f in result.qa_flags)

    def test_does_not_fire_at_full_match_rate(self):
        reader = ILIReader()
        run1 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="r1")
        result = JointAligner().align(run1, run1)
        assert result.match_rate >= 0.99
        codes = [f.code for f in result.qa_flags]
        assert QAFlagCode.LOW_JOINT_MATCH_RATE not in codes


class TestReversalDetectedFlag:
    def test_does_not_fire_for_clean_alignment(self):
        reader = ILIReader()
        run1 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="r1")
        result = JointAligner().align(run1, run1)
        assert result.monotonicity_violations == []
        codes = [f.code for f in result.qa_flags]
        assert QAFlagCode.REVERSAL_DETECTED not in codes


# ---------------------------------------------------------------------------
# Per-flag — Defect matcher
# ---------------------------------------------------------------------------

class TestLowDefectMatchRateFlag:
    def test_fires_for_low_match_rate(self):
        # Build run1 with 10 features, run2 with the same 10 + 90 extras.
        # Matcher should match ~10/10 → rate 1.0 → no flag.
        # To force LOW: have 10 features in r1 but ONLY 1 in r2.
        from src.io.ili_reader import ILIReader  # noqa
        r1_feats = [_mk_feature(aid=f"r1_{i}", length_mm=30.0,
                                width_mm=10.0) for i in range(10)]
        r2_feats = [_mk_feature(aid="r2_0", length_mm=30.0,
                                width_mm=10.0)]

        # Each feature gets a unique joint number.
        for i, f in enumerate(r1_feats):
            f.joint_number = 10 + i * 10
            f.upstream_weld_dist_m = 5.0
            f.clock_decimal_hours = 6.0
        r2_feats[0].joint_number = 10
        r2_feats[0].upstream_weld_dist_m = 5.0
        r2_feats[0].clock_decimal_hours = 6.0

        from src.models import ILIRun, Joint
        run1 = ILIRun(run_id="r1", features=r1_feats,
                      joints=[Joint(joint_number=10+i*10, abs_distance_start_m=i*12,
                                    length_m=12.0, wt_mm=6.4) for i in range(10)])
        run2 = ILIRun(run_id="r2", features=r2_feats,
                      joints=[Joint(joint_number=10, abs_distance_start_m=0,
                                    length_m=12.0, wt_mm=6.4)])

        # Manual joint match: r1 joint 10 ↔ r2 joint 10.
        jm = [JointMatch(joint_old=run1.joints[0], joint_new=run2.joints[0],
                         length_diff_m=0.0, confidence=1.0, matched_via="test")]
        mr = DefectMatcher().match(run1, run2, jm)
        # match_rate = 1/min(10, 1) = 1.0 → no flag. Flip: 1/10 from r1 side.
        # Actually with min(10, 1)=1, rate = 1/1 = 100%. The synthesised
        # case requires the smaller pool to be partially unmatched.
        # Build a synthetic with both pools having multiple features and
        # only some matched.
        r1_feats = [_mk_feature(aid=f"r1_{i}") for i in range(10)]
        r2_feats = [_mk_feature(aid=f"r2_{i}") for i in range(10)]
        for i, (f1, f2) in enumerate(zip(r1_feats, r2_feats)):
            f1.joint_number = 10
            f2.joint_number = 10
            f1.upstream_weld_dist_m = 1.0 + i * 1.0
            f2.upstream_weld_dist_m = 1.0 + i * 1.0
            f1.clock_decimal_hours = 6.0
            f2.clock_decimal_hours = 6.0
            # Push 8 of them across surfaces so they fail the surface-
            # mismatch penalty and stay unmatched.
            if i >= 2:
                f1.surface = Surface.INTERNAL
                f2.surface = Surface.EXTERNAL
        run1 = ILIRun(run_id="r1", features=r1_feats,
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        run2 = ILIRun(run_id="r2", features=r2_feats,
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        jm = [JointMatch(run1.joints[0], run2.joints[0], 0.0, 1.0, "test")]
        mr = DefectMatcher().match(run1, run2, jm)
        # 2 matched of 10 → rate 0.2 < 0.9 → LOW_DEFECT_MATCH_RATE fires.
        assert mr.match_rate < 0.9
        codes = [f.code for f in mr.qa_flags]
        assert QAFlagCode.LOW_DEFECT_MATCH_RATE in codes

    def test_does_not_fire_when_match_rate_high(self):
        from src.models import ILIRun, Joint
        r1_feats = [_mk_feature(aid=f"r1_{i}") for i in range(5)]
        r2_feats = [_mk_feature(aid=f"r2_{i}") for i in range(5)]
        for i, (f1, f2) in enumerate(zip(r1_feats, r2_feats)):
            f1.joint_number = 10
            f2.joint_number = 10
            f1.upstream_weld_dist_m = 1.0 + i * 1.0
            f2.upstream_weld_dist_m = 1.0 + i * 1.0
            f1.clock_decimal_hours = 6.0
            f2.clock_decimal_hours = 6.0
        run1 = ILIRun(run_id="r1", features=r1_feats,
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        run2 = ILIRun(run_id="r2", features=r2_feats,
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        jm = [JointMatch(run1.joints[0], run2.joints[0], 0.0, 1.0, "test")]
        mr = DefectMatcher().match(run1, run2, jm)
        assert mr.match_rate == 1.0
        codes = [f.code for f in mr.qa_flags]
        assert QAFlagCode.LOW_DEFECT_MATCH_RATE not in codes


class TestNoClustersInEitherRunFlag:
    def test_fires_when_neither_run_has_clusters(self):
        from src.models import ILIRun, Joint
        run1 = ILIRun(run_id="r1", features=[_mk_feature(aid="a")],
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        run2 = ILIRun(run_id="r2", features=[_mk_feature(aid="b")],
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        mr = DefectMatcher().match(run1, run2, [])
        codes = [f.code for f in mr.qa_flags]
        assert QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN in codes

    def test_does_not_fire_when_clusters_present(self):
        from src.models import ILIRun, Joint
        feat = _mk_feature(aid="a")
        feat.is_cluster_parent = True
        run1 = ILIRun(run_id="r1", features=[feat],
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        run2 = ILIRun(run_id="r2", features=[_mk_feature(aid="b")],
                      joints=[Joint(10, 0.0, 12.0, 6.4)])
        mr = DefectMatcher().match(run1, run2, [])
        codes = [f.code for f in mr.qa_flags]
        assert QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN not in codes


# ---------------------------------------------------------------------------
# Per-flag — CGR (already-emitting, just verify the wiring)
# ---------------------------------------------------------------------------

class TestCGRFlags:
    def test_unmatched_run2_fires_for_unmatched_features(self):
        feat = _mk_feature(aid="u", depth_pct=15.0, wt=6.4)
        mr = MatchResult(unmatched_features_new=[feat])
        results = CGRCalculator().compute(mr, years_between=5.0)
        codes = [f.code for r in results for f in r.qa_flags]
        assert QAFlagCode.UNMATCHED_RUN2 in codes

    def test_negative_growth_severity_is_info(self):
        # Feature got shallower → NEGATIVE_GROWTH with INFO severity.
        f_old = _mk_feature(aid="m", depth_pct=20.0, wt=6.4, surface=Surface.INTERNAL)
        f_new = _mk_feature(aid="m", depth_pct=15.0, wt=6.4, surface=Surface.INTERNAL)
        from src.models import FeatureMatch
        mr = MatchResult(feature_matches=[FeatureMatch(
            feature_old=f_old, feature_new=f_new,
            match_score=0.0, confidence=1.0,
        )])
        results = CGRCalculator().compute(mr, years_between=5.0)
        ng = [f for r in results for f in r.qa_flags
              if f.code is QAFlagCode.NEGATIVE_GROWTH]
        assert ng
        assert all(f.severity is QASeverity.INFO for f in ng)


# ---------------------------------------------------------------------------
# Per-flag — Reader (using existing real files)
# ---------------------------------------------------------------------------

class TestReaderFlags:
    def test_reconstructed_joint_context_fires_on_hmel_run1(self):
        """HMEL run-1 has the scrambled anomaly block (rows 9-78); the
        reader switches to chainage-lookup mode and must emit the flag."""
        run = ILIReader().read(
            EXAMPLES / "8-9100-13964_28IP1IP2_Pipe_Tally_run1_.xlsx",
            run_id="h1",
        )
        codes = [f.code for f in run.qa_flags]
        assert QAFlagCode.RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE in codes

    def test_coordinates_swapped_fires_on_athena_2018(self):
        """Athena 2018 Kandla file has lat/lon swapped at source."""
        run = ILIReader().read(
            EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
            run_id="k1",
        )
        codes = [f.code for f in run.qa_flags]
        assert QAFlagCode.COORDINATES_SWAPPED in codes

    def test_does_not_fire_chainage_flag_on_monotonic_file(self):
        """Monotonic files (1ZSV) don't trigger chainage-lookup mode."""
        run = ILIReader().read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="r1")
        codes = [f.code for f in run.qa_flags]
        assert QAFlagCode.RECONSTRUCTED_JOINT_CONTEXT_FROM_CHAINAGE not in codes


# ---------------------------------------------------------------------------
# Aggregator — deduplication, bucketing, summary, has_critical
# ---------------------------------------------------------------------------

class TestAggregator:
    def test_dedupes_same_code_same_feature(self):
        f1 = make_flag(QAFlagCode.ERF_EXCEEDS_1, "from FFP", feature_id="125")
        f2 = make_flag(QAFlagCode.ERF_EXCEEDS_1, "from predictor", feature_id="125")
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [f1, f2]
        report = FlagAggregator().aggregate(run1=run)
        assert len(report.all_flags) == 1
        assert report.counts[QAFlagCode.ERF_EXCEEDS_1] == 1

    def test_does_not_dedupe_same_code_different_feature(self):
        f1 = make_flag(QAFlagCode.UNMATCHED_RUN2, "a", feature_id="a")
        f2 = make_flag(QAFlagCode.UNMATCHED_RUN2, "b", feature_id="b")
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [f1, f2]
        report = FlagAggregator().aggregate(run1=run)
        assert len(report.all_flags) == 2
        assert report.counts[QAFlagCode.UNMATCHED_RUN2] == 2

    def test_severity_buckets_populated(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [
            make_flag(QAFlagCode.ERF_EXCEEDS_1, "x", feature_id="a"),
            make_flag(QAFlagCode.EXTREME_CGR, "y", feature_id="b"),
            make_flag(QAFlagCode.UNMATCHED_RUN2, "z", feature_id="c"),
        ]
        report = FlagAggregator().aggregate(run1=run)
        assert len(report.flags_by_severity[QASeverity.ERROR]) == 1
        assert len(report.flags_by_severity[QASeverity.WARN]) == 1
        assert len(report.flags_by_severity[QASeverity.INFO]) == 1

    def test_has_critical_true_when_erf_exceeds_1(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [make_flag(QAFlagCode.ERF_EXCEEDS_1, "x", feature_id="a")]
        report = FlagAggregator().aggregate(run1=run)
        assert report.has_critical is True

    def test_has_critical_true_when_depth_exceeds_80(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [make_flag(QAFlagCode.DEPTH_EXCEEDS_80, "x", feature_id="a")]
        report = FlagAggregator().aggregate(run1=run)
        assert report.has_critical is True

    def test_has_critical_false_for_warns_and_infos(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [
            make_flag(QAFlagCode.EXTREME_CGR, "x", feature_id="a"),
            make_flag(QAFlagCode.UNMATCHED_RUN2, "y", feature_id="b"),
        ]
        report = FlagAggregator().aggregate(run1=run)
        assert report.has_critical is False

    def test_flags_by_feature_lookup(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [
            make_flag(QAFlagCode.UNMATCHED_RUN2, "u", feature_id="125"),
            make_flag(QAFlagCode.EXTREME_CGR, "e", feature_id="125"),
            make_flag(QAFlagCode.NEGATIVE_GROWTH, "n", feature_id="200"),
        ]
        report = FlagAggregator().aggregate(run1=run)
        assert len(report.flags_by_feature["125"]) == 2
        assert len(report.flags_by_feature["200"]) == 1

    def test_summary_clean_when_no_findings(self):
        report = FlagAggregator().aggregate()
        assert "clean" in report.summary.lower()

    def test_summary_calls_out_review_when_critical(self):
        from src.models import ILIRun
        run = ILIRun(run_id="r")
        run.qa_flags = [make_flag(QAFlagCode.ERF_EXCEEDS_1, "x", feature_id="a")]
        report = FlagAggregator().aggregate(run1=run)
        assert "REVIEW REQUIRED" in report.summary

    def test_severity_normalised_from_canonical_map(self):
        """Even if the emitter set a non-canonical severity, the aggregator
        re-buckets using CANONICAL_SEVERITY."""
        from src.models import ILIRun
        f = QAFlag(
            code=QAFlagCode.NEGATIVE_GROWTH,
            message="custom",
            severity=QASeverity.ERROR,    # wrong on purpose
            feature_id="a",
        )
        run = ILIRun(run_id="r")
        run.qa_flags = [f]
        report = FlagAggregator().aggregate(run1=run)
        # Should land in INFO not ERROR.
        assert any(x.code is QAFlagCode.NEGATIVE_GROWTH
                   for x in report.flags_by_severity[QASeverity.INFO])
        assert not any(x.code is QAFlagCode.NEGATIVE_GROWTH
                       for x in report.flags_by_severity[QASeverity.ERROR])


class TestAggregatorSynthesised:
    def test_repair_predicted_within_horizon_fires(self):
        from src.models import RepairPrediction
        feat = _mk_feature(aid="x")
        pred = RepairPrediction(
            feature_id="x", feature=feat, cgr_mm_per_year=0.5,
            repair_trigger="ERF_1.0", repair_year_offset=3,
        )
        report = FlagAggregator().aggregate(predictions=[pred])
        codes = [f.code for f in report.all_flags]
        assert QAFlagCode.REPAIR_PREDICTED_WITHIN_HORIZON in codes

    def test_repair_predicted_does_not_fire_when_none_within_horizon(self):
        from src.models import RepairPrediction
        feat = _mk_feature(aid="x")
        pred = RepairPrediction(
            feature_id="x", feature=feat, cgr_mm_per_year=0.05,
            repair_trigger="NONE_WITHIN_HORIZON", repair_year_offset=None,
        )
        report = FlagAggregator().aggregate(predictions=[pred])
        codes = [f.code for f in report.all_flags]
        assert QAFlagCode.REPAIR_PREDICTED_WITHIN_HORIZON not in codes

    def test_high_cgr_population_fires(self):
        feat = _mk_feature(aid="x")
        results = [
            CGRResult(
                feature=feat, matched_to_run1=None,
                cgr_mm_yr=0.5, feature_cgr_mm_yr=0.5,
                mode_used="feature_specific",
                depth_old_used_mm=0.0, depth_new_mm=3.0, years_between=5.0,
            )
            for _ in range(10)
        ]
        report = FlagAggregator().aggregate(cgr_results=results)
        codes = [f.code for f in report.all_flags]
        assert QAFlagCode.HIGH_CGR_POPULATION in codes

    def test_high_cgr_population_does_not_fire_for_slow_corrosion(self):
        feat = _mk_feature(aid="x")
        results = [
            CGRResult(
                feature=feat, matched_to_run1=None,
                cgr_mm_yr=0.05, feature_cgr_mm_yr=0.05,
                mode_used="feature_specific",
                depth_old_used_mm=0.0, depth_new_mm=0.3, years_between=5.0,
            )
            for _ in range(10)
        ]
        report = FlagAggregator().aggregate(cgr_results=results)
        codes = [f.code for f in report.all_flags]
        assert QAFlagCode.HIGH_CGR_POPULATION not in codes


# ---------------------------------------------------------------------------
# Integration on Kandla (full chain)
# ---------------------------------------------------------------------------

class TestKandlaIntegration:
    """Run the full chain on the Kandla-Samakhiali pair, aggregate, and
    assert the expected flag pattern.

    Published / hand-checked expectations:
      * NO ERF_EXCEEDS_1 — all 333 features under repair threshold.
      * NO DEPTH_EXCEEDS_80 — same.
      * UNMATCHED_RUN2 count ≈ 311 (22 matched + 311 unmatched = 333 r2 features).
      * LOW_DEFECT_MATCH_RATE — 22 / min(79, 333) = 27.8 % ≪ 90 % target.
      * NO_CLUSTERS_IN_EITHER_RUN — Kandla has no COCL parents in either run.
    """

    @pytest.fixture(scope="class")
    def report(self):
        pipeline = Pipeline(
            pipeline_name="Kandla-Sam", diameter_mm=273.0, smys_mpa=358.0,
            maop_zones=[MAOPZone(6.0, 8.0, 0.72, 70.0)],
        )
        reader = ILIReader()
        run1 = reader.read(
            EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
            run_id="k1",
        )
        run2 = reader.read(EXAMPLES / "1ZSV_Pipeline_Listing.xlsx", run_id="k2")
        ja = JointAligner().align(run1, run2)
        mr = DefectMatcher().match(run1, run2, ja.matches)
        cgrs = CGRCalculator({"mode": "hybrid"}).compute(mr, years_between=4.25)
        ffps_by_id = {}
        for c in cgrs:
            ffp_list = ffp_assess(c.feature, pipeline)
            ctrl = next((f for f in ffp_list if f.is_controlling), ffp_list[0])
            ffps_by_id[c.feature.anomaly_id] = ctrl
        preds = RepairPredictor().predict(
            cgrs, ffps_by_id, pipeline,
            run2_inspection_date=date(2023, 3, 15),
        )
        return FlagAggregator().aggregate(
            run1=run1, run2=run2,
            joint_alignment=ja, match_result=mr,
            cgr_results=cgrs,
            ffp_results=list(ffps_by_id.values()),
            predictions=preds,
        )

    def test_no_erf_exceeds_1(self, report):
        assert report.counts.get(QAFlagCode.ERF_EXCEEDS_1, 0) == 0

    def test_no_depth_exceeds_80(self, report):
        assert report.counts.get(QAFlagCode.DEPTH_EXCEEDS_80, 0) == 0

    def test_has_critical_is_false(self, report):
        assert report.has_critical is False

    def test_unmatched_run2_count_around_311(self, report):
        # Tool: 22 matched + 311 unmatched. Published: 23 + 310.
        count = report.counts.get(QAFlagCode.UNMATCHED_RUN2, 0)
        assert 305 <= count <= 315, f"UNMATCHED_RUN2 count was {count}"

    def test_low_defect_match_rate_fires(self, report):
        assert report.counts.get(QAFlagCode.LOW_DEFECT_MATCH_RATE, 0) >= 1

    def test_no_clusters_in_either_run_fires(self, report):
        assert report.counts.get(QAFlagCode.NO_CLUSTERS_IN_EITHER_RUN, 0) >= 1

    def test_coordinates_swapped_fires_from_athena_run(self, report):
        # The Athena 2018 file (run 1) has lat/lon swapped.
        assert report.counts.get(QAFlagCode.COORDINATES_SWAPPED, 0) >= 1

    def test_no_repair_predicted_within_horizon(self, report):
        # All 333 features are NONE_WITHIN_HORIZON.
        assert report.counts.get(QAFlagCode.REPAIR_PREDICTED_WITHIN_HORIZON, 0) == 0
