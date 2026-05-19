"""Tests for the MAOP-vs-WT sanity check at Validate time.

When the user sets an MAOP zone whose value exceeds the Barlow design
pressure for the thinnest WT actually present in Run-2, FFP math gets
meaningless (most features compute ERF ≥ 1.0). The check is purely
advisory — it surfaces a yellow inline banner under the MAOP-zones
table but does NOT block Proceed. The user may have a legitimate
reason (e.g. a derating override) so we warn rather than refuse.

These tests pin:

  * The pure-Python helpers (`_max_design_maop_kgcm2`,
    `_min_wt_in_run2_features`) behave per spec on the worked
    examples the user supplied.
  * The Validate-time integration fires / stays silent in each of
    the 5 spec scenarios.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.gui.screens.project_setup import (
    _max_design_maop_kgcm2,
    _min_wt_in_run2_features,
    _wt_min_per_zone_in_run2,
)


# ---------------------------------------------------------------------------
# Helpers for synthesising a tiny Run-2 xlsx in NGP layout (used by the
# v0.2.4 per-zone tests).
# ---------------------------------------------------------------------------

def _make_synthetic_run2(
    tmp_path: Path,
    wt_values: list[float],
    *,
    filename: str = "synthetic_run2.xlsx",
) -> Path:
    """Build a minimal NGP-format Run-2 xlsx with one feature per WT.

    Each WT becomes one row at a unique chainage so the reader doesn't
    drop any as duplicates. Returns the file path.
    """
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Defects"
    ws.append([
        "Anomaly ID", "Absolute Distance, m", "Joint Number",
        "WT, mm", "Depth, %WT", "Length, mm", "Width, mm",
        "Surface", "POF Acronym",
    ])
    for i, wt in enumerate(wt_values, start=1):
        ws.append([
            f"row{i}", 100.0 * i, i, float(wt), 25.0, 50.0, 20.0,
            "Internal", "CORR",
        ])
    path = tmp_path / filename
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Pure-helper tests — no Qt needed
# ---------------------------------------------------------------------------

class TestMaxDesignMaopKgcm2:
    """The Barlow design pressure helper.

    Worked examples from the prompt:
      X70 SMYS=482 MPa, OD=711 mm, WT=7.9 mm,  Fd=0.72 → 78.6 kg/cm²
      X70 SMYS=482 MPa, OD=711 mm, WT=10.3 mm, Fd=0.72 → ~102.5 kg/cm²
    """

    def test_x70_711_79_fd072_gives_78_6(self):
        result = _max_design_maop_kgcm2(
            smys_mpa=482.0, wt_mm=7.9, od_mm=711.0, design_factor=0.72,
        )
        assert result == pytest.approx(78.6, abs=0.5)

    def test_x70_711_103_fd072_gives_about_102(self):
        result = _max_design_maop_kgcm2(
            smys_mpa=482.0, wt_mm=10.3, od_mm=711.0, design_factor=0.72,
        )
        # 2 * 482 * 10.3 / 711 * 0.72 ≈ 10.06 MPa ≈ 102.5 kg/cm²
        assert result == pytest.approx(102.5, abs=1.0)

    def test_x52_273_64_fd072_kandla(self):
        """Kandla (10" X52, 6.4 mm WT, Fd 0.72) → published MAOP 70 kg/cm²
        is exactly the design limit (within rounding)."""
        # SMYS X52 = 358 MPa.
        result = _max_design_maop_kgcm2(
            smys_mpa=358.0, wt_mm=6.4, od_mm=273.0, design_factor=0.72,
        )
        # 2 * 358 * 6.4 / 273 * 0.72 ≈ 12.10 MPa ≈ 123.4 kg/cm²
        assert result == pytest.approx(123.4, abs=1.0)

    @pytest.mark.parametrize("kwargs", [
        {"smys_mpa": 0.0,   "wt_mm": 7.9, "od_mm": 711.0, "design_factor": 0.72},
        {"smys_mpa": 482.0, "wt_mm": 0.0, "od_mm": 711.0, "design_factor": 0.72},
        {"smys_mpa": 482.0, "wt_mm": 7.9, "od_mm": 0.0,   "design_factor": 0.72},
        {"smys_mpa": 482.0, "wt_mm": 7.9, "od_mm": 711.0, "design_factor": 0.0},
        {"smys_mpa": -1.0,  "wt_mm": 7.9, "od_mm": 711.0, "design_factor": 0.72},
    ])
    def test_non_positive_inputs_return_none(self, kwargs):
        assert _max_design_maop_kgcm2(**kwargs) is None


class TestMinWtInRun2Features:
    """The t_min extractor."""

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert _min_wt_in_run2_features(tmp_path / "nope.xlsx") is None

    def test_unreadable_file_returns_none(self, tmp_path: Path):
        bad = tmp_path / "garbage.xlsx"
        bad.write_bytes(b"this is not an xlsx file")
        assert _min_wt_in_run2_features(bad) is None

    def test_real_file_returns_min_wt(self, tmp_path: Path):
        """Build a tiny xlsx with two WTs; expect the min."""
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Defects"
        ws.append([
            "Anomaly ID", "Absolute Distance, m", "Joint Number",
            "WT, mm", "Depth, %WT", "Length, mm", "Width, mm",
            "Surface", "POF Acronym",
        ])
        ws.append(["1", 100.0, 1, 9.5, 25.0, 50.0, 20.0, "Internal", "CORR"])
        ws.append(["2", 200.0, 2, 7.9, 30.0, 50.0, 20.0, "Internal", "CORR"])
        ws.append(["3", 300.0, 3, 11.1, 20.0, 50.0, 20.0, "Internal", "CORR"])
        path = tmp_path / "wt_min.xlsx"
        wb.save(path)
        assert _min_wt_in_run2_features(path) == pytest.approx(7.9)


# ---------------------------------------------------------------------------
# Validate-time integration — fires / stays silent per spec
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def screen(qt_app):
    from src.gui.screens.project_setup import ProjectSetupScreen
    s = ProjectSetupScreen()
    s.show()
    qt_app.processEvents()
    yield s
    s.close()


def _setup_form_for_validate(
    screen, *,
    smys: float = 482.0,
    od: float = 711.0,
    zones: list[tuple[float, float, float, float]] | None = None,
    run2_path: str = "/tmp/fake_run2.xlsx",
):
    """Fill in the form fields needed for the Validate-time check.

    Each zone tuple is ``(wt_min, wt_max, design_factor, maop_kgcm2)``.
    """
    screen.ed_project_name.setText("Test project")
    screen.ed_pipeline_name.setText("Test pipeline")
    screen.sp_diameter.setValue(od)
    screen.sp_length.setValue(50.0)
    screen.sp_smys.setValue(smys)
    screen.ed_material_grade.setText("API 5L X70")
    if zones is None:
        zones = [(7.9, 10.0, 0.72, 96.7)]
    screen.tbl_zones.setRowCount(0)
    for wt_min, wt_max, df, maop in zones:
        screen._append_zone_row(
            wt_min=wt_min, wt_max=wt_max, df=df, maop=maop,
        )
    screen.ed_run2_path.setText(run2_path)
    screen.ed_run1_path.setText("/tmp/fake_run1.xlsx")


def _banner_text(screen) -> str | None:
    if not hasattr(screen, "_maop_warning_banner"):
        return None
    if not screen._maop_warning_banner.isVisible():
        return None
    return screen._maop_warning_banner.text()


class TestValidateTimeIntegration:

    # ----------- Scenario 1: WT_min=7.9, MAOP=96.7 → banner appears -----------
    def test_x70_711_79_maop_96_7_fires_banner(self, screen):
        _setup_form_for_validate(
            screen,
            zones=[(7.0, 10.0, 0.72, 96.7)],
        )
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None, "banner should fire when MAOP > design limit"
        # Banner should name the specific MAOP and the specific WT.
        assert "96.7" in msg
        assert "7.9" in msg or "7.90" in msg
        # Design max for X70/711/7.9/0.72 ≈ 78.6 kg/cm².
        assert "78" in msg or "79" in msg or "design" in msg.lower()

    # ----------- Scenario 2: WT_min=10.3, MAOP=96.7 → no banner --------------
    def test_x70_711_103_maop_96_7_silent(self, screen):
        _setup_form_for_validate(
            screen,
            zones=[(10.0, 12.0, 0.72, 96.7)],
        )
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[10.3],
        ):
            screen._on_validate_clicked()
        # Design limit for 10.3 mm is ~102 kg/cm² > 96.7 → silent.
        assert _banner_text(screen) is None

    # ----------- Scenario 3: no Run-2 loaded → no banner --------------------
    def test_no_run2_file_silent(self, screen):
        _setup_form_for_validate(
            screen,
            zones=[(7.0, 10.0, 0.72, 96.7)],
            run2_path="",  # blank — no file
        )
        # The patched helper isn't even called when path is blank, but
        # patch it anyway to confirm the early-return path.
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ) as mock_helper:
            screen._on_validate_clicked()
        # Either the helper wasn't called (preferred) or the result
        # didn't surface a banner.
        assert _banner_text(screen) is None
        mock_helper.assert_not_called()

    # ----------- Scenario 4: see TestPerZonePerZoneMaopWarning below ---------
    # (The previous `test_multi_zone_warns_only_about_offending_zone` test
    # was retired in v0.2.4 because it reinforced the buggy global-WT_min
    # behaviour. Per-zone tests live in TestPerZoneMaopWarning.)

    # ----------- Scenario 5: MAOP within 5% tolerance → no banner ----------
    def test_within_5pct_tolerance_silent(self, screen):
        """X70/711/7.9/0.72 design limit = 78.6 kg/cm².
        5% above = 82.5 kg/cm². An MAOP of 80 kg/cm² is OVER the
        design limit (78.6) but WITHIN the 5% tolerance — silent.
        """
        _setup_form_for_validate(
            screen,
            zones=[(7.0, 10.0, 0.72, 80.0)],
        )
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ):
            screen._on_validate_clicked()
        assert _banner_text(screen) is None

    # ----------- Banner is non-blocking: Proceed stays enabled --------------
    def test_warning_does_not_block_proceed(self, screen):
        """Critical UX property: the banner is advisory. If all hard
        validations pass, Proceed stays enabled even with the warning
        active."""
        _setup_form_for_validate(
            screen,
            zones=[(7.0, 10.0, 0.72, 96.7)],
        )
        # The hard validation requires existing run files. We can't
        # easily satisfy that in the test environment, so confirm a
        # narrower property: validation FAILED for the missing files,
        # but the banner still fired. The block/no-block separation
        # is preserved in the code (the banner is set unconditionally
        # in _on_validate_clicked, regardless of `ok`).
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ):
            screen._on_validate_clicked()
        # btn_proceed will be False (files don't exist), but the
        # warning STILL appeared — that's the point.
        assert _banner_text(screen) is not None

    # ----------- Banner clears when MAOP is reduced -------------------------
    def test_banner_hides_when_maop_reduced(self, screen):
        _setup_form_for_validate(
            screen,
            zones=[(7.0, 10.0, 0.72, 96.7)],
        )
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ):
            screen._on_validate_clicked()
        assert _banner_text(screen) is not None

        # User lowers the MAOP to under the design limit + 5%, then
        # re-validates.
        screen.tbl_zones.setRowCount(0)
        screen._append_zone_row(wt_min=7.0, wt_max=10.0, df=0.72, maop=75.0)
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],
        ):
            screen._on_validate_clicked()
        assert _banner_text(screen) is None


# ---------------------------------------------------------------------------
# v0.2.4 — per-zone WT_min helper tests (pure, no Qt)
# ---------------------------------------------------------------------------

class TestWtMinPerZoneInRun2:
    """Direct tests for the v0.2.4 per-zone WT_min helper.

    The helper buckets Run-2 features by zone and returns
    ``[min_wt_per_zone, ...]`` aligned with the zone list. Features
    outside every zone fall back to the nearest zone (same logic as
    ``Pipeline.maop_for_wt``).
    """

    def test_empty_zone_list_returns_empty(self, tmp_path):
        # Defensive: no zones declared -> empty list, no crash.
        path = _make_synthetic_run2(tmp_path, [8.5, 9.2, 10.1])
        assert _wt_min_per_zone_in_run2(path, []) == []

    def test_missing_file_returns_all_none(self, tmp_path):
        zones = [
            {"wt_mm_min": 8.0, "wt_mm_max": 9.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
            {"wt_mm_min": 9.1, "wt_mm_max": 9.9,
             "design_factor": 0.72, "maop_kgcm2": 84.1},
        ]
        assert _wt_min_per_zone_in_run2(
            tmp_path / "nope.xlsx", zones
        ) == [None, None]

    def test_unreadable_file_returns_all_none(self, tmp_path):
        bad = tmp_path / "garbage.xlsx"
        bad.write_bytes(b"not actually an xlsx")
        zones = [
            {"wt_mm_min": 8.0, "wt_mm_max": 9.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
        ]
        assert _wt_min_per_zone_in_run2(bad, zones) == [None]

    def test_tp3_three_zones_each_gets_its_own_min(self, tmp_path):
        # Test Pack 3 scenario: three zones, each with two features
        # neatly inside its WT range. Expect per-zone min == smaller
        # of the two values for each zone.
        zones = [
            {"wt_mm_min": 8.0,  "wt_mm_max": 9.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
            {"wt_mm_min": 9.1,  "wt_mm_max": 9.9,
             "design_factor": 0.72, "maop_kgcm2": 84.1},
            {"wt_mm_min": 10.0, "wt_mm_max": 10.5,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
        ]
        # Two features per zone, min should be the smaller of each pair.
        path = _make_synthetic_run2(
            tmp_path, [8.7, 8.9, 9.2, 9.6, 10.1, 10.4],
        )
        out = _wt_min_per_zone_in_run2(path, zones)
        assert out == [pytest.approx(8.7), pytest.approx(9.2),
                       pytest.approx(10.1)]

    def test_orphan_wt_goes_to_nearest_zone(self, tmp_path):
        # WT=7.5 doesn't fit any zone — should land in the lowest
        # zone (8.0-9.0) because that's nearest.
        zones = [
            {"wt_mm_min": 8.0,  "wt_mm_max": 9.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
            {"wt_mm_min": 10.0, "wt_mm_max": 10.5,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
        ]
        path = _make_synthetic_run2(tmp_path, [7.5, 10.2])
        out = _wt_min_per_zone_in_run2(path, zones)
        # 7.5 orphan -> zone 0 (nearest to 8.0). Min of zone 0's
        # bucket {7.5} = 7.5.
        assert out[0] == pytest.approx(7.5)
        assert out[1] == pytest.approx(10.2)

    def test_zone_with_no_features_returns_none(self, tmp_path):
        # Three zones declared, features only in zones 0 and 2.
        zones = [
            {"wt_mm_min": 8.0,  "wt_mm_max": 9.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
            {"wt_mm_min": 9.1,  "wt_mm_max": 9.9,
             "design_factor": 0.72, "maop_kgcm2": 84.1},
            {"wt_mm_min": 10.0, "wt_mm_max": 10.5,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
        ]
        path = _make_synthetic_run2(tmp_path, [8.5, 10.3])
        out = _wt_min_per_zone_in_run2(path, zones)
        assert out[0] == pytest.approx(8.5)
        # Zone 1 has no native features. 8.5 and 10.3 are both inside
        # other zones, so zone 1 stays empty -> None.
        assert out[1] is None
        assert out[2] == pytest.approx(10.3)

    def test_first_match_wins_on_overlapping_zones(self, tmp_path):
        # Defensive: overlapping zones (8-10 and 9-11) should bucket
        # WT=9.5 to the first match (zone 0).
        zones = [
            {"wt_mm_min": 8.0,  "wt_mm_max": 10.0,
             "design_factor": 0.72, "maop_kgcm2": 80.6},
            {"wt_mm_min": 9.0,  "wt_mm_max": 11.0,
             "design_factor": 0.72, "maop_kgcm2": 84.1},
        ]
        path = _make_synthetic_run2(tmp_path, [9.5])
        out = _wt_min_per_zone_in_run2(path, zones)
        assert out[0] == pytest.approx(9.5)
        assert out[1] is None      # 9.5 went to zone 0 (first match)


# ---------------------------------------------------------------------------
# v0.2.4 — per-zone Validate-time integration
# ---------------------------------------------------------------------------

# X70 / 28" / Fd=0.72 design limits for reference (used by these tests):
#   WT=8.7 mm  -> 86.6 kg/cm²
#   WT=9.2 mm  -> 91.5 kg/cm²
#   WT=10.1 mm -> 100.5 kg/cm²
# Test Pack 3 zones: Z1(8.0-9.0, 80.6), Z2(9.1-9.9, 84.1), Z3(10.0-10.5, 96.7).
# Under the correct per-zone check, ALL three pass (each MAOP <= its
# zone's own design limit + 5%). v0.2.0-v0.2.3 fired falsely on Z3.

_TP3_ZONES = [
    (8.0, 9.0, 0.72, 80.6),     # Z1
    (9.1, 9.9, 0.72, 84.1),     # Z2
    (10.0, 10.5, 0.72, 96.7),   # Z3
]
# Per-zone WT_min as observed in a TP3-style Run-2.
_TP3_PER_ZONE_WT_MIN = [8.7, 9.2, 10.1]


class TestPerZoneMaopWarning:
    """v0.2.4 per-zone integration: the warning must fire per-zone, not
    on the globally thinnest wall. Captures the user's bug repro and
    each of the 6 scenarios spec'd for v0.2.4.
    """

    # ----------- 1. TP3 all-pass: no banner --------------------------------
    def test_tp3_all_zones_pass_no_banner(self, screen):
        """The bug repro: TP3 zones with per-zone WT mins
        [8.7, 9.2, 10.1]. v0.2.3 fired on Z3 using global 8.7;
        v0.2.4 must stay silent because each zone is within its OWN
        design limit + 5%.
        """
        _setup_form_for_validate(screen, zones=_TP3_ZONES)
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=list(_TP3_PER_ZONE_WT_MIN),
        ):
            screen._on_validate_clicked()
        assert _banner_text(screen) is None, (
            f"v0.2.4 false-positive: banner fired on TP3 all-pass — "
            f"{_banner_text(screen)!r}"
        )

    # ----------- 2. One zone fails, only that zone named ------------------
    def test_one_zone_fails_only_that_zone_named(self, screen):
        """Bump Z1's MAOP from 80.6 to 90.0 (over its 86.6 design max).
        Banner fires on Z1 and ONLY Z1, citing Z1's own WT_min=8.7.
        """
        zones = list(_TP3_ZONES)
        # 86.6 × 1.05 = 90.93 is the silent threshold. Use 95.0 to
        # clearly clear it (the 5% tolerance is locked behaviour, see
        # `test_within_5pct_tolerance_silent`).
        zones[0] = (8.0, 9.0, 0.72, 95.0)   # Z1 over the design limit + 5%
        _setup_form_for_validate(screen, zones=zones)
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=list(_TP3_PER_ZONE_WT_MIN),
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None, "banner should fire when Z1 over its design max"
        # Z1 cited with ITS OWN WT_min = 8.7 mm.
        assert "8.0" in msg and "9.0" in msg, (
            f"banner doesn't name Z1's range: {msg}"
        )
        assert "8.7" in msg or "8.70" in msg, (
            f"banner doesn't cite Z1's per-zone WT_min: {msg}"
        )
        assert "95.0" in msg, f"banner missing Z1's MAOP: {msg}"
        # Z2 (84.1) and Z3 (96.7) MUST NOT appear — they pass per-zone.
        assert "84.1" not in msg, (
            f"banner wrongly named within-limit Z2: {msg}"
        )
        assert "96.7" not in msg, (
            f"banner wrongly named within-limit Z3: {msg}"
        )

    # ----------- 3. Two zones fail, both mentioned -------------------------
    def test_two_zones_fail_both_mentioned(self, screen):
        """Bump Z1 to 90.0 (over 86.6) AND Z3 to 110.0 (over 100.5).
        Both fail their per-zone checks; both must appear in the banner.
        """
        zones = list(_TP3_ZONES)
        zones[0] = (8.0, 9.0, 0.72, 95.0)    # Z1 over its 86.6 design max + 5%
        zones[2] = (10.0, 10.5, 0.72, 110.0) # Z3 over its 100.5 design max + 5%
        _setup_form_for_validate(screen, zones=zones)
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=list(_TP3_PER_ZONE_WT_MIN),
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None
        # Both Z1 (90.0) and Z3 (110.0) cited.
        assert "95.0" in msg, f"Z1 missing from multi-banner: {msg}"
        assert "110.0" in msg, f"Z3 missing from multi-banner: {msg}"
        # Z2 (within its limit) NOT cited.
        assert "84.1" not in msg, (
            f"banner wrongly named within-limit Z2: {msg}"
        )
        # Each zone's own thinnest wall is cited (not the global one).
        assert "8.7" in msg or "8.70" in msg
        assert "10.1" in msg or "10.10" in msg

    # ----------- 4. Single-zone — unchanged behaviour ----------------------
    def test_single_zone_unchanged(self, screen):
        """Single-zone pipelines (Kandla, TP1-style) behave identically
        to v0.2.x. Patch returns a single-element list."""
        _setup_form_for_validate(
            screen, zones=[(7.0, 10.0, 0.72, 96.7)],
        )
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.9],   # one element, one zone
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None, "single-zone over-design must still fire"
        assert "96.7" in msg
        assert "7.9" in msg or "7.90" in msg

    # ----------- 5. Orphan WT contributes to nearest zone ------------------
    def test_orphan_wt_contributes_to_nearest_zone(self, screen):
        """A run-2 feature with WT below all zones (orphan / TP3-D10
        scenario) gets bucketed into its nearest zone. The per-zone
        WT_min for that zone uses the orphan WT, and the banner cites it.

        Setup: Z1 (8.0-9.0, 80.6), orphan WT=7.5 → goes to Z1. Z1's
        per-zone WT_min becomes 7.5 → design limit 2*482*7.5/711*0.72
        ≈ 74.7 kg/cm². MAOP 80.6 > 74.7×1.05 = 78.4 → fires on Z1.
        """
        zones = [(8.0, 9.0, 0.72, 80.6), (10.0, 10.5, 0.72, 96.7)]
        _setup_form_for_validate(screen, zones=zones)
        # Orphan WT=7.5 lands in Z1; Z2 has its own WT_min=10.2.
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[7.5, 10.2],
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None
        # Z1 cited with WT_min=7.5 (the orphan).
        assert "7.5" in msg or "7.50" in msg, (
            f"banner doesn't cite orphan WT_min: {msg}"
        )
        assert "80.6" in msg, f"banner missing Z1 MAOP: {msg}"
        # Z2 (10.0-10.5, MAOP 96.7) at 10.2 mm: design max 100.5 > 96.7 → silent.
        assert "96.7" not in msg, (
            f"banner wrongly named within-limit Z2: {msg}"
        )

    # ----------- 6. Zone with no Run-2 features skipped silently -----------
    def test_zone_with_no_run2_features_skipped(self, screen):
        """Three zones declared, only zones 0 and 2 have features
        (zone 1 declared but empty). No banner crash; if the populated
        zones are within limits, no banner at all.
        """
        _setup_form_for_validate(screen, zones=_TP3_ZONES)
        # Zone 1 is empty (None). Zones 0 and 2 have their normal mins.
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[8.7, None, 10.1],
        ):
            screen._on_validate_clicked()
        # All populated zones are within limits -> no banner.
        assert _banner_text(screen) is None, (
            f"v0.2.4: empty-zone case fired falsely: {_banner_text(screen)!r}"
        )

    # ----------- 7. Empty-zone case combined with one offender ------------
    def test_empty_zone_does_not_block_other_zones_firing(self, screen):
        """Zone 1 empty, but Z1 (zone 0) over its design limit. The
        empty zone shouldn't suppress or crash; Z1's banner must fire.
        """
        zones = list(_TP3_ZONES)
        zones[0] = (8.0, 9.0, 0.72, 95.0)    # Z1 well over 86.6 limit
        _setup_form_for_validate(screen, zones=zones)
        with patch(
            "src.gui.screens.project_setup._wt_min_per_zone_in_run2",
            return_value=[8.7, None, 10.1],
        ):
            screen._on_validate_clicked()
        msg = _banner_text(screen)
        assert msg is not None
        assert "95.0" in msg
        assert "8.7" in msg or "8.70" in msg
