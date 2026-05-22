"""Tests for src/models — enums, dataclass validation, unit conversions, parsers."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.models import (
    CGRMode,
    DimensionClass,
    FFPMethod,
    Feature,
    FeatureIdentification,
    Joint,
    MAOPZone,
    Pipeline,
    Project,
    Surface,
)
from src.models.units import (
    bar_to_kgcm2,
    kgcm2_to_bar,
    kgcm2_to_mpa,
    kgcm2_to_psi,
    mpa_to_kgcm2,
    parse_clock,
    parse_depth,
    parse_surface,
    psi_to_kgcm2,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestEnums:
    def test_dimension_class_pof_codes(self):
        # POF 110 codes the user called out.
        assert DimensionClass.GENERAL.value == "GENE"
        assert DimensionClass.PITTING.value == "PITT"
        assert DimensionClass.PINHOLE.value == "PINH"
        assert DimensionClass.AXIAL_SLOTTING.value == "AXSL"
        assert DimensionClass.AXIAL_GROOVING.value == "AXGR"
        assert DimensionClass.CIRCUMFERENTIAL_SLOTTING.value == "CISL"
        assert DimensionClass.CIRCUMFERENTIAL_GROOVING.value == "CIGR"

    def test_dimension_class_is_circumferential(self):
        assert DimensionClass.CIRCUMFERENTIAL_GROOVING.is_circumferential
        assert DimensionClass.CIRCUMFERENTIAL_SLOTTING.is_circumferential
        assert not DimensionClass.PITTING.is_circumferential
        assert not DimensionClass.GENERAL.is_circumferential

    def test_feature_identification_pof_codes(self):
        assert FeatureIdentification.CORROSION.value == "CORR"
        assert FeatureIdentification.CORROSION_CLUSTER.value == "COCL"
        assert FeatureIdentification.DENT.value == "DENT"

    def test_ffp_method_members(self):
        # All five methods the tool must implement.
        names = {m.name for m in FFPMethod}
        assert names == {
            "B31G_ORIGINAL",
            "B31G_MODIFIED",
            "RSTRENG",
            "DNV_RP_F101",
            "KASTNER",
        }

    def test_cgr_mode_members(self):
        assert {m.value for m in CGRMode} == {
            "feature_specific",
            "hybrid",
            "population_only",
        }


# ---------------------------------------------------------------------------
# parse_surface
# ---------------------------------------------------------------------------

class TestParseSurface:
    @pytest.mark.parametrize(
        "raw",
        ["INT", "int", "int.", "Internal", "internal", "INTERNAL", "I", "inner", "Inside"],
    )
    def test_internal_variants(self, raw):
        assert parse_surface(raw) is Surface.INTERNAL

    @pytest.mark.parametrize(
        "raw",
        ["EXT", "ext", "ext.", "External", "external", "EXTERNAL", "outer", "OUT"],
    )
    def test_external_variants(self, raw):
        assert parse_surface(raw) is Surface.EXTERNAL

    @pytest.mark.parametrize("raw", ["MID", "midwall", "MW", "mid-wall"])
    def test_midwall_variants(self, raw):
        assert parse_surface(raw) is Surface.MIDWALL

    @pytest.mark.parametrize("raw", [None, "", "  ", "n/a", "N/A", "undefined", "?", "wat"])
    def test_unknown_variants(self, raw):
        assert parse_surface(raw) is Surface.UNKNOWN

    def test_surface_enum_passthrough(self):
        assert parse_surface(Surface.INTERNAL) is Surface.INTERNAL


# ---------------------------------------------------------------------------
# parse_clock
# ---------------------------------------------------------------------------

class TestParseClock:
    def test_hh_mm_ss_string(self):
        assert parse_clock("06:14:00") == pytest.approx(6 + 14 / 60.0)

    def test_hh_mm_string(self):
        assert parse_clock("6:14") == pytest.approx(6 + 14 / 60.0)
        assert parse_clock("06:14") == pytest.approx(6 + 14 / 60.0)

    def test_dot_separator(self):
        # Some vendors use "6.14" meaning 6 hours 14 minutes (NOT 6.14 decimal hours).
        assert parse_clock("6.14") == pytest.approx(6 + 14 / 60.0)

    def test_decimal_hours_numeric(self):
        assert parse_clock(6.233) == pytest.approx(6.233)
        assert parse_clock(6) == pytest.approx(6.0)

    def test_oclock_string(self):
        assert parse_clock("6 o'clock") == pytest.approx(6.0)
        assert parse_clock("6 oclock") == pytest.approx(6.0)
        assert parse_clock("6h") == pytest.approx(6.0)
        assert parse_clock("06") == pytest.approx(6.0)

    def test_degrees_branch(self):
        # >12 and <=360 -> divide by 30 to get hours.
        assert parse_clock(180.0) == pytest.approx(6.0)
        assert parse_clock(90) == pytest.approx(3.0)

    def test_wrap_12_to_0(self):
        # 12:00 on a clock face == 0:00.
        assert parse_clock("12:00") == 0.0
        assert parse_clock(12.0) == 0.0
        assert parse_clock(360.0) == 0.0

    def test_none_and_blank(self):
        assert parse_clock(None) is None
        assert parse_clock("") is None
        assert parse_clock("n/a") is None
        assert parse_clock("-") is None

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_clock("not a clock")
        with pytest.raises(ValueError):
            parse_clock(400.0)
        with pytest.raises(ValueError):
            parse_clock("13:75")  # minutes out of range

    # --- Locked convention: strings use hh:mm; numerics are decimal hours. ---

    def test_string_dot_is_hh_mm(self):
        # "6.14" is a string -> hh:mm -> 6h 14m.
        assert parse_clock("6.14") == pytest.approx(6 + 14 / 60.0)

    def test_numeric_dot_is_decimal_hours(self):
        # 6.14 is a float -> decimal hours, NOT 6h 14m.
        assert parse_clock(6.14) == pytest.approx(6.14)

    def test_string_colon_six_fourteen(self):
        assert parse_clock("6:14") == pytest.approx(6 + 14 / 60.0)

    def test_string_colon_with_seconds(self):
        assert parse_clock("6:14:00") == pytest.approx(6 + 14 / 60.0)

    def test_string_zero_padded(self):
        assert parse_clock("06:14") == pytest.approx(6 + 14 / 60.0)

    def test_string_and_numeric_dot_disagree(self):
        # Sanity-check the convention itself: same digits, different meanings.
        assert parse_clock("6.14") != pytest.approx(parse_clock(6.14))


# ---------------------------------------------------------------------------
# parse_depth
# ---------------------------------------------------------------------------

class TestParseDepth:
    def test_percent_string(self):
        pct, mm = parse_depth("28.5%", wt_mm=10.0)
        assert pct == pytest.approx(28.5)
        assert mm == pytest.approx(2.85)

    def test_bare_string_numeric_is_percent(self):
        # No "%" sign but value >= 1.0 -> percent.
        pct, mm = parse_depth("28.5", wt_mm=10.0)
        assert pct == pytest.approx(28.5)
        assert mm == pytest.approx(2.85)

    def test_numeric_percent(self):
        pct, mm = parse_depth(28.5, wt_mm=10.0)
        assert pct == pytest.approx(28.5)
        assert mm == pytest.approx(2.85)

    def test_numeric_fraction_below_one(self):
        # Bare value in (0, 1) is interpreted as fraction.
        pct, mm = parse_depth(0.285, wt_mm=10.0)
        assert pct == pytest.approx(28.5)
        assert mm == pytest.approx(2.85)

    def test_zero(self):
        pct, mm = parse_depth(0.0, wt_mm=10.0)
        assert pct == 0.0
        assert mm == 0.0

    def test_none(self):
        assert parse_depth(None, wt_mm=10.0) == (None, None)
        assert parse_depth("", wt_mm=10.0) == (None, None)
        assert parse_depth("n/a", wt_mm=10.0) == (None, None)

    def test_no_wt_yields_no_mm(self):
        pct, mm = parse_depth(28.5, wt_mm=None)
        assert pct == pytest.approx(28.5)
        assert mm is None

    def test_percent_string_without_wt(self):
        pct, mm = parse_depth("28.5%", wt_mm=None)
        assert pct == pytest.approx(28.5)
        assert mm is None

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError):
            parse_depth(150.0, wt_mm=10.0)
        with pytest.raises(ValueError):
            parse_depth("-5", wt_mm=10.0)

    # --- allow_fraction flag (v0.3.4 dent %OD fix) --------------------------

    def test_allow_fraction_false_keeps_sub_one_literal(self):
        """Dent %OD path: a bare 0.53 must stay 0.53, NOT become 53.

        Regression for the 1YCP dent-depth bug — parse_depth's
        fraction rule turned a 0.53 %OD dent into 53, which the
        %OD->mm conversion then inflated to a ~240 mm depth.
        """
        pct, _mm = parse_depth(0.53, wt_mm=6.35, allow_fraction=False)
        assert pct == pytest.approx(0.53)

    def test_allow_fraction_true_is_default_unchanged(self):
        """Default (metal-loss) behaviour: 0.285 -> 28.5 % still holds."""
        pct, _mm = parse_depth(0.285, wt_mm=10.0)
        assert pct == pytest.approx(28.5)
        pct2, _mm2 = parse_depth(0.285, wt_mm=10.0, allow_fraction=True)
        assert pct2 == pytest.approx(28.5)

    def test_allow_fraction_false_above_one_unaffected(self):
        """allow_fraction only governs the (0,1) branch; >=1 is already
        a literal percent and stays so."""
        pct, _mm = parse_depth(28.5, wt_mm=10.0, allow_fraction=False)
        assert pct == pytest.approx(28.5)

    def test_percent_marked_string_ignores_allow_fraction(self):
        """An explicit '%' is always literal, regardless of the flag."""
        pct, _mm = parse_depth("0.53%", wt_mm=6.35, allow_fraction=True)
        assert pct == pytest.approx(0.53)


# ---------------------------------------------------------------------------
# Pressure round-trips
# ---------------------------------------------------------------------------

class TestPressureConversions:
    def test_bar_round_trip(self):
        assert kgcm2_to_bar(bar_to_kgcm2(50.0)) == pytest.approx(50.0)

    def test_psi_round_trip(self):
        assert kgcm2_to_psi(psi_to_kgcm2(725.0)) == pytest.approx(725.0)

    def test_mpa_round_trip(self):
        assert kgcm2_to_mpa(mpa_to_kgcm2(7.0)) == pytest.approx(7.0)

    def test_known_value_bar(self):
        # 1 bar = 1.0197162... kg/cm² (exact, anchored on kgf = 9.80665 N).
        assert bar_to_kgcm2(1.0) == pytest.approx(1.0197162129779283)

    def test_known_value_mpa(self):
        # 1 MPa = 10.197162... kg/cm².
        assert mpa_to_kgcm2(1.0) == pytest.approx(10.197162129779283)

    def test_known_value_psi(self):
        # 14.5037738 psi ~ 1.0197 kg/cm² (since 1 bar ~ 14.504 psi).
        assert psi_to_kgcm2(14.5037738) == pytest.approx(1.01971621, rel=1e-6)

    def test_cross_consistency(self):
        # 100 bar -> kg/cm² -> MPa -> kg/cm² -> bar should round-trip.
        kgcm2 = bar_to_kgcm2(100.0)
        mpa = kgcm2_to_mpa(kgcm2)
        kgcm2_back = mpa_to_kgcm2(mpa)
        bar_back = kgcm2_to_bar(kgcm2_back)
        assert bar_back == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Feature validation
# ---------------------------------------------------------------------------

class TestFeatureValidation:
    def test_valid_feature_constructs(self):
        f = Feature(
            anomaly_id="A001",
            source_run="run_1",
            abs_distance_m=1234.5,
            depth_pct_wt=28.5,
            wt_mm=10.0,
            clock_decimal_hours=6.5,
            latitude=22.5,
            longitude=72.1,
        )
        assert f.depth_mm == pytest.approx(2.85)

    def test_depth_above_100_rejected(self):
        with pytest.raises(ValueError, match="depth_pct_wt"):
            Feature(anomaly_id="A1", source_run="r1", depth_pct_wt=150.0)

    def test_depth_below_0_rejected(self):
        with pytest.raises(ValueError, match="depth_pct_wt"):
            Feature(anomaly_id="A1", source_run="r1", depth_pct_wt=-0.1)

    def test_clock_12_rejected(self):
        # 12.0 is upper-exclusive (clock face wraps to 0).
        with pytest.raises(ValueError, match="clock_decimal_hours"):
            Feature(anomaly_id="A1", source_run="r1", clock_decimal_hours=12.0)

    def test_clock_negative_rejected(self):
        with pytest.raises(ValueError, match="clock_decimal_hours"):
            Feature(anomaly_id="A1", source_run="r1", clock_decimal_hours=-0.5)

    def test_clock_zero_accepted(self):
        f = Feature(anomaly_id="A1", source_run="r1", clock_decimal_hours=0.0)
        assert f.clock_decimal_hours == 0.0

    def test_latitude_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="latitude"):
            Feature(anomaly_id="A1", source_run="r1", latitude=95.0)
        with pytest.raises(ValueError, match="latitude"):
            Feature(anomaly_id="A1", source_run="r1", latitude=-91.0)

    def test_longitude_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="longitude"):
            Feature(anomaly_id="A1", source_run="r1", longitude=200.0)

    def test_wt_must_be_positive(self):
        with pytest.raises(ValueError, match="wt_mm"):
            Feature(anomaly_id="A1", source_run="r1", wt_mm=0.0)

    def test_depth_mm_property_with_no_wt(self):
        f = Feature(anomaly_id="A1", source_run="r1", depth_pct_wt=28.5, wt_mm=None)
        assert f.depth_mm is None

    def test_depth_mm_property_with_no_depth(self):
        f = Feature(anomaly_id="A1", source_run="r1", depth_pct_wt=None, wt_mm=10.0)
        assert f.depth_mm is None


# ---------------------------------------------------------------------------
# MAOPZone / Pipeline / Joint
# ---------------------------------------------------------------------------

class TestPipelineDataclasses:
    def test_maop_zone_contains(self):
        z = MAOPZone(wt_mm_min=6.4, wt_mm_max=7.1, design_factor=0.72, maop_kgcm2=70.0)
        assert z.contains(6.4)
        assert z.contains(7.1)
        assert z.contains(6.8)
        assert not z.contains(6.3)
        assert not z.contains(7.2)

    def test_pipeline_maop_for_wt_exact(self):
        p = Pipeline(
            maop_zones=[
                MAOPZone(8.7, 9.5, 0.72, 96.7),
                MAOPZone(10.3, 11.1, 0.60, 84.1),
                MAOPZone(11.9, 14.3, 0.50, 80.6),
            ]
        )
        assert p.maop_for_wt(9.0).maop_kgcm2 == 96.7
        assert p.maop_for_wt(10.5).maop_kgcm2 == 84.1
        assert p.maop_for_wt(12.0).maop_kgcm2 == 80.6

    def test_pipeline_maop_for_wt_fallback_to_nearest(self):
        # 9.8 falls in a gap; should fall back to the nearest zone.
        p = Pipeline(
            maop_zones=[
                MAOPZone(8.7, 9.5, 0.72, 96.7),
                MAOPZone(10.3, 11.1, 0.60, 84.1),
            ]
        )
        # 9.8 is 0.3 above 9.5 and 0.5 below 10.3 -> nearest is zone 1.
        assert p.maop_for_wt(9.8).maop_kgcm2 == 96.7

    def test_pipeline_no_zones_returns_none(self):
        assert Pipeline().maop_for_wt(10.0) is None

    def test_joint_end_distance(self):
        j = Joint(joint_number=12, abs_distance_start_m=100.0, length_m=11.7, wt_mm=9.5)
        assert j.abs_distance_end_m == pytest.approx(111.7)


# ---------------------------------------------------------------------------
# Project.from_yaml
# ---------------------------------------------------------------------------

class TestProjectFromYaml:
    def test_loads_default_yaml(self):
        cfg_path = Path(__file__).resolve().parents[1] / "config" / "default_project.yaml"
        assert cfg_path.exists(), f"missing config file: {cfg_path}"
        proj = Project.from_yaml(str(cfg_path))
        # SMYS auto-derived from grade lookup (yaml has grade "API 5L X70" -> 482).
        assert proj.pipeline.material_grade == "API 5L X70"
        assert proj.pipeline.smys_mpa == 482
        # Default config has no zones populated.
        assert proj.pipeline.maop_zones == []
        # Raw config retained for downstream engines.
        assert "cgr" in proj.config
        assert proj.config["cgr"]["mode"] == "feature_specific"
