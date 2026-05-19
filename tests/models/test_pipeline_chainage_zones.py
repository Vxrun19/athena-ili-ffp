"""Tests for v0.3.0 chainage-based MAOP zoning.

Pins the new schema, parser/validator, and Pipeline lookup methods.
Backward-compat (WT mode) is covered by the unchanged
``tests/test_maop_design_check.py`` etc., which must continue passing.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml as pyyaml

from src.models import (
    Feature,
    FeatureIdentification,
    MAOPZone,
    Pipeline,
    Project,
    Surface,
    parse_maop_zones,
)


# ---------------------------------------------------------------------------
# parse_maop_zones — schema validation
# ---------------------------------------------------------------------------

class TestParseMaopZones:
    def test_default_mode_is_wt(self):
        mode, zones = parse_maop_zones(None, [
            {"wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        assert mode == "wt"
        assert len(zones) == 1
        assert zones[0].wt_mm_min == 6.0
        assert zones[0].chainage_m_min is None

    def test_empty_mode_string_defaults_to_wt(self):
        mode, _ = parse_maop_zones("", [])
        assert mode == "wt"
        mode, _ = parse_maop_zones("   ", [])
        assert mode == "wt"

    def test_explicit_wt_mode(self):
        mode, zones = parse_maop_zones("wt", [
            {"wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        assert mode == "wt"

    def test_chainage_mode(self):
        mode, zones = parse_maop_zones("chainage", [
            {"chainage_m_min": 0, "chainage_m_max": 28444,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
            {"chainage_m_min": 28444, "chainage_m_max": 64944,
             "design_factor": 0.60, "maop_kgcm2": 84.1},
        ])
        assert mode == "chainage"
        assert len(zones) == 2
        for z in zones:
            assert z.is_chainage_bounded
            assert z.wt_mm_min is None
            assert z.chainage_m_min is not None

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match='must be "wt" or "chainage"'):
            parse_maop_zones("bogus", [], yaml_path="/x/p.yaml")

    def test_non_string_mode_raises(self):
        with pytest.raises(ValueError, match="must be a string"):
            parse_maop_zones(42, [])

    def test_wt_mode_with_chainage_keys_raises(self):
        # User accidentally mixed schemas (wt mode but provided
        # chainage bounds).
        with pytest.raises(
            ValueError, match=r'maop_zoning_mode="wt".*chainage_m_\* keys'
        ):
            parse_maop_zones("wt", [
                {"wt_mm_min": 6, "wt_mm_max": 8,
                 "chainage_m_min": 0, "chainage_m_max": 100,
                 "design_factor": 0.72, "maop_kgcm2": 70},
            ], yaml_path="/x/p.yaml")

    def test_chainage_mode_with_wt_keys_raises(self):
        with pytest.raises(
            ValueError, match=r'maop_zoning_mode="chainage".*wt_mm_\* keys'
        ):
            parse_maop_zones("chainage", [
                {"chainage_m_min": 0, "chainage_m_max": 100,
                 "wt_mm_min": 6, "wt_mm_max": 8,
                 "design_factor": 0.72, "maop_kgcm2": 70},
            ])

    def test_chainage_mode_missing_bounds_raises(self):
        with pytest.raises(ValueError, match="missing chainage_m"):
            parse_maop_zones("chainage", [
                {"design_factor": 0.72, "maop_kgcm2": 70},
            ])

    def test_wt_mode_missing_bounds_raises(self):
        with pytest.raises(ValueError, match="missing wt_mm"):
            parse_maop_zones("wt", [
                {"design_factor": 0.72, "maop_kgcm2": 70},
            ])

    def test_negative_chainage_min_raises(self):
        with pytest.raises(ValueError, match="negative chainage_m_min"):
            parse_maop_zones("chainage", [
                {"chainage_m_min": -5, "chainage_m_max": 100,
                 "design_factor": 0.72, "maop_kgcm2": 70},
            ])

    def test_chainage_max_below_min_raises(self):
        with pytest.raises(ValueError, match="chainage_m_max .* < chainage_m_min"):
            parse_maop_zones("chainage", [
                {"chainage_m_min": 100, "chainage_m_max": 50,
                 "design_factor": 0.72, "maop_kgcm2": 70},
            ])

    def test_overlapping_chainage_zones_raises(self):
        with pytest.raises(ValueError, match="Overlapping chainage zones"):
            parse_maop_zones("chainage", [
                {"chainage_m_min": 0,  "chainage_m_max": 100,
                 "design_factor": 0.72, "maop_kgcm2": 70},
                {"chainage_m_min": 50, "chainage_m_max": 200,
                 "design_factor": 0.72, "maop_kgcm2": 80},
            ], yaml_path="/x/p.yaml")

    def test_adjacent_zones_at_shared_boundary_ok(self):
        # [0, 28444] and [28444, 64944] share a boundary point but
        # don't actually overlap — should be allowed.
        mode, zones = parse_maop_zones("chainage", [
            {"chainage_m_min": 0,     "chainage_m_max": 28444,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
            {"chainage_m_min": 28444, "chainage_m_max": 64944,
             "design_factor": 0.60, "maop_kgcm2": 84.1},
        ])
        assert len(zones) == 2


# ---------------------------------------------------------------------------
# Pipeline.maop_for_chainage — lookup + fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def chainage_pipeline():
    """3-zone HMEL-style pipeline."""
    _, zones = parse_maop_zones("chainage", [
        {"chainage_m_min": 0.0,     "chainage_m_max": 28444.0,
         "design_factor": 0.72, "maop_kgcm2": 96.7},
        {"chainage_m_min": 28444.0, "chainage_m_max": 64944.0,
         "design_factor": 0.60, "maop_kgcm2": 84.1},
        {"chainage_m_min": 64944.0, "chainage_m_max": 100000.0,
         "design_factor": 0.50, "maop_kgcm2": 80.6},
    ])
    return Pipeline(
        diameter_mm=406.0, length_km=100.0, material_grade="X60",
        smys_mpa=413.0, maop_zones=zones,
        maop_zoning_mode="chainage",
    )


class TestMaopForChainage:
    def test_feature_in_first_zone(self, chainage_pipeline):
        z, idx, fb = chainage_pipeline.maop_for_chainage(5000.0)
        assert idx == 0
        assert z.maop_kgcm2 == 96.7
        assert fb is False

    def test_feature_in_middle_zone(self, chainage_pipeline):
        z, idx, fb = chainage_pipeline.maop_for_chainage(30000.0)
        assert idx == 1
        assert z.maop_kgcm2 == 84.1
        assert fb is False

    def test_feature_in_last_zone(self, chainage_pipeline):
        z, idx, fb = chainage_pipeline.maop_for_chainage(70000.0)
        assert idx == 2
        assert z.maop_kgcm2 == 80.6
        assert fb is False

    def test_feature_at_exact_inclusive_max(self, chainage_pipeline):
        # 28444 is in zone 0 (inclusive max) — first match wins over
        # zone 1's inclusive min.
        z, idx, fb = chainage_pipeline.maop_for_chainage(28444.0)
        assert idx == 0

    def test_orphan_chainage_falls_back_to_nearest(self, chainage_pipeline):
        z, idx, fb = chainage_pipeline.maop_for_chainage(150000.0)
        assert fb is True
        assert idx == 2     # nearest to chainage 150000 is zone 2 (max 100000)

    def test_orphan_below_falls_back_to_first(self):
        _, zones = parse_maop_zones("chainage", [
            {"chainage_m_min": 1000, "chainage_m_max": 5000,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        p = Pipeline(maop_zones=zones, maop_zoning_mode="chainage")
        z, idx, fb = p.maop_for_chainage(0.0)
        assert fb is True
        assert idx == 0

    def test_no_zones_returns_none(self):
        p = Pipeline(maop_zones=[], maop_zoning_mode="chainage")
        z, idx, fb = p.maop_for_chainage(1000.0)
        assert z is None
        assert idx is None
        assert fb is False


# ---------------------------------------------------------------------------
# Pipeline.maop_for_feature — mode dispatcher
# ---------------------------------------------------------------------------

class TestMaopForFeature:
    def test_wt_mode_dispatches_to_wt_lookup(self):
        _, zones = parse_maop_zones("wt", [
            {"wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        p = Pipeline(maop_zones=zones, maop_zoning_mode="wt")
        f = Feature(anomaly_id="t", source_run="r2",
                    wt_mm=7.0, abs_distance_m=50000.0,
                    depth_pct_wt=20.0)
        z, idx, fb = p.maop_for_feature(f)
        assert z.maop_kgcm2 == 70
        assert fb is False    # 7.0 in [6, 8]

    def test_wt_mode_orphan_sets_fallback_true(self):
        _, zones = parse_maop_zones("wt", [
            {"wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        p = Pipeline(maop_zones=zones, maop_zoning_mode="wt")
        f = Feature(anomaly_id="t", source_run="r2",
                    wt_mm=10.0, abs_distance_m=50000.0,
                    depth_pct_wt=20.0)
        z, idx, fb = p.maop_for_feature(f)
        assert fb is True

    def test_chainage_mode_uses_chainage_not_wt(self, chainage_pipeline):
        """Feature with WT that would map to a different zone in WT
        mode still routes by chainage."""
        f = Feature(anomaly_id="t", source_run="r2",
                    wt_mm=8.0, abs_distance_m=5000.0,
                    depth_pct_wt=20.0)
        z, idx, fb = chainage_pipeline.maop_for_feature(f)
        assert idx == 0    # chainage 5000 → zone 0
        assert z.maop_kgcm2 == 96.7

    def test_chainage_mode_orphan_chainage(self, chainage_pipeline):
        f = Feature(anomaly_id="t", source_run="r2",
                    wt_mm=8.0, abs_distance_m=150000.0,
                    depth_pct_wt=20.0)
        _z, _idx, fb = chainage_pipeline.maop_for_feature(f)
        assert fb is True

    def test_feature_in_zone_helper(self, chainage_pipeline):
        z, _, _ = chainage_pipeline.maop_for_chainage(5000.0)
        f_in = Feature(anomaly_id="i", source_run="r2",
                       wt_mm=8.0, abs_distance_m=5000.0,
                       depth_pct_wt=20.0)
        f_out = Feature(anomaly_id="o", source_run="r2",
                        wt_mm=8.0, abs_distance_m=150000.0,
                        depth_pct_wt=20.0)
        assert chainage_pipeline.feature_in_zone(z, f_in) is True
        assert chainage_pipeline.feature_in_zone(z, f_out) is False


# ---------------------------------------------------------------------------
# MAOPZone.contains_chainage / contains
# ---------------------------------------------------------------------------

class TestMaopZoneContainmentChecks:
    def test_wt_zone_contains_returns_false_for_chainage_zone(self):
        z = MAOPZone(chainage_m_min=0.0, chainage_m_max=100.0,
                     design_factor=0.72, maop_kgcm2=70)
        assert z.contains(7.0) is False    # WT-shaped check on chainage zone
        assert z.contains_chainage(50.0) is True

    def test_chainage_check_returns_false_for_wt_zone(self):
        z = MAOPZone(wt_mm_min=6.0, wt_mm_max=8.0,
                     design_factor=0.72, maop_kgcm2=70)
        assert z.contains_chainage(50.0) is False
        assert z.contains(7.0) is True

    def test_is_chainage_bounded(self):
        wt = MAOPZone(wt_mm_min=6.0, wt_mm_max=8.0,
                      design_factor=0.72, maop_kgcm2=70)
        ch = MAOPZone(chainage_m_min=0.0, chainage_m_max=100.0,
                      design_factor=0.72, maop_kgcm2=70)
        assert wt.is_chainage_bounded is False
        assert ch.is_chainage_bounded is True


# ---------------------------------------------------------------------------
# Project.from_yaml round-trip with chainage mode
# ---------------------------------------------------------------------------

class TestYamlRoundTripChainageMode:
    def _write_yaml(self, tmp_path, mode, zones_block, fname="p.yaml"):
        data = {
            "project": {"project_name": "T"},
            "pipeline": {
                "diameter_mm": 406.0, "length_km": 100.0,
                "material_grade": "X60", "smys_mpa": 413.0,
            },
            "maop_zones": zones_block,
            "runs": {
                "run_1": {"file_path": "r1.xlsx"},
                "run_2": {"file_path": "r2.xlsx"},
            },
        }
        if mode is not None:
            data["pipeline"]["maop_zoning_mode"] = mode
        path = tmp_path / fname
        path.write_text(pyyaml.safe_dump(data, sort_keys=False),
                        encoding="utf-8")
        return path

    def test_chainage_yaml_load(self, tmp_path):
        path = self._write_yaml(tmp_path, "chainage", [
            {"chainage_m_min": 0,     "chainage_m_max": 28444,
             "design_factor": 0.72, "maop_kgcm2": 96.7},
            {"chainage_m_min": 28444, "chainage_m_max": 64944,
             "design_factor": 0.60, "maop_kgcm2": 84.1},
        ])
        proj = Project.from_yaml(str(path))
        assert proj.pipeline.maop_zoning_mode == "chainage"
        assert len(proj.pipeline.maop_zones) == 2
        assert proj.pipeline.maop_zones[0].chainage_m_max == 28444.0
        assert proj.pipeline.maop_zones[0].wt_mm_min is None

    def test_legacy_yaml_loads_as_wt_mode(self, tmp_path):
        path = self._write_yaml(tmp_path, None, [
            {"wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        proj = Project.from_yaml(str(path))
        assert proj.pipeline.maop_zoning_mode == "wt"
        assert proj.pipeline.maop_zones[0].wt_mm_min == 6.0

    def test_kandla_loads_wt_mode_unchanged(self):
        # Backward-compat smoke: the bundled Kandla YAML continues
        # to load identically to v0.2.6 with no migration.
        proj = Project.from_yaml("examples/kandla_project.yaml")
        assert proj.pipeline.maop_zoning_mode == "wt"
        assert len(proj.pipeline.maop_zones) == 1
        assert proj.pipeline.maop_zones[0].wt_mm_min == 6.0

    def test_invalid_yaml_raises_at_load(self, tmp_path):
        # Chainage mode + wt key → must raise.
        path = self._write_yaml(tmp_path, "chainage", [
            {"chainage_m_min": 0, "chainage_m_max": 100,
             "wt_mm_min": 6, "wt_mm_max": 8,
             "design_factor": 0.72, "maop_kgcm2": 70},
        ])
        with pytest.raises(ValueError):
            Project.from_yaml(str(path))
