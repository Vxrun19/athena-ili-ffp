"""Tests for src/io/format_converter/.

Coverage targets, per Prompt-16 spec:
  * Synthetic Rosen-style fixture round-trips through the converter and
    is then readable by the existing ILIReader.
  * Unit-conversion edge cases (mm depth, decimal clock, km chainage).
  * Value normalisation (I/E → INTERNAL/EXTERNAL).
  * VendorProfile.validate() catches missing required fields.
  * Profile save/load round-trip.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.io.format_converter import (
    CANONICAL_FIELDS,
    REQUIRED_CANONICAL_FIELDS,
    FormatConverter,
    VendorProfile,
    propose_profile,
)
from src.io.format_converter.unit_conversions import (
    chainage_to_m,
    clock_to_hh_mm,
    depth_to_pct_wt,
)


# ============================================================================
# Unit conversion helpers
# ============================================================================

class TestChainageConversion:
    def test_metres_pass_through(self):
        assert chainage_to_m(123.4, "m") == pytest.approx(123.4)

    def test_kilometres_scaled_to_metres(self):
        assert chainage_to_m(1.234, "km") == pytest.approx(1234.0)

    def test_feet_uses_nist_factor(self):
        # 100 ft × 0.3048 = 30.48 m
        assert chainage_to_m(100.0, "ft") == pytest.approx(30.48)

    def test_case_insensitive(self):
        assert chainage_to_m(1.0, "KM") == pytest.approx(1000.0)
        assert chainage_to_m(1.0, "Km") == pytest.approx(1000.0)

    def test_default_unit_is_metres(self):
        # source_unit=None falls back to "m"
        assert chainage_to_m(50.0, None) == pytest.approx(50.0)

    def test_string_value_parsed(self):
        # Real files sometimes hand us numeric strings.
        assert chainage_to_m("1.5", "km") == pytest.approx(1500.0)

    def test_string_with_thousands_separator(self):
        assert chainage_to_m("12,345.67", "m") == pytest.approx(12345.67)

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unrecognised source_unit"):
            chainage_to_m(1.0, "leagues")

    def test_nan_raises(self):
        with pytest.raises(ValueError):
            chainage_to_m(float("nan"), "m")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            chainage_to_m("   ", "m")


class TestDepthConversion:
    def test_percent_passes_through(self):
        assert depth_to_pct_wt(45.5, "%") == pytest.approx(45.5)

    def test_percent_default(self):
        assert depth_to_pct_wt(45.5, None) == pytest.approx(45.5)

    def test_mm_requires_wt(self):
        # 3.5 mm depth in a 8 mm wall = 43.75% WT
        assert depth_to_pct_wt(3.5, "mm", wt_mm=8.0) == pytest.approx(43.75)

    def test_mm_without_wt_raises(self):
        with pytest.raises(ValueError, match="requires wt_mm"):
            depth_to_pct_wt(3.5, "mm")

    def test_mm_with_zero_wt_raises(self):
        with pytest.raises(ValueError, match="wt_mm must be > 0"):
            depth_to_pct_wt(3.5, "mm", wt_mm=0.0)

    def test_fraction_scaled_to_percent(self):
        # 0.42 fraction = 42% WT
        assert depth_to_pct_wt(0.42, "fraction") == pytest.approx(42.0)

    def test_percent_marker_in_string_tolerated(self):
        # "28.5%" should parse to 28.5
        assert depth_to_pct_wt("28.5%", "%") == pytest.approx(28.5)

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unrecognised source_unit"):
            depth_to_pct_wt(3.5, "potatoes", wt_mm=8.0)


class TestClockConversion:
    def test_hh_mm_string_passes_through_normalised(self):
        assert clock_to_hh_mm("3:45", "hh:mm") == "03:45"
        assert clock_to_hh_mm("03:45", "hh:mm") == "03:45"

    def test_decimal_hours_to_hh_mm(self):
        # 6.5 decimal hours → 06:30
        assert clock_to_hh_mm(6.5, "decimal_hr") == "06:30"
        # 11.75 → 11:45
        assert clock_to_hh_mm(11.75, "decimal_hr") == "11:45"

    def test_12_normalised_to_00(self):
        assert clock_to_hh_mm("12:00", "hh:mm") == "00:00"
        # Decimal-hours 12.0 wraps around too.
        assert clock_to_hh_mm(12.0, "decimal_hr") == "00:00"

    def test_decimal_hours_wrap(self):
        # 12.5 in the decimal-hour space wraps to 00:30.
        assert clock_to_hh_mm(12.5, "decimal_hr") == "00:30"

    def test_degrees_to_hh_mm(self):
        # 90° = 3:00 (90 / 30 = 3.0 hours)
        assert clock_to_hh_mm(90.0, "degrees") == "03:00"
        # 180° = 6:00
        assert clock_to_hh_mm(180.0, "degrees") == "06:00"
        # 360° wraps to 00:00
        assert clock_to_hh_mm(360.0, "degrees") == "00:00"

    def test_radians_to_hh_mm(self):
        import math
        # π rad = 180° = 6:00
        assert clock_to_hh_mm(math.pi, "radians") == "06:00"
        # 2π = 12 hours wraps to 00:00
        assert clock_to_hh_mm(2 * math.pi, "radians") == "00:00"

    def test_dot_separator_treated_as_hh_mm_under_hh_mm_unit(self):
        # Some vendors write "5.14" meaning 5:14. The hh:mm regex
        # accepts a dot as separator, matching the column_synonyms.yaml
        # convention (`pattern: ^(\d{1,2})[:.](\d{2})...`).
        assert clock_to_hh_mm("3.45", "hh:mm") == "03:45"

    def test_bare_decimal_under_decimal_hr_unit(self):
        # Same string interpreted as decimal hours: 3.45 hr = 03:27.
        assert clock_to_hh_mm("3.45", "decimal_hr") == "03:27"

    def test_unparseable_string_raises(self):
        with pytest.raises(ValueError, match="cannot parse"):
            clock_to_hh_mm("not-a-time", "hh:mm")

    def test_minute_out_of_range_raises(self):
        with pytest.raises(ValueError, match="minute component"):
            clock_to_hh_mm("3:75", "hh:mm")


# ============================================================================
# VendorProfile — save/load + validate
# ============================================================================

class TestVendorProfileValidate:
    def _baseline_mappings(self) -> dict[str, str]:
        return {
            "abs_distance_m":   "Distance",
            "joint_number":     "Joint",
            "wt_mm":            "WT",
            "length_mm":        "Length",
            "width_mm":         "Width",
            "surface":          "Side",
            "depth_pct_wt":     "Depth",
        }

    def test_valid_profile_has_no_problems(self):
        p = VendorProfile(
            vendor_name="Test Vendor",
            column_mappings=self._baseline_mappings(),
        )
        assert p.validate() == []
        assert p.is_valid()

    def test_missing_required_field_reported(self):
        m = self._baseline_mappings()
        m.pop("length_mm")
        p = VendorProfile(vendor_name="Test", column_mappings=m)
        problems = p.validate()
        assert any("length_mm" in s for s in problems)
        assert not p.is_valid()

    def test_blank_vendor_name_reported(self):
        p = VendorProfile(
            vendor_name="",
            column_mappings=self._baseline_mappings(),
        )
        assert any("vendor_name" in s for s in p.validate())

    def test_depth_mm_satisfies_depth_requirement(self):
        m = self._baseline_mappings()
        m.pop("depth_pct_wt")
        m["depth_mm"] = "Depth [mm]"
        p = VendorProfile(vendor_name="Test", column_mappings=m)
        assert p.validate() == []

    def test_depth_mm_without_wt_reported(self):
        m = self._baseline_mappings()
        m.pop("depth_pct_wt")
        m.pop("wt_mm")
        m["depth_mm"] = "Depth [mm]"
        p = VendorProfile(vendor_name="Test", column_mappings=m)
        problems = p.validate()
        # Either the "missing wt_mm" or the "depth_mm without wt" message
        # should fire — both are correct callouts.
        assert any("wt_mm" in s for s in problems)

    def test_unknown_canonical_name_reported(self):
        m = self._baseline_mappings()
        m["not_a_real_field"] = "X"
        p = VendorProfile(vendor_name="Test", column_mappings=m)
        assert any("not_a_real_field" in s for s in p.validate())

    def test_aliases_are_accepted(self):
        # feature_id → anomaly_id, clock_orientation → clock_position.
        m = self._baseline_mappings()
        m["feature_id"] = "ID"
        m["clock_orientation"] = "Clock"
        p = VendorProfile(vendor_name="Test", column_mappings=m)
        # Aliases shouldn't show up as problems.
        problems = p.validate()
        assert all("feature_id" not in s for s in problems)
        # And the normalised mappings should rewrite them.
        canon_map = p.normalised_mappings()
        assert canon_map["anomaly_id"] == "ID"
        assert canon_map["clock_position"] == "Clock"


class TestVendorProfileRoundTrip:
    def test_save_then_load_preserves_fields(self, tmp_path: Path):
        original = VendorProfile(
            vendor_name="Round-trip Test",
            sheet_name="Defects",
            header_row=2,
            column_mappings={
                "abs_distance_m": "Distance, m",
                "joint_number":   "Joint",
                "wt_mm":          "WT",
                "depth_pct_wt":   "Depth",
                "length_mm":      "Length",
                "width_mm":       "Width",
                "surface":        "Side",
            },
            unit_conventions={"chainage": "m", "depth": "%"},
            value_normalizations={"surface": {"I": "internal", "E": "external"}},
            notes="Some free text\nwith a newline.",
        )
        path = tmp_path / "profile.json"
        original.save_to_json(path)
        loaded = VendorProfile.load_from_json(path)

        assert loaded.vendor_name == original.vendor_name
        assert loaded.sheet_name == original.sheet_name
        assert loaded.header_row == original.header_row
        assert loaded.column_mappings == original.column_mappings
        assert loaded.unit_conventions == original.unit_conventions
        assert loaded.value_normalizations == original.value_normalizations
        assert loaded.notes == original.notes

    def test_load_ignores_unknown_top_level_keys(self, tmp_path: Path):
        path = tmp_path / "profile.json"
        path.write_text(json.dumps({
            "vendor_name": "Future-proof",
            "column_mappings": {},
            "some_future_key": [1, 2, 3],
        }))
        # Should NOT raise on the unknown key.
        loaded = VendorProfile.load_from_json(path)
        assert loaded.vendor_name == "Future-proof"


# ============================================================================
# FormatConverter — synthetic Rosen-style fixture
# ============================================================================

def _build_rosen_style_xlsx(path: Path) -> None:
    """Write a 5-row 'Rosen 2018'-shape Excel file for the converter to chew on."""
    df = pd.DataFrame({
        "Anomaly Identification":   ["A-1", "A-2", "A-3", "A-4", "A-5"],
        "Distance to Reference Point": [10.0, 25.5, 100.0, 250.0, 500.0],  # m
        "Distance to Upstream Weld": [0.5, 1.0, 2.0, 3.5, 5.0],            # m
        "Joint Number":             [1, 1, 2, 3, 4],
        "Joint Length":             [12.0, 12.0, 12.0, 12.0, 12.0],
        "Wall thickness":           [8.0, 8.0, 6.0, 6.0, 8.0],             # mm
        "Depth":                    [25.0, 40.0, 55.0, 12.0, 80.5],        # % WT
        "Length, axial":            [50.0, 80.0, 120.0, 30.0, 200.0],      # mm
        "Width, circ":              [20.0, 25.0, 40.0, 15.0, 50.0],        # mm
        "Clock Position":           ["03:00", "06:30", "11:15", "09:00", "00:30"],
        "Surface":                  ["I", "E", "I", "E", "I"],
        "Feature Ident":            ["CORR", "CORR", "COCL", "CORR", "CORR"],
        "Feature Class":            ["PITT", "GENE", "GENE", "PITT", "AXGR"],
        "Description":              ["", "", "metal loss cluster", "", ""],
        "Latitude":                 [23.01, 23.01, 23.02, 23.03, 23.04],
        "Longitude":                [70.15, 70.16, 70.17, 70.18, 70.19],
    })
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Defects", index=False)


@pytest.fixture
def rosen_fixture_path(tmp_path: Path) -> Path:
    p = tmp_path / "rosen_sample.xlsx"
    _build_rosen_style_xlsx(p)
    return p


@pytest.fixture
def rosen_profile() -> VendorProfile:
    return VendorProfile.load_from_json(
        Path(__file__).resolve().parents[1]
        / "src" / "io" / "format_converter" / "profiles"
        / "rosen_2018.json"
    )


class TestFormatConverterTransform:
    def test_read_source_returns_clean_frame(self, rosen_fixture_path, rosen_profile):
        conv = FormatConverter(rosen_profile)
        df = conv.read_source(rosen_fixture_path)
        assert len(df) == 5
        # The fixture's 16 columns should all survive read_source.
        assert "Anomaly Identification" in df.columns
        assert "Surface" in df.columns

    def test_transform_renames_to_ngp_columns(self, rosen_fixture_path, rosen_profile):
        conv = FormatConverter(rosen_profile)
        df = conv.read_source(rosen_fixture_path)
        out = conv.transform(df)
        # NGP_OUTPUT_COLUMNS form.
        assert "Anomaly ID" in out.columns
        assert "Absolute Distance, m" in out.columns
        assert "WT, mm" in out.columns
        assert "Depth, %WT" in out.columns
        assert "Surface" in out.columns
        # Source columns shouldn't leak through.
        assert "Anomaly Identification" not in out.columns

    def test_transform_normalises_surface_markers(self, rosen_fixture_path, rosen_profile):
        conv = FormatConverter(rosen_profile)
        out = conv.transform(conv.read_source(rosen_fixture_path))
        surface = out["Surface"].tolist()
        # "I"/"E" → "internal"/"external" (from rosen_2018.json value_normalizations).
        assert surface == ["internal", "external", "internal", "external", "internal"]

    def test_transform_preserves_numeric_values(self, rosen_fixture_path, rosen_profile):
        conv = FormatConverter(rosen_profile)
        out = conv.transform(conv.read_source(rosen_fixture_path))
        # Depth column passed through unchanged (already %WT).
        assert out["Depth, %WT"].tolist() == [25.0, 40.0, 55.0, 12.0, 80.5]
        # Clock column already in hh:mm — passes through normalised.
        assert out["Clock Position"].tolist() == [
            "03:00", "06:30", "11:15", "09:00", "00:30",
        ]


class TestFormatConverterDepthMm:
    """Profile reports depth in mm (NDT Global-style); converter resolves to %WT."""

    def test_mm_depth_converted_to_pct_wt(self, tmp_path: Path):
        src = tmp_path / "ut_vendor.xlsx"
        df = pd.DataFrame({
            "Anomaly_ID":          ["A1", "A2"],
            "Chainage":            [100.0, 200.0],
            "Joint_Number":        [1, 2],
            "WT_Nominal":          [8.0, 10.0],
            "Depth":               [2.0, 4.0],     # mm
            "Length":              [50.0, 80.0],
            "Width":               [20.0, 30.0],
            "Surface":             ["Internal", "External"],
        })
        with pd.ExcelWriter(src, engine="openpyxl") as w:
            df.to_excel(w, sheet_name="Defects", index=False)

        profile = VendorProfile(
            vendor_name="UT vendor",
            sheet_name="Defects",
            header_row=0,
            column_mappings={
                "anomaly_id":   "Anomaly_ID",
                "abs_distance_m": "Chainage",
                "joint_number": "Joint_Number",
                "wt_mm":        "WT_Nominal",
                "depth_mm":     "Depth",
                "length_mm":    "Length",
                "width_mm":     "Width",
                "surface":      "Surface",
            },
            unit_conventions={"depth": "mm"},
            value_normalizations={"surface": {"Internal": "internal", "External": "external"}},
        )
        conv = FormatConverter(profile)
        out = conv.transform(conv.read_source(src))

        # 2 mm / 8 mm = 25.0 %WT; 4 mm / 10 mm = 40.0 %WT
        assert out["Depth, %WT"].tolist() == pytest.approx([25.0, 40.0])
        # Source depth_mm column should NOT leak through.
        assert "Depth, mm" not in out.columns


class TestFormatConverterChainageKm:
    """Profile reports chainage in km; converter scales to m."""

    def test_km_chainage_converted_to_m(self, tmp_path: Path):
        src = tmp_path / "km_vendor.xlsx"
        df = pd.DataFrame({
            "ID":         ["a", "b"],
            "Distance":   [0.100, 1.500],         # km
            "Joint":      [1, 2],
            "WT":         [8.0, 8.0],
            "Depth":      [20.0, 30.0],
            "Length":     [50.0, 80.0],
            "Width":      [20.0, 30.0],
            "Surface":    ["I", "E"],
        })
        with pd.ExcelWriter(src, engine="openpyxl") as w:
            df.to_excel(w, sheet_name="Anomalies", index=False)

        profile = VendorProfile(
            vendor_name="km vendor",
            sheet_name="Anomalies",
            header_row=0,
            column_mappings={
                "anomaly_id":     "ID",
                "abs_distance_m": "Distance",
                "joint_number":   "Joint",
                "wt_mm":          "WT",
                "depth_pct_wt":   "Depth",
                "length_mm":      "Length",
                "width_mm":       "Width",
                "surface":        "Surface",
            },
            unit_conventions={"chainage": "km"},
            value_normalizations={"surface": {"I": "internal", "E": "external"}},
        )
        out = FormatConverter(profile).transform(
            FormatConverter(profile).read_source(src)
        )
        assert out["Absolute Distance, m"].tolist() == pytest.approx([100.0, 1500.0])


class TestFormatConverterRoundTripWithReader:
    """The whole point: ILIReader must be able to read what we write."""

    def test_converted_file_consumable_by_reader(
        self, tmp_path: Path, rosen_fixture_path: Path, rosen_profile: VendorProfile,
    ):
        out_path = tmp_path / "converted.xlsx"
        FormatConverter(rosen_profile).convert(rosen_fixture_path, out_path)

        from src.io.ili_reader import ILIReader
        run = ILIReader().read(str(out_path), run_id="run_1")
        # All 5 rows survived the round-trip into Feature objects.
        # The reader may drop non-metal-loss rows or skip welds, so we
        # check it found a reasonable number (here: all 5 are CORR/COCL).
        assert len(run.features) >= 4, (
            f"Expected at least 4 features through the reader, got "
            f"{len(run.features)}"
        )

        # Spot-check that a non-trivial defect made it through with its
        # NGP-canonical fields populated.
        feat = run.features[0]
        assert feat.abs_distance_m is not None
        assert feat.depth_pct_wt is not None
        assert feat.wt_mm is not None


class TestMissingSourceColumnRaises:
    """Profile pointing at a column that's not in the file should fail loud."""

    def test_missing_source_column(self, tmp_path: Path):
        src = tmp_path / "incomplete.xlsx"
        df = pd.DataFrame({"Distance, m": [1.0, 2.0]})  # only 1 column
        with pd.ExcelWriter(src, engine="openpyxl") as w:
            df.to_excel(w, sheet_name="Defects", index=False)

        profile = VendorProfile(
            vendor_name="x",
            column_mappings={
                "abs_distance_m": "Distance, m",
                "joint_number":   "MissingColumn",
                "wt_mm":          "AlsoMissing",
                "depth_pct_wt":   "Depth",
                "length_mm":      "Length",
                "width_mm":       "Width",
                "surface":        "Side",
            },
        )
        conv = FormatConverter(profile)
        with pytest.raises(KeyError, match="aren't in the file"):
            conv.transform(conv.read_source(src))


# ============================================================================
# Auto-detect
# ============================================================================

class TestAutoDetect:
    def test_propose_profile_matches_rosen_columns(
        self, rosen_fixture_path: Path,
    ):
        """Rosen-style headers should be detected via the synonyms file."""
        proposal = propose_profile(rosen_fixture_path)
        assert proposal.profile.sheet_name == "Defects"
        assert proposal.profile.header_row == 0
        # At least the core fields should be detected.
        m = proposal.profile.column_mappings
        # 'Joint Number' is a direct synonym.
        assert m.get("joint_number") == "Joint Number"
        # 'Wall thickness' is a direct synonym for wt_mm.
        assert m.get("wt_mm") == "Wall thickness"
        # 'Surface' and 'Clock Position' are direct synonyms.
        assert m.get("surface") == "Surface"
        assert m.get("clock_position") == "Clock Position"
        # Confidence should be 1.0 for direct synonym hits.
        assert proposal.confidence.get("joint_number") == 1.0


class TestStarterProfilesLoad:
    """Every shipped JSON profile must load without exception."""

    @pytest.mark.parametrize("filename", [
        "rosen_2018.json",
        "baker_hughes_pii.json",
        "ndt_global.json",
        "onstream.json",
        "generic_template.json",
    ])
    def test_starter_profile_loads(self, filename):
        path = (
            Path(__file__).resolve().parents[1]
            / "src" / "io" / "format_converter" / "profiles" / filename
        )
        assert path.exists(), f"missing starter profile: {filename}"
        profile = VendorProfile.load_from_json(path)
        assert profile.vendor_name
        # The generic template has empty mappings — every other shipped
        # profile must at least name a sheet.
        if filename != "generic_template.json":
            assert profile.sheet_name
