"""Tests for the v0.2.5 ``report.annexures`` YAML block.

Covers ``parse_report_annexures`` + ``serialize_report_annexures``
(both in ``src.models``) and the round-trip through
``Project.from_yaml``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml as pyyaml

from src.models import (
    Project,
    parse_report_annexures,
    serialize_report_annexures,
)
from src.reports.topic_registry import default_annexure_selection


# ---------------------------------------------------------------------------
# parse_report_annexures — direct
# ---------------------------------------------------------------------------

class TestParseReportAnnexures:
    def test_missing_block_returns_legacy_default(self):
        # Empty dict (no `annexures` key) -> legacy default
        out = parse_report_annexures({}, yaml_path="/x/p.yaml")
        assert out == default_annexure_selection()

    def test_none_input_returns_legacy_default(self):
        # parse_report_annexures(None, ...) -> legacy default
        out = parse_report_annexures(None, yaml_path="/x/p.yaml")
        assert out == default_annexure_selection()

    def test_empty_list_returns_legacy_default(self):
        # `annexures: []` -> legacy default (also: GUI enforces "≥ 1")
        out = parse_report_annexures({"annexures": []})
        assert out == default_annexure_selection()

    def test_full_explicit_selection_preserves_order(self):
        block = {"annexures": [
            {"topic": "guidelines_formulas",    "letter": "A"},
            {"topic": "qa_findings",            "letter": "B"},
            {"topic": "results_ili_comparison", "letter": "C"},
        ]}
        out = parse_report_annexures(block)
        assert out == [
            ("guidelines_formulas", "A"),
            ("qa_findings", "B"),
            ("results_ili_comparison", "C"),
        ]

    def test_missing_letter_uses_topic_default(self):
        block = {"annexures": [
            {"topic": "guidelines_formulas"},   # no `letter` key
            {"topic": "qa_findings", "letter": "Z"},
        ]}
        out = parse_report_annexures(block)
        assert out == [
            ("guidelines_formulas", "A"),       # default_letter for guidelines
            ("qa_findings", "Z"),
        ]

    def test_unknown_topic_raises_with_yaml_path(self):
        block = {"annexures": [{"topic": "bogus", "letter": "X"}]}
        with pytest.raises(ValueError) as exc:
            parse_report_annexures(block, yaml_path="/x/p.yaml")
        msg = str(exc.value)
        assert "bogus" in msg
        assert "/x/p.yaml" in msg
        # Error must cite the offending list index.
        assert "report.annexures[0]" in msg

    def test_duplicate_letters_raises_naming_both(self):
        block = {"annexures": [
            {"topic": "guidelines_formulas", "letter": "X"},
            {"topic": "qa_findings",          "letter": "X"},
        ]}
        with pytest.raises(ValueError) as exc:
            parse_report_annexures(block)
        msg = str(exc.value)
        assert "X" in msg
        assert "guidelines_formulas" in msg
        assert "qa_findings" in msg

    def test_entry_missing_topic_key_raises(self):
        block = {"annexures": [{"letter": "X"}]}
        with pytest.raises(ValueError) as exc:
            parse_report_annexures(block)
        assert "topic" in str(exc.value)

    def test_non_list_annexures_raises(self):
        block = {"annexures": "not a list"}
        with pytest.raises(ValueError) as exc:
            parse_report_annexures(block, yaml_path="/x/p.yaml")
        assert "list" in str(exc.value).lower()
        assert "/x/p.yaml" in str(exc.value)

    def test_non_dict_entry_raises(self):
        block = {"annexures": ["just a string"]}
        with pytest.raises(ValueError) as exc:
            parse_report_annexures(block)
        assert "mapping" in str(exc.value) or "dict" in str(exc.value).lower()

    def test_letter_with_whitespace_stripped(self):
        block = {"annexures": [
            {"topic": "guidelines_formulas", "letter": "  Q  "},
        ]}
        out = parse_report_annexures(block)
        assert out == [("guidelines_formulas", "Q")]

    def test_empty_letter_falls_back_to_default(self):
        # `letter: ""` and `letter: "   "` both fall back to default.
        for letter_val in ("", "   "):
            block = {"annexures": [
                {"topic": "guidelines_formulas", "letter": letter_val},
            ]}
            out = parse_report_annexures(block)
            assert out == [("guidelines_formulas", "A")]


# ---------------------------------------------------------------------------
# serialize_report_annexures
# ---------------------------------------------------------------------------

class TestSerializeReportAnnexures:
    def test_round_trip_preserves_order_and_letters(self):
        sel = [
            ("guidelines_formulas",   "A"),
            ("qa_findings",           "Z"),
            ("results_ili_comparison", "B"),
        ]
        rendered = serialize_report_annexures(sel)
        # parse it back -> same selection
        roundtrip = parse_report_annexures(rendered)
        assert roundtrip == sel

    def test_renders_explicit_letters_even_when_default(self):
        # Spec: emit letters explicitly so future registry default
        # changes don't shift saved YAMLs.
        sel = [("guidelines_formulas", "A")]
        rendered = serialize_report_annexures(sel)
        assert rendered["annexures"][0]["letter"] == "A"


# ---------------------------------------------------------------------------
# Project.from_yaml round-trip
# ---------------------------------------------------------------------------

class TestProjectFromYamlReportBlock:
    """End-to-end: write a YAML with various ``report.annexures``
    shapes, load it via ``Project.from_yaml``, confirm
    ``project.report_annexures`` matches the spec."""

    @staticmethod
    def _write_yaml(tmp_path, report_block):
        data = {
            "project": {"project_name": "T"},
            "pipeline": {"diameter_mm": 273.0, "length_km": 50.0,
                         "material_grade": "API 5L X52", "smys_mpa": 358.0},
            "maop_zones": [{"wt_mm_min": 6.0, "wt_mm_max": 8.0,
                            "design_factor": 0.72, "maop_kgcm2": 70.0}],
            "runs": {
                "run_1": {"file_path": "r1.xlsx"},
                "run_2": {"file_path": "r2.xlsx"},
            },
        }
        if report_block is not None:
            data["report"] = report_block
        path = tmp_path / "p.yaml"
        path.write_text(pyyaml.safe_dump(data, sort_keys=False),
                        encoding="utf-8")
        return path

    def test_no_report_block_loads_legacy_default(self, tmp_path):
        path = self._write_yaml(tmp_path, None)
        proj = Project.from_yaml(str(path))
        assert proj.report_annexures == default_annexure_selection()

    def test_explicit_block_preserves_order(self, tmp_path):
        path = self._write_yaml(tmp_path, {"annexures": [
            {"topic": "guidelines_formulas",   "letter": "A"},
            {"topic": "qa_findings",           "letter": "B"},
            {"topic": "results_ili_comparison", "letter": "C"},
        ]})
        proj = Project.from_yaml(str(path))
        assert proj.report_annexures == [
            ("guidelines_formulas", "A"),
            ("qa_findings", "B"),
            ("results_ili_comparison", "C"),
        ]

    def test_load_save_roundtrip(self, tmp_path):
        """Load -> serialize -> parse -> identical."""
        original = [
            ("guidelines_formulas", "A"),
            ("estimated_erf_defects", "C2"),
            ("qa_findings", "Z"),
        ]
        path = self._write_yaml(tmp_path, serialize_report_annexures(original))
        proj = Project.from_yaml(str(path))
        assert proj.report_annexures == original

    def test_invalid_yaml_raises_at_load(self, tmp_path):
        path = self._write_yaml(tmp_path, {"annexures": [
            {"topic": "unknown_id"},
        ]})
        with pytest.raises(ValueError):
            Project.from_yaml(str(path))

    def test_kandla_example_loads_with_legacy_default(self):
        """examples/kandla_project.yaml has no `report.annexures`
        block. Backward-compat: loading it via the v0.2.5 model
        produces the legacy 3-topic default."""
        proj = Project.from_yaml("examples/kandla_project.yaml")
        assert proj.report_annexures == default_annexure_selection()
