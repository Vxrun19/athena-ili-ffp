"""v0.3.0 integration tests for chainage-mode MAOP zoning.

Spec scenarios:

  * Two identical-geometry features placed in different chainage zones
    must compute different ERFs (analogous to TP3's D03/D09).
  * One orphan-chainage feature fires :class:`QAFlagCode.MAOP_ZONE_NOT_FOUND`
    with chainage-distance fallback.
  * Chainage-mode banner: a zone deliberately over-pressured for its
    in-zone thinnest WT must fire and cite the chainage range.
"""
from __future__ import annotations

import os
from pathlib import Path

import openpyxl
import pytest

from src.core.cgr import CGRCalculator
from src.core.defect_matcher import DefectMatcher
from src.core.ffp import ffp_assess
from src.core.joint_alignment import JointAligner
from src.io.ili_reader import ILIReader
from src.models import (
    Feature,
    FeatureIdentification,
    MAOPZone,
    Pipeline,
    Project,
    Surface,
    parse_maop_zones,
)
from src.validation import QAFlagCode

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
# Synthetic Run xlsx builder — minimal NGP-format two-sheet pipe tally
# ---------------------------------------------------------------------------

def _make_run_xlsx(
    path: Path,
    features: list[dict],
    *,
    n_joints: int = 100,
    joint_length: float = 12.0,
    wt_mm: float = 7.0,
) -> Path:
    """Build a Run-1/Run-2 style xlsx for a single-WT pipeline."""
    wb = openpyxl.Workbook()
    # Sheet 1: Pipe tally
    ws_pipe = wb.active
    ws_pipe.title = "Pipe"
    ws_pipe.append(["Joint Number", "Absolute Distance, m",
                    "Pipe Length, m", "WT, mm"])
    chain = 0.0
    for jno in range(1, n_joints + 1):
        ws_pipe.append([jno, float(chain), joint_length, wt_mm])
        chain += joint_length
    # Sheet 2: Defects
    ws = wb.create_sheet("Defects")
    ws.append([
        "Anomaly ID", "Absolute Distance, m", "Joint Number",
        "Distance to U/S GW, m", "WT, mm", "Depth, %WT",
        "Length, mm", "Width, mm", "Surface", "POF Acronym",
    ])
    for f in features:
        ws.append([
            f["aid"], f["chainage"], f["joint"], 0.0,
            f.get("wt", wt_mm), f["depth_pct"],
            f.get("length", 20.0), f.get("width", 15.0),
            f.get("surface", "Internal"), "CORR",
        ])
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Identical-geometry across zones — different ERFs
# ---------------------------------------------------------------------------

class TestIdenticalGeometryAcrossChainageZones:
    """Two features with identical depth/length/width but placed in
    different chainage zones must compute different ERFs because the
    zone's MAOP and Fd differ.
    """

    def test_two_features_different_zones_get_different_erfs(
        self, tmp_path,
    ):
        # 3-zone pipeline, ~1.2 km total. Zone 1 has higher MAOP +
        # higher Fd; zone 3 has lower MAOP + lower Fd.
        zones_yaml = [
            {"chainage_m_min": 0,    "chainage_m_max": 400,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
            {"chainage_m_min": 400,  "chainage_m_max": 800,
             "design_factor": 0.60, "maop_kgcm2": 84.1},
            {"chainage_m_min": 800,  "chainage_m_max": 1200,
             "design_factor": 0.50, "maop_kgcm2": 80.6},
        ]
        _, zones = parse_maop_zones("chainage", zones_yaml)
        pipeline = Pipeline(
            pipeline_name="TP4", diameter_mm=406.0, length_km=1.2,
            material_grade="API 5L X60", smys_mpa=413.0,
            maop_zones=zones, maop_zoning_mode="chainage",
        )

        # Two features with IDENTICAL geometry but placed in zones 1 and 3.
        run2 = _make_run_xlsx(
            tmp_path / "run2.xlsx",
            features=[
                {"aid": "F-Z1", "chainage": 100.0, "joint": 9,
                 "depth_pct": 30.0},
                {"aid": "F-Z3", "chainage": 1000.0, "joint": 84,
                 "depth_pct": 30.0},
            ],
            n_joints=100, joint_length=12.0, wt_mm=7.0,
        )
        reader = ILIReader()
        run = reader.read(str(run2), run_id="run_2")
        feats = {str(f.anomaly_id): f for f in run.features_for_assessment()}
        assert {"F-Z1", "F-Z3"} <= feats.keys()
        f_z1 = feats["F-Z1"]
        f_z3 = feats["F-Z3"]
        # Geometry is identical — sanity-check before computing ERFs.
        assert f_z1.depth_pct_wt == f_z3.depth_pct_wt
        assert f_z1.wt_mm == f_z3.wt_mm

        # Run FFP via the coordinator (which uses maop_for_feature now).
        ffp_z1 = ffp_assess(f_z1, pipeline,
                            config={"primary_method": "B31G_Original"})[0]
        ffp_z3 = ffp_assess(f_z3, pipeline,
                            config={"primary_method": "B31G_Original"})[0]

        # Zone 1: MAOP 96.7, Fd 0.72 → higher Psafe (Fd higher) so
        #   ERF = MAOP / Psafe — but higher MAOP also. Need to compute
        #   to see direction.
        # Zone 3: MAOP 80.6, Fd 0.50 → lower Psafe (Fd lower).
        # In practice for these numbers: zone 1 gives Pf×0.72 → larger
        # Psafe; zone 3 gives Pf×0.50 → smaller Psafe. So zone 3 ERF =
        # 80.6 / smaller Psafe could actually be higher than zone 1.
        # The key invariant we test: the two ERFs are DIFFERENT.
        assert ffp_z1.erf != ffp_z3.erf, (
            f"Identical-geometry features in different chainage zones "
            f"got identical ERFs ({ffp_z1.erf}); MAOP zoning isn't "
            f"differentiating them."
        )
        # And the zones the ERFs came from must reflect the chainage
        # not the (identical) WT.
        assert ffp_z1.maop_kgcm2 == 96.7
        assert ffp_z3.maop_kgcm2 == 80.6


# ---------------------------------------------------------------------------
# Orphan chainage → MAOP_ZONE_NOT_FOUND with chainage fallback text
# ---------------------------------------------------------------------------

class TestOrphanChainageFallback:
    def test_orphan_chainage_triggers_maop_zone_not_found(self, tmp_path):
        _, zones = parse_maop_zones("chainage", [
            {"chainage_m_min": 0,    "chainage_m_max": 400,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
        ])
        pipeline = Pipeline(
            pipeline_name="TP4", diameter_mm=406.0, length_km=1.2,
            material_grade="API 5L X60", smys_mpa=413.0,
            maop_zones=zones, maop_zoning_mode="chainage",
        )
        # Place a feature beyond the only zone's max (orphan).
        run2 = _make_run_xlsx(
            tmp_path / "run2.xlsx",
            features=[{"aid": "F-orphan", "chainage": 1000.0,
                       "joint": 84, "depth_pct": 25.0}],
            n_joints=100, joint_length=12.0, wt_mm=7.0,
        )
        reader = ILIReader()
        run = reader.read(str(run2), run_id="run_2")
        feats = run.features_for_assessment()
        assert len(feats) == 1
        results = ffp_assess(feats[0], pipeline,
                              config={"primary_method": "B31G_Original"})
        # Every result should carry MAOP_ZONE_NOT_FOUND flag
        codes = [f.code for r in results for f in r.qa_flags]
        assert QAFlagCode.MAOP_ZONE_NOT_FOUND in codes
        # And the flag message should cite "chainage", not "WT"
        for r in results:
            for flag in r.qa_flags:
                if flag.code == QAFlagCode.MAOP_ZONE_NOT_FOUND:
                    assert "chainage" in flag.message.lower(), (
                        f"flag text doesn't mention chainage: {flag.message}"
                    )
                    assert "1000" in flag.message
                    assert flag.context.get("zoning_mode") == "chainage"
                    break


# ---------------------------------------------------------------------------
# Chainage-mode banner — fires on over-pressured zone, cites chainage range
# ---------------------------------------------------------------------------

class TestChainageBannerIntegration:
    """The MAOP-vs-WT design-sanity banner must work in chainage mode."""

    @pytest.fixture(scope="class")
    def qt_app(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        yield app

    def test_chainage_banner_fires_on_over_pressured_zone(
        self, qt_app, tmp_path,
    ):
        """Synthesize a chainage-mode project YAML with a zone whose
        MAOP exceeds the Barlow design limit for the in-zone thinnest
        WT. Confirm the banner fires and cites the chainage range."""
        import yaml as pyyaml
        from src.gui.screens.project_setup import ProjectSetupScreen

        # WT=6.0 mm (thin), Fd=0.72, OD=406, SMYS=413 → design_max:
        # 2 × 413 × 6.0 / 406 × 0.72 = 8.78 MPa = 89.5 kg/cm².
        # Bump zone-1 MAOP well above (95 > 89.5 × 1.05 = 94 → fires).
        run2_path = _make_run_xlsx(
            tmp_path / "run2.xlsx",
            features=[{"aid": "F1", "chainage": 100.0,
                       "joint": 9, "depth_pct": 20.0}],
            n_joints=50, joint_length=12.0, wt_mm=6.0,
        )
        yaml_path = tmp_path / "p.yaml"
        yaml_data = {
            "project": {"project_name": "TP4-banner"},
            "pipeline": {
                "diameter_mm": 406.0, "length_km": 0.6,
                "material_grade": "API 5L X60", "smys_mpa": 413.0,
                "maop_zoning_mode": "chainage",
            },
            "maop_zones": [
                {"chainage_m_min": 0, "chainage_m_max": 600,
                 "design_factor": 0.72, "maop_kgcm2": 95.0},
            ],
            "runs": {
                "run_1": {"file_path": str(run2_path)},
                "run_2": {"file_path": str(run2_path)},
            },
        }
        yaml_path.write_text(pyyaml.safe_dump(yaml_data, sort_keys=False),
                              encoding="utf-8")

        s = ProjectSetupScreen()
        s.show()
        s._current_config_path = yaml_path
        s._populate_from_dict(yaml_data)
        s.ed_run1_path.setText(str(run2_path))
        s.ed_run2_path.setText(str(run2_path))
        s._refresh_maop_design_warning()
        banner = (
            s._maop_warning_banner.text()
            if hasattr(s, "_maop_warning_banner")
               and s._maop_warning_banner.isVisible()
            else None
        )
        s.close()
        assert banner is not None, "chainage banner should fire on over-pressured zone"
        # Banner must cite chainage units, not WT.
        assert "section" in banner.lower() or "chainage" in banner.lower()
        # Banner must include the offending MAOP value.
        assert "95.0" in banner
        # And the zone's chainage range.
        assert "600.0" in banner or "600" in banner

    def test_chainage_banner_silent_when_zone_safe(
        self, qt_app, tmp_path,
    ):
        """Same setup but MAOP set well below design limit → no banner."""
        import yaml as pyyaml
        from src.gui.screens.project_setup import ProjectSetupScreen

        run2_path = _make_run_xlsx(
            tmp_path / "run2.xlsx",
            features=[{"aid": "F1", "chainage": 100.0,
                       "joint": 9, "depth_pct": 20.0}],
            n_joints=50, joint_length=12.0, wt_mm=8.0,    # thicker WT
        )
        yaml_path = tmp_path / "p.yaml"
        yaml_data = {
            "project": {"project_name": "TP4-banner-safe"},
            "pipeline": {
                "diameter_mm": 406.0, "length_km": 0.6,
                "material_grade": "API 5L X60", "smys_mpa": 413.0,
                "maop_zoning_mode": "chainage",
            },
            "maop_zones": [
                {"chainage_m_min": 0, "chainage_m_max": 600,
                 "design_factor": 0.72, "maop_kgcm2": 70.0},
            ],
            "runs": {
                "run_1": {"file_path": str(run2_path)},
                "run_2": {"file_path": str(run2_path)},
            },
        }
        yaml_path.write_text(pyyaml.safe_dump(yaml_data, sort_keys=False),
                              encoding="utf-8")

        s = ProjectSetupScreen()
        s.show()
        s._current_config_path = yaml_path
        s._populate_from_dict(yaml_data)
        s.ed_run1_path.setText(str(run2_path))
        s.ed_run2_path.setText(str(run2_path))
        s._refresh_maop_design_warning()
        banner_visible = (
            hasattr(s, "_maop_warning_banner")
            and s._maop_warning_banner.isVisible()
        )
        s.close()
        assert not banner_visible, "banner unexpectedly fired on a well-margined zone"
