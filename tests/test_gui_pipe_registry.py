"""Automated equivalent of the Prompt 18 manual test.

Drives the Format Converter GUI screen off-screen:

  1. Load examples/10_inch_Kandla_to_Samakhiali...xlsx.
  2. Confirm the pipe-tally section auto-enables and the "Pipeline Tally"
     sheet is auto-selected with header_row = 3.
  3. Build the VendorProfile from the screen state and run the
     converter directly (bypassing the modal export dialog).
  4. Re-load the exported file through the existing pipeline:
     read with ILIReader → joint align → match → CGR.
  5. Assert feature #125 CGR == 0.2522 mm/yr (the published value).

This catches the regression that Prompt 16 fixed but Prompt 17's GUI
didn't expose: a converter that doesn't include the pipe-registry sheet
ends up with sparse joint alignment and ~12% CGR drift.

Skipped automatically if the Kandla examples aren't present (so this
test can ride along into CI without bundling the binaries).
"""
from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KANDLA_RUN1 = (
    PROJECT_ROOT / "examples"
    / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx"
)
KANDLA_RUN2 = PROJECT_ROOT / "examples" / "1ZSV_Pipeline_Listing.xlsx"


# Force an offscreen Qt platform so the test can run without a display
# (CI, SSH, build farm). Must be set BEFORE any QtWidgets import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    """Single QApplication for the module's tests."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # Don't quit — pytest tear-down handles it; quitting here interacts
    # badly with offscreen mode on Windows.


@pytest.mark.skipif(
    not (KANDLA_RUN1.exists() and KANDLA_RUN2.exists()),
    reason="Kandla example files not present.",
)
class TestGuiPipeRegistryRoundTrip:
    """End-to-end: GUI screen → exported file → pipeline → published CGR."""

    def test_pipe_section_auto_enables_on_kandla_load(self, qt_app, tmp_path: Path):
        from src.gui.screens.format_converter import FormatConverterScreen

        screen = FormatConverterScreen()
        # Show it so layouts settle (offscreen — no actual window).
        screen.show()
        screen._load_source(KANDLA_RUN1)

        ps = screen.pipe_section
        assert ps.isChecked(), (
            "pipe-registry section should auto-enable when the workbook "
            "has a sheet matching the pipe-tally heuristic"
        )
        assert ps.selected_sheet() == "Pipeline Tally", (
            f"expected 'Pipeline Tally', got {ps.selected_sheet()!r}"
        )
        assert ps.selected_header_row() == 3, (
            "pipe section should inherit the defect sheet's header row (3) "
            "by default"
        )
        # Banner should be visible with the standard message.
        assert ps._banner.isVisible()
        banner_text = ps._banner.text()
        assert "Pipeline Tally" in banner_text
        assert "default" in banner_text.lower()

        # Auto-mapping should have caught all REQUIRED pipe fields.
        assert ps.required_unmapped() == [], (
            f"REQUIRED pipe fields unmapped: {ps.required_unmapped()}"
        )

        # Spot-check a couple of mappings.
        mappings = ps.get_mappings()
        assert mappings.get("joint_number") == "Joint Number"
        assert mappings.get("joint_length_m") == "Joint Length, m"

        screen.deleteLater()

    def test_gui_export_preserves_published_125_cgr(self, qt_app, tmp_path: Path):
        """Full round-trip: GUI → exported file → pipeline → CGR check."""
        from src.gui.screens.format_converter import FormatConverterScreen
        from src.io.format_converter import FormatConverter

        # Copy the input to a tmp dir so the converter's "{stem}_NGP.xlsx"
        # output doesn't pollute examples/.
        tmp_input = tmp_path / KANDLA_RUN1.name
        shutil.copy(KANDLA_RUN1, tmp_input)

        screen = FormatConverterScreen()
        screen.show()
        screen._load_source(tmp_input)

        # The GUI normally goes through _on_export() which pops a modal
        # confirm dialog for the missing anomaly_id. Bypass that and
        # call the converter directly — we're testing the profile the
        # screen *built*, not the dialog plumbing.
        profile = screen._build_profile()
        assert profile.pipe_sheet_name == "Pipeline Tally"
        assert profile.pipe_column_mappings, (
            "profile.pipe_column_mappings empty — pipe sheet won't ship"
        )

        # Run the actual converter against the profile the GUI built.
        out_path = tmp_path / "kandla_run1_via_gui.xlsx"
        FormatConverter(profile).convert(tmp_input, out_path)
        assert out_path.exists()
        screen.deleteLater()

        # --- Now feed the exported file through the pipeline.
        from src.core.cgr import CGRCalculator
        from src.core.defect_matcher import DefectMatcher
        from src.core.joint_alignment import JointAligner
        from src.io.ili_reader import ILIReader

        reader = ILIReader()
        run1 = reader.read(str(out_path), run_id="run_1")
        run1.inspection_date = date(2018, 12, 15)
        run2 = reader.read(str(KANDLA_RUN2), run_id="run_2")
        run2.inspection_date = date(2023, 3, 15)
        years_between = (run2.inspection_date - run1.inspection_date).days / 365.25

        ja = JointAligner().align(run1, run2)
        # Without the pipe sheet, this drops to ~39 joint pairs (vs
        # ~4900 with the registry). The assertion below catches that.
        assert len(ja.matches) > 1000, (
            f"only {len(ja.matches)} joint pairs — pipe sheet probably "
            "missing from the exported file"
        )

        mr = DefectMatcher().match(run1, run2, ja.matches)
        cgrs = CGRCalculator({"mode": "hybrid"}).compute(
            mr, years_between=years_between,
        )

        target = next(
            (c for c in cgrs if str(c.feature.anomaly_id) == "125"), None,
        )
        assert target is not None, (
            "feature #125 missing from CGR results — converter dropped it"
        )

        EXPECTED = 0.2522
        assert target.cgr_mm_yr == pytest.approx(EXPECTED, abs=1e-3), (
            f"#125 CGR drift after GUI round-trip: "
            f"got {target.cgr_mm_yr:.4f}, expected {EXPECTED:.4f} mm/yr. "
            f"This is the Prompt 18 bug — the pipe-tally sheet isn't "
            f"being included in the GUI export."
        )
