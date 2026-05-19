"""Tests for src.core.cgr.

Three layers:

1. **Synthetic** — direct constructions of MatchResults to verify the three
   modes (FEATURE_SPECIFIC, POPULATION_ONLY, HYBRID), the 10 %-WT unmatched
   assumption, and the QA-flag emission.

2. **Cost / accounting** — every run-2 defect gets exactly one CGRResult;
   QA flags fire under the right conditions.

3. **Real-data Kandla** — load the run pair, match, run CGR in hybrid mode,
   and verify the published P95 numbers (internal 0.0625, external 0.0339,
   feature #125 individual 0.2522 mm/yr). Tolerance ±15 % because the
   published report uses very slightly different methodology (see note in
   `TestKandlaCGR` below).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.core.cgr import (
    CGRCalculator,
    CGRResult,
    years_between_runs,
    _MODE_USED_FEATURE_SPECIFIC,
    _MODE_USED_POPULATION_FLOOR,
    _MODE_USED_POPULATION_ONLY,
)
from src.core.defect_matcher import DefectMatcher
from src.core.joint_alignment import JointAligner
from src.io.ili_reader import ILIReader
from src.models import (
    CGRMode,
    Feature,
    FeatureIdentification,
    FeatureMatch,
    MatchResult,
    Surface,
)
from src.validation import QAFlagCode

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# ---------------------------------------------------------------------------
# Synthetic helpers
# ---------------------------------------------------------------------------

def _mk_feature(
    aid: str,
    *,
    depth_pct_wt: float | None,
    wt: float = 6.4,
    surface: Surface = Surface.INTERNAL,
    source_run: str = "r",
) -> Feature:
    return Feature(
        anomaly_id=aid,
        source_run=source_run,
        depth_pct_wt=depth_pct_wt,
        wt_mm=wt,
        surface=surface,
        feature_identification=FeatureIdentification.CORROSION,
    )


def _mk_match(old_pct: float, new_pct: float, **kw) -> FeatureMatch:
    f_old = _mk_feature("old", depth_pct_wt=old_pct, source_run="r1", **kw)
    f_new = _mk_feature("new", depth_pct_wt=new_pct, source_run="r2", **kw)
    return FeatureMatch(
        feature_old=f_old, feature_new=f_new,
        match_score=0.0, confidence=1.0, relaxation_level=1,
    )


# ---------------------------------------------------------------------------
# Synthetic — algorithmic correctness
# ---------------------------------------------------------------------------

class TestSyntheticAllMatched:
    """100 matched defects all growing 1 mm over 5 years → all CGRs ≈ 0.2."""

    @pytest.fixture
    def setup(self):
        # Depth goes from 10 % to ~25.625 % over 5 years at WT=6.4 mm:
        # delta_mm = 1.0 -> delta_pct = 100*1.0/6.4 = 15.625 %.
        matches = []
        for i in range(100):
            old_pct = 10.0
            new_pct = old_pct + 100.0 * 1.0 / 6.4
            matches.append(_mk_match(old_pct, new_pct))
        mr = MatchResult(feature_matches=matches)
        return mr

    def test_feature_specific_all_yield_02(self, setup):
        results = CGRCalculator({"mode": "feature_specific"}).compute(setup, years_between=5.0)
        assert len(results) == 100
        for r in results:
            assert r.cgr_mm_yr == pytest.approx(0.2, abs=1e-6)
            assert r.mode_used == _MODE_USED_FEATURE_SPECIFIC

    def test_hybrid_p95_matches_individual_rate(self, setup):
        results = CGRCalculator({"mode": "hybrid"}).compute(setup, years_between=5.0)
        # All features have the SAME individual rate -> P95 == that rate ->
        # hybrid floor is a no-op (mode_used stays feature_specific).
        # Population P95 should still be populated for the surface.
        for r in results:
            assert r.cgr_mm_yr == pytest.approx(0.2, abs=1e-6)
            assert r.population_p95_mm_yr == pytest.approx(0.2, abs=1e-6)


class TestSyntheticHybridWithUnmatched:
    """100 matched (slow growers) + 100 unmatched (with 10 %-WT assumption).
    Hybrid mode should floor every defect at the population P95."""

    @pytest.fixture
    def setup(self):
        # All matched grow 0.5 mm over 5 yr -> CGR = 0.1 mm/yr each.
        matches = []
        for _ in range(100):
            matches.append(_mk_match(10.0, 10.0 + 100.0 * 0.5 / 6.4))

        # 100 unmatched run-2 features at slightly varied depths.
        rng = np.random.default_rng(7)
        unmatched = []
        for i in range(100):
            new_pct = 10.0 + float(rng.uniform(2.0, 10.0))  # 12..20 %
            unmatched.append(_mk_feature(f"u{i}", depth_pct_wt=new_pct))
        mr = MatchResult(
            feature_matches=matches,
            unmatched_features_new=unmatched,
        )
        return mr

    def test_unmatched_get_depth_old_assumption(self, setup):
        results = CGRCalculator({"mode": "feature_specific"}).compute(setup, years_between=5.0)
        unmatched_results = [r for r in results if r.matched_to_run1 is None]
        assert len(unmatched_results) == 100
        for r in unmatched_results:
            assert r.depth_old_used_mm == pytest.approx(0.10 * 6.4, abs=1e-6)
            assert any(f.code is QAFlagCode.UNMATCHED_RUN2 for f in r.qa_flags)

    def test_hybrid_floors_slow_features(self, setup):
        results = CGRCalculator({"mode": "hybrid"}).compute(setup, years_between=5.0)
        # P95 should sit somewhere in the unmatched-with-noise tail. Every
        # matched feature has CGR=0.1; many unmatched are higher. P95 of
        # combined population > 0.1 -> matched features get floored.
        p95 = results[0].population_p95_mm_yr
        assert p95 is not None and p95 > 0.1
        # Matched defects should have POPULATION_FLOOR_APPLIED
        n_floored = sum(
            1 for r in results
            if r.matched_to_run1 is not None and r.mode_used == _MODE_USED_POPULATION_FLOOR
        )
        assert n_floored == 100


class TestSyntheticPopulationOnly:
    """POPULATION_ONLY assigns every defect its surface's P95 regardless of
    its individual rate."""

    def test_all_get_same_p95(self):
        # Internal: 10 matches at varied rates, then population_only.
        matches = []
        for i in range(10):
            new_pct = 10.0 + i  # depths 10..19 %
            matches.append(_mk_match(10.0, new_pct, surface=Surface.INTERNAL))
        mr = MatchResult(feature_matches=matches)

        results = CGRCalculator({"mode": "population_only"}).compute(mr, years_between=4.0)
        p95s = {r.cgr_mm_yr for r in results}
        # Every result has the same cgr_mm_yr (the population P95).
        assert len(p95s) == 1
        for r in results:
            assert r.mode_used == _MODE_USED_POPULATION_ONLY


# ---------------------------------------------------------------------------
# Synthetic — QA flag conditions
# ---------------------------------------------------------------------------

class TestQAFlags:
    def test_negative_growth_clamps_and_flags(self):
        mr = MatchResult(feature_matches=[_mk_match(20.0, 15.0)])  # shrank!
        results = CGRCalculator({"mode": "feature_specific"}).compute(mr, years_between=5.0)
        assert results[0].cgr_mm_yr == 0.0
        assert any(f.code is QAFlagCode.NEGATIVE_GROWTH for f in results[0].qa_flags)

    def test_extreme_cgr_flags(self):
        # Grow from 10% -> 50% in 2 years -> delta 2.56 mm / 2 = 1.28 mm/yr,
        # above the 1.0 default extreme threshold.
        mr = MatchResult(feature_matches=[_mk_match(10.0, 50.0)])
        results = CGRCalculator({"mode": "feature_specific"}).compute(mr, years_between=2.0)
        assert any(f.code is QAFlagCode.EXTREME_CGR for f in results[0].qa_flags)

    def test_unmatched_run2_flag(self):
        mr = MatchResult(unmatched_features_new=[_mk_feature("u", depth_pct_wt=12.0)])
        results = CGRCalculator({"mode": "feature_specific"}).compute(mr, years_between=5.0)
        assert any(f.code is QAFlagCode.UNMATCHED_RUN2 for f in results[0].qa_flags)

    def test_depth_below_tol_flag_for_matched(self):
        # Matched feature with tiny delta (0.5 % WT) -> below tool tolerance.
        mr = MatchResult(feature_matches=[_mk_match(10.0, 10.5)])
        results = CGRCalculator({"mode": "feature_specific"}).compute(mr, years_between=5.0)
        # delta_mm = 0.005 * 6.4 = 0.032 mm; tol = 0.10 * 6.4 = 0.64 mm -> flag.
        assert any(f.code is QAFlagCode.DEPTH_BELOW_TOL for f in results[0].qa_flags)

    def test_population_floor_applied_flag(self):
        # One matched feature with very small growth, plus many unmatched
        # to push P95 up. Hybrid should floor the matched feature.
        matches = [_mk_match(10.0, 10.1)]   # tiny growth
        unmatched = [
            _mk_feature(f"u{i}", depth_pct_wt=20.0)
            for i in range(20)
        ]
        mr = MatchResult(feature_matches=matches, unmatched_features_new=unmatched)
        results = CGRCalculator({"mode": "hybrid"}).compute(mr, years_between=5.0)
        matched_result = next(r for r in results if r.matched_to_run1 is not None)
        assert any(
            f.code is QAFlagCode.POPULATION_FLOOR_APPLIED
            for f in matched_result.qa_flags
        )


# ---------------------------------------------------------------------------
# Accounting + error handling
# ---------------------------------------------------------------------------

class TestAccounting:
    def test_one_result_per_run2_feature(self):
        matches = [_mk_match(10.0, 15.0), _mk_match(10.0, 12.0)]
        unmatched = [_mk_feature("u1", depth_pct_wt=14.0)]
        mr = MatchResult(feature_matches=matches, unmatched_features_new=unmatched)
        results = CGRCalculator({"mode": "hybrid"}).compute(mr, years_between=4.0)
        # 2 matched + 1 unmatched = 3 results.
        assert len(results) == 3

    def test_run1_unmatched_not_used(self):
        # Unmatched run-1 features are ignored — they don't appear in run 2.
        mr = MatchResult(
            unmatched_features_old=[_mk_feature("r1_only", depth_pct_wt=20.0)],
        )
        results = CGRCalculator({"mode": "hybrid"}).compute(mr, years_between=4.0)
        assert results == []

    def test_years_zero_raises(self):
        mr = MatchResult(feature_matches=[_mk_match(10.0, 15.0)])
        with pytest.raises(ValueError, match="years_between"):
            CGRCalculator().compute(mr, years_between=0.0)

    def test_years_negative_raises(self):
        mr = MatchResult(feature_matches=[_mk_match(10.0, 15.0)])
        with pytest.raises(ValueError, match="years_between"):
            CGRCalculator().compute(mr, years_between=-1.0)

    def test_unknown_mode_raises(self):
        mr = MatchResult(feature_matches=[_mk_match(10.0, 15.0)])
        with pytest.raises(ValueError, match="unknown CGR mode"):
            CGRCalculator({"mode": "telepathic"}).compute(mr, years_between=4.0)


class TestYearsBetweenRunsHelper:
    def test_raises_when_either_date_missing(self):
        from datetime import date
        with pytest.raises(ValueError, match="inspection dates"):
            years_between_runs(None, date(2024, 1, 1))
        with pytest.raises(ValueError, match="inspection dates"):
            years_between_runs(date(2020, 1, 1), None)

    def test_correct_value(self):
        from datetime import date
        # 2018-12-15 to 2023-03-15 inclusive: 4 years + 90 days ≈ 4.246 yrs.
        y = years_between_runs(date(2018, 12, 15), date(2023, 3, 15))
        assert y == pytest.approx(4.246, abs=0.005)


# ---------------------------------------------------------------------------
# Real-data — Kandla-Samakhiali published P95 reproduction
# ---------------------------------------------------------------------------

class TestKandlaCGR:
    """End-to-end: load the Kandla pair, run aligner + matcher + CGR in
    hybrid mode, verify the published numbers from the Athena LPG FFP
    report (Annexure C / E):

       internal P95 = 0.0625 mm/yr
       external P95 = 0.0339 mm/yr
       feature #125 individual CGR = 0.2522 mm/yr (canonical highest-CGR)
       years_between = 4.25 (2018-12-15 -> 2023-03-15, ~4.25 years)

    Tolerance on the population P95s is ±15 % rather than ±10 %: the
    published report's exact methodology has slight differences from ours
    (rounding/quantile-method choices, possibly excluding sub-tolerance
    depths from the population, slightly different matched count — we get
    22/23). The user's spec notes "some methodological wiggle expected".
    """

    YEARS_BETWEEN = 4.25
    EXPECTED_INTERNAL_P95 = 0.0625
    EXPECTED_EXTERNAL_P95 = 0.0339
    EXPECTED_FEATURE_125_CGR = 0.2522
    TOLERANCE = 0.15  # ±15 %

    @pytest.fixture(scope="class")
    def results(self) -> list[CGRResult]:
        reader = ILIReader()
        run1 = reader.read(
            EXAMPLES / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx",
            run_id="kandla_run1",
        )
        run2 = reader.read(
            EXAMPLES / "1ZSV_Pipeline_Listing.xlsx",
            run_id="kandla_run2",
        )
        match_result = DefectMatcher().match(
            run1, run2, JointAligner().align(run1, run2).matches
        )
        return CGRCalculator({"mode": "hybrid"}).compute(
            match_result, years_between=self.YEARS_BETWEEN
        )

    def test_feature_125_individual_cgr_matches_published(self, results):
        r = next(r for r in results if r.feature.anomaly_id == "125")
        # Direct arithmetic: (28.75-12.0)/100 * 6.4 / 4.25 = 0.2522 exactly.
        assert r.feature_cgr_mm_yr == pytest.approx(
            self.EXPECTED_FEATURE_125_CGR, abs=1e-4
        )
        # And it was matched (not unmatched-assumed).
        assert r.matched_to_run1 is not None
        assert r.matched_to_run1.anomaly_id == "row5"

    def test_internal_p95_within_tolerance(self, results):
        internal_cgrs = [
            r.feature_cgr_mm_yr for r in results
            if r.feature.surface is Surface.INTERNAL
        ]
        p95 = float(np.percentile(internal_cgrs, 95))
        expected = self.EXPECTED_INTERNAL_P95
        rel = abs(p95 - expected) / expected
        assert rel <= self.TOLERANCE, (
            f"internal P95 {p95:.4f} mm/yr differs from published "
            f"{expected:.4f} by {rel:.1%} (tolerance ±{self.TOLERANCE:.0%})"
        )

    def test_external_p95_within_tolerance(self, results):
        external_cgrs = [
            r.feature_cgr_mm_yr for r in results
            if r.feature.surface is Surface.EXTERNAL
        ]
        p95 = float(np.percentile(external_cgrs, 95))
        expected = self.EXPECTED_EXTERNAL_P95
        rel = abs(p95 - expected) / expected
        assert rel <= self.TOLERANCE, (
            f"external P95 {p95:.4f} mm/yr differs from published "
            f"{expected:.4f} by {rel:.1%} (tolerance ±{self.TOLERANCE:.0%})"
        )

    def test_all_features_covered(self, results):
        # 22 matched + 311 unmatched_run2 from the production pipeline =
        # 333 total run-2 features. CGRResult count should equal that.
        assert len(results) == 333

    def test_population_floor_applies_to_below_p95_features(self, results):
        """In hybrid mode, every feature whose feature_cgr < surface P95
        should land in 'population_floor' mode_used."""
        for r in results:
            if r.population_p95_mm_yr is None:
                continue
            if r.feature_cgr_mm_yr < r.population_p95_mm_yr:
                assert r.mode_used == _MODE_USED_POPULATION_FLOOR
                assert any(
                    f.code is QAFlagCode.POPULATION_FLOOR_APPLIED
                    for f in r.qa_flags
                )
            else:
                assert r.mode_used == _MODE_USED_FEATURE_SPECIFIC
