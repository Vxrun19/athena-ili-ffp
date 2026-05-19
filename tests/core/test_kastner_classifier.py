"""Tests for v0.3.3 Kastner-eligibility classifier.

Surfaced during the BPCL Mathura-Piyala 1ZYT customer validation: the
engine's estimated_erf_circ topic produced 0 features for a dataset
that the reference deliverable assesses with 325 Kastner features.
The fix is a multi-signal classifier (POF enum authoritative; raw-
description substring + geometric proxy as fallbacks when POF is
UNDEFINED). These tests pin the priority order and reject paths.
"""
from __future__ import annotations

import pytest

from src.core.ffp import is_kastner_eligible
from src.models import DimensionClass, FeatureIdentification, Feature, Surface


def _ft(
    *,
    dimension_class: DimensionClass = DimensionClass.UNDEFINED,
    raw_description: str = "",
    length_mm: float | None = None,
    width_mm: float | None = None,
    feature_identification: FeatureIdentification = FeatureIdentification.CORROSION,
) -> Feature:
    """Build a Feature with the inputs that matter for classification."""
    return Feature(
        anomaly_id="test-1",
        source_run="run_2",
        dimension_class=dimension_class,
        raw_description=raw_description,
        length_mm=length_mm,
        width_mm=width_mm,
        feature_identification=feature_identification,
        wt_mm=6.4,
        depth_pct_wt=20.0,
    )


# ---------------------------------------------------------------------------
# Signal 1: POF dimension_class enum (authoritative)
# ---------------------------------------------------------------------------

class TestPofEnumAuthoritative:
    def test_circumferential_slotting_enum_eligible(self):
        f = _ft(dimension_class=DimensionClass.CIRCUMFERENTIAL_SLOTTING)
        assert is_kastner_eligible(f) is True

    def test_circumferential_grooving_enum_eligible(self):
        f = _ft(dimension_class=DimensionClass.CIRCUMFERENTIAL_GROOVING)
        assert is_kastner_eligible(f) is True

    def test_axial_slotting_enum_rejected(self):
        f = _ft(dimension_class=DimensionClass.AXIAL_SLOTTING)
        assert is_kastner_eligible(f) is False

    def test_axial_grooving_enum_rejected(self):
        f = _ft(dimension_class=DimensionClass.AXIAL_GROOVING)
        assert is_kastner_eligible(f) is False

    def test_pitting_enum_rejected(self):
        f = _ft(dimension_class=DimensionClass.PITTING)
        assert is_kastner_eligible(f) is False

    def test_pinhole_enum_rejected(self):
        f = _ft(dimension_class=DimensionClass.PINHOLE)
        assert is_kastner_eligible(f) is False

    def test_general_enum_rejected(self):
        f = _ft(dimension_class=DimensionClass.GENERAL)
        assert is_kastner_eligible(f) is False


# ---------------------------------------------------------------------------
# POF enum overrides geometric proxy: a feature classified as
# non-circumferential by POF is rejected even if W > L.
# ---------------------------------------------------------------------------

class TestPofEnumOverridesGeometric:
    def test_pitting_with_wide_geometry_still_rejected(self):
        """The v0.3.3-iteration-1 bug: geometric proxy would catch
        PITTING features with incidental W>L, overshooting the
        Mathura-Piyala count from 323 to 3318. This test pins the fix:
        POF enum is authoritative; geometric proxy doesn't override it."""
        f = _ft(
            dimension_class=DimensionClass.PITTING,
            length_mm=10.0, width_mm=30.0,    # W > L
        )
        assert is_kastner_eligible(f) is False

    def test_axial_slotting_with_wide_geometry_still_rejected(self):
        f = _ft(
            dimension_class=DimensionClass.AXIAL_SLOTTING,
            length_mm=10.0, width_mm=30.0,
        )
        assert is_kastner_eligible(f) is False

    def test_circumferential_label_with_tall_geometry_still_eligible(self):
        """Reverse direction: POF says CIRC, geometry says axial.
        POF wins. Catches features where the L/W convention is reversed
        in the source row but the POF column is authoritative."""
        f = _ft(
            dimension_class=DimensionClass.CIRCUMFERENTIAL_SLOTTING,
            length_mm=30.0, width_mm=10.0,    # L > W
        )
        assert is_kastner_eligible(f) is True


# ---------------------------------------------------------------------------
# Signal 2: raw_description substring (fires only when POF UNDEFINED)
# ---------------------------------------------------------------------------

class TestRawDescriptionSubstring:
    def test_description_circumferential_slotting_eligible(self):
        f = _ft(raw_description="Circumferential Slotting")
        assert is_kastner_eligible(f) is True

    def test_description_circumferential_grooving_eligible(self):
        f = _ft(raw_description="Circumferential Grooving")
        assert is_kastner_eligible(f) is True

    def test_description_lowercase_circumferential_grooving_eligible(self):
        f = _ft(raw_description="circumferential grooving")
        assert is_kastner_eligible(f) is True

    def test_description_just_the_word_eligible(self):
        f = _ft(raw_description="Circumferential")
        assert is_kastner_eligible(f) is True

    def test_description_axial_slotting_rejected(self):
        f = _ft(raw_description="Axial Slotting")
        assert is_kastner_eligible(f) is False

    def test_description_pitting_rejected(self):
        f = _ft(raw_description="Pitting")
        assert is_kastner_eligible(f) is False

    def test_description_substring_ignored_when_pof_known_non_circ(self):
        """A confident POF classification of PITTING should not be
        overridden by an oddly-phrased description."""
        f = _ft(
            dimension_class=DimensionClass.PITTING,
            raw_description="Pitting (formerly classed circumferential)",
        )
        assert is_kastner_eligible(f) is False


# ---------------------------------------------------------------------------
# Signal 3: geometric proxy (fires only when POF UNDEFINED + no label)
# ---------------------------------------------------------------------------

class TestGeometricProxy:
    def test_w_gt_l_eligible_when_pof_undefined(self):
        """BPCL Malarna feature #24157-style: unclassified by POF but
        geometrically circumferential (W=60, L=26)."""
        f = _ft(length_mm=26.0, width_mm=60.0)
        assert is_kastner_eligible(f) is True

    def test_w_lt_l_rejected_when_pof_undefined(self):
        f = _ft(length_mm=30.0, width_mm=10.0)
        assert is_kastner_eligible(f) is False

    def test_w_eq_l_rejected_when_pof_undefined(self):
        """Exact equality is not strictly greater — not eligible."""
        f = _ft(length_mm=20.0, width_mm=20.0)
        assert is_kastner_eligible(f) is False

    def test_zero_width_rejected(self):
        f = _ft(length_mm=20.0, width_mm=0.0)
        assert is_kastner_eligible(f) is False

    def test_none_dimensions_rejected_no_false_positive(self):
        """Empty / None dimensions and no label → not eligible."""
        f = _ft(length_mm=None, width_mm=None)
        assert is_kastner_eligible(f) is False

    def test_one_dimension_none_rejected(self):
        f = _ft(length_mm=None, width_mm=30.0)
        assert is_kastner_eligible(f) is False


# ---------------------------------------------------------------------------
# Signal priority — label takes priority over geometric proxy
# ---------------------------------------------------------------------------

class TestSignalPriority:
    def test_label_wins_over_geometric_axial(self):
        """POF UNDEFINED, description says circumferential, but L > W.
        Label should win → eligible by label."""
        f = _ft(
            raw_description="Circumferential",
            length_mm=30.0, width_mm=20.0,
        )
        assert is_kastner_eligible(f) is True

    def test_geometric_when_label_silent_and_pof_undefined(self):
        """POF UNDEFINED, no label match, but W > L → eligible by
        geometric proxy."""
        f = _ft(raw_description="metal loss", length_mm=20.0, width_mm=30.0)
        assert is_kastner_eligible(f) is True


# ---------------------------------------------------------------------------
# Defensive — bad inputs don't crash
# ---------------------------------------------------------------------------

class TestDefensive:
    def test_empty_description_no_crash(self):
        f = _ft(raw_description="")
        assert is_kastner_eligible(f) is False

    def test_whitespace_only_description_no_crash(self):
        f = _ft(raw_description="   \t  ")
        assert is_kastner_eligible(f) is False
