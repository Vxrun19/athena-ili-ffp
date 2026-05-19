"""Tests for the MAOP-zone bracketing helper used by the Project Setup
auto-fill path.

Prompt 28 surfaced a latent bug: the v0.2 ±0.5 mm-per-WT bracketing
produced overlapping zones for closely-spaced WTs (e.g. [7.1, 8.7, 9.5]
→ 6.6-7.6 / 8.2-9.2 / 9.0-10.0, where the third zone overlapped the
second so WT=9.5 had no matching zone). _build_maop_zones() replaces
that with two cleaner cases:

  * All WTs share a single MAOP → ONE merged zone (most projects).
  * Distinct MAOP per WT → midpoint cuts between consecutive WTs,
    producing non-overlapping, gap-free zones.

These tests pin both behaviours.
"""
from __future__ import annotations

import os
import sys

# Pure-Python tests; QT_QPA_PLATFORM must be set so the GUI module
# import doesn't try to create a real display connection.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

# The helper lives at module scope in project_setup.py so it imports
# without needing to instantiate the GUI screen.
from src.gui.screens.project_setup import _build_maop_zones


class TestSingleMaopMergesZones:
    """Most Athena/NGP pipelines have one MAOP across all WTs.
    _build_maop_zones() should collapse to a single zone."""

    def test_three_wts_single_maop_merges_to_one_zone(self):
        """Abu Road: WTs [7.1, 8.7, 9.5], MAOP=98 → one zone (6.6-10.0)."""
        zones = _build_maop_zones(
            wall_thicknesses=[7.1, 8.7, 9.5],
            maop_kgcm2=98.0,
        )
        assert len(zones) == 1
        wt_min, wt_max, df, maop = zones[0]
        assert wt_min == pytest.approx(6.6)
        assert wt_max == pytest.approx(10.0)
        assert df == pytest.approx(0.72)
        assert maop == pytest.approx(98.0)

    def test_single_wt(self):
        zones = _build_maop_zones(
            wall_thicknesses=[7.1],
            maop_kgcm2=70.0,
            design_factor=0.72,
        )
        assert len(zones) == 1
        wt_min, wt_max, df, maop = zones[0]
        assert wt_min == pytest.approx(6.6)
        assert wt_max == pytest.approx(7.6)
        assert maop == pytest.approx(70.0)

    def test_unsorted_wts_handled(self):
        """Caller may supply WTs in any order — we sort before bracketing."""
        zones = _build_maop_zones(
            wall_thicknesses=[9.5, 7.1, 8.7],
            maop_kgcm2=98.0,
        )
        assert len(zones) == 1
        assert zones[0][0] == pytest.approx(6.6)
        assert zones[0][1] == pytest.approx(10.0)

    def test_design_factor_default(self):
        zones = _build_maop_zones(wall_thicknesses=[7.1], maop_kgcm2=70.0)
        assert zones[0][2] == pytest.approx(0.72)

    def test_custom_design_factor(self):
        zones = _build_maop_zones(
            wall_thicknesses=[7.1], maop_kgcm2=70.0, design_factor=0.60,
        )
        assert zones[0][2] == pytest.approx(0.60)

    def test_no_zones_emitted_without_maop(self):
        zones = _build_maop_zones(
            wall_thicknesses=[7.1, 8.7], maop_kgcm2=None,
        )
        assert zones == []

    def test_no_zones_emitted_without_wts(self):
        zones = _build_maop_zones(
            wall_thicknesses=None, maop_kgcm2=70.0,
        )
        assert zones == []
        zones = _build_maop_zones(
            wall_thicknesses=[], maop_kgcm2=70.0,
        )
        assert zones == []


class TestPerWtMaopMidpointCuts:
    """HMEL-style: each WT carries its own MAOP, zones cut at midpoints."""

    def test_two_distinct_maops(self):
        """WTs [8.7, 11.1] with MAOPs [80.6, 96.7] → cut at midpoint 9.9."""
        zones = _build_maop_zones(
            wall_thicknesses=[8.7, 11.1],
            maop_kgcm2=None,
            maops_per_wt=[80.6, 96.7],
        )
        assert len(zones) == 2
        z1, z2 = zones
        # Zone 1: 8.2 – 9.9 mm, MAOP 80.6
        assert z1[0] == pytest.approx(8.2)
        assert z1[1] == pytest.approx(9.9)
        assert z1[3] == pytest.approx(80.6)
        # Zone 2: 9.9 – 11.6 mm, MAOP 96.7  (joins seamlessly)
        assert z2[0] == pytest.approx(9.9)
        assert z2[1] == pytest.approx(11.6)
        assert z2[3] == pytest.approx(96.7)

    def test_three_distinct_maops_hmel_style(self):
        """HMEL: WTs [8.7, 9.5, 11.1] with MAOPs [80.6, 84.1, 96.7]."""
        zones = _build_maop_zones(
            wall_thicknesses=[8.7, 9.5, 11.1],
            maop_kgcm2=None,
            maops_per_wt=[80.6, 84.1, 96.7],
        )
        assert len(zones) == 3
        z1, z2, z3 = zones
        # Midpoint 8.7-9.5 = 9.1; 9.5-11.1 = 10.3
        assert z1[1] == pytest.approx(9.1)
        assert z2[0] == pytest.approx(9.1)
        assert z2[1] == pytest.approx(10.3)
        assert z3[0] == pytest.approx(10.3)
        assert (z1[3], z2[3], z3[3]) == (
            pytest.approx(80.6), pytest.approx(84.1), pytest.approx(96.7),
        )

    def test_six_wts_collapse_runs_of_same_maop(self):
        """HMEL-extended (per the prompt): six WTs but only three
        distinct MAOPs — runs of identical MAOPs collapse into one zone.
        """
        zones = _build_maop_zones(
            wall_thicknesses=[8.7, 9.5, 10.3, 11.1, 11.9, 14.3],
            maop_kgcm2=None,
            maops_per_wt=[80.6, 84.1, 96.7, 96.7, 96.7, 96.7],
        )
        assert len(zones) == 3
        z1, z2, z3 = zones
        assert z1[3] == pytest.approx(80.6)
        assert z2[3] == pytest.approx(84.1)
        assert z3[3] == pytest.approx(96.7)

    def test_zones_are_gap_free_and_non_overlapping(self):
        """Zone N's upper bound == zone N+1's lower bound for every pair."""
        zones = _build_maop_zones(
            wall_thicknesses=[8.7, 11.1, 14.3],
            maop_kgcm2=None,
            maops_per_wt=[80.6, 84.1, 96.7],
        )
        for i in range(len(zones) - 1):
            assert zones[i][1] == zones[i + 1][0], (
                f"gap between zones {i} and {i + 1}: "
                f"{zones[i][1]} vs {zones[i + 1][0]}"
            )

    def test_every_wt_falls_into_exactly_one_zone(self):
        """For every input WT, there must be one and only one zone
        whose [wt_min, wt_max] contains it."""
        wts = [7.1, 8.7, 9.5]
        zones = _build_maop_zones(wall_thicknesses=wts, maop_kgcm2=98.0)
        for w in wts:
            matching = [
                z for z in zones if z[0] <= w <= z[1]
            ]
            assert len(matching) == 1, (
                f"WT={w} matched {len(matching)} zones: {matching} "
                f"(all zones: {zones})"
            )

    def test_per_wt_maops_wrong_length_falls_back_to_single(self):
        """If maops_per_wt has the wrong length, fall back to single-MAOP."""
        zones = _build_maop_zones(
            wall_thicknesses=[7.1, 8.7],
            maop_kgcm2=98.0,
            maops_per_wt=[80.6],  # wrong length
        )
        assert len(zones) == 1
        assert zones[0][3] == pytest.approx(98.0)
