"""End-to-end test for the format converter (Prompt 16 Step 5).

Take the real Kandla Run-1 pipe-tally file, pretend it's from an
unknown vendor, write a profile that maps its column names to the
canonical NGP layout, convert it, then re-run the full FFP pipeline
against the converted file + the original Run-2 file. The published
canonical CGR for feature #125 (0.2522 mm/yr) MUST still match.

Why this matters: a converter that silently re-bins / re-rounds values
won't show up in unit tests, but it WILL show up here as a CGR drift.
This test is the safety net.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from src.io.format_converter import FormatConverter, VendorProfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KANDLA_RUN1 = (
    PROJECT_ROOT
    / "examples"
    / "10_inch_Kandla_to_Samakhiali__58_2_km_FR_Pipe_Tally_Rev_0.xlsx"
)
KANDLA_RUN2 = PROJECT_ROOT / "examples" / "1ZSV_Pipeline_Listing.xlsx"


def _kandla_run1_profile() -> VendorProfile:
    """Profile that maps the real Kandla 2018 file as if from an unknown vendor.

    The Kandla file is, in fact, an older NGP/Athena 2018 layout: sheet
    "Metal Loss List", three title rows above the headers, headers on
    row 3 (0-indexed). Surface column uses "Internal"/"External" rather
    than POF codes — covered by the value_normalizations block.
    """
    return VendorProfile(
        vendor_name="Athena 2018 (treated as unknown vendor for round-trip)",
        sheet_name="Metal Loss List",
        header_row=3,
        column_mappings={
            "abs_distance_m":         "Abs. Distance, m",
            "joint_number":           "Joint Number",
            "upstream_weld_dist_m":   "Distance to U/S GW, m",
            "joint_length_m":         "Joint Length, m",
            "feature_identification": "Feature Identification",
            "dimension_class":        "Dimension Class",
            "clock_position":         "Orientation o'clock",
            "wt_mm":                  "Wall Thickness, mm",
            "length_mm":              "Axial Length, mm",
            "width_mm":               "Width, mm",
            "depth_pct_wt":           "Depth, %",
            "surface":                "Location",
            "latitude":               "Latitude",
            "longitude":              "Longitude",
            "altitude_m":             "Altitude",
        },
        unit_conventions={
            "chainage": "m",
            "upstream_weld_dist": "m",
            "depth": "%",
            "clock": "hh:mm",
            "length": "mm",
            "width": "mm",
            "wall_thickness": "mm",
            "altitude": "m",
        },
        value_normalizations={
            "surface": {
                "Internal": "internal",
                "External": "external",
                "INT": "internal",
                "EXT": "external",
            },
            "feature_identification": {
                # Most Kandla rows already say "Corrosion", which is in
                # the global synonyms table — included here so the
                # profile is self-contained when shipped on its own.
                "Corrosion": "CORR",
                "MFG":       "MIAN",
                "Mfg":       "MIAN",
                "Manufacturing": "MIAN",
            },
        },
        notes="Synthetic round-trip profile for the Kandla 2018 file.",
        # Critical: carry the Pipeline Tally sheet through so the joint
        # aligner sees the full ~5000-joint registry, not just the ~39
        # joints that have defects. Without this, alignment degrades and
        # CGR drifts.
        pipe_sheet_name="Pipeline Tally",
        pipe_header_row=3,
        pipe_column_mappings={
            "abs_distance_m":       "Abs. Distance, m",
            "joint_number":         "Joint Number",
            "upstream_weld_dist_m": "Distance to U/S GW, m",
            "joint_length_m":       "Joint Length, m",
            "wt_mm":                "Wall Thickness, mm",
        },
    )


@pytest.mark.skipif(
    not (KANDLA_RUN1.exists() and KANDLA_RUN2.exists()),
    reason="Kandla example files not present (CI without binaries).",
)
def test_kandla_run1_roundtrip_preserves_published_cgr(tmp_path: Path):
    """Convert Kandla Run-1, run the pipeline, assert #125 CGR == 0.2522 mm/yr."""

    # ---- 1. Convert Kandla Run-1 to the NGP layout via our converter
    converted_path = tmp_path / "kandla_run1_converted.xlsx"
    profile = _kandla_run1_profile()
    FormatConverter(profile).convert(KANDLA_RUN1, converted_path)
    assert converted_path.exists(), "converter did not produce an output file"

    # ---- 2. Build a temp project YAML pointing at the converted file.
    #         Re-use the canonical Kandla geometry/MAOP/dates from the
    #         shipped example so we're testing the convert step, not
    #         project-config wiring.
    base_yaml_path = PROJECT_ROOT / "examples" / "kandla_project.yaml"
    with base_yaml_path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Point run_1 at the freshly-converted file (absolute path so the
    # reader doesn't try to resolve it against the project root).
    config["runs"]["run_1"]["file_path"] = str(converted_path)
    # Run-2 path is left as-is (resolved against project root by the CLI
    # path-resolver; for this test we resolve it manually below).

    proj_yaml = tmp_path / "kandla_roundtrip.yaml"
    with proj_yaml.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False)

    # ---- 3. Run the pipeline programmatically (skip CLI; faster + the
    #         error messages are clearer when an assertion fires).
    from src.core.cgr import CGRCalculator
    from src.core.defect_matcher import DefectMatcher
    from src.core.joint_alignment import JointAligner
    from src.io.ili_reader import ILIReader
    from src.models import Project

    project = Project.from_yaml(str(proj_yaml))
    reader = ILIReader()

    run1 = reader.read(str(converted_path), run_id="run_1")
    if project.run_1.inspection_date:
        run1.inspection_date = project.run_1.inspection_date

    run2 = reader.read(str(KANDLA_RUN2), run_id="run_2")
    if project.run_2.inspection_date:
        run2.inspection_date = project.run_2.inspection_date

    # ---- 4. Run alignment + matching + CGR for feature 125.
    years_between = (run2.inspection_date - run1.inspection_date).days / 365.25

    ja = JointAligner().align(run1, run2)
    mr = DefectMatcher().match(run1, run2, ja.matches)
    cgr_mode = (project.config.get("cgr") or {}).get("mode", "hybrid")
    cgrs = CGRCalculator({"mode": cgr_mode}).compute(
        mr, years_between=years_between,
    )

    # ---- 5. Find feature #125 (canonical published reference defect).
    target = None
    for c in cgrs:
        if str(c.feature.anomaly_id) == "125":
            target = c
            break
    assert target is not None, (
        "feature #125 not found in CGR results — converter dropped it or "
        "renamed its anomaly_id"
    )

    # Published canonical CGR (from examples/expected_results/kandla_samakhiali.yaml).
    EXPECTED = 0.2522
    # Tolerance: 1e-3 mm/yr is more than tight enough for "did anything
    # drift" — the published value itself was rounded to 4 sig figs.
    assert target.cgr_mm_yr == pytest.approx(EXPECTED, abs=1e-3), (
        f"Feature #125 CGR drift after round-trip: "
        f"got {target.cgr_mm_yr:.4f}, expected {EXPECTED:.4f} mm/yr"
    )
